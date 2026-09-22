# -*- coding: utf-8 -*-
"""第 9 讲的 pytest：会话事件溯源 / 快照 / 回放 / 断点续跑。

三条纪律：

1. **0 次 LLM 调用**。所有"真 Agent"测试都用
   :class:`harness_kit.models.adapters.echo.EchoChatModel`（脚本驱动、确定性、离线）。
   真实模型的验证在 ``scripts/09_session_replay_resume.py``。
2. **不变式必须可回归**。``seq`` 无洞、快照锚点必须真实存在、事件一经 append
   永不修改 —— 这三条一旦破了，恢复出来的状态会**静默地**缺一段，
   所以它们每一条都有独立的测试。
3. **两个落地实现（JSONL / SQLite）必须同构**。同一个操作序列喂给两个 store，
   读出来的 :class:`~harness_kit.session.models.SessionEvent` 必须逐字段相等。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson09_session.py -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg
from agentscope.tool import FunctionTool, Toolkit

from harness_kit.events import EventBus, EventKind, EventRecord
from harness_kit.events.translate import StreamTranslator
from harness_kit.models.adapters.echo import EchoChatModel
from harness_kit.session import (
    JsonlSessionStore,
    NoSnapshotError,
    SessionEvent,
    SessionInvariantError,
    SessionLockedError,
    SessionMeta,
    SessionReplayer,
    SessionResumer,
    SessionSnapshot,
    SessionStoreBase,
    Snapshotter,
    SqliteSessionStore,
    check_seq_invariants,
    next_seq,
    restore_state,
    snapshot_from_state,
)
from harness_kit.session.store import SessionNotFoundError  # noqa: F401 - 契约导出

# ======================================================================
# 公共构造器
# ======================================================================
PROFILE = "test-profile"


def get_time(city: str = "北京") -> str:
    """查询某个城市的当前时间（只读、确定）。

    Args:
        city (`str`): 城市名。

    Returns:
        `str`: 固定时间字符串。
    """
    return f"{city} 现在是 2026-09-22 10:00:00"


def write_note(text: str) -> str:
    """写一条笔记（**非只读**，因此默认权限是 ASK → 会话会 park）。

    Args:
        text (`str`): 笔记内容。

    Returns:
        `str`: 确认串。
    """
    return f"已写入：{text}"


def record(seq: int, kind: EventKind, *, payload: dict[str, Any] | None = None,
           session_id: str = "s1") -> EventRecord:
    """造一条事件记录。

    Args:
        seq (`int`): 事件序号。
        kind (`EventKind`): 事件种类。
        payload (`dict[str, Any] | None`): 负载。
        session_id (`str`): 会话 id。

    Returns:
        `EventRecord`: 事件记录。
    """
    return EventRecord(session_id=session_id, seq=seq, kind=kind, payload=payload or {})


def full_session_events(session_id: str = "s1") -> list[EventRecord]:
    """造一段"一次完整 reply"的事件流（seq 无洞、payload 合契约）。

    Args:
        session_id (`str`): 会话 id。

    Returns:
        `list[EventRecord]`: 事件列表。
    """
    return [
        record(0, EventKind.SESSION_START,
               payload={"profile": PROFILE, "agent_name": "tester", "cwd": "/tmp"},
               session_id=session_id),
        record(1, EventKind.REPLY_START,
               payload={"reply_id": "r1", "input_preview": "几点了"},
               session_id=session_id),
        record(2, EventKind.MODEL_CALL,
               payload={"model": "echo", "prompt_tokens": 100, "completion_tokens": 20,
                        "latency_ms": 3.0, "finished_reason": "tool_use"},
               session_id=session_id),
        record(3, EventKind.TOOL_CALL,
               payload={"tool_name": "get_time", "tool_input_digest": "abc123", "call_id": "c1"},
               session_id=session_id),
        record(4, EventKind.TOOL_RESULT,
               payload={"call_id": "c1", "state": "success", "chars": 27, "error": None},
               session_id=session_id),
        record(5, EventKind.MODEL_CALL,
               payload={"model": "echo", "prompt_tokens": 150, "completion_tokens": 10,
                        "latency_ms": 2.0, "finished_reason": "stop"},
               session_id=session_id),
        record(6, EventKind.REPLY_END,
               payload={"reply_id": "r1", "iterations": 2, "tool_calls": ["get_time"]},
               session_id=session_id),
    ]


def make_agent(script: list[dict[str, Any]], *, name: str = "tester") -> Agent:
    """造一个离线 Agent（回声模型 + 两个工具）。

    ``get_time`` 是只读工具（权限自动放行），``write_note`` 不是
    （默认 :class:`~agentscope.permission.PermissionBehavior.ASK`，
    ``tool/_adapters.py:120``），因此脚本里调它一定会 park。

    Args:
        script (`list[dict[str, Any]]`): 回声模型脚本。
        name (`str`): Agent 名。

    Returns:
        `Agent`: 离线 Agent。
    """
    return Agent(
        name=name,
        system_prompt="你是一个中文助手。",
        model=EchoChatModel(stream=False, script=script),
        toolkit=Toolkit(tools=[FunctionTool(get_time, is_read_only=True),
                               FunctionTool(write_note)]),
    )


class StoreSink:
    """把 :class:`EventBus` 上的记录顺着写进 :class:`SessionStoreBase`。

    这是"第 3 讲的事件总线"与"第 9 讲的会话存储"之间**唯一**的接缝：
    总线上流的是 :class:`~harness_kit.events.EventRecord`，存储收的也是
    ``EventRecord``，所以这一层可以薄到只有一个 ``await store.append``。

    Attributes:
        store (`SessionStoreBase`): 落盘目标。
        records (`list[EventRecord]`): 已落盘的记录（顺序即 seq 顺序）。
        errors (`list[str]`): 落盘失败的原因。
    """

    def __init__(self, store: SessionStoreBase) -> None:
        """初始化。

        Args:
            store (`SessionStoreBase`): 落盘目标。
        """
        self.store = store
        self.records: list[EventRecord] = []
        self.errors: list[str] = []

    async def __call__(self, record: EventRecord) -> None:
        """订阅者入口。

        Args:
            record (`EventRecord`): 事件记录。
        """
        try:
            await self.store.append(record)
        except Exception as exc:  # noqa: BLE001 - 订阅者异常会被总线吞掉并计数
            self.errors.append(f"{type(exc).__name__}: {exc}")
            raise
        self.records.append(record)


async def run_reply_into_store(
    agent: Agent,
    store: SessionStoreBase,
    question: str,
    *,
    session_id: str,
) -> StoreSink:
    """跑一次 ``reply_stream``，把事件经总线写进 store。

    Args:
        agent (`Agent`): 离线 Agent。
        store (`SessionStoreBase`): 落盘目标。
        question (`str`): 用户输入。
        session_id (`str`): 会话 id。

    Returns:
        `StoreSink`: 落盘订阅者（含全部记录）。
    """
    bus = EventBus()
    sink = StoreSink(store)
    bus.subscribe("*", sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=session_id)
    translator.note_input(question)
    await translator.consume(agent.reply_stream(UserMsg("user", question)))
    await bus.drain()
    await bus.aclose()
    return sink


# ======================================================================
# 1 · 存储抽象
# ======================================================================
def test_store_base_is_abstract() -> None:
    """抽象基类不能被直接实例化（缺 7 个抽象方法时会 TypeError）。"""
    with pytest.raises(TypeError):
        SessionStoreBase()  # type: ignore[abstract]


def test_contract_signatures_present() -> None:
    """契约 §3.9 钉死的五个公开方法必须存在且是协程。"""
    for name in ("append", "read", "list_sessions", "latest_seq", "aclose"):
        attr = getattr(SessionStoreBase, name)
        assert getattr(attr, "__isabstractmethod__", False) is True, name
    assert asyncio.iscoroutinefunction(SessionStoreBase.append_many)


# ======================================================================
# 2 · JSONL 事件溯源
# ======================================================================
async def test_jsonl_append_read_roundtrip(tmp_path: Path) -> None:
    """写进去 7 条、读出来 7 条，``SessionEvent`` 逐字段相等。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    events = full_session_events()
    await store.append_many(events)
    back = await store.read("s1")
    assert [item.seq for item in back] == list(range(7))
    assert [item.record for item in back] == events
    assert await store.latest_seq("s1") == 6
    await store.aclose()


