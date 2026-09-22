# -*- coding: utf-8 -*-
"""第 13 讲验证脚本：Subagent 与多智能体 —— 派生、路由、主管-工人。

本脚本把第 13 讲的主结论全部变成**可执行、可断言、可复现**的输出：

  A. **``SpawnLimiter`` 的三道闸门**（0 次 LLM）：``max_depth`` / ``max_spawn`` /
     ``max_concurrent`` 各自单独触发、``SpawnLimitExceeded.gate`` 的可区分性、
     ``release()`` 的幂等与「``active`` 变负数被钳回 0」、``async with slot()``
     与 ``with guard()`` 两个上下文管理器（后者要验证 ``@contextmanager``
     装饰器那个真实踩过的坑）。
  B. **``CapabilityRouter`` 的确定性**（0 次 LLM）：``_normalize`` 让
     ``code_review`` / ``code-review`` / ``CodeReview`` 落在同一个能力空间、
     ``required`` 过滤、排序键 ``(-score, attempts, member)`` 的平局决胜、
     「能力分是比例不是个数」这条防作弊规则、``record()`` 让历史成功率真正
     改变下一个接活的人。
  C. **``AgentTeam`` 的构造守卫与派活**（0 次 LLM，用离线 ``EchoChatModel``
     驱动**真** ``Agent``）：成员必须是独立 ``Agent`` 实例、``dispatch`` 的
     三种失败形态（路由不到人 / 成员不存在 / 闸门拒绝）、``broadcast`` 的
     去重与并发限流。
  D. **``HandoffTool`` 的权限与两种返回形状**（0 次 LLM）：无条件 ``ALLOW``
     与 ``FunctionTool`` 默认 ``ASK`` 的**实测对比**、``await tool(...)``
     拿到 ``ToolChunk``、自己交给自己被拒、``feedback`` 永不为空、
     ``install_handoff_tool`` 的幂等与「工具真的进了模型可见的 schema 列表」。
  E. **主管-工人端到端**（0 次 LLM，脚本化 ``EchoChatModel``）：主管的 ReAct
     循环**自己**决定调 ``handoff_to_teammate``、工具结果自动回填、结果回收
     能从 ``HandoffTool.outcomes`` 查到。
  F. **「什么时候不该拆多 Agent」的量化依据**（0 次 LLM）：用 ``ModelCallCounter``
     中间件数「一次任务」在「单 Agent」与「3 人团队」下各花多少次模型调用。
  G.（需要 key，``--live`` 打开）**真实 deepseek 主管-工人**：主管拿到
     ``handoff_to_teammate`` 后自己派活，工人真实作答。**3 次 LLM 调用**。

用法（``PYTHONPATH`` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/13_multiagent.py

    加 --live 才会跑 G 段（真实 deepseek-flash）。

LLM 调用预算：A~F 段 **0 次**；G 段 **3 次**（上限 6 次，留 3 次余量给重试）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import harness_kit

# 从 import 到的包反推路径，这样脚本放在任何地方都能跑：
#   <repo>/tutorial_agsc_reme/reference/harness_kit/__init__.py
#     .parent        -> .../reference/harness_kit
#     .parent.parent -> .../reference
REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402


def _find_dotenv(*bases: Path) -> Path | None:
    """在若干候选目录里找 ``.env``（由近及远，先找到先用）。

    为什么不能写死 ``REPO / ".env"``：本脚本号称「放在任何地方都能跑」，
    而「仓库根」是从 ``harness_kit`` 的位置**反推**出来的 —— 把 ``harness_kit``
    平铺到 ``/tmp/lesson13_verify`` 之后，反推出来的根会一路退到 ``/private``，
    于是 ``.env`` 找不到、G 段直接报「环境变量未定义」。从近到远找一圈就与
    目录层级无关了。

    Args:
        *bases (`Path`): 候选目录，由近及远。

    Returns:
        `Path | None`: 第一个存在的 ``.env``；一个都没有则 ``None``
            （此时完全依赖进程里已有的环境变量）。
    """
    for base in bases:
        candidate = base / ".env"
        if candidate.is_file():
            return candidate
    return None


_ENV_FILE = _find_dotenv(REF, REF.parent, REPO, Path.cwd())
if _ENV_FILE is not None:
    load_dotenv(_ENV_FILE, override=False)
    logger.debug("已加载环境变量文件：{}", _ENV_FILE)

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.message import ToolCallBlock, UserMsg  # noqa: E402
from agentscope.middleware import MiddlewareBase  # noqa: E402
from agentscope.permission import (  # noqa: E402
    PermissionBehavior,
    PermissionContext,
)
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402

from harness_kit.config.schema import ModelSpec  # noqa: E402
from harness_kit.models import build_chat_model  # noqa: E402
from harness_kit.models.adapters.echo import EchoChatModel  # noqa: E402
from harness_kit.multiagent import (  # noqa: E402
    AgentTeam,
    CapabilityRouter,
    HandoffOutcome,
    HandoffTool,
    NoRouteError,
    SpawnLimitExceeded,
    SpawnLimiter,
    collect_results,
)
from harness_kit.settings import Settings  # noqa: E402

LIVE: bool = "--live" in sys.argv

#: 全程使用的模型名（.env 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。
MODEL_NAME: str = os.getenv("LLM_MODEL") or "deepseek-chat"

#: 本脚本开始时 loguru 的默认级别会被压到 WARNING，避免 0 次 LLM 的段落
#: 被 ``CapabilityRouter.route`` / ``SpawnLimiter`` 的 INFO 日志刷屏。
logger.remove()
logger.add(sys.stderr, level="WARNING")


def banner(title: str) -> None:
    """打一条段落标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ======================================================================
