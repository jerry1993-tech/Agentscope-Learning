# -*- coding: utf-8 -*-
"""把 AgentScope 的 ``AgentEvent`` 流翻译成 ``EventRecord``（契约 §3.3，第 3 讲）。

**双层投影，一个翻译器。** AgentScope 的 28 种事件（``third_party/agentscope/
src/agentscope/event/_event.py:568`` 的 ``AgentEvent`` 联合类型）要满足两类完全
不同的消费者：

============================== ==========================================================
**审计日志**（``to_record``）  只需要"发生了什么"：模型调了几次、花了多少 token、
                              调了哪个工具、结果是好是坏。**不落正文**（正文在
                              ``AgentState.context`` 里），9 种 ``EventKind`` 就够。
**实时 UI**（``to_sse_frame``）  需要**每一个**字符：文本增量、思考增量、工具参数
                              增量，一个都不能少，否则前端打不出字。
============================== ==========================================================

所以本模块刻意**不**做"事件 → 一种输出"的单一翻译，而是给两条投影：

- :meth:`StreamTranslator.to_record` —— 审计投影，未识别的返回 ``None``；
- :meth:`StreamTranslator.to_sse_frame` —— UI 投影，原样透传 ``model_dump(mode="json")``。

**为什么事件不能直接当审计日志用**（这是本讲最值钱的一条结论）：
``AgentEvent`` 是"瘦"的——它只带 ``id`` / ``created_at`` / ``metadata`` / ``type``
和各自的业务字段，**不携带内容块对象**。更麻烦的是它**把一件事拆成了好几条事件**：

- ``TOOL_CALL_START`` 只有 ``tool_call_id`` + ``tool_call_name``，
  工具入参是后续 N 条 ``TOOL_CALL_DELTA`` 的字符串拼出来的（
  ``third_party/agentscope/src/agentscope/message/_block.py:149`` 的
  ``ToolCallBlock.input`` 就是"原始 JSON 字符串"）；
- ``TOOL_RESULT_END`` 只有 ``state`` + ``metadata``，**没有输出正文**
  （对比 ``third_party/agentscope/src/agentscope/agent/_agent.py:2021`` 的
  构造点：只传了 ``tool_call_id`` / ``state`` / ``metadata``）；
- ``ModelCallEndEvent`` 有 token 数但**没有耗时**，也**没有模型名**
  （模型名只在 ``ModelCallStartEvent`` 上）；
- ``ReplyEndEvent`` **没有**迭代轮次与工具调用次数（那是主循环的内部计数）。

于是翻译器必须**自己维护一个跨事件的小状态机**（"事件是瘦的，翻译器负责长胖"）：

=========================== ==================================================
``_model_name``             从 ``ModelCallStartEvent`` 记住，给 ``ModelCallEndEvent`` 用
``_model_started_at``       从 ``ModelCallStartEvent`` 记住，两条时间戳相减得到 ``latency_ms``
``_tool_names``             ``tool_call_id → 工具名``，``ToolCallEndEvent`` 里没有名字
``_tool_input``             ``tool_call_id → 入参增量片段``，END 时才算摘要
``_tool_chars``             ``tool_call_id → 输出字符数``，靠 ``TEXT_DELTA`` 累加
``_iterations`` / ``_tool_calls``  本 reply 的模型调用次数 / 工具调用次数
=========================== ==================================================

这个状态机就是和 ``Msg.append_event``（``message/_base.py:244``）**同构**的东西：
官方用它把流折叠成一条消息，我们用它把流折叠成审计记录。区别只有一个：
**官方的产物会进 ``state.context``，我们的产物会进不可变日志**。

**seq 的权威在这里。** :meth:`StreamTranslator._next_seq` 是会话内单调递增的
唯一来源；:meth:`to_record` 只在"确定要产出记录"之后才取号，所以事件流**不会
出现空洞**（契约 §5.2 不变式 1）。未识别的事件直接返回 ``None`` 并计入
:attr:`skipped`，不占用 seq。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, AsyncGenerator, Final, Sequence

from loguru import logger

from harness_kit.events.bus import EventBus
from harness_kit.events.types import EventKind, EventRecord
from harness_kit.session.models import INPUT_PREVIEW_LIMIT, preview

__all__ = [
    "HANDLED_EVENT_TYPES",
    "IGNORED_EVENT_TYPES",
    "MEMORY_HIT_CUSTOM_NAME",
    "StreamTranslator",
]

MEMORY_HIT_CUSTOM_NAME: Final[str] = "memory_hit"
"""``CustomEvent(name="memory_hit")`` 的约定名（``EventKind.MEMORY_HIT`` 的入口）。

