# -*- coding: utf-8 -*-
"""权限层：规则文件 / 引擎与预设 / 人机确认 / 审计（契约 §3.11，第 11 讲）。

四个模块的分工，一句话各自说清"它补的是 AgentScope 的哪个缺口"：

=================================== =========================================================
:mod:`~harness_kit.permission.rules`   **规则文件**。AgentScope 的 ``PermissionEngine``
                                    只能靠 ``engine.add_rule()`` 在代码里加规则
                                    （``third_party/agentscope/src/agentscope/permission/_engine.py:49``），
                                    没有任何磁盘入口。``RuleSet`` 把 YAML 变成原生
                                    ``PermissionRule``，并让 ``default_behavior`` 落地。
:mod:`~harness_kit.permission.policy`  **引擎与预设**。``HarnessPermissionEngine`` 是原生
                                    引擎的子类（判定照旧委托给原生实现），额外做兜底改写与审计；
                                    ``HarnessPermissionMiddleware`` 走中间件扩展点，
                                    让这两件事在真实 Agent 循环里生效。
:mod:`~harness_kit.permission.hitl`    **人机确认**。把 ``RequireUserConfirmEvent`` /
                                    ``UserConfirmResultEvent`` 这对事件接到终端 / HTTP，
                                    并且**失败关闭**：超时、异常、非布尔一律拒绝。
:mod:`~harness_kit.permission.audit`   **审计**。只追加、默认只落 ``sha256`` 摘要，
                                    出事故后能回答"上次为什么放行了"。
=================================== =========================================================

**为什么审计和权限在同一个包里**：审计记录的是**权限决策**，两者共享同一个
:class:`~agentscope.permission.PermissionDecision` 对象；分开会逼着调用方
自己把决策对象传过来传过去，最终一定会有人忘了传。
"""

from harness_kit.permission.audit import (
    AuditEntry,
    AuditLog,
    digest_input,
)
from harness_kit.permission.hitl import (
    ConfirmationTimeout,
    HITLBridge,
    Prompter,
    default_prompter,
)
from harness_kit.permission.policy import (
    PRESETS,
    HarnessPermissionEngine,
    HarnessPermissionMiddleware,
    build_permission_engine,
    preset_engine,
    resolve_mode,
)
from harness_kit.permission.rules import (
    DEFAULT_ORDER,
    MODE_FALLBACK_PREFIX,
    RuleSet,
    RuleSetError,
    build_rulesets,
    is_mode_fallback,
)

__all__ = [
    "DEFAULT_ORDER",
    "MODE_FALLBACK_PREFIX",
    "PRESETS",
    "AuditEntry",
    "AuditLog",
    "ConfirmationTimeout",
    "HITLBridge",
    "HarnessPermissionEngine",
    "HarnessPermissionMiddleware",
    "Prompter",
    "RuleSet",
    "RuleSetError",
    "build_permission_engine",
    "build_rulesets",
    "default_prompter",
    "digest_input",
    "is_mode_fallback",
    "preset_engine",
    "resolve_mode",
]
