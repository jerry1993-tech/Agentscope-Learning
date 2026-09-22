# -*- coding: utf-8 -*-
"""第 19 讲的 pytest：把 ReMe 做成 Harness 的长期记忆中间件。

六条纪律（延续第 15~18 讲）：

1. **0 次 LLM 调用**。本讲要钉住的是"中间件在哪个 hook 上做什么决定"：
   召回门控的四种拒绝原因、写入门控的五种原因、异步化的返回时机、
   超时与 ``close()`` 排空、成功率按 ``created/modified`` 判定 —— 全都是
   纯逻辑或假 job 能覆盖的。真实模型只在
   ``scripts/19_memory_middleware.py --live``（实测 1 次补全）。
2. **纯逻辑测试连 ReMe 都不启动**。门控矩阵、``normalize_scores`` 的量纲、
   ``build_memory_middleware`` 的键校验、``TenantRouter`` 的路径模型、
   ``MemoryMetrics`` 的比值 —— 全部毫秒级。
3. **每个测试自己建工作区，绝不共享**。ReMe 的 ``Application._start()`` 会建
   ``asyncio.Lock``，而 ``asyncio_default_fixture_loop_scope = "function"``
   意味着**每个测试一个事件循环**：跨测试复用一个 client 就是跨事件循环复用锁。
   本讲里中间件自己持有一个嵌入式 app，这条纪律更要紧 —— 所以需要真 app 的
   测试写成 ``async def`` 且自己 ``await mw.close()``。
4. **回归测试钉住已经踩过的坑**：
   ``test_writeback_success_uses_created_not_success_flag`` 对应
   "``auto_memory`` 会 ``success=True`` 却什么都没写"；
   ``test_write_gate_disabled_is_not_allowed`` 对应
   "日志里要能区分检查通过 / 没做检查"；
   ``test_normalized_score_cannot_drop_a_nonzero_batch`` 对应
   "相对归一化让 ``min_score`` 永远拦不住整批"；
   ``test_budget_counts_the_injected_object`` 对应
   "预算计量的必须就是注入的那段文本"。
5. **责任边界要显式断言**：``test_on_reasoning_injects_exactly_one_memory_msg``
   用一个假 agent 钉住"注入一条、命名 memory、只带 HintBlock、落在尾部"——
   这是中间件对 Agent Loop 的**全部**承诺，多一条少一条都是 bug。
6. **失败路径一条都不能少**：未知 Profile 键、非法 mode、非正 top_k、
   没有 session_id、空增量、门控被拒、写回超时、租户名越界。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson19_memory_middleware.py -v
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentscope.message import AssistantMsg, HintBlock, Msg, UserMsg

from harness_kit.memory import (
    DEFAULT_MIN_SCORE,
    HarnessMemoryConfig,
    MemoryBudget,
    MemoryClient,
    MemoryGate,
    MemoryHit,
    MemoryIngestor,
    MemoryMetrics,
    MemoryWriteGate,
    ReMeWorkspace,
    TenantError,
    TenantRouter,
    estimate_tokens_heuristic,
)
from harness_kit.memory.middleware import (
    LongTermMemoryMiddleware,
    build_memory_middleware,
)

# ======================================================================
# 共用夹具与替身
# ======================================================================
#: 允许的 Profile 键（与 ``middleware._MEMORY_MIDDLEWARE_PARAMS`` 对齐）。
EXPECTED_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "workspace_dir",
        "mode",
        "top_k",
        "min_score",
        "budget_tokens",
        "tool_context_id",
        "chat_model",
        "gate_min_score",
        "sensitive_tags",
        "session_tags",
        "write_min_messages",
        "write_min_chars",
        "write_async",
        "write_timeout_s",
    },
)


def make_hit(path: str, text: str, score: float) -> MemoryHit:
    """造一条 ``MemoryHit``（纯数据）。

    Args:
        path (`str`): 工作区相对路径。
        text (`str`): 片段正文。
        score (`float`): 融合分数。

    Returns:
        `MemoryHit`: 命中对象。
    """
    return MemoryHit(
        chunk_id=f"{path}#{score}",
        path=path,
        start_line=1,
        end_line=1,
        text=text,
        score=score,
        source="fused",
    )


def spec_of(**params: Any) -> SimpleNamespace:
    """造一个只有 ``params`` 的 ``MiddlewareSpec`` 替身。

    Args:
        **params (`Any`): 参数。

    Returns:
        `SimpleNamespace`: 替身。
    """
    return SimpleNamespace(params=dict(params))


def fake_response(
    *,
    success: bool = True,
    created: bool = False,
    modified: bool = False,
    n_messages: int = 2,
) -> SimpleNamespace:
    """造一个形如 ReMe ``Response`` 的替身。

    Args:
        success (`bool`): ``success`` 字段。
        created (`bool`): ``metadata["created"]``。
        modified (`bool`): ``metadata["modified"]``。
        n_messages (`int`): ``metadata["n_messages"]``。

    Returns:
        `SimpleNamespace`: 替身响应。
    """
    return SimpleNamespace(
        success=success,
        answer="ok",
        metadata={"created": created, "modified": modified, "n_messages": n_messages},
    )


def make_job_spy(
    sink: list[str],
    *,
    response: Any = None,
    delay: float = 0.0,
) -> Any:
    """造一个假 ``_run_job``：记 job 名、按需延迟、返回固定响应。

    Args:
        sink (`list[str]`): 记录 job 名的列表。
        response (`Any`): 返回的响应。
        delay (`float`): 每次调用前的 ``asyncio.sleep`` 秒数。

    Returns:
        `Any`: 可直接赋给 ``mw._run_job`` 的协程函数。
    """

    async def _job(name: str, **kwargs: Any) -> Any:
        if delay:
            await asyncio.sleep(delay)
        sink.append(name)
        if response is None:
            return fake_response(created=True)
        return response

    return _job


def increment() -> list[Msg]:
    """一轮像样的"用户说 + 助手答"增量。

    Returns:
        `list[Msg]`: 增量消息。
    """
    return [
        UserMsg("alice", "把部署令牌放到 ~/.secrets/token，权限 600。"),
        AssistantMsg("probe", "记下了：路径 ~/.secrets/token，权限 600。"),
    ]


def memory_middleware(tmp_path: Path, **params: Any) -> LongTermMemoryMiddleware:
    """造一个指向临时工作区的中间件（**不启动 app**）。

    Args:
        tmp_path (`Path`): pytest 的临时目录。
        **params (`Any`): 透传 ``LongTermMemoryMiddleware`` 的参数。

    Returns:
        `LongTermMemoryMiddleware`: 中间件实例。
    """
    return LongTermMemoryMiddleware(workspace_dir=str(tmp_path / "ws"), **params)


# ======================================================================
# A 装配层：Profile 键校验与"标量 → 对象"
# ======================================================================
def test_unknown_param_raises_value_error() -> None:
    """拼错的 Profile 键必须报错，而不是静默不生效。"""
    with pytest.raises(ValueError, match="write_asnyc"):
        build_memory_middleware(spec_of(write_asnyc=False))


def test_param_whitelist_matches_contract() -> None:
    """键白名单要与契约 §3.19 逐字对齐。"""
    from harness_kit.memory import middleware as mw_module

    assert frozenset(mw_module._MEMORY_MIDDLEWARE_PARAMS) == EXPECTED_PARAM_KEYS


@pytest.mark.parametrize("mode", ["static", "AGENT_CONTROL", ""])
def test_bad_mode_raises(mode: str) -> None:
    """``mode`` 只认三个取值。

    Args:
        mode (`str`): 非法取值。
    """
    with pytest.raises(ValueError, match="memory mode"):
        build_memory_middleware(spec_of(mode=mode))


def test_non_positive_top_k_raises() -> None:
    """``top_k<=0`` 会让检索无意义，直接拒绝。"""
    with pytest.raises(ValueError, match="top_k"):
        build_memory_middleware(spec_of(top_k=0))


def test_defaults_do_not_gate() -> None:
    """不配门控时，中间件的行为与官方实现一致（每轮都写、不做召回门控）。"""
    mw = build_memory_middleware(spec_of())
    assert mw._parameters.gate is None
    assert mw._parameters.write_gate is None
    assert mw._parameters.write_async is True
    assert mw._parameters.session_tags == ()


def test_gate_scalars_become_objects() -> None:
    """``gate_min_score`` / ``write_min_messages`` 由 builder 变成对象。"""
    mw = build_memory_middleware(
        spec_of(
            gate_min_score=0.4,
            sensitive_tags=["pii"],
            session_tags=["acme"],
            write_min_messages=3,
            write_min_chars=20,
            write_async=False,
            write_timeout_s=7.5,
        ),
    )
    params = mw._parameters
    assert isinstance(params.gate, MemoryGate)
    assert params.gate.min_score == 0.4
    assert params.gate.sensitive_tags == ["pii"]
    assert params.session_tags == ("acme",)
    assert isinstance(params.write_gate, MemoryWriteGate)
    assert params.write_gate.min_messages == 3
    assert params.write_gate.min_chars == 20
    assert params.write_async is False
    assert params.write_timeout_s == 7.5


def test_sensitive_tags_alone_enable_gate_with_default_threshold() -> None:
    """只给 ``sensitive_tags`` 也能建出门控，阈值回落到契约默认值。"""
    mw = build_memory_middleware(spec_of(sensitive_tags=["pii"]))
    assert isinstance(mw._parameters.gate, MemoryGate)
    assert mw._parameters.gate.min_score == DEFAULT_MIN_SCORE


def test_zero_budget_means_no_truncation() -> None:
    """``budget_tokens=0`` 表示不裁剪（与官方行为一致），而不是"预算为 0"。"""
    assert build_memory_middleware(spec_of(budget_tokens=0))._parameters.budget is None
    assert isinstance(
        build_memory_middleware(spec_of(budget_tokens=800))._parameters.budget,
        MemoryBudget,
    )


def test_compat_patch_is_lazy(tmp_path: Path) -> None:
    """兼容补丁必须在"建 app"之前才打，构造中间件时不能已经打过。"""
    mw = memory_middleware(tmp_path)
    assert mw.compat_patched == []


# ======================================================================
# B 召回门控
# ======================================================================
def test_normalize_scores_is_relative_to_best() -> None:
    """归一化是"相对最佳分"，绝对量纲不参与。"""
    hits = [make_hit("a.md", "x", 0.9), make_hit("b.md", "y", 0.45)]
    assert MemoryGate.normalize_scores(hits) == [1.0, 0.5]
    other = [make_hit("a.md", "x", 100.0), make_hit("b.md", "y", 50.0)]
    assert MemoryGate.normalize_scores(other) == [1.0, 0.5]


def test_normalize_scores_handles_zero_and_empty() -> None:
    """全 0 分给全 0，空输入给空。"""
    assert MemoryGate.normalize_scores([]) == []
    assert MemoryGate.normalize_scores([make_hit("a.md", "x", 0.0)]) == [0.0]
    assert MemoryGate.normalize_scores([make_hit("a.md", "x", -3.0)]) == [0.0]


def test_normalized_score_cannot_drop_a_nonzero_batch() -> None:
    """**量纲事实**：只要有一批非零分，最高分那条归一化后恒为 1.0，

    所以 ``below_min_score`` 永远拦不住整批 —— 想按绝对相关性过滤，
    必须把 ``min_score`` 交给 ReMe 的 search job 在原始分上过滤。
    """
    gate = MemoryGate(budget=MemoryBudget(max_tokens=1200), min_score=0.99)
    decision, kept = gate.apply(
        [make_hit("a.md", "全文命中", 0.01)],
        session_tags=[],
    )
    assert decision.allow and len(kept) == 1


def test_recall_gate_reasons(tmp_path: Path) -> None:
    """四种拒绝原因各来一次。"""
    gate = MemoryGate(budget=MemoryBudget(max_tokens=1200), min_score=0.2)
    assert gate.decide([]).reason == "no_hits"

    # 敏感会话：必须先于分数判定。
    sensitive = MemoryGate(
        budget=MemoryBudget(max_tokens=1200),
        min_score=0.2,
        sensitive_tags=["PII", "hr"],
    )
    assert sensitive.decide([make_hit("a.md", "x", 9.0)], session_tags=["pii"]).reason == (
        "sensitive_session"
    )
    assert sensitive.decide([make_hit("a.md", "x", 9.0)], session_tags=["hr"]).reason == (
        "sensitive_session"
    )
    assert sensitive.decide([make_hit("a.md", "x", 9.0)], session_tags=["other"]).allow

    # 全 0 分才会触发 below_min_score。
    assert gate.decide([make_hit("a.md", "x", 0.0)], session_tags=[]).reason == (
        "below_min_score"
    )

    # 预算装不下任何一条。
    tiny = MemoryGate(budget=MemoryBudget(max_tokens=1), min_score=0.0)
    assert tiny.decide([make_hit("a.md", "很长" * 200, 1.0)], session_tags=[]).reason == (
        "over_budget"
    )


def test_recall_gate_drops_low_score_tail() -> None:
    """阈值真的会裁掉尾部（``dropped_low_score`` 是 chunk_id 列表）。"""
    gate = MemoryGate(budget=MemoryBudget(max_tokens=1200), min_score=0.2)
    decision, kept = gate.apply(
        [make_hit("a.md", "全文", 1.0), make_hit("b.md", "边缘", 0.01)],
        session_tags=[],
    )
    assert decision.allow
    assert decision.dropped_low_score == ["b.md#0.01"]
    assert [hit.path for hit in kept] == ["a.md"]


def test_gate_counts_rejections_when_metrics_given() -> None:
    """拒绝原因要能进指标（``gate_rejections_*`` 两个计数）。"""
    metrics = MemoryMetrics()
    gate = MemoryGate(budget=MemoryBudget(max_tokens=1), min_score=0.0, metrics=metrics)
    gate.decide([make_hit("a.md", "很长" * 200, 1.0)], session_tags=[])
    assert metrics.snapshot()["gate_rejections_over_budget"] == 1.0


# ======================================================================
# C 写入门控
# ======================================================================
@pytest.mark.parametrize(
    ("messages", "reason"),
    [
        ([], "empty"),
        ([UserMsg("u", "一句话")], "not_enough_messages"),
        (
            [AssistantMsg("a", "我答了"), AssistantMsg("a", "我又答了")],
            "no_user_text",
        ),
        ([UserMsg("u", "嗯"), AssistantMsg("a", "好的")], "too_short"),
        (
            [
                UserMsg("u", "把部署令牌放到 ~/.secrets/token，权限 600。"),
                AssistantMsg("a", "记下了。"),
            ],
            "allowed",
        ),
    ],
)
def test_write_gate_reason_matrix(messages: list[Msg], reason: str) -> None:
    """写入门控的五个原因逐一钉住。

    Args:
        messages (`list[Msg]`): 增量。
        reason (`str`): 期望原因。
    """
    gate = MemoryWriteGate(min_messages=2, min_chars=12)
    assert gate.decide(messages).reason == reason


def test_write_gate_excludes_injected_memory_messages() -> None:
    """中间件自己注入的 ``name="memory"`` 消息不算"用户说的话"。"""
    gate = MemoryWriteGate(min_messages=2, min_chars=12)
    injected = AssistantMsg(
        name="memory",
        content=[HintBlock(hint="## 相关长期记忆\n- 某条注入")],
    )
    decision = gate.decide([*increment(), injected])
    assert decision.allow and decision.messages == 2


def test_write_gate_disabled_is_not_allowed() -> None:
    """``min_messages<=0`` 的结论是 ``disabled`` —— 与 ``allowed`` 分开，

    这样日志能区分"检查通过了"与"根本没做检查"。
    """
    gate = MemoryWriteGate(min_messages=0)
    assert gate.decide([UserMsg("u", "一句话")]).reason == "disabled"
    # 但空增量的结论仍是 empty（disabled 排在 empty 之后）。
    assert gate.decide([]).reason == "empty"


def test_write_gate_rejects_bare_msg() -> None:
    """误传单条 ``Msg`` 必须报错：``len()`` 能算，但"只有一条"会静默通过。"""
    gate = MemoryWriteGate()
    with pytest.raises(TypeError, match="消息序列"):
        gate.decide(UserMsg("u", "一句话"))  # type: ignore[arg-type]


def test_write_gate_rejects_non_int_min_chars() -> None:
    """``min_chars`` 必须是 int（``bool`` 也不行）。"""
    with pytest.raises(ValueError, match="min_chars"):
        MemoryWriteGate(min_chars=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_chars"):
        MemoryWriteGate(min_chars=True)


# ======================================================================
# D 写回：门控 / 异步 / 超时 / 排空
# ======================================================================
async def test_write_gate_blocks_auto_memory_call(tmp_path: Path) -> None:
    """被写入门控拒绝时，``auto_memory`` 一次都不该被调用。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(
        tmp_path,
        write_gate=MemoryWriteGate(min_messages=5, metrics=metrics),
        write_async=False,
    )
    calls: list[str] = []
    mw._run_job = make_job_spy(calls)  # type: ignore[method-assign]
    await mw._write_back(increment(), "s1")
    assert calls == []
    assert metrics.snapshot()["writeback_failures"] == 1.0