# 公共件
# ======================================================================
class ModelCallCounter(MiddlewareBase):
    """数模型调用次数的中间件（第 12 讲同款，这里复用来量化「拆不拆」）。

    它不是「自研中间件链」—— 链仍然是 ``Agent._reply`` 内部那条
    ``execute_chain``（``third_party/agentscope/src/agentscope/agent/_agent.py:945``），
    本类只实现了官方暴露的 ``on_model_call`` 钩子
    （``third_party/agentscope/src/agentscope/middleware/_base.py:213``），
    并且**必须调用 ``next_handler``**，否则模型调用根本不会发生。

    Args:
        label (`str`): 打印时用的标签。
    """

    def __init__(self, label: str) -> None:
        """初始化计数器。"""
        self.label = label
        self.calls: int = 0

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        """计数后原样转发给下一个 handler。

        Args:
            agent (`Agent`): 正在跑的 Agent。
            input_kwargs (`dict`): 钩子入参（``messages`` / ``tools`` 等）。
            next_handler (`Any`): 链上的下一个 handler。

        Returns:
            `Any`: 模型的原始返回（``ChatResponse`` 或它的异步生成器）。
        """
        self.calls += 1
        return await next_handler(**input_kwargs)


class SlowModel(MiddlewareBase):
    """让模型调用真的「花时间」（``on_model_call`` 里 await 一次 ``asyncio.sleep``）。

    **它的唯一用途是让并发测试有意义。** ``EchoChatModel`` 是纯内存计算，
    一次模型调用里没有任何会让出事件循环的 ``await``：于是 ``broadcast`` 用
    ``asyncio.gather`` 起的三个任务会**依次跑完**而不是交错跑，``max_concurrent``
    这个闸门根本来不及触发 —— 这是「离线测试通过、线上限流失效」的经典假绿。
    加一次 ``asyncio.sleep`` 之后，任务会在持有票据的状态下真正挂起，
    闸门的行为才和线上一致。

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


def build_echo_agent(
    name: str,
    *,
    script: list[dict[str, Any]] | None = None,
    toolkit: Toolkit | None = None,
    counter: ModelCallCounter | None = None,
    middlewares: list[Any] | None = None,
) -> Agent:
    """造一个离线、确定性的 ``Agent``（``EchoChatModel`` 驱动）。

    这是全脚本能「0 次 LLM 跑通真 Agent Loop」的关键：``EchoChatModel``
    是 ``harness_kit.models.adapters.echo`` 里的**一等 provider**
    （``harness_kit/models/adapters/echo.py``），它按脚本回放每一次模型调用，
    因此「第几轮调哪个工具、传什么参数」是逐字可控的。

    Args:
        name (`str`): Agent 名（也会成为 ``Msg.name``）。
        script (`list[dict[str, Any]] | None`, optional): 脚本，见
            :class:`~harness_kit.models.adapters.echo.EchoChatModel` 的 docstring。
        toolkit (`Toolkit | None`, optional): 工具箱；``None`` 表示空箱。
        counter (`ModelCallCounter | None`, optional): 调用计数器。
        middlewares (`list[Any] | None`, optional): 额外的中间件（如
            :class:`SlowModel`）。

    Returns:
        `Agent`: 一个真实、完整的 ``AgentScope`` ``Agent``。
    """
    stack: list[Any] = list(middlewares or [])
    if counter is not None:
        stack.append(counter)
    return Agent(
        name=name,
        system_prompt=f"你是 {name}。",
        model=EchoChatModel(script=script, stream=False),
        toolkit=toolkit if toolkit is not None else Toolkit(),
        middlewares=stack,
        react_config=ReActConfig(max_iters=6),
    )


def build_live_agent(
    name: str,
    *,
    toolkit: Toolkit | None = None,
    system_prompt: str | None = None,
) -> Agent:
    """造一个真实 deepseek 驱动的 ``Agent``。

    Args:
        name (`str`): Agent 名。
        toolkit (`Toolkit | None`, optional): 工具箱。
        system_prompt (`str | None`, optional): 系统提示词。

    Returns:
        `Agent`: 真实模型驱动的 ``Agent``。
    """
    settings = Settings.from_env(
        repo_root=REF,
        profile_dir=REF / "harness_kit" / "profiles",
    )
    spec = ModelSpec(
        provider="deepseek",
        model_name=MODEL_NAME,
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
        temperature=0.0,
        stream=False,
    )
    return Agent(
        name=name,
        system_prompt=system_prompt or f"你是 {name}。",
        model=build_chat_model(spec, settings=settings),
        toolkit=toolkit if toolkit is not None else Toolkit(),
        react_config=ReActConfig(max_iters=6),
    )


def make_router(**kwargs: Any) -> CapabilityRouter:
    """造本脚本统一使用的四成员路由表。

    成员与能力（刻意包含 ``Code-Review`` 这种**连字符**写法，用来验证
    ``_normalize`` 把 ``code_review`` / ``Code-Review`` 视为同一个能力）：

    ============== ==========================================
    成员            能力标签
    ============== ==========================================
    ``researcher``   ``web_search`` / ``summarize``
    ``coder``        ``code`` / ``python``
    ``reviewer``     ``Code-Review`` / ``security``
    ``writer``       ``prose`` / ``summarize``
    ============== ==========================================

    Args:
        **kwargs (`Any`): 透传给 :class:`CapabilityRouter`（如 ``history_weight``）。

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


