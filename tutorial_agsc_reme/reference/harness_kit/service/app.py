# -*- coding: utf-8 -*-
"""服务层（第 20 讲）：FastAPI + SSE 的运营入口（契约 §3.20）。

契约给这一层的签名只有一行：

.. code-block:: python

    def create_harness_app(*, profile: ResolvedProfile, settings: Settings) -> Any:
        # FastAPI 应用：POST /chat（SSE 流式）、GET /sessions、GET /sessions/{id}、
        # GET /healthz、GET /（Web UI）。
        # 铁律：默认端口 ≥ 18000；起服务后必须能被优雅关闭。
        # 必须复用 AgentScope 的 app/_app.py:78 create_app 的思路，而不是另起一套事件循环。

**"复用 create_app 的思路"具体指三件事**（都对着
``third_party/agentscope/src/agentscope/app/_app.py:78`` 抄的）：

1. 工厂函数返回一个配好的 ``FastAPI`` 实例，**不**在这里 ``uvicorn.run``
   —— 于是同一个 app 既能独立跑，也能 ``root.mount("/harness", app)``
   挂到别人的服务上（官方 docstring 里那两种用法）；
2. 资源生命周期交给 ``lifespan``（官方是 storage / message_bus，
   这里是会话存储 + 活着的 Agent）；
3. SSE 的写法照抄 ``.../app/_router/_session.py:925``：一个后台
   feeder 任务把事件塞进 ``asyncio.Queue``，主循环带超时地从队列取，
   超时就发一帧心跳 ``:\\n\\n``。**不是**把 Agent 直接跑在 SSE 生成器里 ——
   客户端一断开，生成器被关，Agent 就被腰斩在半路。

**本层自己不实现的东西**（红线）：Agent Loop 是 AgentScope 的
``Agent.reply_stream``（``third_party/agentscope/src/agentscope/agent/_agent.py:288``）；
Agent 装配是第 2 讲的 :class:`~harness_kit.config.builder.HarnessBuilder`；
会话存储是第 9 讲的 ``JsonlSessionStore``；事件翻译是第 3 讲的
``harness_kit/events/translate.py:StreamTranslator`` —— 它已交付，
:func:`_translate_event` 现在直接委托给它的 ``to_sse_frame``；
函数里保留的那条兜底分支只在"没读第 3 讲就跳到本讲"时才会走到。

**默认端口 18000**：契约铁律是 ≥ 18000。:data:`DEFAULT_PORT` = ``18420``，
选一个不圆整的号是为了降低与别人撞车的概率（与巡检脚本里"用 18000 以上"的
纪律一致）。

Example:
    >>> from harness_kit.service.app import create_harness_app   # doctest: +SKIP
    >>> app = create_harness_app(profile=profile, settings=settings)  # doctest: +SKIP
    >>> import uvicorn                                          # doctest: +SKIP
    >>> uvicorn.run(app, host="127.0.0.1", port=18420)          # doctest: +SKIP
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator, Sequence
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, Field

from harness_kit.config.builder import BuiltHarness, HarnessBuilder
from harness_kit.events.types import EventKind, EventRecord, utc_now
from harness_kit.observe.metrics import MetricsRegistry
from harness_kit.observe.tracing import Tracer
from harness_kit.session.jsonl_store import JsonlSessionStore
from harness_kit.session.models import SessionMeta
from harness_kit.session.replay import ReplayTurn, SessionReplayer
from harness_kit.session.store import SessionStoreBase

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from fastapi import FastAPI

    from harness_kit.config.schema import ResolvedProfile
    from harness_kit.settings import Settings

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ChatRequest",
    "ChatService",
    "ChatSession",
    "SessionRequest",
    "StoreEventBus",
    "create_harness_app",
    "sse_frame",
]


DEFAULT_HOST: str = "127.0.0.1"
"""默认监听地址。默认只绑本机 —— 这是个调试用服务，不是公网服务。"""

DEFAULT_PORT: int = 18420
"""默认端口（契约铁律：≥ 18000）。"""

HEARTBEAT_INTERVAL_S: float = 15.0
"""SSE 心跳间隔（秒）。与 ``.../app/_router/_session.py`` 的
``_HEARTBEAT_INTERVAL_SECS`` 同一个用途：让中间的代理知道连接还活着。"""

MAX_MESSAGE_CHARS: int = 20_000
"""单条用户消息的字符上限。超过就拒绝，而不是让请求打到模型才发现超长。"""

_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
"""SSE 响应头，逐字照抄 ``.../app/_router/_session.py:928``。"""


# ======================================================================
# 请求模型
# ======================================================================
# **为什么这两个模型定义在模块级、而不是塞进 create_harness_app 里**：
# 本模块有 ``from __future__ import annotations``，所有注解都是字符串；
# FastAPI 注册路由时用 ``typing.get_type_hints`` 解析它们，而解析只查
# **模块全局名字空间**。把 ``ChatRequest`` 定义在工厂函数内部，注解就解析不了，
# FastAPI 会把 ``payload`` 当成**查询参数**——实测表现是 POST /chat 恒返回
# ``422 {"loc": ["query", "payload"], "msg": "Field required"}``，
# 与请求体写什么都没关系。这是"本地定义 + 延迟注解"撞在一起的真实坑。
class ChatRequest(BaseModel):
    """``POST /chat`` 的请求体。"""

    model_config = {"extra": "forbid"}

    message: str = Field(min_length=1, description="用户输入")
    session_id: str | None = Field(default=None, description="会话 id；缺省则新建")
    stream: bool = Field(default=True, description="是否用 SSE；False 时直接返回 JSON")


class SessionRequest(BaseModel):
    """``POST /sessions`` 的请求体。"""

    model_config = {"extra": "forbid"}

    session_id: str | None = Field(default=None, description="会话 id；缺省则自动生成")


def sse_frame(payload: Any) -> str:
    """把任意可 JSON 化的对象包成一帧 SSE。

    Args:
        payload (`Any`): 载荷。

    Returns:
        `str`: ``"data: {...}\\n\\n"``。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _translate_event(chunk: Any) -> dict[str, Any] | None:
    """把一个 AgentScope ``AgentEvent`` 翻译成 SSE 载荷。

    翻译的口径归第 3 讲的 ``harness_kit/events/translate.py:StreamTranslator``：
    它在的时候**一律用它**（``StreamTranslator.to_sse_frame`` 是 ``@staticmethod``，
    不需要实例、不需要 session 状态，正好适配这里"无状态的单帧翻译"）。

    本函数保留一行兜底，是为了让"只读第 1、2 讲就跳到第 20 讲"的读者也能跑起来：
    靠探测 ``StreamTranslator`` 能否导入来决定用哪条路径，模块一旦存在就说明
    第 3 讲已交付，它的口径就该赢。

    Args:
        chunk (`Any`): ``AgentEvent``（``EventBase`` 的子类实例）。

    Returns:
        `dict[str, Any] | None`: ``{"event": <事件名>, "data": {...}}``；
        不是事件对象时返回 ``None``（例如 ``reply_stream`` 最后 yield 的 ``Msg``）。
    """
    translator_cls = _stream_translator_cls()
    if translator_cls is not None:
        return translator_cls.to_sse_frame(chunk)

    event_type = getattr(chunk, "type", None)
    if event_type is None:
        return None
    dumper = getattr(chunk, "model_dump", None)
    if not callable(dumper):
        return None
    return {
        "event": str(event_type),
        "data": dumper(mode="json"),
    }