async def test_writeback_success_uses_created_not_success_flag(tmp_path: Path) -> None:
    """``success=True`` 不等于记忆落盘 —— 成功与否只看 ``created``/``modified``。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, write_async=False, metrics=metrics)
    mw._run_job = make_job_spy(  # type: ignore[method-assign]
        [],
        response=fake_response(success=True, created=False, modified=False),
    )
    assert await mw._do_write_back(increment(), "s2") is False
    snap = metrics.snapshot()
    assert snap["writebacks"] == 1.0 and snap["writeback_failures"] == 1.0


async def test_writeback_modified_counts_as_landed(tmp_path: Path) -> None:
    """``modified=True``（没有新卡片，只是改了旧卡）同样算成功。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, write_async=False, metrics=metrics)
    mw._run_job = make_job_spy(  # type: ignore[method-assign]
        [],
        response=fake_response(success=True, created=False, modified=True),
    )
    assert await mw._do_write_back(increment(), "s3") is True
    assert metrics.snapshot()["writeback_success_rate"] == 1.0


async def test_writeback_without_session_id_is_skipped(tmp_path: Path) -> None:
    """没有 ``session_id`` 时跳过（官方同款：warning + return）。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, metrics=metrics)
    calls: list[str] = []
    mw._run_job = make_job_spy(calls)  # type: ignore[method-assign]
    await mw._write_back(increment(), None)
    assert calls == []
    assert metrics.snapshot()["writebacks"] == 0.0


async def test_writeback_job_failure_is_counted_not_raised(tmp_path: Path) -> None:
    """``auto_memory`` 抛异常时不许影响回复，但必须记一笔失败。"""
    metrics = MemoryMetrics()

    async def boom(name: str, **kwargs: Any) -> Any:
        raise RuntimeError("ReMe 挂了")

    mw = memory_middleware(tmp_path, write_async=False, metrics=metrics)
    mw._run_job = boom  # type: ignore[method-assign]
    assert await mw._do_write_back(increment(), "s4") is False
    assert metrics.snapshot()["writeback_failures"] == 1.0


async def test_async_writeback_returns_immediately_and_is_tracked(tmp_path: Path) -> None:
    """异步写回：立刻返回、任务被持有、跑完后自动摘除。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, write_async=True, write_timeout_s=5.0, metrics=metrics)
    calls: list[str] = []
    mw._run_job = make_job_spy(calls, delay=0.2)  # type: ignore[method-assign]

    started = time.perf_counter()
    await mw._write_back(increment(), "s5")
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert elapsed_ms < 50.0, f"应当立即返回，实测 {elapsed_ms:.1f} ms"
    assert len(mw._write_tasks) == 1

    await asyncio.sleep(0.5)
    assert calls == ["auto_memory"]
    assert len(mw._write_tasks) == 0
    assert metrics.snapshot()["writebacks"] == 1.0


