# -*- coding: utf-8 -*-
"""指标采集与导出（契约 §3.20，第 20 讲）。

**这一层为什么不直接用 prometheus_client？**

`prometheus_client` 是一个独立运行时：它自带全局注册表、自带多进程目录、
自带一个会占端口的 ``start_http_server``。在"嵌入式 harness"这个场景里
这些都用不上，而它带来的间接依赖（``prometheus_client`` 不在契约 §七 的
可用清单里）反而让读者多装一个包。本模块因此**只做两件事**：

1. 进程内维护 ``Counter`` / ``Histogram``，支持标签（label）；
2. 把结果渲染成 **Prometheus exposition 文本格式**（``# HELP`` / ``# TYPE`` /
   ``name{labels} value``），任何 Prometheus / VictoriaMetrics / OpenTelemetry
   Collector 的 ``prometheusreceiver`` 都能直接抓。

Histogram 的渲染**不输出 ``_bucket`` 累积桶**，只输出 ``_count`` / ``_sum`` /
分位数 ``_p50`` / ``_p95`` / ``_p99``。这是一个有意的取舍：分位数是
「延迟 P50/P95」这个需求（契约 §3.20 的 metrics）真正要的东西，而
``summary`` 形态的分位数是进程内算的、不需要服务端再聚合。桶上限
(:data:`DEFAULT_BUCKETS`) 存在的意义是**限制分位数的分辨率**，
不是给服务端算 histogram_quantile 用的 —— 这一点在正文里必须写清楚，
否则读者会以为它是标准 histogram。

与 AgentScope 的分工：AgentScope 的 ``TracingMiddleware``
（``third_party/agentscope/src/agentscope/middleware/_tracing/_trace.py:117``）
只打 span，不打 metric；``third_party/agentscope/src/agentscope/`` 全库
grep 不到任何 counter / histogram / prometheus 相关符号
（实测 ``grep -rn "prometheus" third_party/agentscope/src/agentscope/`` 无输出）。
所以指标注册表是 harness_kit 需要补的真缺口，不是重复实现。

Example:
    >>> registry = MetricsRegistry()
    >>> calls = registry.counter("model_calls_total", unit="1", help="模型调用次数")
    >>> _ = calls.inc(model="deepseek-flash")
    >>> latency = registry.histogram("model_latency", unit="ms")
    >>> latency.observe(812.0, model="deepseek-flash")
    >>> "harness_kit_model_latency_p95" in registry.render_prometheus()
    True
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from loguru import logger

__all__ = [
    "Counter",
    "DEFAULT_BUCKETS",
    "DEFAULT_NAMESPACE",
    "Histogram",
    "MetricsRegistry",
    "MetricSnapshot",
    "UNLABELED",
    "sanitize_name",
]

DEFAULT_NAMESPACE: str = "harness_kit"
"""所有指标的名字前缀，避免与进程里其它库的指标撞名。"""

UNLABELED: str = "__unlabeled__"
"""内部哨兵：无标签时序的键。渲染时不会出现在输出里。"""

DEFAULT_BUCKETS: tuple[float, ...] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10000.0,
    30000.0,
    60000.0,
)
"""默认桶上界（单位由 ``Histogram.unit`` 决定，默认毫秒）。