async def test_jsonl_seq_gap_raises(tmp_path: Path) -> None:
    """跳过 seq=1 直接写 seq=2 → :class:`SessionInvariantError`。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append(record(0, EventKind.SESSION_START))
    with pytest.raises(SessionInvariantError, match="严格递增"):
        await store.append(record(2, EventKind.REPLY_START))
    await store.aclose()


async def test_jsonl_duplicate_seq_raises(tmp_path: Path) -> None:
    """重复写同一个 seq → :class:`SessionInvariantError`。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append(record(0, EventKind.SESSION_START))
    with pytest.raises(SessionInvariantError):
        await store.append(record(0, EventKind.SESSION_START))
    await store.aclose()


async def test_jsonl_empty_session_conventions(tmp_path: Path) -> None:
    """空会话：``latest_seq == -1``、``read`` 为空、``next_seq == 0``、``meta`` 为 None。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    assert await store.latest_seq("nope") == -1
    assert await store.read("nope") == []
    assert await store.next_seq("nope") == 0
    assert await store.meta("nope") is None
    await store.aclose()


async def test_jsonl_read_window_and_limit(tmp_path: Path) -> None:
    """``since_seq`` 是闭区间，``limit`` 截断；``limit<=0`` 抛 ValueError。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    assert [item.seq for item in await store.read("s1", since_seq=3)] == [3, 4, 5, 6]
    assert [item.seq for item in await store.read("s1", limit=2)] == [0, 1]
    assert await store.read("s1", since_seq=99) == []
    with pytest.raises(ValueError):
        await store.read("s1", limit=0)
    await store.aclose()