async def test_writeback_timeout_records_failure(tmp_path: Path) -> None:
    """超时按失败记账并放行 —— 官方没有超时，卡住的 ``auto_memory`` 会挂住回复。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, write_async=False, write_timeout_s=0.1, metrics=metrics)
    mw._run_job = make_job_spy([], delay=5.0)  # type: ignore[method-assign]

    started = time.perf_counter()
    landed = await mw._do_write_back(increment(), "s6")
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert landed is False
    assert elapsed_ms < 1000.0, f"应当 0.1s 就返回，实测 {elapsed_ms:.0f} ms"
    assert metrics.snapshot()["writeback_failures"] == 1.0


async def test_close_drains_pending_writebacks(tmp_path: Path) -> None:
    """``close()`` 必须排空在飞写入：先关 app 会把写回静默弄丢。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, write_async=True, write_timeout_s=5.0, metrics=metrics)
    drained: list[str] = []
    mw._run_job = make_job_spy(drained, delay=0.3)  # type: ignore[method-assign]

    await mw._write_back(increment(), "s7")
    assert len(mw._write_tasks) == 1

    await mw.close()
    assert drained == ["auto_memory"]
    assert len(mw._write_tasks) == 0


async def test_close_cancels_writebacks_that_exceed_the_budget(tmp_path: Path) -> None:
    """排空有上限：超过 ``write_timeout_s`` 的任务被取消，不无限等。"""
    mw = memory_middleware(tmp_path, write_async=True, write_timeout_s=0.05)
    mw._run_job = make_job_spy([], delay=10.0)  # type: ignore[method-assign]
    await mw._write_back(increment(), "s8")

    started = time.perf_counter()
    await mw.close()
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"应当很快放弃，实测 {elapsed:.2f}s"
    assert len(mw._write_tasks) == 0


