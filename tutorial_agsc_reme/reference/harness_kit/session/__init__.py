# -*- coding: utf-8 -*-
"""会话层：事件溯源 / 快照 / 回放 / 断点续跑（契约 §3.9，第 9 讲）。

四层结构，职责严格分层（**上层只依赖下层**）：

============================ ==============================================================
:mod:`~harness_kit.session.models`    纯数据结构：``SessionMeta`` / ``SessionEvent`` /
                                ``SessionSnapshot``，以及 ``AgentState`` ↔ 快照的双向通道
                                （:func:`snapshot_from_state` / :func:`restore_state`）。
                                **没有任何 I/O。**
:mod:`~harness_kit.session.store`     存储抽象 ``SessionStoreBase``：不可变追加日志的接口
                                与不变式校验（``seq`` 无洞、快照锚点必须真实存在）。
:mod:`~harness_kit.session.jsonl_store` 落地实现 ``JsonlSessionStore``：按会话分文件、
                                每行一个 zstd 帧、``flock`` 单写者、元数据 sidecar。
:mod:`~harness_kit.session.sqlite_store` 落地实现 ``SqliteSessionStore``：``sessions`` /
                                ``events`` / ``blobs`` / ``snapshots`` 四张表，大 payload 外置。
:mod:`~harness_kit.session.snapshot`  ``Snapshotter``：按事件条数间隔把 ``AgentState``
                                固化成快照，把"恢复要重放的步数"钉死在 ``every_n_events``。
:mod:`~harness_kit.session.replay`    ``SessionReplayer``：从事件流重建**发生了什么**
                                （时间线 / 回合 / 差异 / token 用量）。
:mod:`~harness_kit.session.resume`    ``SessionResumer``：快照 + 尾部事件 → 可继续对话的
                                ``AgentState``，并判定"是干净收尾、还是 park 在确认上"。
============================ ==============================================================

**为什么 replay 和 resume 是两件事**：事件日志是**审计记录**（``REPLY_START`` 只存截断到
500 字符的 ``input_preview``，没有消息体），因此它**不能**重算出上下文；能重算上下文的
只有快照里的 ``AgentState.context``。``replay`` 回答"这轮聊了什么"，``resume`` 回答
"怎么接着聊"——前者不需要快照，后者必须要有。
"""

from harness_kit.session.jsonl_store import (
    DEFAULT_SESSION_DIR,
    EVENT_SUFFIXES,
    JsonlSessionStore,
)
from harness_kit.session.models import (
    INPUT_PREVIEW_LIMIT,
    SESSION_SCHEMA_VERSION,
    SessionEvent,
    SessionInvariantError,
    SessionMeta,
    SessionSnapshot,
    check_seq_invariants,
    next_seq,
    preview,
    restore_state,
    snapshot_from_state,
    tail_events,
)
from harness_kit.session.replay import (
    ReplayResult,
    ReplayTurn,
    SessionReplayer,
    TokenUsage,
)
from harness_kit.session.resume import (
    NoSnapshotError,
    ResumeError,
    ResumeResult,
    SessionResumer,
)
from harness_kit.session.snapshot import (
    DEFAULT_SNAPSHOT_INTERVAL,
    Snapshotter,
)
from harness_kit.session.sqlite_store import (
    DEFAULT_BLOB_THRESHOLD,
    SqliteSessionStore,
)
from harness_kit.session.store import (
    SessionLockedError,
    SessionNotFoundError,
    SessionStoreBase,
)

__all__ = [
    "DEFAULT_BLOB_THRESHOLD",
    "DEFAULT_SESSION_DIR",
    "DEFAULT_SNAPSHOT_INTERVAL",
    "EVENT_SUFFIXES",
    "INPUT_PREVIEW_LIMIT",
    "SESSION_SCHEMA_VERSION",
    "JsonlSessionStore",
    "NoSnapshotError",
    "ReplayResult",
    "ReplayTurn",
    "ResumeError",
    "ResumeResult",
    "SessionEvent",
    "SessionInvariantError",
    "SessionLockedError",
    "SessionMeta",
    "SessionNotFoundError",
    "SessionReplayer",
    "SessionResumer",
    "SessionSnapshot",
    "SessionStoreBase",
    "Snapshotter",
    "SqliteSessionStore",
    "TokenUsage",
    "check_seq_invariants",
    "next_seq",
    "preview",
    "restore_state",
    "snapshot_from_state",
    "tail_events",
]
