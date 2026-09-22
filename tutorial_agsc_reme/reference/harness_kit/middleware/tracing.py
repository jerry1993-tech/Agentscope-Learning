# -*- coding: utf-8 -*-
"""链路追踪中间件（契约 §3.8，第 8 讲）。

**为什么不是简单地把 AgentScope 官方的 ``TracingMiddleware`` 挂上就完事？**

官方实现（``third_party/agentscope/src/agentscope/middleware/_tracing/_trace.py:117``）
只往 OpenTelemetry 打 span，而它自己第一件事就是
``if not _check_tracing_enabled(): return``（同文件 ``:143``）——
``_check_tracing_enabled``（``.../_tracing/_trace.py:59``）要求全局
``TracerProvider`` 已经是 SDK 实现（即已经调过 ``agentscope.setup_tracing``）。
**没配 OTel 时它是个彻底的 no-op，一条 trace 都不留**。这在本地开发和
"线上出了问题但 OTel collector 挂了"这两个场景下都很难受。

所以本中间件的策略是**双写、可降级**：

1. **始终**在进程内维护一棵 span 树（``ContextVar`` 栈 + ``SpanRecord``），
   ``to_json()`` 随时落盘，不依赖任何外部基础设施；
2. 若检测到 OTel SDK 已就绪（复用官方的 ``_check_tracing_enabled``，
   **不重复实现一份探针**），则同时开真实 OTel span，trace 直接进 Jaeger / Tempo；
3. 若给了事件总线，再把 hook 翻译成 ``EventRecord`` 发出去
   （``REPLY_START`` / ``MODEL_CALL`` / ``TOOL_CALL`` / ``TOOL_RESULT`` /
   ``REPLY_END``，payload 字段严格按 ``harness_kit/events/types.py:39`` 的
   ``PAYLOAD_FIELDS`` 填）。

**两个必须说清楚的实现约束**：

1. ``ContextVar`` 的可见性跟着 asyncio 执行上下文走。``asyncio.create_task``
   出来的子任务会**拷贝**创建时刻的上下文，因此子任务里的 span 会把当前 span
   认作父节点（这是我们想要的）；但父节点之后新压入的 span 不会被子任务看到。
   AgentScope 的并行工具调用属于前者，所以它们会正确挂在同一个父 span 下。
2. **async generator 的收尾里不能随便 await**。消费方提前跳出 ``async for`` 时，
   生成器会收到 ``GeneratorExit``，此时再 ``await`` 会触发
   ``RuntimeError: async generator ignored GeneratorExit``。所以本模块把
   span 的收尾拆成**同步的** :meth:`TracingMiddleware.close_span` 与**异步的**
   事件投递两步，``GeneratorExit`` 分支只走同步那一步。
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Literal, Protocol
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.events.types import EventKind, EventRecord, utc_now
from harness_kit.middleware.base import HarnessMiddleware, call_next, call_next_stream

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.agent import Agent

__all__ = [
    "EventBusLike",
    "SpanHandle",
    "SpanRecord",
    "TracingMiddleware",
]


class EventBusLike(Protocol):
    """事件总线的最小接口（结构类型）。

    真实实现是 ``harness_kit/events/bus.py`` 的 ``EventBus``（契约 §3.8），
    本模块只依赖它的 ``publish`` 一个方法，因此用 ``Protocol`` 描述，
    避免对尚未交付的模块产生硬依赖。
    """

    async def publish(self, topic: str | EventKind, record: EventRecord) -> int:
        """投递一条事件。

        Args:
            topic (`str | EventKind`): 主题。
            record (`EventRecord`): 事件记录。

        Returns:
            `int`: 投递到的订阅者数。
        """
        ...  # pragma: no cover


def _digest(value: Any) -> str:
    """把任意值压成一个定长摘要（sha1 前 16 位十六进制）。

    工具入参里常常有凭据、路径、用户数据，**不能原样进 trace**；
    但完全不留痕又没法排查"这次调用到底传了什么"。摘要是折中：
    同一次调用必然同摘要（可做关联），但不可逆。

    Args:
        value (`Any`): 任意值。

    Returns:
        `str`: 摘要；``value`` 为 ``None`` 时返回 ``""``。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 不可序列化对象
            raw = repr(value)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _preview(value: Any, *, max_chars: int = 120) -> str:
    """把任意值压成一行短预览。

    Args:
        value (`Any`): 任意值。
        max_chars (`int`): 最大字符数。

    Returns:
        `str`: 单行预览。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


class SpanRecord(BaseModel):
    """一个 span 的不可变快照。

    Attributes:
        span_id (`str`): span 唯一 id（``uuid4().hex``）。
        parent_id (`str | None`): 父 span id；根 span 为 ``None``。
        name (`str`): span 名，如 ``"reply reviewer"`` / ``"model_call
            deepseek-chat"`` / ``"tool_call read_file"``。
        kind (`Literal["reply", "model_call", "tool_call", "custom"]`): span 类别。
        started_at (`datetime`): 开始时间（UTC、tz-aware）。
        ended_at (`datetime | None`): 结束时间；未结束时为 ``None``。
        duration_ms (`float | None`): 时长（毫秒）；未结束时为 ``None``。
        status (`Literal["running", "ok", "error"]`): 状态。
        error (`str | None`): 失败时的 ``"{类型}: {消息}"``。
        attributes (`dict[str, Any]`): 结构化属性。
    """

    model_config = ConfigDict(frozen=True)

    span_id: str
    parent_id: str | None = None
    name: str
    kind: Literal["reply", "model_call", "tool_call", "custom"] = "custom"
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: Literal["running", "ok", "error"] = "running"
    error: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_root(self) -> bool:
        """是否是根 span。

        Returns:
            `bool`: ``parent_id`` 为空时为 ``True``。
        """
        return self.parent_id is None

    def to_json_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict。

        Returns:
            `dict[str, Any]`: 时间字段转 ISO 字符串，时长保留 3 位小数。
        """
        payload = self.model_dump()
        payload["started_at"] = self.started_at.astimezone(timezone.utc).isoformat()
        payload["ended_at"] = (
            self.ended_at.astimezone(timezone.utc).isoformat()
            if self.ended_at
            else None
        )
        payload["duration_ms"] = (
            round(self.duration_ms, 3) if self.duration_ms is not None else None
        )
        return payload