_TRANSLATOR_CACHE: list[Any] = []
"""``StreamTranslator`` 的探测缓存（空列表 = 还没探测过）。"""


def _stream_translator_cls() -> Any | None:
    """探测第 3 讲的 ``StreamTranslator`` 是否已交付。

    Returns:
        `Any | None`: 类对象；未交付时返回 ``None``。
    """
    if _TRANSLATOR_CACHE:
        return _TRANSLATOR_CACHE[0]
    try:
        from harness_kit.events.translate import StreamTranslator
    except ImportError:
        _TRANSLATOR_CACHE.append(None)
        return None
    _TRANSLATOR_CACHE.append(StreamTranslator)
    logger.info("检测到 harness_kit.events.translate.StreamTranslator，SSE 改用它翻译事件")
    return StreamTranslator


class StoreEventBus:
    """把 ``TracingMiddleware`` 发出来的 ``EventRecord`` 追加进会话存储。

    这一类补的是 **seq 的权威归属**问题。第 8 讲的 ``TracingMiddleware``
    自己维护一个 ``_next_seq``（``harness_kit/middleware/tracing.py:495``），
    理由是"跨进程无洞由存储层保证，组件层只要单调"。而这里正是那个存储层：
    同一个会话里还有别人在写事件（``SESSION_START``、``MEMORY_HIT``、
    甚至另一个中间件），所以**只有本类能决定 seq**。

    因此 :meth:`publish` 会**丢弃** ``record.seq`` 并重新编号。这不是"不尊重
    producer"：``EventRecord`` 是不可变的（``frozen=True``，见
    ``harness_kit/events/types.py:109``），所以这里用 :meth:`EventRecord.model_copy`
    造一份新的，而不是改原对象。

    Args:
        store (`SessionStoreBase`): 会话存储。
        session_id (`str`): 会话 id。
    """

    def __init__(self, store: SessionStoreBase, session_id: str) -> None:
        """初始化。

        Args:
            store (`SessionStoreBase`): 会话存储。
            session_id (`str`): 会话 id。
        """
        self.store: SessionStoreBase = store
        self.session_id: str = session_id
        self.published: int = 0
        """已投递的事件条数。"""
        self.dropped: int = 0
        """因存储报错被丢弃的事件条数（丢弃时打 warning，不往上抛）。"""
        self._lock = asyncio.Lock()

    async def publish(self, topic: str | EventKind, record: EventRecord) -> int:
        """追加一条事件，返回它最终的 ``seq``。

        ``publish`` 与存储的 ``append`` 之间用一把 asyncio 锁串起来：
        "读 last_seq → 写" 这个复合操作必须原子，否则并发投递的事件会拿到
        同一个 seq 而被存储拒绝。

        单条事件写失败**不往上抛** —— 事件溯源是旁路，不该因为"日志写不进去"
        就把用户正在等的回复打断。失败会记 warning 并计入 :attr:`dropped`。

        Args:
            topic (`str | EventKind`): 主题（转成 ``EventKind`` 后落盘）。
            record (`EventRecord`): 事件记录。

        Returns:
            `int`: 落盘后的 ``seq``；投递失败时返回 ``-1``。
        """
        kind = topic if isinstance(topic, EventKind) else _coerce_kind(topic)
        async with self._lock:
            try:
                seq = await self.store.next_seq(self.session_id)
                stored = record.model_copy(
                    update={"session_id": self.session_id, "seq": seq, "kind": kind},
                )
                await self.store.append(stored)
            except Exception as exc:  # noqa: BLE001 - 旁路失败不打断主流程
                self.dropped += 1
                logger.bind(session_id=self.session_id).warning(
                    "事件投递失败（已丢弃第 {} 条）: {}: {}",
                    self.dropped,
                    type(exc).__name__,
                    exc,
                )
                return -1
            self.published += 1
            return seq


