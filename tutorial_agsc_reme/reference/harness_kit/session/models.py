# -*- coding: utf-8 -*-
"""会话与事件记录的数据结构（契约 §3.3 / §5.2）。

三个模型的分工：

- :class:`SessionMeta` —— 会话的**索引卡片**（不含正文），``list_sessions()`` 返回它；
- :class:`SessionEvent` —— **落盘形态**：``EventRecord`` 外面套一层 ``v`` 版本号，
  将来要改 ``payload`` 的 schema 时靠它做迁移；
- :class:`SessionSnapshot` —— ``AgentState`` 的**序列化切片**，恢复会话的锚点。

与 AgentScope 的关系：``AgentState`` 是 AgentScope 唯一的持久化边界
（``third_party/agentscope/src/agentscope/state/_state.py:209``），
:func:`snapshot_from_state` / :func:`restore_state` 就是它和
:class:`SessionSnapshot` 之间的双向通道。事件流本身则对应
``Agent`` 的 ``reply_stream`` 产出的 ``AgentEvent``
（``third_party/agentscope/src/agentscope/agent/_agent.py:288``），
由第 3 讲的 ``events/translate.py`` 翻译成 :class:`~harness_kit.events.EventRecord`，
再经类的:class:`SessionEvent` 落盘。

**不变式**（契约 §5.2 硬约定，本模块用 :func:`check_seq_invariants` 强制）：

1. 同一 ``session_id`` 内 ``seq`` 严格递增且无洞；
2. ``EventRecord`` 一经 append 永不修改、永不删除（``frozen=True`` 在
   :mod:`harness_kit.events.types` 里保证）；
3. ``SessionSnapshot.seq`` 必须等于某个已存在的 ``EventRecord.seq``；
4. 从 ``snapshot.seq + 1`` 开始重放尾部事件，必须能重建同构的 ``AgentState``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness_kit.events import EventRecord, EventKind, utc_now

__all__ = [
    "INPUT_PREVIEW_LIMIT",
    "SESSION_SCHEMA_VERSION",
    "SessionInvariantError",
    "SessionEvent",
    "SessionMeta",
    "SessionSnapshot",
    "check_seq_invariants",
    "next_seq",
    "preview",
    "restore_state",
    "snapshot_from_state",
    "tail_events",
]

SESSION_SCHEMA_VERSION: int = 1
"""``SessionEvent.v`` 的当前值。改动落盘结构时 +1，并在 store 里加迁移分支。"""

INPUT_PREVIEW_LIMIT: int = 500
"""``REPLY_START.payload["input_preview"]`` 的截断长度（契约 §5.2 表格硬约定）。"""


class SessionInvariantError(ValueError):
    """会话事件流违反了契约 §5.2 的不变式。"""


def preview(text: str, limit: int = INPUT_PREVIEW_LIMIT) -> str:
    """截断成预览串。事件日志**不存**完整输入，只存预览。

    Args:
        text (`str`): 原始文本。
        limit (`int`): 最大字符数，默认 :data:`INPUT_PREVIEW_LIMIT`。

    Returns:
        `str`: 截断后的文本（超出时追加 ``"..."``）。

    Example:
        >>> preview("a" * 600)[-3:]
        '...'
        >>> len(preview("a" * 600))
        503
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


