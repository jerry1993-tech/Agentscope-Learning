# -*- coding: utf-8 -*-
"""任务拆解与失败重规划（契约 §3.12，第 12 讲）。

**本模块一行 Agent Loop 都不写**。它做的是把 AgentScope 已经写好的三块能力
编排起来：

1. **结构化输出**（拆解）：``Agent.reply(..., structured_schema=_PlanDraft)`` ——
   AgentScope 收到 ``structured_schema`` 后会临时挂上 ``GenerateStructuredOutput``
   工具并在需要时用 ``ToolChoice(mode="GenerateStructuredOutput")`` 强制调用
   （``third_party/agentscope/src/agentscope/agent/_agent.py:1118`` 挂工具、
   ``:3626`` 强制 tool_choice），最后把校验过的 dict 放在
   ``Msg.structured_output``（``.../message/_base.py:107``）。
   我们不解析自由文本、不写 JSON 修补器 —— 那是 AgentScope 的活。
2. **执行**（跑节点）：``Agent.reply_stream(inputs=..., yield_final_msg=True)``，
   逐个节点用自己的 ``UserMsg`` 触发，Agent 的 ReAct 状态机自己决定是否调工具。
3. **执行 + 验收的循环**（单节点多次尝试）：
   ``GoalPipeline``（``.../pipeline/_goal_pipeline.py:55``）—— executor 干活、
   verifier 判定，直到 ``_VerificationResult.result == "pass"`` 或耗尽
   ``max_iters``。**这是 AgentScope 原生的「失败重试直到验收通过」语义**，
   所以当调用方提供了 ``verifier`` 时，本模块直接把整张图交给它跑，
   而不是自己写重试。

**与 AgentScope 的分工边界**：

====================== =============================================
本模块负责              AgentScope 负责
====================== =============================================
DAG 的依赖顺序与状态     单次 reply 的 ReAct 循环
``max_steps`` 全局预算   单次 reply 的 ``max_iters``
失败节点标记与重规划     执行者/验证者的拉扯（``GoalPipeline``）
跨节点的产物传递         工具调用与权限
====================== =============================================
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg

from harness_kit.planning.graph import (
    TaskGraph,
    TaskNode,
    TaskStatus,
)

__all__ = [
    "HarnessPlanner",
    "PlanDraft",
    "PlanResult",
    "SubTaskDraft",
    "VerdictRecorder",
]


# ======================================================================
# 结构化输出的 schema
# ======================================================================
class SubTaskDraft(BaseModel):
    """LLM 拆解出的单个子任务。

    这是**给模型看的 schema**：每个字段都要有 ``description``，
    否则模型只能靠猜（``Field(description=...)`` 会进 ``input_schema``，
    模型看到的就只有描述）。
    """

    id: str = Field(
        description=(
            "子任务的短 id，只用小写字母、数字和下划线，例如 'collect_data'。"
            "同一份拆解里必须唯一。"
        ),
    )
    goal: str = Field(
        description=(
            "这一步要达成什么，一句话，写成能直接交给一个执行者去做的任务。"
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "本步骤依赖的其它子任务 id。没有任何依赖就留空数组。"
            "只能引用同一份拆解里出现过的 id，不能成环。"
        ),
    )
    capability: str = Field(
        default="",
        description=(
            "完成这一步最需要的能力标签，例如 'code' / 'search' / 'write'。"
            "多智能体路由会用它选人；单 Agent 场景留空字符串。"
        ),
    )


class PlanDraft(BaseModel):
    """一次拆解的完整产物。"""

    reasoning: str = Field(
        default="",
        description="为什么这样拆：一句话说明并行/串行的依据。",
    )
    subtasks: list[SubTaskDraft] = Field(
        description="子任务列表，2~8 个为宜；少于 2 个说明该任务不需要拆解。",
    )


class PlanResult(BaseModel):
    """``HarnessPlanner.execute`` 的返回值（契约 §3.12）。"""

    model_config = ConfigDict(extra="forbid")

    graph: TaskGraph
    """执行完（或被预算掐断）的任务图，带每个节点的终态。"""

    steps_used: int = 0
    """实际执行掉的节点数（即被置为 ``running`` 的次数）。"""

    summary: str = ""
    """汇总：由 ``summarize`` 产出，或预算耗尽时的兜底文本。"""

    failed_nodes: list[str] = Field(default_factory=list)
    """终态为 ``failed`` 的节点 id（按拓扑序）。"""

    replans: int = 0
    """发生过的重规划次数。"""

    @property
    def ok(self) -> bool:
        """是否全部成功。

        Returns:
            `bool`: 没有失败节点且图全部 ``done`` 时为 ``True``。
        """
        return not self.failed_nodes and self.graph.is_successful()


# ======================================================================
# Prompt
# ======================================================================
_DECOMPOSE_PROMPT = """\
<system-reminder>You are the planning head of an agent harness. Break the \
goal below into a DAG of concrete subtasks and return them through the \
structured output tool.