# ======================================================================
# E 责任边界：注入的确切形状（用假 agent，不启动 ReMe）
# ======================================================================
class _FakeState:
    """只带 ``session_id`` 与 ``context`` 的假 ``AgentState``。"""

    def __init__(self, session_id: str) -> None:
        """初始化。

        Args:
            session_id (`str`): 会话 id。
        """
        self.session_id = session_id
        self.context: list[Any] = []


class _FakeAgent:
    """只带 ``state`` 的假 agent。

    中间件的 hook 只读 ``agent.state.session_id`` 与 ``agent.state.context``
    （``_middleware.py:296-307`` 的 ``_session_id_of``），所以鸭子类型足够。
    这样"注入形状"这条契约就能在**不启动 ReMe** 的情况下钉住 ——
    它本来就是中间件对 Agent Loop 的承诺，与检索从哪来无关。
    """

    def __init__(self, session_id: str = "sess-1") -> None:
        """初始化。

        Args:
            session_id (`str`): 会话 id。
        """
        self.state = _FakeState(session_id)


async def _drive_on_reasoning(
    mw: LongTermMemoryMiddleware,
    agent: _FakeAgent,
    hits: list[MemoryHit] | Exception,
) -> None:
    """把一次 ``on_reasoning`` 跑完（假 agent，真 hook）。

    做法是模拟 ``on_reply`` 里那个后台检索任务：把 ``_retrieve`` 换掉、
    往 ``_retrieval_tasks`` 放一个**真的在跑那个协程**的 task，
    等它就绪后再调 hook —— 这与生产路径的形状完全一致，
    只是把 ReMe 换成了固定命中。

    Args:
        mw (`LongTermMemoryMiddleware`): 中间件。
        agent (`_FakeAgent`): 假 agent。
        hits (`list[MemoryHit] | Exception`): 固定命中，或要抛出的异常。
    """

    async def _retrieve(query: str, *, session_id: str | None) -> list[MemoryHit]:
        if isinstance(hits, Exception):
            raise hits
        return hits

    mw._retrieve = _retrieve  # type: ignore[method-assign]
    mw._retrieval_tasks[agent.state.session_id] = asyncio.create_task(  # type: ignore[arg-type]
        _retrieve("查询", session_id=agent.state.session_id),
    )
    await asyncio.sleep(0.01)

    async def _pass_through(**kwargs: Any):
        if False:  # pragma: no cover - 只是让它成为 async generator
            yield None

    async for _ in mw.on_reasoning(agent, {"inputs": None}, _pass_through):
        pass


