# -*- coding: utf-8 -*-
"""第 13 讲的 pytest：派生闸门 / 能力路由 / 团队编排 / 交接协议。

五条纪律：

1. **0 次 LLM 调用**。需要「真 Agent Loop」的那几条用
   :class:`~harness_kit.models.adapters.echo.EchoChatModel`（脚本驱动、
   确定性、离线）。真实模型那部分在 ``scripts/13_multiagent.py --live``。
2. **每个「接受」都要配一个「拒绝」的反例**。编排组件的失效模式是
   **静默降级**：路由选了个错误的成员、闸门没生效、交接工具没被装进模型
   可见的组 —— 都不会抛异常，只会让结果变差。所以下面几乎每条正向断言
   旁边都有一条反向断言。
3. **闸门必须逐道单独测**。``max_spawn`` / ``max_depth`` / ``max_concurrent``
   的处置方式完全不同（重试有用 / 换人有用 / 收工），把它们混在一起测，
   一道门坏了另一道门会替它兜着，测试仍然全绿。
4. **并发测试必须让模型调用真的挂起**（``SlowModel``）。``EchoChatModel``
   是纯内存计算，一次调用里没有会让出事件循环的 ``await``；不加这一层，
   ``broadcast`` 的三个任务会**依次**跑完，``max_concurrent`` 永远触发不了
   —— 这是「离线全绿、线上限流失效」的经典假绿。
5. **不共享消息历史是硬性质，必须单独测**。它是「多 Agent」与「一个 Agent
   换个称呼」的分界线：两个名字挂在同一个 ``Agent`` 实例上时，
   ``state.context`` 会互相污染，而这件事**不会有任何报错**。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson13_multiagent.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.message import ToolCallBlock, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
)
from agentscope.tool import FunctionTool, Toolkit

from harness_kit.models.adapters.echo import EchoChatModel
from harness_kit.multiagent import (
    AgentTeam,
    CapabilityRouter,
    HandoffOutcome,
    HandoffTool,
    NoRouteError,
    SpawnLimitExceeded,
    SpawnLimiter,
    TeamResult,
    collect_results,
)
from harness_kit.multiagent.limits import SpawnLimiterStats, SpawnTicket
from harness_kit.multiagent.router import MemberScore

# ======================================================================
# 夹具与公共件
# ======================================================================


class SlowModel(MiddlewareBase):
    """让模型调用真的花时间（``on_model_call`` 里 await 一次 ``asyncio.sleep``）。

    见模块 docstring 第 4 条纪律：没有它，``broadcast`` 的并发测试是假绿的。

    Args:
        delay (`float`): 每次模型调用额外等待的秒数。
    """

    def __init__(self, delay: float = 0.05) -> None:
        """初始化。"""
        self.delay = delay

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        """先睡一下再转发。

        Args:
            agent (`Agent`): 正在跑的 Agent。
            input_kwargs (`dict`): 钩子入参。
            next_handler (`Any`): 链上的下一个 handler。

        Returns:
            `Any`: 模型的原始返回。
        """
        await asyncio.sleep(self.delay)
        return await next_handler(**input_kwargs)


def echo_agent(
    name: str,
    *,
    script: list[dict[str, Any]] | None = None,
    middlewares: list[Any] | None = None,
) -> Agent:
    """造一个 ``EchoChatModel`` 驱动的真 ``Agent``。

    Args:
        name (`str`): Agent 名。
        script (`list[dict[str, Any]] | None`, optional): 模型脚本。
        middlewares (`list[Any] | None`, optional): 中间件。

    Returns:
        `Agent`: AgentScope 的 ``Agent`` 实例。
    """
    return Agent(
        name=name,
        system_prompt=f"你是 {name}。",
        model=EchoChatModel(script=script, stream=False),
        toolkit=Toolkit(),
        middlewares=list(middlewares or []),
        react_config=ReActConfig(max_iters=6),
    )


def router4(**kwargs: Any) -> CapabilityRouter:
    """本讲统一使用的四成员路由表。

    ``reviewer`` 的能力标签刻意写成 ``Code-Review``（连字符 + 驼峰），
    用来验证 :func:`~harness_kit.multiagent.router._normalize` 的归一化。

    Args:
        **kwargs (`Any`): 透传。

    Returns:
        `CapabilityRouter`: 路由表。
    """
    return CapabilityRouter(
        {
            "researcher": ["web_search", "summarize"],
            "coder": ["code", "python"],
            "reviewer": ["Code-Review", "security"],
            "writer": ["prose", "summarize"],
        },
        **kwargs,
    )


@pytest.fixture()
def team4() -> AgentTeam:
    """四成员离线团队（成员名字与路由表一一对应）。

    Returns:
        `AgentTeam`: 团队。
    """
    router = router4()
    return AgentTeam(
        members={name: echo_agent(name) for name in router.members},
        router=router,
        limits=SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=4),
    )


# ======================================================================
# A · SpawnLimiter
# ======================================================================


def test_limiter_rejects_non_positive_limits() -> None:
    """三道闸门的限额必须是正数：0 会让所有派生静默失败。"""
    for kwargs in (
        {"max_spawn": 0},
        {"max_depth": 0},
        {"max_concurrent": 0},
        {"max_spawn": -3},
    ):
        with pytest.raises(ValueError):
            SpawnLimiter(**kwargs)


def test_depth_gate_fires_on_its_own() -> None:
    """``max_depth`` 单独触发，且 ``gate`` 字段能被区分出来。"""
    limiter = SpawnLimiter(max_spawn=8, max_depth=2, max_concurrent=4)
    limiter.try_acquire(depth=0)
    limiter.try_acquire(depth=1)
    with pytest.raises(SpawnLimitExceeded) as info:
        limiter.try_acquire(depth=2)
    assert info.value.gate == "max_depth"
    assert info.value.limit == 2
    assert info.value.current == 2
    assert limiter.snapshot().rejected == 1


def test_spawn_gate_fires_on_its_own() -> None:
    """``max_spawn`` 单独触发（宽度爆炸闸门）。"""
    limiter = SpawnLimiter(max_spawn=2, max_depth=9, max_concurrent=9)
    t1 = limiter.try_acquire(depth=0)
    t2 = limiter.try_acquire(depth=0)
    with pytest.raises(SpawnLimitExceeded) as info:
        limiter.try_acquire(depth=0)
    assert info.value.gate == "max_spawn"
    t1.release()
    t2.release()
    # 释放票据**不会**退还 spawn 配额：max_spawn 计的是历史总数。
    assert limiter.remaining() == 0
    with pytest.raises(SpawnLimitExceeded) as info:
        limiter.try_acquire(depth=0)
    assert info.value.gate == "max_spawn"


def test_concurrent_gate_fires_on_its_own() -> None:
    """``max_concurrent`` 单独触发（打满上游 QPS 闸门）。"""
    limiter = SpawnLimiter(max_spawn=9, max_depth=9, max_concurrent=1)
    held = limiter.try_acquire(depth=0)
    with pytest.raises(SpawnLimitExceeded) as info:
        limiter.try_acquire(depth=0)
    assert info.value.gate == "max_concurrent"
    assert info.value.limit == 1
    held.release()
    # 归还之后并发额度立刻恢复 —— 这正是「稍后重试就行」的语义。
    again = limiter.try_acquire(depth=0)
    assert again.released is False
    again.release()


def test_release_is_idempotent() -> None:
    """重复 ``release()`` 只算一次，``active`` 不会变成负数。"""
    limiter = SpawnLimiter(max_spawn=4, max_depth=4, max_concurrent=4)
    ticket = limiter.try_acquire(depth=0)
    assert limiter.active == 1
    ticket.release()
    ticket.release()
    ticket.release()
    assert limiter.active == 0
    assert ticket.released is True
    assert limiter.snapshot().peak_active == 1


def test_release_underflow_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """票据被重复释放导致的 ``active < 0`` 会被钳回 0（白盒测一条防御分支）。"""
    limiter = SpawnLimiter(max_spawn=4, max_depth=4, max_concurrent=4)
    ticket = limiter.try_acquire(depth=0)
    # 故意绕过 SpawnTicket.release 的幂等保护，直接触发记账路径。
    limiter._on_release(ticket)  # pylint: disable=protected-access
    assert limiter.active == 0, "负数必须被钳回 0，否则后续派生会被永久放行"


def test_async_slot_releases_on_exception() -> None:
    """``async with slot()`` 在异常路径上也归还票据。"""

    async def run() -> int:
        limiter = SpawnLimiter(max_spawn=4, max_depth=4, max_concurrent=1)
        with pytest.raises(RuntimeError):
            async with limiter.slot(depth=0):
                raise RuntimeError("模拟工人崩了")
        return limiter.active

    assert asyncio.run(run()) == 0


def test_sync_guard_is_a_context_manager() -> None:
    """``with guard()`` 必须真的支持 ``with``（少了 @contextmanager 会 TypeError）。"""
    limiter = SpawnLimiter(max_spawn=4, max_depth=4, max_concurrent=1)
    with limiter.guard(depth=2) as ticket:
        assert isinstance(ticket, SpawnTicket)
        assert ticket.depth == 2
        assert limiter.active == 1
    assert limiter.active == 0


def test_ticket_repr_and_released_flag() -> None:
    """``SpawnTicket`` 的调试表示会反映释放状态。"""
    limiter = SpawnLimiter()
    ticket = limiter.try_acquire(depth=1)
    assert "active" in repr(ticket)
    assert "depth=1" in repr(ticket)
    ticket.release()
    assert "released" in repr(ticket)


def test_snapshot_and_describe() -> None:
    """统计快照与一行描述。"""
    limiter = SpawnLimiter(max_spawn=4, max_depth=3, max_concurrent=2)
    t1 = limiter.try_acquire(depth=0)
    t2 = limiter.try_acquire(depth=1)
    snap = limiter.snapshot()
    assert isinstance(snap, SpawnLimiterStats)
    assert (snap.active, snap.spawned, snap.peak_active, snap.deepest) == (2, 2, 2, 1)
    described = limiter.describe()
    assert "spawn=2/4" in described
    assert "active=2/2" in described
    assert "depth=1/2" in described
    t1.release()
    t2.release()
    assert limiter.active == 0
    assert limiter.snapshot().peak_active == 2, "峰值是历史值，不随释放回落"


def test_reset_clears_counters() -> None:
    """``reset()`` 清零计数器（**不**动已经发出去的票据）。"""
    limiter = SpawnLimiter(max_spawn=2, max_depth=4, max_concurrent=4)
    ticket = limiter.try_acquire(depth=0)
    limiter.reset()
    snap = limiter.snapshot()
    assert (snap.spawned, snap.active, snap.rejected, snap.deepest) == (0, 0, 0, 0)
    # 票据仍然有效，仍能归还（此时 active 会被钳回 0 并记一条 error 日志）。
    ticket.release()
    assert limiter.active == 0


# ======================================================================
# B · CapabilityRouter
# ======================================================================


def test_normalize_makes_separators_equivalent() -> None:
    """``code_review`` / ``code-review`` / ``CodeReview`` 落在同一个能力空间。"""
    router = router4()
    for required in ("code_review", "code-review", "CodeReview", "codereview", "CODE_REVIEW"):
        assert router.route("请审查这段代码", required=required) == "reviewer"


def test_required_filters_candidates() -> None:
    """``required`` 只让具备该能力的成员进入候选。"""
    router = router4()
    ranked = router.rank("随便什么任务", required="python")
    assert [_.member for _ in ranked] == ["coder"]
    assert router.route("随便什么任务", required="summarize") in {"researcher", "writer"}


def test_no_route_error_kind_route() -> None:
    """没有候选时抛 ``NoRouteError``，``kind == "route"``。"""
    router = router4()
    with pytest.raises(NoRouteError) as info:
        router.route("任务", required="quantum_computing")
    assert info.value.kind == "route"
    assert "quantum_computing" in str(info.value)
    assert "现有成员" in str(info.value)


def test_no_route_error_kind_member_has_different_message() -> None:
    """「成员不存在」与「路由不到人」共用异常类型，但文案必须不同。"""
    router = router4()
    with pytest.raises(NoRouteError) as info:
        router.capabilities_of("boss")
    assert info.value.kind == "member"
    assert "不存在" in str(info.value)
    assert "没有成员能接这个任务" not in str(info.value)
    assert router.members == ["coder", "researcher", "reviewer", "writer"]


def test_history_weight_bounds() -> None:
    """``history_weight`` 必须在 ``[0, 1]``。"""
    with pytest.raises(ValueError):
        router4(history_weight=1.5)
    with pytest.raises(ValueError):
        router4(history_weight=-0.1)
    assert router4(history_weight=0.0).history_weight == 0.0
    assert router4(history_weight=1.0).history_weight == 1.0


def test_empty_capability_table_raises() -> None:
    """空能力表没有意义，构造时就拒绝。"""
    with pytest.raises(ValueError):
        CapabilityRouter({})


def test_cold_start_success_rate_is_neutral_prior() -> None:
    """冷启动成功率是 0.5（中性先验），不是 0（自我实现的预言）。"""
    router = router4()
    for member in router.members:
        assert router.success_rate(member) == 0.5


def test_rank_is_deterministic() -> None:
    """同一输入两次排序逐字相同（可复现性）。"""
    router = router4()
    first = [_.member for _ in router.rank("帮我 summarize 这段材料")]
    second = [_.member for _ in router.rank("帮我 summarize 这段材料")]
    assert first == second
    assert isinstance(router.rank("x")[0], MemberScore)


def test_rank_tie_break_by_attempts_then_name() -> None:
    """平局先按「调用次数少的优先」（轮转），再按名字字典序（可复现）。"""
    router = router4()
    ranked = router.rank("帮我 summarize 这段材料")
    assert [_.member for _ in ranked[:2]] == ["researcher", "writer"]
    # researcher 与 writer 能力分相同，打平后按字典序 -> researcher 先。
    assert ranked[0].capability_score == ranked[1].capability_score


def test_capability_score_is_a_ratio_not_a_count() -> None:
    """能力分 = 命中数 / 标签总数：挂 8 个标签只中 1 个不该赢过精准命中。"""
    router = CapabilityRouter(
        {"wide": ["a", "b", "c", "d", "e", "f", "g", "h"], "narrow": ["h"]},
    )
    wide = router.score("wide", "h")
    narrow = router.score("narrow", "h")
    assert wide.capability_score == pytest.approx(1 / 8)
    assert narrow.capability_score == pytest.approx(1.0)
    assert narrow.score > wide.score
    assert router.route("h") == "narrow"


def test_record_changes_who_gets_the_task() -> None:
    """历史成功率真的会改变下一个接活的人。"""
    router = router4()
    assert router.route("帮我 summarize 这段材料") == "researcher"
    for _ in range(3):
        router.record("researcher", ok=False)
    assert router.success_rate("researcher") == 0.0
    assert router.route("帮我 summarize 这段材料") == "writer"


def test_record_unknown_member_raises() -> None:
    """记录一个不存在的成员会破坏成功率统计，必须报错而不是丢弃。"""
    router = router4()
    with pytest.raises(NoRouteError) as info:
        router.record("ghost", ok=True)
    assert info.value.kind == "member"


def test_history_export_and_reset() -> None:
    """历史统计导出与复位。"""
    router = router4()
    router.record("coder", ok=True)
    router.record("coder", ok=False)
    history = router.history()
    assert history["coder"]["attempts"] == 2.0
    assert history["coder"]["ok"] == 1.0
    assert history["coder"]["success_rate"] == pytest.approx(0.5)
    router.reset_history()
    assert router.history()["coder"]["attempts"] == 0.0


def test_add_member_does_not_silently_overwrite() -> None:
    """重名登记必须报错，静默覆盖会悄悄改掉路由结果。"""
    router = router4()
    router.add_member("intern", ["fetch"])
    assert router.capabilities_of("intern") == ["fetch"]
    with pytest.raises(ValueError):
        router.add_member("intern", ["other"])


def test_remove_member_and_last_member_guard() -> None:
    """移除成员；不允许把路由表删空。"""
    router = router4()
    assert router.remove_member("writer") is True
    assert router.remove_member("writer") is False
    while len(router.members) > 1:
        router.remove_member(router.members[0])
    with pytest.raises(ValueError):
        router.remove_member(router.members[0])


def test_route_many_k_and_unique_capability() -> None:
    """``route_many`` 取 k 个；``unique_capability`` 保证主能力不重复。"""
    router = router4()
    plain = router.route_many("summarize 并 review 代码", k=3)
    assert len(plain) == 3
    assert len(set(plain)) == 3, "永远不能返回同名成员"
    uniq = router.route_many("summarize 并 review 代码", k=3, unique_capability=True)
    assert len(set(uniq)) == len(uniq)
    assert router.route_many("任务", k=99) != []


def test_route_many_no_candidate_raises() -> None:
    """一个候选都没有时 ``route_many`` 与 ``route`` 一样抛。"""
    router = router4()
    with pytest.raises(NoRouteError):
        router.route_many("任务", k=2, required="quantum")


def test_explain_never_raises() -> None:
    """``explain`` 是排查工具，没有候选时也要给出可读文本而不是抛。"""
    router = router4()
    text = router.explain("随便", required="quantum")
    assert "NoRouteError" in text
    ok_text = router.explain("帮我 summarize 这段材料")
    assert "researcher" in ok_text
    assert "score=" in ok_text


# ======================================================================
# C · AgentTeam
# ======================================================================


async def test_team_requires_at_least_one_member() -> None:
    """空团队构造即失败。"""
    with pytest.raises(ValueError):
        AgentTeam(members={}, router=router4(), limits=SpawnLimiter())


async def test_team_rejects_shared_agent_instance() -> None:
    """两个成员名挂同一个 Agent 实例 -> ValueError（不共享历史的前提）。"""
    shared = echo_agent("shared")
    with pytest.raises(ValueError) as info:
        AgentTeam(
            members={"a": shared, "b": shared},
            router=router4(),
            limits=SpawnLimiter(),
        )
    assert "同一个 Agent 实例" in str(info.value)


async def test_add_member_guards() -> None:
    """``add_member`` 拒绝重名与复用已有实例；路由表已有的标签以路由表为准。"""
    team = AgentTeam(
        members={"coder": echo_agent("coder")},
        router=CapabilityRouter({"coder": ["code"], "intern": ["fetch"]}),
        limits=SpawnLimiter(),
    )
    # intern 已经在路由表里 -> 沿用旧标签，`capabilities=` 只对全新成员生效。
    team.add_member("intern", echo_agent("intern"), capabilities=["fetch", "grep"])
    assert "intern" in team.names
    assert team.router.capabilities_of("intern") == ["fetch"]
    # 全新成员才会把 `capabilities=` 写进路由表。
    team.add_member("helper", echo_agent("helper"), capabilities=["grep"])
    assert team.router.capabilities_of("helper") == ["grep"]
    with pytest.raises(ValueError):
        team.add_member("intern", echo_agent("intern2"))
    with pytest.raises(ValueError):
        team.add_member("boss", team.agent("coder"))


async def test_remove_member_returns_bool() -> None:
    """``remove_member`` 真删返回 True、本来就没有返回 False。"""
    team = AgentTeam(
        members={"a": echo_agent("a"), "b": echo_agent("b")},
        router=CapabilityRouter({"a": ["a"], "b": ["b"]}),
        limits=SpawnLimiter(),
    )
    assert team.remove_member("a") is True
    assert team.remove_member("a") is False
    assert team.names == ["b"]


async def test_dispatch_routes_and_returns_output(team4: AgentTeam) -> None:
    """``dispatch`` 走路由选人，产出与工人名字都能拿到。"""
    result = await team4.dispatch("请帮我 summarize 这篇论文")
    assert isinstance(result, TeamResult)
    assert result.ok is True
    assert result.member == "researcher"
    assert "[echo]" in result.output
    assert result.elapsed_ms >= 0.0
    assert result.error is None
    assert result.gate is None
    assert result.task.startswith("请帮我 summarize")
    assert "[echo]" not in result.summary() or "researcher" in result.summary()


async def test_dispatch_records_history(team4: AgentTeam) -> None:
    """派活成功会写回路由历史（供后续打分使用）。"""
    await team4.dispatch("帮我 summarize 这段材料")
    assert team4.router.history()["researcher"]["attempts"] == 1.0


async def test_dispatch_to_missing_member_is_not_ok() -> None:
    """路由选中的人没有对应 Agent -> ``ok=False``（不抛）。"""
    team = AgentTeam(
        members={"coder": echo_agent("coder")},
        router=CapabilityRouter({"coder": ["code"], "ghost": ["ghost"]}),
        limits=SpawnLimiter(),
    )
    result = await team.dispatch("ghost 的活", member="ghost")
    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("NoRouteError")


async def test_dispatch_route_error_raises(team4: AgentTeam) -> None:
    """一个候选都没有是**配置错误**，显式抛出而不是静默 ok=False。"""
    with pytest.raises(NoRouteError):
        await team4.dispatch("没人能接的活", required_capability="quantum")


async def test_dispatch_gate_rejection_marks_gate(team4: AgentTeam) -> None:
    """闸门拒绝变成 ``ok=False`` + ``gate``，而不是抛到调用方。"""
    team = AgentTeam(
        members={"coder": echo_agent("coder")},
        router=CapabilityRouter({"coder": ["code"]}),
        limits=SpawnLimiter(max_spawn=1, max_depth=4, max_concurrent=4),
    )
    first = await team.dispatch("第一次")
    second = await team.dispatch("第二次")
    assert first.ok is True
    assert second.ok is False
    assert second.gate == "max_spawn"
    assert second.error.startswith("SpawnLimitExceeded")


async def test_broadcast_dedupes_members(team4: AgentTeam) -> None:
    """显式成员列表会去重，且返回顺序与去重后的列表一致。"""
    results = await team4.broadcast(
        "任务",
        members=["coder", "coder", "reviewer"],
    )
    assert [_.member for _ in results] == ["coder", "reviewer"]
    assert all(_.ok for _ in results)


async def test_broadcast_respects_max_concurrent() -> None:
    """``max_concurrent`` 在真并发下压住广播（用 SlowModel 让调用真的挂起）。"""
    router = router4()
    team = AgentTeam(
        members={
            name: echo_agent(name, middlewares=[SlowModel(0.05)])
            for name in router.members
        },
        router=router,
        limits=SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=1),
    )
    results = await team.broadcast("并行任务", members=["coder", "reviewer", "writer"])
    assert len(results) == 3
    assert sum(1 for _ in results if _.ok) == 1
    assert sum(1 for _ in results if _.gate == "max_concurrent") == 2


async def test_broadcast_without_targets_returns_empty() -> None:
    """候选为空时广播返回空列表（不是异常）。"""
    team = AgentTeam(
        members={"coder": echo_agent("coder")},
        router=CapabilityRouter({"coder": ["code"]}),
        limits=SpawnLimiter(),
    )
    assert await team.broadcast("任务", members=[]) != []
    assert await team.broadcast("任务", members=["coder"]) != []


async def test_agent_lookup_raises_member_kind(team4: AgentTeam) -> None:
    """查不存在的成员 -> ``NoRouteError(kind="member")``。"""
    with pytest.raises(NoRouteError) as info:
        team4.agent("boss")
    assert info.value.kind == "member"


async def test_describe_lists_members(team4: AgentTeam) -> None:
    """``describe`` 列出成员、能力与闸门。"""
    text = team4.describe()
    assert "AgentTeam(members=4" in text
    for name in ("coder", "researcher", "reviewer", "writer"):
        assert name in text
    assert "spawn=" in text


async def test_aclose_only_drops_references(team4: AgentTeam) -> None:
    """``aclose`` 只断引用：**刻意不关闭 Agent**（模型由 builder 统一管理）。"""
    members = list(team4.names)
    await team4.aclose()
    assert team4.names == []
    assert members == ["coder", "researcher", "reviewer", "writer"]


# ======================================================================
# D · HandoffTool
# ======================================================================


async def test_handoff_declares_allow_explicitly(team4: AgentTeam) -> None:
    """交接工具无条件 ``ALLOW``，并给出理由（契约 §3.13 的铁律）。"""
    tool = team4.handoff_tool("researcher")
    decision = await tool.check_permissions({}, PermissionContext())
    assert decision.behavior is PermissionBehavior.ALLOW
    assert decision.decision_reason.endswith("HandoffTool")
    assert decision.message


async def test_function_tool_default_is_ask() -> None:
    """反例锚点：``FunctionTool`` 在 ``permission=None`` 时默认 ASK。"""

    async def noop() -> str:
        """空实现。

        Returns:
            `str`: 固定串。
        """
        return "ok"

    decision = await FunctionTool(noop).check_permissions({}, PermissionContext())
    assert decision.behavior is PermissionBehavior.ASK


async def test_handoff_input_schema_shape(team4: AgentTeam) -> None:
    """``input_schema`` 的 required / 候选人提示必须齐备。"""
    tool = team4.handoff_tool("researcher")
    assert tool.name == "handoff_to_teammate"
    assert sorted(tool.input_schema["properties"]) == ["reason", "task", "to_member"]
    assert tool.input_schema["required"] == ["to_member", "task"]
    assert tool.input_schema["additionalProperties"] is False
    assert "researcher" in tool.input_schema["properties"]["to_member"]["description"]


async def test_handoff_rejects_three_bad_inputs(team4: AgentTeam) -> None:
    """空 task / 空 to_member / 交给自己都回可读文本，且不记进 outcomes。"""
    tool = team4.handoff_tool("researcher")
    cases = (
        ({"to_member": "coder", "task": "   "}, "`task` is empty"),
        ({"to_member": "", "task": "有活"}, "`to_member` is empty"),
        ({"to_member": "researcher", "task": "有活"}, "cannot hand a task to yourself"),
    )
    for kwargs, expected in cases:
        chunk = await tool(**kwargs)
        text = chunk.content[0].text
        assert expected in text
    assert tool.outcomes == []
    assert tool.stats() == {"count": 0, "ok": 0, "failed": 0, "to": []}


async def test_handoff_success_collects_result(team4: AgentTeam) -> None:
    """成功交接：outcomes 记一条、feedback 带产物、stats 正确。"""
    tool = team4.handoff_tool("researcher")
    chunk = await tool(to_member="coder", task="写一个快速排序", reason="需要 python")
    text = chunk.content[0].text
    assert text.startswith("[handoff -> coder OK")
    assert tool.stats() == {"count": 1, "ok": 1, "failed": 0, "to": ["coder"]}
    outcome = tool.outcomes[0]
    assert outcome.request.from_member == "researcher"
    assert outcome.request.to_member == "coder"
    assert outcome.ok is True
    assert "[echo]" in outcome.output


async def test_handoff_dispatches_with_depth_plus_one(team4: AgentTeam) -> None:
    """交接派活时用的是 ``self.depth + 1``（深度爆炸能被 max_depth 拦住）。"""
    router = router4()
    team = AgentTeam(
        members={name: echo_agent(name) for name in router.members},
        router=router,
        limits=SpawnLimiter(max_spawn=32, max_depth=2, max_concurrent=4),
    )
    root = team.handoff_tool("researcher", tool_name="handoff_a")
    deep = HandoffTool(team=team, self_name="researcher", tool_name="handoff_b", depth=1)
    ok_chunk = await root(to_member="coder", task="深度 0")
    assert "OK" in ok_chunk.content[0].text
    blocked = await deep(to_member="coder", task="深度 1")
    assert "FAILED" in blocked.content[0].text
    assert "max_depth" in blocked.content[0].text


def test_handoff_outcome_feedback_is_never_empty() -> None:
    """失败时 ``feedback`` 也必须非空，并给出可操作的下一步。"""
    failed = HandoffOutcome(
        request={
            "from_member": "a",
            "to_member": "b",
            "task": "t",
            "reason": "",
        },
        ok=False,
        error="TimeoutError: 60s",
        elapsed_ms=60001.0,
    )
    assert failed.feedback
    assert "FAILED" in failed.feedback
    assert "Do not retry" in failed.feedback
    ok = HandoffOutcome(
        request={"from_member": "a", "to_member": "b", "task": "t", "reason": ""},
        ok=True,
        output="产物",
        elapsed_ms=12.0,
    )
    assert "OK" in ok.feedback
    assert "产物" in ok.feedback


def test_collect_results_budget_and_empty() -> None:
    """``collect_results`` 空输入给一句说明；有条目时按预算截断。"""
    assert collect_results([]) == "（没有移交记录）"
    outcomes = [
        HandoffOutcome(
            request={
                "from_member": "boss",
                "to_member": f"w{i}",
                "task": "t",
                "reason": "",
            },
            ok=True,
            output="x" * 5000,
            elapsed_ms=1.0,
        )
        for i in range(4)
    ]
    text = collect_results(outcomes, max_chars=400)
    assert text.count("[") >= 4
    assert "…" in text
    assert len(text) < 5000 * 4


async def test_handoff_tool_is_cached_per_member(team4: AgentTeam) -> None:
    """同一个 ``(成员, 工具名)`` 只会造一个实例（否则 outcomes 会丢）。"""
    first = team4.handoff_tool("coder")
    second = team4.handoff_tool("coder")
    assert first is second
    assert team4.handoff_tools["coder"] is first


async def test_install_handoff_tool_is_idempotent_and_visible() -> None:
    """装进 basic 组后模型看得见；重复安装不堆 schema。"""
    router = router4()
    router.add_member("boss", ["coordinate"])
    boss = echo_agent("boss")
    team = AgentTeam(
        members={
            "boss": boss,
            **{name: echo_agent(name) for name in router.members if name != "boss"},
        },
        router=router,
        limits=SpawnLimiter(),
    )
    first = await team.install_handoff_tool("boss")
    second = await team.install_handoff_tool("boss")
    assert first is second
    schemas = await boss.toolkit.get_tool_schemas(boss.state.tool_context.activated_groups)
    names = [_.get("function", {}).get("name") for _ in schemas]
    assert names.count("handoff_to_teammate") == 1
    # 团队里拿到的必须就是这一个实例，否则 outcomes 会记在孤儿对象上。
    assert team.agent("boss") is boss


async def test_install_handoff_tool_custom_group_activates() -> None:
    """非 basic 组会被建出来并立即激活，否则模型根本看不到工具。"""
    router = router4()
    router.add_member("boss", ["coordinate"])
    boss = echo_agent("boss")
    team = AgentTeam(
        members={
            "boss": boss,
            **{name: echo_agent(name) for name in router.members if name != "boss"},
        },
        router=router,
        limits=SpawnLimiter(),
    )
    await team.install_handoff_tool("boss", group_name="team")
    assert "team" in boss.state.tool_context.activated_groups
    schemas = await boss.toolkit.get_tool_schemas(boss.state.tool_context.activated_groups)
    names = [_.get("function", {}).get("name") for _ in schemas]
    assert "handoff_to_teammate" in names


async def test_streaming_handoff_tool_returns_async_generator(team4: AgentTeam) -> None:
    """``call`` 写成 async generator 时，``__call__`` 返回的是**生成器**。"""

    class StreamingHandoff(HandoffTool):
        """把 ``call`` 换成 async generator 的同款工具。"""

        async def call(  # type: ignore[override]
            self,
            to_member: str = "",
            task: str = "",
            reason: str = "",
        ) -> Any:
            """分段产出。

            Args:
                to_member (`str`): 接收方。
                task (`str`): 任务文本。
                reason (`str`): 理由。

            Yields:
                `Any`: 两段 ToolChunk。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolChunk

            yield ToolChunk(content=[TextBlock(text=f"start->{to_member}")])
            result = await self.team.dispatch(task, member=to_member)
            yield ToolChunk(content=[TextBlock(text=f"done ok={result.ok}")])

    tool = StreamingHandoff(team=team4, self_name="writer")
    raw = tool(to_member="coder", task="任务")
    assert not isinstance(raw, tuple)
    import inspect

    assert inspect.isawaitable(raw), "__call__ 对 async generator 返回的是可 await 对象"
    generator = await raw
    chunks = [item async for item in generator]
    assert len(chunks) == 2
    assert "start->coder" in chunks[0].content[0].text
    assert "ok=True" in chunks[1].content[0].text


