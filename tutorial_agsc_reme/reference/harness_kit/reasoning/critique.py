# -*- coding: utf-8 -*-
"""自我批判循环：generate → critique → revise（契约 §3.14，第 14 讲）。

**为什么要有「同一件事再做一遍」这种听起来很蠢的循环**：一次性生成的产物
质量方差极大 —— 同一个 prompt，第 1 次比第 3 次差是常态。而**批判比生成容易**：
判断「这段代码有没有处理空输入」比「写出这段代码」简单得多，这个不对称性
正是 Self-Refine 那一类方法能work的全部理由。所以把「产出一版」和「挑毛病」
分成两次调用，用挑出的毛病去驱动下一版，比「让模型一次写好」更划算。

**但必须有三道闸**，否则它就是一个烧钱的死循环：

1. **判据必须是结构化的**（``score`` + ``passed`` + ``issues``），不是「模型觉得
   还行」。自由文本的自我批判极易退化成「我再夸它一遍」—— 因为没有明确的
   停止条件，模型会在「差不多好了」和「还能更好」之间无限摇摆。
   本模块用 :class:`~harness_kit.reasoning.structured.CallStructured` 拿判据，
   所以判据**一定**是合法对象（schema 校验不过就抛错，不会静默当成通过）。
2. **轮数硬上限**（``max_rounds``）。到顶就交最后一版，并**诚实地把
   ``finished=False`` 报出来** —— 把「没收敛」这件事暴露给上层，由上层决定
   是接受、是换人、还是升级给人。偷偷返回最后一版当成功，是最坏的做法：
   它让「质量不达标」在监控里完全隐形。
3. **分数要落盘**（``CritiqueResult.scores``）。只有分数序列能回答
   「加了这一轮批判到底有没有用」——单看最终产物看不出来。

**revise 用的是同一个 ``Agent`` 实例**，所以它带着完整上下文（原任务 + 上一版 +
批判意见），而不是在真空里重写。这也是为什么本模块拿的是 ``Agent`` 而不是
``ChatModelBase``：工作区、工具、记忆都在 Agent 身上。
"""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg
from agentscope.model import ChatModelBase

from harness_kit.reasoning.structured import CallStructured

__all__ = [
    "CRITIQUE_PROMPT",
    "REVISE_PROMPT",
    "CritiqueLoop",
    "CritiqueResult",
    "CritiqueVerdict",
]

GENERATE_PROMPT = """\
{task}"""

CRITIQUE_PROMPT = """\
You are reviewing a candidate answer to a task. Be a demanding reviewer, not a \
cheerleader: a review that finds nothing wrong is only useful when there is \
genuinely nothing wrong.

## The task

{task}

## The candidate answer

{candidate}

## How to review

1. Check the answer against **the task as literally written** first — wrong \
deliverable, missing required item, or answering a neighbouring question are the \
most common fatal flaws.
2. Then check correctness, completeness, and whether any claim is unsupported.
3. Score 0.0–1.0, where 1.0 means "a demanding reviewer would ship this as-is".
   Do not give 0.9+ for "good enough with caveats" — list the caveats as issues \
instead and score accordingly.
4. Set `passed` to true only if the answer could be shipped without edits. \
`score` and `passed` must agree: a passing answer needs a score at or above the \
acceptance bar, and any issue that requires an edit means `passed` is false.
5. Every issue you list must be **actionable** — say what is missing or wrong \
and where, never "could be improved"."""

REVISE_PROMPT = """\
A reviewer rejected your previous answer. Rewrite it, fixing every issue below. \
Keep what was already correct — a rewrite that fixes the issues but breaks \
something else is a worse answer.

## The original task

{task}

## Your previous answer

{candidate}

## Reviewer's issues

{issues}

## Reviewer's suggestion

{suggestion}

Write the revised answer only. Do not explain what you changed."""


class CritiqueVerdict(BaseModel):
    """一次批判的判据（结构化，由 schema 校验保证合法）。

    Attributes:
        score (`float`): 0.0~1.0 的质量分。
        passed (`bool`): 是否可以直接交付。
        issues (`list[str]`): 必须具体到「哪里缺什么」，不能是「可以更好」。
        suggestion (`str`): 一句话的改进方向。
    """

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    issues: list[str] = Field(default_factory=list)
    suggestion: str = ""

    @field_validator("issues")
    @classmethod
    def _clean_issues(cls, value: list[str]) -> list[str]:
        """去掉空条目（模型经常吐一个空字符串凑数）。

        Args:
            value (`list[str]`): 原始 issues。

        Returns:
            `list[str]`: 去空后的列表。
        """
        return [_.strip() for _ in value if _.strip()]


