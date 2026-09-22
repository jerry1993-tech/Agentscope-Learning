# -*- coding: utf-8 -*-
"""评测指标函数（契约 §3.20 / §5.6，第 20 讲）。

契约给的指标签名是 ``MetricFn = Callable[[EvalCase, str], Awaitable[float]]``
—— 只有「用例」和「最终输出文本」两个入参。这对 ``exact_match`` /
``contains`` 足够，但 :func:`tool_call_accuracy` 与 :func:`latency_score`
需要的证据（这一轮调了哪些工具、跑了多久、烧了多少 token）**不在那句
输出文本里**。

本模块的解法是 :data:`_OBSERVED_RUN` 这个 ``ContextVar``：

- :class:`~harness_kit.eval.runner.EvalRunner` 在跑完一个用例后，把该轮的
  :class:`ObservedRun` 设进当前执行上下文，再依次 ``await`` 每个指标；
- 指标函数用 :func:`current_run` 取它，取不到（例如单独单测某个指标）
  就退化成"只看 ``output`` 文本"或返回"不适用"。

``ContextVar`` 而不是"给指标多传一个参数"，理由有两条：一是签名必须与契约
逐字一致（教程里的指标函数要能直接互相替换）；二是 ``ContextVar`` 对
``asyncio`` 任务是隔离的，并发跑用例时 A 用例的指标绝不会读到 B 用例的
证据 —— 这一点在 :class:`EvalRunner` 的并发验证里是硬要求。

**"不适用"的约定**：当用例没有给出某项期望（``expected`` / ``expected_tools`` /
``expected_citations`` 为空）时，对应指标返回 :data:`NOT_APPLICABLE`（**-1.0**），
而不是 0.0，也不是跳过。理由：0.0 会把"没考这一项"算成"考砸了"，
跳过则会让不同用例的 ``scores`` 字典长度不一，报告没法对齐成表。
**为什么哨兵取 -1.0 而不是 1.0**：指标分数量纲是 ``[0, 1]``，负数天然出界，
于是"这一项没考"和"这一项考了满分"可以被无歧义地区分开 ——
用 1.0 当哨兵时两者混在一起，聚合均值必然算错（真实踩过）。
**代价**：``EvalReport.summary`` 里的均值是"已考项"的均值，所以它同时给出
``*_applicable`` 计数，报告里必须一起看。

Example:
    >>> import asyncio
    >>> case = EvalCase(id="c1", input="2+2", expected="4")
    >>> asyncio.run(exact_match(case, "答案是 4"))
    0.0
    >>> asyncio.run(contains(case, "答案是 4"))
    1.0
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.eval.dataset import EvalCase
from harness_kit.session.replay import TokenUsage

__all__ = [
    "CitationMarkerPattern",
    "DEFAULT_LATENCY_BUDGET_MS",
    "DEFAULT_THRESHOLDS",
    "MetricFn",
    "NOT_APPLICABLE",
    "ObservedRun",
    "citation_coverage",
    "contains",
    "current_run",
    "exact_match",
    "latency_score",
    "llm_judge",
    "make_llm_judge",
    "observed_run",
    "score_summary",
    "tool_call_accuracy",
]

NOT_APPLICABLE: float = -1.0
"""指标"不适用"时的哨兵值（负数，出 ``[0,1]`` 量纲；见模块 docstring）。"""

DEFAULT_LATENCY_BUDGET_MS: float = 20_000.0
"""默认延迟预算：单用例端到端 20s。本环境实测 ``deepseek-flash`` 带 2~3 次
工具调用的问答在 5s~15s 之间，20s 是一个不会天天报假警的阈值。"""

_PUNCT_RE: re.Pattern[str] = re.compile(r"[\s，。、；：！？,.;:!?\"'`（）()\[\]【】]+")
"""做"宽松相等"时要抹掉的空白与标点。"""

DEFAULT_THRESHOLDS: dict[str, float] = {
    "exact_match": 1.0,
    "contains": 1.0,
    "citation_coverage": 0.5,
    "tool_call_accuracy": 0.5,
    "latency_score": 0.5,
}
"""各指标的默认通过门槛。