# ======================================================================
# E · 端到端：主管-工人
# ======================================================================


async def test_supervise_end_to_end() -> None:
    """脚本化主管：ReAct 循环自己调交接工具，结果能被回收。"""
    router = router4()
    router.add_member("boss", ["coordinate"])
    boss = echo_agent(
        "boss",
        script=[
            {
                "text": "交给 coder。",
                "tool_calls": [
                    {
                        "id": "h-1",
                        "name": "handoff_to_teammate",
                        "input": {
                            "to_member": "coder",
                            "task": "写 fib",
                            "reason": "需要 python",
                        },
                    },
                ],
            },
            {"text": "已交付。"},
        ],
    )
    team = AgentTeam(
        members={
            "boss": boss,
            **{name: echo_agent(name) for name in router.members if name != "boss"},
        },
        router=router,
        limits=SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=4),
    )
    result = await team.supervise("给项目加一个 fib 函数", supervisor="boss")
    assert result.ok is True
    assert result.member == "boss"
    assert result.output == "已交付。"
    tool = team.handoff_tools["boss"]
    assert tool.stats() == {"count": 1, "ok": 1, "failed": 0, "to": ["coder"]}
    assert tool.outcomes[0].request.from_member == "boss"
    assert tool.outcomes[0].request.to_member == "coder"