class SpanHandle:
    """写 span 用的可变句柄。

    ``SpanRecord`` 是 frozen 的（要能安全地跨协程共享、要能直接落盘），
    所以"边跑边改属性"这件事交给本类，收尾时一次性固化成 ``SpanRecord``。

    Args:
        record (`SpanRecord`): 起始快照（``status="running"``）。
        start_perf (`float`): ``time.perf_counter()`` 计时起点。
    """

    def __init__(self, record: SpanRecord, start_perf: float) -> None:
        """初始化。

        Args:
            record (`SpanRecord`): 起始快照。
            start_perf (`float`): 计时起点。
        """
        self.record = record
        self.start_perf = start_perf
        self.attributes: dict[str, Any] = dict(record.attributes)
        self.status: Literal["running", "ok", "error"] = "running"
        self.error: str | None = None
        self.otel_span: Any = None
        """OTel 侧的真实 span；OTel 未接通时为 ``None``。"""

    @property
    def span_id(self) -> str:
        """span id。

        Returns:
            `str`: span id。
        """
        return self.record.span_id

    @property
    def elapsed_ms(self) -> float:
        """从开启到现在的毫秒数。

        Returns:
            `float`: 毫秒。
        """
        return (time.perf_counter() - self.start_perf) * 1000.0

    def set(self, **attributes: Any) -> None:
        """写入/覆盖属性。

        Args:
            **attributes (`Any`): 属性键值。
        """
        self.attributes.update(attributes)

    def incr(self, key: str, delta: int = 1) -> None:
        """把某个整型属性累加。

        Args:
            key (`str`): 属性名。
            delta (`int`): 增量。
        """
        self.attributes[key] = int(self.attributes.get(key, 0)) + delta

    def ok(self) -> None:
        """标记成功（不覆盖已有的 error）。"""
        if self.status != "error":
            self.status = "ok"

    def fail(self, exc: BaseException) -> None:
        """标记失败。

        Args:
            exc (`BaseException`): 捕获到的异常。
        """
        self.status = "error"
        self.error = f"{type(exc).__name__}: {exc}"


