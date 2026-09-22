# -*- coding: utf-8 -*-
"""状态快照（契约 §3.9，第 9 讲）。

**快照解决的是"回放太长"的问题，不是"回放不存在"的问题**

事件溯源系统的最小恢复路径是"从 seq=0 把所有事件重放一遍"。这在本项目里**做不到**，
而且原因是架构级的：:class:`~agentscope.state.AgentState` 是 AgentScope 唯一的持久化
边界（``third_party/agentscope/src/agentscope/state/_state.py:209``），而
``harness_kit`` 的事件（:class:`~harness_kit.events.EventRecord`）是**审计记录**，
不是可重放的指令流 —— ``REPLY_START`` 只存 ``input_preview``（截断到 500 字符），
``MODEL_CALL`` 只存 token 数，都不足以重建出一条 ``Msg``。

所以恢复的正确姿势是：**快照（权威状态） + 尾部事件（增量线索）**。
:class:`Snapshotter` 负责前者：把 ``AgentState.model_dump(mode="json")`` 连同
"这个状态覆盖到哪个 seq"一起落盘（``snapshot_from_state``，见
:mod:`harness_kit.session.models`），从而让第 9 讲的
:class:`~harness_kit.session.resume.SessionResumer` 能从 O(尾部) 而不是 O(全部) 恢复。

**写快照的触发策略**：每 ``every_n_events`` 条事件写一个。为什么不是每条都写？
因为快照是整个 ``context`` 的完整序列化，成本随上下文长度增长；而事件是增量。
200 是一个工程折中：**最多重放 200 条事件就能追上最新状态**，同时快照体积可控。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from harness_kit.session.models import (
    SessionSnapshot,
    snapshot_from_state,
)
from harness_kit.session.store import SessionStoreBase

__all__ = [
    "DEFAULT_SNAPSHOT_INTERVAL",
    "Snapshotter",
]

DEFAULT_SNAPSHOT_INTERVAL: int = 200
"""默认快照间隔（事件条数）。契约 §3.9 的 ``every_n_events`` 默认值。"""


class Snapshotter:
    """把 ``AgentState`` 变成 :class:`~harness_kit.session.SessionSnapshot` 并落盘。

    **为什么是"间隔式"而不是"事件式"**：``AgentState`` 本身就是"事件的累积结果"，
    对每条事件都快照等于把同一份上下文抄 N 遍。间隔式快照把
    "恢复到最新状态需要重放的步数"钉死在 ``every_n_events`` 以内。

    **锚点不变式**：``SessionSnapshot.seq`` 必须等于某个已存在的 ``EventRecord.seq``
    （契约 §5.2 不变式 3）。本类不做"猜测 seq"，而是直接取
    :meth:`~harness_kit.session.store.SessionStoreBase.latest_seq`，并让存储层复核
    这个锚点确实存在。空会话（``seq == -1``）允许落一个"什么都没发生"的快照。

    Example:
        >>> snapshotter = Snapshotter(store, every_n_events=100)
        >>> snapshot = await snapshotter.force_snapshot(agent.state)
        >>> snapshot.seq == await store.latest_seq(agent.state.session_id)
        True
    """

    def __init__(self, store: SessionStoreBase, *, every_n_events: int = DEFAULT_SNAPSHOT_INTERVAL) -> None:
        """构造快照器。

        Args:
            store (`SessionStoreBase`): 快照的落盘目标（与事件流同一个存储）。
            every_n_events (`int`): 距上一个快照至少积累多少条事件才写新快照。

        Raises:
            ValueError: ``every_n_events`` 小于 1。
        """
        if every_n_events < 1:
            raise ValueError(f"every_n_events 必须 >= 1，收到 {every_n_events}")

        self.store: SessionStoreBase = store
        self.every_n_events: int = every_n_events

        self.snapshots_taken: int = 0
        """本进程内累计写入的快照数（诊断用）。"""

        self.last_snapshot: SessionSnapshot | None = None
        """本进程内最近一次写入的快照。"""

    # ------------------------------------------------------------------
    # 触发式快照
    # ------------------------------------------------------------------
    async def maybe_snapshot(self, agent_state: Any) -> SessionSnapshot | None:
        """按间隔策略决定是否写快照。

        判定规则（契约 §3.9 的 ``maybe_snapshot``）：

        - 会话还没有任何事件（``latest_seq == -1``）→ 不写，返回 ``None``
          （没有可锚定的 seq，写了也只是个空快照）；
        - ``latest_seq - 上一个快照的 seq >= every_n_events`` → 调 :meth:`force_snapshot`；
        - 否则返回 ``None``。

        Args:
            agent_state (`Any`): AgentScope ``AgentState``（pydantic 模型）。

        Returns:
            `SessionSnapshot | None`: 本次写入的快照，或 ``None``（没到阈值）。

        Raises:
            TypeError: ``agent_state`` 不是 pydantic 模型。
        """
        session_id = getattr(agent_state, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise TypeError(
                "agent_state 必须带 session_id 属性（AgentScope AgentState 的约定，"
                "third_party/agentscope/src/agentscope/state/_state.py:212）",
            )

        latest = await self.store.latest_seq(session_id)
        if latest < 0:
            logger.bind(session_id=session_id).trace("会话尚无事件，跳过快照")
            return None

        previous = await self.store.latest_snapshot(session_id)
        previous_seq = -1 if previous is None else previous.seq
        if latest - previous_seq < self.every_n_events:
            logger.bind(
                session_id=session_id,
                latest=latest,
                previous=previous_seq,
                threshold=self.every_n_events,
            ).trace("未达到快照阈值，跳过")
            return None

        return await self.force_snapshot(agent_state)

    async def force_snapshot(self, agent_state: Any) -> SessionSnapshot:
        """无条件写一个快照（锚点 = 当前 :meth:`latest_seq`）。

        用在两个地方：① 间隔到了；② **主动收口** —— 例如一次对话结束、进程要退出、
        或者评测要固定现场。收口时写一个快照能让"断点续跑"从 O(1) 开始。

        Args:
            agent_state (`Any`): AgentScope ``AgentState``。

        Returns:
            `SessionSnapshot`: 已落盘的快照。

        Raises:
            TypeError: ``agent_state`` 缺 ``session_id``。
            SessionInvariantError: 锚点在事件流里不存在（说明事件被外部改过）。
        """
        session_id = getattr(agent_state, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("agent_state 必须带 session_id 属性")

        seq = await self.store.latest_seq(session_id)
        snapshot = snapshot_from_state(agent_state, seq=seq)
        await self.store.save_snapshot(snapshot)

        self.snapshots_taken += 1
        self.last_snapshot = snapshot
        logger.bind(
            session_id=session_id,
            seq=seq,
            context_len=self._context_len(agent_state),
        ).debug("已写入快照 #{}", self.snapshots_taken)
        return snapshot

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def last_snapshot_seq(self, session_id: str) -> int:
        """该会话最新快照的锚点 ``seq``（无快照时 ``-1``）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 锚点 ``seq``。
        """
        snapshot = await self.store.latest_snapshot(session_id)
        return -1 if snapshot is None else snapshot.seq

    async def pending_events(self, session_id: str) -> int:
        """自上一个快照以来累积了多少条事件（诊断 / 判断是否值得收口）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: ``latest_seq - last_snapshot_seq``；无事件时为 0。
        """
        latest = await self.store.latest_seq(session_id)
        if latest < 0:
            return 0
        previous = await self.last_snapshot_seq(session_id)
        return latest - previous

    @staticmethod
    def _context_len(agent_state: Any) -> int:
        """``AgentState.context`` 的消息条数（日志用，取不到就给 -1）。

        Args:
            agent_state (`Any`): AgentState。

        Returns:
            `int`: 消息条数。
        """
        context = getattr(agent_state, "context", None)
        return len(context) if isinstance(context, list) else -1
