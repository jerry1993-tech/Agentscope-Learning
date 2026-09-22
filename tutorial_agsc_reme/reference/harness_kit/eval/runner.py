# -*- coding: utf-8 -*-
"""评测运行器（``EvalRunner`` / ``EvalResult``，契约 §3.20，第 20 讲）。

职责只有一条：**把一批用例并发地喂给一个 Agent，把每条的证据收干净，
把得分算出来，把结果交给报告层**。它不做三件事：

1. **不自己造 Agent** —— 由调用方传 ``agent_factory``（契约 §3.20 的签名就是
   ``Callable[[], Awaitable[Agent]]``）。这样评测才能覆盖第 2 讲的
   ``HarnessBuilder`` 装配出来的真实 Agent，而不是另起一套。
2. **不自己实现 Agent Loop** —— 用的是 AgentScope 的
   ``Agent.reply_stream``（``third_party/agentscope/src/agentscope/agent/_agent.py:288``），
   这里只消费它 yield 出来的 ``AgentEvent``。
3. **不自己算 token** —— 从 ``ModelCallEndEvent``
   （``third_party/agentscope/src/agentscope/event/_event.py:139``）的
   ``input_tokens`` / ``output_tokens`` 累加，成本折算复用
   ``harness_kit.models.pricing.cost_of``。

**并发语义**：``concurrency`` 个用例同时在跑，**单用例失败绝不中断整体**
（契约 §3.20 的原话）。失败有两条路径 —— 超时 / 异常 —— 二者都会被记成
``EvalResult(ok=False, error=...)``，而不是往上抛。

**每个用例一个 Agent**：``agent_factory`` 每跑一条用例就被调用一次，因为
"上下文隔离"是评测的前提（共用 Agent 会把上一条的问答带进下一条，
``exact_match`` 立刻失真）。代理对象的清理走
:meth:`EvalRunner.aclose`：优先调 ``agent.close()`` / ``aclose()``，
再退到 ``agent.model.close()``，都没有就放弃（不报错）。

Example:
    >>> from harness_kit.eval.dataset import EvalCase, EvalDataset
    >>> dataset = EvalDataset(name="t", cases=[EvalCase(id="c1", input="hi")])
    >>> runner = EvalRunner(agent_factory=my_agent_factory)   # doctest: +SKIP
    >>> report = asyncio.run(runner.run(dataset, metrics=[]))  # doctest: +SKIP
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.eval.dataset import EvalCase, EvalDataset
from harness_kit.eval.metrics import (
    DEFAULT_THRESHOLDS,
    MetricFn,
    ObservedRun,
    current_run,
    observed_run,
)
from harness_kit.session.replay import TokenUsage

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from agentscope.agent import Agent

    from harness_kit.eval.report import EvalReport

__all__ = ["AgentFactory", "EvalResult", "EvalRunner", "collect_observed_run"]

AgentFactory = Callable[[], Awaitable["Agent"]]
"""每跑一条用例调用一次，返回一个**全新**的 Agent（契约 §3.20）。"""


class EvalResult(BaseModel):
    """单条用例的评测结果（契约 §5.6）。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    """用例 id。"""

    output: str = ""
    """Agent 的最终输出文本。"""

    ok: bool = False
    """是否通过 —— 所有**适用**指标都达到门槛（不适用项不计入）。"""

    scores: dict[str, float] = Field(default_factory=dict)
    """指标名 → 得分；``-1.0`` 表示该指标对这条用例不适用。"""

    latency_ms: float = 0.0
    """端到端耗时（毫秒）。"""

    tokens: TokenUsage = Field(default_factory=TokenUsage)
    """token 用量。"""

    cost_usd: float = 0.0
    """按内置示例价目表折算的费用（美元）。"""

    tool_calls: list[str] = Field(default_factory=list)
    """本轮实际调用的工具名（按发生顺序）。"""

    error: str | None = None
    """异常文本；``None`` 表示正常收尾。"""

    def failed_metrics(self, thresholds: Mapping[str, float]) -> dict[str, float]:
        """返回未达门槛的指标。

        Args:
            thresholds (`Mapping[str, float]`): 门槛。

        Returns:
            `dict[str, float]`: ``{指标: 得分}``，只含未达标项。
        """
        return {
            name: value
            for name, value in self.scores.items()
            if value >= 0.0 and name in thresholds and value < thresholds[name]
        }