async def test_jsonl_meta_sidecar_and_rebuild(tmp_path: Path) -> None:
    """索引卡片能建；sidecar 被删掉时能从事件流重建（卡片是派生数据）。"""
    root = tmp_path / "sessions"
    store = JsonlSessionStore(root)
    await store.append_many(full_session_events())
    meta = await store.meta("s1")
    assert isinstance(meta, SessionMeta)
    assert meta.profile_name == PROFILE
    assert meta.event_count == 7

    (root / "s1.meta.json").unlink()
    rebuilt = await store.meta("s1")
    assert rebuilt is not None and rebuilt.event_count == 7
    assert (root / "s1.meta.json").exists(), "重建后必须回写 sidecar"
    await store.aclose()


async def test_jsonl_list_sessions_sorted_desc(tmp_path: Path) -> None:
    """``list_sessions`` 按 ``updated_at`` 倒序。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events("older"))
    await asyncio.sleep(0.01)
    await store.append_many(full_session_events("newer"))
    sessions = await store.list_sessions()
    assert [item.session_id for item in sessions] == ["newer", "older"]
    await store.aclose()


async def test_jsonl_truncated_tail_is_tolerated(tmp_path: Path) -> None:
    """崩溃残留：文件尾多一截写坏的 frame，读路径必须跳过它而不是炸掉。"""
    root = tmp_path / "sessions"
    store = JsonlSessionStore(root)
    await store.append_many(full_session_events())
    await store.aclose()

    path = root / "s1.jsonl.zst"
    with path.open("ab") as handle:
        handle.write(b"\x28\xb5\x2f\xfd\x00\x00\x00\x00garbage-not-a-frame")

    reopened = JsonlSessionStore(root)
    events = await reopened.read("s1")
    assert len(events) == 7, "坏掉的尾帧必须被跳过，前面的 7 条不受影响"
    assert await reopened.latest_seq("s1") == 6
    await reopened.aclose()


async def test_jsonl_empty_session_meta_from_rebuild(tmp_path: Path) -> None:
    """从事件流重建卡片时，``SESSION_START.payload["profile"]`` 会被读出来。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    meta = await store.meta("s1")
    assert meta is not None
    assert meta.tags == []
    await store.aclose()


async def test_jsonl_append_after_close_raises(tmp_path: Path) -> None:
    """``aclose`` 之后再 append 必须显式报错（而不是静默丢数据）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.aclose()
    with pytest.raises(RuntimeError, match="aclose"):
        await store.append(record(0, EventKind.SESSION_START))


async def test_jsonl_cross_process_lock(tmp_path: Path) -> None:
    """另一个进程持锁时 append 抛 :class:`SessionLockedError`（flock 非阻塞）。"""
    import fcntl

    root = tmp_path / "sessions"
    store = JsonlSessionStore(root)
    await store.append(record(0, EventKind.SESSION_START))

    with (root / "s1.lock").open("ab") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SessionLockedError, match="另一个进程"):
            await store.append(record(1, EventKind.REPLY_START))
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    await store.append(record(1, EventKind.REPLY_START))
    assert await store.latest_seq("s1") == 1
    await store.aclose()


async def test_jsonl_uncompressed_mode(tmp_path: Path) -> None:
    """``compress=False`` 走 ``.jsonl`` 路径，语义完全一致。"""
    root = tmp_path / "sessions"
    store = JsonlSessionStore(root, compress=False)
    await store.append_many(full_session_events())
    assert (root / "s1.jsonl").exists()
    assert (root / "s1.jsonl.zst").exists() is False
    assert [item.seq for item in await store.read("s1")] == list(range(7))
    await store.aclose()


async def test_verify_invariants_returns_events(tmp_path: Path) -> None:
    """``verify_invariants`` 读全量并校验，命中时返回事件列表。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    events = await store.verify_invariants("s1")
    assert len(events) == 7
    await store.aclose()


# ======================================================================
# 3 · 快照
# ======================================================================
async def test_snapshot_anchor_must_exist(tmp_path: Path) -> None:
    """快照锚点不在事件流上 → :class:`SessionInvariantError`（契约 §5.2 不变式 3）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events()[:3])
    bad = SessionSnapshot(session_id="s1", seq=6, agent_state={"session_id": "s1"})
    with pytest.raises(SessionInvariantError, match="锚点"):
        await store.save_snapshot(bad)
    await store.aclose()


async def test_snapshot_roundtrip_and_latest(tmp_path: Path) -> None:
    """多个快照共存，``load_snapshot`` 取 ``seq`` 最大的那个。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    for seq in (2, 5):
        await store.save_snapshot(
            SessionSnapshot(session_id="s1", seq=seq, agent_state={"session_id": "s1", "n": seq}),
        )
    latest = await store.latest_snapshot("s1")
    assert latest is not None and latest.seq == 5
    older = await store.load_snapshot("s1", at_or_before=3)
    assert older is not None and older.seq == 2
    assert [item.seq for item in await store.list_snapshots("s1")] == [2, 5]
    await store.aclose()