AgentScope 侧**没有**记忆事件（``grep -rn "memory" event/_event.py`` 无命中），
所以"记忆被命中"只能走 :class:`~agentscope.event.CustomEvent` 这条逃生舱。
本约定由 harness_kit 定，第 19 讲的长期记忆中间件按它发射；
``value`` 需要含 ``query`` / ``chunk_ids`` / ``kept`` / ``tokens`` 四个键
（与契约 §5.2 的 ``MEMORY_HIT`` payload 一一对应）。
"""

HANDLED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "REPLY_START",
        "REPLY_END",
        "MODEL_CALL_END",
        "TOOL_CALL_END",
        "TOOL_RESULT_END",
        "HINT_BLOCK",
        "REQUIRE_USER_CONFIRM",
        "REQUIRE_EXTERNAL_EXECUTION",
        "USER_CONFIRM_RESULT",
        "USER_INTERRUPT",
        "EXTERNAL_EXECUTION_RESULT",
        "CUSTOM",
    },
)
"""会产出 ``EventRecord`` 的事件类型（12 个）。"""

IGNORED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        # 只用于给 MODEL_CALL_END 补模型名与耗时，本身不落记录
        "MODEL_CALL_START",
        # 逐字增量：审计日志不存正文，只有 SSE / UI 需要它们
        "TEXT_BLOCK_START",
        "TEXT_BLOCK_DELTA",
        "TEXT_BLOCK_END",
        "THINKING_BLOCK_START",
        "THINKING_BLOCK_DELTA",
        "THINKING_BLOCK_END",
        "DATA_BLOCK_START",
        "DATA_BLOCK_DELTA",
        "DATA_BLOCK_END",
        # 工具调用的"开始"与"增量"：入参要拼完才成摘要，所以只在 END 落记录
        "TOOL_CALL_START",
        "TOOL_CALL_DELTA",
        "TOOL_RESULT_START",
        "TOOL_RESULT_TEXT_DELTA",
        "TOOL_RESULT_DATA_DELTA",
        # 已废弃：``ExceedMaxItersEvent`` 的语义搬到了
        # ``ReplyEndEvent.finished_reason``（``event/_event.py:431`` 的 docstring
        # 写明 "emitted for backward compatibility without semantics"）
        "EXCEED_MAX_ITERS",
    },
)
"""刻意**不**产出 ``EventRecord`` 的事件类型（16 个）。