def make_team(
    *,
    router: CapabilityRouter,
    limits: SpawnLimiter | None = None,
    agents: dict[str, Agent] | None = None,
) -> AgentTeam:
    """造一个四成员离线队伍（成员都是 ``EchoChatModel`` 驱动）。

    Args:
        router (`CapabilityRouter`): 路由表。
        limits (`SpawnLimiter | None`, optional): 闸门。
        agents (`dict[str, Agent] | None`, optional): 自定义成员；``None`` 时
            为路由表里的四个名字各造一个 echo Agent。

    Returns:
        `AgentTeam`: 团队。
    """
    members = agents or {
        name: build_echo_agent(name) for name in router.members
    }
    return AgentTeam(
        members=members,
        router=router,
        limits=limits or SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=4),
    )


# ======================================================================
# A · SpawnLimiter：三道闸门
# ======================================================================
async def section_a() -> None:
    """A 段：派生闸门（纯计数，0 次 LLM）。"""
    banner("A · SpawnLimiter：三道互相独立、缺一不可的闸门")

    limiter = SpawnLimiter(max_spawn=4, max_depth=2, max_concurrent=2)
    print(f"  初始：{limiter.describe()}")

    banner("A1 · max_depth 单独触发：连锁派生永远回不到根")
    try:
        limiter.try_acquire(depth=2)
    except SpawnLimitExceeded as exc:
        print(f"  depth=2 -> gate={exc.gate!r} limit={exc.limit} current={exc.current}")
        print(f"           {exc}")
    assert limiter.snapshot().rejected == 1

    banner("A2 · max_concurrent 单独触发：把上游 QPS 打满")
    t1 = limiter.try_acquire(depth=0)
    t2 = limiter.try_acquire(depth=0)
    print(f"  两张票据在手：{limiter.describe()}")
    try:
        limiter.try_acquire(depth=0)
    except SpawnLimitExceeded as exc:
        print(f"  第三张   -> gate={exc.gate!r} limit={exc.limit} current={exc.current}")
        print(f"           {exc}")
    assert limiter.snapshot().rejected == 2

    banner("A3 · max_spawn 单独触发：宽度爆炸，重试没有意义")
    t1.release()
    t2.release()
    print(f"  释放后：{limiter.describe()}")
    t3 = limiter.try_acquire(depth=0)
    t4 = limiter.try_acquire(depth=0)
    print(f"  再拿两张（spawned=4/4）：{limiter.describe()}")
    try:
        limiter.try_acquire(depth=0)
    except SpawnLimitExceeded as exc:
        print(f"  第五张   -> gate={exc.gate!r} limit={exc.limit} current={exc.current}")
        print(f"           {exc}")
    print(f"  remaining() = {limiter.remaining()}")
    assert limiter.snapshot().rejected == 3
    print("  >>> 三道门的报错文案各不相同，因为处置方式不同：")
    print("      max_concurrent -> 稍后重试 / 降低并行度；")
    print("      max_depth      -> 检查是不是形成了 A->B->C->A 的环；")
    print("      max_spawn      -> 整体策略错了，重试无意义。")

    banner("A4 · release() 幂等：重复释放不会把 active 减成负数")
    t3.release()
    t3.release()
    t3.release()
    print(f"  同一张票据 release 三次后 active = {limiter.active}")
    assert limiter.active == 1  # 只剩 t4
    t4.release()
    t4.release()
    print(f"  再释放 t4 两次后 active = {limiter.active}（0，不是 -1）")
    assert limiter.active == 0

    banner("A5 · async with slot()：异常与取消路径都会自动归还")
    fresh = SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=1)
    try:
        async with fresh.slot(depth=0):
            print(f"  slot 内：{fresh.describe()}")
            raise RuntimeError("模拟工人崩了")
    except RuntimeError as exc:
        print(f"  捕获：{exc}")
    print(f"  slot 外：{fresh.describe()}  active={fresh.active}")
    assert fresh.active == 0, "异常路径必须归还票据"

    banner("A6 · with guard()：同步上下文管理器（@contextmanager 那个坑）")
    with fresh.guard(depth=1) as ticket:
        print(f"  guard 内：{ticket!r}  active={fresh.active}")
        assert fresh.active == 1
    print(f"  guard 外：active={fresh.active}")
    assert fresh.active == 0
    print("  >>> 少了 @contextmanager 的话，这里会直接")
    print("      TypeError: 'generator' object does not support the context manager protocol")

    banner("A7 · 参数校验与统计快照")
    for bad in (0, -1):
        try:
            SpawnLimiter(max_spawn=bad)
        except ValueError as exc:
            print(f"  max_spawn={bad} -> ValueError: {str(exc)[:60]}…")
    print(f"  reset 前：{fresh.describe()}")
    fresh.reset()
    print(f"  reset 后：{fresh.describe()}")


