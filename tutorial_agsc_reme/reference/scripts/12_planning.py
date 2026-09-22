# -*- coding: utf-8 -*-
"""第 12 讲验证脚本：Planning 与 SOP —— 长任务拆解、状态机与断点恢复。

它把本讲的主结论全部变成可执行的断言 / 可观察的输出：

  A. **``TaskGraph`` 六状态机**（0 次 LLM）：``pending`` / ``running`` /
     ``blocked`` / ``done`` / ``failed`` / ``skipped`` 的合法转移、
     ``ready()`` 的算术、``blocked`` 的级联与自愈、``topological_order()``
     的确定性、环与悬空依赖的拒绝。
     **``blocked`` 是本讲补出来的第六个状态**：AgentScope 的
     ``pipeline/`` 与 ``sop/`` 都不回答「上游挂了，下游算什么」。
  B. **``PlanStore``：落盘 + 断点续跑 + 与会话事件溯源对接**（0 次 LLM）：
     原子替换写盘、``revisions`` 递增、``running`` 归一成 ``pending``、
     ``blocked`` 重算、``plan_digest`` 的稳定性、以及**每次快照都往第 9 讲的
     会话日志里追加一条 ``CUSTOM`` 事件**。
  C. **``HarnessSOP``：YAML → ``SOPEngine``，用 stub 执行者确定性跑**（0 次 LLM）：
     这里用的是 ``AgentLike`` 这个 **Protocol**（``sop/_schema.py:28``）——
     只要对象有 ``reply_stream``，它就能当 executor / verifier。
     于是「拒一次再通过」「预算耗尽 → FAILED」「存状态 → 新引擎续跑」
     三件事都能在**不花一次模型调用**的前提下被钉死。
  D.（需要 key，``--live`` 打开）**``HarnessPlanner`` 真实拆解 + 执行 + 汇总**：
     拆解走 AgentScope 原生的 ``structured_schema=PlanDraft``，
     执行按 ``TaskGraph.ready()`` 逐个节点交给 ``Agent.reply_stream``。
     **4 次 LLM 调用**（1 拆解 + 2 节点 + 1 汇总）。
  E.（需要 key，``--live`` 打开）**真实 LLM 跑 YAML SOP**：2 步、都 ``verify: false``，
     **2 次 LLM 调用**。

用法（``PYTHONPATH`` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/12_planning.py

    加 ``--live`` 才会跑 D / E 段（真实 deepseek-flash）。

LLM 调用预算：A~C 段 **0 次**；D 段 **4 次**、E 段 **2 次**，合计 **6 次**（上限）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

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

load_dotenv(REPO / ".env", override=False)

from agentscope.agent import Agent  # noqa: E402
from agentscope.message import Msg, TextBlock  # noqa: E402
from agentscope.middleware import MiddlewareBase  # noqa: E402
from agentscope.tool import Toolkit  # noqa: E402
from agentscope.types import ReplyFinishedReason  # noqa: E402

from harness_kit.config.schema import ModelSpec  # noqa: E402
from harness_kit.models import build_chat_model  # noqa: E402
from harness_kit.planning import (  # noqa: E402
    HarnessPlanner,
    HarnessSOP,
    PlanNotFoundError,
    PlanStore,
    SOPDefinition,
    SOPError,
    TaskGraph,
    TaskGraphError,
    TaskNode,
    TaskStatus,
    build_graph,
    plan_digest,
)
from harness_kit.session import JsonlSessionStore  # noqa: E402
from harness_kit.settings import Settings  # noqa: E402

LIVE: bool = "--live" in sys.argv

#: 本脚本所有临时文件的根（计划文件、会话日志、SOP 状态都落在这里）。
SCRATCH: Path = Path(tempfile.mkdtemp(prefix="lesson12_"))

#: 源码树里那份真实的 SOP 定义。
SOPS_DIR: Path = REF / "harness_kit" / "planning" / "sops"

#: 全程使用的模型名（.env 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。
MODEL_NAME: str = os.getenv("LLM_MODEL") or "deepseek-chat"


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
    """数模型调用次数的中间件（``on_model_call`` 洋葱钩子）。

    它不是"自研中间件链"—— 链仍然是 ``Agent._reply`` 内部那条
    ``execute_chain``（``third_party/agentscope/src/agentscope/agent/_agent.py:945``），
    本类只实现了官方暴露的 ``on_model_call`` 钩子
    （``third_party/agentscope/src/agentscope/middleware/_base.py:213``），
    并且**必须调用 ``next_handler``**，否则模型调用根本不会发生。

    存在的理由只有一个：让"本脚本花了多少次 LLM 调用"变成可断言的数字，
    而不是靠感觉。
    """

    def __init__(self) -> None:
        """初始化计数器。"""
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


class StubAgentLike:
    """一个满足 ``AgentLike`` Protocol 的假执行者 / 假验证者。

    它**不是** ``Agent``，也不需要是：``SOPStep`` 只要求传进来的对象有一个
    ``reply_stream(inputs, structured_schema, yield_final_msg)``
    （``third_party/agentscope/src/agentscope/sop/_schema.py:35`` 的
    ``AgentLike`` Protocol）。这正是 ``PipelineProtocol`` 鸭子类型的收益 ——
    SOP 引擎因此可以在**零模型调用**的前提下被完整测试。

    Args:
        name (`str`): 角色名（会进 ``VerificationResult.verifier``）。
        payloads (`list[dict]`): 每次调用依次弹出的结构化输出。
            弹空之后返回**空 payload**，用来模拟"模型这一轮没给出结构化输出"。
    """

    def __init__(self, name: str, payloads: list[dict[str, Any]]) -> None:
        """初始化。"""
        self.name = name
        self.payloads = list(payloads)
        self.schemas: list[str] = []
        """每次调用请求的 schema 名（``_Handover`` / ``_Verdict``），用于断言。"""

    async def reply_stream(
        self,
        inputs: Any = None,
        structured_schema: Any = None,
        yield_final_msg: bool = False,
    ) -> AsyncGenerator[Msg, None]:
        """吐一条带 ``structured_output`` 的终态 ``Msg``。

        Args:
            inputs (`Any`, optional): 引擎交过来的输入（本 stub 只看不用）。
            structured_schema (`Any`, optional): 请求的结构化 schema。
            yield_final_msg (`bool`, optional): 是否额外 yield 终态消息（透传语义）。

        Yields:
            `Msg`: 一条 ``finished_reason=COMPLETED`` 的消息。
        """
        self.schemas.append(getattr(structured_schema, "__name__", "?"))
        payload = self.payloads.pop(0) if self.payloads else {}
        yield Msg(
            name=self.name,
            role="assistant",
            content=[TextBlock(text="(stub)")],
            finished_reason=ReplyFinishedReason.COMPLETED,
            structured_output=payload,
        )


def build_live_agent() -> tuple[Agent, ModelCallCounter]:
    """造一个真实模型驱动、**没有任何工具**的 Agent。

    **为什么空工具箱**：本讲关心的是"任务怎么拆、怎么排、怎么续"，
    不是"工具怎么调"。给 Agent 挂上内置工具包会让它在拆分阶段就去
    ``run_command``（然后因为没有权限引擎而 park 在 ``RequireUserConfirmEvent`` 上，
    留下一句 "I'm waiting for your permission..."），把整张 DAG 带偏。
    空工具箱 + 纯写作型目标，是让 planner 的语义（而不是权限的语义）
    成为唯一变量的最短路径。

    Returns:
        `tuple[Agent, ModelCallCounter]`: Agent 与挂在它身上的调用计数器。
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
    counter = ModelCallCounter()
    agent = Agent(
        name="planner-demo",
        system_prompt="你是一名务实的写作策划：只产出被要求的东西，不调用工具。",
        model=build_chat_model(spec, settings=settings),
        toolkit=Toolkit(),
        middlewares=[counter],
    )
    return agent, counter


