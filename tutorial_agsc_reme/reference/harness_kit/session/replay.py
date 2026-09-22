# -*- coding: utf-8 -*-
"""事件回放：从不可变事件流重建"当时发生了什么"（契约 §3.9，第 9 讲）。

**回放的边界（这一节必须先说清楚，否则教程会写歪）**

``harness_kit`` 的事件日志是**审计日志**，不是**可重放指令流**。两者的差别在于：
指令流能 1:1 复原状态，审计日志只能复原"发生了什么"。具体到本项目的 payload
契约（契约 §5.2）：

============================ ==========================================================
``REPLY_START``              只有 ``input_preview``（截断 500 字符）
``MODEL_CALL``               只有模型名 / token 数 / 耗时 / 结束原因，**没有消息体**
``TOOL_CALL``                只有 ``tool_name`` + ``tool_input_digest`` + ``call_id``
``TOOL_RESULT``              只有 ``call_id`` / ``state`` / ``chars`` / ``error``
============================ ==========================================================

因此本模块给两类不同的答案，**绝不混为一谈**：

1. **"发生了什么"** —— :meth:`SessionReplayer.timeline` / :meth:`fold` / :meth:`diff`
   / :meth:`turns`。纯读、无副作用、不碰 AgentScope，可离线跑、可审计、可评测；
2. **"当时的消息是什么"** —— :meth:`SessionReplayer.context_from_snapshot`。答案来自
   快照里的 ``AgentState.context``（快照是权威状态），而**不是**从事件拼出来的。
   谁要是试图从 ``input_preview`` 拼回完整 prompt，那是自欺欺人。

**与第 3 讲的分工**：``events/translate.py`` 的 ``StreamTranslator`` 负责把
AgentScope 的 ``AgentEvent`` **翻译成** ``EventRecord`` 并投递到 ``EventBus``；
本模块负责把这些 ``EventRecord``（已经落盘的那份）**折回**成可读的结论。
一条是"写进去"的路，一条是"读出来"的路，共用同一套 :class:`~harness_kit.events.EventRecord`。

**``TokenUsage`` 为什么定义在这里而不是 import reme**：契约 §3.9 的 ``ReplayResult``
用 ``TokenUsage``，而 ReMe 侧的同名类在 ``third_party/ReMe/reme/schema/token_usage.py:8``。
但 ``session`` 是 Layer 1 的基础设施，**不应为了一个三元组把整个 ReMe 拖进来**
（``import reme`` 会拉起完整的 ReMe 包，且本环境存在 0.3.1.10 抢先的坑，见
``tutorial_agsc_reme/_recon/00_environment_and_smoke.md``）。所以这里定义一个
**字段名与语义逐字一致**的本地 ``TokenUsage``；需要与 ReMe 互转时用
``reme.schema.TokenUsage.model_validate(local.model_dump())`` 即可（字段同名同义）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness_kit.events import EventKind, EventRecord
from harness_kit.session.models import SessionSnapshot
from harness_kit.session.store import SessionStoreBase

__all__ = [
    "ReplayResult",
    "ReplayTurn",
    "SessionReplayer",
    "TokenUsage",
]


class TokenUsage(BaseModel):
    """跨 provider 的 token 计数（与 ReMe 的 ``TokenUsage`` 同构）。

    只保留 input / output 两个原始计数，``total_tokens`` 一律由
    ``model_validator(mode="after")`` 派生 —— 与
    ``third_party/ReMe/reme/schema/token_usage.py:21`` 完全一致，
    这样"从事件流折出来的用量"与"provider 报的用量"可以直接相加比较。

    Attributes:
        input_tokens (`int`): 输入 token 数。
        output_tokens (`int`): 输出 token 数。
        total_tokens (`int`): 恒等于前两者之和（派生，不要手动赋值）。
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    """输入 token 数。"""

    output_tokens: int = Field(default=0, ge=0)
    """输出 token 数。"""

    total_tokens: int = Field(default=0, ge=0)
    """派生字段：``input_tokens + output_tokens``。"""

    @model_validator(mode="after")
    def _set_total(self) -> "TokenUsage":
        """把 ``total_tokens`` 钉成两个分量的和。

        Returns:
            `TokenUsage`: 自身。
        """
        self.total_tokens = self.input_tokens + self.output_tokens
        return self

    @classmethod
    def combine(cls, usages: list["TokenUsage"]) -> "TokenUsage":
        """累加多段用量。

        Args:
            usages (`list[TokenUsage]`): 分段用量。

        Returns:
            `TokenUsage`: 合计。
        """
        return cls(
            input_tokens=sum(item.input_tokens for item in usages),
            output_tokens=sum(item.output_tokens for item in usages),
        )