# ======================================================================
# B · CapabilityRouter：确定性路由
# ======================================================================
async def section_b() -> None:
    """B 段：能力路由（纯计算，0 次 LLM）。"""
    banner("B · CapabilityRouter：用查表替代「让模型选人」")

    router = make_router()
    print(router.explain("帮我 review 这段 Python 代码的安全性"))

    banner("B1 · 连字符 / 下划线 / 驼峰 是同一个能力（否则路由会静默失效）")
    for required in ("code_review", "code-review", "CodeReview", "codereview"):
        chosen = router.route("请审查这段代码", required=required)
        print(f"  required={required!r:20} -> {chosen}")
        assert chosen == "reviewer", required
    print("  >>> 归一化后都是 'codereview'，所以四种写法都指向 reviewer。")
    print("      这是真实踩过的坑：写成 [a-zA-Z0-9_] 的话 'code_review' 原样保留、")
    print("      'Code-Review' 变成 'codereview'，两边匹配不上，路由静默失败。")

    banner("B2 · required 过滤不到任何人 -> NoRouteError(kind='route')")
    try:
        router.route("随便什么任务", required="quantum_computing")
    except NoRouteError as exc:
        print(f"  kind={exc.kind!r} required={exc.required!r}")
        print(f"  {exc}")
    assert NoRouteError("x", None, []).kind == "route"

    banner("B3 · 平局按「调用次数少的优先」再按「名字字典序」决胜")
    ranked = router.rank("帮我 summarize 这段材料")
    print(router.explain("帮我 summarize 这段材料"))
    names = [_.member for _ in ranked]
    assert names[:2] == ["researcher", "writer"], names
    print(f"  排序结果 = {names}")
    print("  >>> researcher 与 writer 能力分都是 0.5（各命中 1 个标签 / 共 2 个），")
    print("      但 researcher 字典序在前，所以第一次选 researcher。")
    print("      排序键是 (-score, attempts, member)：中段是**轮转**，末段是**可复现**。")

    banner("B4 · record() 让历史成功率真正改变下一个接活的人")
    router.record("researcher", ok=False)
    router.record("researcher", ok=False)
    router.record("researcher", ok=False)
    print(f"  researcher 连败 3 次后：")
    print(router.explain("帮我 summarize 这段材料"))
    assert router.route("帮我 summarize 这段材料") == "writer"
    print("  >>> 现在轮到 writer 了 —— 这就是「历史成功率」这个维度的实际作用：")
    print("      一个总是失败的成员会自动被降权，不需要人去改路由表。")

    banner("B5 · 能力分是「命中数 / 标签总数」，不是「命中数」")
    wide = CapabilityRouter({"wide": ["a", "b", "c", "d", "e", "f", "g", "h"], "narrow": ["h"]})
    print(wide.explain("h"))
    s_wide = wide.score("wide", "h")
    s_narrow = wide.score("narrow", "h")
    print(f"  wide   cap={s_wide.capability_score:.3f}  score={s_wide.score:.3f}")
    print(f"  narrow cap={s_narrow.capability_score:.3f}  score={s_narrow.score:.3f}")
    assert s_narrow.score > s_wide.score
    print("  >>> 挂 8 个标签只中 1 个（1/8）不该赢过精准命中（1/1）。")
    print("      用「个数」的话，谁把标签写得多谁就永远赢，路由表会迅速失效。")

    banner("B6 · 冷启动是 0.5 而不是 0（否则新成员永远轮不到出场）")
    fresh = make_router()
    print(f"  fresh.success_rate('coder') = {fresh.success_rate('coder')}")
    assert fresh.success_rate("coder") == 0.5
    print(f"  {fresh.explain('python 代码')}")

    banner("B7 · route_many：unique_capability 防止「同一件事做三遍」")
    r = make_router()
    plain = r.route_many("summarize 并 review 这段代码", k=3)
    uniq = r.route_many("summarize 并 review 这段代码", k=3, unique_capability=True)
    print(f"  不去重 = {plain}")
    print(f"  去重   = {uniq}")
    assert len(set(plain)) == len(plain), "route_many 本身就不该给出重复成员"
    print("  >>> 两种模式都不会返回同名成员（rank 一次只产出一个成员）。")
    print("      unique_capability 额外保证「主能力」不同，避免选出两个 summarize 专家。")

    banner("B8 · 查一个不存在的成员 -> NoRouteError(kind='member')，文案不同")
    try:
        r.capabilities_of("boss")
    except NoRouteError as exc:
        print(f"  kind={exc.kind!r}")
        print(f"  {exc}")
    assert NoRouteError.for_member("boss", ["a"]).kind == "member"
    print("  >>> 早期版本两种失败共用一句文案，于是查成员时报的是")
    print("      「没有成员能接这个任务」，让人以为是路由配置问题。")


