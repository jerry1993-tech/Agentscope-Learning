# -*- coding: utf-8 -*-
"""人机确认桥接（HITL，契约 §3.11，第 11 讲）。

**AgentScope 已经把 HITL 的状态机写完了，本模块只负责"接一根线"**

AgentScope 用两个事件表达人机确认（``third_party/agentscope/src/agentscope/event/_event.py``）：

- 待确认：:class:`~agentscope.event.RequireUserConfirmEvent`（``:443``），
  带 ``reply_id`` 和一组 ``tool_calls``（其 ``state`` 是 ``asking``）；
- 回传结果：:class:`~agentscope.event.UserConfirmResultEvent`（``:483``），
  带一组 :class:`~agentscope.event.ConfirmResult`。

Agent 收到结果后的行为是**确定**的（``third_party/agentscope/src/agentscope/agent/_agent.py:1958`` 起）：

- ``confirmed=True`` → 该 tool call 的 state 置为 ``ALLOWED``，
  把 ``confirmation.tool_call`` 的 ``name`` / ``input`` 覆盖回去（所以用户可以改参数），
  **并把 ``confirmation.rules`` 里的规则 ``add_rule`` 进引擎**（"允许，并且以后别再问"）；
- ``confirmed=False`` → 记一条 ``ToolResultState.DENIED`` 的工具结果。

所以 :class:`HITLBridge` 要做的只有三件事：**问人 → 组装 ConfirmResult → 严格失败关闭**。

**失败关闭（fail-closed）是本模块的第一原则**

超时、prompter 抛异常、prompter 返回非布尔 —— 一律**拒绝**。
更关键的是：``_check_incoming_event`` 只校验"回传的 id 是**等待中**的 id"
（``.../agent/_agent.py:1903``），**不会**要求"每个等待中的 id 都有结果"。
漏掉一个，那个 tool call 就永远停在 ``ASKING``，会话卡死在那里。
因此 :meth:`HITLBridge.request` 保证**对事件里的每一个 tool call 都产出一条结果**——
一个都不漏，这是它存在的意义。

**不实现事件循环**：本模块不碰 ``reply_stream``，不处理 ``UserInterruptEvent``；
调用方拿到 :class:`UserConfirmResultEvent` 后喂回 ``agent.reply_stream(event)`` 即可
（这正是 AgentScope 的 resume 流程，见 ``.../agent/_agent.py:1031``）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from agentscope.event import (
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionRule
from loguru import logger

from harness_kit.events import EventKind, EventRecord
from harness_kit.middleware.tracing import EventBusLike

__all__ = [
    "ConfirmationTimeout",
    "HITLBridge",
    "Prompter",
    "default_prompter",
]


Prompter = Callable[[str], Awaitable[bool]]
"""提问回调：``async (question: str) -> bool``。

