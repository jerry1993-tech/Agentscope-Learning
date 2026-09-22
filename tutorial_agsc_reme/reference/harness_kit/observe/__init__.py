# -*- coding: utf-8 -*-
"""可观测性层（第 20 讲）：tracing + metrics。

- :mod:`harness_kit.observe.tracing` —— 进程内 span 树 + JSON 导出，
  装了 OpenTelemetry 就双写上报（``Tracer``）；
- :mod:`harness_kit.observe.metrics` —— ``Counter`` / ``Histogram`` 与
  Prometheus exposition 文本渲染（``MetricsRegistry``）。

这一层的共同约束：**任何外部依赖不可用时都要优雅降级，绝不因为
"没配 collector" 就把主流程炸掉**。契约 §3.20 对 ``Tracer`` 的原话是
"没有配置 exporter 时退化为 no-op，绝不因此报错"。

本模块刻意**不做**重导出：``from harness_kit.observe import Tracer`` 与
``from harness_kit.observe.tracing import Tracer`` 二选一，前者会让
``observe`` 一被 import 就加载整个 tracing 模块（连带 opentelemetry 探测）。
与 ``harness_kit/__init__.py`` 的惰性原则保持一致，这里只放一个惰性表。
"""

from typing import TYPE_CHECKING, Any

__all__ = [
    "Counter",
    "Histogram",
    "MetricsRegistry",
    "Span",
    "SpanStatus",
    "Tracer",
    "current_span",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "Tracer": ("harness_kit.observe.tracing", "Tracer"),
    "Span": ("harness_kit.observe.tracing", "Span"),
    "SpanStatus": ("harness_kit.observe.tracing", "SpanStatus"),
    "current_span": ("harness_kit.observe.tracing", "current_span"),
    "MetricsRegistry": ("harness_kit.observe.metrics", "MetricsRegistry"),
    "Counter": ("harness_kit.observe.metrics", "Counter"),
    "Histogram": ("harness_kit.observe.metrics", "Histogram"),
}


def __getattr__(name: str) -> Any:
    """惰性导入（:pep:`562`）。

    Args:
        name (`str`): 属性名。

    Returns:
        `Any`: 目标对象。

    Raises:
        `AttributeError`: 名字不在导出表内。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}; available: {sorted(_LAZY_EXPORTS)}",
        )
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让 ``dir()`` 同时列出惰性导出项。

    Returns:
        `list[str]`: 排序后的公开名字。
    """
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from harness_kit.observe.metrics import Counter, Histogram, MetricsRegistry
    from harness_kit.observe.tracing import Span, SpanStatus, Tracer, current_span
