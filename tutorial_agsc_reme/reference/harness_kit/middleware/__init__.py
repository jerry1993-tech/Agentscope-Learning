# -*- coding: utf-8 -*-
"""harness_kit 的中间件层（契约 §3.8，第 8 讲）。

导出两组东西：

- **基座**（:mod:`harness_kit.middleware.base`）—— :class:`HarnessMiddleware`
  与四个组合辅助函数。写新中间件时只需要这几个；
- **五个开箱中间件** —— ``LoggingMiddleware`` / ``BudgetMiddleware`` /
  ``RedactMiddleware`` / ``TracingMiddleware`` / ``GuardsMiddleware``，
  分别对应 ``harness_kit/registry.py`` 里 ``_register_middlewares``
  登记的 5 个名字（``logging`` / ``budget`` / ``redact`` / ``tracing`` / ``guards``）。
  Profile 里写 ``middleware: [{name: guards, params: {...}}]``，
  :class:`~harness_kit.config.builder.HarnessBuilder` 就按这些名字实例化。

**这里没有 ``__getattr__`` 惰性导入**，与 ``harness_kit/__init__.py`` 的取舍不同：
中间件模块本来就 import agentscope / loguru，本包在被导入的那一刻，
调用方显然已经在 AgentScope 的地界里了，再拖一次没有收益。
"""

from harness_kit.middleware.base import (
    HOOK_NAMES,
    STREAM_HOOKS,
    VALUE_HOOKS,
    HarnessMiddleware,
    call_next,
    call_next_stream,
    filter_by_hook,
    implemented_hooks,
    onion_order,
    tool_schemas,
)
from harness_kit.middleware.budget import (
    BudgetExceededError,
    BudgetMiddleware,
    BudgetUsage,
)
from harness_kit.middleware.compact import (
    CompactionRecord,
    ContextBudgetSpec,
    ContextCompactionMiddleware,
    build_context_config,
    count_pending_tool_calls,
)
from harness_kit.middleware.guards import (
    GuardPattern,
    GuardsMiddleware,
    GuardTrippedError,
    default_injection_patterns,
)
from harness_kit.middleware.logging import LoggingMiddleware
from harness_kit.middleware.redact import (
    RedactMiddleware,
    RedactPattern,
    default_patterns,
    redact_text,
)
from harness_kit.middleware.tracing import (
    EventBusLike,
    SpanHandle,
    SpanRecord,
    TracingMiddleware,
)

__all__ = [
    "HOOK_NAMES",
    "STREAM_HOOKS",
    "VALUE_HOOKS",
    "BudgetExceededError",
    "BudgetMiddleware",
    "BudgetUsage",
    "CompactionRecord",
    "ContextBudgetSpec",
    "ContextCompactionMiddleware",
    "EventBusLike",
    "GuardPattern",
    "GuardTrippedError",
    "GuardsMiddleware",
    "HarnessMiddleware",
    "LoggingMiddleware",
    "RedactMiddleware",
    "RedactPattern",
    "SpanHandle",
    "SpanRecord",
    "TracingMiddleware",
    "build_context_config",
    "call_next",
    "call_next_stream",
    "count_pending_tool_calls",
    "default_injection_patterns",
    "default_patterns",
    "filter_by_hook",
    "implemented_hooks",
    "onion_order",
    "redact_text",
    "tool_schemas",
]