def _coerce_kind(topic: str) -> EventKind:
    """把字符串主题转成 :class:`EventKind`。

    接受三种写法：枚举的**成员名**（``"REPLY_END"``）、**字符串值**
    （``"reply_end"``）、大小写混合。都不匹配时退到 ``EventKind.CUSTOM``
    并打 warning —— 事件总线不该因为一个没登记的主题名把整条链打断。

    Args:
        topic (`str`): 主题名。

    Returns:
        `EventKind`: 事件种类。
    """
    lowered = topic.strip().lower()
    for member in EventKind:
        if lowered in (member.value, member.name.lower()):
            return member
    logger.bind(topic=topic).warning("未知事件主题，按 CUSTOM 记")
    return EventKind.CUSTOM


class ChatSession:
    """一个活着的会话：装配好的 Agent + 存储 + 指标 + trace。

    会话**不是**进程级单例：每个 ``ChatSession`` 有自己的
    :class:`~harness_kit.config.builder.HarnessBuilder`，因此有自己的模型客户端、
    工具包、权限上下文与 ``AgentState``。这正是"多会话互不串味"的前提。

    Args:
        session_id (`str`): 会话 id。
        built (`BuiltHarness`): 装配产物。
        builder (`HarnessBuilder`): 装配器（持有 ``aclose`` 生命周期）。
        store (`SessionStoreBase`): 会话存储。
        metrics (`MetricsRegistry | None`): 指标注册表。
        tracer (`Tracer | None`): tracer。
        profile_name (`str`): Profile 名（写进 ``SessionMeta``）。
    """

    def __init__(
        self,
        *,
        session_id: str,
        built: BuiltHarness,
        builder: HarnessBuilder,
        store: SessionStoreBase,
        metrics: MetricsRegistry | None = None,
        tracer: Tracer | None = None,
        profile_name: str = "",
    ) -> None:
        """初始化。

        Args:
            session_id (`str`): 会话 id。
            built (`BuiltHarness`): 装配产物。
            builder (`HarnessBuilder`): 装配器。
            store (`SessionStoreBase`): 会话存储。
            metrics (`MetricsRegistry | None`): 指标注册表。
            tracer (`Tracer | None`): tracer。
            profile_name (`str`): Profile 名。
        """
        self.session_id: str = session_id
        self.built: BuiltHarness = built
        self.builder: HarnessBuilder = builder
        self.store: SessionStoreBase = store
        self.profile_name: str = profile_name or built.profile.name
        self.bus: StoreEventBus = StoreEventBus(store, session_id)
        self.metrics: MetricsRegistry | None = metrics
        self.tracer: Tracer | None = tracer
        self.created_at = utc_now()
        self._reply_lock = asyncio.Lock()
        self._closed: bool = False
        self._wire_bus()

    # ------------------------------------------------------------------
    # 装配后回填
    # ------------------------------------------------------------------
    def _wire_bus(self) -> None:
        """把会话级的 bus / session_id 回填给需要它们的中间件。

        **为什么必须回填、而不能在 Profile 里配**：``EventBus`` 与 ``session_id``
        都是**会话级**对象，而 Profile 是**进程级**声明 —— YAML 里没法写
        "把我这一轮的事件写到这个 session_id 的存储里"。所以装配完成后由会话
        自己把句柄塞回去。判据很窄：只认同时有 ``bus`` 与 ``session_id`` 两个
        属性、且 ``bus`` 当前是 ``None`` 的中间件（就是第 8 讲的
        ``TracingMiddleware``，``harness_kit/middleware/tracing.py:300``）。
        """
        for middleware in self.built.middlewares:
            if not hasattr(middleware, "bus") or not hasattr(middleware, "session_id"):
                continue
            if getattr(middleware, "bus", None) is not None:
                continue
            middleware.bus = self.bus
            middleware.session_id = self.session_id
            logger.bind(session_id=self.session_id, middleware=type(middleware).__name__).debug(
                "已把会话级事件总线回填给中间件",
            )

    # ------------------------------------------------------------------
    # 首事件
    # ------------------------------------------------------------------
    async def ensure_started(self) -> SessionMeta:
        """确保 ``SESSION_START`` 已落盘，并返回会话卡片。

        ``SESSION_START`` 的 payload 字段是契约 §5.2 定死的三个
        （``profile`` / ``agent_name`` / ``cwd``，见
        ``harness_kit/events/types.py:46``），这里逐字照填。

        Returns:
            `SessionMeta`: 会话卡片。
        """
        meta = await self.store.meta(self.session_id)
        if meta is not None:
            return meta

        await self.bus.publish(
            EventKind.SESSION_START,
            EventRecord(
                session_id=self.session_id,
                seq=0,
                kind=EventKind.SESSION_START,
                payload={
                    "profile": self.profile_name,
                    "agent_name": self.built.agent.name,
                    "cwd": str(Path.cwd()),
                },
                source="harness_kit.service",
            ),
        )
        created = SessionMeta.create(self.session_id, profile_name=self.profile_name)
        return await self.store.meta(self.session_id) or created

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------
    async def reply(self, message: str) -> str:
        """跑一次完整回复（非流式入口）。

        Args:
            message (`str`): 用户输入。

        Returns:
            `str`: 回复文本。

        Raises:
            `ValueError`: 输入为空或超长。
        """
        _check_message(message)
        await self.ensure_started()
        async with self._reply_lock:
            tracer = self.tracer
            if tracer is None:
                reply = await self.built.agent.reply(_user_msg(message))
                return reply.get_text_content()

            with tracer.span("reply", session_id=self.session_id, profile=self.profile_name):
                reply = await self.built.agent.reply(_user_msg(message))
                text = reply.get_text_content()
            self._record(text)
            return text

    async def stream_frames(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """跑一次回复并把每一帧事件 yield 出来（SSE 的数据源）。

        Agent 跑在**后台任务**里，事件经 ``asyncio.Queue`` 转手；主循环带超时地
        取，超时就 yield 一帧"心跳"字典。这样做的好处与官方 SSE 路由一致：
        客户端断开时被取消的是生成器，而 Agent 任务会被 ``finally`` 显式取消并
        等着收尾（``await task``），不会留一个野生任务继续烧 token。

        Args:
            message (`str`): 用户输入。

        Yields:
            `dict[str, Any]`: 帧载荷；``{"event": "__heartbeat__"}`` 是心跳。

        Raises:
            `ValueError`: 输入为空或超长。
        """
        _check_message(message)
        await self.ensure_started()

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        task = asyncio.create_task(self._produce(message, queue), name=f"chat:{self.session_id}")
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
                except asyncio.TimeoutError:
                    yield {"event": "__heartbeat__"}
                    continue
                if frame is None:
                    break
                if frame.get("event") == "__final__":
                    # 收尾帧：补上本次回复的统计，再让循环自然结束
                    yield {"event": "final", "data": frame.get("data", {})}
                    break
                yield frame
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不掩盖主异常
                pass

    async def _produce(
        self,
        message: str,
        queue: "asyncio.Queue[dict[str, Any] | None]",
    ) -> None:
        """后台生产者：跑 Agent，把事件翻译后塞进队列。

        Args:
            message (`str`): 用户输入。
            queue (`asyncio.Queue[dict[str, Any] | None]`): 帧队列；结束时放 ``None``。
        """
        started = asyncio.get_running_loop().time()
        tokens_in = 0
        tokens_out = 0
        tool_calls = 0
        text_parts: list[str] = []
        try:
            async with self._reply_lock:
                async for chunk in self.built.agent.reply_stream(
                    _user_msg(message),
                    yield_final_msg=True,
                ):
                    frame = _translate_event(chunk)
                    if frame is None:
                        continue
                    name = frame["event"]
                    data = frame["data"]
                    if name == "TEXT_BLOCK_DELTA":
                        text_parts.append(str(data.get("delta", "")))
                    elif name == "MODEL_CALL_END":
                        tokens_in += int(data.get("input_tokens") or 0)
                        tokens_out += int(data.get("output_tokens") or 0)
                    elif name == "TOOL_CALL_START":
                        tool_calls += 1
                    await queue.put(frame)
        except asyncio.CancelledError:
            await queue.put({"event": "cancelled", "data": {"session_id": self.session_id}})
            raise
        except Exception as exc:  # noqa: BLE001 - 错误要以帧的形式告诉客户端
            logger.bind(session_id=self.session_id).exception("流式回复失败")
            await queue.put(
                {
                    "event": "error",
                    "data": {"type": type(exc).__name__, "message": str(exc)},
                },
            )
        finally:
            elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0
            text = "".join(text_parts)
            self._record(text, latency_ms=elapsed_ms, tokens=(tokens_in, tokens_out), tool_calls=tool_calls)
            await queue.put(
                {
                    "event": "__final__",
                    "data": {
                        "session_id": self.session_id,
                        "latency_ms": round(elapsed_ms, 2),
                        "input_tokens": tokens_in,
                        "output_tokens": tokens_out,
                        "tool_calls": tool_calls,
                        "text_chars": len(text),
                    },
                },
            )
            await queue.put(None)

    # ------------------------------------------------------------------
    # 指标与 trace
    # ------------------------------------------------------------------
    def _record(
        self,
        text: str,
        *,
        latency_ms: float = 0.0,
        tokens: tuple[int, int] = (0, 0),
        tool_calls: int = 0,
    ) -> None:
        """把一次回复的观测量记进指标注册表。

        Args:
            text (`str`): 回复文本（只用来记字符数）。
            latency_ms (`float`): 端到端耗时。
            tokens (`tuple[int, int]`): ``(输入, 输出)`` token。
            tool_calls (`int`): 工具调用次数。
        """
        if self.metrics is None:
            return
        labels = {"profile": self.profile_name}
        self.metrics.counter("replies_total", help="完成的回复次数").inc(**labels)
        if latency_ms:
            self.metrics.histogram("reply_latency", unit="ms").observe(latency_ms, **labels)
        if tokens[0]:
            self.metrics.counter("prompt_tokens_total", unit="token").inc(float(tokens[0]), **labels)
        if tokens[1]:
            self.metrics.counter("completion_tokens_total", unit="token").inc(float(tokens[1]), **labels)
        if tool_calls:
            self.metrics.counter("tool_calls_total", help="工具调用次数").inc(
                float(tool_calls),
                **labels,
            )
        self.metrics.histogram("reply_chars", unit="1").observe(float(len(text)), **labels)

    async def turns(self) -> list[ReplayTurn]:
        """回放本会话的回合列表。

        Returns:
            `list[ReplayTurn]`: 回合列表。
        """
        return await SessionReplayer(self.store).turns(self.session_id)

    async def aclose(self) -> None:
        """关闭会话持有的全部资源（幂等）。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self.builder.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响服务退出
            logger.bind(session_id=self.session_id).warning(
                "关闭会话时出错（已忽略）: {}: {}",
                type(exc).__name__,
                exc,
            )


def _check_message(message: str) -> None:
    """校验用户输入。

    Args:
        message (`str`): 用户输入。

    Raises:
        `ValueError`: 空白或超长。
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message 不能为空")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(
            f"message 过长（{len(message)} 字符 > 上限 {MAX_MESSAGE_CHARS}）；"
            "请拆分后再发，或调大 harness_kit.service.app.MAX_MESSAGE_CHARS",
        )