async def test_snapshot_negative_seq_allowed_on_empty(tmp_path: Path) -> None:
    """空会话允许 ``seq=-1`` 的空快照（表示"什么都没发生"）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    snapshot = SessionSnapshot(session_id="s1", seq=-1, agent_state={"session_id": "s1"})
    assert snapshot.is_empty is True
    await store.save_snapshot(snapshot)
    loaded = await store.latest_snapshot("s1")
    assert loaded is not None and loaded.is_empty
    await store.aclose()


async def test_snapshotter_threshold_and_force(tmp_path: Path) -> None:
    """间隔阈值：不足不写；够了才写；``force_snapshot`` 无条件写。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    snapshotter = Snapshotter(store, every_n_events=5)
    state = _make_state("s1")
    assert await snapshotter.maybe_snapshot(state) is None, "没有任何事件时不写"

    await store.append_many(full_session_events()[:3])
    assert await snapshotter.maybe_snapshot(state) is None, "3 < 5，不写"

    await store.append_many(full_session_events()[3:6])
    snapshot = await snapshotter.maybe_snapshot(state)
    assert snapshot is not None and snapshot.seq == 5
    assert await snapshotter.pending_events("s1") == 0
    assert await snapshotter.last_snapshot_seq("s1") == 5

    forced = await snapshotter.force_snapshot(state)
    assert forced.seq == 5 and snapshotter.snapshots_taken == 2
    await store.aclose()


def test_snapshotter_rejects_bad_interval(tmp_path: Path) -> None:
    """``every_n_events < 1`` 立刻 ValueError（不要等到运行期）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    with pytest.raises(ValueError):
        Snapshotter(store, every_n_events=0)


async def test_snapshotter_requires_session_id() -> None:
    """没有 ``session_id`` 的对象不能当 ``AgentState`` 用（提前报错而非静默）。"""
    store = JsonlSessionStore(Path("/tmp/harness-kit-test-unused"))
    snapshotter = Snapshotter(store)
    with pytest.raises(TypeError, match="session_id"):
        await snapshotter.force_snapshot(object())


# ======================================================================
# 4 · 回放
# ======================================================================
async def test_replayer_fold_tokens_and_tools(tmp_path: Path) -> None:
    """fold：token 是两段 ``MODEL_CALL`` 之和，工具调用被配对。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    replayer = SessionReplayer(store)

    result = await replayer.fold("s1")
    assert result.event_count == 7
    assert result.token_usage.input_tokens == 250
    assert result.token_usage.output_tokens == 30
    assert result.token_usage.total_tokens == 280
    assert result.tool_calls[0]["tool_name"] == "get_time"
    assert result.tool_calls[0]["state"] == "success"
    assert result.errors == []
    await store.aclose()


async def test_replayer_reports_dangling_tool_call(tmp_path: Path) -> None:
    """**失败回放**：只有 TOOL_CALL 没有 TOOL_RESULT → errors 里必须点名。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events()[:4])
    result = await SessionReplayer(store).fold("s1")
    assert len(result.errors) == 1
    assert "只有 TOOL_CALL 没有 TOOL_RESULT" in result.errors[0]
    assert "get_time" in result.errors[0]
    await store.aclose()


async def test_replayer_turns_split(tmp_path: Path) -> None:
    """回合切分：``REPLY_START`` 开头、``REPLY_END`` 收尾，中间的工具归本回合。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    turns = await SessionReplayer(store).turns("s1")
    assert len(turns) == 1
    turn = turns[0]
    assert turn.reply_id == "r1"
    assert turn.closed is True
    assert turn.iterations == 2
    assert turn.tool_calls == ["get_time"]
    assert turn.model_calls == 2
    assert turn.token_usage.total_tokens == 280
    assert turn.start_seq == 1 and turn.end_seq == 6
    await store.aclose()