async def test_on_reasoning_injects_exactly_one_memory_msg(tmp_path: Path) -> None:
    """注入形状：一条 ``AssistantMsg(name="memory")``，只带 ``HintBlock``，落在尾部。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, metrics=metrics)
    agent = _FakeAgent()

    await _drive_on_reasoning(mw, agent, [make_hit("pref.md", "用户偏好深色主题。", 0.9)])

    injected = [m for m in agent.state.context if getattr(m, "name", None) == "memory"]
    assert len(injected) == 1
    msg = injected[0]
    assert msg.role == "assistant"
    assert [block.type for block in msg.content] == ["hint"]
    assert "用户偏好深色主题。" in msg.content[0].hint
    assert agent.state.context[-1] is msg
    assert metrics.snapshot()["injections"] == 1.0


async def test_on_reasoning_records_gated_injection_with_zero_tokens(tmp_path: Path) -> None:
    """被召回门控拒绝时：**不注入**，但记一次 ``gated=True`` 的注入决策。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(
        tmp_path,
        metrics=metrics,
        gate=MemoryGate(
            budget=MemoryBudget(max_tokens=600),
            min_score=0.2,
            sensitive_tags=["pii"],
        ),
        session_tags=("pii",),
    )
    agent = _FakeAgent()

    await _drive_on_reasoning(mw, agent, [make_hit("pref.md", "用户偏好深色主题。", 0.9)])

    assert agent.state.context == []
    snap = metrics.snapshot()
    assert snap["injections"] == 1.0 and snap["gated"] == 1.0
    assert snap["injected_tokens"] == 0.0