# ======================================================================
# A · TaskGraph 六状态机
# ======================================================================
async def section_a() -> None:
    """A 段：任务 DAG 与状态机（纯计算，0 次 LLM）。"""
    banner("A · TaskGraph：六个状态、拓扑序、阻塞级联与自愈")

    graph = TaskGraph(goal="给一个 Python 库发布 v0.2.0")
    graph.add(TaskNode(id="collect", goal="收集本次改动的全部条目"))
    graph.add(
        TaskNode(id="draft", goal="按规范写出 CHANGELOG 草稿", depends_on=["collect"]),
    )
    graph.add(
        TaskNode(id="verify", goal="校对格式与版本号", depends_on=["draft"]),
    )
    graph.add(
        TaskNode(id="announce", goal="给发布公告写一段摘要", depends_on=["collect"]),
    )
    graph.add(
        TaskNode(id="publish", goal="打 tag 并发布", depends_on=["verify", "announce"]),
    )
    graph.validate()
    print(graph.describe())
    print(f"  初始 ready() = {[_.id for _ in graph.ready()]}")

    banner("A2 · 拓扑序是确定性的（同一张图两次调用逐字相同）")
    first = [_.id for _ in graph.topological_order()]
    second = [_.id for _ in graph.topological_order()]
    print(f"  第 1 次 = {first}")
    print(f"  第 2 次 = {second}")
    assert first == second, "拓扑序必须确定性"
    print("  >>> 平局按「节点插入顺序」决胜，所以断言 first == second 成立。")

    banner("A3 · 一次失败：失败节点钉死，下游级联 blocked")
    graph.mark("collect", TaskStatus.RUNNING)
    graph.mark("announce", TaskStatus.RUNNING)
    graph.mark("collect", TaskStatus.DONE, artifact="本次改动 7 条")
    graph.mark("draft", TaskStatus.RUNNING)
    graph.mark("draft", TaskStatus.FAILED, error="模型连续 3 次给出空产物")
    print(f"  draft 失败后 progress = {graph.progress()}")
    print(f"  ready() = {[_.id for _ in graph.ready()]}（verify/publish 已被冻死）")
    assert graph.node("verify").status is TaskStatus.BLOCKED
    assert graph.node("publish").status is TaskStatus.BLOCKED
    print("  >>> blocked 是**显式状态**而不是调度器的临时判断：")
    print("      落盘之后「还有 3 个非终态节点」与「其中 2 个注定做不成」一眼可分 ——")
    print("      running 是「在飞」，blocked 是「不用等它了」。")
    print(f"  is_complete() = {graph.is_complete()}（announce 还在 running，所以 False）")
    print(f"  注意 is_complete() 问的是「终态收齐了没」，不是「还跑不跑得动」：")
    print(f"      announce 是 running，verify/publish 是 blocked，都还没进终态。")

    banner("A4 · 重规划救活上游：blocked 必须能自愈回 pending")
    # 「把这活重做一遍」是**业务判断**，必须由调用方明说。failed 是终态，
    # mark() 会拒绝把它改回去（见 A5），所以重规划走的是直接改 status 这条路。
    revived = ["draft"]
    for node_id in revived:
        graph.node(node_id).status = TaskStatus.PENDING
        graph.node(node_id).error = None
    graph.refresh_blocked()
    graph.mark("announce", TaskStatus.DONE, artifact="公告摘要 3 行")
    graph.mark("draft", TaskStatus.RUNNING)
    graph.mark("draft", TaskStatus.DONE, artifact="CHANGELOG 草稿（人工补写）")
    print(f"  重规划（把 draft 打回 pending 再重跑）之后 progress = {graph.progress()}")
    assert graph.node("verify").status is TaskStatus.PENDING
    assert graph.node("publish").status is TaskStatus.PENDING
    print(f"  ready() = {[_.id for _ in graph.ready()]}")
    print(f"  draft.attempts = {graph.node('draft').attempts}（重跑一次，attempts 累加到 2）")
    print("  >>> 一次失败不会永久冻死整张图；这就是 A3/A4 两条规则必须同时存在的原因：")
    print("      A3 保证「失败会被看见」，A4 保证「看见之后还能救」。")


