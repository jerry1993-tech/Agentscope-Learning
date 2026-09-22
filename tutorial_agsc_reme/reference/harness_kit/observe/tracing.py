# -*- coding: utf-8 -*-
"""轻量 tracing：进程内 span 树 + JSON 导出，装了 OpenTelemetry 就上报（第 20 讲）。

**为什么不直接用 AgentScope 的 ``TracingMiddleware``？**

它在 ``third_party/agentscope/src/agentscope/middleware/_tracing/_trace.py:143``
的第一件事是 ``if not _check_tracing_enabled(): return``，而
``_check_tracing_enabled``（同文件 ``:59``）要求全局 ``TracerProvider`` 已经是
SDK 实现（要先调 ``agentscope.setup_tracing``）。**没配 OTel 时它是个彻底的
no-op**，本地开发与"线上 collector 挂了"两种情况都留不下任何痕迹。

本模块的策略是**双写、可降级**（与
``harness_kit/middleware/tracing.py`` 的中间件版同思路，但这一层是**库**，
不依赖任何 AgentScope 对象，因此 eval / service / demo 都能直接用）：

1. **始终**在进程内维护一棵 span 树（``contextvars`` 栈 + :class:`Span`），
   :meth:`Tracer.to_json` 随时落盘，零外部依赖；
2. 若显式要求（``exporter="otlp"`` / ``"console"``，或直接注入一个
   ``otel_exporter``），再开真实的 OTel span。**任何一步失败都退化为纯进程内**，
   只打 warning，绝不抛异常 —— 这是契约 §3.20 对 ``Tracer`` 的硬要求
   （"没有配置 exporter 时退化为 no-op，绝不因此报错"）。

两个必须说清楚的实现约束（与 ``harness_kit/middleware/tracing.py`` 一致）：

1. ``ContextVar`` 的可见性跟着 asyncio 执行上下文走。``asyncio.create_task``
   出来的子任务会**拷到**创建时刻的上下文，因此子任务里的 span 会把当时
   栈顶的 span 认作父节点（这正是我们要的）；反过来，父任务随后新压的 span
   不会凭空出现在已创建的子任务里。
2. span 的收尾必须是**同步**的。``@contextmanager`` 在 ``GeneratorExit``
   （消费方提前 break）时也会走 ``finally``，此时里面再 ``await`` 会炸；
   所以 :meth:`Tracer.finish_span` 是同步方法，OTel 那边的 ``span.end()``
   同样是同步调用，没有任何 await 点。

Example:
    >>> tracer = Tracer(service_name="demo")
    >>> with tracer.span("reply", model="deepseek-flash") as span:
    ...     span.add_event("first_token")
    ...     span.set_attribute("iterations", 2)
    >>> span.status
    <SpanStatus.OK: 'ok'>
    >>> tracer.to_dict()["spans"][0]["name"]
    'reply'
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from loguru import logger

__all__ = [
    "EXPORTER_KINDS",
    "Span",
    "SpanEvent",
    "SpanStatus",
    "Tracer",
    "current_span",
    "new_trace_id",
]

_MAX_ATTRIBUTE_CHARS: int = 2000
"""单个属性值的最大字符数；超长截断，避免把整段 prompt 塞进 trace。"""


def _now_utc() -> datetime:
    """返回当前 UTC 时间（tz-aware）。

    Returns:
        `datetime`: 带 ``timezone.utc`` 的当前时间。
    """
    return datetime.now(timezone.utc)


def new_trace_id() -> str:
    """生成一个新的 trace id。

    Returns:
        `str`: 32 位十六进制字符串（与 OTel 的 trace id 同宽度）。
    """
    return uuid4().hex


class SpanStatus(StrEnum):
    """span 的终态。与 OTel 的 ``StatusCode`` 同名同值。"""

    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


def _clip(value: Any) -> Any:
    """把属性值压成可 JSON 序列化且不超长的形态。

    Args:
        value (`Any`): 原始值。

    Returns:
        `Any`: ``str`` 会截断；``int`` / ``float`` / ``bool`` / ``None`` 原样；
        其余转成 ``repr`` 并截断。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_ATTRIBUTE_CHARS else value[:_MAX_ATTRIBUTE_CHARS] + "…"
    text = repr(value)
    return text if len(text) <= _MAX_ATTRIBUTE_CHARS else text[:_MAX_ATTRIBUTE_CHARS] + "…"