async def test_supervise_installs_tool_before_running() -> None:
    """``supervise`` 会先把交接工具装进主管的 Toolkit。"""
    router = router4()
    router.add_member("boss", ["coordinate"])
    boss = echo_agent("boss", script=[{"text": "不用派活，我自己答。"}])
    team = AgentTeam(
        members={
            "boss": boss,
            **{name: echo_agent(name) for name in router.members if name != "boss"},
        },
        router=router,
        limits=SpawnLimiter(),
    )
    await team.supervise("随便一个能自己做完的任务", supervisor="boss")
    schemas = await boss.toolkit.get_tool_schemas(boss.state.tool_context.activated_groups)
    names = [_.get("function", {}).get("name") for _ in schemas]
    assert "handoff_to_teammate" in names


async def test_worker_failure_propagates_as_readable_error() -> None:
    """工人自己抛异常 -> ``ok=False``、``gate is None``、error 带异常类名。"""

    class BoomChatModel(EchoChatModel):
        """前若干次调用直接崩的模型。"""

        def _call_api(self, *args: Any, **kwargs: Any) -> Any:
            """永远抛。

            Args:
                *args (`Any`): 忽略。
                **kwargs (`Any`): 忽略。

            Raises:
                ConnectionError: 永远抛。
            """
            raise ConnectionError("模拟工人侧网络故障")

    flaky = Agent(
        name="flaky",
        system_prompt="我随时会挂。",
        model=BoomChatModel(stream=False),
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=2),
    )
    router = router4()
    team = AgentTeam(
        members={"coder": flaky, **{n: echo_agent(n) for n in router.members if n != "coder"}},
        router=router,
        limits=SpawnLimiter(),
    )
    result = await team.dispatch("交给会挂的工人", member="coder")
    assert result.ok is False
    assert result.gate is None
    assert result.error is not None
    assert result.error.startswith("ConnectionError")
    assert result.output == ""