class TracingMiddleware(HarnessMiddleware):
    """链路追踪（契约 §3.8）。

    Args:
        bus (`EventBusLike | None`): 事件总线；``None`` 表示不发事件，
            只维护本地 span 树。契约里它是必填（``bus: EventBus``），
            但 ``harness_kit/events/bus.py`` 属于第 3 讲、当前尚未交付，
            所以这里放宽为可选并在缺省时降级 —— 见 ``unresolved``。
        session_id (`str`): 会话 id，写进 ``EventRecord.session_id`` 与 span 属性。
        export_dir (`Path | str | None`): 非空时，每个 reply 结束自动把 span 树
            导出到 ``{export_dir}/{session_id}-{时间戳}.json``。
        service_name (`str`): OTel tracer 的 service 名，默认 ``"harness_kit"``。
        publish_spans (`bool`): 是否额外把 span 结束事件以 ``EventKind.CUSTOM``
            发到总线，默认 ``False``（span 数量大，容易淹没有业务语义的事件）。

    Note:
        ``seq`` 由本类自己维护（:meth:`_next_seq`），保证同一实例内单调递增。
        跨进程序列号无洞由第 9 讲的 ``SessionStore`` 保证，不属于本类职责。

    Example:
        >>> mw = TracingMiddleware(session_id="s-1")               # doctest: +SKIP
        >>> agent = Agent(..., middlewares=[mw])                   # doctest: +SKIP
        >>> len(mw.spans) > 0                                      # doctest: +SKIP
        True
    """

    _STACK: ContextVar[tuple[str, ...]] = ContextVar(
        "harness_kit_tracing_stack",
        default=(),
    )
    """进程级共享的 span 栈。刻意做成**类属性**：同一进程里多个 Agent 各自挂一个
    TracingMiddleware 时，A 里发起的嵌套调用在 B 的记录里也能找到父节点，
    否则并行 Agent / 子 Agent 的 trace 会断成互不相干的森林。"""

    def __init__(
        self,
        *,
        bus: EventBusLike | None = None,
        session_id: str = "",
        export_dir: Path | str | None = None,
        service_name: str = "harness_kit",
        publish_spans: bool = False,
    ) -> None:
        """初始化。

        Args:
            bus (`EventBusLike | None`): 见类文档。
            session_id (`str`): 见类文档。
            export_dir (`Path | str | None`): 见类文档。
            service_name (`str`): 见类文档。
            publish_spans (`bool`): 见类文档。
        """
        self.bus = bus
        self.session_id = session_id
        self.service_name = service_name
        self.publish_spans = publish_spans
        self.export_dir = Path(export_dir) if export_dir is not None else None

        self._spans: dict[str, SpanRecord] = {}
        self._order: list[str] = []
        self._seq: int = 0
        self._otel_tracer: Any = None
        self._otel_enabled: bool = False
        self._setup_otel()

        if bus is None:
            logger.bind(middleware=self.name()).info(
                "未提供事件总线，TracingMiddleware 只维护本地 span 树"
                "（harness_kit/events/bus.py 交付后可传入）",
            )

    # ------------------------------------------------------------------
    # OTel 接入
    # ------------------------------------------------------------------
    def _setup_otel(self) -> None:
        """探测 OTel 是否就绪；就绪则取一个 tracer。

        复用官方 ``_check_tracing_enabled``（``.../_tracing/_trace.py:59``），
        而不是自己再判断一次 ``get_tracer_provider()`` —— 官方那段判断还处理了
        "SDK 没装"的情况，重复实现只会两边漂移。
        """
        try:
            from agentscope.middleware._tracing._trace import (  # noqa: PLC0415
                _check_tracing_enabled,
            )
            from opentelemetry import trace as otel_trace  # noqa: PLC0415
        except ImportError:  # pragma: no cover - opentelemetry 缺失时降级
            self._otel_enabled = False
            return
        if not _check_tracing_enabled():
            self._otel_enabled = False
            return
        self._otel_tracer = otel_trace.get_tracer(self.service_name)
        self._otel_enabled = True

    @property
    def otel_enabled(self) -> bool:
        """OTel 是否真的接通（用于自查"我的 trace 到底进没进 Jaeger"）。

        Returns:
            `bool`: 接通为 ``True``。
        """
        return self._otel_enabled

    # ------------------------------------------------------------------
    # span 生命周期（open / close 是同步的，见模块 docstring 约束 2）
    # ------------------------------------------------------------------
    def current_span_id(self) -> str | None:
        """当前上下文栈顶的 span id。

        Returns:
            `str | None`: 栈空时为 ``None``。
        """
        stack = self._STACK.get()
        return stack[-1] if stack else None

    def open_span(
        self,
        name: str,
        kind: Literal["reply", "model_call", "tool_call", "custom"] = "custom",
        **attributes: Any,
    ) -> tuple[SpanHandle, Any]:
        """开一个 span（同步）。

        Args:
            name (`str`): span 名。
            kind (`Literal["reply", "model_call", "tool_call", "custom"]`): span 类别。
            **attributes (`Any`): 初始属性。

        Returns:
            `tuple[SpanHandle, Any]`: ``(句柄, contextvar token)``；
            token 必须原样交给 :meth:`close_span`。
        """
        parent = self.current_span_id()
        record = SpanRecord(
            span_id=uuid4().hex,
            parent_id=parent,
            name=name,
            kind=kind,
            started_at=utc_now(),
            attributes={"session_id": self.session_id, **attributes},
        )
        self._spans[record.span_id] = record
        self._order.append(record.span_id)
        handle = SpanHandle(record, time.perf_counter())
        if self._otel_enabled:
            handle.otel_span = self._otel_tracer.start_span(name)
            handle.set(otel="true")
        token = self._STACK.set((*self._STACK.get(), record.span_id))
        return handle, token

    def close_span(self, handle: SpanHandle, token: Any) -> SpanRecord:
        """关闭 span（同步），返回固化后的记录。

        Args:
            handle (`SpanHandle`): :meth:`open_span` 返回的句柄。
            token (`Any`): :meth:`open_span` 返回的 token。

        Returns:
            `SpanRecord`: 终止态快照。
        """
        try:
            self._STACK.reset(token)
        except ValueError:  # pragma: no cover - token 已失效（跨 context 误用）
            logger.bind(middleware=self.name(), span_id=handle.span_id).warning(
                "span token 复位失败，span 栈可能已错位",
            )
        if handle.status == "running":
            handle.ok()
        final = handle.record.model_copy(
            update={
                "ended_at": utc_now(),
                "duration_ms": handle.elapsed_ms,
                "status": handle.status,
                "error": handle.error,
                "attributes": handle.attributes,
            },
        )
        self._spans[final.span_id] = final
        if handle.otel_span is not None:
            if handle.status == "error":
                handle.otel_span.set_status(_otel_status(True), handle.error or "")
            else:
                handle.otel_span.set_status(_otel_status(False))
            handle.otel_span.end()
        if self.publish_spans:
            logger.bind(
                middleware=self.name(),
                span_id=final.span_id,
                parent_id=final.parent_id,
                kind=final.kind,
                name=final.name,
                duration_ms=round(final.duration_ms or 0.0, 3),
                status=final.status,
            ).debug("span 结束")
        return final

    @asynccontextmanager
    async def span(
        self,
        name: str,
        kind: Literal["reply", "model_call", "tool_call", "custom"] = "custom",
        **attributes: Any,
    ) -> AsyncGenerator[SpanHandle, None]:
        """``open_span`` / ``close_span`` 的 ``async with`` 糖。

        适合"不产出异步流"的场景（自定义中间件、被追踪的普通协程）。
        产出异步流的 hook 请直接用前两个方法 —— 生成器被提前关闭时不能 await。

        Args:
            name (`str`): span 名。
            kind (`Literal["reply", "model_call", "tool_call", "custom"]`): span 类别。
            **attributes (`Any`): 初始属性。

        Yields:
            `SpanHandle`: 句柄。
        """
        handle, token = self.open_span(name, kind, **attributes)
        try:
            yield handle
        except BaseException as exc:
            handle.fail(exc)
            raise
        finally:
            self.close_span(handle, token)

    # ------------------------------------------------------------------
    # 事件总线
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        """分配下一个会话内序号。

        Returns:
            `int`: 从 0 开始的单调递增值。
        """
        seq = self._seq
        self._seq += 1
        return seq

    async def publish_event(
        self,
        kind: EventKind | str,
        payload: dict[str, Any],
        *,
        topic: str | EventKind | None = None,
    ) -> bool:
        """构造 ``EventRecord`` 并投递；无总线时降级成 debug 日志。

        Args:
            kind (`EventKind | str`): 事件种类。允许直接写字符串
                （``"custom"`` / ``"tool_call"`` …），内部会归一成
                :class:`~harness_kit.events.EventKind` —— pydantic 在
                ``EventRecord`` 里已经会做这一步转换，但转换结果只存在于
                模型实例上，本函数后面还要读 ``kind.value`` 打日志，
                不归一就会在 ``"custom"`` 这种**看着完全合法**的调用上抛
                ``AttributeError: 'str' object has no attribute 'value'``。
            payload (`dict[str, Any]`): 负载，字段按 ``PAYLOAD_FIELDS`` 填。
            topic (`str | EventKind | None`): 主题；``None`` 时用 ``kind``。

        Returns:
            `bool`: 真的投递出去了为 ``True``。

        Raises:
            ValueError: ``kind`` 不是合法的事件种类。
        """
        if not isinstance(kind, EventKind):
            kind = EventKind(kind)
        record = EventRecord(
            session_id=self.session_id,
            seq=self._next_seq(),
            kind=kind,
            payload=payload,
            source="harness_kit.middleware.tracing",
        )
        missing = record.missing_payload_fields()
        if missing:
            logger.bind(
                middleware=self.name(),
                kind=kind.value,
                missing=missing,
            ).warning("EventRecord payload 缺字段，按契约 §5.2 应补齐")
        if self.bus is None:
            logger.bind(
                middleware=self.name(),
                kind=kind.value,
                seq=record.seq,
                payload=payload,
            ).debug("无事件总线，EventRecord 未投递")
            return False
        delivered = await self.bus.publish(
            topic if topic is not None else kind,
            record,
        )
        logger.bind(
            middleware=self.name(),
            kind=kind.value,
            seq=record.seq,
            delivered=delivered,
        ).debug("EventRecord 已投递")
        return True

    # ------------------------------------------------------------------
    # 查询与导出
    # ------------------------------------------------------------------
    @property
    def spans(self) -> list[SpanRecord]:
        """按开始顺序返回全部 span。

        Returns:
            `list[SpanRecord]`: span 列表。
        """
        return [self._spans[span_id] for span_id in self._order]

    def open_spans(self) -> list[SpanRecord]:
        """返回仍在运行中的 span（用于排查"卡在哪一步"）。

        Returns:
            `list[SpanRecord]`: ``status == "running"`` 的 span。
        """
        return [record for record in self.spans if record.status == "running"]

    def children_of(self, span_id: str) -> list[SpanRecord]:
        """返回某 span 的直接子节点。

        Args:
            span_id (`str`): 父 span id。

        Returns:
            `list[SpanRecord]`: 子 span 列表。
        """
        return [r for r in self.spans if r.parent_id == span_id]

    def tree(self) -> list[dict[str, Any]]:
        """把 span 树导出成嵌套 dict。

        Returns:
            `list[dict[str, Any]]`: 根 span 列表，每个节点带 ``children``。
        """
        by_parent: dict[str | None, list[SpanRecord]] = {}
        for record in self.spans:
            by_parent.setdefault(record.parent_id, []).append(record)

        def build(node: SpanRecord) -> dict[str, Any]:
            payload = node.to_json_dict()
            payload["children"] = [build(c) for c in by_parent.get(node.span_id, [])]
            return payload

        return [build(root) for root in by_parent.get(None, [])]

    def summary(self) -> dict[str, Any]:
        """给出一行可观测摘要（span 数 / 失败数 / 总时长）。

        Returns:
            `dict[str, Any]`: 摘要。
        """
        roots = [r for r in self.spans if r.is_root]
        return {
            "session_id": self.session_id,
            "spans": len(self._order),
            "roots": len(roots),
            "errors": sum(1 for r in self.spans if r.status == "error"),
            "open": len(self.open_spans()),
            "otel": self._otel_enabled,
            "bus": self.bus is not None,
            "duration_ms": round(sum(r.duration_ms or 0.0 for r in roots), 3),
        }

    def to_json(self, path: Path | str) -> Path:
        """把 span 树写到磁盘。

        Args:
            path (`Path | str`): 目标文件路径；父目录会自动创建。

        Returns:
            `Path`: 实际写入的路径。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "service": self.service_name,
            "session_id": self.session_id,
            "summary": self.summary(),
            "spans": self.tree(),
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.bind(
            middleware=self.name(),
            path=str(target),
            spans=len(self._order),
        ).info("span 树已导出")
        return target

    def reset(self) -> None:
        """清空已记录的 span（``seq`` 保留，避免事件序号回退）。"""
        self._spans.clear()
        self._order.clear()

    def describe(self) -> dict[str, Any]:
        """覆写基类摘要。

        Returns:
            `dict[str, Any]`: 含 :meth:`summary` 的内容。
        """
        return {**super().describe(), **self.summary()}

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """包住一次 reply：根 span + ``REPLY_START`` / ``REPLY_END`` 事件。

        Args:
            agent (`Agent`): 发起 reply 的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``inputs``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的事件。
        """
        inputs = input_kwargs.get("inputs")
        reply_id = uuid4().hex[:12]
        handle, token = self.open_span(
            f"reply {agent.name}",
            "reply",
            agent=agent.name,
            reply_id=reply_id,
            input_preview=_preview(inputs),
        )
        await self.publish_event(
            EventKind.REPLY_START,
            {"reply_id": reply_id, "input_preview": _preview(inputs)},
        )
        try:
            async for event in next_handler(**input_kwargs):
                self._observe_reply_event(handle, event)
                yield event
        except GeneratorExit as exc:
            # 消费方提前关闭生成器：这里再 await 会炸（见模块 docstring 约束 2）
            handle.fail(exc)
            self.close_span(handle, token)
            raise
        except BaseException as exc:
            handle.fail(exc)
            self.close_span(handle, token)
            await self._emit_reply_end(handle, reply_id)
            raise
        else:
            handle.ok()
            self.close_span(handle, token)
            await self._emit_reply_end(handle, reply_id)

    def _observe_reply_event(self, handle: SpanHandle, event: Any) -> None:
        """从 reply 的事件流里抽取计数（不解析消息内容，只计数）。

        Args:
            handle (`SpanHandle`): 当前 reply 的 span 句柄。
            event (`Any`): ``AgentEvent``。
        """
        handle.incr("events")
        if type(event).__name__ == "ReplyEndEvent":
            handle.set(
                finished_reason=str(getattr(event, "finished_reason", "")),
            )

    async def _emit_reply_end(self, handle: SpanHandle, reply_id: str) -> None:
        """投递 ``REPLY_END`` 并按配置导出 span 树。

        Args:
            handle (`SpanHandle`): 已关闭的 span 句柄。
            reply_id (`str`): 本次 reply 的 id。
        """
        await self.publish_event(
            EventKind.REPLY_END,
            {
                "reply_id": reply_id,
                "iterations": int(handle.attributes.get("iterations", 0)),
                "tool_calls": int(handle.attributes.get("tool_calls", 0)),
            },
        )
        if self.export_dir is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            self.to_json(
                self.export_dir / f"{self.session_id or 'session'}-{stamp}.json",
            )

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Any],
    ) -> Any:
        """包住一次模型调用，结束时报 ``MODEL_CALL``。

        Args:
            agent (`Agent`): 发起调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``messages`` / ``tools`` /
                ``current_model``。
            next_handler (`Callable[..., Any]`): 链上的下一环。

        Returns:
            `Any`: 原样透传的 ``ChatResponse`` 或其异步生成器。
        """
        model = input_kwargs.get("current_model")
        model_name = str(getattr(model, "model", "<unknown>"))
        messages = input_kwargs.get("messages") or []
        tools = input_kwargs.get("tools") or []
        handle, token = self.open_span(
            f"model_call {model_name}",
            "model_call",
            agent=agent.name,
            model=model_name,
            messages=len(messages),
            tools=len(tools),
        )
        try:
            result = await call_next(next_handler, input_kwargs)
        except BaseException as exc:
            handle.fail(exc)
            self.close_span(handle, token)
            await self._emit_model_call(handle)
            raise
        if hasattr(result, "__aiter__"):
            return self._trace_stream(handle, token, result, model_name)
        self._record_model_usage(handle, result)
        handle.ok()
        self.close_span(handle, token)
        await self._emit_model_call(handle)
        return result

    async def _emit_model_call(self, handle: SpanHandle) -> None:
        """投递 ``MODEL_CALL`` 事件。

        Args:
            handle (`SpanHandle`): 已关闭的 span 句柄。
        """
        await self.publish_event(
            EventKind.MODEL_CALL,
            {
                "model": str(handle.attributes.get("model", "")),
                "prompt_tokens": int(handle.attributes.get("prompt_tokens", 0)),
                "completion_tokens": int(
                    handle.attributes.get("completion_tokens", 0),
                ),
                "latency_ms": round(handle.elapsed_ms, 3),
                "finished_reason": str(
                    handle.attributes.get("finished_reason", ""),
                ),
            },
        )

    async def _trace_stream(
        self,
        handle: SpanHandle,
        token: Any,
        stream: AsyncGenerator[Any, None],
        model_name: str,
    ) -> AsyncGenerator[Any, None]:
        """流式响应：span 直到流耗尽才关闭。

        这里刻意**不用** ``async with``：``on_model_call`` 返回生成器时就退出了
        那个 with 块，span 会在第一个 chunk 之前就被关掉。

        Args:
            handle (`SpanHandle`): 已打开的 span 句柄。
            token (`Any`): span 栈 token。
            stream (`AsyncGenerator[Any, None]`): 下游的流。
            model_name (`str`): 模型名（只用于日志）。

        Yields:
            `Any`: 原样透传的 chunk。
        """
        try:
            async for chunk in stream:
                self._record_model_usage(handle, chunk)
                yield chunk
        except GeneratorExit as exc:
            handle.fail(exc)
            self.close_span(handle, token)
            raise
        except BaseException as exc:
            handle.fail(exc)
            self.close_span(handle, token)
            await self._emit_model_call(handle)
            logger.bind(
                middleware=self.name(),
                model=model_name,
                error=str(exc),
            ).warning("模型调用流出错，span 记为 error")
            raise
        else:
            handle.ok()
            self.close_span(handle, token)
            await self._emit_model_call(handle)

    def _record_model_usage(self, handle: SpanHandle, response: Any) -> None:
        """把一次 ``ChatResponse`` 的 usage / 结束原因写进 span。

        Args:
            handle (`SpanHandle`): span 句柄。
            response (`Any`): ``ChatResponse``。
        """
        usage = getattr(response, "usage", None)
        if usage is not None:
            handle.set(
                prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            )
        reason = getattr(response, "finished_reason", None)
        if reason is not None:
            handle.set(finished_reason=str(reason))

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """包住一次工具执行，并报 ``TOOL_CALL`` / ``TOOL_RESULT``。

        Args:
            agent (`Agent`): 执行工具调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``tool_call``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的 ``ToolChunk`` / ``ToolResponse``。
        """
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "<unknown>"))
        call_id = str(getattr(tool_call, "id", ""))
        parent = self.current_span_id()

        handle, token = self.open_span(
            f"tool_call {tool_name}",
            "tool_call",
            agent=agent.name,
            tool=tool_name,
            call_id=call_id,
        )
        await self.publish_event(
            EventKind.TOOL_CALL,
            {
                "tool_name": tool_name,
                "tool_input_digest": _digest(getattr(tool_call, "input", None)),
                "call_id": call_id,
            },
        )
        self._bump_parent_tool_calls(parent)
        last: Any = None
        chunks = 0
        error: str | None = None
        try:
            async for item in call_next_stream(next_handler, input_kwargs):
                chunks += 1
                last = item
                yield item
        except GeneratorExit as exc:
            handle.fail(exc)
            self._settle_tool_span(handle, last, chunks, error)
            self.close_span(handle, token)
            raise
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            handle.fail(exc)
            self._settle_tool_span(handle, last, chunks, error)
            self.close_span(handle, token)
            await self._emit_tool_result(call_id, handle, chunks, error)
            raise
        else:
            handle.ok()
            self._settle_tool_span(handle, last, chunks, error)
            self.close_span(handle, token)
            await self._emit_tool_result(call_id, handle, chunks, error)

    def _settle_tool_span(
        self,
        handle: SpanHandle,
        last: Any,
        chunks: int,
        error: str | None,
    ) -> None:
        """把工具结果的状态与字符数写进 span（同步，安全可随时调用）。

        Args:
            handle (`SpanHandle`): span 句柄。
            last (`Any`): 最后一个 ``ToolChunk`` / ``ToolResponse``。
            chunks (`int`): 收到的增量块数。
            error (`str | None`): 错误文本。
        """
        state = getattr(last, "state", None)
        chars = sum(
            len(getattr(block, "text", "") or "")
            for block in (getattr(last, "content", None) or [])
        )
        handle.set(chunks=chunks, state=str(state), result_chars=chars)
        if error is not None:
            handle.set(error=error)

    async def _emit_tool_result(
        self,
        call_id: str,
        handle: SpanHandle,
        chunks: int,
        error: str | None,
    ) -> None:
        """投递 ``TOOL_RESULT`` 事件。

        Args:
            call_id (`str`): 工具调用 id。
            handle (`SpanHandle`): 已关闭的 span 句柄。
            chunks (`int`): 增量块数（仅日志用）。
            error (`str | None`): 错误文本。
        """
        await self.publish_event(
            EventKind.TOOL_RESULT,
            {
                "call_id": call_id,
                "state": str(handle.attributes.get("state", "")),
                "chars": int(handle.attributes.get("result_chars", 0)),
                "error": error,
            },
        )

    def _bump_parent_tool_calls(self, parent: str | None) -> None:
        """给父 span 的工具调用计数 +1（父 span 可能还没关闭，就地更新）。

        Args:
            parent (`str | None`): 父 span id；``None`` 时什么都不做。
        """
        if parent is None or parent not in self._spans:
            return
        current = self._spans[parent]
        self._spans[parent] = current.model_copy(
            update={
                "attributes": {
                    **current.attributes,
                    "tool_calls": int(current.attributes.get("tool_calls", 0)) + 1,
                },
            },
        )


def _otel_status(error: bool) -> Any:
    """返回 OTel 的 OK / ERROR 状态。

    刻意不在模块顶层 import ``opentelemetry`` 的符号：那会把一个可选依赖
    变成硬依赖（``import harness_kit.middleware.tracing`` 就炸）。

    Args:
        error (`bool`): ``True`` 取 ERROR，``False`` 取 OK。

    Returns:
        `Any`: ``StatusCode`` 成员；opentelemetry 缺失时返回 ``None``。
    """
    try:
        from opentelemetry.trace import StatusCode  # noqa: PLC0415

        return StatusCode.ERROR if error else StatusCode.OK
    except ImportError:  # pragma: no cover
        return None
