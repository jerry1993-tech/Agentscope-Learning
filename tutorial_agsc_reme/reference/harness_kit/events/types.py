# -*- coding: utf-8 -*-
"""不可变事件记录（``EventRecord`` / ``EventKind``）。

与 AgentScope 的 ``AgentEvent`` 的关键区别：

- ``AgentEvent`` 只在**一次 reply 的流**里存在，出了流就没了
  （``third_party/agentscope/src/agentscope/event/_event.py:83`` 起是全部事件类）；
- ``EventRecord`` 是可序列化、可落盘、**永不修剪**的审计记录，是 harness_kit
  「会话事件溯源」的地基（第 3 / 第 9 讲）。

不变式（第 9 讲正文必须断言）：

1. 同一 ``session_id`` 内 ``seq`` 严格递增且无洞，从 0 开始；
2. ``EventRecord`` 一经 append 永不修改、永不删除（``frozen=True``）；
3. ``SessionSnapshot.seq`` 必须等于某个已存在的 ``EventRecord.seq``；
4. 从 ``snapshot.seq + 1`` 开始重放尾部事件，必须能重建出与 ``snapshot.agent_state``
   同构的状态。
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

WILDCARD_TOPIC: str = "*"
"""订阅全部主题的通配符（``EventBus.subscribe("*", handler)``）。"""


class EventKind(StrEnum):
    """事件种类。字符串值即落盘值，一旦发布不得改名。"""

    SESSION_START = "session_start"
    REPLY_START = "reply_start"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION = "permission"
    MEMORY_HIT = "memory_hit"
    REPLY_END = "reply_end"
    CUSTOM = "custom"


PAYLOAD_FIELDS: dict[EventKind, tuple[str, ...]] = {
    EventKind.SESSION_START: ("profile", "agent_name", "cwd"),
    EventKind.REPLY_START: ("reply_id", "input_preview"),
    EventKind.MODEL_CALL: (
        "model",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "finished_reason",
    ),
    EventKind.TOOL_CALL: ("tool_name", "tool_input_digest", "call_id"),
    EventKind.TOOL_RESULT: ("call_id", "state", "chars", "error"),
    EventKind.PERMISSION: ("tool_name", "behavior", "reason"),
    EventKind.MEMORY_HIT: ("query", "chunk_ids", "kept", "tokens"),
    EventKind.REPLY_END: ("reply_id", "iterations", "tool_calls"),
    EventKind.CUSTOM: ("name", "data"),
}
"""每种 ``kind`` 的 ``payload`` 必备字段（契约 §5.2 的硬约定）。

只做「必备字段」校验，不禁止生产者多塞字段：生产中常常需要把
``tenant_id`` / ``trace_id`` 一并带上，多出来的键由消费方自行解释。
"""


def topic_matches(topic: str | EventKind, kind: EventKind) -> bool:
    """判断一个订阅主题是否命中某个事件种类（事件过滤的唯一规则）。

    ``*`` 命中全部；其余情况按**相等**匹配。因为 :class:`EventKind` 继承
    ``StrEnum``，``EventKind.TOOL_CALL == "tool_call"`` 为 ``True``，
    所以订阅方写枚举或字符串都能工作。

    Args:
        topic (`str | EventKind`): 订阅时登记的主题。
        kind (`EventKind`): 事件的种类。

    Returns:
        `bool`: 命中为 ``True``。

    Example:
        >>> topic_matches("*", EventKind.CUSTOM)
        True
        >>> topic_matches("tool_call", EventKind.TOOL_CALL)
        True
        >>> topic_matches("tool_call", EventKind.MEMORY_HIT)
        False
    """
    if topic == WILDCARD_TOPIC:
        return True
    return kind == topic


def utc_now() -> datetime:
    """返回当前 UTC 时间（tz-aware）。

    Returns:
        `datetime`: 带 ``timezone.utc`` 时区的当前时间。
    """
    return datetime.now(timezone.utc)


class EventRecord(BaseModel):
    """不可变事件记录：可序列化、可落盘、永不修剪。"""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    """事件 id，``uuid4().hex``。"""

    session_id: str
    """所属会话 id。"""

    seq: int = Field(ge=0)
    """会话内单调递增序号，从 0 开始且无洞。"""

    kind: EventKind
    """事件种类，决定 ``payload`` 的 schema（见 :data:`PAYLOAD_FIELDS`）。"""

    ts: datetime = Field(default_factory=utc_now)
    """事件时间，一律 UTC 且 tz-aware。"""

    payload: dict[str, Any] = Field(default_factory=dict)
    """事件负载，字段约定见 :data:`PAYLOAD_FIELDS`。"""

    source: str = "harness_kit"
    """事件生产者标识。"""

    def missing_payload_fields(self) -> list[str]:
        """返回本记录 ``payload`` 中缺失的必备字段。

        Returns:
            `list[str]`: 缺失字段名；为空表示符合契约。
        """
        required = PAYLOAD_FIELDS.get(self.kind, ())
        return [name for name in required if name not in self.payload]

    def warn_if_incomplete(self) -> list[str]:
        """校验 ``payload`` 并在缺字段时打一条 warning 日志。

        Returns:
            `list[str]`: 缺失字段名；为空表示符合契约。
        """
        missing = self.missing_payload_fields()
        if missing:
            logger.bind(
                session_id=self.session_id,
                event_id=self.event_id,
            ).warning(
                "EventRecord(kind={}) payload 缺少契约字段: {}",
                self.kind.value,
                missing,
            )
        return missing