@dataclass
class SpanEvent:
    """span 内的一次瞬时事件（对应 OTel 的 ``Span.add_event``）。"""

    name: str
    ts: datetime = field(default_factory=_now_utc)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。

        Returns:
            `dict[str, Any]`: 字典。
        """
        return {
            "name": self.name,
            "ts": self.ts.isoformat(),
            "attributes": dict(self.attributes),
        }


@dataclass
class Span:
    """一个进程内 span。

    Attributes:
        name (`str`): span 名。
        trace_id (`str`): 所属 trace。
        span_id (`str`): 自身 id。
        parent_id (`str | None`): 父 span id。
        start_ns (`int`): 起始时刻（``time.perf_counter_ns``）。
    """

    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid4().hex[:16])
    parent_id: str | None = None
    start_ns: int = field(default_factory=time.perf_counter_ns)
    start_time: datetime = field(default_factory=_now_utc)
    end_ns: int | None = None
    end_time: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[SpanEvent] = field(default_factory=list)
    status: SpanStatus = SpanStatus.UNSET
    error: str | None = None
    service_name: str = "harness-kit"

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def set_attribute(self, key: str, value: Any) -> None:
        """写入一个属性。

        Args:
            key (`str`): 属性名。
            value (`Any`): 属性值（会被 :func:`_clip` 处理）。
        """
        self.attributes[key] = _clip(value)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        """批量写入属性。

        Args:
            attributes (`Mapping[str, Any]`): 属性映射。
        """
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def add_event(self, name: str, **attributes: Any) -> SpanEvent:
        """记录一个瞬时事件。

        Args:
            name (`str`): 事件名。
            **attributes (`Any`): 事件属性。

        Returns:
            `SpanEvent`: 记录下来的事件。
        """
        event = SpanEvent(
            name=name,
            attributes={key: _clip(value) for key, value in attributes.items()},
        )
        self.events.append(event)
        return event

    def record_exception(self, exc: BaseException) -> None:
        """记录一个异常（同时把状态置为 ERROR）。

        Args:
            exc (`BaseException`): 异常对象。
        """
        self.status = SpanStatus.ERROR
        self.error = f"{type(exc).__name__}: {exc}"
        self.add_event("exception", type=type(exc).__name__, message=str(exc))

    def set_status(self, status: SpanStatus, description: str | None = None) -> None:
        """设置终态。

        Args:
            status (`SpanStatus`): 目标状态。
            description (`str | None`): 可选描述；给定时并入 ``error`` 字段。
        """
        self.status = status
        if description:
            self.error = description

    def end(self, *, status: SpanStatus | None = None) -> None:
        """结束 span（幂等）。

        Args:
            status (`SpanStatus | None`): 显式终态；``None`` 时保持现状，
                若从未设过则落成 ``OK``。
        """
        if self.end_ns is None:
            self.end_ns = time.perf_counter_ns()
            self.end_time = _now_utc()
        if status is not None:
            self.status = status
        elif self.status is SpanStatus.UNSET:
            self.status = SpanStatus.OK

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    @property
    def duration_ms(self) -> float:
        """持续时间（毫秒）。

        Returns:
            `float`: 已结束的用真实耗时；未结束的用"到此刻为止"的耗时。
        """
        span_ns = self.end_ns if self.end_ns is not None else time.perf_counter_ns()
        return (span_ns - self.start_ns) / 1_000_000

    @property
    def is_recording(self) -> bool:
        """是否仍在进行中。

        Returns:
            `bool`: 未结束为 ``True``。
        """
        return self.end_ns is None

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。

        Returns:
            `dict[str, Any]`: 字典。
        """
        return {
            "name": self.name,
            "service": self.service_name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status.value,
            "error": self.error,
            "attributes": dict(self.attributes),
            "events": [event.to_dict() for event in self.events],
        }