async def section_a4b() -> None:
    """A4b 段：阻塞的传递性 —— 只冻第一层等于静默卡死。"""
    banner("A4b · blocked 必须沿依赖边传到底（传递闭包）")

    graph = TaskGraph(goal="一条长链")
    for index in range(1, 5):
        deps = [f"n{index - 1}"] if index > 1 else []
        graph.add(TaskNode(id=f"n{index}", goal=f"第 {index} 环", depends_on=deps))
    graph.mark("n1", TaskStatus.FAILED, error="第一环就挂了")
    print(graph.describe())
    print(f"  progress = {graph.progress()}")
    assert len(graph.by_status(TaskStatus.PENDING)) == 0
    print("  >>> 只冻直接下游的话，n3/n4 会停在 pending：它们永远不会被 ready() 选中，")
    print("      progress() 就会长期报着「还有 2 个任务」，实际是「这 2 个永远不会跑」。")
    print("      传递闭包之后，blocked 集合恰好等于「从失败节点沿依赖边可达」的集合 ——")
    print(f"      也就是 downstream('n1') = {graph.downstream('n1')}。")


async def section_a5a7() -> None:
    """A5~A7 段：终态不可逆、非法图的三种拒绝、串行兜底。"""
    banner("A5 · 终态不可逆：对终态节点再 mark 会抛错，重规划必须由调用方明说")
    graph = build_graph("三步", subtasks=["一", "二", "三"])
    graph.mark("t1", TaskStatus.DONE, artifact="做完了")
    try:
        graph.mark("t1", TaskStatus.RUNNING)
    except TaskGraphError as exc:
        print(f"  对 done 再 mark -> {exc}")
    print("  >>> 「这活我们决定重做」是业务判断，状态机不替你决定；")
    print("      要重做就显式改 node.status = PENDING 再 refresh_blocked()（见 A4）。")

    banner("A6 · 非法图：环、悬空依赖、自依赖")
    cyclic = TaskGraph(goal="成环")
    cyclic.add(TaskNode(id="a", goal="A", depends_on=["b"]))
    cyclic.add(TaskNode(id="b", goal="B", depends_on=["a"]))
    try:
        cyclic.validate()
    except TaskGraphError as exc:
        print(f"  环       -> {exc}")

    dangling = TaskGraph(goal="悬空")
    dangling.add(TaskNode(id="a", goal="A", depends_on=["nope"]))
    try:
        dangling.validate()
    except TaskGraphError as exc:
        print(f"  悬空依赖 -> {exc}")

    self_dep = TaskGraph(goal="自依赖")
    self_dep.add(TaskNode(id="a", goal="A", depends_on=["a"]))
    try:
        self_dep.validate()
    except TaskGraphError as exc:
        print(f"  自依赖   -> {exc}")

    banner("A7 · build_graph：拆解失败时的串行兜底")
    linear = build_graph(
        "写一份 3 段式 README",
        subtasks=["列提纲", "写正文", "校对"],
    )
    print(linear.describe())
    print("  >>> 不传 depends_on 时构造串行链：串行一定能跑，并行优化是后话。")
    print(f"  to_mermaid() 的第一行 = {linear.to_mermaid().splitlines()[0]!r}")