class ReplayResult(BaseModel):
    """一次回放的结论（契约 §3.9 的字段级定义，不得改名）。

    Attributes:
        session_id (`str`): 会话 id。
        event_count (`int`): 参与本次回放的事件条数。
        tool_calls (`list[dict[str, Any]]`): 工具调用清单，每项含
            ``call_id`` / ``tool_name`` / ``state`` / ``chars`` / ``error``。
        token_usage (`TokenUsage`): 由 ``MODEL_CALL`` 事件累加出的用量。
        errors (`list[str]`): 事件的 ``error`` 字段 + 回放期发现的结构性问题
            （例如工具调用没有结果、回复没有收尾）。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    """会话 id。"""

    event_count: int = Field(default=0, ge=0)
    """参与回放的事件条数。"""

    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    """工具调用清单。"""

    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    """累加出的 token 用量。"""

    errors: list[str] = Field(default_factory=list)
    """错误与结构性问题。"""


class ReplayTurn(BaseModel):
    """一次 ``reply`` 的边界与产出（``REPLY_START`` ↔ ``REPLY_END`` 之间的一切）。

    这是"重建消息"的正确粒度：**回合**。真正的消息内容在快照里
    （:meth:`SessionReplayer.context_from_snapshot`），事件流给出的是回合的骨架。

    Attributes:
        reply_id (`str`): 回合 id（``REPLY_START.payload["reply_id"]``）。
        start_seq (`int`): ``REPLY_START`` 的 ``seq``。
        end_seq (`int | None`): ``REPLY_END`` 的 ``seq``；未收尾时 ``None``。
        input_preview (`str`): 输入预览（截断 500 字符，契约 §5.2）。
        iterations (`int`): ``REPLY_END.payload["iterations"]``。
        closed (`bool`): 是否收到过 ``REPLY_END``。
        tool_calls (`list[str]`): 本回合调用的工具名（按发生顺序）。
        tool_results (`list[dict[str, Any]]`): 本回合的工具结果摘要。
        memory_hits (`list[dict[str, Any]]`): 本回合的记忆命中。
        model_calls (`int`): ``MODEL_CALL`` 次数。
        token_usage (`TokenUsage`): 本回合的 token 用量。
        permissions (`list[dict[str, Any]]`): 本回合的权限决策。
    """

    model_config = ConfigDict(extra="forbid")

    reply_id: str
    """回合 id。"""

    start_seq: int = Field(ge=0)
    """``REPLY_START`` 的 ``seq``。"""

    end_seq: int | None = None
    """``REPLY_END`` 的 ``seq``；``None`` 表示没收到收尾事件。"""

    input_preview: str = ""
    """输入预览。"""

    iterations: int = Field(default=0, ge=0)
    """循环轮数。"""

    closed: bool = False
    """是否已收尾。"""

    tool_calls: list[str] = Field(default_factory=list)
    """本回合的工具调用名。"""

    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    """本回合的工具结果。"""

    memory_hits: list[dict[str, Any]] = Field(default_factory=list)
    """本回合的记忆命中。"""

    model_calls: int = Field(default=0, ge=0)
    """本回合的模型调用次数。"""

    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    """本回合的 token 用量。"""

    permissions: list[dict[str, Any]] = Field(default_factory=list)
    """本回合的权限决策（``PERMISSION`` 事件）。"""


class SessionReplayer:
    """按事件流重建"当时发生了什么"。语义：**纯读、无副作用**。

    纯读意味着：不写任何文件、不改任何状态、不调 LLM。因此它可以被安全地用在
    审计、评测（第 20 讲）、排障与"事后复盘"里。

    Example:
        >>> replayer = SessionReplayer(store)
        >>> result = await replayer.fold("s1")
        >>> result.event_count == len(await replayer.timeline("s1"))
        True
    """

    def __init__(self, store: SessionStoreBase) -> None:
        """构造回放器。

        Args:
            store (`SessionStoreBase`): 事件来源。
        """
        self.store: SessionStoreBase = store

    # ------------------------------------------------------------------
    # 基础读取
    # ------------------------------------------------------------------
    async def timeline(self, session_id: str) -> list[EventRecord]:
        """按 ``seq`` 升序列出全部事件（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `list[EventRecord]`: 事件记录列表（只读快照，不是可写的活对象）。
        """
        events = await self.store.read(session_id)
        return [item.record for item in events]

    async def timeline_until(self, session_id: str, seq: int) -> list[EventRecord]:
        """只取 ``seq <= 上界`` 的事件（"回到某个时刻"）。

        Args:
            session_id (`str`): 会话 id。
            seq (`int`): 上界（含）。

        Returns:
            `list[EventRecord]`: 事件列表。
        """
        if seq < 0:
            return []
        events = await self.store.read(session_id, limit=seq + 1)
        return [item.record for item in events]

    # ------------------------------------------------------------------
    # 折叠
    # ------------------------------------------------------------------
    async def fold(self, session_id: str) -> ReplayResult:
        """把整个会话折成一个结论（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `ReplayResult`: 工具调用清单 + token 用量 + 错误清单。
        """
        return self.fold_records(await self.timeline(session_id), session_id=session_id)

    async def fold_until(self, session_id: str, seq: int) -> ReplayResult:
        """折到某个 ``seq`` 为止（"如果当时就停在这里，结论是什么"）。

        Args:
            session_id (`str`): 会话 id。
            seq (`int`): 上界（含）。

        Returns:
            `ReplayResult`: 结论。
        """
        return self.fold_records(
            await self.timeline_until(session_id, seq),
            session_id=session_id,
        )

    @staticmethod
    def fold_records(records: list[EventRecord], *, session_id: str | None = None) -> ReplayResult:
        """纯函数式折叠：**不碰存储**，因此可以被单测直接调用。

        它也是"回放"这件事最朴素的定义 —— 一个 ``reduce``。

        Args:
            records (`list[EventRecord]`): 按 ``seq`` 升序的事件。
            session_id (`str | None`): 覆盖结果里的会话 id（默认取第一条事件的）。

        Returns:
            `ReplayResult`: 折叠结果。
        """
        sid = session_id or (records[0].session_id if records else "")
        usage_input = 0
        usage_output = 0
        tool_calls: list[dict[str, Any]] = []
        errors: list[str] = []
        open_calls: dict[str, dict[str, Any]] = {}

        for record in records:
            payload = record.payload
            if record.kind is EventKind.MODEL_CALL:
                usage_input += int(payload.get("prompt_tokens") or 0)
                usage_output += int(payload.get("completion_tokens") or 0)
            elif record.kind is EventKind.TOOL_CALL:
                call_id = str(payload.get("call_id") or "")
                entry = {
                    "call_id": call_id,
                    "tool_name": str(payload.get("tool_name") or ""),
                    "input_digest": str(payload.get("tool_input_digest") or ""),
                    "seq": record.seq,
                    "state": None,
                    "chars": 0,
                    "error": None,
                }
                tool_calls.append(entry)
                if call_id:
                    open_calls[call_id] = entry
            elif record.kind is EventKind.TOOL_RESULT:
                call_id = str(payload.get("call_id") or "")
                entry = open_calls.pop(call_id, None)
                if entry is None:
                    entry = {
                        "call_id": call_id,
                        "tool_name": "",
                        "input_digest": "",
                        "seq": record.seq,
                        "state": None,
                        "chars": 0,
                        "error": None,
                    }
                    tool_calls.append(entry)
                entry["state"] = payload.get("state")
                entry["chars"] = int(payload.get("chars") or 0)
                entry["error"] = payload.get("error")
                if payload.get("error"):
                    errors.append(f"seq={record.seq} 工具 {entry['tool_name']}: {payload['error']}")
            elif record.kind is EventKind.CUSTOM:
                if payload.get("name") == "error":
                    errors.append(f"seq={record.seq} {payload.get('data')}")

        for entry in open_calls.values():
            errors.append(
                f"seq={entry['seq']} 工具 {entry['tool_name'] or entry['call_id']} "
                "只有 TOOL_CALL 没有 TOOL_RESULT（回合被中断或进程被杀）",
            )

        return ReplayResult(
            session_id=sid,
            event_count=len(records),
            tool_calls=tool_calls,
            token_usage=TokenUsage(
                input_tokens=usage_input,
                output_tokens=usage_output,
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # 回合
    # ------------------------------------------------------------------
    async def turns(self, session_id: str) -> list[ReplayTurn]:
        """把事件流切成回合列表（``REPLY_START`` 开头，``REPLY_END`` 收尾）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `list[ReplayTurn]`: 回合列表；未收尾的回合 ``closed=False``。
        """
        return self.turns_from_records(await self.timeline(session_id))

    @staticmethod
    def turns_from_records(records: list[EventRecord]) -> list[ReplayTurn]:
        """纯函数式切分回合（可单测）。

        Args:
            records (`list[EventRecord]`): 按 ``seq`` 升序的事件。

        Returns:
            `list[ReplayTurn]`: 回合列表。
        """
        turns: list[ReplayTurn] = []
        current: ReplayTurn | None = None
        call_names: dict[str, str] = {}
        usage_input = 0
        usage_output = 0

        def _close(turn: ReplayTurn) -> ReplayTurn:
            turn.token_usage = TokenUsage(
                input_tokens=usage_input,
                output_tokens=usage_output,
            )
            return turn

        for record in records:
            payload = record.payload
            kind = record.kind
            if kind is EventKind.REPLY_START:
                if current is not None:
                    turns.append(_close(current))
                usage_input = 0
                usage_output = 0
                current = ReplayTurn(
                    reply_id=str(payload.get("reply_id") or ""),
                    start_seq=record.seq,
                    input_preview=str(payload.get("input_preview") or ""),
                )
                continue
            if current is None:
                # 收尾事件或工具事件出现在 REPLY_START 之前：视为"前一轮的余波"，跳过
                continue
            if kind is EventKind.REPLY_END:
                current.end_seq = record.seq
                current.closed = True
                current.iterations = int(payload.get("iterations") or 0)
                turns.append(_close(current))
                current = None
                usage_input = 0
                usage_output = 0
            elif kind is EventKind.TOOL_CALL:
                name = str(payload.get("tool_name") or "")
                current.tool_calls.append(name)
                call_names[str(payload.get("call_id") or "")] = name
            elif kind is EventKind.TOOL_RESULT:
                current.tool_results.append(
                    {
                        "call_id": str(payload.get("call_id") or ""),
                        "tool_name": call_names.get(str(payload.get("call_id") or ""), ""),
                        "state": payload.get("state"),
                        "chars": int(payload.get("chars") or 0),
                        "seq": record.seq,
                    },
                )
            elif kind is EventKind.MEMORY_HIT:
                current.memory_hits.append(
                    {
                        "query": str(payload.get("query") or ""),
                        "kept": list(payload.get("kept") or []),
                        "tokens": int(payload.get("tokens") or 0),
                        "seq": record.seq,
                    },
                )
            elif kind is EventKind.PERMISSION:
                current.permissions.append(
                    {
                        "tool_name": str(payload.get("tool_name") or ""),
                        "behavior": str(payload.get("behavior") or ""),
                        "reason": str(payload.get("reason") or ""),
                        "seq": record.seq,
                    },
                )
            elif kind is EventKind.MODEL_CALL:
                current.model_calls += 1
                usage_input += int(payload.get("prompt_tokens") or 0)
                usage_output += int(payload.get("completion_tokens") or 0)

        if current is not None:
            turns.append(_close(current))
        return turns

    # ------------------------------------------------------------------
    # 差分
    # ------------------------------------------------------------------
    async def diff(self, session_id: str, seq_a: int, seq_b: int) -> dict[str, Any]:
        """``seq_a`` 与 ``seq_b`` 之间发生了什么（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。
            seq_a (`int`): 起点 ``seq``（**不含**）。
            seq_b (`int`): 终点 ``seq``（**含**）。

        Returns:
            `dict[str, Any]`: 含 ``added`` / ``kinds`` / ``tool_calls`` /
            ``models`` / ``errors`` / ``token_delta`` 的摘要。

        Raises:
            ValueError: ``seq_b < seq_a``。
        """
        if seq_b < seq_a:
            raise ValueError(f"要求 seq_b >= seq_a，收到 seq_a={seq_a}, seq_b={seq_b}")

        records = [
            record
            for record in await self.timeline(session_id)
            if seq_a < record.seq <= seq_b
        ]
        kinds: dict[str, int] = {}
        tool_names: list[str] = []
        models: list[str] = []
        errors: list[str] = []
        for record in records:
            kinds[record.kind.value] = kinds.get(record.kind.value, 0) + 1
            if record.kind is EventKind.TOOL_CALL:
                tool_names.append(str(record.payload.get("tool_name") or ""))
            elif record.kind is EventKind.MODEL_CALL:
                models.append(str(record.payload.get("model") or ""))
            if record.payload.get("error"):
                errors.append(str(record.payload["error"]))

        window = self.fold_records(records, session_id=session_id)
        return {
            "session_id": session_id,
            "from_seq": seq_a,
            "to_seq": seq_b,
            "added": len(records),
            "first_seq": records[0].seq if records else None,
            "last_seq": records[-1].seq if records else None,
            "kinds": kinds,
            "tool_calls": tool_names,
            "models": models,
            "errors": errors,
            "token_delta": window.token_usage.model_dump(),
        }

    # ------------------------------------------------------------------
    # 权威状态（来自快照，不是拼出来的）
    # ------------------------------------------------------------------
    async def context_from_snapshot(
        self,
        session_id: str,
        *,
        at_or_before: int | None = None,
    ) -> list[Any]:
        """从快照里取出"当时的消息列表"（``AgentState.context``）。

        这是唯一可信的"重建消息"途径：``AgentState`` 是 AgentScope 的持久化边界
        （``third_party/agentscope/src/agentscope/state/_state.py:220`` 的 ``context``），
        而事件流里没有消息体。

        Args:
            session_id (`str`): 会话 id。
            at_or_before (`int | None`): 快照锚点上界（含）。

        Returns:
            `list[Msg]`: AgentScope 消息列表；没有快照时返回空列表。

        Raises:
            ValueError: 快照里的 ``agent_state`` 结构不合法（被外部改过）。
        """
        from agentscope.message import Msg

        snapshot = await self.store.load_snapshot(session_id, at_or_before=at_or_before)
        if snapshot is None:
            return []
        raw = snapshot.agent_state.get("context")
        if not isinstance(raw, list):
            raise ValueError(
                f"快照 {session_id}@{snapshot.seq} 的 agent_state.context 不是列表，"
                "文件可能被外部修改过",
            )
        return [Msg.model_validate(item) for item in raw]

    async def snapshot_of(self, session_id: str) -> SessionSnapshot | None:
        """取最新快照（透传存储层，方便调用方只依赖回放器）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionSnapshot | None`: 快照或 ``None``。
        """
        return await self.store.latest_snapshot(session_id)

    async def summarize(self, session_id: str) -> dict[str, Any]:
        """把 :meth:`fold` 与 :meth:`turns` 的结论合成一份人类可读摘要。

        教程 / CLI / 评测都用它打印"这段会话到底干了什么"。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `dict[str, Any]`: 含 ``events`` / ``turns`` / ``tool_calls`` /
            ``tokens`` / ``errors`` / ``closed_turns`` 的摘要。
        """
        turns = await self.turns(session_id)
        result = await self.fold(session_id)
        return {
            "session_id": session_id,
            "events": result.event_count,
            "turns": len(turns),
            "closed_turns": sum(1 for turn in turns if turn.closed),
            "tool_calls": [entry["tool_name"] for entry in result.tool_calls],
            "tokens": result.token_usage.model_dump(),
            "errors": result.errors,
        }