Rules:
1. Every subtask must be independently executable by one executor that \
only sees its own goal and the artifacts of the subtasks it depends on.
2. Express parallelism with `depends_on`: two subtasks that do not read each \
other's output must NOT depend on each other.
3. Keep it small: 2 to 6 subtasks. Do not add a "summarize everything" step, \
the harness does that itself.
4. `depends_on` may only contain ids defined in the same answer. No cycles.

<goal>
{goal}
</goal></system-reminder>"""

_NODE_PROMPT = """\
<system-reminder>You are one node of a task DAG. Complete exactly the \
subtask below. Do not do the work of other nodes.

## Your subtask

{goal}

## What your dependencies produced

{artifacts}

When you are done, answer with a short report of what you produced: the \
concrete result, and how someone else can verify it.</system-reminder>"""

_SUMMARIZE_PROMPT = """\
<system-reminder>You are the planning head of an agent harness. Below are \
the per-node reports of a task DAG that has just finished. Produce the final \
answer to the original goal for the user, in the user's language. Merge the \
reports; do not list them one by one. If some nodes failed, say plainly what \
is missing instead of pretending it worked.

## Original goal

{goal}

## Node reports

{reports}</system-reminder>"""

_TOOL_MARKUP_HINTS: tuple[str, ...] = (
    "dsml",
    "<tool_calls",
    "</tool_calls>",
    "｜tool_calls｜",
    "<tool_call>",
)
"""「模型把工具调用写成了文本」的指纹（**启发式**）。

**这不是凭空想的**：真跑 deepseek-flash 时（思考模式下的 function calling 被
写进正文而不是走 API 的 tool_calls 字段），节点产出的「报告」是
``<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="Bash">...`` 这样的原始标记，
而 ``Msg.get_text_content()`` 会把它当**正文**返回。若不拦，它会被
``_execute_node`` 当成产物写进 ``TaskNode.artifacts``、再被
``_artifacts_of`` 拼进下游提示词 —— 下游看到一坨没法读的标记，还以为上游干完了。

命中它只说明「这轮没有产出可用文本」，不代表节点做错了事：典型成因是**执行者
没有可用的工具**（工具箱为空时模型仍会想调工具），正确修法是给
``Agent`` 配 ``Toolkit``，或者把该节点改成不需要工具的写作任务。
"""


def _looks_like_tool_markup(text: str) -> bool:
    """文本里是否出现了工具调用的原始标记。

    Args:
        text (`str`): 模型产出的文本。

    Returns:
        `bool`: 命中任一指纹为 ``True``。

    示例:
        >>> _looks_like_tool_markup('<｜｜DSML｜｜ calls>')
        True
        >>> _looks_like_tool_markup('## 第一节｜任务 DAG 的状态机')
        False
    """
    lowered = text.lower()
    return any(hint in lowered for hint in _TOOL_MARKUP_HINTS)


_REPLAN_PROMPT = """\
<system-reminder>The plan below did not work: some nodes failed. Produce a \
revised DAG that reaches the original goal. You may split a failed node into \
smaller ones, replace its approach, or add a prerequisite it was missing. \
Keep the ids of the nodes that already succeeded and keep their status \
implicit — reuse the same id and the same goal, and the harness will skip \
them.