async def test_on_reasoning_is_silent_when_retrieval_failed(tmp_path: Path) -> None:
    """检索失败时**不注入也不抛**：记忆坏了不该让回复坏掉。"""
    metrics = MemoryMetrics()
    mw = memory_middleware(tmp_path, metrics=metrics)
    agent = _FakeAgent()

    await _drive_on_reasoning(mw, agent, RuntimeError("ReMe 不可用"))

    assert agent.state.context == []
    assert metrics.snapshot()["injections"] == 0.0


async def test_budget_counts_the_injected_object(tmp_path: Path) -> None:
    """预算计量的必须**就是**注入的那段文本，否则指标会永远高估。"""
    mw = memory_middleware(tmp_path, budget=MemoryBudget(max_tokens=600))
    hits = [make_hit("pref.md", "用户偏好深色主题的图表。" * 5, 0.9)]
    rendered, tokens = mw._render_hits(hits)
    assert isinstance(rendered, str)
    assert tokens == estimate_tokens_heuristic(rendered)
    assert tokens <= 600


async def test_no_budget_renders_plain_texts(tmp_path: Path) -> None:
    """没有预算时退回官方的 ``list[str]`` 形态（交给 ``- bullet`` 拼装）。"""
    mw = memory_middleware(tmp_path)
    rendered, tokens = mw._render_hits([make_hit("a.md", "正文一", 1.0)])
    assert rendered == ["正文一"]
    assert tokens == estimate_tokens_heuristic("正文一")