``True`` = 同意执行，``False`` = 拒绝。终端实现见 :func:`default_prompter`；
HTTP / WebSocket 实现只要满足这个签名即可（把问题发出去、等回答）。
"""


class ConfirmationTimeout(RuntimeError):
    """用户在 ``timeout_s`` 内没有回应（契约 §3.11）。"""


async def default_prompter(question: str) -> bool:
    """终端提问（``input()`` 的异步包装）。

    非交互环境（stdin 不是 tty、或 EOF）**不算"同意"**：
    ``EOFError`` → ``False``，符合本模块的失败关闭原则。
    阻塞式 ``input()`` 放进 ``asyncio.to_thread``，避免卡住事件循环。

    Args:
        question (`str`): 要问用户的问题（多行文本）。

    Returns:
        `bool`: ``True`` 表示同意。

    Example:
        >>> import asyncio
        >>> # 交互环境下会真的等用户敲 y/n
        >>> # asyncio.run(default_prompter("继续?"))
    """
    def _ask() -> bool:
        """同步提问。

        Returns:
            `bool`: 用户是否同意。
        """
        try:
            answer = input(question)
        except EOFError:
            logger.warning("stdin 已关闭，按拒绝处理（fail-closed）")
            return False
        return answer.strip().lower() in ("y", "yes", "是", "同意")

    return await asyncio.to_thread(_ask)


class HITLBridge:
    """把 AgentScope 的确认事件桥接到外部（契约 §3.11）。

    Example:
        >>> bridge = HITLBridge(bus=None, timeout_s=60.0, prompter=fake_prompter)
        >>> result = await bridge.request(require_confirm_event)  # doctest: +SKIP
        >>> # 把 result 喂回 agent.reply_stream(result)

    Attributes:
        bus (`EventBusLike | None`): 可观测性总线（可选）。
        timeout_s (`float`): 单次提问的超时秒数。
        prompter (`Prompter`): 提问回调。
    """

    def __init__(
        self,
        *,
        bus: EventBusLike | None = None,
        timeout_s: float = 300.0,
        prompter: Prompter | None = None,
        session_id: str = "",
        seq: int = 0,
    ) -> None:
        """构造桥接器。

        Args:
            bus (`EventBusLike | None`): 事件总线。契约 §3.11 把它列为必填
                （``bus: EventBus``），但真正的实现 ``harness_kit/events/bus.py``
                可能尚未装配；这里按 :class:`~harness_kit.middleware.tracing.EventBusLike`
                的结构类型接收，``None`` 时退化为"只打日志"。语义上它只服务
                **可观测性** —— 让人看到"有一次确认在等人" —— 不参与判定，
                因此缺失它不影响正确性。
            timeout_s (`float`): 超时秒数；超时即拒绝。
            prompter (`Prompter | None`): 提问回调；``None`` 时用
                :func:`default_prompter`（终端提问）。
            session_id (`str`): 会话 id，仅用于事件与日志。
            seq (`int`): 事件序号起点（发给 bus 的 ``EventRecord.seq``）。

        Raises:
            ValueError: ``timeout_s`` 不是正数。
        """
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须是正数，收到 {timeout_s}")

        self.bus: EventBusLike | None = bus
        self.timeout_s: float = timeout_s
        self.prompter: Prompter = prompter or default_prompter
        self.session_id: str = session_id
        self._seq: int = seq

        self.prompts: int = 0
        """本进程内发起的提问次数。"""

        self.approvals: int = 0
        """其中被同意的次数。"""

        self.timeouts: int = 0
        """其中超时的次数。"""

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def request(
        self,
        event: RequireUserConfirmEvent,
    ) -> UserConfirmResultEvent:
        """向用户逐个确认 ``event.tool_calls``，返回可直接回传的结果事件（契约 §3.11）。

        **保证对每个 tool call 都有一条结果**（见模块 docstring 的失败关闭说明）。

        Args:
            event (`RequireUserConfirmEvent`): Agent park 时吐出的事件。

        Returns:
            `UserConfirmResultEvent`: ``reply_id`` 与入参一致，``confirm_results``
                数量与 ``event.tool_calls`` 相同。

        Example:
            >>> # 全部同意时，每条 ConfirmResult 都带上 suggested_rules
            >>> result = await bridge.request(event)  # doctest: +SKIP
            >>> all(r.confirmed for r in result.confirm_results)
            True
        """
        await self._publish_pending(event)

        results: list[ConfirmResult] = []
        for index, tool_call in enumerate(event.tool_calls):
            confirmed = await self._ask_one(index, len(event.tool_calls), tool_call)
            results.append(
                ConfirmResult(
                    confirmed=confirmed,
                    tool_call=tool_call,
                    # 只在同意时带上建议规则：拒绝的场景下把规则塞进去，
                    # 会让"拒绝"这条反馈意外地扩大权限（add_rule 只看 rules 非空）。
                    rules=(
                        list(tool_call.suggested_rules)
                        if confirmed and tool_call.suggested_rules
                        else None
                    ),
                ),
            )

        result = UserConfirmResultEvent(
            reply_id=event.reply_id,
            confirm_results=results,
        )
        self.approvals += sum(1 for item in results if item.confirmed)
        logger.bind(
            session_id=self.session_id,
            reply_id=event.reply_id,
            total=len(results),
            approved=self.approvals,
        ).info(
            "HITL 确认完成：{} 同意 / {} 拒绝",
            sum(1 for item in results if item.confirmed),
            sum(1 for item in results if not item.confirmed),
        )
        await self._publish_resolved(event, results)
        return result

    # ------------------------------------------------------------------
    # 单条提问
    # ------------------------------------------------------------------
    async def _ask_one(
        self,
        index: int,
        total: int,
        tool_call: ToolCallBlock,
    ) -> bool:
        """问用户一条工具调用是否放行。

        Args:
            index (`int`): 第几条（从 0 起，用于提问里的序号）。
            total (`int`): 总共几条。
            tool_call (`ToolCallBlock`): 待确认的调用。

        Returns:
            `bool`: 是否放行。
        """
        question = self._render(index, total, tool_call)
        self.prompts += 1
        try:
            async with asyncio.timeout(self.timeout_s):
                answer = await self.prompter(question)
        except TimeoutError:
            self.timeouts += 1
            logger.bind(
                session_id=self.session_id,
                tool=tool_call.name,
                timeout_s=self.timeout_s,
            ).warning("HITL 确认超时，按拒绝处理（fail-closed）")
            return False
        except asyncio.CancelledError:
            # 任务被取消同样意味着"没人确认过"，按拒绝收场后继续抛，
            # 让取消语义如实传播（吞掉它会破坏调用方的结构化并发）。
            logger.bind(session_id=self.session_id, tool=tool_call.name).warning(
                "HITL 提问被取消，按拒绝处理",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 提问通道故障不能等同于同意
            logger.bind(
                session_id=self.session_id,
                tool=tool_call.name,
                error=str(exc),
            ).warning("HITL 提问通道异常，按拒绝处理（fail-closed）")
            return False
        return bool(answer)

    def _render(self, index: int, total: int, tool_call: ToolCallBlock) -> str:
        """把一条待确认调用渲染成人能读的问题。

        Args:
            index (`int`): 序号。
            total (`int`): 总数。
            tool_call (`ToolCallBlock`): 待确认的调用。

        Returns:
            `str`: 多行问题文本。
        """
        lines = [
            f"[{index + 1}/{total}] 工具 {tool_call.name} 请求执行，是否允许？",
            f"  参数: {tool_call.input}",
        ]
        if tool_call.suggested_rules:
            suggestions = ", ".join(
                f"{rule.tool_name}:{rule.rule_content or '*'}"
                for rule in tool_call.suggested_rules
            )
            lines.append(f"  （同意后将记住规则: {suggestions}）")
        lines.append("输入 y 允许，其他任何输入视为拒绝: ")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 可观测性
    # ------------------------------------------------------------------
    async def _publish_pending(self, event: RequireUserConfirmEvent) -> None:
        """把"有人在等确认"投到总线上（无总线时只打日志）。

        Args:
            event (`RequireUserConfirmEvent`): 待确认事件。
        """
        payload = {
            "reply_id": event.reply_id,
            "tool_calls": [call.name for call in event.tool_calls],
            "count": len(event.tool_calls),
            "timeout_s": self.timeout_s,
        }
        await self._publish("hitl.pending", payload)

    async def _publish_resolved(
        self,
        event: RequireUserConfirmEvent,
        results: list[ConfirmResult],
    ) -> None:
        """把确认结果投到总线上。

        Args:
            event (`RequireUserConfirmEvent`): 原事件。
            results (`list[ConfirmResult]`): 确认结果。
        """
        payload = {
            "reply_id": event.reply_id,
            "approved": [item.tool_call.name for item in results if item.confirmed],
            "denied": [item.tool_call.name for item in results if not item.confirmed],
            "count": len(results),
        }
        await self._publish("hitl.resolved", payload)

    async def _publish(self, name: str, payload: dict[str, object]) -> None:
        """投递一条 ``CUSTOM`` 事件；无总线时降级为 debug 日志。

        Args:
            name (`str`): 自定义事件名（写进 ``payload["name"]``）。
            payload (`dict[str, object]`): 其余负载。
        """
        record = EventRecord(
            session_id=self.session_id or "<unbound>",
            seq=self._seq,
            kind=EventKind.CUSTOM,
            payload={"name": name, **payload},
            source="harness_kit.permission.hitl",
        )
        self._seq += 1
        if self.bus is None:
            logger.bind(**{k: str(v) for k, v in record.payload.items()}).debug(
                "无事件总线，HITL 事件未投递",
            )
            return
        delivered = await self.bus.publish(f"hitl.{name}", record)
        logger.bind(name=name, delivered=delivered).debug("HITL 事件已投递")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def describe(self) -> dict[str, object]:
        """返回自述（CLI ``harness inspect`` 用）。

        Returns:
            `dict[str, object]`: 超时、是否接了总线、提问/同意/超时计数。
        """
        return {
            "timeout_s": self.timeout_s,
            "has_bus": self.bus is not None,
            "prompter": getattr(self.prompter, "__name__", repr(self.prompter)),
            "prompts": self.prompts,
            "approvals": self.approvals,
            "timeouts": self.timeouts,
        }

    @staticmethod
    def auto_deny(reason: str) -> Prompter:
        """造一个"永远拒绝"的 prompter（无人值守 / 测试用）。

        Args:
            reason (`str`): 拒绝原因（会打一条 info 日志）。

        Returns:
            `Prompter`: 永远返回 ``False`` 的提问回调。

        Example:
            >>> import asyncio
            >>> asyncio.run(HITLBridge.auto_deny("no human")(\"?\") )
            False
        """
        async def _deny(question: str) -> bool:
            """拒绝。

            Args:
                question (`str`): 被忽略的问题。

            Returns:
                `bool`: 恒为 ``False``。
            """
            del question
            logger.info("HITL 自动拒绝：{}", reason)
            return False

        return _deny

    @staticmethod
    def auto_allow(reason: str) -> Prompter:
        """造一个"永远同意"的 prompter（**仅限测试**）。

        Args:
            reason (`str`): 同意原因（会打一条 warning 日志）。

        Returns:
            `Prompter`: 永远返回 ``True`` 的提问回调。
        """
        async def _allow(question: str) -> bool:
            """同意。

            Args:
                question (`str`): 被忽略的问题。

            Returns:
                `bool`: 恒为 ``True``。
            """
            del question
            logger.warning("HITL 自动同意（仅限测试）：{}", reason)
            return True

        return _allow

    @staticmethod
    def rules_from(call: ToolCallBlock) -> list[PermissionRule]:
        """取一条调用携带的建议规则（"允许并记住"要写回 ConfirmResult 的东西）。

        AgentScope 把建议放在 :attr:`ToolCallBlock.suggested_rules`
        （``third_party/agentscope/src/agentscope/message/_block.py:176``），
        由引擎在生成 ASK 决策时填好（``.../permission/_engine.py:161`` /
        ``:187`` / ``:208`` 三处调 ``_generate_suggestions``）。
        **不是**从事件里拿，事件里没有。

        Args:
            call (`ToolCallBlock`): 待确认的调用。

        Returns:
            `list[PermissionRule]`: 建议规则（可能为空列表）。
        """
        return list(call.suggested_rules)
