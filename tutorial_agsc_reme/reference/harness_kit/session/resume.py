# -*- coding: utf-8 -*-
"""断点续跑：从快照 + 尾部事件恢复一个**可继续对话**的 ``AgentState``（契约 §3.9，第 9 讲）。

**这一步到底恢复了什么**

::

    AgentState  = 快照里的 agent_state（权威状态，含完整 context）
                + 尾部事件的"增量线索"（这一轮之后发生了什么）

尾部事件**不能**重算出上下文（事件里没有消息体，见
:mod:`harness_kit.session.replay` 的说明），它们能做的是**判断这次会话是怎么停下来的**，
从而决定恢复出来的状态能不能直接继续用：

=============================== ===========================================================
尾部最后一条是 ``REPLY_END``    回合干净收尾 → 直接 ``agent.reply(下一条输入)`` 即可
尾部停在 ``REPLY_START`` 之后    回合没收尾（进程被杀 / 挂了）→ 见下
且存在 ASKING 的工具调用          会话是**park 在用户确认**上的 → 回传
                                ``UserConfirmResultEvent`` 才能继续（第 11 讲的
                                :class:`~harness_kit.permission.hitl.HITLBridge` 正好接这里）
=============================== ===========================================================

**为什么不去"修补"半截回合**：AgentScope 已经把这件事做完了 ——
``UserInterruptEvent`` 的语义就是"关掉所有未完成的工具调用、补一条 assistant 消息、
以 ``ReplyEndReason.INTERRUPTED`` 结束这个 reply"
（``third_party/agentscope/src/agentscope/event/_event.py:496`` 的类 docstring）。
本模块只**生成**这个事件（:meth:`SessionResumer.interrupt_event`），
由调用方喂回 ``agent.reply_stream`` —— 我们绝不自己写一遍事件循环。

**权限策略的时效性**：快照里带着**生成快照那一刻**的 ``permission_context``。
恢复时以**当前 Profile** 的权限声明为准（:meth:`SessionResumer.resume` 会重建它），
理由是安全策略必须"即时生效"——不能因为会话是三天前建的，就用三天前的宽松策略继续放行。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.events import EventKind
from harness_kit.session.models import (
    SessionEvent,
    SessionMeta,
    SessionSnapshot,
    restore_state,
)
from harness_kit.session.snapshot import Snapshotter
from harness_kit.session.store import SessionStoreBase

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行期不 import agentscope
    from agentscope.agent import Agent
    from agentscope.event import UserInterruptEvent
    from agentscope.state import AgentState

    from harness_kit.config.schema import ResolvedProfile

__all__ = [
    "NoSnapshotError",
    "ResumeError",
    "ResumeResult",
    "SessionResumer",
]


class ResumeError(RuntimeError):
    """会话无法恢复。"""


class NoSnapshotError(ResumeError):
    """会话有事件但没有快照，因此**没有权威状态可恢复**。

    这是一个刻意设计的失败：与其"从零拼一个空上下文"让用户以为历史还在，
    不如明确告诉他"这个会话恢复不了，因为没有快照"。
    生产里应该由 :class:`~harness_kit.session.snapshot.Snapshotter` 在每轮对话结束时
    收口（``force_snapshot``），避免这种情况发生。
    """


class ResumeResult(BaseModel):
    """一次恢复的完整结果（:meth:`SessionResumer.resume` 的详细版）。

    Attributes:
        session_id (`str`): 会话 id。
        state (`AgentState`): 恢复出的状态（已绑定会话、已套用当前 Profile 的权限）。
        snapshot_seq (`int`): 恢复所用的快照锚点；``-1`` 表示空会话。
        tail_events (`int`): 快照之后仍需"读一遍"的尾部事件条数。
        interrupted (`bool`): 尾部是否有未收尾的 reply。
        awaiting_tool_calls (`list[dict[str, Any]]`): 仍在等外部回应的工具调用
            （``id`` / ``name`` / ``state``）。
        parked_on_confirm (`bool`): 是否 park 在用户确认上。
        profile_name (`str`): 本次恢复使用的 Profile 名。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_id: str
    """会话 id。"""

    state: Any
    """恢复出的 ``AgentState``。"""

    snapshot_seq: int = Field(default=-1)
    """快照锚点 ``seq``。"""

    tail_events: int = Field(default=0, ge=0)
    """尾部事件条数。"""

    interrupted: bool = False
    """尾部是否有未收尾的 reply。"""

    awaiting_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    """仍在等外部回应的工具调用。"""

    parked_on_confirm: bool = False
    """是否 park 在用户确认上。"""

    profile_name: str = ""
    """本次恢复使用的 Profile 名。"""

    @property
    def resume_hint(self) -> str:
        """给调用方的一句话提示：接下来该怎么继续。

        Returns:
            `str`: 人类可读的下一步动作。
        """
        if self.parked_on_confirm:
            return "会话 park 在用户确认上：回传 UserConfirmResultEvent 即可继续"
        if self.interrupted:
            return "会话停在半截 reply 上：先回传 interrupt_event() 收口，再发新输入"
        return "会话已干净收尾：直接 agent.reply(新输入) 即可"