_CURRENT_SPAN: ContextVar[Span | None] = ContextVar("harness_kit_current_span", default=None)
"""当前 span 的栈顶。用 ``ContextVar`` 而不是线程局部，因为整套是异步的。"""


def current_span() -> Span | None:
    """返回当前执行上下文里最内层的 span。

    Returns:
        `Span | None`: 当前 span；不在任何 span 里时返回 ``None``。
    """
    return _CURRENT_SPAN.get()


EXPORTER_KINDS: tuple[str, ...] = ("none", "console", "otlp", "custom")
"""支持的 exporter 取值。

- ``none`` / ``None``：只留进程内 span 树（默认）；
- ``console``：把 span 打到 OTel 的 console exporter（本地调试）；
- ``otlp``：推到 ``OTEL_EXPORTER_OTLP_ENDPOINT`` 指定的 collector；
- ``custom``：由调用方通过 ``otel_exporter`` 注入一个 ``SpanExporter``。
"""


class Tracer:
    """进程内 span 树的持有者，可选双写 OpenTelemetry。

    Example:
        >>> tracer = Tracer(service_name="harness-kit", exporter=None)
        >>> with tracer.span("build_agent", tools=6) as span:
        ...     pass
        >>> tracer.span_count()
        1
    """

    def __init__(
        self,
        *,
        service_name: str = "harness-kit",
        exporter: str | None = None,
        otel_exporter: Any | None = None,
        max_spans: int = 5000,
        record_otel: bool = True,
    ) -> None:
        """构造 tracer。

        Args:
            service_name (`str`): 服务名，写进 ``Resource`` 与每个 span。
            exporter (`str | None`): 取值见 :data:`EXPORTER_KINDS`。
                未知取值会打 warning 并退化为 ``none``（**不抛异常**）。
            otel_exporter (`Any | None`): 直接注入的 OTel ``SpanExporter``
                （例如测试用的 ``InMemorySpanExporter``）；给定即等价于
                ``exporter="custom"``。
            max_spans (`int`): 进程内保留的 span 上限，超出后丢弃最旧的
                （防止长跑服务内存无界增长）。
            record_otel (`bool`): 是否尝试建立 OTel 通道。``False`` 时
                无论 ``exporter`` 是什么都只留进程内 span 树。

        Raises:
            `ValueError`: ``max_spans <= 0``。
        """
        if max_spans <= 0:
            raise ValueError(f"max_spans 必须为正，收到 {max_spans}")
        self.service_name: str = service_name
        self.max_spans: int = int(max_spans)
        self._spans: list[Span] = []
        self._otel_tracer: Any | None = None
        self._otel_provider: Any | None = None
        # 每个实例独享的两张表：id(span) -> ContextVar token / OTel span。
        # 必须放在 __init__ 里，写成类属性会让所有 Tracer 实例共享同一张表，
        # 并发时互相 reset 别人的 token（真实踩过）。
        self._tokens: dict[int, Token[Span | None]] = {}
        self._otel_span_ids: dict[int, Any] = {}

        requested = (exporter or "none").strip().lower()
        if otel_exporter is not None:
            requested = "custom"
        if requested not in EXPORTER_KINDS:
            logger.warning(
                "未知的 tracing exporter {!r}（可选 {}），退化为进程内 span 树",
                exporter,
                list(EXPORTER_KINDS),
            )
            requested = "none"
        self.exporter_kind: str = requested

        if record_otel and requested != "none":
            self._setup_otel(otel_exporter)

    # ------------------------------------------------------------------
    # OTel 装配（失败即降级）
    # ------------------------------------------------------------------
    def _setup_otel(self, injected: Any | None) -> None:
        """尝试建立 OTel 通道；任何失败都退化为纯进程内。

        Args:
            injected (`Any | None`): 调用方注入的 ``SpanExporter``。
        """
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            logger.warning(
                "未安装 opentelemetry-sdk（{}），tracing 退化为进程内 span 树",
                exc,
            )
            self.exporter_kind = "none"
            return

        span_exporter = injected
        try:
            if span_exporter is None and self.exporter_kind == "console":
                from opentelemetry.sdk.trace.export import ConsoleSpanExporter

                span_exporter = ConsoleSpanExporter()
            if span_exporter is None and self.exporter_kind == "otlp":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                span_exporter = OTLPSpanExporter()

            provider = TracerProvider(
                resource=Resource.create({"service.name": self.service_name}),
            )
            processor = (
                SimpleSpanProcessor(span_exporter)
                if self.exporter_kind in ("console", "custom")
                else BatchSpanProcessor(span_exporter)
            )
            provider.add_span_processor(processor)
            self._otel_provider = provider
            self._otel_tracer = provider.get_tracer(self.service_name)
            logger.bind(service=self.service_name, exporter=self.exporter_kind).info(
                "OTel tracing 已启用",
            )
        except Exception as exc:  # noqa: BLE001 - 追踪永远不该炸主流程
            logger.warning(
                "OTel tracing 装配失败（{}: {}），退化为进程内 span 树",
                type(exc).__name__,
                exc,
            )
            self._otel_tracer = None
            self._otel_provider = None
            self.exporter_kind = "none"
            return

        # 让 agentScope 的官方 TracingMiddleware 也能看到这个 provider
        try:
            otel_trace.set_tracer_provider(self._otel_provider)
        except Exception:  # pragma: no cover - 已设置过时 OTel 会拒绝
            logger.debug("全局 TracerProvider 已被占用，harness Tracer 仍按自身 provider 上报")

    # ------------------------------------------------------------------
    # span 生命周期
    # ------------------------------------------------------------------
    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent: Span | None = None,
        **attributes: Any,
    ) -> Span:
        """开始一个 span 并把它压入当前执行上下文的栈顶。

        调用方**必须**配一个 :meth:`finish_span`（或用 :meth:`span`）。

        Args:
            name (`str`): span 名。
            trace_id (`str | None`): 显式 trace id；``None`` 时沿用父 span。
            parent (`Span | None`): 显式父 span；``None`` 时取当前栈顶。
            **attributes (`Any`): 初始属性。

        Returns:
            `Span`: 新 span。

        Raises:
            `ValueError`: ``name`` 为空。
        """
        if not name.strip():
            raise ValueError("span 名不能为空")
        resolved_parent = parent if parent is not None else current_span()
        span = Span(
            name=name,
            trace_id=trace_id or (resolved_parent.trace_id if resolved_parent else new_trace_id()),
            parent_id=resolved_parent.span_id if resolved_parent else None,
            service_name=self.service_name,
        )
        span.set_attributes(attributes)
        self._spans.append(span)
        if len(self._spans) > self.max_spans:
            self._spans = self._spans[-self.max_spans :]
        self._push(span)
        return span

    def _push(self, span: Span) -> None:
        """把 span 设为当前（同时把 token 存到 span 上供 pop 用）。

        Args:
            span (`Span`): 目标 span。
        """
        token: Token[Span | None] = _CURRENT_SPAN.set(span)
        self._tokens[id(span)] = token
        if self._otel_tracer is not None:
            try:
                self._otel_span_ids[id(span)] = self._otel_tracer.start_span(span.name)
            except Exception:  # pragma: no cover - 上报侧失败不影响主流程
                logger.debug("OTel start_span 失败，仅保留进程内 span: {}", span.name)

    def finish_span(
        self,
        span: Span,
        *,
        status: SpanStatus | None = None,
        error: BaseException | None = None,
    ) -> Span:
        """结束 span 并恢复栈顶（幂等、同步）。

        Args:
            span (`Span`): 待结束的 span。
            status (`SpanStatus | None`): 终态。
            error (`BaseException | None`): 若给了异常，先 ``record_exception``。

        Returns:
            `Span`: 同一个 span（便于链式调用）。
        """
        if error is not None:
            span.record_exception(error)
        span.end(status=status)

        token = self._tokens.pop(id(span), None)
        if token is not None:
            try:
                _CURRENT_SPAN.reset(token)
            except ValueError:  # pragma: no cover - token 属于别的上下文
                logger.debug("span token 不在当前上下文，跳过 reset")

        otel_span = self._otel_span_ids.pop(id(span), None)
        if otel_span is not None:
            try:
                for key, value in span.attributes.items():
                    otel_span.set_attribute(key, value)
                for event in span.events:
                    otel_span.add_event(event.name, event.attributes)
                if span.status is SpanStatus.ERROR:
                    from opentelemetry.trace import Status, StatusCode

                    otel_span.set_status(Status(StatusCode.ERROR, span.error or "error"))
                otel_span.end()
            except Exception:  # pragma: no cover - 上报侧失败不影响主流程
                logger.debug("OTel span 收尾失败（已忽略）: {}", span.name)
        return span

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------
    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        """``with`` 语法的 span。

        Args:
            name (`str`): span 名。
            **attributes (`Any`): 初始属性。

        Yields:
            `Span`: 当前 span。

        Example:
            >>> tracer = Tracer()
            >>> with tracer.span("model_call", model="deepseek-flash") as s:
            ...     s.set_attribute("input_tokens", 128)
        """
        span = self.start_span(name, **attributes)
        try:
            yield span
        except BaseException as exc:  # noqa: BLE001 - 记录后必须继续抛
            self.finish_span(span, error=exc)
            raise
        else:
            self.finish_span(span)

    def span_of(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent: Span | None = None,
        **attributes: Any,
    ) -> "_SpanScope":
        """给异步代码用的 span 作用域（``async with``）。

        Args:
            name (`str`): span 名。
            trace_id (`str | None`): 显式 trace id。
            parent (`Span | None`): 显式父 span。
            **attributes (`Any`): 初始属性。

        Returns:
            `_SpanScope`: 支持 ``async with`` 与 ``with`` 的作用域对象。
        """
        return _SpanScope(self, name, trace_id=trace_id, parent=parent, attributes=attributes)

    # ------------------------------------------------------------------
    # 读取与导出
    # ------------------------------------------------------------------
    def spans(self) -> list[Span]:
        """返回全部进程内 span 的浅拷贝。

        Returns:
            `list[Span]`: span 列表（按开始顺序）。
        """
        return list(self._spans)

    def roots(self) -> list[Span]:
        """返回所有根 span（``parent_id is None``）。

        Returns:
            `list[Span]`: 根 span 列表。
        """
        return [span for span in self._spans if span.parent_id is None]

    def span_count(self) -> int:
        """已记录的 span 数。

        Returns:
            `int`: 数量。
        """
        return len(self._spans)

    def find(self, name: str) -> list[Span]:
        """按名字查 span。

        Args:
            name (`str`): span 名。

        Returns:
            `list[Span]`: 匹配的 span。
        """
        return [span for span in self._spans if span.name == name]

    def summary(self) -> dict[str, float]:
        """按 span 名汇总耗时与错误数。

        Returns:
            `dict[str, float]`: 键形如 ``"reply.count"`` / ``"reply.p95_ms"`` /
            ``"reply.errors"``。
        """
        grouped: dict[str, list[Span]] = {}
        for span in self._spans:
            grouped.setdefault(span.name, []).append(span)
        out: dict[str, float] = {}
        for name, items in grouped.items():
            durations = sorted(item.duration_ms for item in items)
            out[f"{name}.count"] = float(len(items))
            out[f"{name}.p50_ms"] = _pct(durations, 0.5)
            out[f"{name}.p95_ms"] = _pct(durations, 0.95)
            out[f"{name}.errors"] = float(sum(1 for item in items if item.status is SpanStatus.ERROR))
        return out

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（含 span 树与摘要）。

        Returns:
            `dict[str, Any]`: 字典。
        """
        return {
            "service": self.service_name,
            "exporter": self.exporter_kind,
            "generated_at": _now_utc().isoformat(),
            "span_count": len(self._spans),
            "summary": self.summary(),
            "spans": [span.to_dict() for span in self._spans],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """导出 JSON 文本。

        Args:
            indent (`int | None`): 缩进；``None`` 输出紧凑单行。

        Returns:
            `str`: JSON 文本。
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def dump(self, path: str | Path, *, indent: int | None = 2) -> Path:
        """把 span 树落盘。

        Args:
            path (`str | Path`): 目标路径；父目录会被创建。
            indent (`int | None`): 缩进。

        Returns:
            `Path`: 写入后的绝对路径。
        """
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(indent=indent), encoding="utf-8")
        logger.debug("span 树已落盘: {} ({} 个 span)", target, len(self._spans))
        return target.resolve()

    def reset(self) -> None:
        """清空所有 span（测试用）。"""
        self._spans.clear()
        self._tokens.clear()
        self._otel_span_ids.clear()

    def shutdown(self) -> None:
        """关闭 OTel provider（幂等）。"""
        if self._otel_provider is not None:
            try:
                self._otel_provider.shutdown()
            except Exception:  # pragma: no cover - 关闭失败不该炸主流程
                logger.debug("OTel provider 关闭失败（已忽略）")
        self._otel_provider = None
        self._otel_tracer = None