# ======================================================================
# B · PlanStore：落盘 + 断点续跑 + 事件溯源
# ======================================================================
async def section_b() -> None:
    """B 段：计划持久化与断点恢复（0 次 LLM）。"""
    banner("B · PlanStore：原子写盘 / 断点恢复 / 与会话事件日志对接")

    plan_dir = SCRATCH / "plans"
    session_store = JsonlSessionStore(SCRATCH / "sessions")
    store = PlanStore(plan_dir, store=session_store, session_id="sess-12")
    print(f"  store = {store.describe()}")

    graph = build_graph(
        "把 harness_kit/planning 的 docstring 补全",
        subtasks=["读现有代码", "写 graph 的说明", "写 planner 的说明"],
    )
    graph.mark("t1", TaskStatus.RUNNING)
    graph.mark("t1", TaskStatus.DONE, artifact="读完 4 个模块")
    graph.mark("t2", TaskStatus.RUNNING)
    await store.save("plan-doc", graph)
    print(f"  保存后 plans = {await store.list_plans()}")

    record = await store.load_record("plan-doc")
    print(
        f"  revisions = {record.revisions}, digest = {record.digest}, "
        f"updated_at = {record.updated_at}",
    )
    file_path = plan_dir / "plan-doc.json"
    print(f"  落盘文件 = {file_path.name}（{file_path.stat().st_size} 字节，原子替换写入）")

    banner("B2 · 断点续跑：running 归一成 pending，attempts 保留")
    resumed = await store.resume("plan-doc")
    print(resumed.describe())
    assert resumed.node("t1").status is TaskStatus.DONE
    assert resumed.node("t2").status is TaskStatus.PENDING
    assert resumed.node("t2").attempts == 1
    print("  >>> t2 被调度过 1 次（attempts=1）但现在没人跑它 —— 若照原样恢复，")
    print("      调度器会以为「它还在飞」，这一格永远等不到结果，整张图静默卡死。")
    print("      所以恢复时必须把 running 打回 pending，并保留 attempts 供熔断。")

    banner("B3 · 计划快照真的进了会话事件日志（第 9 讲的事件溯源）")
    history = await store.history("plan-doc")
    print(f"  该计划在会话 sess-12 里的快照事件条数 = {len(history)}")
    for event in history:
        data = event.payload["data"]
        print(
            f"    seq={event.seq} kind={event.kind.value} "
            f"name={event.payload['name']} progress={data['progress']}",
        )
    events = await session_store.verify_invariants("sess-12")
    print(f"  会话日志共 {len(events)} 条，seq 不变式（无洞、无重复）校验通过")
    print("  >>> payload 里**不放整张图**（会撑爆日志），只放")
    print("      plan_id / digest / progress / nodes；整张图在 {plan_id}.json 里，")
    print("      靠 digest 关联 —— 一个会话的时间线里因此既有对话也有计划变更。")

    banner("B4 · plan_digest 只含结构与状态，不含时间戳")
    before = plan_digest(resumed)
    await store.save("plan-doc", resumed)
    again = await store.load("plan-doc")
    after = plan_digest(again)
    print(f"  存盘前 digest = {before}")
    print(f"  存盘后 digest = {after}")
    assert before == after, "digest 必须稳定，否则它无法用来判定计划有没有真的变"
    print("  >>> 稳定性的前提是摘要里**不含 created_at / updated_at**，")
    print("      只含 id / goal / status / depends_on / attempts。")

    banner("B5 · 计划 id 防目录穿越 + 不存在时的报错")
    for bad in ("../etc/passwd", "a/b", ".."):
        try:
            store.plan_path(bad)
        except ValueError as exc:
            print(f"  plan_path({bad!r:16s}) -> {exc}")
    try:
        await store.load("no-such-plan")
    except PlanNotFoundError as exc:
        print(f"  load('no-such-plan') -> {str(exc)[:70]}...")

    await session_store.aclose()