class CritiqueResult(BaseModel):
    """批判循环的结果（契约 §3.14）。

    Attributes:
        output (`str`): 最后一版产物。
        rounds (`int`): 实际发生的批判轮数（0 表示一次都没批 —— 只可能出现在
            ``max_rounds=0`` 的非法配置里，正常路径至少 1）。
        finished (`bool`): 是否**因为通过而**结束。``False`` = 轮数用尽仍未通过，
            产物是「最好的努力」而不是「合格的交付」。
        scores (`list[float]`): 每轮的分数，按轮次排列。上升 / 下降 / 抖动
            分别对应三种完全不同的处理方式，所以必须保留序列而不是只留最后一个。
        verdicts (`list[CritiqueVerdict]`): 每轮的完整判据。
    """

    model_config = ConfigDict(extra="forbid")

    output: str
    rounds: int
    finished: bool
    scores: list[float] = Field(default_factory=list)
    verdicts: list[CritiqueVerdict] = Field(default_factory=list)

    @property
    def last_score(self) -> float | None:
        """最后一轮的分数。

        Returns:
            `float | None`: 没有轮次时为 ``None``。
        """
        return self.scores[-1] if self.scores else None

    @property
    def trend(self) -> str:
        """分数趋势。

        Returns:
            `str`: ``"up"`` / ``"down"`` / ``"flat"`` / ``"none"``。
            连续下降是「该换人而不是再改一轮」的信号。
        """
        if len(self.scores) < 2:
            return "none"
        delta = self.scores[-1] - self.scores[0]
        if delta > 1e-9:
            return "up"
        if delta < -1e-9:
            return "down"
        return "flat"

    def summary(self, *, limit: int = 120) -> str:
        """单行摘要。

        Args:
            limit (`int`, defaults to `120`): 产物截断长度。

        Returns:
            `str`: 形如 ``"[finished] 2 轮 scores=[0.40, 0.90] 趋势=up :: ..."``。
        """
        body = self.output if len(self.output) <= limit else self.output[:limit] + "…"
        return (
            f"[{'finished' if self.finished else 'unfinished'}] "
            f"{self.rounds} 轮 scores={[round(_, 3) for _ in self.scores]} "
            f"趋势={self.trend} :: {body}"
        )