# ======================================================================
# F 多租户与指标
# ======================================================================
def test_tenant_workspaces_are_physically_isolated(tmp_path: Path) -> None:
    """两个租户的工作区根互不为前缀。"""
    router = TenantRouter(root=tmp_path / "tenants")
    acme = router.ensure_workspace("acme")
    globex = router.ensure_workspace("globex")
    assert acme.root != globex.root
    router.assert_isolated("acme", "globex")
    assert set(router.list_tenants()) == {"acme", "globex"}


@pytest.mark.parametrize(
    "bad",
    ["..", "a/b", "/abs", "", ".hidden", "x" * 65, "a b", "-lead"],
)
def test_tenant_id_rejects_unsafe_names(tmp_path: Path, bad: str) -> None:
    """越界名一律拒（上跳、分隔符、绝对路径、空串、空格、超长）。

    Args:
        tmp_path (`Path`): 临时目录。
        bad (`str`): 非法租户名。
    """
    router = TenantRouter(root=tmp_path / "tenants")
    with pytest.raises(TenantError):
        router.validate_tenant_id(bad)


def test_tenant_id_accepts_safe_names(tmp_path: Path) -> None:
    """合法字符集：字母数字开头，可含 ``.`` ``-`` ``_``。"""
    router = TenantRouter(root=tmp_path / "tenants")
    assert router.validate_tenant_id("acme-cn.dev_1") == "acme-cn.dev_1"


def test_metrics_hit_rate_and_gated_rate() -> None:
    """命中率 / 门控率 / 写回成功率的定义要钉住。"""
    metrics = MemoryMetrics()
    metrics.record_search(session_id="a", hits=3, elapsed_ms=1.0)
    metrics.record_search(session_id="a", hits=0, elapsed_ms=1.0)
    metrics.record_injection(session_id="a", tokens=10, gated=False)
    metrics.record_injection(session_id="a", tokens=0, gated=True)
    metrics.record_writeback(session_id="a", ok=True)
    metrics.record_writeback(session_id="a", ok=False)

    snap = metrics.snapshot()
    assert snap["hit_rate"] == 0.5
    assert snap["gated_rate"] == 0.5
    assert snap["injected_tokens"] == 10.0
    assert snap["writeback_success_rate"] == 0.5
    assert snap["sessions"] == 1.0


def test_metrics_session_snapshot_isolates_tenants() -> None:
    """会话维度可分开看 —— 多租户下这就是按租户对账。"""
    metrics = MemoryMetrics()
    metrics.record_search(session_id="acme", hits=2, elapsed_ms=1.0)
    metrics.record_search(session_id="globex", hits=5, elapsed_ms=1.0)
    assert metrics.session_snapshot("acme")["hits"] == 2.0
    assert metrics.session_snapshot("globex")["hits"] == 5.0
    assert metrics.session_snapshot("nobody")["hits"] == 0.0


# ======================================================================
# G 与真 ReMe 合体（嵌入式，0 次模型调用）
# ======================================================================
#: 语料：一条用户偏好。
CORPUS: str = """---
name: 绘图偏好
description: 用户对 matplotlib 图表的偏好
memory_tags: [pref]
---

# 绘图偏好

用户偏好**深色主题**的 matplotlib 图表，坐标轴标签一律用中文。
"""