def _user_msg(text: str) -> Any:
    """把纯文本包成 AgentScope ``Msg``。

    Args:
        text (`str`): 用户输入。

    Returns:
        `Any`: ``Msg``。
    """
    from agentscope.message import Msg, TextBlock

    return Msg(name="user", role="user", content=[TextBlock(type="text", text=text)])


class ChatService:
    """服务级门面：持有存储、指标、tracer 与活着的会话。

    Args:
        profile (`ResolvedProfile`): 默认 Profile。
        settings (`Settings`): 全局设置（路径锚点）。
        store (`SessionStoreBase | None`): 会话存储；``None`` 时按
            ``settings.session_dir`` 建 ``JsonlSessionStore``。
        metrics (`MetricsRegistry | None`): 指标注册表。
        tracer (`Tracer | None`): tracer。
        max_sessions (`int`): 常驻会话上限；超过时按最近使用淘汰（并 ``aclose``）。
    """

    def __init__(
        self,
        *,
        profile: "ResolvedProfile",
        settings: "Settings",
        store: SessionStoreBase | None = None,
        metrics: MetricsRegistry | None = None,
        tracer: Tracer | None = None,
        max_sessions: int = 32,
    ) -> None:
        """初始化（不会立刻装配任何 Agent —— 那是 :meth:`start` 的事）。

        Args:
            profile (`ResolvedProfile`): 默认 Profile。
            settings (`Settings`): 全局设置。
            store (`SessionStoreBase | None`): 会话存储。
            metrics (`MetricsRegistry | None`): 指标注册表。
            tracer (`Tracer | None`): tracer。
            max_sessions (`int`): 常驻会话上限。
        """
        if max_sessions <= 0:
            raise ValueError(f"max_sessions 必须为正，收到 {max_sessions}")
        self.profile: ResolvedProfile = profile
        self.settings: Settings = settings
        self.store: SessionStoreBase = store or JsonlSessionStore(settings.session_dir)
        self.metrics: MetricsRegistry = metrics or MetricsRegistry()
        self.tracer: Tracer = tracer or Tracer(service_name="harness-kit-service")
        self.max_sessions: int = int(max_sessions)
        self.sessions: dict[str, ChatSession] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()
        self.started_at = utc_now()

    async def start(self) -> None:
        """服务启动钩子（当前只打一条日志；存储是懒创建的）。"""
        logger.bind(
            profile=self.profile.name,
            sessions_dir=str(self.settings.session_dir),
            max_sessions=self.max_sessions,
        ).info("harness_kit 服务已启动")

    async def aclose(self) -> None:
        """关闭全部会话与存储（幂等，可被 ``lifespan`` 反复调用）。"""
        async with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self._order.clear()
        for session in sessions:
            await session.aclose()
        await self.store.aclose()
        self.tracer.shutdown()
        logger.info("harness_kit 服务已关闭（{} 个会话）", len(sessions))

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    async def create_session(self, *, session_id: str | None = None) -> ChatSession:
        """新建一个会话（含一次完整的 Agent 装配）。

        Args:
            session_id (`str | None`): 指定 id；``None`` 时生成 ``uuid4().hex[:12]``。

        Returns:
            `ChatSession`: 新会话。

        Raises:
            `ValueError`: ``session_id`` 已存在。
        """
        sid = session_id or uuid4().hex[:12]
        async with self._lock:
            if sid in self.sessions:
                raise ValueError(f"会话 {sid!r} 已存在；请换个 id，或用 get_session 取它")
            builder = HarnessBuilder(self.profile, settings=self.settings, session_id=sid)
            built = await builder.build_all()
            session = ChatSession(
                session_id=sid,
                built=built,
                builder=builder,
                store=self.store,
                metrics=self.metrics,
                tracer=self.tracer,
                profile_name=self.profile.name,
            )
            self.sessions[sid] = session
            self._order.append(sid)
            evicted = self._evict_locked()
        for victim in evicted:
            await victim.aclose()
        await session.ensure_started()
        logger.bind(session_id=sid, profile=self.profile.name).info("会话已创建")
        return session

    async def get_session(self, session_id: str) -> ChatSession | None:
        """取一个活跃会话。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `ChatSession | None`: 会话；不活跃时返回 ``None``。
        """
        session = self.sessions.get(session_id)
        if session is not None and session_id in self._order:
            self._order.remove(session_id)
            self._order.append(session_id)
        return session

    async def ensure_session(self, session_id: str | None) -> ChatSession:
        """取活跃会话；不存在就从磁盘恢复的语义上"新建"。

        ``session_id`` 指向一个**历史上存在过但进程重启后不在内存里**的会话时，
        这里不会假装能续上：那需要第 9 讲的 ``SessionResumer`` 把
        ``AgentState`` 从快照里恢复出来，属于另一个讲次的路径。本方法的行为是
        **报错说清楚**，而不是悄悄开一个同名新会话把旧事件流覆盖掉。

        Args:
            session_id (`str | None`): 会话 id；``None`` 时新建。

        Returns:
            `ChatSession`: 活跃会话。

        Raises:
            `ValueError`: 会话 id 在存储里有历史、但不在内存里。
        """
        if session_id is None:
            return await self.create_session()
        session = await self.get_session(session_id)
        if session is not None:
            return session
        if await self.store.meta(session_id) is not None:
            raise ValueError(
                f"会话 {session_id!r} 在存储里有历史事件，但本进程没有它的活 Agent。"
                "续跑历史会话要走 SessionResumer（第 9 讲），本服务不做隐式恢复。",
            )
        return await self.create_session(session_id=session_id)

    def _evict_locked(self) -> list[ChatSession]:
        """按 LRU 淘汰超出上限的会话（调用方必须已持锁）。

        Returns:
            `list[ChatSession]`: 被淘汰的会话（调用方负责 ``aclose``）。
        """
        evicted: list[ChatSession] = []
        while len(self._order) > self.max_sessions:
            victim_id = self._order.pop(0)
            victim = self.sessions.pop(victim_id, None)
            if victim is not None:
                evicted.append(victim)
        return evicted

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def list_sessions(self) -> list[SessionMeta]:
        """列出存储里的全部会话。

        Returns:
            `list[SessionMeta]`: 会话卡片列表。
        """
        return await self.store.list_sessions()

    async def session_detail(self, session_id: str, *, event_limit: int = 200) -> dict[str, Any]:
        """一个会话的详情：卡片 + 回合 + 尾部事件。

        Args:
            session_id (`str`): 会话 id。
            event_limit (`int`): 最多返回多少条尾部事件。

        Returns:
            `dict[str, Any]`: 详情字典。

        Raises:
            `KeyError`: 会话不存在。
        """
        meta = await self.store.meta(session_id)
        if meta is None:
            raise KeyError(session_id)
        replayer = SessionReplayer(self.store)
        turns = await replayer.turns(session_id)
        events = await self.store.read(session_id, limit=None)
        tail = events[-event_limit:] if event_limit > 0 else []
        return {
            "meta": meta.model_dump(mode="json"),
            "active": session_id in self.sessions,
            "turns": [
                {
                    "reply_id": turn.reply_id,
                    "start_seq": turn.start_seq,
                    "end_seq": turn.end_seq,
                    "input_preview": turn.input_preview,
                    "iterations": turn.iterations,
                    "closed": turn.closed,
                    "tool_calls": turn.tool_calls,
                    "model_calls": turn.model_calls,
                    "token_usage": turn.token_usage.model_dump(),
                }
                for turn in turns
            ],
            "events": [
                {
                    "seq": item.record.seq,
                    "kind": item.record.kind.value,
                    "ts": item.record.ts.isoformat(),
                    "payload": item.record.payload,
                    "source": item.record.source,
                }
                for item in tail
            ],
            "event_total": len(events),
        }