## Original goal

{goal}

## Current plan (id :: status :: goal)

{plan}

## Failures

{failures}</system-reminder>"""


# ======================================================================
# Planner
# ======================================================================
class VerdictRecorder:
    """给 verifier 套一层薄壳，把它的结构化判定截留下来。

    **为什么需要它**：``GoalPipeline`` 会**把 verifier 的终态 ``Msg`` 吞掉**，
    只 yield 别的部分 —— 判定那一条走的是 ``final_msg = _`` 然后 ``continue``，
    调用方从流里看不到：

    .. code-block:: python

        # third_party/agentscope/src/agentscope/pipeline/_goal_pipeline.py:249
        async for _ in self.verifier.reply_stream(...):
            if isinstance(_, Msg) and _.finished_reason == COMPLETED:
                final_msg = _            # ← 记下来，不 yield
            else:
                yield _

    于是「这个节点到底过没过」从流里拿不到。executor 的 ``_ExecutionReport``
    没这个问题（它是在 yield 之后才被记下的，``:201``）。

    这个类**不改 AgentScope 的行为**：``reply_stream`` 原样转发，只是顺手把
    ``Msg.structured_output`` 记在 :attr:`last_verdict` 上。它靠的是
    ``GoalPipeline`` 对 verifier 只需 ``reply_stream`` + ``state`` 这一事实
    （``.../pipeline/_base.py:15`` 的 ``PipelineProtocol`` 就是为这种鸭子类型
    留的口子）。

    Args:
        agent (`Any`): 真正的 verifier（``Agent``）。
    """

    def __init__(self, agent: Any) -> None:
        """记录 :attr:`last_verdict`，其余属性全部转发给 ``agent``。"""
        self.agent = agent
        self.last_verdict: dict[str, Any] | None = None
        """最近一次 verifier 结构化输出（``{"result": ..., "message": ...}``）。"""

    @property
    def state(self) -> Any:
        """转发 ``AgentState``（``GoalPipeline`` 的 HITL 分支要读 ``reply_id``）。"""
        return getattr(self.agent, "state", None)

    def __getattr__(self, item: str) -> Any:
        """其余属性一律转发（``name`` / ``toolkit`` 等）。"""
        return getattr(self.agent, item)

    async def reply_stream(self, inputs: Any = None, **kwargs: Any):  # noqa: ANN201
        """原样转发 verifier 的流，同时截留结构化判定。

        Args:
            inputs (`Any`, optional): 透传给 verifier。
            **kwargs (`Any`): 透传（``structured_schema`` / ``yield_final_msg``）。

        Yields:
            `AgentEvent | Msg`: verifier 产出的一切，顺序不变。
        """
        async for item in self.agent.reply_stream(inputs=inputs, **kwargs):
            if isinstance(item, Msg) and item.structured_output:
                self.last_verdict = dict(item.structured_output)
            yield item


class HarnessPlanner:
    """任务拆解 + 拓扑执行 + 失败重规划。

    Args:
        agent (`Agent`): 拆解者与默认执行者。**它是 AgentScope 的 Agent 本身**，
            本类不包装、不继承它，只调用它的 ``reply`` / ``reply_stream``。
        max_steps (`int`, defaults to `20`): 整张图最多执行多少个节点。
            这是**跨节点的**预算，与 ``Agent.react_config.max_iters``（单次
            reply 的迭代上限）是两个维度。
        verifier (`Agent | None`, optional): 给了它就为本图构造一条
            :class:`GoalPipeline`（executor=agent、verifier=verifier），
            每个节点都走「执行者做 → 验证者判 → 不合格就带着反馈重做」的循环。
            这是 AgentScope 原生的验收语义，比在本模块里写重试正确得多。
        goal_pipeline (`GoalPipeline | None`, optional): 直接注入一条现成的
            pipeline。给了它就忽略 ``verifier``。**这是 ``PipelineProtocol``
            的鸭子类型收益**：任何实现了 ``reply_stream`` 的对象都能塞进来
            （``.../pipeline/_base.py:15``）。
        replan_on_failure (`bool`, defaults to `False`): 节点失败时是否自动调用
            :meth:`replan`。默认关：重规划要花 LLM 调用，是否值得由调用方决定。
        max_replans (`int`, defaults to `1`): 自动重规划的次数上限。
        max_report_chars (`int`, defaults to `1200`): 单个节点产物写进下游
            提示词时的截断长度（防止上游的长报告吃掉下游的上下文预算）。

    Raises:
        TypeError: ``replan_on_failure`` 为真但 ``max_replans`` 为 0，
            意味着「要求自动重规划但一次都不许做」——静默失效比报错危险。
    """

    def __init__(
        self,
        *,
        agent: Agent,
        max_steps: int = 20,
        verifier: Agent | None = None,
        goal_pipeline: Any | None = None,
        replan_on_failure: bool = False,
        max_replans: int = 1,
        max_report_chars: int = 1200,
    ) -> None:
        """初始化，按需装配 ``GoalPipeline``。"""
        if replan_on_failure and max_replans <= 0:
            raise TypeError(
                "replan_on_failure=True 但 max_replans<=0：重规划永远不会触发，"
                "这是静默失效，直接拒绝。",
            )
        self.agent = agent
        self.max_steps = int(max_steps)
        self.verifier = verifier
        self.replan_on_failure = bool(replan_on_failure)
        self.max_replans = int(max_replans)
        self.max_report_chars = int(max_report_chars)
        self.replans = 0
        self._steps_used = 0

        if goal_pipeline is not None:
            self.pipeline: Any | None = goal_pipeline
            self.recorder: VerdictRecorder | None = None
        elif verifier is not None:
            # 延迟 import：GoalPipeline 在 agentscope.pipeline 里，
            # 顶层 import 会把 pipeline 子模块及其依赖提前拉进来。
            from agentscope.pipeline import GoalPipeline

            # verifier 外面套一层记录器：GoalPipeline 不会 yield verifier 的
            # 终态 Msg（见 VerdictRecorder 的 docstring），判定要从那里读。
            self.recorder = VerdictRecorder(verifier)
            self.pipeline = GoalPipeline(
                executor=agent,
                verifier=self.recorder,
                max_iters=max(1, self.max_steps),
            )
        else:
            self.pipeline = None
            self.recorder = None

    # ==================================================================
    # 拆解
    # ==================================================================
    async def decompose(self, goal: str) -> TaskGraph:
        """把一句目标拆成子任务 DAG。

        走的是 AgentScope 原生的结构化输出：``structured_schema=PlanDraft``。
        产出再交给 :meth:`TaskGraph.from_specs` 校验；**校验失败不回退到
        「猜」**，而是抛 :class:`TaskGraphError` —— 一张非法 DAG 会一路跑到
        调度期才炸，越早拦住越便宜。

        Args:
            goal (`str`): 顶层目标，用户语言即可。

        Returns:
            `TaskGraph`: 校验通过的任务图，``goal`` 字段即入参。

        Raises:
            TaskGraphError: 模型给出的依赖悬空 / 成环 / id 重复。
            RuntimeError: 模型没有产出结构化输出（例如被中断）。
        """
        prompt = _DECOMPOSE_PROMPT.format(goal=goal)
        msg = await self.agent.reply(
            UserMsg("user", prompt),
            structured_schema=PlanDraft,
        )
        if not msg.structured_output:
            raise RuntimeError(
                "拆解失败：Agent 没有产出结构化输出。"
                f"finished_reason={msg.finished_reason!r}, "
                f"text={msg.get_text_content()!r}",
            )
        draft = PlanDraft.model_validate(msg.structured_output)
        logger.info(
            "decompose: {} 个子任务 (reasoning={!r})",
            len(draft.subtasks),
            draft.reasoning[:80],
        )
        graph = TaskGraph.from_specs(
            goal,
            [
                {
                    "id": _sub.id,
                    "goal": _sub.goal,
                    "depends_on": _sub.depends_on,
                    "metadata": {"capability": _sub.capability},
                }
                for _sub in draft.subtasks
            ],
        )
        graph.validate()
        return graph

    # ==================================================================
    # 执行
    # ==================================================================
    async def execute(
        self,
        graph: TaskGraph,
        *,
        summarize: bool = True,
    ) -> PlanResult:
        """按拓扑序执行到没有可推进的节点，或耗尽 ``max_steps``。

        循环体只有一件事：取 :meth:`TaskGraph.ready` 的节点 → 标记 ``running``
        → 交给 :meth:`run_node` → 标记终态。**没有并发**：``Agent`` 的
        ``AgentState`` 是共享可变状态，同一个 Agent 并发 reply 会把上下文
        交错写乱（``.../state/_state.py:209`` 的 ``context`` 是单条列表）。
        要并发请用 :class:`~harness_kit.multiagent.team.AgentTeam` —— 那里每个
        成员是**独立 Agent / 独立 state**，才具备真正的并发前提。

        Args:
            graph (`TaskGraph`): 待执行的任务图。**原地修改**。
            summarize (`bool`, defaults to `True`): 是否在结束时调一次 LLM
                汇总（多花一次调用）。关掉时用本地拼装兜底。

        Returns:
            `PlanResult`: 含终态图、步数、汇总与失败节点。
        """
        graph.validate()
        self._steps_used = 0
        self.replans = 0
        started = time.perf_counter()

        while True:
            if self._steps_used >= self.max_steps:
                logger.warning(
                    "execute: 达到 max_steps={} 预算，停止调度。"
                    "剩余 pending={} blocked={}",
                    self.max_steps,
                    len(graph.by_status(TaskStatus.PENDING)),
                    len(graph.by_status(TaskStatus.BLOCKED)),
                )
                break

            ready = graph.ready()
            if not ready:
                # 没有可跑的：要么全完了，要么剩下的全被 blocked 冻死。
                break

            for node in ready:
                if self._steps_used >= self.max_steps:
                    break
                await self._execute_node(graph, node)

            if self.replan_on_failure:
                failed = [_.id for _ in graph.by_status(TaskStatus.FAILED)]
                if failed and self.replans < self.max_replans:
                    await self.replan(graph, failed=failed)

        failed_nodes = [_ for _ in graph.by_status(TaskStatus.FAILED)]
        summary = (
            await self.summarize(graph)
            if summarize
            else self._local_summary(graph)
        )
        result = PlanResult(
            graph=graph,
            steps_used=self._steps_used,
            summary=summary,
            failed_nodes=[_.id for _ in failed_nodes],
            replans=self.replans,
        )
        logger.info(
            "execute 完成: steps={} ok={} progress={} 用时 {:.0f}ms",
            result.steps_used,
            result.ok,
            graph.progress(),
            (time.perf_counter() - started) * 1000,
        )
        return result

    async def run(self, goal: str, *, summarize: bool = True) -> PlanResult:
        """``decompose`` + ``execute`` 的快捷方式。

        Args:
            goal (`str`): 顶层目标。
            summarize (`bool`, defaults to `True`): 透传给 :meth:`execute`。

        Returns:
            `PlanResult`: 执行结果。
        """
        graph = await self.decompose(goal)
        return await self.execute(graph, summarize=summarize)

    async def _execute_node(self, graph: TaskGraph, node: TaskNode) -> None:
        """跑一个节点并把结果写回图。

        Args:
            graph (`TaskGraph`): 所属图。
            node (`TaskNode`): 待执行节点。
        """
        graph.mark(node.id, TaskStatus.RUNNING)
        self._steps_used += 1
        started = time.perf_counter()
        try:
            ok, output, error = await self.run_node(node, graph)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # 单个节点炸掉不能带走整张图：标记失败、让下游 blocked、
            # 由调用方决定是否重规划。这是「评测/编排」类代码的基本要求。
            logger.exception("节点 {} 执行抛异常", node.id)
            ok, output, error = False, "", f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        if ok:
            graph.mark(
                node.id,
                TaskStatus.DONE,
                artifact=output[: self.max_report_chars],
            )
            logger.info(
                "节点 {} done ({:.0f}ms, {} 字)",
                node.id,
                elapsed,
                len(output),
            )
        else:
            graph.mark(node.id, TaskStatus.FAILED, error=error)
            logger.warning("节点 {} failed: {}", node.id, error)

    async def run_node(
        self,
        node: TaskNode,
        graph: TaskGraph,
    ) -> tuple[bool, str, str | None]:
        """执行单个节点，返回 ``(是否成功, 产物文本, 错误摘要)``。

        两条路径：

        - **有 pipeline**：把节点目标交给 ``GoalPipeline.reply_stream``，
          executor 干、verifier 判，直到 `pass`/`impossible`/耗尽迭代。判定
          ``pass`` 则成功；``impossible`` 或无 verdict 则失败并带上验证者原话。
        - **无 pipeline**：直接 ``agent.reply_stream(inputs=..., yield_final_msg=True)``
          取最后一条 Msg 的文本作为产物。

        Args:
            node (`TaskNode`): 节点。
            graph (`TaskGraph`): 所属图（用于取上游产物）。

        Returns:
            `tuple[bool, str, str | None]`: ``(成功, 产物, 错误)``。
        """
        prompt = _NODE_PROMPT.format(
            goal=node.goal,
            artifacts=self._artifacts_of(node, graph) or "（无，这是起点）",
        )
        if self.pipeline is not None:
            return await self._run_node_via_pipeline(prompt)
        return await self._run_node_via_agent(prompt)

    async def _run_node_via_agent(self, prompt: str) -> tuple[bool, str, str | None]:
        """直接让 Agent 跑一次 reply，取最终文本。

        Args:
            prompt (`str`): 节点提示词。

        Returns:
            `tuple[bool, str, str | None]`: ``(成功, 产物, 错误)``。
        """
        final: Msg | None = None
        async for item in self.agent.reply_stream(
            inputs=UserMsg("user", prompt),
            yield_final_msg=True,
        ):
            if isinstance(item, Msg):
                final = item
        if final is None:
            return False, "", "Agent 没有产出最终消息。"
        text = final.get_text_content() or ""
        if not text.strip():
            return False, "", f"空产物 (finished_reason={final.finished_reason!r})"
        if _looks_like_tool_markup(text):
            # 见 ``_TOOL_MARKUP_HINTS``：这轮的真实产物是「没被执行的一次工具
            # 调用」，不是报告。放它过去会污染下游的上下文（下游读到的是标记），
            # 还会把 blocked 级联的判断建立在假成功上。
            return (
                False,
                text,
                "这轮没有产出可用文本：模型以工具调用结束，但工具箱里没有可执行的"
                "对应工具（给 Agent 配 Toolkit，或把这个节点改成不需要工具的任务）。",
            )
        return True, text, None

    async def _run_node_via_pipeline(
        self,
        prompt: str,
    ) -> tuple[bool, str, str | None]:
        """让 ``GoalPipeline`` 跑一个节点：executor + verifier 循环。

        ``GoalPipeline`` 只 yield 事件与 Msg，**不返回结构化结果对象**，因此
        这里要拼两条信息：

        - executor 的 ``_ExecutionReport.report``（产物）：从流里捞，它是在
          ``yield`` 之后才被记下的，看得见（``.../_goal_pipeline.py:201``）；
        - verifier 的 ``_VerificationResult.result``（判定）：**从流里看不到**
          —— 终态 ``Msg`` 被 pipeline 吞了（``.../_goal_pipeline.py:258``）。
          所以真实 ``GoalPipeline`` 的判定走 :class:`VerdictRecorder`
          （``self.recorder.last_verdict``）；同时保留「流里出现 verdict」的
          兜底，让**自定义 pipeline**（``goal_pipeline=`` 注入的）也能照常工作。

        两者都没有 → 判失败并说明「没见到 verdict」，而不是乐观放行。

        Args:
            prompt (`str`): 节点提示词。

        Returns:
            `tuple[bool, str, str | None]`: ``(成功, 产物, 错误)``。
        """
        report: str | None = None
        verdict: dict[str, Any] | None = None
        if self.recorder is not None:
            # 上一轮的判定不能串到这一轮来。
            self.recorder.last_verdict = None
        async for item in self.pipeline.reply_stream(
            inputs=UserMsg("user", prompt),
        ):
            if not isinstance(item, Msg):
                continue
            structured = item.structured_output or {}
            if "report" in structured:
                report = str(structured["report"])
            elif "result" in structured:
                verdict = dict(structured)

        if verdict is None and self.recorder is not None:
            verdict = self.recorder.last_verdict

        if verdict is None:
            return (
                False,
                report or "",
                "验证者没有给出结论（pipeline 结束但无 verdict）。",
            )
        if verdict.get("result") == "pass":
            return True, report or "", None
        return (
            False,
            report or "",
            f"verifier={verdict.get('result')}: {verdict.get('message', '')}",
        )

    # ==================================================================
    # 重规划
    # ==================================================================
    async def replan(
        self,
        graph: TaskGraph,
        *,
        failed: Sequence[str] | None = None,
    ) -> TaskGraph:
        """失败后重新拆解，并把新方案**合并进原图**。

        合并语义（这三条是重规划不失控的关键）：

        1. 原图中已 ``done`` 的节点 id 一律保留；新方案里同 id 的节点被忽略
           （不重跑已经成功的工作）；
        2. 新方案里出现的**新 id** 直接加进图；
        3. 原图中 ``failed`` 的节点不删（它们是审计记录），只是不再被调度；
           新节点若声明依赖一个**永远不会产出**的失败节点，该依赖会被就地
           丢掉并打一条 warning —— 否则新节点一出生就是 ``blocked``，
           重规划等于没做。

        Args:
            graph (`TaskGraph`): 原图，**原地修改**。
            failed (`Sequence[str] | None`, optional): 失败节点 id；``None``
                时自动取图中所有 ``failed`` 节点。

        Returns:
            `TaskGraph`: 同一张图（就地合并后的）。

        Raises:
            TaskGraphError: 新方案给出的图非法。
        """
        failed_ids = list(failed or [_.id for _ in graph.by_status(TaskStatus.FAILED)])
        failures = "\n".join(
            f"- {node_id}: {graph.node(node_id).error or '未记录原因'}"
            for node_id in failed_ids
        ) or "（无常失败记录）"
        plan_text = "\n".join(
            f"- {_.id} :: {_.status.value} :: {_.goal}"
            for _ in graph.topological_order()
        )
        prompt = _REPLAN_PROMPT.format(
            goal=graph.goal,
            plan=plan_text,
            failures=failures,
        )
        msg = await self.agent.reply(
            UserMsg("user", prompt),
            structured_schema=PlanDraft,
        )
        if not msg.structured_output:
            raise RuntimeError("重规划失败：没有拿到结构化输出。")
        draft = PlanDraft.model_validate(msg.structured_output)

        existing = set(graph.nodes)
        added: list[str] = []
        for spec in draft.subtasks:
            if spec.id in existing:
                logger.debug("replan: 复用已有节点 {}，跳过。", spec.id)
                continue
            # 新节点的依赖：丢掉指向「已失败且不会被替代」的节点的边，
            # 否则新节点一出生就被 blocked（见本方法 docstring 第 3 条）。
            dropped = [
                _
                for _ in spec.depends_on
                if _ in graph.nodes and graph.nodes[_].is_dead
            ]
            if dropped:
                logger.warning(
                    "replan: 新节点 {} 依赖已死亡的 {}，这些边被丢弃。",
                    spec.id,
                    dropped,
                )
            graph.add(
                TaskNode(
                    id=spec.id,
                    goal=spec.goal,
                    depends_on=[_ for _ in spec.depends_on if _ not in dropped],
                    metadata={"capability": spec.capability, "replanned": True},
                ),
            )
            added.append(spec.id)
        graph.validate()
        self.replans += 1
        logger.info(
            "replan #{}: 新增 {} 个节点 {}",
            self.replans,
            len(added),
            added,
        )
        return graph

    # ==================================================================
    # 汇总
    # ==================================================================
    async def summarize(self, graph: TaskGraph) -> str:
        """让 Agent 把各节点产物汇总成对原目标的最终答复。

        Args:
            graph (`TaskGraph`): 执行完的图。

        Returns:
            `str`: 汇总文本；LLM 这一步失败时退回 :meth:`_local_summary`，
            因为「汇总失败」不该让已经做成的活白费。
        """
        reports = "\n\n".join(
            f"### {_.id} ({_.status.value})\n"
            f"goal: {_.goal}\n"
            f"report: {''.join(_.artifacts) or '（无产物）'}"
            for _ in graph.topological_order()
        )
        try:
            msg = await self.agent.reply(
                UserMsg(
                    "user",
                    _SUMMARIZE_PROMPT.format(
                        goal=graph.goal or "（未提供）",
                        reports=reports,
                    ),
                ),
            )
            text = msg.get_text_content() or ""
            if _looks_like_tool_markup(text):
                logger.warning(
                    "summarize 拿到的是工具调用标记（模型把调用写进了正文），"
                    "退回本地汇总 —— 宁可给人看逐节点状态，也不能把标记当答复。"
                )
                return self._local_summary(graph)
            return text.strip() or self._local_summary(graph)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("summarize 失败，退回本地汇总: {}", exc)
            return self._local_summary(graph)

    def _local_summary(self, graph: TaskGraph) -> str:
        """不花 LLM 的本地汇总（预算耗尽 / LLM 失败时的兜底）。

        Args:
            graph (`TaskGraph`): 图。

        Returns:
            `str`: 逐节点的状态与产物。
        """
        lines = [
            f"目标: {graph.goal or '（未提供）'}",
            f"进度: {graph.progress()}",
        ]
        for node in graph.topological_order():
            body = "".join(node.artifacts) or node.error or "（无产物）"
            lines.append(f"- [{node.status.value}] {node.id}: {body}")
        return "\n".join(lines)

    # ==================================================================
    # 工具
    # ==================================================================
    def _artifacts_of(self, node: TaskNode, graph: TaskGraph) -> str:
        """把上游节点的产物拼成给下游看的文本。

        Args:
            node (`TaskNode`): 当前节点。
            graph (`TaskGraph`): 所属图。

        Returns:
            `str`: 形如 ``### t1\\n<产物>``；无上游时是空串。
        """
        chunks: list[str] = []
        for dep_id in node.depends_on:
            dep = graph.node(dep_id)
            body = "".join(dep.artifacts)
            if not body and dep.error:
                body = f"（失败：{dep.error}）"
            if body:
                chunks.append(f"### {dep_id}\n{body}")
        return "\n\n".join(chunks)

    async def aclose(self) -> None:
        """释放上游 Agent（``Agent`` 本身没有 ``aclose``，这里只清引用）。

        保留这个方法是为了让调用方可以统一写 ``finally: await planner.aclose()``，
        不与 :class:`~harness_kit.config.builder.HarnessBuilder.aclose` 的用法分叉。
        """
        self.pipeline = None