def build_from_profile(tmp_path: Path, workspace_root: Path, **params: Any):
    """走**生产装配路径**造中间件（而不是直接 ``LongTermMemoryMiddleware(...)``）。

    为什么这几条 e2e 测试必须走 builder：``build_memory_middleware`` 会调用
    ``_export_llm_env(ctx.environ)``，把 ``Settings`` 里的
    ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL_NAME`` 补进
    ``os.environ`` —— 少了这一步，嵌入式 app 会在 ``as_llm`` 建 client 时
    抛 ``Missing credentials``。这条"必须走 builder"本身就是本讲的一个坑，
    测试把它钉住：直接构造的中间件在只有 ``OPENAI_*`` 的环境里是起不来的。

    Args:
        tmp_path (`Path`): 临时目录（用于 ``workspace_dir`` 兜底）。
        workspace_root (`Path`): 要指向的工作区根。
        **params (`Any`): 额外的 Profile 参数。

    Returns:
        `Any`: ``build_memory_middleware`` 的返回值。
    """
    from harness_kit.settings import Settings

    settings = Settings.from_env()
    ctx = SimpleNamespace(
        settings=None,
        profile=None,
        environ=settings.environ_overlay(),
    )
    return build_memory_middleware(
        spec_of(workspace_dir=str(workspace_root), mode="static_control", top_k=3, **params),
        ctx=ctx,
    )


async def test_end_to_end_retrieval_reaches_the_search_job(tmp_path: Path) -> None:
    """真嵌入式 ReMe：``min_score`` / ``tool_context_id`` 真的进了 search job。

    这是本讲最重要的**回归测试**：官方的 ``_search``
    （``_middleware.py:475-487``）只传 ``query`` 与 ``limit``，
    于是 ``min_score`` 永远是默认 0.0、``tool_context_id`` 永远是 ``None``。
    这个断言把"harness 确实补上了这两个参数"钉死在 job 入参上。
    """
    workspace = ReMeWorkspace(root=tmp_path / "reme")
    workspace.ensure()
    client = MemoryClient(
        HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None)
        .with_jobs("search", "reindex")
        .build(),
    )
    await client.start()
    try:
        ingestor = MemoryIngestor(client, workspace=workspace, chunker="markdown")
        await ingestor.add_text(CORPUS, name="pref", tags=[])
        await client.run_job("reindex")

        mw = build_from_profile(
            tmp_path,
            tmp_path / "reme",
            min_score=0.2,
            tool_context_id="test-bucket",
        )
        calls: list[tuple[str, dict[str, Any]]] = []
        original = mw._run_job

        async def spy(name: str, **kwargs: Any) -> Any:
            calls.append((name, dict(kwargs)))
            return await original(name, **kwargs)

        mw._run_job = spy  # type: ignore[method-assign]
        try:
            hits = await mw._retrieve("画图用什么主题？", session_id="sess-e2e")
        finally:
            await mw.close()

        searches = [kwargs for name, kwargs in calls if name == "search"]
        assert searches, f"没有发生 search 调用：{[n for n, _ in calls]}"
        assert searches[0]["min_score"] == 0.2
        assert searches[0]["tool_context_id"] == "test-bucket"
        assert searches[0]["limit"] == 3
        assert hits, "语料已入库并 reindex，应当至少命中一条"
        assert all(hit.path for hit in hits)
    finally:
        await client.aclose()


async def test_workspace_dir_mismatch_yields_no_hits(tmp_path: Path) -> None:
    """中间件指向的工作区与语料所在工作区不一致时，检索**必然**空手而归。

    这条测试把"``workspace_dir`` 必须与写入侧一致"这条约束变成可执行的：
    配错了不会报错，只会静默地永远 0 命中。
    """
    workspace = ReMeWorkspace(root=tmp_path / "written")
    workspace.ensure()
    client = MemoryClient(
        HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None)
        .with_jobs("search", "reindex")
        .build(),
    )
    await client.start()
    try:
        ingestor = MemoryIngestor(client, workspace=workspace, chunker="markdown")
        await ingestor.add_text(CORPUS, name="pref", tags=[])
        await client.run_job("reindex")

        # 故意指向另一个目录。
        mw = build_from_profile(tmp_path, tmp_path / "elsewhere")
        try:
            hits = await mw._retrieve("画图用什么主题？", session_id="sess-mismatch")
        finally:
            await mw.close()

        assert hits == []
    finally:
        await client.aclose()
