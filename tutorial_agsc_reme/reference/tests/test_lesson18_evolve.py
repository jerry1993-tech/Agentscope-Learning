# -*- coding: utf-8 -*-
"""第 18 讲的 pytest：自演化四机制与维护调度。

六条纪律（延续第 15/16/17 讲，本讲的重点是"**触发时机**与**如实汇报**"）：

1. **0 次 LLM 调用**。维护调度的语义（什么时候调、调不调、失败了算什么）
   与模型无关：用一个记录调用的假 maintainer 就能全部钉住。
   ``auto_memory`` / ``auto_dream`` 的真实调用在
   ``scripts/18_evolve.py --live``（实测 2 次补全）。
2. **纯逻辑部分连 ReMe 都不启动**。``MemoryMaintenanceScheduler`` 的
   事件语义、``ForgetPolicy.normalized_protected``、``ForgetPlan.summary``、
   ``SessionDistiller.shape_messages``、``parse_structured_reply`` 都是纯函数，
   跑在毫秒级测试里；只有"真实 reindex / 标签 / 删除"那些要走
   ``await client.start()``。
3. **每个测试自己建工作区，绝不共享**。ReMe 的 ``Application._start()`` 会建
   ``asyncio.Lock``，而 ``asyncio_default_fixture_loop_scope = "function"``
   意味着**每个测试一个事件循环**：跨测试复用一个 client 就是跨事件循环复用锁。
4. **回归测试钉住已经踩过的坑**：
   ``test_event_kind_has_no_session_end`` 对应"不要新造 EventKind"；
   ``test_apply_deletes_then_demotes_by_hit_count`` 对应"hits 决定删还是降权"；
   ``test_nightly_never_raises_when_dream_fails`` 对应"例行维护不许炸整条链"；
   ``test_parse_structured_reply_swallows_bad_yaml`` 对应
   "auto_dream 的提取会对模型输出格式静默失手"。
5. **枚举与常量对齐要断言到值**：``SESSION_END_EVENT_NAME`` 必须是
   ``"session_end"``、``RESIDENT_BACKENDS`` 必须是 ``{background, cron}``、
   ``DEFAULT_JOB_TIMEOUT_S`` 必须是 ``60.0`` —— 这些是契约 §3.18 写死的。
6. **失败路径一条都不能少**：空 ``session_id``、没有 ``msgs_of``、
   ``min_messages`` 拦截、``timeout_s=0``、常驻 job、未知 job、
   负的 ``max_age_days``、越界的 ``min_confidence``。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson18_evolve.py -v
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentscope.message import AssistantMsg, UserMsg

from harness_kit.events.bus import EventBus
from harness_kit.events.types import EventKind, EventRecord
from harness_kit.memory import (
    DIALOG_DIR,
    ForgetPlan,
    ForgetPolicy,
    HarnessMemoryConfig,
    HitCounter,
    MaintenanceResult,
    MemoryClient,
    MemoryForgetter,
    MemoryJobs,
    MemoryMaintenanceScheduler,
    MemoryMaintainer,
    MemoryMetrics,
    MemorySearch,
    NightlyReport,
    ProactiveReader,
    ReMeWorkspace,
    SESSION_END_EVENT_NAME,
    SessionDistiller,
)
from harness_kit.memory.client import MemoryUnavailableError
from harness_kit.memory.jobs import (
    DEFAULT_JOB_TIMEOUT_S,
    RESIDENT_BACKENDS,
    JobTimeout,
)

# ======================================================================
# 共用夹具与替身
# ======================================================================


class RecordingMaintainer:
    """记录调用的假 maintainer（鸭子类型，不继承 ``MemoryMaintainer``）。

    ``MemoryMaintenanceScheduler`` 只调 ``auto_memory`` / ``auto_dream`` /
    ``.client``，不做 ``isinstance`` 校验。所以本类可以完全不碰 ReMe 与 LLM ——
    A 组测试要证明的是"调度器什么时候决定调、把什么参数传下去"，
    而不是"ReMe 能不能蒸出记忆"。

    Attributes:
        calls (`list[dict[str, Any]]`): ``auto_memory`` 的入参快照。
        dreams (`list[dict[str, Any]]`): ``auto_dream`` 的入参快照。
        result (`MaintenanceResult`): ``auto_memory`` 的返回值。
    """

    def __init__(self, result: MaintenanceResult | None = None, *, client: Any = None) -> None:
        """初始化。

        Args:
            result (`MaintenanceResult | None`): 预设返回值。
            client (`Any`): ``.client`` 属性。
        """
        self.calls: list[dict[str, Any]] = []
        self.dreams: list[dict[str, Any]] = []
        self.client = client
        self.result = result or MaintenanceResult(action="created", path="daily/2026-09-22/fake.md")

    async def auto_memory(
        self,
        *,
        session_id: str,
        msgs: list[Any],
        allowed_paths: list[str] | None = None,
    ) -> MaintenanceResult:
        """记录一次调用。

        Args:
            session_id (`str`): 会话 id。
            msgs (`list[Any]`): 消息。
            allowed_paths (`list[str] | None`): 白名单。

        Returns:
            `MaintenanceResult`: 预设结果。
        """
        self.calls.append(
            {"session_id": session_id, "n_msgs": len(list(msgs or ())), "allowed_paths": allowed_paths},
        )
        return self.result

    async def auto_dream(self, **kwargs: Any) -> MaintenanceResult:
        """记录一次 dream 调用。

        Args:
            **kwargs (`Any`): 透传参数。

        Returns:
            `MaintenanceResult`: ``skipped``。
        """
        self.dreams.append(dict(kwargs))
        return MaintenanceResult(action="skipped", path=None, detail="假 maintainer")


def session_end_record(session_id: str, seq: int = 3) -> EventRecord:
    """造一条"会话结束"事件。

    Args:
        session_id (`str`): 会话 id。
        seq (`int`): 序号。

    Returns:
        `EventRecord`: ``kind=CUSTOM`` 且 ``payload["name"] == "session_end"``。
    """
    return EventRecord(
        session_id=session_id,
        seq=seq,
        kind=EventKind.CUSTOM,
        payload={"name": SESSION_END_EVENT_NAME, "data": {}},
    )


async def msgs_of(n: int) -> list[Any]:
    """造 ``n`` 条会话消息。

    Args:
        n (`int`): 条数。

    Returns:
        `list[Any]`: ``Msg`` 列表。
    """
    out: list[Any] = []
    for index in range(n):
        if index % 2:
            out.append(AssistantMsg(name="assistant", content=f"回复 {index}：已记录。"))
        else:
            out.append(UserMsg(name="user", content=f"事实 {index}：预算上限 200 USD/月。"))
    return out


@pytest.fixture()
async def jobs_client(tmp_path: Path) -> AsyncIterator[tuple[MemoryJobs, MemoryClient, ReMeWorkspace]]:
    """一个隔离的嵌入式 ReMe，装配了本讲用到的全部 job。

    Args:
        tmp_path (`Path`): pytest 给的临时目录（每个测试一个）。

    Yields:
        `tuple[MemoryJobs, MemoryClient, ReMeWorkspace]`: job 门面、客户端、工作区。
    """
    workspace = ReMeWorkspace(root=tmp_path / "ws")
    workspace.ensure()
    client = MemoryClient(
        HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None)
        .with_jobs(
            # 这份名单不是抄来的，是 ``AutoMemoryStep`` 的 create/update 工具表
            # （``third_party/ReMe/reme/steps/evolve/auto_memory.py:72-73``）
            # 加上它自查笔记用的 ``daily_list``、改名用的 ``move``、
            # 以及本讲自己的删除 / 降权路径。
            "auto_memory",
            "auto_dream",
            "daily_list",
            "daily_write",
            "daily_reindex",
            "move",
            "read",
            "edit",
            "write",
            "delete",
            "frontmatter_update",
            "list_tags",
            "reindex",
            "search",
            "traverse",
        )
        .build(),
    )
    await client.start()
    try:
        yield MemoryJobs(client), client, workspace
    finally:
        await client.aclose()


# ======================================================================
# A 组：调度器的事件语义（纯逻辑，不启动 ReMe）
# ======================================================================


def test_contract_constants_match_the_spec() -> None:
    """契约 §3.18 里写死的常量必须逐字相等。"""
    assert SESSION_END_EVENT_NAME == "session_end"
    assert DEFAULT_JOB_TIMEOUT_S == 60.0
    assert RESIDENT_BACKENDS == frozenset({"background", "cron"})
    assert DIALOG_DIR == "session/dialog"
    assert HitCounter().count("whatever") == 0


def test_event_kind_has_no_session_end() -> None:
    """``EventKind`` 是封闭枚举，**没有** ``SESSION_END`` —— 回归钉。

    这不是"没查到"，而是有意的设计：新增一个枚举值会让所有按 ``EventKind``
    穷举的消费者（``PAYLOAD_FIELDS`` / ``topic_matches`` / 任何 ``match``）
    在升级时静默漏掉它。所以"会话结束"走 ``CUSTOM`` + ``payload["name"]``。
    """
    assert "SESSION_END" not in EventKind.__members__
    assert not hasattr(EventKind, "SESSION_END")
    from harness_kit.events.types import PAYLOAD_FIELDS

    assert PAYLOAD_FIELDS[EventKind.CUSTOM] == ("name", "data")

    # 反向确认：payload 缺字段**不会抛异常**，只会被 ``missing_payload_fields()``
    # 报出来。这一点很关键 —— EventRecord 是"不可变事件记录"，
    # 它对 payload 做的是**约定 + 告警**而不是强校验；
    # 谁以为"少写 name 会被挡住"，谁就会在线上收到一堆不触发任何东西的事件。
    incomplete = EventRecord(session_id="s", seq=1, kind=EventKind.CUSTOM, payload={"data": {}})
    assert incomplete.missing_payload_fields() == ["name"]
    assert session_end_record("s").missing_payload_fields() == []


def test_scheduler_rejects_bad_construction() -> None:
    """``min_messages`` 必须为正，``event_name`` 不能为空。"""
    maintainer = RecordingMaintainer()
    with pytest.raises(ValueError):
        MemoryMaintenanceScheduler(maintainer, min_messages=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryMaintenanceScheduler(maintainer, min_messages=-3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryMaintenanceScheduler(maintainer, event_name="   ")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MemoryMaintenanceScheduler(maintainer).attach(object())  # type: ignore[arg-type]


async def test_attach_filters_non_target_events() -> None:
    """只有 ``CUSTOM`` + 目标名字才触发；其余事件连计数都不该动。"""
    maintainer = RecordingMaintainer()
    scheduler = MemoryMaintenanceScheduler(maintainer, msgs_of=lambda sid: msgs_of(4))  # type: ignore[arg-type]
    bus = EventBus()
    subscription = scheduler.attach(bus)
    await bus.start()
    try:
        assert subscription.topic == EventKind.CUSTOM
        await bus.publish(EventKind.REPLY_END, EventRecord(session_id="s", seq=1, kind=EventKind.REPLY_END, payload={"n_messages": 1}))
        await bus.publish(
            EventKind.CUSTOM,
            EventRecord(session_id="s", seq=2, kind=EventKind.CUSTOM, payload={"name": "not_us", "data": {}}),
        )
        await bus.drain()
        assert scheduler.stats() == {"triggered": 0, "delegated": 0, "delivered": 0, "skipped": 0, "failed": 0}
        assert maintainer.calls == []

        await bus.publish(EventKind.CUSTOM, session_end_record("s-1"))
        await bus.drain()
        assert maintainer.calls == [{"session_id": "s-1", "n_msgs": 4, "allowed_paths": None}]
        assert scheduler.stats()["delivered"] == 1
    finally:
        await bus.aclose()


async def test_min_messages_blocks_before_calling_the_job() -> None:
    """消息不足时返回 ``skipped`` 且**不调 job** —— 省一次 LLM 往返。"""
    maintainer = RecordingMaintainer()
    scheduler = MemoryMaintenanceScheduler(maintainer, msgs_of=lambda sid: msgs_of(1), min_messages=5)  # type: ignore[arg-type]
    result = await scheduler.flush("s-quiet")
    assert result.action == "skipped"
    assert result.path is None
    assert maintainer.calls == []
    assert "min_messages=5" in result.detail

    ok = MemoryMaintenanceScheduler(maintainer, msgs_of=lambda sid: msgs_of(5), min_messages=5)  # type: ignore[arg-type]
    assert (await ok.flush("s-loud")).action == "created"
    assert len(maintainer.calls) == 1


async def test_missing_message_source_is_failed_not_skipped() -> None:
    """没有 ``msgs_of`` 是**配置错误**（``failed``），不是"这个会话没内容"（``skipped``）。"""
    maintainer = RecordingMaintainer()
    blind = MemoryMaintenanceScheduler(maintainer)  # type: ignore[arg-type]
    result = await blind.flush("s-1")
    assert result.action == "failed"
    assert "msgs_of" in result.detail

    empty = MemoryMaintenanceScheduler(maintainer, msgs_of=lambda sid: msgs_of(3))  # type: ignore[arg-type]
    assert (await empty.flush("   ")).action == "failed"


async def test_msgs_of_exception_becomes_failed() -> None:
    """``msgs_of`` 抛异常时也翻成 ``failed``：事件 handler 里抛异常会丢事件。"""

    async def boom(session_id: str) -> list[Any]:
        raise RuntimeError("会话存储连不上")

    maintainer = RecordingMaintainer()
    scheduler = MemoryMaintenanceScheduler(maintainer, msgs_of=boom)  # type: ignore[arg-type]
    result = await scheduler.flush("s-1")
    assert result.action == "failed"
    assert "会话存储连不上" in result.detail
    assert maintainer.calls == []


async def test_flush_does_not_touch_stats_but_on_event_does() -> None:
    """``flush`` 是人为调用（不动统计、不记 metrics），``on_event`` 才是事件流量。"""
    maintainer = RecordingMaintainer()
    metrics = MemoryMetrics()
    scheduler = MemoryMaintenanceScheduler(maintainer, msgs_of=lambda sid: msgs_of(2), metrics=metrics)  # type: ignore[arg-type]

    await scheduler.flush("s-manual")
    assert scheduler.stats()["triggered"] == 0
    assert metrics.session_snapshot("s-manual").get("writebacks", 0.0) == 0.0

    await scheduler.on_event(session_end_record("s-manual"))
    assert scheduler.stats()["triggered"] == 1
    assert metrics.session_snapshot("s-manual")["writebacks"] == 1.0


async def test_on_event_returns_none_for_foreign_kinds() -> None:
    """不是目标事件时返回 ``None``（不是 ``skipped`` —— 那会污染统计）。"""
    maintainer = RecordingMaintainer()
    scheduler = MemoryMaintenanceScheduler(maintainer)  # type: ignore[arg-type]
    assert await scheduler.on_event(EventRecord(session_id="s", seq=1, kind=EventKind.REPLY_START, payload={})) is None
    assert await scheduler.on_event(session_end_record("s-2")) is not None


async def test_failed_event_is_counted_as_failed() -> None:
    """事件驱动路径上的失败要落到 ``failed`` 计数里。"""

    async def boom(session_id: str) -> list[Any]:
        raise RuntimeError("nope")

    maintainer = RecordingMaintainer()
    scheduler = MemoryMaintenanceScheduler(maintainer, msgs_of=boom)  # type: ignore[arg-type]
    await scheduler.on_event(session_end_record("s-x"))
    assert scheduler.stats()["failed"] == 1
    assert scheduler.stats()["delivered"] == 0


def test_explain_is_honest_about_missing_source() -> None:
    """``explain()`` 在没配 ``msgs_of`` 时要明说，而不是安静地空着。"""
    text = MemoryMaintenanceScheduler(RecordingMaintainer()).explain()  # type: ignore[arg-type]
    assert "未配置" in text
    assert "session_end" in text


def test_nightly_report_summary_carries_the_zero_reindex_warning() -> None:
    """``reindex`` 成功但三个计数全 0 时，``summary()`` 仍然把数摆出来。"""
    report = NightlyReport(date="2026-09-22", reindex_ok=True, reindex_counts={"added": 0, "modified": 0, "deleted": 0})
    assert "added=0" in report.summary()
    assert "dream skipped" in report.summary()
    failed = NightlyReport(date="2026-09-22", reindex_ok=False)
    assert "reindex FAILED" in failed.summary()


# ======================================================================
# B 组：会话结束自动落记忆（整形 + 调度，0 次模型调用）
# ======================================================================


def test_shape_messages_drops_injected_memory_hint() -> None:
    """``name == "memory"`` 的消息是检索产物，写回去会让记忆自我复制。"""
    shaped = SessionDistiller.shape_messages(
        [
            UserMsg(name="user", content="事实 A"),
            UserMsg(name="memory", content="[记忆] 事实 A 的旧版本"),
            AssistantMsg(name="assistant", content="已记录"),
        ],
    )
    assert [item["name"] for item in shaped] == ["user", "assistant"]


def test_shape_messages_wraps_plain_string_content() -> None:
    """``{"content": "纯文本"}`` 是 JSON 反序列化的形状，必须被包成块列表。"""
    shaped = SessionDistiller.shape_messages([{"name": "user", "role": "user", "content": "纯文本", "id": "m1"}])
    first = shaped[0]["content"][0]
    assert first["type"] == "text" and first["text"] == "纯文本"


def test_shape_messages_drops_blank_messages() -> None:
    """只有空文本块的消息会让 ``n_messages`` 虚高，一律丢弃。"""
    assert SessionDistiller.shape_messages([UserMsg(name="user", content="   "), AssistantMsg(name="assistant", content="")]) == []


def test_shape_messages_rejects_unknown_types() -> None:
    """既不是 ``Msg`` 也不是 dict 的元素要报错，而不是被静默跳过。"""
    with pytest.raises(ValueError):
        SessionDistiller.shape_messages([42])  # type: ignore[list-item]


def test_messages_from_agent_requires_state_context() -> None:
    """没有 ``state.context`` 的对象要报错说清需要什么。"""
    with pytest.raises(ValueError):
        SessionDistiller.messages_from_agent(object())


async def test_scheduler_drives_a_real_maintainer_offline(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """调度器 + 真实 ``MemoryMaintainer``：无消息源时 ``failed``，不碰 ReMe。

    这一条把"调度层"和"job 层"的接缝钉住：``MemoryMaintainer`` 是真实对象
    （它的 ``run_job`` 会真的走 ReMe），但因为消息源缺失，**一次 job 都不会被调**。
    """
    _jobs, client, _ws = jobs_client
    maintainer = MemoryMaintainer(client)
    scheduler = MemoryMaintenanceScheduler(maintainer, min_messages=2)
    result = await scheduler.flush("s-no-source")
    assert result.action == "failed"
    assert "msgs_of" in result.detail


async def test_maintainer_translates_created_metadata(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``MemoryMaintainer.auto_memory`` 把 ``metadata`` 的两种形状翻成同一个枚举。

    这一条**不调 job**：它用一个假 client 顶掉 ``run_job``，
    好让"翻译层"的逻辑在 0 次模型调用下被钉住。
    """
    from reme.schema import Response

    class _StubClient:
        """只实现 ``run_job`` 的假 client。"""

        def __init__(self, metadata: dict[str, Any], answer: str = "") -> None:
            self.metadata = metadata
            self.answer = answer

        async def run_job(self, name: str, /, **kwargs: Any) -> Response:
            return Response(success=True, answer=self.answer, metadata=dict(self.metadata))

    # 形状 1：ReMe 给 ``created: True`` + ``path``。
    created = MemoryMaintainer(_StubClient({"created": True, "path": "daily/2026-09-22/x.md"}, "建好了"))
    result = await created.auto_memory(session_id="s-1", msgs=[])
    assert result.action == "created" and result.path == "daily/2026-09-22/x.md"

    # 形状 2：ReMe 只给 ``modified: True``（更新分支），不给 path。
    modified = MemoryMaintainer(_StubClient({"modified": True, "path": "daily/2026-09-22/x.md"}))
    assert (await modified.auto_memory(session_id="s-1", msgs=[])).action == "updated"

    # 形状 3：两个标记都是 False —— 模型判断"不值得记"，是**正常**结果。
    nothing = MemoryMaintainer(_StubClient({}, "这一轮没有值得落盘的长期事实"))
    outcome = await nothing.auto_memory(session_id="s-1", msgs=[])
    assert outcome.action == "skipped"
    assert outcome.detail