class CritiqueLoop:
    """生成-批判-修订循环（契约 §3.14）。

    Args:
        agent (`Agent`): **同一个** Agent 负责生成与修订（它带着工作区、工具
            与上下文）。
        max_rounds (`int`, defaults to `3`): 最大批判轮数。**3 是经验值**：
            第 2 轮通常能把明显缺陷修掉，第 3 轮之后的收益急剧衰减，而每一轮
            的成本是「一次生成 + 一次批判」两次完整调用。
        acceptance_score (`float`, defaults to `0.8`): 分数达到它就停。
            与 ``verdict.passed`` 是**或**关系：模型说通过、或者分数够高，
            都算收敛。
        critique_model (`ChatModelBase | None`, optional): 用来出判据的模型。
            ``None`` 时用 ``agent.model``。**为什么允许不同**：批判比生成简单，
            生产里常用便宜模型做批判（省钱）；评测里也常用不同的模型来避免
            「自己批自己一律通过」的同源偏差。

    Raises:
        ValueError: ``max_rounds < 1``，或 ``acceptance_score`` 不在 ``[0, 1]``。
    """

    def __init__(
        self,
        *,
        agent: Agent,
        max_rounds: int = 3,
        acceptance_score: float = 0.8,
        critique_model: ChatModelBase | None = None,
    ) -> None:
        """初始化。"""
        if max_rounds < 1:
            raise ValueError(
                f"max_rounds 必须 >= 1，收到 {max_rounds}。"
                "0 会让 run() 直接返回未批判的第一版，那不是「自我批判」，"
                "那是「假装批判」。",
            )
        if not 0.0 <= acceptance_score <= 1.0:
            raise ValueError(
                f"acceptance_score 必须在 [0, 1]，收到 {acceptance_score}。",
            )
        self.agent = agent
        self.max_rounds = int(max_rounds)
        self.acceptance_score = float(acceptance_score)
        self.critique_model = critique_model or agent.model

    # ==================================================================
    # 主循环
    # ==================================================================
    async def run(self, task: str) -> CritiqueResult:
        """跑完整循环。

        Args:
            task (`str`): 任务文本。

        Returns:
            `CritiqueResult`: 结果。**任何一步失败都不抛异常** —— 批判循环是
            「尽力提升质量」的环节，把它变成异常源会让上层编排更难写。代价是
            调用方必须看 ``finished``。

        Raises:
            ValueError: ``task`` 为空。
        """
        if not task or not task.strip():
            raise ValueError("task 不能为空。")

        critic = CallStructured(
            model=self.critique_model,
            schema=CritiqueVerdict,
            tool_name="emit_critique",
        )

        output = await self._generate(task)
        scores: list[float] = []
        verdicts: list[CritiqueVerdict] = []
        finished = False

        for round_index in range(1, self.max_rounds + 1):
            verdict = await self._critique(critic, task, output)
            verdicts.append(verdict)
            scores.append(verdict.score)
            logger.info(
                "critique 第 {}/{} 轮：score={:.2f} passed={} issues={}",
                round_index,
                self.max_rounds,
                verdict.score,
                verdict.passed,
                len(verdict.issues),
            )

            if verdict.passed or verdict.score >= self.acceptance_score:
                finished = True
                break
            if round_index == self.max_rounds:
                # 轮数用尽：**保留最后一版并诚实报 finished=False**。
                logger.warning(
                    "critique 用尽 {} 轮仍未通过（最后 {:.2f} < {:.2f}）",
                    self.max_rounds,
                    verdict.score,
                    self.acceptance_score,
                )
                break
            output = await self._revise(task, output, verdict)

        return CritiqueResult(
            output=output,
            rounds=len(scores),
            finished=finished,
            scores=scores,
            verdicts=verdicts,
        )

    # ==================================================================
    # 三步
    # ==================================================================
    async def _generate(self, task: str) -> str:
        """第一版产物。

        Args:
            task (`str`): 任务文本。

        Returns:
            `str`: 产物文本。
        """
        msg = await self.agent.reply(
            UserMsg("user", GENERATE_PROMPT.format(task=task)),
        )
        return _text_of(msg)

    async def _critique(
        self,
        critic: CallStructured,
        task: str,
        candidate: str,
    ) -> CritiqueVerdict:
        """批判一版产物。

        Args:
            critic (`CallStructured`): 判据抽取器。
            task (`str`): 任务文本。
            candidate (`str`): 待批判的产物。

        Returns:
            `CritiqueVerdict`: 判据。抽取失败时返回一个 ``passed=False`` 且
            ``score=0`` 的保守判据 —— **绝不**把它当通过（「批判失败」必须
            偏保守，否则模型一抽风，坏产物就被放行了）。
        """
        try:
            verdict = await critic.run(
                [UserMsg(
                    "user",
                    CRITIQUE_PROMPT.format(task=task, candidate=candidate),
                )],
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("批判本身失败了：{}，保守判定为不通过。", exc)
            return CritiqueVerdict(
                score=0.0,
                passed=False,
                issues=[f"批判步骤本身失败：{type(exc).__name__}: {exc}"],
                suggestion="",
            )
        return verdict  # type: ignore[return-value]

    async def _revise(
        self,
        task: str,
        candidate: str,
        verdict: CritiqueVerdict,
    ) -> str:
        """按批判意见重写。

        Args:
            task (`str`): 任务文本。
            candidate (`str`): 上一版。
            verdict (`CritiqueVerdict`): 判据。

        Returns:
            `str`: 新一版。
        """
        issues = "\n".join(f"- {_}" for _ in verdict.issues) or "- （批判没给具体问题）"
        msg = await self.agent.reply(
            UserMsg(
                "user",
                REVISE_PROMPT.format(
                    task=task,
                    candidate=candidate,
                    issues=issues,
                    suggestion=verdict.suggestion or "（无）",
                ),
            ),
        )
        return _text_of(msg)

    # ==================================================================
    # 观测
    # ==================================================================
    def describe(self) -> str:
        """一行描述。

        Returns:
            `str`: 形如 ``"CritiqueLoop(agent=writer, max_rounds=3, accept=0.80)"``。
        """
        return (
            f"CritiqueLoop(agent={self.agent.name}, "
            f"max_rounds={self.max_rounds}, "
            f"accept={self.acceptance_score:.2f})"
        )


def _text_of(msg: Msg) -> str:
    """从 Agent 的回复里取纯文本。

    ``get_text_content()``（``third_party/agentscope/src/agentscope/message/_base.py:156``）
    会跳过 ``ThinkingBlock`` / ``ToolCallBlock``，只拼 ``TextBlock`` ——
    这一点很重要：思考模型的回复里 ``ThinkingBlock`` 往往比正文长，
    直接拼会把「模型的内心戏」当成产物交给批判者。

    Args:
        msg (`Msg`): Agent 的最终消息。

    Returns:
        `str`: 文本；没有文本块时为占位说明（**不返回空串**：空产物会让批判
        步骤失去意义，而「模型只调了工具没说话」是真实情况）。
    """
    text = msg.get_text_content() or ""
    if not text.strip():
        return "（模型这一版没有产出文本内容，只产生了工具调用或思考。）"
    return text