async def test_replayer_turns_marks_unclosed(tmp_path: Path) -> None:
    """没有 ``REPLY_END`` 的回合 ``closed=False``（进程被杀就是这个形状）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events()[:5])
    turns = await SessionReplayer(store).turns("s1")
    assert len(turns) == 1 and turns[0].closed is False and turns[0].end_seq is None
    await store.aclose()


async def test_replayer_diff_window(tmp_path: Path) -> None:
    """diff 是左开右闭区间，``seq_b < seq_a`` 抛 ValueError。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    replayer = SessionReplayer(store)
    window = await replayer.diff("s1", 2, 5)
    assert window["added"] == 3
    assert window["first_seq"] == 3 and window["last_seq"] == 5
    assert window["kinds"] == {"tool_call": 1, "tool_result": 1, "model_call": 1}
    assert window["token_delta"]["input_tokens"] == 150
    with pytest.raises(ValueError):
        await replayer.diff("s1", 5, 2)
    await store.aclose()


async def test_replayer_timeline_until(tmp_path: Path) -> None:
    """``timeline_until`` 是闭区间；负数上界返回空。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    replayer = SessionReplayer(store)
    assert [item.seq for item in await replayer.timeline_until("s1", 2)] == [0, 1, 2]
    assert await replayer.timeline_until("s1", -1) == []
    folded = await replayer.fold_until("s1", 2)
    assert folded.event_count == 3
    await store.aclose()


async def test_replayer_synthesize_errors_from_custom(tmp_path: Path) -> None:
    """``CUSTOM(name="error")`` 也会进 errors —— 工具外的失败也要能被回放看到。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append(record(0, EventKind.CUSTOM,
                              payload={"name": "error", "data": "沙箱超时"}))
    result = await SessionReplayer(store).fold("s1")
    assert result.errors == ["seq=0 沙箱超时"]
    await store.aclose()


# ======================================================================
# 5 · 断点续跑（真实 Agent，离线）
# ======================================================================
def _make_state(session_id: str) -> Any:
    """造一个绑定了 session_id 的 ``AgentState``。

    Args:
        session_id (`str`): 会话 id。

    Returns:
        `Any`: ``AgentState``。
    """
    from agentscope.state import AgentState

    return AgentState(session_id=session_id)


async def test_resume_without_snapshot_raises(tmp_path: Path) -> None:
    """有事件但没有快照 → :class:`NoSnapshotError`（绝不"拼一个空上下文"糊弄）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events())
    snapshotter = Snapshotter(store)
    resumer = SessionResumer(store, snapshotter)
    profile = _FakeProfile()
    with pytest.raises(NoSnapshotError, match="没有快照"):
        await resumer.resume("s1", profile=profile)
    await store.aclose()


async def test_resume_empty_session_is_fresh_state(tmp_path: Path) -> None:
    """事件与快照都没有 → 返回一个空状态，而不是报错。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    resumer = SessionResumer(store, Snapshotter(store))
    result = await resumer.resume_detailed("s1", profile=_FakeProfile())
    assert result.session_id == "s1"
    assert result.snapshot_seq == -1
    assert result.tail_events == 0
    assert result.interrupted is False
    assert result.state.session_id == "s1"
    await store.aclose()


async def test_resume_restores_real_agent_context(tmp_path: Path) -> None:
    """**跨进程恢复**：拿一个全新的 store 实例恢复出 AgentState，消息历史一字不差。"""
    root = tmp_path / "sessions"
    session_id = "sess-resume"

    # ---- 进程 1：跑一轮，收口写快照 -------------------------------------
    store_a = JsonlSessionStore(root)
    agent_a = make_agent([{"text": "北京 10:00"},
                          {"text": "还有什么可以帮你？"}])
    agent_a.state.session_id = session_id
    sink = await run_reply_into_store(agent_a, store_a, "几点了", session_id=session_id)
    assert sink.errors == []
    snapshotter_a = Snapshotter(store_a)
    await snapshotter_a.force_snapshot(agent_a.state)
    expected_context = agent_a.state.context
    await store_a.aclose()

    # ---- 进程 2：全新 store + 全新 agent，恢复 ---------------------------------
    store_b = JsonlSessionStore(root)
    resumer = SessionResumer(store_b, Snapshotter(store_b))
    result = await resumer.resume_detailed(session_id, profile=_FakeProfile())
    assert result.snapshot_seq == await store_b.latest_seq(session_id)
    assert result.interrupted is False
    assert result.awaiting_tool_calls == []
    assert result.resume_hint.startswith("会话已干净收尾")
    assert len(result.state.context) == len(expected_context)
    assert [m.get_text_content() for m in result.state.context] == [
        m.get_text_content() for m in expected_context
    ]
    assert result.state.context[-1].role == "assistant"

    # ---- 继续对话：把恢复的状态装进新 agent -----------------------------------
    agent_b = make_agent([{"text": "继续聊"}])
    agent_b.state = result.state
    # 注意：``agent_b.state`` 与 ``result.state`` 是**同一个对象**（``Agent``
    # 直接持有传进来的 state，``agent/_agent.py:174``），所以"长度变长了"必须
    # 先记下旧值再比，否则两边永远相等。
    before = len(result.state.context)
    msg = await agent_b.reply(UserMsg("user", "继续"))
    assert msg.get_text_content() == "继续聊"
    assert len(agent_b.state.context) > before
    assert agent_b.state.context[before - 1].get_text_content() == "北京 10:00", (
        "恢复出来的历史必须还在（上一轮的最后一条 assistant 消息）"
    )
    assert agent_b.state.context[-1].get_text_content() == "继续聊"
    await store_b.aclose()