# ======================================================================
# C · AgentTeam：构造守卫、派活、闸门拒绝
# ======================================================================
async def section_c() -> None:
    """C 段：团队编排（真 Agent + 离线模型，0 次 LLM）。"""
    banner("C · AgentTeam：独立 AgentState 是并发的**前提**")

    router = make_router()

    banner("C1 · 构造守卫：成员必须是**各自独立**的 Agent 实例")
    try:
        AgentTeam(members={}, router=router, limits=SpawnLimiter())
    except ValueError as exc:
        print(f"  空成员 -> ValueError: {exc}")

    shared = build_echo_agent("shared")
    try:
        AgentTeam(
            members={"a": shared, "b": shared},
            router=router,
            limits=SpawnLimiter(),
        )
    except ValueError as exc:
        print(f"  同一实例 -> ValueError:\n    {exc}")

    banner("C2 · dispatch：我们的代码选人，工人独立作答")
    team = make_team(router=make_router())
    print(team.describe())
    result = await team.dispatch("请帮我 summarize 这篇论文")
    print(f"  {result.summary()}")
    assert result.ok and result.member == "researcher"
    assert "[echo]" in result.output
    print(f"  dispatch 后：{team.describe().splitlines()[-1]}")
    print("  >>> 工人是一条独立的 Agent，它拿到的是 _TASK_PROMPT 包好的任务文本，")
    print("      两边 state.context 一个是主管的、一个是工人的，永不相通。")

    banner("C3 · 三种失败形态之二：路由选中的人不在 members 里")
    lonely = AgentTeam(
        members={"coder": build_echo_agent("coder")},
        router=CapabilityRouter({"coder": ["code"], "ghost": ["ghost"]}),
        limits=SpawnLimiter(),
    )
    bad = await lonely.dispatch("ghost work", member="ghost")
    print(f"  {bad.summary()}")
    assert bad.ok is False and bad.error.startswith("NoRouteError")
    print("  >>> 这类是**配置错误**，但 dispatch 仍然返回 ok=False 而不是抛：")
    print("      编排层需要一个统一的返回值形状，否则每个调用点都要写 try/except。")
    print("      例外是**路由失败**（一个候选都没有）—— 那会显式抛 NoRouteError。")
    try:
        await lonely.dispatch("完全没人能接的活", required_capability="quantum")
    except NoRouteError as exc:
        print(f"  路由失败 -> 显式抛出: {str(exc)[:70]}…")

    banner("C4 · 三种失败形态之三：闸门拒绝，换人也没用")
    tight = SpawnLimiter(max_spawn=1, max_depth=3, max_concurrent=4)
    team2 = make_team(router=make_router(), limits=tight)
    first = await team2.dispatch("第一次派活")
    second = await team2.dispatch("第二次派活")
    print(f"  {first.summary(limit=40)}")
    print(f"  {second.summary(limit=40)}")
    assert first.ok is True and second.ok is False
    assert second.gate == "max_spawn", second.error
    print(f"  second.gate = {second.gate!r}")
    print("  >>> TeamResult.gate 把「这个工人干不了」和「闸门不放行」分开：")
    print("      前者可以换人重试，后者换谁都没用。")

    banner("C5 · broadcast：去重 + 顺序一致 + 并发限流")
    bteam = make_team(router=make_router())
    results = await bteam.broadcast("请总结并审查这段代码", members=["coder", "coder", "reviewer"])
    for item in results:
        print(f"  {item.summary(limit=40)}")
    assert len(results) == 2, "显式列表必须去重"
    print("  >>> 传 ['coder','coder','reviewer'] 只跑两个成员：同一成员跑两遍")
    print("      既浪费钱，又会让它自己的上下文串起来。")

    squeezed = make_team(
        router=make_router(),
        limits=SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=1),
        agents={
            name: build_echo_agent(name, middlewares=[SlowModel(0.05)])
            for name in ("coder", "reviewer", "writer", "researcher")
        },
    )
    squeezed_results = await squeezed.broadcast(
        "并行任务",
        members=["coder", "reviewer", "writer"],
    )
    print("  max_concurrent=1 时广播 3 人（成员带 SlowModel，让模型调用真的挂起）：")
    for item in squeezed_results:
        print(f"    {item.summary(limit=40)}")
    assert sum(1 for _ in squeezed_results if _.ok) == 1
    assert sum(1 for _ in squeezed_results if _.gate == "max_concurrent") == 2
    print("  >>> broadcast 敢用 asyncio.gather，是因为它按**成员**分派，")
    print("      而 max_concurrent 把并发真正压住了 —— 拒绝了 2 个而不是排队。")
    print("      注意 SlowModel 的必要性：EchoChatModel 是纯内存计算、")
    print("      一次调用里没有任何会让出事件循环的 await，三个任务会**依次**")
    print("      跑完，闸门永远触发不了 —— 那是「离线全绿、线上限流失效」的假绿。")

    banner("C6 · add_member / remove_member / aclose")
    late = make_team(router=make_router())
    late.add_member("intern", build_echo_agent("intern"), capabilities=["fetch", "grep"])
    print(f"  加人后 names = {late.names}")
    assert "intern" in late.router.members
    assert late.remove_member("intern") is True
    assert late.remove_member("intern") is False
    print(f"  减人后 names = {late.names}")
    await late.aclose()
    print(f"  aclose 后 names = {late.names}（只断引用，不关 Agent）")


