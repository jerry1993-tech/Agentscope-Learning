# -*- coding: utf-8 -*-
"""编排层：任务 DAG、拆解与重规划、SOP、计划持久化（契约 §3.12，第 12 讲）。

四个模块职责严格分层，**上层依赖下层，不反向**：

============================================== ==========================================================
:mod:`~harness_kit.planning.graph`             纯数据结构 + 拓扑算法。``TaskNode`` / ``TaskGraph`` /
                                               ``TaskStatus``。**不 import agentscope。**
:mod:`~harness_kit.planning.resume`            ``PlanStore``：DAG 落盘 + 与会话事件日志（第 9 讲）对接。
                                               只依赖 ``graph`` 与 ``events``。
:mod:`~harness_kit.planning.sop`               ``HarnessSOP``：YAML 定义 → AgentScope ``SOP`` /
                                               ``SOPEngine``。依赖 ``agentscope.sop``。
:mod:`~harness_kit.planning.planner`           ``HarnessPlanner``：拆解（结构化输出）→ 拓扑执行 →
                                               重规划 → 汇总。依赖 ``agentscope.agent`` 与
                                               ``agentscope.pipeline``。
============================================== ==========================================================

**为什么 ``graph`` 不 import agentscope**：DAG 是纯计算，把它绑上 Agent 会让
「单测一张图的状态机」变成「起一个 Agent 才能测」。第 12 讲的测试用例里有一半
是纯同步断言，靠的就是这条边界。
"""

from harness_kit.planning.graph import (
    TERMINAL_STATUSES,
    TaskGraph,
    TaskGraphError,
    TaskNode,
    TaskStatus,
    TaskStatusLiteral,
    build_graph,
)
from harness_kit.planning.planner import (
    HarnessPlanner,
    PlanDraft,
    PlanResult,
    SubTaskDraft,
    VerdictRecorder,
)
from harness_kit.planning.resume import (
    DEFAULT_PLAN_DIRNAME,
    PlanNotFoundError,
    PlanRecord,
    PlanStore,
    plan_digest,
)
from harness_kit.planning.sop import (
    HarnessSOP,
    SOPDefinition,
    SOPError,
    SOPResult,
    SOPStepResult,
    SOPStepSpec,
)

__all__ = [
    "DEFAULT_PLAN_DIRNAME",
    "HarnessPlanner",
    "HarnessSOP",
    "PlanDraft",
    "PlanNotFoundError",
    "PlanRecord",
    "PlanResult",
    "PlanStore",
    "SOPDefinition",
    "SOPError",
    "SOPResult",
    "SOPStepResult",
    "SOPStepSpec",
    "SubTaskDraft",
    "TERMINAL_STATUSES",
    "TaskGraph",
    "TaskGraphError",
    "TaskNode",
    "TaskStatus",
    "TaskStatusLiteral",
    "VerdictRecorder",
    "build_graph",
    "plan_digest",
]