``HANDLED_EVENT_TYPES | IGNORED_EVENT_TYPES`` 必须恰好等于
``EventType`` 的全集 —— 这是"AgentScope 升级后事件类型变了，我们的日志口径
有没有跟着变"的可回归断言，见 ``tests/test_lesson03_events.py``。
"""


class StreamTranslator:
    """``AgentEvent`` 异步流 → ``EventRecord`` → :class:`EventBus`。

    用法（也是本讲验证脚本的用法）::

        bus = EventBus()
        bus.subscribe("*", on_event)          # 任意订阅者
        await bus.start()
        translator = StreamTranslator(bus, session_id=agent.state.session_id)
        translator.note_input("北京现在几点？")   # 给 REPLY_START 的 input_preview 用
        count = await translator.consume(agent.reply_stream(UserMsg("user", "...")))
        await bus.aclose()

    Attributes:
        bus (`EventBus`): 事件总线。
        session_id (`str`): 会话 id，写进每条记录。
        counts (`dict[str, int]`): 各类事件的落记录条数。
        skipped (`int`): 被识别为"不需要审计"的事件条数。
        published (`int`): 已投递的记录条数。
        errors (`int`): 投递失败的条数。
        records_seen (`int`): 从流里读到的事件对象总数（含被跳过的）。
    """

    def __init__(self, bus: EventBus, *, session_id: str) -> None:
        """初始化。

        Args:
            bus (`EventBus`): 事件总线。
            session_id (`str`): 会话 id。

        Raises:
            ValueError: ``session_id`` 为空。
        """
        if not session_id:
            raise ValueError("StreamTranslator 需要非空的 session_id")
        self.bus: EventBus = bus
        """事件总线。"""
        self.session_id: str = session_id
        """会话 id。"""
        self.counts: dict[str, int] = {}
        """各类事件（``EventKind`` 的字符串值）的落记录条数。"""
        self.skipped: int = 0
        """被跳过（不产记录）的事件条数。"""
        self.published: int = 0
        """已投递的记录条数。"""
        self.errors: int = 0
        """投递失败的条数（总线关闭等）。"""
        self.records_seen: int = 0
        """从流里读到的事件对象总数。"""
        self._seq: int = 0
        self._pending_input: str = ""
        self._model_name: str | None = None
        self._model_started_at: datetime | None = None
        self._tool_names: dict[str, str] = {}
        self._tool_input: dict[str, list[str]] = {}
        self._tool_chars: dict[str, int] = {}
        self._iterations: int = 0
        self._tool_calls: int = 0

    # ------------------------------------------------------------------
    # seq
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        """取下一个会话内序号（从 0 开始，严格递增，无洞）。

        契约 §3.3 要求它"复用 events/types.EventRecord.seq"：即编号的语义就是
        ``EventRecord.seq`` 的语义——**会话内**单调、**只在真的产出记录时**递增。

        Returns:
            `int`: 下一个可用序号。
        """
        seq = self._seq
        self._seq += 1
        return seq

    def _rollback_seq(self) -> None:
        """退回最近一次 :meth:`_next_seq`。

        只在"记录已经取号、但投递失败被吞掉"时调用（见 :meth:`records` /
        :meth:`note_confirmation`）。不退回去的话，seq 就在**落盘产物里**
        留下一个洞 —— 而契约 §5.2 不变式 1 要求的是"**已存在**的记录
        严格递增且无洞"，:func:`~harness_kit.session.models.check_seq_invariants`
        正是按落盘顺序逐条比对的。
        """
        if self._seq > 0:
            self._seq -= 1

    @property
    def next_seq(self) -> int:
        """下一个会被用到的序号（只读，给断点续跑对齐用）。

        Returns:
            `int`: ``_seq`` 的当前值。
        """
        return self._seq

    def seek(self, seq: int) -> None:
        """把计数器对齐到 ``seq``（恢复已有会话时用）。

        Args:
            seq (`int`): 下一个要使用的序号。

        Raises:
            ValueError: ``seq`` 为负，或试图回退到已发出的序号之前。
        """
        if seq < self._seq:
            raise ValueError(
                f"不能把 seq 回退到 {seq}（已发到 {self._seq}）；"
                "回到过去会让已有事件与新事件重号，违反契约 §5.2 不变式 1",
            )
        self._seq = seq

    # ------------------------------------------------------------------
    # 输入上下文
    # ------------------------------------------------------------------
    def note_input(self, text: str) -> None:
        """记下"这一轮的用户输入"，供下一条 ``REPLY_START`` 的 ``input_preview`` 用。

        为什么需要这个手工步骤：``ReplyStartEvent`` 的字段只有
        ``type`` / ``session_id`` / ``reply_id`` / ``name`` / ``role``
        （``event/_event.py:83``），**它不带用户输入**。契约 §5.2 又要求
        ``REPLY_START.payload`` 里有 ``input_preview``，所以只能由调用方在
        开始消费之前标注一次；不标注就是空串（不编造）。

        Args:
            text (`str`): 用户输入原文；内部按 :data:`INPUT_PREVIEW_LIMIT` 截断。
        """
        self._pending_input = text

    async def note_confirmation(
        self,
        *,
        tool_names: Sequence[str],
        confirmed: bool,
        reason: str = "",
    ) -> EventRecord | None:
        """把**调用方自己做的一次人工确认**记成一条 ``PERMISSION`` 记录。

        这是实测出来的一条硬需求，不是锦上添花：HITL 的恢复路径是把
        ``UserConfirmResultEvent`` **喂进** ``reply_stream``
        （``third_party/agentscope/src/agentscope/agent/_agent.py:352``），
        它是"输入"不是"输出" —— 所以事件流里**只有 ask、没有 allow**
        （``scripts/03_event_bus_from_agent.py`` 的实测输出里能看到这个缺口）。
        翻译器只能看到流，看不见调用方手里的答案，于是这一条记录只能由
        调用方显式地"报账"。

        与 :meth:`note_input` 的分工：``note_input`` 记的是**进来**的东西，
        这里记的是**决策**。两者都是"事件流不携带、但审计必须留痕"的信息。

        Args:
            tool_names (`Sequence[str]`): 被确认（或拒绝）的工具名。
            confirmed (`bool`): 用户是否批准。
            reason (`str`): 决策理由，写进 ``payload["reason"]``。

        Returns:
            `EventRecord | None`: 落下的记录；总线已关闭时返回 ``None``
            （与 :meth:`records` 一样：观测失败不打断对话）。
        """
        record = EventRecord(
            session_id=self.session_id,
            seq=self._next_seq(),
            kind=EventKind.PERMISSION,
            payload={
                "tool_name": ", ".join(str(name) for name in tool_names),
                "behavior": "allow" if confirmed else "deny",
                "reason": reason
                or (
                    "用户批准了工具调用"
                    if confirmed
                    else "用户拒绝了工具调用"
                ),
            },
            source="harness_kit.events.translate:note_confirmation",
        )
        record.warn_if_incomplete()
        self.counts[record.kind.value] = (
            self.counts.get(record.kind.value, 0) + 1
        )
        try:
            await self.bus.publish(record.kind, record)
        except Exception as exc:  # noqa: BLE001 - 观测失败不打断对话
            self.errors += 1
            self._rollback_seq()
            logger.bind(
                session_id=self.session_id,
                seq=record.seq,
            ).warning("确认记录投递失败: {}: {}", type(exc).__name__, exc)
            return None
        self.published += 1
        return record

    # ------------------------------------------------------------------
    # 翻译：审计投影
    # ------------------------------------------------------------------
    def to_record(self, event: Any) -> EventRecord | None:
        """把一个 ``AgentEvent`` 投影成 :class:`EventRecord`。

        Args:
            event (`Any`): ``AgentEvent`` 之一；传 ``Msg`` 或任何非事件对象时返回 ``None``。

        Returns:
            `EventRecord | None`: 审计记录；该事件不需要审计时返回 ``None``。
        """
        projected = self._project(event)
        if projected is None:
            self.skipped += 1
            return None
        kind, payload = projected
        record = EventRecord(
            session_id=self.session_id,
            seq=self._next_seq(),
            kind=kind,
            payload=payload,
            source="harness_kit.events.translate",
        )
        record.warn_if_incomplete()
        self.counts[kind.value] = self.counts.get(kind.value, 0) + 1
        return record

    def _project(self, event: Any) -> tuple[EventKind, dict[str, Any]] | None:
        """纯投影：只算 payload，不取 seq（返回 ``None`` 表示不产记录）。

        Args:
            event (`Any`): 事件对象。

        Returns:
            `tuple[EventKind, dict[str, Any]] | None`: ``(种类, payload)`` 或 ``None``。
        """
        from agentscope.event import EventType

        raw = getattr(event, "type", None)
        if raw is None:
            return None  # 不是事件（例如 reply_stream 最后 yield 的 Msg）
        try:
            etype = EventType(raw)
        except ValueError:  # pragma: no cover - 上游加了新事件类型才会走到
            logger.bind(session_id=self.session_id).warning(
                "未知事件类型 {}，本讲的口径未覆盖它（请更新 "
                "harness_kit/events/translate.py 的映射表）",
                raw,
            )
            return None

        # ---- 一次 reply 的边界 -------------------------------------------
        if etype == EventType.REPLY_START:
            self._iterations = 0
            self._tool_calls = 0
            self._model_name = None
            self._model_started_at = None
            self._tool_names.clear()
            self._tool_input.clear()
            self._tool_chars.clear()
            payload = {
                "reply_id": str(getattr(event, "reply_id", "")),
                "input_preview": preview(self._pending_input, INPUT_PREVIEW_LIMIT),
            }
            self._pending_input = ""
            return EventKind.REPLY_START, payload

        if etype == EventType.REPLY_END:
            return EventKind.REPLY_END, {
                "reply_id": str(getattr(event, "reply_id", "")),
                "iterations": self._iterations,
                "tool_calls": self._tool_calls,
            }

        # ---- 模型调用 -----------------------------------------------------
        if etype == EventType.MODEL_CALL_START:
            # 只做状态记忆，不产记录（它没有 token 数，也没有结束原因）
            self._model_name = getattr(event, "model_name", None)
            self._model_started_at = _parse_ts(getattr(event, "created_at", None))
            return None

        if etype == EventType.MODEL_CALL_END:
            self._iterations += 1
            latency = _elapsed_ms(
                self._model_started_at,
                _parse_ts(getattr(event, "created_at", None)),
            )
            self._model_started_at = None
            return EventKind.MODEL_CALL, {
                "model": str(self._model_name or ""),
                "prompt_tokens": int(getattr(event, "input_tokens", 0) or 0),
                "completion_tokens": int(getattr(event, "output_tokens", 0) or 0),
                "latency_ms": latency,
                "finished_reason": _text(getattr(event, "finished_reason", "")),
            }

        # ---- 工具调用（入参要拼完才算得出摘要）------------------------------
        if etype == EventType.TOOL_CALL_START:
            call_id = str(getattr(event, "tool_call_id", ""))
            self._tool_names[call_id] = str(getattr(event, "tool_call_name", ""))
            self._tool_input[call_id] = []
            return None

        if etype == EventType.TOOL_CALL_DELTA:
            call_id = str(getattr(event, "tool_call_id", ""))
            self._tool_input.setdefault(call_id, []).append(
                str(getattr(event, "delta", "") or ""),
            )
            return None

        if etype == EventType.TOOL_CALL_END:
            call_id = str(getattr(event, "tool_call_id", ""))
            raw_input = "".join(self._tool_input.pop(call_id, []))
            self._tool_calls += 1
            return EventKind.TOOL_CALL, {
                "tool_name": self._tool_names.pop(call_id, ""),
                # 只落摘要：入参里可能有 token、路径、用户数据（契约 §5.2）
                "tool_input_digest": _digest(raw_input),
                "call_id": call_id,
            }

        # ---- 工具结果（正文要在 delta 里数出来）----------------------------
        if etype == EventType.TOOL_RESULT_START:
            self._tool_chars.setdefault(str(getattr(event, "tool_call_id", "")), 0)
            return None

        if etype == EventType.TOOL_RESULT_TEXT_DELTA:
            call_id = str(getattr(event, "tool_call_id", ""))
            self._tool_chars[call_id] = self._tool_chars.get(call_id, 0) + len(
                str(getattr(event, "delta", "") or ""),
            )
            return None

        if etype == EventType.TOOL_RESULT_DATA_DELTA:
            call_id = str(getattr(event, "tool_call_id", ""))
            blob = getattr(event, "data", None) or getattr(event, "url", None) or ""
            self._tool_chars[call_id] = self._tool_chars.get(call_id, 0) + len(
                str(blob),
            )
            return None

        if etype == EventType.TOOL_RESULT_END:
            call_id = str(getattr(event, "tool_call_id", ""))
            state = _text(getattr(event, "state", ""))
            return EventKind.TOOL_RESULT, {
                "call_id": call_id,
                "state": state,
                "chars": self._tool_chars.pop(call_id, 0),
                "error": state if state not in ("success", "running") else None,
            }

        # ---- 权限 / 人工确认 ---------------------------------------------
        if etype == EventType.REQUIRE_USER_CONFIRM:
            return EventKind.PERMISSION, {
                "tool_name": _tool_names(getattr(event, "tool_calls", [])),
                "behavior": "ask",
                "reason": "AgentScope 要求用户确认后执行",
            }

        if etype == EventType.USER_CONFIRM_RESULT:
            results = list(getattr(event, "confirm_results", []) or [])
            confirmed = bool(results) and all(
                bool(getattr(item, "confirmed", False)) for item in results
            )
            return EventKind.PERMISSION, {
                "tool_name": _tool_names(
                    [getattr(item, "tool_call", None) for item in results],
                ),
                "behavior": "allow" if confirmed else "deny",
                "reason": f"用户对 {len(results)} 个工具调用做了确认",
            }

        if etype == EventType.REQUIRE_EXTERNAL_EXECUTION:
            return EventKind.PERMISSION, {
                "tool_name": _tool_names(getattr(event, "tool_calls", [])),
                "behavior": "passthrough",
                "reason": "权限层不拦截，交给外部执行器",
            }

        # ---- 逃生舱：不需要污染 EventKind 的那些 ----------------------------
        if etype == EventType.HINT_BLOCK:
            return EventKind.CUSTOM, {
                "name": "hint_block",
                "data": {
                    "reply_id": str(getattr(event, "reply_id", "")),
                    "block_id": str(getattr(event, "block_id", "")),
                    "source": getattr(event, "source", None),
                    "chars": _hint_chars(getattr(event, "hint", None)),
                },
            }

        if etype == EventType.USER_INTERRUPT:
            return EventKind.CUSTOM, {
                "name": "user_interrupt",
                "data": {"reply_id": str(getattr(event, "reply_id", ""))},
            }

        if etype == EventType.EXTERNAL_EXECUTION_RESULT:
            results = list(getattr(event, "execution_results", []) or [])
            return EventKind.CUSTOM, {
                "name": "external_execution_result",
                "data": {
                    "reply_id": str(getattr(event, "reply_id", "")),
                    "call_ids": [str(getattr(item, "id", "")) for item in results],
                    "states": [_text(getattr(item, "state", "")) for item in results],
                },
            }

        if etype == EventType.CUSTOM:
            name = str(getattr(event, "name", ""))
            value = getattr(event, "value", None) or {}
            if name == MEMORY_HIT_CUSTOM_NAME:
                return EventKind.MEMORY_HIT, {
                    "query": str(value.get("query", "")),
                    "chunk_ids": list(value.get("chunk_ids", []) or []),
                    "kept": list(value.get("kept", []) or []),
                    "tokens": int(value.get("tokens", 0) or 0),
                }
            return EventKind.CUSTOM, {"name": name, "data": dict(value)}

        # ---- 其余 16 种：见模块级 IGNORED_EVENT_TYPES ------------------------
        return None

    # ------------------------------------------------------------------
    # 翻译：SSE / UI 投影
    # ------------------------------------------------------------------
    @staticmethod
    def to_sse_frame(chunk: Any) -> dict[str, Any] | None:
        """把一个 ``AgentEvent`` 投影成一帧 SSE 载荷（**无状态**，可当工具函数用）。

        与 :meth:`to_record` 的分工：这里**不做任何过滤与聚合**，原样透传
        ``model_dump(mode="json")`` —— 前端要的就是逐字增量。所以它是
        ``@staticmethod``：谁都能调，不需要一个 translator 实例。

        帧形状与 AgentScope 官方 SSE 路由一致：``{"event": <类型名>, "data": {...}}``，
        其中 ``event`` 是 ``"TEXT_BLOCK_DELTA"`` 这类**大写**事件类型名
        （来自 ``EventType`` 的字符串值，``event/_event.py:26``）。

        Args:
            chunk (`Any`): 流里的元素：``AgentEvent`` 或 ``reply_stream`` 最后
                yield 的 ``Msg``。

        Returns:
            `dict[str, Any] | None`: ``{"event": ..., "data": ...}``；
            不是事件对象时返回 ``None``（``Msg`` 的文本已经通过
            ``TEXT_BLOCK_DELTA`` 推过了，不重复推；要完整消息请读
            ``agent.state.context[-1]``）。
        """
        raw = getattr(chunk, "type", None)
        if raw is None:
            return None
        dumper = getattr(chunk, "model_dump", None)
        if not callable(dumper):
            return None
        return {"event": str(raw), "data": dumper(mode="json")}

    # ------------------------------------------------------------------
    # 消费
    # ------------------------------------------------------------------
    async def consume(self, stream: AsyncGenerator[Any, None]) -> int:
        """耗尽事件流，逐条翻译并投递，返回**产出的记录条数**。

        契约 §3.3 明确要求"必须用 ``async for``，禁止 ``list()`` 整个流"：
        事件流是**实时**的，``list()`` 会把它攒成一个列表——那就等于放弃了流式，
        首字延迟会退化到"整轮结束"，而且 HITL 场景下流可能长时间挂着不结束。

        Args:
            stream (`AsyncGenerator[Any, None]`): ``Agent.reply_stream(...)`` 的产物。

        Returns:
            `int`: 实际产出并投递的记录条数。
        """
        produced = 0
        async for record in self.records(stream):
            produced += 1
        return produced

    async def records(
        self,
        stream: AsyncGenerator[Any, None],
    ) -> AsyncGenerator[EventRecord, None]:
        """把事件流翻译成**记录流**（逐条 yield，同时投递到总线）。

        单独暴露它的理由：有些消费者只想"边翻译边自己处理"，不想经过总线
        （例如把事件直接写进一个本地文件）。:meth:`consume` 就是它的计数包装。

        Args:
            stream (`AsyncGenerator[Any, None]`): 事件流。

        Yields:
            `EventRecord`: 翻译出来的记录（已在 yield 前投递过）。
        """
        async for event in stream:
            self.records_seen += 1
            record = self.to_record(event)
            if record is None:
                continue
            try:
                await self.bus.publish(record.kind, record)
            except Exception as exc:  # noqa: BLE001 - 观测失败不打断对话
                self.errors += 1
                self._rollback_seq()  # 没投出去就不该占号，否则落盘产物有洞
                logger.bind(
                    session_id=self.session_id,
                    seq=record.seq,
                    kind=record.kind.value,
                ).warning("事件投递失败: {}: {}", type(exc).__name__, exc)
                continue
            self.published += 1
            yield record

    # ------------------------------------------------------------------
    # 指标
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """把翻译器的健康度压成一个 dict（写日志 / 上报指标用）。

        Returns:
            `dict[str, Any]`: 计数与分布。
        """
        return {
            "session_id": self.session_id,
            "records_seen": self.records_seen,
            "records": self.published,
            "skipped": self.skipped,
            "errors": self.errors,
            "last_seq": self.next_seq - 1,
            "counts": dict(sorted(self.counts.items())),
        }


# ----------------------------------------------------------------------
# 模块级小工具
# ----------------------------------------------------------------------
def _text(value: Any) -> str:
    """把枚举/字符串统一成字符串。

    因为 ``EventBase`` 设了 ``use_enum_values=True``（``event/_event.py:73``），
    事件里的枚举字段落下来**已经是字符串**；但手工构造的事件对象可能还是枚举实例，
    统一走一遍 ``value`` 属性最省心。

    Args:
        value (`Any`): 枚举实例、字符串或其它对象。

    Returns:
        `str`: 字符串形式。
    """
    return str(getattr(value, "value", value))


def _digest(raw: str) -> str:
    """算入参摘要（sha1 前 16 位十六进制）。

    与 ``harness_kit/middleware/tracing.py:_digest`` 同一口径：入参可能含凭据，
    不能原样进日志；但完全不留痕又没法排查，摘要正好"可关联、不可逆"。

    Args:
        raw (`str`): 原始入参（工具调用拼完的 JSON 字符串）。

    Returns:
        `str`: 摘要；``raw`` 为空时返回 ``""``。
    """
    if not raw:
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_ts(value: Any) -> datetime | None:
    """解析 AgentScope 事件上的 ``created_at``（ISO 字符串）。

    ``EventBase.created_at`` 是 ``datetime.now().isoformat()`` 的产物
    （``event/_event.py:77``）——**本地时区、naive**。这里只做"同一台机器上
    两次时间相减"，所以不补时区也不会错。

    Args:
        value (`Any`): ISO 字符串或 ``None``。

    Returns:
        `datetime | None`: 解析结果；无法解析时为 ``None``。
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _elapsed_ms(start: datetime | None, end: datetime | None) -> float:
    """算两次事件时间戳之间的毫秒数。

    Args:
        start (`datetime | None`): 起点；``None`` 视为不可测。
        end (`datetime | None`): 终点；``None`` 视为不可测。

    Returns:
        `float`: 毫秒数；不可测时返回 ``0.0``（**不编造**延迟数据）。
    """
    if start is None or end is None:
        return 0.0
    return round((end - start).total_seconds() * 1000, 3)


def _tool_names(tool_calls: Any) -> str:
    """把一组 ``ToolCallBlock`` 或 ``ConfirmResult`` 里的工具名拼成一行。

    Args:
        tool_calls (`Any`): 可迭代对象；每项可能是 ``ToolCallBlock``
            （有 ``.name``），也可能带 ``.tool_call`` 属性。

    Returns:
        `str`: ``"a, b"`` 形式；空集合返回 ``""``。
    """
    names: list[str] = []
    for item in tool_calls or []:
        block = getattr(item, "tool_call", item)
        name = getattr(block, "name", None)
        if name:
            names.append(str(name))
    return ", ".join(names)


def _hint_chars(hint: Any) -> int:
    """算 ``HintBlock.hint`` 的字符数（``str`` 或 ``list[TextBlock | DataBlock]``）。

    Args:
        hint (`Any`): ``HintBlock.hint`` 的值。

    Returns:
        `int`: 字符数；无法计算时为 ``0``。
    """
    if hint is None:
        return 0
    if isinstance(hint, str):
        return len(hint)
    total = 0
    for block in hint:
        total += len(str(getattr(block, "text", "") or ""))
    return total