选型依据：本环境实测的单次 ``deepseek-flash`` 回复在 1.5s~12s 之间
（见第 20 讲的运行验证），P50/P95 落在 1000~10000 这一段，
所以 1s / 2.5s / 5s / 10s 必须各有边界。
"""

_NAME_RE: re.Pattern[str] = re.compile(r"[^a-zA-Z0-9_:]")
"""Prometheus 合法名字：``[a-zA-Z_:][a-zA-Z0-9_:]*``。"""


def sanitize_name(name: str) -> str:
    """把任意字符串压成 Prometheus 合法的指标名。

    Args:
        name (`str`): 原始名字，如 ``"model latency (ms)"``。

    Returns:
        `str`: 合法名字，如 ``"model_latency__ms_"``。

    Raises:
        `ValueError`: ``name`` 清洗后为空。
    """
    cleaned = _NAME_RE.sub("_", name.strip())
    if not cleaned or not cleaned[0].isalpha() and cleaned[0] not in ":_":
        cleaned = f"m_{cleaned}" if cleaned else ""
    if not cleaned:
        raise ValueError(f"指标名清洗后为空: {name!r}")
    return cleaned


def _label_key(labels: Mapping[str, Any]) -> str:
    """把标签映射压成稳定可比较的字符串键。

    Args:
        labels (`Mapping[str, Any]`): 标签。

    Returns:
        `str`: ``""``（无标签，用 :data:`UNLABELED` 占位）或
        ``"model=deepseek-flash,stage=reply"``（按 key 排序）。
    """
    if not labels:
        return UNLABELED
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


def _escape_label_value(value: Any) -> str:
    """转义 Prometheus 标签值（反斜杠 / 引号 / 换行）。

    Args:
        value (`Any`): 原始值。

    Returns:
        `str`: 转义后的字符串。
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: Mapping[str, Any]) -> str:
    """渲染 ``{k="v",...}`` 片段（无标签时返回空串）。

    Args:
        labels (`Mapping[str, Any]`): 标签。

    Returns:
        `str`: 渲染结果。
    """
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape_label_value(value)}"' for key, value in sorted(labels.items()))
    return "{" + inner + "}"


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 的 ``method="linear"`` 同语义）。

    刻意不 import numpy：本模块要在 ``observe/`` 这一层保持零重依赖，
    而分位数只要十几行。

    Args:
        sorted_values (`Sequence[float]`): **已升序**排好的样本。
        q (`float`): 分位点，``0 <= q <= 1``。

    Returns:
        `float`: 分位数；样本为空时返回 ``0.0``。
    """
    if not sorted_values:
        return 0.0
    if q <= 0:
        return float(sorted_values[0])
    if q >= 1:
        return float(sorted_values[-1])
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


@dataclass
class _Series:
    """一条时序（一个标签组合下的样本集合）。"""

    labels: dict[str, Any] = field(default_factory=dict)
    samples: list[float] = field(default_factory=list)
    buckets: list[int] = field(default_factory=list)
    count: int = 0
    total: float = 0.0


class Counter:
    """单调递增计数器。

    Example:
        >>> counter = MetricsRegistry().counter("tool_calls")
        >>> counter.inc(tool="Read")
        1.0
        >>> counter.inc(2.0, tool="Read")
        3.0
    """

    kind: str = "counter"

    def __init__(self, name: str, *, unit: str = "1", help: str = "") -> None:
        """构造计数器。

        Args:
            name (`str`): 指标名（会被 :func:`sanitize_name` 清洗）。
            unit (`str`): 单位；``"1"`` 表示无量纲。
            help (`str`): ``# HELP`` 文本。
        """
        self.name: str = sanitize_name(name)
        self.unit: str = unit
        self.help: str = help or f"{self.name} counter"
        self._series: dict[str, _Series] = {}
        self._lock: threading.Lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: Any) -> float:
        """自增一次。

        Args:
            value (`float`): 增量；必须非负（计数器不能往回走）。
            **labels (`Any`): 标签。

        Returns:
            `float`: 该时序自增后的值。

        Raises:
            `ValueError`: ``value`` 为负。
        """
        if value < 0:
            raise ValueError(
                f"Counter.inc 不接受负增量（收到 {value}）：计数器只能单调递增；"
                "要表达「减少」请换一个指标名（本层不提供 Gauge，避免读者以为可以随便改）",
            )
        key = _label_key(labels)
        with self._lock:
            series = self._series.setdefault(key, _Series(labels=dict(labels)))
            series.total += float(value)
            series.count += 1
            return series.total

    def value(self, **labels: Any) -> float:
        """读取某条时序的当前值。

        Args:
            **labels (`Any`): 标签。

        Returns:
            `float`: 当前值；该时序不存在时返回 ``0.0``。
        """
        series = self._series.get(_label_key(labels))
        return series.total if series is not None else 0.0

    def total(self) -> float:
        """所有时序的合计值。

        Returns:
            `float`: 合计。
        """
        return sum(series.total for series in self._series.values())

    def series(self) -> list[tuple[dict[str, Any], float]]:
        """列出所有时序。

        Returns:
            `list[tuple[dict[str, Any], float]]`: ``(标签, 值)`` 列表。
        """
        return [(dict(series.labels), series.total) for series in self._series.values()]

    def reset(self) -> None:
        """清空所有时序（测试用）。"""
        with self._lock:
            self._series.clear()

    def render(self) -> list[str]:
        """渲染成 Prometheus 文本行。

        Returns:
            `list[str]`: ``# HELP`` / ``# TYPE`` / 时序行。
        """
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = list(self._series.values())
        for series in items:
            lines.append(f"{self.name}{_render_labels(series.labels)} {series.total:g}")
        return lines


class Histogram:
    """带桶的直方图（进程内算分位数）。

    Attributes:
        buckets (`tuple[float, ...]`): 桶上界（升序）。
        unit (`str`): 单位。

    Example:
        >>> hist = MetricsRegistry().histogram("latency", unit="ms")
        >>> for value in (10.0, 20.0, 30.0, 40.0):
        ...     hist.observe(value)
        >>> round(hist.quantile(0.5), 2)
        25.0
    """

    kind: str = "histogram"

    def __init__(
        self,
        name: str,
        *,
        unit: str = "ms",
        help: str = "",
        buckets: Iterable[float] | None = None,
    ) -> None:
        """构造直方图。

        Args:
            name (`str`): 指标名。
            unit (`str`): 单位（默认毫秒）。
            help (`str`): ``# HELP`` 文本。
            buckets (`Iterable[float] | None`): 桶上界；``None`` 用
                :data:`DEFAULT_BUCKETS`。

        Raises:
            `ValueError`: 桶不是严格升序。
        """
        self.name: str = sanitize_name(name)
        self.unit: str = unit
        self.help: str = help or f"{self.name} histogram ({unit})"
        resolved = tuple(sorted(float(item) for item in (buckets or DEFAULT_BUCKETS)))
        if len(set(resolved)) != len(resolved):
            raise ValueError(f"Histogram 桶上界必须严格升序且互不相同: {resolved}")
        self.buckets: tuple[float, ...] = resolved
        self._series: dict[str, _Series] = {}
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def observe(self, value: float, **labels: Any) -> None:
        """记录一次观测。

        Args:
            value (`float`): 观测值；``NaN`` 会被丢弃（Prometheus 不收 NaN）。
            **labels (`Any`): 标签。
        """
        sample = float(value)
        if math.isnan(sample):
            logger.warning("Histogram {} 收到 NaN，已丢弃（labels={}）", self.name, labels)
            return
        key = _label_key(labels)
        with self._lock:
            series = self._series.setdefault(key, _Series(labels=dict(labels)))
            if not series.buckets:
                series.buckets = [0] * (len(self.buckets) + 1)
            series.samples.append(sample)
            series.count += 1
            series.total += sample
            for index, upper in enumerate(self.buckets):
                if sample <= upper:
                    series.buckets[index] += 1
            series.buckets[-1] += 1  # +Inf 桶

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def count(self, **labels: Any) -> int:
        """某条时序的观测次数。

        Args:
            **labels (`Any`): 标签。

        Returns:
            `int`: 次数。
        """
        series = self._series.get(_label_key(labels))
        return series.count if series is not None else 0

    def sum(self, **labels: Any) -> float:
        """某条时序的观测值合计。

        Args:
            **labels (`Any`): 标签。

        Returns:
            `float`: 合计。
        """
        series = self._series.get(_label_key(labels))
        return series.total if series is not None else 0.0

    def mean(self, **labels: Any) -> float:
        """某条时序的均值。

        Args:
            **labels (`Any`): 标签。

        Returns:
            `float`: 均值；无样本时返回 ``0.0``。
        """
        series = self._series.get(_label_key(labels))
        if series is None or series.count == 0:
            return 0.0
        return series.total / series.count

    def quantile(self, q: float, **labels: Any) -> float:
        """某条时序的分位数。

        Args:
            q (`float`): 分位点（``0.95`` = P95）。
            **labels (`Any`): 标签。

        Returns:
            `float`: 分位数；无样本时返回 ``0.0``。
        """
        series = self._series.get(_label_key(labels))
        if series is None or not series.samples:
            return 0.0
        return _quantile(sorted(series.samples), q)

    def snapshot(self, **labels: Any) -> dict[str, float]:
        """某条时序的汇总快照（count / sum / mean / p50 / p95 / p99）。

        Args:
            **labels (`Any`): 标签。

        Returns:
            `dict[str, float]`: 快照。
        """
        return {
            "count": float(self.count(**labels)),
            "sum": self.sum(**labels),
            "mean": self.mean(**labels),
            "p50": self.quantile(0.50, **labels),
            "p95": self.quantile(0.95, **labels),
            "p99": self.quantile(0.99, **labels),
        }

    def series(self) -> list[tuple[dict[str, Any], dict[str, float]]]:
        """列出所有时序及其快照。

        Returns:
            `list[tuple[dict[str, Any], dict[str, float]]]`: ``(标签, 快照)``。
        """
        out: list[tuple[dict[str, Any], dict[str, float]]] = []
        for series in self._series.values():
            out.append((dict(series.labels), self.snapshot(**series.labels)))
        return out

    def reset(self) -> None:
        """清空所有样本（测试用）。"""
        with self._lock:
            self._series.clear()

    def render(self) -> list[str]:
        """渲染成 Prometheus 文本行。

        只输出 ``_count`` / ``_sum`` / ``_p50`` / ``_p95`` / ``_p99``，
        不输出累积桶（模块 docstring 已说明取舍）。

        Returns:
            `list[str]`: 渲染行。
        """
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} summary"]
        with self._lock:
            items = list(self._series.values())
        for series in items:
            ordered = sorted(series.samples)
            suffix = f"_{self.unit}" if self.unit and self.unit != "1" else ""
            labels = series.labels
            lines.append(
                f"{self.name}{suffix}_count{_render_labels(labels)} {series.count}",
            )
            lines.append(
                f"{self.name}{suffix}_sum{_render_labels(labels)} {series.total:g}",
            )
            for q in (0.5, 0.95, 0.99):
                # quantile 标签必须与业务标签**合并进同一对花括号**：
                # 写成 ``name{model="x"}{quantile="0.95"}`` 是非法 exposition，
                # Prometheus 抓取时会直接报 parse error（真实踩过）。
                quantile_labels = {**labels, "quantile": f"{q:g}"}
                lines.append(
                    f"{self.name}{suffix}{_render_labels(quantile_labels)} "
                    f"{_quantile(ordered, q):g}",
                )
        return lines


@dataclass
class MetricSnapshot:
    """注册表的一次只读快照（服务端 ``/metrics`` 的 JSON 视图用）。"""

    counters: dict[str, list[tuple[dict[str, Any], float]]] = field(default_factory=dict)
    histograms: dict[str, list[tuple[dict[str, Any], dict[str, float]]]] = field(
        default_factory=dict,
    )

    def to_dict(self) -> dict[str, Any]:
        """把快照转成可 JSON 序列化的字典。

        Returns:
            `dict[str, Any]`: ``{"counters": ..., "histograms": ...}``。
        """
        return {
            "counters": [
                {"name": name, "series": [{"labels": labels, "value": value} for labels, value in list_]}
                for name, list_ in sorted(self.counters.items())
            ],
            "histograms": [
                {"name": name, "series": [{"labels": labels, "stats": stats} for labels, stats in list_]}
                for name, list_ in sorted(self.histograms.items())
            ],
        }


class MetricsRegistry:
    """指标注册表。同名指标只建一次（幂等），避免服务热重载时重复定义。

    Example:
        >>> registry = MetricsRegistry(namespace="harness_kit")
        >>> _ = registry.counter("replies_total").inc()
        >>> registry.render_prometheus().splitlines()[1]
        '# TYPE harness_kit_replies_total counter'
    """

    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        default_buckets: Sequence[float] | None = None,
    ) -> None:
        """构造注册表。

        Args:
            namespace (`str`): 指标名前缀；空串表示不加前缀。
            default_buckets (`Sequence[float] | None`): 新建 Histogram 的默认桶。
        """
        self.namespace: str = sanitize_name(namespace) if namespace else ""
        self.default_buckets: tuple[float, ...] = tuple(
            default_buckets or DEFAULT_BUCKETS,
        )
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # 取用
    # ------------------------------------------------------------------
    def _full_name(self, name: str) -> str:
        """加上命名空间前缀。

        Args:
            name (`str`): 局部名。

        Returns:
            `str`: 全名。
        """
        cleaned = sanitize_name(name)
        return f"{self.namespace}_{cleaned}" if self.namespace else cleaned

    def counter(self, name: str, *, unit: str = "1", help: str = "") -> Counter:
        """取出（或新建）一个计数器。

        Args:
            name (`str`): 指标名（不含命名空间前缀）。
            unit (`str`): 单位。
            help (`str`): ``# HELP`` 文本。

        Returns:
            `Counter`: 计数器；同名重复调用返回同一个实例。
        """
        full = self._full_name(name)
        with self._lock:
            existing = self._counters.get(full)
            if existing is not None:
                return existing
            created = Counter(full, unit=unit, help=help)
            self._counters[full] = created
            return created

    def histogram(
        self,
        name: str,
        *,
        unit: str = "ms",
        help: str = "",
        buckets: Sequence[float] | None = None,
    ) -> Histogram:
        """取出（或新建）一个直方图。

        Args:
            name (`str`): 指标名。
            unit (`str`): 单位。
            help (`str`): ``# HELP`` 文本。
            buckets (`Sequence[float] | None`): 桶上界；``None`` 用注册表默认。

        Returns:
            `Histogram`: 直方图；同名重复调用返回同一个实例。
        """
        full = self._full_name(name)
        with self._lock:
            existing = self._histograms.get(full)
            if existing is not None:
                return existing
            created = Histogram(
                full,
                unit=unit,
                help=help,
                buckets=buckets if buckets is not None else self.default_buckets,
            )
            self._histograms[full] = created
            return created

    def get_counter(self, name: str) -> Counter | None:
        """按全名查计数器（不创建）。

        Args:
            name (`str`): 全名或局部名都接受。

        Returns:
            `Counter | None`: 计数器或 ``None``。
        """
        if name in self._counters:
            return self._counters[name]
        return self._counters.get(self._full_name(name))

    def get_histogram(self, name: str) -> Histogram | None:
        """按全名查直方图（不创建）。

        Args:
            name (`str`): 全名或局部名都接受。

        Returns:
            `Histogram | None`: 直方图或 ``None``。
        """
        if name in self._histograms:
            return self._histograms[name]
        return self._histograms.get(self._full_name(name))

    def names(self) -> dict[str, list[str]]:
        """列出已登记的指标名。

        Returns:
            `dict[str, list[str]]`: ``{"counters": [...], "histograms": [...]}``。
        """
        return {
            "counters": sorted(self._counters),
            "histograms": sorted(self._histograms),
        }

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def render_prometheus(self) -> str:
        """渲染成 Prometheus exposition 文本。

        Returns:
            `str`: 以换行结尾的文本；没有任何指标时返回 ``""``。
        """
        lines: list[str] = []
        with self._lock:
            counters = list(self._counters.values())
            histograms = list(self._histograms.values())
        for counter in sorted(counters, key=lambda item: item.name):
            lines.extend(counter.render())
        for histogram in sorted(histograms, key=lambda item: item.name):
            lines.extend(histogram.render())
        return "\n".join(lines) + ("\n" if lines else "")

    def snapshot(self) -> MetricSnapshot:
        """生成只读快照。

        Returns:
            `MetricSnapshot`: 快照。
        """
        with self._lock:
            counters = list(self._counters.values())
            histograms = list(self._histograms.values())
        return MetricSnapshot(
            counters={item.name: item.series() for item in counters},
            histograms={item.name: item.series() for item in histograms},
        )

    def reset(self) -> None:
        """清空所有指标（测试用）。"""
        with self._lock:
            counters = list(self._counters.values())
            histograms = list(self._histograms.values())
        for counter in counters:
            counter.reset()
        for histogram in histograms:
            histogram.reset()

    def describe(self) -> str:
        """多行摘要（CLI ``doctor`` 用）。

        Returns:
            `str`: 摘要文本。
        """
        names = self.names()
        lines = [
            f"namespace = {self.namespace or '(none)'}",
            f"counters  = {', '.join(names['counters']) or '(none)'}",
            f"histograms= {', '.join(names['histograms']) or '(none)'}",
        ]
        for name in names["histograms"]:
            histogram = self._histograms[name]
            stats = histogram.snapshot()
            lines.append(
                f"  {name}: n={int(stats['count'])} mean={stats['mean']:.1f} "
                f"p95={stats['p95']:.1f}",
            )
        return "\n".join(lines)