放在 :mod:`harness_kit.eval.metrics` 而不是 ``runner`` / ``report`` 里，
是因为这两边都要用（runner 判 ``ok``、report 渲染"门槛"列），
而 metrics 谁都不依赖 —— 写在其中一边会让另一边产生循环 import。
自定义门槛的口径是「分数越高越好」，因此"越低越好"的指标（例如延迟）
只能通过 :func:`latency_score` 这种已经翻过方向的得分来表达。
"""

CitationMarkerPattern: str = r"\[(\d+)\]"
"""引用标记的正则（正文里写成 ``[1]`` ``[2]``）。"""


class ObservedRun(BaseModel):
    """一个评测用例的"运行证据"（:data:`_OBSERVED_RUN` 的载荷）。

    这些字段**不是**契约 §5.6 的一部分，它们是 harness_kit 为了让
    ``tool_call_accuracy`` / ``latency_score`` 这类指标真的可算而补的旁路。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    """用例 id。"""

    output: str = ""
    """Agent 的最终输出文本。"""

    tool_calls: list[str] = Field(default_factory=list)
    """本轮实际调用的工具名（按发生顺序，含重复）。"""

    tool_errors: list[str] = Field(default_factory=list)
    """失败的工具调用（``"tool_name: error"`` 形态）。"""

    latency_ms: float = 0.0
    """端到端耗时（毫秒）。"""

    tokens: TokenUsage = Field(default_factory=TokenUsage)
    """本轮 token 用量。"""

    iterations: int = 0
    """ReAct 循环轮数。"""

    model: str = ""
    """模型名（成本核算用）。"""

    error: str | None = None
    """运行期异常；``None`` 表示这一轮正常收尾。"""

    def unique_tools(self) -> list[str]:
        """去重后的工具名（保序）。

        Returns:
            `list[str]`: 去重工具名。
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for name in self.tool_calls:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered


_OBSERVED_RUN: ContextVar[ObservedRun | None] = ContextVar("harness_kit_eval_run", default=None)
"""当前用例的运行证据（见模块 docstring）。"""

MetricFn = Callable[[EvalCase, str], Awaitable[float]]
"""指标函数签名（契约 §3.20，逐字一致）。"""


def current_run() -> ObservedRun:
    """返回当前用例的运行证据；不在评测上下文里时返回空证据。

    Returns:
        `ObservedRun`: 证据对象（可能是 ``case_id=""`` 的空对象）。
    """
    return _OBSERVED_RUN.get() or ObservedRun(case_id="")


@contextmanager
def observed_run(run: ObservedRun) -> Iterator[ObservedRun]:
    """把一份运行证据设为当前上下文（:class:`EvalRunner` 用）。

    Args:
        run (`ObservedRun`): 证据。

    Yields:
        `ObservedRun`: 同一个对象。
    """
    token = _OBSERVED_RUN.set(run)
    try:
        yield run
    finally:
        _OBSERVED_RUN.reset(token)


# ======================================================================
# 文本类指标
# ======================================================================
def _normalize(text: str) -> str:
    """抹掉空白与标点并转小写（宽松比较用）。

    Args:
        text (`str`): 原文。

    Returns:
        `str`: 归一化文本。
    """
    return _PUNCT_RE.sub("", text).lower()


async def exact_match(case: EvalCase, output: str) -> float:
    """严格/宽松完全匹配。

    ``case.metadata["loose"] == True`` 时先归一化（抹空白与标点、转小写）
    再比，用于"语义对、标点不同"的场景；默认走严格相等。

    Args:
        case (`EvalCase`): 用例。
        output (`str`): Agent 输出。

    Returns:
        `float`: ``1.0`` 命中 / ``0.0`` 未命中；``case.expected is None``
        时返回 :data:`NOT_APPLICABLE`。
    """
    if case.expected is None:
        return NOT_APPLICABLE
    if case.metadata.get("loose"):
        hit = _normalize(output) == _normalize(case.expected)
    else:
        hit = output.strip() == case.expected.strip()
    return 1.0 if hit else 0.0


async def contains(case: EvalCase, output: str) -> float:
    """子串包含（大小写不敏感）。

    ``case.metadata["keywords"]`` 给成列表时，改成算"关键词命中率"，
    这样一条用例能表达"答案里必须出现 A、B、C"。

    Args:
        case (`EvalCase`): 用例。
        output (`str`): Agent 输出。

    Returns:
        `float`: 命中率，``[0, 1]``；没有任何期望时返回 :data:`NOT_APPLICABLE`。
    """
    keywords = case.metadata.get("keywords")
    if keywords:
        wanted = [str(item) for item in keywords]
        if not wanted:
            return NOT_APPLICABLE
        lowered = output.lower()
        hits = sum(1 for item in wanted if item.lower() in lowered)
        return hits / len(wanted)
    if case.expected is None:
        return NOT_APPLICABLE
    return 1.0 if case.expected.lower() in output.lower() else 0.0


async def citation_coverage(case: EvalCase, output: str) -> float:
    """引用覆盖率：期望的来源是否都出现在输出里。

    判定分两步，先严后宽：

    1. 期望路径（``case.expected_citations``，如 ``"agent/_agent.py"``）
       直接作为子串出现在输出里 —— 这是"答得有出处"的强证据；
    2. 若输出里带了 ``[n]`` 形式的引用标记，且期望路径出现在引用块附近
       （**注意**：本实现不解析引用块位置，只看全文；这是有意的简化，
       正文里必须说明，否则读者会以为它做了引用溯源）。

    ``case.metadata["require_citation"] == True`` 且用例**没有**给期望路径时，
    退化为"输出里至少有一个 ``[n]`` 标记"。

    Args:
        case (`EvalCase`): 用例。
        output (`str`): Agent 输出。

    Returns:
        `float`: 命中比例，``[0, 1]``；无任何引用要求时返回
        :data:`NOT_APPLICABLE`。
    """
    if case.expected_citations:
        wanted = [item for item in case.expected_citations if item]
        hits = sum(1 for item in wanted if item in output or _basename(item) in output)
        return hits / len(wanted)
    if case.metadata.get("require_citation"):
        return 1.0 if re.search(CitationMarkerPattern, output) else 0.0
    return NOT_APPLICABLE


def _basename(path: str) -> str:
    """取路径最后一段（``a/b/c.py`` → ``c.py``）。

    Args:
        path (`str`): 路径。

    Returns:
        `str`: 最后一段；没有分隔符时原样返回。
    """
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


async def tool_call_accuracy(case: EvalCase, output: str) -> float:
    """工具调用正确率（F1 视角的"该调的调了、不该调的没调"）。

    判定依据是 :func:`current_run` 里的 ``tool_calls``（由 ``EvalRunner``
    从 AgentScope 的 ``ToolCallStartEvent`` 流里收集，**不是**从输出文本里猜的）。
    不在评测上下文里时退化为 :data:`NOT_APPLICABLE`。

    打分规则：

    - ``expected`` 为空 → :data:`NOT_APPLICABLE`；
    - 期望的工具**全部**被调用 → 得分 = ``1 / (1 + 多余调用数 * 0.25)``，
      即多调一个扣一点，但不会因为多调就归零；
    - 漏调 \(k\) 个 → 得分 = ``召回率``（``命中数 / 期望数``）再乘同样的多余调用惩罚。

    Args:
        case (`EvalCase`): 用例。
        output (`str`): Agent 输出（本指标不读它，但签名必须一致）。

    Returns:
        `float`: ``[0, 1]``；不适用时 :data:`NOT_APPLICABLE`。
    """
    if not case.expected_tools:
        return NOT_APPLICABLE
    run = _OBSERVED_RUN.get()
    if run is None:
        logger.debug("tool_call_accuracy: 不在评测上下文里（case={}），返回不适用", case.id)
        return NOT_APPLICABLE

    expected = list(dict.fromkeys(case.expected_tools))
    observed = run.unique_tools()
    hits = sum(1 for name in expected if name in observed)
    recall = hits / len(expected)
    extras = max(0, len(observed) - hits)
    return round(recall / (1.0 + extras * 0.25), 6)


async def latency_score(case: EvalCase, output: str) -> float:
    """延迟得分：在预算内得 1，超时按超出比例线性衰减到 0。

    判定依据同样是 :func:`current_run`。预算取
    ``case.metadata["latency_budget_ms"]``，缺省
    :data:`DEFAULT_LATENCY_BUDGET_MS`。惩罚规则：超出预算后，每超出
    一个"预算长度"扣一半，即 ``score = max(0, 1 - (latency - budget) / (2 * budget))``。
    这条曲线让"稍微超一点"不至于判死，"超一倍"才归零。

    Args:
        case (`EvalCase`): 用例。
        output (`str`): Agent 输出（本指标不读它）。

    Returns:
        `float`: ``[0, 1]``；不在评测上下文里时 :data:`NOT_APPLICABLE`。
    """
    run = _OBSERVED_RUN.get()
    if run is None:
        return NOT_APPLICABLE
    budget = float(case.metadata.get("latency_budget_ms", DEFAULT_LATENCY_BUDGET_MS))
    if budget <= 0:
        raise ValueError(f"latency_budget_ms 必须为正，收到 {budget}")
    if run.latency_ms <= budget:
        return 1.0
    return round(max(0.0, 1.0 - (run.latency_ms - budget) / (2.0 * budget)), 6)


# ======================================================================
# LLM-as-judge（可选）
# ======================================================================
JUDGE_PROMPT: str = """你是一个严格的评测员。请给下面这条问答打分。