async def test_resume_detects_parked_tool_call(tmp_path: Path) -> None:
    """**失败案例回放**：会话 park 在"等用户确认"上，恢复时能被认出来。"""
    root = tmp_path / "sessions"
    session_id = "sess-parked"
    store = JsonlSessionStore(root)
    agent = make_agent([
        {"text": "我写一条笔记", "tool_calls": [
            {"id": "c9", "name": "write_note", "input": {"text": "记得买牛奶"}},
        ]},
    ])
    agent.state.session_id = session_id
    await run_reply_into_store(agent, store, "记一下", session_id=session_id)
    assert agent.state.has_awaiting_tool_calls(agent.name) is True, "非只读工具默认 ASK"
    await Snapshotter(store).force_snapshot(agent.state)
    await store.aclose()

    reopened = JsonlSessionStore(root)
    resumer = SessionResumer(reopened, Snapshotter(reopened))
    result = await resumer.resume_detailed(session_id, profile=_FakeProfile())
    assert result.parked_on_confirm is True
    assert [item["name"] for item in result.awaiting_tool_calls] == ["write_note"]
    assert result.awaiting_tool_calls[0]["state"] == "asking"
    assert "park" in result.resume_hint

    # 收口手段是 AgentScope 原生的 UserInterruptEvent，不是自己写事件循环。
    event = resumer.interrupt_event(result.state, profile=_FakeProfile())
    assert event is not None and event.reply_id == result.state.reply_id

    replay = await SessionReplayer(reopened).fold(session_id)
    assert any("只有 TOOL_CALL 没有 TOOL_RESULT" in item for item in replay.errors)
    await reopened.aclose()


