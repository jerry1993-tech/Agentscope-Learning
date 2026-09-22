# -*- coding: utf-8 -*-
"""多 Agent 协作层：路由、团队、交接、闸门（契约 §3.13，第 13 讲）。

四个模块的依赖是**单向**的（``handoff`` → ``team`` → ``router`` / ``limits``），
所以 ``import harness_kit.multiagent`` 永远不会成环：

============================================== ==========================================================
:mod:`~harness_kit.multiagent.limits`           ``SpawnLimiter`` / ``SpawnTicket`` / ``SpawnLimitExceeded``。
                                               纯计数器，**不 import agentscope**。
:mod:`~harness_kit.multiagent.router`           ``CapabilityRouter``：确定性选人。**不 import agentscope**。
:mod:`~harness_kit.multiagent.team`             ``AgentTeam``：把 ``Agent`` 实例、路由表、闸门缝在一起。
                                               依赖 ``agentscope.agent``。
:mod:`~harness_kit.multiagent.handoff`          ``HandoffTool``：让**模型自己**决定把活交给谁。
                                               依赖 ``agentscope.tool`` / ``agentscope.permission``。
============================================== ==========================================================

**边界声明**：AgentScope 2.0.8 的核心 SDK 里**没有子 Agent 原语**。派生能力
只存在于 app 服务层（``agentscope/app/_tool/_agent_create.py:145`` 的
``AgentCreate``、``agentscope/app/_tool/_team_create.py:30`` 的 ``TeamCreate``，
配套 ``agentscope/app/middleware/_team_member_middleware.py:21`` 的
``TeamMemberLoopMiddleware``），而那一层
需要一个跑起来的 ``AgentApp`` 服务与消息总线。本层**不走那条路**：它直接用
``Agent`` + ``ToolBase`` 组合出自带的团队语义，因此可以嵌进任意脚本、任意测试，
不需要起服务、不占端口。真要用 A2A 必须装未安装的 ``a2a`` extra。
"""

from harness_kit.multiagent.handoff import (
    HANDOFF_TOOL_DESCRIPTION,
    HandoffOutcome,
    HandoffProtocol,
    HandoffRequest,
    HandoffTool,
    collect_results,
)
from harness_kit.multiagent.limits import (
    SpawnLimitExceeded,
    SpawnLimiter,
    SpawnLimiterStats,
    SpawnTicket,
)
from harness_kit.multiagent.router import (
    CapabilityRouter,
    MemberScore,
    NoRouteError,
)
from harness_kit.multiagent.team import (
    DEFAULT_HANDOFF_TOOL_NAME,
    AgentTeam,
    TeamResult,
)

__all__ = [
    "AgentTeam",
    "CapabilityRouter",
    "DEFAULT_HANDOFF_TOOL_NAME",
    "HANDOFF_TOOL_DESCRIPTION",
    "HandoffOutcome",
    "HandoffProtocol",
    "HandoffRequest",
    "HandoffTool",
    "MemberScore",
    "NoRouteError",
    "SpawnLimitExceeded",
    "SpawnLimiter",
    "SpawnLimiterStats",
    "SpawnTicket",
    "TeamResult",
    "collect_results",
]