# ======================================================================
# D · HandoffTool：权限、两种返回形状、结果回收
# ======================================================================
async def section_d() -> None:
    """D 段：交接工具（0 次 LLM）。"""
    banner("D · HandoffTool：把「谁交给谁」的决定权还给模型")

    team = make_team(router=make_router())
    tool = team.handoff_tool("researcher")
    print(f"  工具名   = {tool.name!r}")
    print(f"  required = {tool.input_schema['required']}")
    print(f"  参数     = {sorted(tool.input_schema['properties'])}")
    print(f"  自描述里列出的候选人 = {tool.input_schema['properties']['to_member']['description'][-60:]!r}")

    banner("D1 · 权限：自定义 ToolBase 默认会 ASK，我们必须显式声明")
    decision = await tool.check_permissions({}, PermissionContext())
    print(f"  HandoffTool.check_permissions -> {decision.behavior.name} / {decision.decision_reason}")
    assert decision.behavior is PermissionBehavior.ALLOW

    async def noop() -> str:
        """一个什么也不做的函数，用来观察 FunctionTool 的默认权限。"""
        return "ok"

    plain = FunctionTool(noop)
    plain_decision = await plain.check_permissions({}, PermissionContext())
    print(f"  FunctionTool.check_permissions -> {plain_decision.behavior.name}"
          f" / {plain_decision.message}")
    assert plain_decision.behavior is PermissionBehavior.ASK
    print("  >>> 这就是契约 §3.13 那条铁律的实测依据（`tool/_adapters.py:132`）：")
    print("      FunctionTool 在 permission=None 时返回 ASK。")
    print("      交接是纯控制流、无副作用，每次问用户只会制造噪声。")
    print("      注意 ALLOW 不是绕过规则：引擎里用户配的 DENY 优先级更高。")

    banner("D2 · await tool(...) 拿到的是 ToolChunk（协程分支）")
    chunk = await tool(to_member="coder", task="写一个快速排序", reason="需要 python 能力")
    texts = [_.text for _ in chunk.content if getattr(_, "type", None) == "text"]
    print(f"  ToolChunk.content[0].type = {chunk.content[0].type!r}")
    print(f"  文本前 80 字 = {texts[0][:80]!r}")
    assert texts and texts[0].startswith("[handoff -> coder OK")
    print("  >>> 本工具的 call 是**协程**函数，所以 await 一次就拿到 ToolChunk。")
    print("      如果 call 写成 async generator，__call__ 会返回生成器")
    print("      （`tool/_base.py:220` 那段 inspect.isasyncgenfunction 分支），")
    print("      那时必须先 await 再 async for —— 两种形状我们都演示了。")

    banner("D3 · call 是 async generator 时，__call__ 返回生成器而不是 ToolChunk")

    class StreamingHandoff(HandoffTool):
        """把 call 换成 async generator 的同款工具，用来演示第二种返回形状。"""

        async def call(  # type: ignore[override]
            self,
            to_member: str = "",
            task: str = "",
            reason: str = "",
        ) -> Any:
            """yield 若干 ToolChunk（异步生成器）。

            Args:
                to_member (`str`): 接收方。
                task (`str`): 任务文本。
                reason (`str`): 理由。

            Yields:
                `Any`: 分段产出。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolChunk

            yield ToolChunk(content=[TextBlock(text="[1/2] 正在把任务交给 " + to_member)])
            result = await self.team.dispatch(task, member=to_member, depth=self.depth + 1)
            yield ToolChunk(content=[TextBlock(text=f"[2/2] {result.member} ok={result.ok}")])

    streaming = StreamingHandoff(team=team, self_name="writer")
    raw = streaming(to_member="coder", task="排序实现")
    print(f"  __call__ 返回类型 = {type(raw).__name__}")
    got = await raw
    print(f"  await 之后类型   = {type(got).__name__}")
    chunks = [item async for item in got]
    print(f"  再 async for 拿到 {len(chunks)} 个 ToolChunk")
    assert len(chunks) == 2
    print("  >>> 「await 一次」与「await 之后再 async for」是两种不同形状，")
    print("      Agent 的 call_tool 两种都吃，但手写调用时写错就会得到")
    print("      'async_generator' object is not subscriptable 之类的怪错。")

    banner("D4 · 三种拒绝：空 task / 空 to_member / 自己交给自己（都不抛异常）")
    before = len(tool.outcomes)
    for kwargs, label in (
        ({"to_member": "coder", "task": "   "}, "空 task"),
        ({"to_member": "", "task": "有活"}, "空 to_member"),
        ({"to_member": "researcher", "task": "有活"}, "自己交给自己"),
    ):
        rejected = await tool(**kwargs)
        text = rejected.content[0].text
        print(f"  {label:12} -> {text[:66]}…")
    assert len(tool.outcomes) == before, "被拒的移交不该记进 outcomes"
    print("  >>> 失败回一段**可操作的**文本而不是抛异常：抛异常会被 Agent 循环")
    print("      记成工具错误，模型看不到「下一步该做什么」。")

    banner("D5 · feedback 永不为空 + collect_results 的预算裁剪")
    fake = HandoffOutcome(
        request={"from_member": "a", "to_member": "b", "task": "x", "reason": ""},
        ok=False,
        error="TimeoutError: 60s",
        elapsed_ms=60001.0,
    )
    print(f"  失败 feedback 的长度 = {len(fake.feedback)}（非空）")
    assert fake.feedback
    assert "Do not retry" in fake.feedback
    print(f"  collect_results 空输入 = {collect_results([])!r}")
    one = await team.dispatch("一次普通派活", member="coder")
    out = collect_results(
        [
            HandoffOutcome(
                request={"from_member": "boss", "to_member": "coder", "task": "t", "reason": ""},
                ok=one.ok,
                output=one.output,
                elapsed_ms=one.elapsed_ms,
            ),
        ],
    )
    print(f"  collect_results 单条 = {out[:100]!r}")
    print("  >>> 8 个成员的长文全塞回发起方上下文是**要花钱**的，")
    print("      所以 collect_results 有 max_chars 预算，超了就按条截断。")

    banner("D6 · install_handoff_tool：默认进 basic 组，模型才看得见")
    boss = build_echo_agent("boss")
    d6_router = make_router()
    d6_router.add_member("boss", ["coordinate"])
    team3 = make_team(
        router=d6_router,
        agents={
            "boss": boss,
            "coder": build_echo_agent("coder"),
            "reviewer": build_echo_agent("reviewer"),
            "researcher": build_echo_agent("researcher"),
            "writer": build_echo_agent("writer"),
        },
    )
    installed = await team3.install_handoff_tool("boss")
    again = await team3.install_handoff_tool("boss")
    assert installed is again, "重复安装必须返回同一实例"
    schemas = await boss.toolkit.get_tool_schemas(boss.state.tool_context.activated_groups)
    names = [_.get("function", {}).get("name") for _ in schemas]
    print(f"  basic 组里模型能看到的工具 = {names}")
    assert "handoff_to_teammate" in names
    assert names.count("handoff_to_teammate") == 1, "幂等：不能堆出两份同名 schema"
    print(f"  install_handoff_tool 幂等（同一实例）: {installed is again}")
    print("  >>> 默认放进 'basic' 组是有原因的：模型可见的工具 = basic 组")
    print("      + activated_groups（`agent/_agent.py:3268` 把 activated_groups")
    print("      传给 `Toolkit.get_tool_schemas`，而它的 docstring 明确写")
    print("      「The basic group will always be included」）。")
    print("      放进一个非 basic 组又不激活它 = 主管**从不派活**这种静默失效。")


# ======================================================================
# E · 主管-工人端到端（脚本化模型，0 次 LLM）
# ======================================================================
async def section_e() -> None:
    """E 段：主管自己决定派活（0 次 LLM）。"""
    banner("E · 端到端：主管的 ReAct 循环自己调 handoff_to_teammate")

    router = make_router()
    router.add_member("boss", ["coordinate"])
    counter = ModelCallCounter("boss")
    boss = build_echo_agent(
        "boss",
        script=[
            {
                "text": "这需要 python 能力，我把它交给 coder。",
                "tool_calls": [
                    {
                        "id": "handoff-1",
                        "name": "handoff_to_teammate",
                        "input": {
                            "to_member": "coder",
                            "task": "写一个 fib(n) 的迭代实现，返回代码块。",
                            "reason": "需要 python 能力",
                        },
                    },
                ],
            },
            {"text": "coder 已经交付，任务完成。"},
        ],
        counter=counter,
    )
    team = make_team(
        router=router,
        agents={
            "boss": boss,
            "coder": build_echo_agent("coder"),
            "reviewer": build_echo_agent("reviewer"),
            "researcher": build_echo_agent("researcher"),
            "writer": build_echo_agent("writer"),
        },
    )

    result = await team.supervise("给项目加一个 fib 函数", supervisor="boss")
    print(f"  supervise 返回：{result.summary(limit=60)}")
    assert result.ok is True
    assert result.member == "boss"
    print(f"  主管的模型调用次数 = {counter.calls}（1 次派活 + 1 次收尾）")
    assert counter.calls == 2

    tool = team.handoff_tools["boss"]
    print(f"  移交记录 = {tool.stats()}")
    assert len(tool.outcomes) == 1
    outcome = tool.outcomes[0]
    print(f"  from={outcome.request.from_member} to={outcome.request.to_member} "
          f"ok={outcome.ok} elapsed={outcome.elapsed_ms:.1f}ms")
    assert outcome.request.to_member == "coder"
    assert outcome.ok is True
    print("  >>> 全程**没有一行我们写的调度循环**：")
    print("      主管的 Agent._reply（`agent/_agent.py:892`）自己跑了 ReAct，")
    print("      工具结果由 `_acting` 回填（`agent/_agent.py:2723`），")
    print("      我们只提供了「一个工具 + 一个派活函数」。")

    banner("E1 · 深度传播：A->B->C->D 会被 max_depth 拦住")
    deep_team = make_team(
        router=make_router(),
        limits=SpawnLimiter(max_spawn=32, max_depth=2, max_concurrent=4),
    )
    d0 = await deep_team.dispatch("深度 0", member="coder", depth=0)
    d1 = await deep_team.dispatch("深度 1", member="coder", depth=1)
    d2 = await deep_team.dispatch("深度 2", member="coder", depth=2)
    print(f"  depth=0 -> ok={d0.ok}")
    print(f"  depth=1 -> ok={d1.ok}")
    print(f"  depth=2 -> ok={d2.ok} error={d2.error}")
    assert d0.ok and d1.ok and not d2.ok
    assert d2.gate == "max_depth"
    print("  >>> HandoffTool 派活时用的是 self.depth + 1，所以「谁被谁派的」")
    print("      这条链是有账的，max_depth 拦得住 A->B->C->D->… 的无限递归。")

    banner("E2 · 失败传播：工人失败时发起方拿到的是**可读的**失败说明")
    class BoomChatModel(EchoChatModel):
        """一个前 N 次调用直接崩的模型，用来模拟工人侧失败。"""

        def _call_api(self, *args: Any, **kwargs: Any) -> Any:
            """直接抛异常。

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
    fail_team = make_team(
        router=make_router(),
        agents={
            "coder": flaky,
            "reviewer": build_echo_agent("reviewer"),
            "researcher": build_echo_agent("researcher"),
            "writer": build_echo_agent("writer"),
        },
    )
    failed = await fail_team.dispatch("交给一个会挂的工人", member="coder")
    print(f"  {failed.summary(limit=50)}")
    assert failed.ok is False and failed.error
    print(f"  gate = {failed.gate}（None 表示不是闸门问题，是工人自己挂了）")
    assert failed.gate is None
    boss2 = build_echo_agent("boss")
    print(f"  另外造了个闲人 boss2={boss2.name!r}（本段不用它派活）")
    print("  >>> 三类失败（SpawnLimitExceeded / NoRouteError / 工人异常）在")
    print("      TeamResult.error 里前缀不同，gate 属性只对第一类返回非 None。")
    print("      这正是「失败传播」要解决的问题：编排层必须能分辨")
    print("      「重试有用」「换人有用」「收工」这三种不同的处置。")