【用户问题】
{input}

【参考答案】
{expected}

【待评回答】
{output}

{criteria}

打分规则：只输出一个 0 到 10 的整数（10 = 完全正确且完整，0 = 完全错误）。
不要输出任何其它文字、不要解释、不要加标点。"""


def _judge_score(text: str) -> float:
    """从裁判模型的回复里抽出 0~10 的分数。

    Args:
        text (`str`): 模型原始回复。

    Returns:
        `float`: 归一化到 ``[0, 1]`` 的分数。

    Raises:
        `ValueError`: 回复里找不到 0~10 的整数。
    """
    match = re.search(r"\b(10|[0-9])\b", text)
    if match is None:
        raise ValueError(f"裁判回复里没有 0~10 的整数: {text[:200]!r}")
    return min(10, max(0, int(match.group(1)))) / 10.0


def make_llm_judge(
    model: Any,
    *,
    name: str = "llm_judge",
    criteria: str = "",
) -> MetricFn:
    """构造一个 LLM-as-judge 指标（契约 §3.20 里"LLM-as-judge 可选"那一项）。

    只调用传入的 ``ChatModelBase``（``await model(messages=...)``，
    真实签名见 ``third_party/agentscope/src/agentscope/model/_base.py:182`` 起），
    不自己造模型、不自己实现 HTTP。要求 ``case.expected`` 非空，
    否则返回 :data:`NOT_APPLICABLE`。

    裁判模型的回复**必须是 0~10 的整数**；解析不出来时抛
    :class:`ValueError` 让 ``EvalRunner`` 把这条用例记成 ``error``，
    而不是静默给 0 分（静默 0 分会让人以为是模型答错了）。

    Args:
        model (`Any`): 任意 ``ChatModelBase`` 实例。
        name (`str`): 指标名（进 ``scores`` 字典）。
        criteria (`str`): 追加的评分细则，会插进 prompt。

    Returns:
        `MetricFn`: 异步指标函数；``__name__`` 被设成 ``name``，
        这样 ``scores`` 的键就是指标名。

    Example:
        >>> judge = make_llm_judge(None, name="correctness")   # 只检查构造
        >>> judge.__name__
        'correctness'
    """
    from agentscope.message import Msg, TextBlock

    async def _judge(case: EvalCase, output: str) -> float:
        if case.expected is None:
            return NOT_APPLICABLE
        prompt = JUDGE_PROMPT.format(
            input=case.input,
            expected=case.expected,
            output=output,
            criteria=criteria or "",
        )
        response = await model(
            messages=[Msg(name="judge", role="user", content=[TextBlock(type="text", text=prompt)])],
        )
        text = _collect_text(response)
        return _judge_score(text)

    _judge.__name__ = name
    return _judge


def _collect_text(response: Any) -> str:
    """从 ``ChatResponse``（或其异步生成器）里取出全部文本。

    Args:
        response (`Any`): ``ChatResponse``；若模型 ``stream=True``，
            给到的是异步生成器，会在这里被耗尽。

    Returns:
        `str`: 拼接后的文本。
    """
    chunks: list[str] = []

    def _take(chunk: Any) -> None:
        blocks = getattr(chunk, "content", None) or []
        for block in blocks:
            text = getattr(block, "text", None)
            if text:
                chunks.append(str(text))

    if hasattr(response, "__aiter__"):
        raise TypeError(
            "llm_judge 拿到的是流式响应（异步生成器）。请在构造裁判模型时用 "
            "stream=False，或在调用处先把流耗尽 —— 指标函数是 async 的，"
            "但这里不做隐式 await，避免把'模型在流式'这件事藏起来。",
        )
    _take(response)
    return "".join(chunks)


async def llm_judge(case: EvalCase, output: str) -> float:
    """占位式入口：**必须**先用 :func:`make_llm_judge` 绑定模型。

    保留这个函数是为了在教程里说明"契约 §3.20 只写了『LLM-as-judge 可选』，
    没有写签名"，因此本实现把它做成工厂而不是固定函数。

    Args:
        case (`EvalCase`): 用例。
        output (`str`): Agent 输出。

    Raises:
        `RuntimeError`: 总是抛 —— 直接调用没有任何可用模型。
    """
    raise RuntimeError(
        "llm_judge 需要先绑定裁判模型：judge = make_llm_judge(model, name='correctness')；"
        "然后把 judge 放进 EvalRunner.run(..., metrics=[judge])。",
    )


def score_summary(
    scores_by_case: Sequence[dict[str, float]],
    *,
    names: Sequence[str] | None = None,
) -> dict[str, float]:
    """把"每条用例的 scores"聚合成"每个指标的均值 + 适用条数"。

    Args:
        scores_by_case (`Sequence[dict[str, float]]`): 每条用例的得分。
        names (`Sequence[str] | None`): 显式指定指标名顺序；``None`` 时取并集。

    Returns:
        `dict[str, float]`: ``{"<metric>": mean}`` 与 ``{"<metric>_applicable": n}``。
    """
    keys = list(names) if names is not None else sorted(
        {key for scores in scores_by_case for key in scores},
    )
    out: dict[str, float] = {}
    for key in keys:
        values = [
            float(scores[key])
            for scores in scores_by_case
            if key in scores and float(scores[key]) >= 0.0
        ]
        out[key] = round(sum(values) / len(values), 6) if values else 0.0
        out[f"{key}_applicable"] = float(len(values))
    return out