# ======================================================================
# C · HarnessSOP：stub 执行者，确定性跑通状态机
# ======================================================================
async def section_c() -> None:
    """C 段：SOP 状态机（用 AgentLike stub，0 次 LLM）。"""
    banner("C · HarnessSOP：YAML → SOPEngine（executor/verifier 是 stub）")

    sop_path = SOPS_DIR / "changelog_entry.yaml"
    definition = SOPDefinition.from_file(sop_path)
    print(f"  定义文件 = {sop_path.relative_to(REF)}")
    print(f"  name     = {definition.name}")
    print(f"  steps    = {[(s.subject, s.max_attempts, s.verify) for s in definition.steps]}")

    banner("C2 · 非法定义会被拦下（缺字段 / 空 steps / 转不成 bool）")
    for raw in (
        {"name": "bad", "steps": [{"subject": "x"}]},
        {"name": "bad", "steps": []},
        {"name": "bad", "steps": [{"subject": "x", "description": "y", "verify": "maybe"}]},
        {"name": "bad", "steps": [{"subject": "x", "description": "y", "max_attempts": 0}]},
    ):
        try:
            SOPDefinition.from_dict(raw)
        except SOPError as exc:
            print(f"  已拒绝：{str(exc).splitlines()[0][:70]}")
    print("  >>> 注意 pydantic 会替你做真值转换：verify: \"yes\" 会被当成 True，")
    print('      所以判据不是"是不是字符串"，而是"能不能解释成 bool"。')

    banner("C3 · 真实定义（两步都 verify: false）：做完即过，不需要判定者")
    executor = StubAgentLike(
        "exec",
        [
            {"handover": "v0.1.0 新增 blocked 状态；升级无需改代码；验证：pytest -k blocked"},
            {"handover": "已发布条目：Added - blocked 状态；验证：pytest -k blocked"},
        ],
    )
    verifier = StubAgentLike("verif", [{"passed": True}])
    sop = HarnessSOP(sop_path=sop_path, agent=executor, verifier=verifier)
    print(f"  装配完成：{len(sop.sop.steps)} 步；current_state（未跑）= {sop.current_state}")

    result = await sop.run(initial_input="为 harness_kit 的 v0.1.0 写一条 CHANGELOG 条目")
    print(f"  phase = {result.phase}  ok = {result.ok}  awaiting = {result.awaiting}")
    for step in result.steps:
        print(
            f"    {step.subject:14s} phase={step.phase:10s} attempts={step.attempts} "
            f"passed={step.passed} msg={step.message[:28]!r}",
        )
    print(f"  output = {result.output[:60]!r}")
    print(f"  executor 被调用 {len(executor.schemas)} 次，schema = {executor.schemas}")
    print(f"  verifier 被调用 {len(verifier.schemas)} 次，schema = {verifier.schemas}")
    print("  >>> 两步都 verify: false → SOPStep(verifier=None)，做完即过。")
    print("      传进来的 verifier 一次都没被叫 —— 「不验证」不是靠空跑，是靠不挂。")
    assert result.ok and result.phase == "completed"
    assert [_.attempts for _ in result.steps] == [1, 1]
    assert verifier.schemas == []

    banner("C3b · 被拒一次 → 重做 → 通过（submission 被清空是这里的关键）")
    strict_spec = SOPDefinition.from_dict(
        {
            "name": "changelog_entry_verified",
            "steps": [
                {
                    "subject": "gather_facts",
                    "description": definition.steps[0].description,
                    "max_attempts": 2,
                    "verify": True,
                },
                {
                    "subject": "write_entry",
                    "description": definition.steps[1].description,
                    "max_attempts": 2,
                    "verify": False,
                },
            ],
        },
    )
    verified_path = SCRATCH / "changelog_entry_verified.json"
    verified_path.write_text(
        json.dumps(strict_spec.model_dump(), ensure_ascii=False),
        encoding="utf-8",
    )
    executor_b = StubAgentLike(
        "exec",
        [
            {"handover": "第一版：只说了新增了 blocked 状态"},
            {"handover": "第二版：新增 blocked；升级无需改代码；验证：pytest -k blocked"},
            {"handover": "已发布条目：Added - blocked 状态；验证：pytest -k blocked"},
        ],
    )
    verifier_b = StubAgentLike(
        "verif",
        [
            {"passed": False, "message": "缺少「升级后要不要改代码」这句"},
            {"passed": True, "message": ""},
        ],
    )
    sop_b = HarnessSOP(sop_path=verified_path, agent=executor_b, verifier=verifier_b)
    result_b = await sop_b.run(initial_input="写一条 CHANGELOG 条目")
    for step in result_b.steps:
        print(
            f"    {step.subject:14s} phase={step.phase:10s} attempts={step.attempts} "
            f"passed={step.passed} msg={step.message[:26]!r}",
        )
    print(f"  executor 被调用 {len(executor_b.schemas)} 次（第 1 步 2 次 + 第 2 步 1 次）")
    print(f"  verifier 被调用 {len(verifier_b.schemas)} 次（第 2 步 verify: false，不叫它）")
    print(f"  第 1 步最终 submission = {result_b.steps[0].submission[:30]!r}")
    print("  >>> record(passed=False) 会**清空 submission**（sop/_schema.py:127），")
    print("      于是第 2 次尝试从「干活」重新开始，而不是从「被判定」开始；")
    print("      被拒的那一版不会被当成产物传下去。拒绝理由则原样回给执行者。")
    assert result_b.ok
    assert result_b.steps[0].attempts == 2 and result_b.steps[1].attempts == 1
    assert result_b.steps[0].submission.startswith("第二版")

    banner("C4 · 尝试预算耗尽 → FAILED（预算是引擎扣的，步骤自己不判断）")
    stubborn = StubAgentLike("exec", [{"handover": f"draft-{i}"} for i in range(6)])
    always_refuse = StubAgentLike(
        "verif",
        [{"passed": False, "message": "还是不行"} for _ in range(6)],
    )
    strict = SOPDefinition.from_dict(
        {
            "name": "strict",
            "steps": [
                {
                    "subject": "draft",
                    "description": "写点什么",
                    "max_attempts": 2,
                    "verify": True,
                },
            ],
        },
    )
    strict_path = SCRATCH / "strict.yaml"
    strict_path.write_text(
        json.dumps(strict.model_dump(), ensure_ascii=False),
        encoding="utf-8",
    )
    strict_sop = HarnessSOP(sop_path=strict_path, agent=stubborn, verifier=always_refuse)
    strict_result = await strict_sop.run(initial_input="随便写")
    print(f"  phase = {strict_result.phase}  failed_steps = {strict_result.failed_steps}")
    print(f"  attempts = {strict_result.steps[0].attempts}（= max_attempts = 2）")
    print("  >>> 判定逻辑在 SOPEngine.reply_stream：")
    print("      `if len(record.verifications) >= step.max_attempts: record.phase = FAILED`")
    print("      （third_party/agentscope/src/agentscope/sop/_engine.py:151）")
    assert strict_result.phase == "failed"

    banner("C5 · 存状态 → 换一个引擎续跑（跨进程续跑的最小充分条件）")
    state_file = SCRATCH / "sop_state.json"
    sop.save_state(state_file, result)
    print(f"  run_state 落盘 = {state_file.name}（{state_file.stat().st_size} 字节）")
    print(f"  run_state 顶层键 = {sorted(result.run_state)}")

    # 新引擎 + 新的执行者：如果状态真的被恢复了，它一次都不该被调用。
    fresh_executor = StubAgentLike("exec", [])
    fresh_verifier = StubAgentLike("verif", [])
    resumed_sop = HarnessSOP(
        sop_path=sop_path,
        agent=fresh_executor,
        verifier=fresh_verifier,
    )
    loaded_state = resumed_sop.load_state(state_file)
    resumed_result = await resumed_sop.run(
        initial_input="（这条输入在续跑时会被忽略吗？）",
        state=loaded_state,
    )
    print(f"  续跑后 phase = {resumed_result.phase}")
    print(f"  续跑时 executor 被调用 {len(fresh_executor.schemas)} 次")
    assert resumed_result.phase == "completed"
    assert not fresh_executor.schemas, "已完成的步骤不该重跑"
    print("  >>> SOPRunState 就是「这轮跑到哪了」的全部；已 COMPLETED 的步骤")
    print("      在引擎里被 `continue` 跳过（sop/_engine.py:111），所以新引擎")
    print("      一次执行者都不用调。")

    banner("C6 · 步骤数与定义不符 → 明确报错，而不是默默跑错")
    edited = SOPDefinition.from_dict(
        {
            "name": "edited",
            "steps": [
                {"subject": "gather_facts", "description": "x", "verify": False},
                {"subject": "new_step", "description": "y", "verify": False},
                {"subject": "one_more", "description": "z", "verify": False},
            ],
        },
    )
    edited_path = SCRATCH / "edited.yaml"
    edited_path.write_text(json.dumps(edited.model_dump(), ensure_ascii=False), "utf-8")
    try:
        await HarnessSOP(sop_path=edited_path, agent=fresh_executor).run(
            initial_input="x",
            state=loaded_state,
        )
    except SOPError as exc:
        print(f"  已拒绝：{exc}")
    print("  >>> SOPEngine.__init__ 的守门（sop/_engine.py:51）：状态里有 2 步、")
    print("      定义里有 3 步 → ValueError；HarnessSOP 把它翻译成 SOPError。")