# ======================================================================
# C 组：定时与后台编排（真实 ReMe，0 次模型调用）
# ======================================================================


async def test_resident_jobs_are_refused_in_the_foreground(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``background`` / ``cron`` 后端的 job 前台执行会被拒绝，并指出替代品。"""
    jobs, client, _ws = jobs_client
    assert {jobs.backend_of(name) for name in jobs.available()} == {"base"}

    class _CronStub:
        """假装 ``context.jobs["dream_cron"]`` 上的 cron job。"""

        backend = "cron"

    client.application.context.jobs["dream_cron"] = _CronStub()  # type: ignore[assignment]
    try:
        with pytest.raises(MemoryUnavailableError) as excinfo:
            await jobs.run_once("dream_cron")
        assert "常驻" in str(excinfo.value)
        assert "auto_dream" in str(excinfo.value)
    finally:
        del client.application.context.jobs["dream_cron"]


async def test_unknown_job_and_bad_timeout(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """未知 job 与非法超时都要给出可执行的下一步。"""
    jobs, _client, _ws = jobs_client
    with pytest.raises(MemoryUnavailableError) as excinfo:
        await jobs.run_once("no_such_job")
    assert "with_jobs" in str(excinfo.value)
    with pytest.raises(ValueError):
        await jobs.run_once("reindex", timeout_s=0)
    with pytest.raises(ValueError):
        await jobs.run_once("reindex", timeout_s=None)  # type: ignore[arg-type]


async def test_run_all_reports_failures_without_raising(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """一批 job 里有一个不可用，不该让其余结果拿不到。"""
    jobs, _client, _ws = jobs_client
    results = await jobs.run_all(["daily_list", "no_such_job", "list_tags"])
    assert set(results) == {"daily_list", "no_such_job", "list_tags"}
    assert results["no_such_job"].success is False
    assert results["list_tags"].success is True


async def test_reindex_reports_counts_not_just_success(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``reindex`` 的 ``success=True`` 不够 —— 三个计数才是真相。"""
    jobs, _client, workspace = jobs_client
    daily = workspace.daily_path() / "2026-09-22"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "a.md").write_text("---\nname: 卡片 A\nmemory_tags: [ops]\n---\n\n# 卡片 A\n\n回滚三步：停流量、回滚镜像、验单。\n", encoding="utf-8")
    response = await jobs.reindex()
    counts = dict((response.metadata or {}).get("counts") or {})
    assert response.success is True
    assert counts.get("added") == 1, counts
    assert "added" in counts and "modified" in counts and "deleted" in counts