class SessionMeta(BaseModel):
    """会话索引卡片（契约 §3.3）。

    Attributes:
        session_id (`str`): 会话 id。
        profile_name (`str`): 使用的 Profile 名。
        created_at (`datetime`): 创建时间（UTC, tz-aware）。
        updated_at (`datetime`): 最后写入时间（UTC, tz-aware）。
        event_count (`int`): 事件条数，非负。
        tags (`list[str]`): 自由标签。
    """

    session_id: str = Field(min_length=1)
    profile_name: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    event_count: int = Field(default=0, ge=0)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_time_order(self) -> "SessionMeta":
        """校验 ``updated_at >= created_at`` 且两者都 tz-aware。

        Returns:
            `SessionMeta`: 校验通过的自身。

        Raises:
            ValueError: 时间顺序颠倒，或丢掉了时区。
        """
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError(
                "created_at / updated_at 必须是 tz-aware 的 UTC 时间"
                "（裸 datetime 在不同机器上会读出不同时刻）",
            )
        if self.updated_at < self.created_at:
            raise ValueError(
                f"updated_at({self.updated_at.isoformat()}) 早于 "
                f"created_at({self.created_at.isoformat()})",
            )
        return self

    @classmethod
    def create(
        cls,
        session_id: str,
        *,
        profile_name: str,
        tags: Sequence[str] | None = None,
    ) -> "SessionMeta":
        """新建一张卡片，两个时间戳取同一时刻。

        Args:
            session_id (`str`): 会话 id。
            profile_name (`str`): Profile 名。
            tags (`Sequence[str] | None`): 初始标签。

        Returns:
            `SessionMeta`: 新建的卡片。
        """
        now = utc_now()
        return cls(
            session_id=session_id,
            profile_name=profile_name,
            created_at=now,
            updated_at=now,
            tags=list(tags or []),
        )

    def touched(self, *, event_count: int | None = None) -> "SessionMeta":
        """返回一份 ``updated_at`` 刷新过的副本（卡片本身不可变地更新）。

        Args:
            event_count (`int | None`): 新的事件计数；``None`` 表示沿用旧值。

        Returns:
            `SessionMeta`: 新卡片。
        """
        payload = self.model_dump()
        payload["updated_at"] = utc_now()
        if event_count is not None:
            payload["event_count"] = event_count
        return SessionMeta.model_validate(payload)


class SessionEvent(BaseModel):
    """落盘形态：:class:`EventRecord` + 版本号（契约 §3.3）。

    Attributes:
        v (`int`): schema 版本，见 :data:`SESSION_SCHEMA_VERSION`。
        record (`EventRecord`): 真正的事件记录。
    """

    v: int = Field(default=SESSION_SCHEMA_VERSION, ge=1)
    record: EventRecord

    @classmethod
    def wrap(cls, record: EventRecord) -> "SessionEvent":
        """把一个 :class:`EventRecord` 包成落盘形态。

        Args:
            record (`EventRecord`): 事件记录。

        Returns:
            `SessionEvent`: 包装结果。
        """
        return cls(v=SESSION_SCHEMA_VERSION, record=record)

    @property
    def seq(self) -> int:
        """``record.seq`` 的快捷访问（排序、切片时最常用）。

        Returns:
            `int`: 事件序号。
        """
        return self.record.seq

    @property
    def kind(self) -> EventKind:
        """``record.kind`` 的快捷访问。

        Returns:
            `EventKind`: 事件类型。
        """
        return self.record.kind

    def to_json_line(self) -> str:
        """序列化成一行 JSONL（不含换行符）。

        Returns:
            `str`: 单行 JSON。
        """
        return self.model_dump_json()