def _pct(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（只给 :meth:`Tracer.summary` 用）。

    Args:
        sorted_values (`list[float]`): 已升序的样本。
        q (`float`): 分位点。

    Returns:
        `float`: 分位数；空列表返回 ``0.0``。
    """
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


class _SpanScope:
    """支持 ``with`` / ``async with`` 的 span 作用域（:meth:`Tracer.span_of` 的产物）。"""

    def __init__(
        self,
        tracer: Tracer,
        name: str,
        *,
        trace_id: str | None,
        parent: Span | None,
        attributes: Mapping[str, Any],
    ) -> None:
        """记录待创建的 span 参数。

        Args:
            tracer (`Tracer`): 宿主 tracer。
            name (`str`): span 名。
            trace_id (`str | None`): 显式 trace id。
            parent (`Span | None`): 显式父 span。
            attributes (`Mapping[str, Any]`): 初始属性。
        """
        self._tracer: Tracer = tracer
        self._name: str = name
        self._trace_id: str | None = trace_id
        self._parent: Span | None = parent
        self._attributes: dict[str, Any] = dict(attributes)
        self.span: Span | None = None

    def __enter__(self) -> Span:
        """进入同步上下文。

        Returns:
            `Span`: 新建的 span。
        """
        self.span = self._tracer.start_span(
            self._name,
            trace_id=self._trace_id,
            parent=self._parent,
            **self._attributes,
        )
        return self.span

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出同步上下文。

        Args:
            exc_type (`Any`): 异常类型。
            exc (`Any`): 异常实例。
            tb (`Any`): traceback。
        """
        if self.span is not None:
            self._tracer.finish_span(self.span, error=exc if isinstance(exc, BaseException) else None)

    async def __aenter__(self) -> Span:
        """进入异步上下文。

        Returns:
            `Span`: 新建的 span。
        """
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出异步上下文。

        Args:
            exc_type (`Any`): 异常类型。
            exc (`Any`): 异常实例。
            tb (`Any`): traceback。
        """
        self.__exit__(exc_type, exc, tb)