class SessionResumer:
    """从最近的 :class:`~harness_kit.session.SessionSnapshot` + 尾部事件恢复 ``AgentState``。

    契约 §3.9 的签名：``__init__(self, store, snapshotter)`` /
    ``resume(session_id, *, profile)`` / ``list_resumable()``。

    Example:
        >>> resumer = SessionResumer(store, snapshotter)
        >>> state = await resumer.resume("s1", profile=resolved_profile)
        >>> agent = Agent(name=..., system_prompt=..., model=..., state=state)
    """

    def __init__(self, store: SessionStoreBase, snapshotter: Snapshotter) -> None:
        """构造恢复器。

        Args:
            store (`SessionStoreBase`): 事件与快照来源。
            snapshotter (`Snapshotter`): 快照器（恢复后收口时复用同一个，
                保证"每隔 N 条事件写一次快照"的计数口径一致）。
        """
        self.store: SessionStoreBase = store
        self.snapshotter: Snapshotter = snapshotter

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def resume(
        self,
        session_id: str,
        *,
        profile: "ResolvedProfile",
    ) -> "AgentState":
        """恢复出一个可直接喂给 ``Agent(state=...)`` 的 ``AgentState``（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。
            profile (`ResolvedProfile`): 当前生效的 Profile（用于重建权限策略、
                取 agent 名以判断"是否 park 在确认上"）。

        Returns:
            `AgentState`: 恢复出的状态。

        Raises:
            NoSnapshotError: 会话有事件但从未写过快照。
            ResumeError: 快照损坏、会话 id 不一致等结构性问题。
        """
        result = await self.resume_detailed(session_id, profile=profile)
        return result.state

    async def resume_detailed(
        self,
        session_id: str,
        *,
        profile: "ResolvedProfile",
    ) -> ResumeResult:
        """恢复并返回**完整诊断信息**（教程 / CLI / 排障用）。

        Args:
            session_id (`str`): 会话 id。
            profile (`ResolvedProfile`): 当前生效的 Profile。

        Returns:
            `ResumeResult`: 状态 + 锚点 + 尾部事件数 + 停放状态。

        Raises:
            NoSnapshotError: 有事件但没有快照。
            ResumeError: 快照与事件流的结构对不上。
        """
        events = await self.store.verify_invariants(session_id)
        snapshot = await self.store.latest_snapshot(session_id)

        if snapshot is None:
            if events:
                raise NoSnapshotError(
                    f"会话 {session_id} 有 {len(events)} 条事件但没有快照，"
                    "恢复不出权威状态（AgentState.context 只存在于快照里，"
                    "事件流里没有消息体）。请在本轮对话结束时调用 "
                    "Snapshotter.force_snapshot(agent.state) 收口。",
                )
            state = self._fresh_state(session_id)
            snapshot_seq = -1
        else:
            state = self._restore(session_id, snapshot)
            snapshot_seq = snapshot.seq

        tail = [event for event in events if event.seq > snapshot_seq]
        interrupted = self._inspect_tail(tail)

        self._apply_profile(session_id, state, profile=profile)
        awaiting_blocks = self._awaiting_blocks(state, profile=profile)

        result = ResumeResult(
            session_id=session_id,
            state=state,
            snapshot_seq=snapshot_seq,
            tail_events=len(tail),
            interrupted=interrupted,
            awaiting_tool_calls=awaiting_blocks,
            parked_on_confirm=any(
                item["state"] == "asking" for item in awaiting_blocks
            ),
            profile_name=profile.name,
        )
        logger.bind(
            session_id=session_id,
            snapshot_seq=snapshot_seq,
            tail_events=len(tail),
            interrupted=interrupted,
            parked=result.parked_on_confirm,
            profile=profile.name,
        ).info("会话已恢复：{}", result.resume_hint)
        return result

    async def list_resumable(self) -> list[SessionMeta]:
        """列出**真正可恢复**的会话（写过至少一个快照的）。

        为什么不是"有事件的都算"：没有快照就恢复不出上下文（见
        :class:`NoSnapshotError`），把它列出来只会让调用方白跑一趟。

        Returns:
            `list[SessionMeta]`: 按 ``updated_at`` 倒序的会话卡片。
        """
        metas = await self.store.list_sessions()
        resumable: list[SessionMeta] = []
        for meta in metas:
            if await self.snapshotter.last_snapshot_seq(meta.session_id) >= 0:
                resumable.append(meta)
        return resumable

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _fresh_state(self, session_id: str) -> "AgentState":
        """构造一个"全新会话"的状态。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `AgentState`: 绑定了 ``session_id`` 的空状态。
        """
        from agentscope.state import AgentState

        return AgentState(session_id=session_id)

    def _restore(self, session_id: str, snapshot: SessionSnapshot) -> "AgentState":
        """把快照还原成 ``AgentState`` 并做一致性校验。

        Args:
            session_id (`str`): 会话 id。
            snapshot (`SessionSnapshot`): 快照。

        Returns:
            `AgentState`: 还原出的状态。

        Raises:
            ResumeError: 快照里的 session_id 与请求的不一致。
        """
        state = restore_state(snapshot)
        if state.session_id != session_id:
            raise ResumeError(
                f"快照里的 session_id={state.session_id!r} 与请求的 {session_id!r} 不一致；"
                "快照文件可能被手工改过（事件流与快照必须同源）",
            )
        return state

    @staticmethod
    def _inspect_tail(tail: Sequence[SessionEvent]) -> bool:
        """看一眼尾部事件：这个 reply 收尾了吗？

        计数而不是"看最后一条"，是因为尾部可能既有收尾的 reply、又有下一条
        reply 的 ``REPLY_START``（进程正好死在两次 reply 之间）。

        Args:
            tail (`Sequence[SessionEvent]`): 快照之后的尾部事件（落盘形态，
                ``.kind`` 直接代理到 ``record.kind``）。

        Returns:
            `bool`: ``True`` 表示有未收尾的 reply。
        """
        started = 0
        ended = 0
        for event in tail:
            if event.kind is EventKind.REPLY_START:
                started += 1
            elif event.kind is EventKind.REPLY_END:
                ended += 1
        return started > ended

    @staticmethod
    def _awaiting_blocks(
        state: "AgentState",
        *,
        profile: "ResolvedProfile",
    ) -> list[dict[str, Any]]:
        """列出仍在等外部回应的工具调用（ASKING 的确认 / SUBMITTED 的外部执行）。

        直接复用 AgentScope 的方法
        （``third_party/agentscope/src/agentscope/state/_state.py:345``），
        不自己扫 ``context`` —— 状态机的判定条件（最后一条消息、role、name）
        由它负责，我们只消费结论。

        Args:
            state (`AgentState`): 恢复出的状态。
            profile (`ResolvedProfile`): 当前 Profile（取 agent 名）。

        Returns:
            `list[dict[str, Any]]`: 每项含 ``id`` / ``name`` / ``state``。
        """
        blocks = state.get_awaiting_tool_calls(profile.agent.name)
        return [
            {
                "id": block.id,
                "name": block.name,
                "state": str(block.state),
                "suggested_rules": [
                    {
                        "tool_name": rule.tool_name,
                        "rule_content": rule.rule_content,
                        "behavior": rule.behavior.value,
                    }
                    for rule in block.suggested_rules
                ],
            }
            for block in blocks
        ]

    def _apply_profile(
        self,
        session_id: str,
        state: "AgentState",
        *,
        profile: "ResolvedProfile",
    ) -> None:
        """把**当前 Profile** 的权限策略套到恢复出的状态上。

        快照里的 ``permission_context`` 是历史，不能复活（安全策略必须即时生效）。
        这里复用第 11 讲的 ``HarnessPermissionEngine.from_profile``，保证
        "装配期"与"恢复期"算出来的权限上下文**是同一段代码**，不会漂移。

        Args:
            session_id (`str`): 会话 id。
            state (`AgentState`): 待改写权限上下文的状态（就地修改）。
            profile (`ResolvedProfile`): 当前 Profile。
        """
        try:
            from harness_kit.permission.policy import HarnessPermissionEngine
        except ImportError:  # pragma: no cover - 第 11 讲未交付
            logger.bind(session_id=session_id).warning(
                "harness_kit.permission 不可用，恢复时保留快照里的权限上下文",
            )
            return

        previous = state.permission_context.mode.value
        try:
            engine = HarnessPermissionEngine.from_profile(profile.permission)
        except (FileNotFoundError, OSError) as exc:
            # 这里只重建 ``PermissionContext``，而 ``from_profile`` 会把
            # ``spec.rule_files`` 里的相对路径按**默认** repo_root 解析
            # （它拿不到本方法作用域里的 Settings，见 harness_kit/permission/policy.py
            # 的 from_profile 文档）。调用方若换过 repo_root，规则文件就可能定位不到。
            # 此时保留快照里的权限上下文并打 warning —— 恢复会话不该因为
            # "某个规则文件找不到"而整个失败，而且"权限更严一点"是安全的那一侧。
            logger.bind(session_id=session_id, error=str(exc)).warning(
                "恢复时重建权限引擎失败（{}），保留快照里的权限上下文",
                type(exc).__name__,
            )
            return
        state.permission_context = engine.context
        logger.bind(
            session_id=session_id,
            previous_mode=previous,
            mode=engine.context.mode.value,
        ).debug("恢复时已按当前 Profile 重建权限上下文")

    def interrupt_event(
        self,
        state: "AgentState",
        *,
        profile: "ResolvedProfile",
    ) -> "UserInterruptEvent | None":
        """为"半截 reply"生成 AgentScope 原生的 ``UserInterruptEvent``。

        返回 ``None`` 表示这个状态没有未完成的工具调用，不需要收口 ——
        **只有真的存在未完成调用时才生成事件**，否则把它喂给
        ``reply_stream`` 会被当成新输入处理。

        Args:
            state (`AgentState`): 恢复出的状态（需要知道 ``reply_id``）。
            profile (`ResolvedProfile`): 当前 Profile（取 agent 名）。

        Returns:
            `UserInterruptEvent | None`: 收口事件，或 ``None``。
        """
        from agentscope.event import UserInterruptEvent

        if not state.get_awaiting_tool_calls(profile.agent.name):
            return None
        return UserInterruptEvent(reply_id=state.reply_id)

    async def close_out(
        self,
        session_id: str,
        *,
        profile: "ResolvedProfile",
    ) -> SessionSnapshot:
        """收口：读回状态并强制写一个快照（"这轮聊完了，固定现场"）。

        Args:
            session_id (`str`): 会话 id。
            profile (`ResolvedProfile`): 当前 Profile。

        Returns:
            `SessionSnapshot`: 新写的快照。

        Raises:
            NoSnapshotError: 会话有事件但没有快照（此时应先恢复+收口，不能凭空造）。
        """
        state = await self.resume(session_id, profile=profile)
        return await self.snapshotter.force_snapshot(state)

    async def attach(
        self,
        agent: "Agent",
        session_id: str,
        *,
        profile: "ResolvedProfile",
    ) -> ResumeResult:
        """把一个已经构造好的 ``Agent`` 恢复到指定会话（就地覆盖其 ``state``）。

        ``Agent.state`` 是普通属性（``third_party/agentscope/src/agentscope/agent/_agent.py:174``），
        但 Agent 内部还有一个用 ``state.permission_context`` 构造出来的权限引擎
        （同文件 ``:193``）。因此这里**同时**重建引擎，避免"状态是新的、引擎还是旧的"
        这种只在生产上才发作的错位。

        Args:
            agent (`Agent`): 已构造的 Agent。
            session_id (`str`): 目标会话。
            profile (`ResolvedProfile`): 当前 Profile。

        Returns:
            `ResumeResult`: 恢复结果（``state`` 已装进 agent）。
        """
        from agentscope.permission import PermissionEngine

        result = await self.resume_detailed(session_id, profile=profile)
        agent.state = result.state
        agent._engine = PermissionEngine(agent.state.permission_context)
        logger.bind(session_id=session_id, agent=agent.name).info(
            "Agent 已挂接到会话：{}",
            result.resume_hint,
        )
        return result