async def test_daily_list_sees_the_written_card(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``daily_list`` 按日期列卡片，``metadata["notes"]`` 带 front matter。"""
    jobs, _client, workspace = jobs_client
    daily = workspace.daily_path() / "2026-08-01"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "b.md").write_text("---\nname: 卡片 B\nmemory_tags: [ops]\n---\n\n# 卡片 B\n\n内容。\n", encoding="utf-8")
    response = await jobs.daily_list("2026-08-01")
    notes = list((response.metadata or {}).get("notes") or [])
    assert (response.metadata or {}).get("count") == 1
    assert any("b.md" in str(note.get("path", "")) for note in notes)


def test_job_timeout_carries_the_job_and_seconds() -> None:
    """``JobTimeout`` 要能被单独捕获，并带上 job 名与秒数。"""
    error = JobTimeout("auto_memory", 12.5)
    assert isinstance(error, RuntimeError)
    assert error.job == "auto_memory" and error.timeout_s == 12.5
    assert "12.5" in str(error)


# ======================================================================
# D 组：遗忘与归档（真实 ReMe，0 次模型调用）
# ======================================================================

OLD_MTIME: float = time.time() - 90 * 86400.0


def write_card(path: Path, name: str, tags: str, body: str) -> Path:
    """写一张带 front matter 的记忆卡并把 mtime 拨到 90 天前。

    Args:
        path (`Path`): 目标文件。
        name (`str`): ``name`` 字段。
        tags (`str`): ``memory_tags`` 字段（``[a, b]`` 形式）。
        body (`str`): 正文。

    Returns:
        `Path`: ``path``。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\nmemory_tags: {tags}\n---\n\n{body}\n", encoding="utf-8")
    os.utime(path, (OLD_MTIME, OLD_MTIME))
    return path


async def test_plan_is_dry_run_and_classifies_three_ways(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``plan`` 只算不删，并把文件分成 delete / demote / protected 三类。"""
    jobs, client, workspace = jobs_client
    cold = write_card(workspace.daily_path() / "2026-06-01" / "cold.md", "冷记忆", "[ops]", "# 冷记忆\n\n三个月没人查。")
    warm = write_card(workspace.resource_path() / "warm.md", "常用手册", "[ops]", "# 常用手册\n\n被查过好几次。")
    pinned = write_card(workspace.digest_path() / "pinned.md", "钉住的摘要", "[pinned]", "# 钉住的摘要\n\n长期有效。")
    await jobs.reindex()

    counter = HitCounter()
    counter.record(["resource/warm.md", "daily/2026-06-01/cold.md"])
    counter.record(["resource/warm.md"])  # 第二次 —— 同一次 record 里的重复项会被去重

    forgetter = MemoryForgetter(
        client,
        ForgetPolicy(max_age_days=30, min_hits=1, protected_tags=["pinned"]),
        hits=counter,
        workspace=workspace,
    )
    plan = await forgetter.plan()
    assert cold.is_file(), "plan 不许改磁盘"
    assert plan.protected == ["digest/pinned.md"]
    assert plan.delete == ["daily/2026-06-01/cold.md"]
    assert plan.demote == ["resource/warm.md"]
    assert not plan.is_empty()
    assert plan.summary() == "delete=1 demote=1 protected=1"
    assert warm.is_file() and pinned.is_file()


async def test_apply_deletes_then_demotes_by_hit_count(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``hits <= min_hits`` 的删除，``hits > min_hits`` 的只降权。

    降权写的是 ``memory_status: stale``（``DEMOTE_KEY`` / ``DEMOTE_VALUE``），
    并且必须能被 :meth:`MemoryForgetter.stale_paths` 读回来 ——
    否则这个标记写了等于没写。
    """
    jobs, client, workspace = jobs_client
    cold = write_card(workspace.daily_path() / "2026-06-01" / "cold.md", "冷记忆", "[ops]", "# 冷记忆\n\n没人查。")
    warm = write_card(workspace.resource_path() / "warm.md", "常用手册", "[ops]", "# 常用手册\n\n查过。")
    await jobs.reindex()

    counter = HitCounter({"resource/warm.md": 1, "daily/2026-06-01/cold.md": 0})
    forgetter = MemoryForgetter(
        client,
        ForgetPolicy(max_age_days=30, min_hits=0, protected_tags=["pinned"]),
        hits=counter,
        workspace=workspace,
    )
    plan = await forgetter.plan()
    assert plan.delete == ["daily/2026-06-01/cold.md"]
    assert plan.demote == ["resource/warm.md"]

    deleted = await forgetter.apply(plan)
    assert deleted == 1
    assert not cold.is_file()
    assert warm.is_file()
    assert "memory_status: stale" in warm.read_text(encoding="utf-8")
    assert await forgetter.stale_paths() == ["resource/warm.md"]
    assert (await forgetter.plan()).is_empty()


async def test_empty_policy_never_deletes(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``max_age_days=None`` = 不做年龄判断：再老的文件也不进 delete。"""
    jobs, client, workspace = jobs_client
    old = write_card(workspace.daily_path() / "2020-01-01" / "ancient.md", "远古卡", "[ops]", "# 远古卡\n\n很老。")
    await jobs.reindex()
    forgetter = MemoryForgetter(client, ForgetPolicy(max_age_days=None), hits=HitCounter(), workspace=workspace)
    plan = await forgetter.plan()
    assert plan.delete == [] and plan.demote == []
    assert old.is_file()


def test_forget_policy_and_plan_shapes() -> None:
    """策略与计划的形状（默认值、归一化、校验）。"""
    policy = ForgetPolicy()
    assert policy.max_age_days is None and policy.min_hits == 0
    assert policy.normalized_protected() == {"pinned"}
    assert ForgetPolicy(protected_tags=["PINNED", " Pinned "]).normalized_protected() == {"pinned"}
    assert ForgetPolicy(protected_tags=[]).normalized_protected() == set()
    # ``ForgetPolicy`` 是**纯数据模型**，字段上没有任何约束 —— 传 -1 它照样接受。
    # 把关的是执行者 ``MemoryForgetter.__init__``。这条断言钉的就是这个分工：
    # 别指望在构造策略时就被拦住。
    assert ForgetPolicy(max_age_days=-1).max_age_days == -1
    plan = ForgetPlan()
    assert plan.is_empty() and plan.summary() == "delete=0 demote=0 protected=0"
    assert not ForgetPlan(delete=["a.md"]).is_empty()


async def test_forgetter_rejects_negative_policy(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """绕过 pydantic 直接传负值时也要被挡住。"""
    _jobs, client, _ws = jobs_client
    policy = ForgetPolicy()
    object.__setattr__(policy, "max_age_days", -5)
    with pytest.raises(ValueError):
        MemoryForgetter(client, policy)


def test_hit_counter_dedupes_within_one_record() -> None:
    """同一文件的多个 chunk 命中一次检索只算一次（按路径计数）。"""
    counter = HitCounter()
    counter.record(["a.md", "a.md", "b.md"])
    assert counter.count("a.md") == 1 and counter.count("b.md") == 1
    counter.record(["a.md"])
    assert counter.count("a.md") == 2
    assert counter.count("missing.md") == 0
    assert counter.as_dict() == {"a.md": 2, "b.md": 1}


# ======================================================================
# E 组：主动读取（真实 ReMe，0 次模型调用）
# ======================================================================

DIALOG: list[dict[str, Any]] = [
    {"id": "d1", "name": "user", "role": "user", "content": [{"type": "text", "text": "帮我看看蓝绿切换的检查清单"}]},
    {"id": "d2", "name": "assistant", "role": "assistant", "content": [{"type": "text", "text": "好的，我查一下。"}]},
    {"id": "d3", "name": "user", "role": "user", "content": [{"type": "text", "text": "确认下回滚流程是不是停流量 → 回滚镜像 → 验单"}]},
]


async def test_proactive_derives_query_from_dialog_then_gates(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """主动读取：从对话文件推查询串 → 检索 → 按**归一化**置信度放行/挡下。

    三个要点：

    1. 查询串**只取 user 文本** —— assistant 的回复里有"我查一下"这类
       由记忆自己产生的内容，拿它当查询串等于用记忆查记忆。
    2. 归一化置信度是"相对最好的一条"，所以最强那条恒为 1.0；
       阈值只在**有第二名**时才有可观察效果。
    3. 被阈值挡下的每一条都要记 ``gate_rejections_min_score``，
       否则"什么都没返回"会被误读成"什么都没发生"。
    """
    import json

    jobs, client, workspace = jobs_client
    dialog = workspace.dialog_path()
    dialog.mkdir(parents=True, exist_ok=True)
    (dialog / "s-1.jsonl").write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in DIALOG) + "\n",
        encoding="utf-8",
    )
    today = time.strftime("%Y-%m-%d")
    daily = workspace.daily_path() / today
    write_card(
        daily / "rollback.md",
        "回滚流程",
        "[ops]",
        "# 回滚流程\n\n回滚三步：先停流量，再回滚镜像 tag，最后验单（冒烟测试）。",
    )
    write_card(daily / "rollback-notes.md", "回滚随手记", "[ops]", "# 回滚随手记\n\n回滚这件事踩过坑，细节待补。")
    await jobs.reindex()

    search = MemorySearch(client, workspace=workspace)
    reader = ProactiveReader(search, min_confidence=0.0)
    query = await reader.query_for("s-1")
    assert query and "蓝绿" in query and "好的，我查一下" not in query

    hits = await reader.suggest(session_id="s-1", limit=3)
    assert hits and hits[0].path.endswith("rollback.md")
    assert len(hits) <= 3

    metrics = MemoryMetrics()
    strict = ProactiveReader(MemorySearch(client, workspace=workspace), min_confidence=0.95, metrics=metrics)
    kept = await strict.suggest(session_id="s-1", limit=3)
    assert len(kept) == 1 and kept[0].path.endswith("rollback.md")
    assert metrics.snapshot()["gate_rejections_min_score"] >= 1

    # 最强那条的归一化置信度恒为 1.0 —— 钉住"归一化是相对量"。
    from harness_kit.memory.gating import MemoryGate

    result = await search.search(query, limit=3)
    assert MemoryGate.normalize_scores(result.hits)[0] == pytest.approx(1.0)


async def test_proactive_without_dialog_returns_empty(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """推不出查询串时返回空列表，**不**退回一个固定查询串。"""
    _jobs, client, workspace = jobs_client
    reader = ProactiveReader(MemorySearch(client, workspace=workspace), min_confidence=0.0)
    assert await reader.query_for("s-missing") is None
    assert await reader.suggest(session_id="s-missing", limit=5) == []


async def test_proactive_validates_arguments(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``min_confidence`` 越界、``lookback_days`` 非正、空 ``session_id`` 都要报错。"""
    _jobs, client, workspace = jobs_client
    search = MemorySearch(client, workspace=workspace)
    with pytest.raises(ValueError):
        ProactiveReader(search, min_confidence=1.5)
    with pytest.raises(ValueError):
        ProactiveReader(search, min_confidence=-0.1)
    with pytest.raises(ValueError):
        ProactiveReader(search, lookback_days=0)
    reader = ProactiveReader(search, min_confidence=0.0)
    with pytest.raises(ValueError):
        await reader.suggest(session_id="  ", limit=3)
    with pytest.raises(ValueError):
        await reader.suggest(session_id="s-1", limit=0)


# ======================================================================
# F 组：nightly 编排与上游脆弱性（0 次模型调用）
# ======================================================================


async def test_nightly_runs_reindex_without_touching_dream(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``nightly(dream=False)``：只重扫索引，不碰模型。"""
    _jobs, client, workspace = jobs_client
    daily = workspace.daily_path() / "2026-09-22"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "a.md").write_text("---\nname: A\nmemory_tags: [ops]\n---\n\n# A\n\n内容。\n", encoding="utf-8")
    scheduler = MemoryMaintenanceScheduler(MemoryMaintainer(client))
    report = await scheduler.nightly(dream=False)
    assert isinstance(report, NightlyReport)
    assert report.reindex_ok is True
    assert report.reindex_counts.get("added") == 1
    assert report.dream is None


async def test_nightly_never_raises_when_reindex_targets_missing(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """reindex 失败也要返回 report（``dream`` 仍会被尝试），而不是抛异常。"""
    _jobs, client, _ws = jobs_client

    class _BrokenJobs:
        """``reindex()`` 直接抛异常的替身。"""

        async def reindex(self, **_kwargs: Any) -> Any:
            raise RuntimeError("索引组件没装好")

    class _ClientWithBrokenJobs:
        """只暴露 ``jobs()`` 的假 client。"""

        def jobs(self) -> Any:
            return _BrokenJobs()

    maintainer = MemoryMaintainer(_ClientWithBrokenJobs())  # type: ignore[arg-type]
    scheduler = MemoryMaintenanceScheduler(maintainer)
    report = await scheduler.nightly(dream=False)
    assert report.reindex_ok is False
    assert "reindex FAILED" in report.summary()


def test_parse_structured_reply_swallows_bad_yaml() -> None:
    """回归钉：``auto_dream`` 的提取对模型输出格式极度敏感。

    ``dream/utils.py:137`` 先把**整段回复**当 YAML 解析；标量值里只要有一个
    未加引号的 ``": "``，``yaml.safe_load`` 就抛
    ``mapping values are not allowed here``，兜底的 ``_parse_scalar_mapping``
    只抓 ``action|target_path|note`` 三个键，对需要 ``units`` 列表的提取完全无用。
    于是整段提取**静默变成空 dict**，而 ``DreamExtractStep`` 一次重试后仍拿不到
    ``units`` 就继续往下走，``DreamFinishStep`` 照常 checkpoint 已变更路径 ——
    这批材料被永久标记为"已处理"。

    本测试不花模型调用：直接喂一段与真实回复同形的坏 YAML。
    """
    from reme.steps.evolve.dream.utils import parse_structured_reply

    reply = (
        "units:\n"
        "  - title: auto_memory 的 create 与 update 分叉\n"
        "    summary: 创建分支只挂 daily_write: 一条工具；更新分支挂 read/edit/frontmatter_update/write\n"
        "    kind: procedure\n"
    )
    assert parse_structured_reply(reply) == {}
    assert parse_structured_reply("前缀\n```yaml\n" + reply + "```\n后记") == {}
    # 对照：把那个冒号用引号包起来，解析立刻成功。
    fixed = reply.replace(
        "summary: 创建分支只挂 daily_write: 一条工具；更新分支挂 read/edit/frontmatter_update/write",
        'summary: "创建分支只挂 daily_write: 一条工具；更新分支挂 read/edit/frontmatter_update/write"',
    )
    units = parse_structured_reply(fixed).get("units")
    assert isinstance(units, list) and units[0]["title"].startswith("auto_memory")


async def test_maintainer_auto_dream_reports_failure_as_failed() -> None:
    """``auto_dream`` 的上游失败必须翻成 ``failed``，不许被吞成"成功"。"""

    class _FailingClient:
        """``run_job`` 抛 ``MemoryJobError`` 的替身。"""

        async def run_job(self, name: str, /, **kwargs: Any) -> Any:
            from harness_kit.memory.client import MemoryJobError

            raise MemoryJobError(name, "上游提取失手（expected a units list）")

    maintainer = MemoryMaintainer(_FailingClient())  # type: ignore[arg-type]
    result = await maintainer.auto_dream(date="2026-09-22")
    assert result.action == "failed"
    assert result.path is None, "auto_dream 拿不到产物路径，不许假装能给"
    assert "上游提取失手" in result.detail
    assert "auto_dream" in result.detail


async def test_auto_memory_enforces_allowed_paths_post_hoc(
    jobs_client: tuple[MemoryJobs, MemoryClient, ReMeWorkspace],
) -> None:
    """``allowed_paths`` 是**事后校验**：ReMe 的更新分支会覆写请求级白名单。

    ``AutoMemoryStep`` 在更新分支里注入 ``_allowed_paths = [note_path]``
    （``third_party/ReMe/reme/steps/evolve/auto_memory.py:415-416``），
    把请求里带的白名单顶掉。所以 harness 只能在响应回来后自己再查一遍。
    """

    class _OutOfBoundsClient:
        """``run_job`` 返回一个越界路径的替身。"""

        async def run_job(self, name: str, /, **kwargs: Any) -> Any:
            from reme.schema import Response

            return Response(
                success=True,
                answer="写好了",
                metadata={"created": True, "path": "resource/sneaky.md"},
            )

    maintainer = MemoryMaintainer(_OutOfBoundsClient())  # type: ignore[arg-type]
    result = await maintainer.auto_memory(
        session_id="s-1",
        msgs=[],
        allowed_paths=["daily/2026-09-22/"],
    )
    assert result.action == "failed"
    assert "allowed_paths" in result.detail or "越界" in result.detail or "sneaky" in result.detail


def test_maintenance_result_enum_is_closed() -> None:
    """``MaintenanceResult.action`` 只有四个合法值。"""
    from pydantic import ValidationError

    for action in ("created", "updated", "skipped", "failed"):
        assert MaintenanceResult(action=action).action == action
    with pytest.raises(ValidationError):
        MaintenanceResult(action="deleted")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        MaintenanceResult(action="created", extra_field=1)  # type: ignore[call-arg]