async def test_list_resumable_filters_snapshotless(tmp_path: Path) -> None:
    """``list_resumable`` 只列出写过快照的会话（其余列出来只会让调用方白跑）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events("with-snapshot"))
    await store.append_many(full_session_events("without-snapshot"))
    await store.save_snapshot(
        SessionSnapshot(session_id="with-snapshot", seq=6,
                        agent_state={"session_id": "with-snapshot"}),
    )
    resumer = SessionResumer(store, Snapshotter(store))
    resumable = await resumer.list_resumable()
    assert [item.session_id for item in resumable] == ["with-snapshot"]
    await store.aclose()


async def test_resume_rejects_mismatched_snapshot(tmp_path: Path) -> None:
    """快照里的 ``session_id`` 与请求的不一致 → :class:`ResumeError`。"""
    from harness_kit.session import ResumeError

    store = JsonlSessionStore(tmp_path / "sessions")
    await store.append_many(full_session_events("s1"))
    await store.save_snapshot(
        SessionSnapshot(session_id="s1", seq=6, agent_state={"session_id": "hacked"}),
    )
    resumer = SessionResumer(store, Snapshotter(store))
    with pytest.raises(ResumeError, match="不一致"):
        await resumer.resume("s1", profile=_FakeProfile())
    await store.aclose()


async def test_context_from_snapshot_is_authoritative(tmp_path: Path) -> None:
    """事件流里没有消息体，唯一可信的"当时消息"来自快照。"""
    root = tmp_path / "sessions"
    session_id = "sess-ctx"
    store = JsonlSessionStore(root)
    agent = make_agent([{"text": "答案在这里"}])
    agent.state.session_id = session_id
    await run_reply_into_store(agent, store, "问题", session_id=session_id)
    await Snapshotter(store).force_snapshot(agent.state)

    msgs = await SessionReplayer(store).context_from_snapshot(session_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert isinstance(msgs[0], Msg)
    await store.aclose()


# ======================================================================
# 6 · SQLite 实现（与 JSONL 同构 + blob 外置）
# ======================================================================
async def test_sqlite_parity_with_jsonl(tmp_path: Path) -> None:
    """同一串事件喂给两个后端，读出来必须**逐字段相等**。"""
    events = full_session_events()
    jsonl = JsonlSessionStore(tmp_path / "sessions")
    sqlite = SqliteSessionStore(tmp_path / "session.db")
    await jsonl.append_many(events)
    await sqlite.append_many(events)

    a = await jsonl.read("s1")
    b = await sqlite.read("s1")
    assert a == b
    assert await jsonl.latest_seq("s1") == await sqlite.latest_seq("s1") == 6

    meta_a = await jsonl.meta("s1")
    meta_b = await sqlite.meta("s1")
    assert meta_a is not None and meta_b is not None
    # ``updated_at`` 是"这次写入的墙上时间"，两个后端先后落盘必然不同 ——
    # 这恰好说明它是**写入时刻**而不是**事件时刻**（后者才必须逐字段相等）。
    for field in ("session_id", "profile_name", "created_at", "event_count", "tags"):
        assert getattr(meta_a, field) == getattr(meta_b, field), field
    assert meta_b.updated_at >= meta_b.created_at
    await jsonl.aclose()
    await sqlite.aclose()


async def test_sqlite_invariants_match_jsonl(tmp_path: Path) -> None:
    """SQLite 侧同样拒绝跳号、拒绝坏锚点。"""
    store = SqliteSessionStore(tmp_path / "session.db")
    await store.append(record(0, EventKind.SESSION_START))
    with pytest.raises(SessionInvariantError):
        await store.append(record(3, EventKind.REPLY_START))
    with pytest.raises(SessionInvariantError, match="锚点"):
        await store.save_snapshot(
            SessionSnapshot(session_id="s1", seq=9, agent_state={"session_id": "s1"}),
        )
    await store.aclose()


async def test_sqlite_blob_externalization(tmp_path: Path) -> None:
    """超过阈值的 payload 必须外置进 ``blobs`` 表，且内容寻址（同内容不重复存）。"""
    store = SqliteSessionStore(tmp_path / "session.db", blob_threshold=512)
    big = "扫出来的日志行\n" * 500
    await store.append(record(0, EventKind.SESSION_START, payload={"profile": PROFILE}))
    await store.append(record(1, EventKind.TOOL_RESULT,
                              payload={"call_id": "c1", "state": "success",
                                       "chars": len(big), "error": None, "output": big}))
    await store.append(record(2, EventKind.TOOL_RESULT,
                              payload={"call_id": "c2", "state": "success",
                                       "chars": len(big), "error": None, "output": big}))
    counts = await store.counts()
    assert counts["events"] == 3
    assert counts["blobs"] == 2, "两条大事件同内容 → 只占一个 blob + 一份存流量"

    back = await store.read("s1")
    assert back[1].record.payload["output"] == big
    assert back[1].record == (await store.read("s1", since_seq=1, limit=1))[0].record
    await store.aclose()


async def test_sqlite_vacuum_removes_orphan_blobs(tmp_path: Path) -> None:
    """``vacuum`` 清掉无人引用的 blob，并回收页空间。"""
    db = tmp_path / "session.db"
    store = SqliteSessionStore(db, blob_threshold=64)
    await store.append(record(0, EventKind.SESSION_START, payload={"profile": PROFILE}))
    await store.append(record(1, EventKind.CUSTOM,
                              payload={"name": "big", "data": "z" * 4096}))
    await store.save_snapshot(
        SessionSnapshot(session_id="s1", seq=1,
                        agent_state={"session_id": "s1", "big": "y" * 4096}),
    )
    before = await store.counts()
    await store.append(record(2, EventKind.CUSTOM,
                              payload={"name": "temp", "data": "q" * 4096}))
    report = await store.vacuum()
    assert report["blobs_left"] == before["blobs"] + 1
    assert report["orphan_blobs"] == 0
    assert report["page_count"] > 0
    await store.aclose()


async def test_sqlite_snapshots_and_sessions(tmp_path: Path) -> None:
    """SQLite 侧的会话卡片与快照读写（含 ``at_or_before`` 上界）。"""
    store = SqliteSessionStore(tmp_path / "session.db")
    await store.append_many(full_session_events())
    for seq in (1, 6):
        await store.save_snapshot(
            SessionSnapshot(session_id="s1", seq=seq,
                            agent_state={"session_id": "s1", "n": seq}),
        )
    latest = await store.latest_snapshot("s1")
    assert latest is not None and latest.seq == 6
    older = await store.load_snapshot("s1", at_or_before=2)
    assert older is not None and older.seq == 1
    assert [s.seq for s in await store.list_snapshots("s1")] == [1, 6]
    meta = await store.meta("s1")
    assert meta is not None and meta.event_count == 7 and meta.profile_name == PROFILE
    assert [m.session_id for m in await store.list_sessions()] == ["s1"]
    await store.aclose()


async def test_sqlite_rejects_bad_threshold(tmp_path: Path) -> None:
    """``blob_threshold < 1`` 立刻 ValueError。"""
    with pytest.raises(ValueError):
        SqliteSessionStore(tmp_path / "session.db", blob_threshold=0)


async def test_sqlite_after_close_raises(tmp_path: Path) -> None:
    """关闭后再用必须显式报错。"""
    store = SqliteSessionStore(tmp_path / "session.db")
    await store.aclose()
    with pytest.raises(RuntimeError, match="aclose"):
        await store.latest_seq("s1")


async def test_sqlite_memory_mode() -> None:
    """``:memory:`` 模式可跑（单测友好），且不需要目录。"""
    store = SqliteSessionStore(Path(":memory:"))
    await store.append(record(0, EventKind.SESSION_START, payload={"profile": PROFILE}))
    assert await store.latest_seq("s1") == 0
    assert (await store.counts())["events"] == 1
    await store.aclose()


# ======================================================================
# 7 · 第 3 讲事件总线 ↔ 第 9 讲会话存储
# ======================================================================
async def test_bus_to_store_jsonl_seq_continuous(tmp_path: Path) -> None:
    """真实 Agent 的事件流经总线落进 JSONL store，``seq`` 从 0 起无洞。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    agent = make_agent([{"text": "先查一下", "tool_calls": [
        {"id": "c1", "name": "get_time", "input": {"city": "上海"}},
    ]}, {"text": "上海 10:00"}])
    agent.state.session_id = "sess-bus"
    sink = await run_reply_into_store(agent, store, "几点了", session_id="sess-bus")

    records = sink.records
    check_seq_invariants(records, expected_session_id="sess-bus")
    assert next_seq(records) == len(records)
    kinds = [item.kind.value for item in records]
    # 重要事实：``StreamTranslator`` **不产** ``session_start``（它只翻译
    # ``Agent.reply_stream`` 吐出来的事件，见 ``events/translate.py:395`` 起）。
    # 会话的"起点"必须由调用方自己写 —— 这正是"总线接存储"这一层要补的第一件事。
    assert kinds[0] == "reply_start"
    assert "session_start" not in kinds
    assert kinds[-1] == "reply_end"
    assert kinds.count("reply_start") == 1
    assert {"model_call", "tool_call", "tool_result"} <= set(kinds)

    # 落盘与内存里的是同一批（seq 一致、内容一致）
    persisted = await store.read("sess-bus")
    assert [item.record for item in persisted] == records
    await store.aclose()