# ======================================================================
# FastAPI 应用
# ======================================================================
def create_harness_app(
    *,
    profile: "ResolvedProfile",
    settings: "Settings",
    max_sessions: int = 32,
) -> "FastAPI":
    """建一个配好的 FastAPI 应用（契约 §3.20 的唯一签名）。

    返回的对象**不**自己监听端口：``uvicorn.run(app, ...)`` 由
    :mod:`harness_kit.cli` 负责（与官方 ``create_app`` 的分工一致，
    ``third_party/agentscope/src/agentscope/app/_app.py:78``）。

    路由：

    ============================== ==========================================
    ``POST /sessions``             新建会话
    ``GET  /sessions``             列出全部会话
    ``GET  /sessions/{id}``        会话详情（卡片 + 回合 + 尾部事件）
    ``POST /chat``                 **SSE** 流式对话
    ``GET  /traces``               trace 概况
    ``GET  /traces/{trace_id}``    单条 trace 的 span 树
    ``GET  /metrics``              Prometheus exposition 文本
    ``GET  /healthz``              健康检查
    ``GET  /``                     单文件 Web UI
    ============================== ==========================================

    Args:
        profile (`ResolvedProfile`): 默认 Profile。
        settings (`Settings`): 全局设置。
        max_sessions (`int`): 常驻会话上限。

    Returns:
        `FastAPI`: 应用实例。

    Raises:
        `ImportError`: ``fastapi`` 未安装（本层依赖它，不做降级）。
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise ImportError(
            "harness_kit.service.app 需要 fastapi 与 uvicorn；请先安装（本仓库的 "
            "agentscope_reme_pip_env 已自带）",
        ) from exc

    service = ChatService(profile=profile, settings=settings, max_sessions=max_sessions)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """应用级生命周期：启动服务、退出时优雅关闭。

        Args:
            app (`FastAPI`): 应用实例。

        Yields:
            `None`: 供 FastAPI 使用。
        """
        await service.start()
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        title="harness-kit service",
        version="1.0.0",
        description="harness_kit 运营层：会话事件溯源 + SSE 流式对话 + trace / metrics。",
        lifespan=lifespan,
    )
    app.state.service = service

    # ------------------------------------------------------------------
    # 会话（请求模型 ``ChatRequest`` / ``SessionRequest`` 见模块级定义 ——
    # 放在这里会解析不了注解，路由参数会被当成查询参数）
    # ------------------------------------------------------------------
    @app.post("/sessions")
    async def create_session(payload: SessionRequest | None = None) -> JSONResponse:
        """新建一个会话。

        Args:
            payload (`SessionRequest | None`): 请求体。

        Returns:
            `JSONResponse`: ``{"session_id": ..., "profile": ...}``。

        Raises:
            `HTTPException`: 会话 id 冲突或装配失败（400）。
        """
        wanted = payload.session_id if payload is not None else None
        try:
            session = await service.create_session(session_id=wanted)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {"session_id": session.session_id, "profile": session.profile_name},
        )

    @app.get("/sessions")
    async def list_sessions() -> JSONResponse:
        """列出存储里的全部会话。

        Returns:
            `JSONResponse`: ``{"sessions": [...]}``。
        """
        metas = await service.list_sessions()
        return JSONResponse(
            {
                "sessions": [
                    {
                        **meta.model_dump(mode="json"),
                        "active": meta.session_id in service.sessions,
                    }
                    for meta in metas
                ],
            },
        )

    @app.get("/sessions/{session_id}")
    async def get_session(
        session_id: str,
        event_limit: int = Query(default=200, ge=0, le=5000),
    ) -> JSONResponse:
        """取会话详情。

        Args:
            session_id (`str`): 会话 id。
            event_limit (`int`): 尾部事件条数上限。

        Returns:
            `JSONResponse`: 详情。

        Raises:
            `HTTPException`: 会话不存在（404）。
        """
        try:
            return JSONResponse(await service.session_detail(session_id, event_limit=event_limit))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}") from exc

    # ------------------------------------------------------------------
    # 对话（SSE）
    # ------------------------------------------------------------------
    @app.post("/chat")
    async def chat(payload: ChatRequest) -> Any:
        """对话。默认 SSE 流式，``stream=false`` 时返回一次性 JSON。

        Args:
            payload (`ChatRequest`): 请求体。

        Returns:
            `Any`: ``StreamingResponse`` 或 ``JSONResponse``。

        Raises:
            `HTTPException`: 输入非法或会话无法续跑（400）。
        """
        try:
            session = await service.ensure_session(payload.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not payload.stream:
            try:
                text = await session.reply(payload.message)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(
                {"session_id": session.session_id, "reply": text},
            )

        async def _generator() -> AsyncIterator[str]:
            """把会话的帧序列转成 SSE 文本流。

            Yields:
                `str`: SSE 帧。
            """
            yield sse_frame({"event": "session", "data": {"session_id": session.session_id}})
            try:
                async for frame in session.stream_frames(payload.message):
                    if frame.get("event") == "__heartbeat__":
                        yield ":\n\n"
                        continue
                    yield sse_frame(frame)
            except ValueError as exc:
                yield sse_frame({"event": "error", "data": {"type": "ValueError", "message": str(exc)}})
            finally:
                yield "data: [DONE]\n\n"

        return StreamingResponse(_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # ------------------------------------------------------------------
    # trace / metrics / health
    # ------------------------------------------------------------------
    @app.get("/traces")
    async def traces() -> JSONResponse:
        """trace 概况（span 数 + 分位耗时）。

        Returns:
            `JSONResponse`: 概况。
        """
        tracer = service.tracer
        return JSONResponse(
            {
                "service_name": tracer.service_name,
                "exporter": tracer.exporter_kind,
                "span_count": tracer.span_count(),
                "roots": [span.name for span in tracer.roots()],
                "summary": tracer.summary(),
            },
        )

    @app.get("/traces/{trace_id}")
    async def trace_detail(trace_id: str) -> JSONResponse:
        """一条 trace 的完整 span 树。

        Args:
            trace_id (`str`): trace id。

        Returns:
            `JSONResponse`: ``{"trace_id": ..., "spans": [...]}``。

        Raises:
            `HTTPException`: 找不到该 trace（404）。
        """
        spans = [span.to_dict() for span in service.tracer.spans if span.trace_id == trace_id]
        if not spans:
            raise HTTPException(status_code=404, detail=f"trace 不存在: {trace_id}")
        return JSONResponse({"trace_id": trace_id, "spans": spans})

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """Prometheus exposition 文本。

        Returns:
            `PlainTextResponse`: ``text/plain; version=0.0.4``。
        """
        return PlainTextResponse(
            service.metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """健康检查。

        Returns:
            `JSONResponse`: ``{"status": "ok", ...}``。
        """
        return JSONResponse(
            {
                "status": "ok",
                "profile": profile.name,
                "model": profile.model.model_name,
                "llm_configured": settings.has_llm(),
                "active_sessions": len(service.sessions),
                "started_at": service.started_at.isoformat(),
                "port_policy": f">= 18000（默认 {DEFAULT_PORT}）",
            },
        )

    # ------------------------------------------------------------------
    # Web UI
    # ------------------------------------------------------------------
    @app.get("/")
    async def index() -> HTMLResponse:
        """单文件调试 UI。

        Returns:
            `HTMLResponse`: ``index.html`` 的内容；文件缺失时给一段说明文本。
        """
        page = Path(__file__).parent / "webui" / "index.html"
        if not page.is_file():  # pragma: no cover - 文件随包分发
            return HTMLResponse("<h1>harness-kit</h1><p>webui/index.html 缺失</p>", status_code=200)
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app


def iter_routes(app: "FastAPI") -> Iterator[tuple[str, str]]:
    """列出应用的路由（``(方法, 路径)``），CLI 的 ``serve --list-routes`` 用。

    Args:
        app (`FastAPI`): 应用实例。

    Yields:
        `tuple[str, str]`: ``(HTTP 方法, 路径)``。
    """
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", "")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            yield method, path


def describe_routes(app: "FastAPI") -> list[str]:
    """把路由渲染成 ``"GET  /healthz"`` 形态的字符串列表。

    Args:
        app (`FastAPI`): 应用实例。

    Returns:
        `list[str]`: 排序后的路由文本。
    """
    return [f"{method:<4} {path}" for method, path in sorted(iter_routes(app), key=lambda x: x[1])]


def session_summary_lines(metas: Sequence[SessionMeta]) -> list[str]:
    """把会话卡片渲染成 CLI 表格行。

    Args:
        metas (`Sequence[SessionMeta]`): 会话卡片。

    Returns:
        `list[str]`: 每行一个会话。
    """
    return [
        f"{meta.session_id:<16} {meta.profile_name:<24} {meta.event_count:>5} 事件  "
        f"{meta.updated_at.isoformat()}"
        for meta in metas
    ]
