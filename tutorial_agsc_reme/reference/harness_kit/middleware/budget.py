# -*- coding: utf-8 -*-
"""token 与成本预算中间件（契约 §3.8，第 8 讲）。

AgentScope 自带的 ``ReplyBudgetControlMiddleware``
（``third_party/agentscope/src/agentscope/middleware/_budget.py:21``）解决的是
**单次 reply 内**的加权 token 预算：超了就插一条 ``HintBlock`` 并强制
``tool_choice="none"``，让模型收尾。它有两个生产里不够用的地方：

1. **不会拒绝**。超预算后模型仍然会被调用一次（只是不给工具），
   对"按 token 计费的外部 API"来说，这一刀砍得太晚；
2. **只算 token，不算工具调用**。一次失控的 ``while`` + 工具循环能把
   token 花在"工具返回的垃圾"上，工具调用次数是更早的刹车点。

本中间件补这两点，并且**与原生实现共存**（原生实现登记在
``harness_kit/registry.py`` 的 ``_native_budget_middleware`` 回退里，
见 ``tutorial_agsc_reme/reference/harness_kit/registry.py:917``）：

- ``on_model_call`` 读 ``response.usage`` 累计 token；
- ``on_acting`` 累计工具调用次数；
- 超限时按 ``on_exceed`` 二选一：
  ``"raise"`` 立刻抛 :class:`BudgetExceededError`；
  ``"truncate"`` 不抛，改为在 ``on_reasoning`` 里把 ``tool_choice`` 强制成
  ``"none"``（复刻原生行为，让模型收尾）。

**预算窗口是"每次 reply"**：``on_reply`` 收到 ``ReplyStartEvent`` 时清零当次窗口
（``third_party/agentscope/src/agentscope/event/_event.py`` 的事件流），
另用 :attr:`total` 记录进程内的累计消费，供成本报表使用。

成本换算：``cost_per_1k_input`` / ``cost_per_1k_output`` 是"每 1000 token 的
单价（美元）"，由 Profile 传入 —— harness_kit 不内置任何价目表，
因为价格会变，写死在库里是最容易过期的东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Literal

from loguru import logger
from pydantic import BaseModel, Field

from agentscope.event import ReplyStartEvent
from agentscope.tool import ToolChoice

from harness_kit.middleware.base import HarnessMiddleware, call_next, call_next_stream

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.agent import Agent

__all__ = [
    "BudgetExceededError",
    "BudgetMiddleware",
    "BudgetUsage",
]


class BudgetExceededError(RuntimeError):
    """超出 token / 成本 / 工具调用预算（契约 §3.8）。"""


class BudgetUsage(BaseModel):
    """一次窗口内的用量累计（契约 §3.8）。"""

    prompt_tokens: int = 0
    """输入（prompt）token 数。"""

    completion_tokens: int = 0
    """输出（completion）token 数。"""

    tool_calls: int = 0
    """工具调用次数。"""

    cost_usd: float = Field(default=0.0)
    """按单价折算出的美元成本；单价为 0 时恒为 0。"""

    @property
    def total_tokens(self) -> int:
        """输入 + 输出 token 总和。

        Returns:
            `int`: 总 token 数。
        """
        return self.prompt_tokens + self.completion_tokens

    def is_within(self, m: "BudgetMiddleware") -> bool:
        """按 ``m`` 的三条上限判断本用量是否仍在上限内（契约 §3.8）。

        Args:
            m (`BudgetMiddleware`): 提供上限与单价的中间件。

        Returns:
            `bool`: 三条上限全部未突破时为 ``True``。
        """
        return (
            self.prompt_tokens <= m.max_prompt_tokens
            and self.completion_tokens <= m.max_completion_tokens
            and self.tool_calls <= m.max_tool_calls
        )

    def exceeded_reasons(self, m: "BudgetMiddleware") -> list[str]:
        """列出所有被突破的上限，用于报错信息与日志。

        Args:
            m (`BudgetMiddleware`): 提供上限的中间件。

        Returns:
            `list[str]`: 描述列表；为空表示未超限。
        """
        reasons: list[str] = []
        if self.prompt_tokens > m.max_prompt_tokens:
            reasons.append(
                f"prompt tokens {self.prompt_tokens} > {m.max_prompt_tokens}",
            )
        if self.completion_tokens > m.max_completion_tokens:
            reasons.append(
                f"completion tokens {self.completion_tokens} "
                f"> {m.max_completion_tokens}",
            )
        if self.tool_calls > m.max_tool_calls:
            reasons.append(f"tool calls {self.tool_calls} > {m.max_tool_calls}")
        return reasons

    def snapshot(self) -> dict[str, float]:
        """转成扁平 dict，便于写日志/指标。

        Returns:
            `dict[str, float]`: 各字段。
        """
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 6),
        }


class BudgetMiddleware(HarnessMiddleware):
    """token / 成本 / 工具调用三重预算（契约 §3.8）。

    Args:
        max_prompt_tokens (`int`): 单次 reply 的输入 token 上限。
        max_completion_tokens (`int`): 单次 reply 的输出 token 上限。
        max_tool_calls (`int`): 单次 reply 的工具调用次数上限。
        on_exceed (`Literal["raise", "truncate"]`): 超限行为，默认 ``"raise"``。
        cost_per_1k_input (`float`): 每 1000 输入 token 的美元单价。
        cost_per_1k_output (`float`): 每 1000 输出 token 的美元单价。
        reset_each_reply (`bool`): 是否每次 reply 清零窗口，默认 ``True``。
            ``False`` 时窗口就是整个进程生命周期（适合"一次任务一个进程"的 CLI）。

    Raises:
        ValueError: 任一上限为负数。

    Example:
        >>> from harness_kit.middleware import BudgetMiddleware   # doctest: +SKIP
        >>> mw = BudgetMiddleware(max_prompt_tokens=60000,
        ...                       max_completion_tokens=16000,
        ...                       max_tool_calls=40)
        >>> agent = Agent(..., middlewares=[mw])                  # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        max_prompt_tokens: int,
        max_completion_tokens: int,
        max_tool_calls: int,
        on_exceed: Literal["raise", "truncate"] = "raise",
        cost_per_1k_input: float = 0.0,
        cost_per_1k_output: float = 0.0,
        reset_each_reply: bool = True,
    ) -> None:
        """初始化并校验上限。

        Raises:
            ValueError: 上限为负，或 ``on_exceed`` 取值非法。
        """
        # Profile / YAML 里这些字段可能是字符串（``"60000"``），而
        # harness_kit.registry 的 _spec_class_adapter 是 ``target(**spec.params)``，
        # 不做类型转换 —— 所以在这里显式转一次，别让 "60000" < 0 抛 TypeError。
        for label, value in (
            ("max_prompt_tokens", max_prompt_tokens),
            ("max_completion_tokens", max_completion_tokens),
            ("max_tool_calls", max_tool_calls),
        ):
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label} 必须是整数，收到 {value!r}") from exc
            if number < 0:
                raise ValueError(f"{label} 不能为负数，收到 {number}")
        if on_exceed not in ("raise", "truncate"):
            raise ValueError(
                f"on_exceed 只能是 'raise' 或 'truncate'，收到 {on_exceed!r}",
            )

        self.max_prompt_tokens = int(max_prompt_tokens)
        self.max_completion_tokens = int(max_completion_tokens)
        self.max_tool_calls = int(max_tool_calls)
        self.on_exceed: Literal["raise", "truncate"] = on_exceed
        self.cost_per_1k_input = float(cost_per_1k_input)
        self.cost_per_1k_output = float(cost_per_1k_output)
        self.reset_each_reply = bool(reset_each_reply)

        self._usage = BudgetUsage()
        self._total = BudgetUsage()
        self.trip_count: int = 0
        """被触发（抛异常或降级）的次数，便于观测"这个 Agent 有多不听话"。"""

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def used(self) -> BudgetUsage:
        """当前窗口的用量（契约 §3.8）。

        Returns:
            `BudgetUsage`: 当次 reply（或整个进程，取决于 ``reset_each_reply``）的用量。
        """
        return self._usage

    @property
    def total(self) -> BudgetUsage:
        """进程内的累计用量（不随窗口清零）。

        Returns:
            `BudgetUsage`: 累计用量。
        """
        return self._total

    @property
    def exceeded(self) -> bool:
        """当前窗口是否已突破上限。

        Returns:
            `bool`: 是否超限。
        """
        return not self._usage.is_within(self)

    def reset(self) -> None:
        """清零当前窗口（累计值保留）。"""
        self._usage = BudgetUsage()

    def charge(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tool_calls: int = 0,
    ) -> None:
        """手工记账（供测试与外部计量器使用）。

        Args:
            prompt_tokens (`int`): 输入 token 增量。
            completion_tokens (`int`): 输出 token 增量。
            tool_calls (`int`): 工具调用增量。
        """
        for usage in (self._usage, self._total):
            usage.prompt_tokens += max(prompt_tokens, 0)
            usage.completion_tokens += max(completion_tokens, 0)
            usage.tool_calls += max(tool_calls, 0)
            usage.cost_usd += (
                max(prompt_tokens, 0) / 1000.0 * self.cost_per_1k_input
                + max(completion_tokens, 0) / 1000.0 * self.cost_per_1k_output
            )

    def _enforce(self, *, where: str) -> None:
        """检查上限并执行 ``on_exceed`` 策略。

        Args:
            where (`str`): 触发点（``"model_call"`` / ``"acting"``），写进日志。

        Raises:
            BudgetExceededError: ``on_exceed="raise"`` 且已超限。
        """
        if not self.exceeded:
            return
        reasons = self._usage.exceeded_reasons(self)
        self.trip_count += 1
        if self.on_exceed == "raise":
            raise BudgetExceededError(
                f"预算超限（{where}）：{'; '.join(reasons)}"
                f"；用量 {self._usage.snapshot()}，上限 "
                f"prompt={self.max_prompt_tokens} "
                f"completion={self.max_completion_tokens} "
                f"tool_calls={self.max_tool_calls}",
            )
        logger.bind(
            middleware=self.name(),
            where=where,
            usage=self._usage.snapshot(),
            reasons=reasons,
        ).warning("预算超限，降级为强制收尾（on_exceed=truncate）")

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """在 ``ReplyStartEvent`` 处清零窗口，并透传整条事件流。

        Args:
            agent (`Agent`): 发起 reply 的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``inputs``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的事件。
        """
        async for event in call_next_stream(next_handler, input_kwargs):
            if (
                self.reset_each_reply
                and isinstance(event, ReplyStartEvent)
            ):
                self.reset()
                logger.bind(
                    middleware=self.name(),
                    agent=agent.name,
                    reply_id=event.reply_id,
                ).debug("预算窗口已清零")
            yield event

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """``on_exceed="truncate"`` 时，超预算就强制 ``tool_choice="none"``。

        这正是原生 ``ReplyBudgetControlMiddleware`` 的做法
        （``third_party/agentscope/src/agentscope/middleware/_budget.py:150``）。

        Args:
            agent (`Agent`): 当前 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``tool_choice``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的事件。
        """
        if self.on_exceed == "truncate" and self.exceeded:
            input_kwargs = {
                **input_kwargs,
                "tool_choice": ToolChoice(mode="none"),
            }
        async for event in call_next_stream(next_handler, input_kwargs):
            yield event

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Any],
    ) -> Any:
        """累计模型调用的 token 用量，并在超限时执行策略。

        Args:
            agent (`Agent`): 发起调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``messages`` / ``tools``。
            next_handler (`Callable[..., Any]`): 链上的下一环。

        Returns:
            `Any`: 原样透传的 ``ChatResponse``（流式时是它的生成器）。

        Raises:
            BudgetExceededError: 超限且 ``on_exceed="raise"``。
        """
        result = await call_next(next_handler, input_kwargs)
        if hasattr(result, "__aiter__"):
            return self._meter_stream(result)
        self._meter_response(result)
        self._enforce(where="model_call")
        return result

    async def _meter_stream(self, stream: AsyncGenerator[Any, None]) -> AsyncGenerator[Any, None]:
        """流式响应：每个 chunk 都试算一次用量，并在这里执行策略。

        **为什么策略必须在流里执行**：``stream=True`` 时 ``on_model_call``
        拿到的是生成器，真正的 token 用量来自最后一块（``ChatUsage`` 挂在
        ``ChatModelBase`` 收尾的 ``is_last=True`` 块上，
        ``third_party/agentscope/src/agentscope/model/_base.py:262-288``）。
        只把 ``_enforce`` 写在非流式分支里，等于**流式下预算永不生效** ——
        实测就是"模型照跑、异常从不抛出"（2026-09-21 验证脚本的 W10 抓到）。

        ``_enforce`` 放在 ``yield`` **之后**：这一块已经交给上层，不会出现
        "结果丢了一半"；上层要拿下一块才会回到这里，而 AgentScope 一定会把
        流迭代到 ``StopAsyncIteration``（它要靠收尾块 ``build()`` 拼出完整响应）。

        Args:
            stream (`AsyncGenerator[Any, None]`): 下游流。

        Yields:
            `Any`: 原样透传的 chunk。

        Raises:
            BudgetExceededError: 超限且 ``on_exceed="raise"``。
        """
        async for chunk in stream:
            self._meter_response(chunk)
            yield chunk
            self._enforce(where="model_call")

    def _meter_response(self, response: Any) -> None:
        """把一次 ``ChatResponse`` 的 usage 计入账本。

        注意 ``ChatResponse`` **没有** ``input_tokens`` 属性，
        用量在 ``response.usage``（``ChatUsage``）上
        （``third_party/agentscope/src/agentscope/model/_model_usage.py:10``）。

        Args:
            response (`Any`): ``ChatResponse``；没有 usage 时静默跳过。
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.charge(
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """累计工具调用次数，并在超限时执行策略。

        Args:
            agent (`Agent`): 执行工具调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``tool_call``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的 ``ToolChunk`` / ``ToolResponse``。

        Raises:
            BudgetExceededError: 超限且 ``on_exceed="raise"``。
        """
        tool_call = input_kwargs.get("tool_call")
        self.charge(tool_calls=1)
        logger.bind(
            middleware=self.name(),
            tool=getattr(tool_call, "name", "<unknown>"),
            tool_calls=self._usage.tool_calls,
            max_tool_calls=self.max_tool_calls,
        ).debug("工具调用计数")

        async for item in call_next_stream(next_handler, input_kwargs):
            yield item

        # 放在工具执行之后：这一次调用的结果已经产出，不会半路丢结果；
        # 下一次调用会在进入时立刻被拦住。
        self._enforce(where="acting")