# ======================================================================
# F · 什么时候不该拆多 Agent（量化）
# ======================================================================
async def section_f() -> None:
    """F 段：用模型调用次数量化「拆」的成本（0 次 LLM）。"""
    banner("F · 工程判断：拆多 Agent 之前先数一遍模型调用")

    solo_counter = ModelCallCounter("solo")
    solo = build_echo_agent("solo", counter=solo_counter)
    final: Any = None
    async for item in solo.reply_stream(
        inputs=UserMsg("user", "写一个 fib 函数"),
        yield_final_msg=True,
    ):
        if item is not None:
            final = item
    print(f"  单 Agent 干一件事：{solo_counter.calls} 次模型调用")
    print(f"  单 Agent 的产物：{str(final.get_text_content())[:60]!r}")
    print("  >>> 注意 reply_stream 是 async generator，只能 async for，不能 await")

    team_counters = {
        name: ModelCallCounter(name) for name in ("researcher", "coder", "writer")
    }
    router = CapabilityRouter(
        {
            "researcher": ["summarize"],
            "coder": ["code"],
            "writer": ["prose"],
        },
    )
    team = AgentTeam(
        members={
            name: build_echo_agent(name, counter=counter)
            for name, counter in team_counters.items()
        },
        router=router,
        limits=SpawnLimiter(max_spawn=8, max_depth=3, max_concurrent=4),
    )
    results = await team.broadcast("写一个 fib 函数", members=["researcher", "coder", "writer"])
    total = sum(c.calls for c in team_counters.values())
    print(f"  3 人团队广播同一件事：{total} 次模型调用"
          f"  明细={ {k: v.calls for k, v in team_counters.items()} }")
    assert sum(1 for _ in results if _.ok) == 3
    print(f"  结论：拆成 3 个成员，模型调用从 {solo_counter.calls} 次变成 {total} 次，"
          f"放大 {total / max(1, solo_counter.calls):.1f} 倍。")
    print("  >>> 判定表（本讲的工程结论）：")
    print("      · 任务**能并行**且**彼此不需要对方的中间结果** -> 拆，收益与人数成正比；")
    print("      · 任务**串行**、后一步依赖前一步的产物 -> 不拆，多 Agent 只会")
    print("        把一次调用变成 N 次调用 + N 份上下文；")
    print("      · 只是**角色口吻不同**（「你是资深审稿人」）-> 不拆，换一段 system prompt 就行；")
    print("      · 需要**不同权限 / 不同 workspace / 不同模型**才能安全隔离 -> 拆，")
    print("        这是隔离需求而不是性能需求；")
    print("      · 需要**独立上下文预算**（一个成员读完 20 个文件后上下文该扔掉）-> 拆。")
    print("      · 一刀切的反面：拆了以后**没有任何成员需要对方的中间结果**才算真并行，")
    print("        否则你只是把串行链伪装成了团队。")