# ======================================================================
# D · HarnessPlanner（真实 LLM，4 次调用）
# ======================================================================
async def section_d() -> None:
    """D 段：真实模型的拆解 → 拓扑执行 → 汇总（4 次 LLM 调用）。"""
    banner("D · HarnessPlanner：decompose → execute → summarize（--live）")

    agent, counter = build_live_agent()
    planner = HarnessPlanner(agent=agent, max_steps=6)
    goal = (
        "为一个名为 harness_kit.planning 的 Python 模块写一段 4 句话的模块级 "
        "docstring（读者是刚学 Python 的人）。只拆成 2 个子任务。"
    )
    print(f"  目标 = {goal}")

    graph = await planner.decompose(goal)
    print(graph.describe())
    print(f"  拆解出的节点 = {list(graph.nodes)}")
    print(f"  依赖关系     = {[(_.id, _.depends_on) for _ in graph.topological_order()]}")

    result = await planner.execute(graph)
    print()
    for node in result.graph.topological_order():
        print(
            f"    [{node.status.value:7s}] {node.id:22s} attempts={node.attempts} "
            f"artifacts={len(node.artifacts)}",
        )
    print(
        f"  ok = {result.ok}  steps_used = {result.steps_used}  "
        f"failed_nodes = {result.failed_nodes}  replans = {result.replans}",
    )
    print()
    print("  ---- summary（LLM 汇总，若失败会自动退回本地汇总）----")
    print("  " + result.summary[:700].replace("\n", "\n  "))
    print()
    print(f"  本段真实模型调用次数 = {counter.calls}")
    assert counter.calls <= 4, f"D 段预算 4 次，实际 {counter.calls}"
    assert result.ok, f"拆解执行未全部成功：failed={result.failed_nodes}"
    await planner.aclose()