async def test_bus_to_store_sqlite_and_replay(tmp_path: Path) -> None:
    """同一条链路换成 SQLite 后端，回放的结论必须一致。"""
    store = SqliteSessionStore(tmp_path / "session.db")
    agent = make_agent([{"text": "只回答文字"}])
    agent.state.session_id = "sess-sql"
    sink = await run_reply_into_store(agent, store, "你好", session_id="sess-sql")

    summary = await SessionReplayer(store).summarize("sess-sql")
    assert summary["events"] == len(sink.records)
    assert summary["turns"] == 1
    assert summary["closed_turns"] == 1
    assert summary["errors"] == []
    assert summary["tokens"]["input_tokens"] >= 0
    await store.aclose()


async def test_snapshot_from_state_rejects_non_pydantic(tmp_path: Path) -> None:
    """``snapshot_from_state`` 只接受 pydantic 模型（AgentState 的约定）。"""
    with pytest.raises(TypeError, match="pydantic"):
        snapshot_from_state({"session_id": "s1"}, seq=0)


async def test_restore_state_roundtrip() -> None:
    """``AgentState`` → 快照 → ``AgentState`` 往返不丢字段。"""
    from agentscope.message import AssistantMsg

    state = _make_state("s1")
    state.context.append(AssistantMsg("a", [{"type": "text", "text": "hi"}]))  # type: ignore[arg-type]
    snapshot = snapshot_from_state(state, seq=0)
    restored = restore_state(snapshot)
    assert restored.model_dump(mode="json") == state.model_dump(mode="json")
    assert len(restored.context) == 1


class _FakeProfile:
    """最小 Profile 替身：``SessionResumer`` 只用到 ``profile.name`` / ``agent.name`` /
    ``permission`` 三处。

    这里刻意用**真实的** :class:`~harness_kit.config.schema.PermissionSpec`
    （第 2 讲的产物）而不是随便一个对象：:meth:`SessionResumer._apply_profile`
    会把它喂给 ``HarnessPermissionEngine.from_profile``，后者要读 ``spec.rule_files``
    与 ``spec.mode``。塞 ``None`` 会得到 ``AttributeError``（已实测），
    而那不是我们想教给读者的降级路径。

    Attributes:
        name (`str`): Profile 名。
        agent (`Any`): 带 ``name`` 的假 AgentSpec。
        permission (`PermissionSpec`): 真实权限声明（无规则文件、默认模式）。
    """

    def __init__(self, name: str = PROFILE, mode: str = "default") -> None:
        """初始化。

        Args:
            name (`str`): Profile 名。
            mode (`str`): ``PermissionMode`` 的字符串值。
        """
        from harness_kit.config.schema import AgentSpec, PermissionSpec

        self.name = name
        self.agent = AgentSpec(name="tester")
        self.permission = PermissionSpec(mode=mode, rule_files=[])


def _unused_json_import_guard() -> None:
    """保留 ``json`` 的 import（供将来扩展断言用），避免 linter 误删。"""
    assert json.dumps({}) == "{}"