# ======================================================================
# G · 真实 deepseek 主管-工人（--live）
# ======================================================================
async def section_g() -> None:
    """G 段：真实模型下跑一次主管-工人（3 次 LLM 调用）。"""
    banner("G ·（--live）真实 deepseek：主管自己决定派活")

    team = AgentTeam(
        members={
            "boss": build_live_agent(
                "boss",
                system_prompt=(
                    "你是团队主管。你**没有**直接写代码的能力，"
                    "必须用 handoff_to_teammate 把编程任务交给 coder，"
                    "然后把它返回的结论转述给用户。"
                ),
            ),
            "coder": build_live_agent(
                "coder",
                system_prompt="你是 Python 工程师。只输出代码与一句话说明，不要寒暄。",
            ),
        },
        router=CapabilityRouter({"boss": ["coordinate"], "coder": ["code", "python"]}),
        limits=SpawnLimiter(max_spawn=4, max_depth=2, max_concurrent=2),
    )

    result = await team.supervise(
        "请让 coder 写一个 Python 函数 fib(n)（迭代实现），"
        "然后把它的实现原样贴给我。",
        supervisor="boss",
    )
    print(f"  supervise -> {result.summary(limit=400)}")
    tool = team.handoff_tools["boss"]
    print(f"  移交统计 = {tool.stats()}")
    if tool.outcomes:
        print(f"  工人物件前 200 字：\n    {tool.outcomes[0].output[:200]}")
    print("  >>> G 段预算 3 次模型调用（主管派活 1 + 工人作答 1 + 主管收尾 1）。")
    print("      脚本**不断言**主管一定调了工具：那次决策在模型手里，")
    print("      确定性的交接路径由 E 段的脚本化模型守。")


async def main() -> int:
    """跑全部段落，返回退出码。

    Returns:
        `int`: 0 = 全部通过。
    """
    await section_a()
    await section_b()
    await section_c()
    await section_d()
    await section_e()
    await section_f()
    if LIVE:
        await section_g()
    else:
        print()
        print("=" * 78)
        print("跳过 G 段（真实 LLM）。加 --live 打开：3 次 deepseek-flash 调用。")
        print("=" * 78)
    print()
    print("=" * 78)
    print("PASS · 第 13 讲全部断言通过（A~F 段 0 次 LLM 调用）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