# ======================================================================
# E · HarnessSOP（真实 LLM，2 次调用）
# ======================================================================
async def section_e() -> None:
    """E 段：真实模型跑 YAML SOP（2 次 LLM 调用）。"""
    banner("E · HarnessSOP：真实 deepseek-flash 跑 2 步 YAML SOP（--live）")

    agent, counter = build_live_agent()
    sop_path = SOPS_DIR / "changelog_entry.yaml"
    sop = HarnessSOP(sop_path=sop_path, agent=agent)
    print(f"  SOP = {sop.name}，步骤 = {[s.subject for s in sop.sop.steps]}")

    result = await sop.run(
        initial_input=(
            "本次改动：harness_kit/planning/graph.py 新增了 TaskGraph 的 "
            "blocked 状态，上游节点失败时下游会被显式标成 blocked 而不是静默挂起。"
            "版本 v0.2.0。"
        ),
    )
    print(f"  phase = {result.phase}  ok = {result.ok}  {result.elapsed_ms:.0f}ms")
    for step in result.steps:
        print(
            f"    {step.subject:14s} phase={step.phase:10s} attempts={step.attempts} "
            f"submission={step.submission[:40]!r}",
        )
    print()
    print("  ---- 最终交付物（最后一步的 submission）----")
    print("  " + result.output[:700].replace("\n", "\n  "))
    print()
    print(f"  本段真实模型调用次数 = {counter.calls}")
    assert counter.calls <= 2, f"E 段预算 2 次，实际 {counter.calls}"

    state_file = SCRATCH / "live_sop_state.json"
    sop.save_state(state_file, result)
    print(f"  run_state 已落盘 = {state_file}")


# ======================================================================
# main
# ======================================================================
async def main() -> int:
    """跑完全部段落。

    Returns:
        `int`: 退出码。
    """
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    print(f"scratch = {SCRATCH}")
    print(f"sops    = {SOPS_DIR.relative_to(REF)}")
    print(f"model   = {MODEL_NAME}（仅 --live 段使用）")
    await section_a()
    await section_a4b()
    await section_a5a7()
    await section_b()
    await section_c()
    if LIVE:
        await section_d()
        await section_e()
    else:
        print()
        print("=" * 78)
        print("D / E 段被跳过（没有 --live）。加上 --live 会真实调用 deepseek-flash：")
        print("  D 段 4 次（拆解 1 + 节点 2 + 汇总 1）")
        print("  E 段 2 次（YAML SOP 两步，均 verify: false）")
        print("  合计 6 次，正好是本系列的单脚本上限。")
    print("=" * 78)
    print("ALL SECTIONS DONE")
    print(f"scratch 保留在 {SCRATCH}（内含计划 JSON、会话日志、SOP 运行态）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
