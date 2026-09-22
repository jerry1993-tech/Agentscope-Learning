# -*- coding: utf-8 -*-
"""会话存储的抽象基类（契约 §3.9，第 9 讲）。

**为什么需要这一层**

AgentScope 只有一个持久化边界：:class:`~agentscope.state.AgentState`
（``third_party/agentscope/src/agentscope/state/_state.py:209``）。它是**可变**的
—— :meth:`Agent.on_compress_context` 会把 ``state.context`` 就地换成压缩后的摘要
（调用点 ``third_party/agentscope/src/agentscope/agent/_agent.py:434``），
被压掉的消息**再也找不回来**。``app/`` 层虽然有一个 replay，但它只是
**有界内存窗口**：``_SESSION_REPLAY_MAX_LEN = 1000``
（``third_party/agentscope/src/agentscope/app/message_bus/_keys.py:126``，
消费点 ``.../app/message_bus/_base.py:576``），进程重启即失忆，且不落盘。

本模块定义的 :class:`SessionStoreBase` 就是补上这个缺口的**不可变追加日志**：

- 写：只有 :meth:`SessionStoreBase.append` / :meth:`append_many`，没有 update / delete；
- 读：:meth:`read` / :meth:`latest_seq` / :meth:`list_sessions` / :meth:`load_snapshot`；
- 不变式（契约 §5.2）：同一会话内 ``seq`` 从 0 起严格递增且无洞；事件一经 append
  永不修改；快照的 ``seq`` 必须落在事件流的某个真实 ``seq`` 上。

**快照为什么也挂在这里**

快照不是事件（它描述的是"某个 seq 处的整体状态"，不是"又发生了一件事"），
但它必须与事件流**在同一个存储介质上、用同一套生命周期**管理，否则
"事件在 A 库、快照在 B 库"会带来灾难性的不一致。所以 :class:`SessionStoreBase`
的接口里既有事件方法也有快照方法，具体存储实现（JSONL / SQLite）各自决定物理布局。

**方法名与契约的对应**：契约 §3.9 钉死了 ``append`` / ``read`` / ``list_sessions`` /
``latest_seq`` / ``aclose``；本模块另外按任务书补齐了
``save_snapshot`` / ``load_snapshot`` / ``latest_snapshot``，并给
``append_event`` / ``load_events`` 提供了契约口径的别名（两者完全等价，
别名只是为了让教程正文里"append_event / load_events"的说法也能直接落到代码上）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from harness_kit.events import EventRecord
from harness_kit.session.models import (
    SessionEvent,
    SessionInvariantError,
    SessionMeta,
    SessionSnapshot,
    check_seq_invariants,
)

__all__ = [
    "SessionLockedError",
    "SessionNotFoundError",
    "SessionStoreBase",
]


class SessionNotFoundError(KeyError):
    """请求的会话在存储里不存在。"""


class SessionLockedError(RuntimeError):
    """另一个进程正持有该会话的写锁。

    事件流是**单写者**结构：两个进程同时给同一个 ``session_id`` 追加事件，
    ``seq`` 的分配会撞车（两边都读到"最后一个 seq 是 7"，于是都写 8）。
    harness_kit 的选择是**显式失败**而不是"尽力而为地写坏它"。
    """


class SessionStoreBase(ABC):
    """不可变追加日志的存储接口。

    **语义契约**（任何实现都必须满足，契约 §3.9 / §5.2）：

    1. :meth:`append` 成功返回后，任何接口都不能修改或删除该事件；
    2. 同一 ``session_id`` 内 ``seq`` 从 0 起严格递增且无洞 —— 违反时
       :meth:`append` 抛 :class:`~harness_kit.session.models.SessionInvariantError`；
    3. :meth:`latest_seq` 对空会话返回 ``-1``（"还没发生过任何事件"），
       于是"下一个可用 seq"恒为 ``latest_seq + 1``；
    4. :meth:`save_snapshot` 落盘的 ``SessionSnapshot.seq`` 必须等于该会话事件流里
       某个真实的 ``seq``（空会话允许 ``-1``），否则抛
       :class:`~harness_kit.session.models.SessionInvariantError`。

    Example:
        >>> store = JsonlSessionStore(Path("/tmp/harness/sessions"))
        >>> await store.append(EventRecord(session_id="s1", seq=0, kind=EventKind.SESSION_START))
        >>> await store.latest_seq("s1")
        0
    """

    # ------------------------------------------------------------------
    # 事件：写
    # ------------------------------------------------------------------
    @abstractmethod
    async def append(self, record: EventRecord) -> None:
        """追加一条事件记录。

        Args:
            record (`EventRecord`): 待追加的不可变事件记录。

        Raises:
            SessionInvariantError: ``record.seq`` 不等于 ``latest_seq + 1``
                （跳号、重复或倒序），或存储里已有其它会话的同名事件。
            SessionLockedError: 另一个进程正持有该会话的写锁。
        """

    async def append_many(self, records: Sequence[EventRecord]) -> None:
        """批量追加。

        默认实现逐条调用 :meth:`append`；子类可以覆写为批量提交
        （例如 SQLite 用单个事务）。

        Args:
            records (`Sequence[EventRecord]`): 按 ``seq`` 升序排列的事件。

        Raises:
            SessionInvariantError: 任一条违反 ``seq`` 不变式。
            SessionLockedError: 另一个进程正持有该会话的写锁。
        """
        for record in records:
            await self.append(record)

    # ------------------------------------------------------------------
    # 事件：读
    # ------------------------------------------------------------------
    @abstractmethod
    async def read(
        self,
        session_id: str,
        *,
        since_seq: int = 0,
        limit: int | None = None,
    ) -> list[SessionEvent]:
        """读取 ``seq >= since_seq`` 的事件，按 ``seq`` 升序。

        Args:
            session_id (`str`): 会话 id。
            since_seq (`int`): 起始 ``seq``（**闭区间**），默认 0。
            limit (`int | None`): 最多返回多少条；``None`` 表示不限。
                用于"只判存在性"的场景（例如校验快照锚点）。

        Returns:
            `list[SessionEvent]`: 落盘形态的事件列表；会话不存在时返回空列表。
        """

    @abstractmethod
    async def latest_seq(self, session_id: str) -> int:
        """该会话最后一个事件的 ``seq``。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 最后一条事件的 ``seq``；会话不存在或没有任何事件时返回 ``-1``。
        """

    @abstractmethod
    async def list_sessions(self) -> list[SessionMeta]:
        """列出全部会话的索引卡片（不含事件正文）。

        Returns:
            `list[SessionMeta]`: 按 ``updated_at`` 倒序（最近的在前）。
        """

    async def meta(self, session_id: str) -> SessionMeta | None:
        """取单个会话的索引卡片。

        默认实现走 :meth:`list_sessions` 线性查找；实现了 O(1) 索引的存储
        （例如 JSONL 的 sidecar 元数据文件）应当覆写它。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMeta | None`: 卡片；会话不存在时 ``None``。
        """
        for meta in await self.list_sessions():
            if meta.session_id == session_id:
                return meta
        return None

    async def next_seq(self, session_id: str) -> int:
        """下一个可用的 ``seq``（``latest_seq + 1``，空会话为 0）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 下一个可用 ``seq``。
        """
        return await self.latest_seq(session_id) + 1

    async def verify_invariants(self, session_id: str) -> list[SessionEvent]:
        """把整个会话读出来并校验契约 §5.2 不变式 1 / 2。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `list[SessionEvent]`: 读出的全部事件（校验通过）。

        Raises:
            SessionInvariantError: ``seq`` 有洞、重复或串了会话。
        """
        events = await self.read(session_id)
        check_seq_invariants(events, expected_session_id=session_id)
        return events

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    @abstractmethod
    async def save_snapshot(self, snapshot: SessionSnapshot) -> None:
        """落盘一个状态快照。

        快照是**追加式**的：同一个会话可以有很多个快照，读取时取
        "``seq`` 最大且不超过指定位置"的那个，因此写快照永远不会破坏旧快照。

        Args:
            snapshot (`SessionSnapshot`): 待落盘的快照。

        Raises:
            SessionInvariantError: ``snapshot.seq`` 不是该会话事件流里真实存在的 ``seq``。
        """

    @abstractmethod
    async def load_snapshot(
        self,
        session_id: str,
        *,
        at_or_before: int | None = None,
    ) -> SessionSnapshot | None:
        """读取 ``seq <= at_or_before`` 的最新快照。

        Args:
            session_id (`str`): 会话 id。
            at_or_before (`int | None`): 上界（含）；``None`` 表示"最新一个"。

        Returns:
            `SessionSnapshot | None`: 快照；没有落过任何快照时 ``None``。
        """

    async def latest_snapshot(self, session_id: str) -> SessionSnapshot | None:
        """读取该会话最新的快照（``at_or_before=None`` 的语法糖）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionSnapshot | None`: 快照或 ``None``。
        """
        return await self.load_snapshot(session_id)

    async def list_snapshots(self, session_id: str) -> list[SessionSnapshot]:
        """列出该会话的全部快照，按 ``seq`` 升序。

        默认实现只返回"最新一个"，够 :class:`~harness_kit.session.resume.SessionResumer`
        使用；需要完整时间旅行（回到任意 seq）的实现应当覆写。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `list[SessionSnapshot]`: 快照列表。
        """
        latest = await self.load_snapshot(session_id)
        return [] if latest is None else [latest]

    # ------------------------------------------------------------------
    # 生命周期 / 别名
    # ------------------------------------------------------------------
    @abstractmethod
    async def aclose(self) -> None:
        """释放存储持有的资源（连接池、文件句柄）。

        JSONL 实现每次操作即时开关文件，收尾是空操作；SQLite 实现要关连接。
        契约 §3.9 把它列为抽象方法，正是为了让"忘记关"这类问题在**构造子类时**
        就被 Python 拦下来，而不是在生产上泄漏句柄。
        """

    async def append_event(self, record: EventRecord) -> None:
        """:meth:`append` 的契约别名（语义完全一致）。

        Args:
            record (`EventRecord`): 待追加的事件记录。
        """
        await self.append(record)

    async def load_events(
        self,
        session_id: str,
        *,
        since_seq: int = 0,
        limit: int | None = None,
    ) -> list[SessionEvent]:
        """:meth:`read` 的契约别名（语义完全一致）。

        Args:
            session_id (`str`): 会话 id。
            since_seq (`int`): 起始 ``seq``（闭区间）。
            limit (`int | None`): 最多返回条数。

        Returns:
            `list[SessionEvent]`: 事件列表。
        """
        return await self.read(session_id, since_seq=since_seq, limit=limit)

    # ------------------------------------------------------------------
    # 子类共用的校验
    # ------------------------------------------------------------------
    @staticmethod
    def check_appendable(record: EventRecord, last_seq: int) -> None:
        """校验 ``record`` 可以接在 ``last_seq`` 之后。

        Args:
            record (`EventRecord`): 待追加的事件。
            last_seq (`int`): 该会话当前最后一个 ``seq``（空会话为 ``-1``）。

        Raises:
            SessionInvariantError: ``seq`` 跳号、重复或倒序。
        """
        expected = last_seq + 1
        if record.seq != expected:
            raise SessionInvariantError(
                f"会话 {record.session_id} 的事件流要求 seq 从 0 起严格递增且无洞："
                f"当前最后一条是 {last_seq}，因此新事件必须是 {expected}，"
                f"收到 {record.seq}（契约 §5.2 不变式 1）",
            )

    @staticmethod
    def check_snapshot_anchor(snapshot: SessionSnapshot, events: Sequence[SessionEvent]) -> None:
        """校验 ``snapshot.seq`` 落在事件流的真实 ``seq`` 上（契约 §5.2 不变式 3）。

        Args:
            snapshot (`SessionSnapshot`): 待校验的快照。
            events (`Sequence[SessionEvent]`): 该快照锚点附近的事件（至少包含锚点那一条）。

        Raises:
            SessionInvariantError: 事件流里没有这个 ``seq``。
        """
        if snapshot.seq < 0:
            return
        if not any(item.seq == snapshot.seq for item in events):
            raise SessionInvariantError(
                f"快照锚点 seq={snapshot.seq} 在会话 {snapshot.session_id} 的事件流里"
                "不存在（契约 §5.2 不变式 3：SessionSnapshot.seq 必须等于某个已存在的"
                " EventRecord.seq）",
            )