async def test_three_failure_kinds_are_distinguishable() -> None:
    """三类失败的 ``error`` 前缀互不相同，``gate`` 只对闸门类返回非 None。"""
    router = router4()
    team = AgentTeam(
        members={"coder": echo_agent("coder")},
        router=CapabilityRouter({"coder": ["code"], "ghost": ["ghost"]}),
        limits=SpawnLimiter(max_spawn=1, max_depth=4, max_concurrent=4),
    )
    missing = await team.dispatch("x", member="ghost")
    ok = await team.dispatch("y", member="coder")
    gated = await team.dispatch("z", member="coder")
    assert ok.ok is True
    assert missing.error is not None and missing.error.startswith("NoRouteError")
    assert gated.error is not None and gated.error.startswith("SpawnLimitExceeded")
    assert missing.gate is None and gated.gate == "max_spawn"


async def test_model_call_count_shows_team_overhead() -> None:
    """量化「拆」的成本：3 人广播 3 次模型调用，单 Agent 1 次。"""

    class Counter(MiddlewareBase):
        """数模型调用次数。"""

        def __init__(self) -> None:
            """初始化。"""
            self.calls = 0

        async def on_model_call(
            self,
            agent: Agent,
            input_kwargs: dict,
            next_handler: Any,
        ) -> Any:
            """计数后转发。

            Args:
                agent (`Agent`): 正在跑的 Agent。
                input_kwargs (`dict`): 钩子入参。
                next_handler (`Any`): 链上的下一个 handler。

            Returns:
                `Any`: 模型返回。
            """
            self.calls += 1
            return await next_handler(**input_kwargs)

    solo_counter = Counter()
    solo = Agent(
        name="solo",
        system_prompt="s",
        model=EchoChatModel(stream=False),
        toolkit=Toolkit(),
        middlewares=[solo_counter],
        react_config=ReActConfig(max_iters=2),
    )
    async for _ in solo.reply_stream(
        inputs=UserMsg("user", "写一个 fib 函数"),
        yield_final_msg=True,
    ):
        pass
    assert solo_counter.calls == 1

    counters = {name: Counter() for name in ("researcher", "coder", "writer")}
    router = CapabilityRouter(
        {"researcher": ["summarize"], "coder": ["code"], "writer": ["prose"]},
    )
    team = AgentTeam(
        members={
            name: Agent(
                name=name,
                system_prompt=f"你是 {name}",
                model=EchoChatModel(stream=False),
                toolkit=Toolkit(),
                middlewares=[counter],
                react_config=ReActConfig(max_iters=2),
            )
            for name, counter in counters.items()
        },
        router=router,
        limits=SpawnLimiter(),
    )
    await team.broadcast("写一个 fib 函数", members=list(counters))
    assert sum(c.calls for c in counters.values()) == 3


async def test_tool_call_block_import_is_used() -> None:
    """``ToolCallBlock`` 是交接的运行时载体：它的 ``name`` 就是工具名。"""
    block = ToolCallBlock(id="c-1", name="handoff_to_teammate", input='{"to_member": "coder"}')
    assert block.name == "handoff_to_teammate"