class SessionSnapshot(BaseModel):
    """``AgentState`` 的序列化切片（契约 §3.3 / §5.2）。

    Attributes:
        session_id (`str`): 会话 id。
        seq (`int`): 快照覆盖到的 ``seq``（含）。
        agent_state (`dict[str, Any]`): ``AgentState.model_dump(mode="json")``。
        created_at (`datetime`): 生成时间（UTC, tz-aware）。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    seq: int = Field(ge=-1)
    agent_state: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check_state_payload(self) -> "SessionSnapshot":
        """校验 ``agent_state`` 是映射且带 ``session_id``，时间戳带时区。

        ``seq`` 允许 ``-1``：表示"还没发生过任何事件"的空快照，
        恢复时等价于全新会话。其余情况由 :func:`check_seq_invariants` 和
        store 层校验"必须命中某个已存在的 seq"。

        Returns:
            `SessionSnapshot`: 校验通过的自身。

        Raises:
            ValueError: 时间戳丢时区，或 ``agent_state`` 缺 ``session_id``。
        """
        if self.created_at.tzinfo is None:
            raise ValueError("SessionSnapshot.created_at 必须是 tz-aware 的 UTC 时间")
        if "session_id" not in self.agent_state:
            raise ValueError(
                "SessionSnapshot.agent_state 必须含 'session_id'"
                "（它是 AgentState.model_dump(mode='json') 的产物）",
            )
        return self

    @property
    def is_empty(self) -> bool:
        """是否是"什么都没发生"的空快照。

        Returns:
            `bool`: ``seq == -1`` 时为 ``True``。
        """
        return self.seq < 0


def snapshot_from_state(
    agent_state: Any,
    *,
    seq: int,
    created_at: datetime | None = None,
) -> SessionSnapshot:
    """``AgentState`` → :class:`SessionSnapshot`（契约 §3.3 的 store 只读接口用）。

    Args:
        agent_state (`Any`): AgentScope ``AgentState``；必须是 pydantic 模型，
            以便调用 ``model_dump(mode="json")``。
        seq (`int`): 快照覆盖到的 ``seq``。
        created_at (`datetime | None`): 生成时间；``None`` 用当前 UTC。

    Returns:
        `SessionSnapshot`: 快照。

    Raises:
        TypeError: ``agent_state`` 没有 ``model_dump``（不是 pydantic 模型）。
    """
    dumper = getattr(agent_state, "model_dump", None)
    if not callable(dumper):
        raise TypeError(
            f"AgentState 必须是 pydantic 模型，收到 {type(agent_state).__name__}；"
            "SessionSnapshot.agent_state 的约定就是 model_dump(mode='json') 的输出"
            "（third_party/agentscope/src/agentscope/state/_state.py:209）",
        )
    return SessionSnapshot(
        session_id=agent_state.session_id,
        seq=seq,
        agent_state=dumper(mode="json"),
        created_at=created_at or utc_now(),
    )


def restore_state(snapshot: SessionSnapshot) -> Any:
    """:class:`SessionSnapshot` → ``AgentState``（会话恢复的第 1 步）。

    Args:
        snapshot (`SessionSnapshot`): 快照。

    Returns:
        `Any`: 重建的 ``AgentState``。
    """
    from agentscope.state import AgentState

    return AgentState.model_validate(snapshot.agent_state)


def next_seq(events: Iterable[SessionEvent | EventRecord]) -> int:
    """算下一个可用的 ``seq``（``max(seq) + 1``，空集合给 0）。

    Args:
        events (`Iterable[SessionEvent | EventRecord]`): 已有事件。

    Returns:
        `int`: 下一个 ``seq``。
    """
    highest = -1
    for item in events:
        seq: int = item.seq
        if seq > highest:
            highest = seq
    return highest + 1


def check_seq_invariants(
    events: Sequence[SessionEvent | EventRecord],
    *,
    expected_session_id: str | None = None,
) -> None:
    """校验契约 §5.2 不变式 1（同会话内 ``seq`` 从 0 起严格递增、无洞）。

    Args:
        events (`Sequence[SessionEvent | EventRecord]`): **按落盘顺序**排列的事件。
        expected_session_id (`str | None`): 若给出，额外校验会话 id 一致。

    Raises:
        SessionInvariantError: 出现重复 ``seq``、跳号、起始不为 0，或串了会话。
    """
    seen: set[int] = set()
    for index, item in enumerate(events):
        record = item.record if isinstance(item, SessionEvent) else item
        if record.seq != index:
            raise SessionInvariantError(
                f"第 {index} 条事件的 seq={record.seq}，"
                "要求从 0 开始、严格递增且无洞（契约 §5.2 不变式 1）",
            )
        if record.seq in seen:
            raise SessionInvariantError(f"seq={record.seq} 重复")
        seen.add(record.seq)
        if (
            expected_session_id is not None
            and record.session_id != expected_session_id
        ):
            raise SessionInvariantError(
                f"第 {index} 条事件属于会话 {record.session_id}，"
                f"期望 {expected_session_id}",
            )


def tail_events(
    events: Sequence[SessionEvent | EventRecord],
    *,
    after_seq: int,
) -> list[SessionEvent | EventRecord]:
    """取 ``seq > after_seq`` 的尾部事件（会话恢复的第 2 步）。

    Args:
        events (`Sequence[SessionEvent | EventRecord]`): 全部事件。
        after_seq (`int`): 快照覆盖到的 ``seq``。

    Returns:
        `list[SessionEvent | EventRecord]`: 尾部事件，保持原有顺序。
    """
    return [
        item
        for item in events
        if (item.record if isinstance(item, SessionEvent) else item).seq > after_seq
    ]