class EvalRunner:
    """并发评测执行器（契约 §3.20）。

    Example:
        >>> runner = EvalRunner(agent_factory=factory, concurrency=2, timeout_s=60)  # doctest: +SKIP
        >>> report = await runner.run(dataset, metrics=[contains])  # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        agent_factory: AgentFactory,
        concurrency: int = 4,
        timeout_s: float = 120.0,
        thresholds: Mapping[str, float] | None = None,
        profile_name: str = "",
        model: Any | None = None,
        price_table: Any | None = None,
        close_agents: bool = True,
    ) -> None:
        """构造运行器。

        Args:
            agent_factory (`AgentFactory`): 每用例一次的 Agent 工厂。
            concurrency (`int`): 并发上限；必须为正。
            timeout_s (`float`): 单用例超时（秒）；必须为正。
            thresholds (`Mapping[str, float] | None`): 通过门槛；
                ``None`` 用 :data:`~harness_kit.eval.metrics.DEFAULT_THRESHOLDS`。
            profile_name (`str`): 写进报告的 Profile 名。
            model (`Any | None`): 可选的模型名（字符串）或带 ``model`` 属性的对象，
                用于成本折算时定位价格；``None`` 时从 Agent 上探测。
            price_table (`Any | None`): 自定义 ``PriceTable``；``None`` 用内置示例表。
            close_agents (`bool`): 每条用例跑完是否尝试关闭 Agent。

        Raises:
            `ValueError`: ``concurrency <= 0`` 或 ``timeout_s <= 0``。
        """
        if concurrency <= 0:
            raise ValueError(f"concurrency 必须为正，收到 {concurrency}")
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须为正，收到 {timeout_s}")
        self.agent_factory: AgentFactory = agent_factory
        self.concurrency: int = int(concurrency)
        self.timeout_s: float = float(timeout_s)
        self.thresholds: dict[str, float] = dict(thresholds or DEFAULT_THRESHOLDS)
        self.profile_name: str = profile_name
        self.model_hint: Any = model
        self.price_table: Any = price_table
        self.close_agents: bool = close_agents
        self._counter: dict[str, int] = {"cases": 0, "errors": 0}

    # ------------------------------------------------------------------
    # 批量
    # ------------------------------------------------------------------
    async def run(
        self,
        dataset: EvalDataset,
        metrics: Sequence[MetricFn],
    ) -> "EvalReport":
        """跑完整个数据集（契约 §3.20）。

        Args:
            dataset (`EvalDataset`): 评测集。
            metrics (`Sequence[MetricFn]`): 指标函数序列；可以为空
                （只收集输出与 token，不打分）。

        Returns:
            `EvalReport`: 报告。

        Raises:
            `ValueError`: 数据集为空，或用例 id 重复。
        """
        from datetime import datetime, timezone

        from harness_kit.eval.report import EvalReport

        if not dataset.cases:
            raise ValueError(f"数据集 {dataset.name!r} 是空的，没有可跑的用例")
        duplicated = dataset.validate_unique_ids()
        if duplicated:
            raise ValueError(
                f"数据集 {dataset.name!r} 里 id 重复: {duplicated}；"
                "报告按 case_id 索引，重号会让两条结果互相覆盖",
            )

        started_at = datetime.now(timezone.utc)
        self._counter = {"cases": len(dataset.cases), "errors": 0}
        logger.bind(
            dataset=dataset.name,
            cases=len(dataset.cases),
            concurrency=self.concurrency,
            timeout_s=self.timeout_s,
        ).info("评测开始")

        semaphore = asyncio.Semaphore(self.concurrency)

        async def _guarded(case: EvalCase) -> EvalResult:
            async with semaphore:
                return await self.run_case(case, metrics)

        results: list[EvalResult] = list(
            await asyncio.gather(*[_guarded(case) for case in dataset.cases]),
        )
        finished_at = datetime.now(timezone.utc)
        self._counter["errors"] = sum(1 for item in results if item.error)
        logger.bind(
            dataset=dataset.name,
            errors=self._counter["errors"],
            elapsed_s=round((finished_at - started_at).total_seconds(), 2),
        ).info("评测结束")

        return EvalReport(
            dataset=dataset.name,
            profile=self.profile_name,
            started_at=started_at,
            finished_at=finished_at,
            results=results,
            thresholds=dict(self.thresholds),
        )

    # ------------------------------------------------------------------
    # 单条
    # ------------------------------------------------------------------
    async def run_case(
        self,
        case: EvalCase,
        metrics: Sequence[MetricFn],
    ) -> EvalResult:
        """跑一条用例（契约 §3.20）。

        **不抛异常**：超时、Agent 构造失败、Agent 报错都收敛成
        ``EvalResult(ok=False, error=...)``。指标函数自己抛的异常会被记进
        ``error`` 且该指标**不出现在 scores 里**（而不是记 0 分）。

        Args:
            case (`EvalCase`): 用例。
            metrics (`Sequence[MetricFn]`): 指标函数。

        Returns:
            `EvalResult`: 结果。
        """
        started = time.perf_counter()
        observed = ObservedRun(case_id=case.id)
        log = logger.bind(case_id=case.id, dataset_case=case.id)

        try:
            run = await asyncio.wait_for(self._execute(case), timeout=self.timeout_s)
        except asyncio.CancelledError:  # pragma: no cover - 取消继续往上抛
            raise
        except TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000.0
            log.warning("用例超时（{:.0f}s）", self.timeout_s)
            observed.latency_ms = elapsed
            observed.error = f"TimeoutError: 超过 {self.timeout_s:.0f}s 未返回"
            return EvalResult(
                case_id=case.id,
                output="",
                ok=False,
                scores={},
                latency_ms=elapsed,
                error=observed.error,
            )
        except Exception as exc:  # noqa: BLE001 - 单用例失败不中断整体
            elapsed = (time.perf_counter() - started) * 1000.0
            log.warning("用例异常: {}: {}", type(exc).__name__, exc)
            observed.latency_ms = elapsed
            observed.error = f"{type(exc).__name__}: {exc}"
            return EvalResult(
                case_id=case.id,
                output="",
                ok=False,
                scores={},
                latency_ms=elapsed,
                error=observed.error,
            )

        observed = run
        observed.latency_ms = (time.perf_counter() - started) * 1000.0

        scores: dict[str, float] = {}
        errors: list[str] = []
        with observed_run(observed):
            for metric in metrics:
                name = getattr(metric, "__name__", type(metric).__name__)
                try:
                    value = await metric(case, observed.output)
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # noqa: BLE001 - 单个指标失败不中断
                    errors.append(f"metric:{name}: {type(exc).__name__}: {exc}")
                    log.warning("指标 {} 计算出错: {}", name, exc)
                    continue
                scores[name] = round(float(value), 6)

        failed = {
            name: value
            for name, value in scores.items()
            if value >= 0.0 and name in self.thresholds and value < self.thresholds[name]
        }
        ok = not failed and not errors
        if not ok:
            log.debug("未通过: failed={} errors={}", failed, errors)

        return EvalResult(
            case_id=case.id,
            output=observed.output,
            ok=ok,
            scores=scores,
            latency_ms=observed.latency_ms,
            tokens=observed.tokens,
            cost_usd=observed_cost(observed, model=self.model_hint, table=self.price_table),
            tool_calls=observed.tool_calls,
            error="; ".join(errors) if errors else None,
        )

    # ------------------------------------------------------------------
    # 真实执行
    # ------------------------------------------------------------------
    async def _execute(self, case: EvalCase) -> ObservedRun:
        """真正跑一次 Agent（被 :meth:`run_case` 包在超时里）。

        Args:
            case (`EvalCase`): 用例。

        Returns:
            `ObservedRun`: 运行证据。

        Raises:
            `Exception`: 由调用方收敛。
        """
        agent = await self.agent_factory()
        try:
            await _observe_context(agent, case, name="harness-eval")
            return await collect_observed_run(agent, case.input)
        finally:
            if self.close_agents:
                await self.aclose_agent(agent)

    @staticmethod
    async def aclose_agent(agent: Any) -> None:
        """尽力关闭一个 Agent（顺序：``aclose`` → ``close`` → ``model.close``）。

        AgentScope 的 ``Agent``（``third_party/agentscope/src/agentscope/agent/_agent.py:117``）
        **没有** ``close`` / ``aclose`` / ``shutdown``，``ChatModelBase``
        （``third_party/agentscope/src/agentscope/model/_base.py:37``）也没有 ——
        关闭 HTTP 连接是各家 SDK 自己的事。所以这里是**通用**的尽力而为：
        有就调，没有就跳过，任何异常都吞掉并记 debug（清理失败不该让评测失败）。

        Args:
            agent (`Any`): Agent 对象。
        """
        for attr in ("aclose", "close"):
            closer = getattr(agent, attr, None)
            if callable(closer):
                try:
                    outcome = closer()
                    if asyncio.iscoroutine(outcome):
                        await outcome
                    return
                except Exception as exc:  # noqa: BLE001 - 清理失败不影响结果
                    logger.debug("关闭 Agent 失败（已忽略）: {}: {}", type(exc).__name__, exc)
                    return
        model = getattr(agent, "model", None)
        closer = getattr(model, "aclose", None) or getattr(model, "close", None)
        if callable(closer):
            try:
                outcome = closer()
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 Agent.model 失败（已忽略）: {}: {}", type(exc).__name__, exc)


async def _observe_context(agent: Any, case: EvalCase, *, name: str) -> None:
    """把 ``case.context`` 作为**先导消息**喂给 Agent。

    用 ``Agent.observe``（``third_party/agentscope/src/agentscope/agent/_agent.py:381``）
    而不是拼进用户输入：资料属于"背景"，用户那句话属于"问题"，
    混在一起会让 ``exact_match`` 这类指标失去意义。

    Args:
        agent (`Any`): AgentScope Agent。
        case (`EvalCase`): 用例。
        name (`str`): 资料消息的发送者名。
    """
    if not case.context:
        return
    from agentscope.message import Msg, TextBlock

    msgs = [
        Msg(
            name=name,
            role="user",
            content=[TextBlock(type="text", text=str(item))],
        )
        for item in case.context
    ]
    await agent.observe(msgs)


async def collect_observed_run(agent: Any, prompt: str) -> ObservedRun:
    """跑一次 Agent 并把证据收成 :class:`ObservedRun`。

    只消费 AgentScope 的事件流，不干预 Agent 的行为：

    - ``TextBlockDeltaEvent`` → 输出文本（``delta`` 字段）；
    - ``ToolCallStartEvent`` → ``tool_calls``（字段名是 ``tool_call_name``，
      ``third_party/agentscope/src/agentscope/event/_event.py:314``）；
    - ``ToolResultEndEvent`` → 非 ``success`` 的结果进 ``tool_errors``；
    - ``ModelCallEndEvent`` → token 累加，同时 ``iterations += 1``；
    - ``ReplyEndEvent`` → 收尾原因（``finished_reason``，
      ``third_party/agentscope/src/agentscope/event/_event.py:112``）。

    **文本口径**：优先取最终 ``Msg.get_text_content()``（Agent 的收尾消息），
    取不到时退回"流里拼出来的文本"。理由：思考块与工具入参不进正文，
    用拼接值会把 ``ThinkingBlock`` 的内容也算进去。要拿到那条收尾
    ``Msg`` 必须显式传 ``yield_final_msg=True`` —— ``reply_stream`` 默认是
    ``False``，会把最后的 ``Msg`` 从流里过滤掉
    （``third_party/agentscope/src/agentscope/agent/_agent.py:328``）。

    Args:
        agent (`Any`): AgentScope Agent。
        prompt (`str`): 用户输入。

    Returns:
        `ObservedRun`: 证据。

    Raises:
        `ValueError`: ``prompt`` 为空。
    """
    if not prompt.strip():
        raise ValueError("评测用例的 input 不能为空 —— 空输入跑出来的分数没有意义")

    from agentscope.event import (
        ModelCallEndEvent,
        ReplyEndEvent,
        ReplyFinishedReason,
        TextBlockDeltaEvent,
        ToolCallStartEvent,
        ToolResultEndEvent,
    )
    from agentscope.message import Msg, TextBlock

    observed = ObservedRun(case_id="", model=_model_name_of(agent))
    streamed: list[str] = []
    final_text: str | None = None
    finished_reason: str = ""

    message = Msg(name="user", role="user", content=[TextBlock(type="text", text=prompt)])
    async for chunk in agent.reply_stream(message, yield_final_msg=True):
        if isinstance(chunk, Msg):
            text = chunk.get_text_content()
            if text:
                final_text = text
            continue
        if isinstance(chunk, TextBlockDeltaEvent):
            streamed.append(chunk.delta)
        elif isinstance(chunk, ToolCallStartEvent):
            observed.tool_calls.append(str(chunk.tool_call_name))
        elif isinstance(chunk, ToolResultEndEvent):
            state = str(chunk.state)
            if state != "success":
                name = observed.tool_calls[-1] if observed.tool_calls else "?"
                observed.tool_errors.append(f"{name}: {state}")
        elif isinstance(chunk, ModelCallEndEvent):
            observed.tokens = TokenUsage(
                input_tokens=observed.tokens.input_tokens + int(chunk.input_tokens or 0),
                output_tokens=observed.tokens.output_tokens + int(chunk.output_tokens or 0),
            )
            observed.iterations += 1
        elif isinstance(chunk, ReplyEndEvent):
            finished_reason = str(chunk.finished_reason)

    if finished_reason == str(ReplyFinishedReason.EXCEED_MAX_ITERS):
        observed.error = "ExceedMaxIters: Agent 达到 max_iters 仍未收尾"
        logger.warning("用例达到 max_iters，输出可能不完整")
    elif finished_reason == str(ReplyFinishedReason.ERROR):
        observed.error = "ReplyFinishedReason.ERROR: Agent 以错误收尾"
    elif finished_reason == str(ReplyFinishedReason.INTERRUPTED):
        observed.error = "ReplyFinishedReason.INTERRUPTED: Agent 被中断"

    observed.output = final_text if final_text is not None else "".join(streamed)
    return observed


def _model_name_of(agent: Any) -> str:
    """从 Agent 上探测模型名。

    Args:
        agent (`Any`): Agent 对象。

    Returns:
        `str`: 模型名；探测不到时返回 ``""``。
    """
    model = getattr(agent, "model", None)
    for attr in ("model", "model_name", "name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def observed_cost(
    observed: ObservedRun,
    *,
    model: Any | None = None,
    table: Any | None = None,
) -> float:
    """把 token 折算成费用（复用 ``harness_kit.models.pricing.cost_of``）。

    查不到价格时返回 ``0.0`` 并打 debug —— 评测不该因为"价目表里没有这个
    模型名"而失败（本环境用的 ``deepseek-flash`` 就是靠内置别名兜住的）。

    Args:
        observed (`ObservedRun`): 运行证据。
        model (`Any | None`): 模型名或带 ``model`` 属性的对象；``None`` 时
            用 ``observed.model``。
        table (`Any | None`): ``PriceTable``；``None`` 用内置示例表。

    Returns:
        `float`: 费用（美元）。
    """
    from agentscope.model import ChatUsage

    from harness_kit.models.pricing import DEFAULT_PRICE_TABLE, UnknownPriceError, cost_of

    name = model if isinstance(model, str) else _model_name_of_object(model or observed.model)
    if not name:
        return 0.0
    resolved_table = table or DEFAULT_PRICE_TABLE
    try:
        price = resolved_table.lookup(name)
    except UnknownPriceError:
        logger.debug("价目表里没有 {}，本次成本记 0", name)
        return 0.0
    usage = ChatUsage(
        input_tokens=observed.tokens.input_tokens,
        output_tokens=observed.tokens.output_tokens,
        time=observed.latency_ms / 1000.0,
    )
    return round(float(cost_of(usage, price)), 8)


def _model_name_of_object(value: Any) -> str:
    """从一个"可能是字符串、也可能是对象"的值里取模型名。

    Args:
        value (`Any`): 字符串 / 对象。

    Returns:
        `str`: 模型名；取不到时返回 ``""``。
    """
    if isinstance(value, str):
        return value
    for attr in ("model", "model_name"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""
