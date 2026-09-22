# -*- coding: utf-8 -*-
"""进程内 asyncio 事件总线（契约 §3.3，第 3 讲）。

**为什么需要它。** AgentScope 的 ``AgentEvent`` 只活在**一次 reply 的异步流**里
（``third_party/agentscope/src/agentscope/agent/_agent.py:288`` 的
``reply_stream`` 是一个 ``AsyncGenerator[AgentEvent | Msg, None]``），
那个 ``async for`` 循环一结束，事件对象就没人再持有了。于是三件事做不到：

1. **多个消费者同时看**：落盘、SSE 推送、指标、Web UI 想并行消费同一条事件，
   只能在一个循环里手写分发；
2. **消费者慢一点**：写盘、发网络都会**拖慢 Agent 主循环**（因为是在同一个
   ``async for`` 里 await 的）；
3. **事后订阅**：消费者在事件发生之后才想订阅（断线重连、审计巡检）什么都拿不到。

``EventBus`` 是补这一层的最小实现：:meth:`EventBus.publish` **不阻塞**
（只做 ``put_nowait`` 入队），每个订阅者一条独立的 ``asyncio.Queue``
加一个后台 worker；订阅者抛异常会被吞掉并计入 :attr:`EventBus.errors`，
**绝不让一个坏订阅者打断主流程**。

**背压策略**：队列满时**丢弃新事件**并计入 :attr:`EventBus.dropped`，
不是阻塞、也不是丢最老的。理由：这是观测旁路，"观测失败"不该升级成
"对话失败"；而丢最老的会让排障时看到的日志出现空洞（前面有、中间没有、
后面又有），丢最新的至少保证前缀是连续的。

**为什么不用别的东西**：不用 ``loop.call_soon``（丢了背压信号，内存会被撑爆）、
不用第三方 broker（本契约§七禁止引入未安装的依赖）、不用 ``asyncio.Event`` +
共享 list（那是"轮询"，不是"订阅"）。

**总线上流动的是什么**：是 :class:`~harness_kit.events.types.EventRecord`
—— 不可变的审计记录，**不是** AgentScope 的 ``AgentEvent``。两者的翻译在
``harness_kit/events/translate.py:StreamTranslator``。这个分层是刻意的：
总线不该依赖 agentscope，这样它也能承载 ReMe 侧、权限侧、评测侧自造的事件
（它们都只认 ``EventRecord``）。

**与 ``EventBusLike`` 的关系**：``harness_kit/middleware/tracing.py:67`` 用
``Protocol`` 描述了"我只用 ``publish`` 一个方法"。本类满足那个协议。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Final, cast
from uuid import uuid4

from loguru import logger

from harness_kit.events.types import (
    WILDCARD_TOPIC,
    EventKind,
    EventRecord,
    topic_matches,
)

__all__ = [
    "BusClosedError",
    "EventBus",
    "Handler",
    "Subscription",
]

Handler = Callable[[EventRecord], Awaitable[None]]
"""订阅者：收到一条事件记录，做完自己的事，返回 ``None``。"""

_STOP: Final[object] = object()
"""投进队列的"收工"哨兵。用模块级单例，避免与任何真实记录混淆。"""


class BusClosedError(RuntimeError):
    """向已经 :meth:`EventBus.aclose` 的总线投递事件。

    刻意**抛异常**而不是静默返回 0：事件总线被关掉之后还在投事件，
    说明生命周期管理出了错（多半是 async 上下文退出的顺序反了），
    静默丢弃会让这种错在几个月后才以"日志缺了一段"的形式暴露出来。
    """


class Subscription:
    """一次订阅的句柄（契约 §3.3）。

    订阅者不直接碰 :class:`EventBus`，只拿这个句柄做三件事：
    注销（:meth:`unsubscribe`）、看自己的健康度（:attr:`delivered` /
    :attr:`dropped`）、看自己是否还活着（:attr:`active`）。

    句柄由 :meth:`EventBus.subscribe` 创建，**不要手工 ``Subscription(...)``**。

    Attributes:
        bus (`EventBus`): 所属总线。
        topic (`str | EventKind`): 订阅主题，``"*"`` 表示全部。
        handler (`Handler`): 订阅者协程函数。
        delivered (`int`): 已成功交付给 ``handler`` 的条数。
        dropped (`int`): 因队列满被丢弃的条数。
        active (`bool`): 是否仍在订阅。
    """

    def __init__(
        self,
        bus: "EventBus",
        topic: str | EventKind,
        handler: Handler,
    ) -> None:
        """创建一个订阅（仅由 :meth:`EventBus.subscribe` 调用）。

        Args:
            bus (`EventBus`): 所属总线。
            topic (`str | EventKind`): 主题。
            handler (`Handler`): 订阅者协程函数。
        """
        self.bus: EventBus = bus
        self.topic: str | EventKind = topic
        self.handler: Handler = handler
        self.delivered: int = 0
        """已成功交付给 ``handler`` 的条数（不含抛异常的那些）。"""
        self.dropped: int = 0
        """因队列满被丢弃的条数。"""
        self.active: bool = True
        """是否仍在订阅。"""
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=bus.max_queue)
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 过滤
    # ------------------------------------------------------------------
    def matches(self, topic: str | EventKind) -> bool:
        """本订阅是否关心该主题。

        Args:
            topic (`str | EventKind`): 发布时用的主题。

        Returns:
            `bool`: 关心为 ``True``；主题不是合法事件种类时为 ``False``
            （宁可漏投也不要在过滤阶段抛异常——抛了会打断发布方）。
        """
        kind = _as_kind_or_none(topic)
        if kind is None:
            return False
        return topic_matches(self.topic, kind)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def unsubscribe(self) -> None:
        """注销订阅（幂等）。

        只把 :attr:`active` 置 ``False`` 并从总线的订阅表里摘掉，
        **不**取消正在跑的 worker —— worker 会把队列里已有的记录处理完
        再自然退出，这样"注销前已经投出去的事件"不会被吞掉。
        想确保处理完，请在 :meth:`EventBus.aclose` 之后再看 :attr:`delivered`。
        """
        if not self.active:
            return
        self.active = False
        if self in self.bus.subscriptions:
            self.bus.subscriptions.remove(self)
        # 摘掉之后 worker 还在跑（要把它队列里已有的记录消化完）。
        # 记进"退役名单"，否则 :meth:`EventBus.aclose` 再也找不到它，
        # 这个协程就成了没人回收的孤儿 —— 事件循环关闭时会打
        # ``Task was destroyed but it is pending!``。
        if self._task is not None and not self._task.done():
            self.bus._retired.append(self)

    def _ensure_worker(self) -> asyncio.Task[None] | None:
        """惰性启动 worker 任务（没有事件循环时返回 ``None``）。

        Returns:
            `asyncio.Task[None] | None`: worker 任务；无运行中的事件循环时为 ``None``。
        """
        if self._task is not None and not self._task.done():
            return self._task
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 在同步上下文里 subscribe()（例如模块级装配）时没有循环，
            # 等 EventBus.start() 或第一次 publish() 时再补上。
            return None
        self._task = asyncio.create_task(
            self._run(),
            name=f"eventbus:{self.bus.name}:{_topic_name(self.topic)}",
        )
        return self._task

    async def _run(self) -> None:
        """worker 主循环：逐条取记录交给 ``handler``。"""
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                record = cast(EventRecord, item)
                try:
                    await self.handler(record)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 订阅者的错不该传播
                    self.bus._record_error(self, exc)
                else:
                    self.delivered += 1
            finally:
                self._queue.task_done()

    async def _drain_and_stop(self) -> None:
        """让 worker 处理完队列里剩下的记录后退出。"""
        task = self._task
        if task is None or task.done():
            return
        try:
            self._queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            # 队列还满着，说明还有没消费完的；直接取消，丢失的条数已经
            # 计在 dropped 里（记录在 :meth:`EventBus.aclose` 的日志里）。
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - 取消路径
            pass
        except Exception as exc:  # noqa: BLE001 - 收尾不掩盖主异常
            self.bus._record_error(self, exc)


class EventBus:
    """进程内 asyncio pub/sub 事件总线（契约 §3.3）。

    语义（三条，写进契约就必须成立）：

    1. :meth:`publish` **不阻塞**：只入队，不等订阅者处理完；
    2. 订阅者异常被捕获，计入 :attr:`errors`，不影响其它订阅者、不影响发布方；
    3. 一条记录会投递给**所有**命中的订阅者，返回值为投递数。

    Attributes:
        max_queue (`int`): 每个订阅者队列的容量上限。
        name (`str`): 总线名（仅用于日志与任务名）。
        published (`int`): 累计 publish 次数。
        delivered (`int`): 累计投递条数（一次 publish 命中 N 个订阅者记 N）。
        dropped (`int`): 累计因队列满被丢弃的条数。
        subscriptions (`list[Subscription]`): 当前订阅表。
    """

    def __init__(self, *, max_queue: int = 1024) -> None:
        """初始化。

        Args:
            max_queue (`int`): 每个订阅者队列的容量上限，必须 ≥ 1。

        Raises:
            ValueError: ``max_queue`` 小于 1。
        """
        if max_queue < 1:
            raise ValueError(f"max_queue 必须 ≥ 1，收到 {max_queue}")
        self.max_queue: int = max_queue
        """每个订阅者队列的容量上限。"""
        self.name: str = f"bus-{uuid4().hex[:8]}"
        """总线名。"""
        self.published: int = 0
        """累计 publish 次数。"""
        self.delivered: int = 0
        """累计投递条数。"""
        self.dropped: int = 0
        """累计丢弃条数。"""
        self.subscriptions: list[Subscription] = []
        """当前订阅表。"""
        self._retired: list[Subscription] = []
        """已注销但 worker 还在收尾的订阅（``aclose`` 时一并等待）。"""
        self._errors: int = 0
        self._error_samples: list[str] = []
        self._started: bool = False
        self._closed: bool = False

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(self, topic: str | EventKind, handler: Handler) -> Subscription:
        """登记一个订阅者。

        Args:
            topic (`str | EventKind`): ``"*"`` 订阅全部；其余按种类相等匹配
                （见 :func:`~harness_kit.events.types.topic_matches`）。
            handler (`Handler`): 协程函数，签名 ``async def f(record) -> None``。

        Returns:
            `Subscription`: 订阅句柄。

        Raises:
            BusClosedError: 总线已关闭。
            TypeError: ``handler`` 不是可调用对象。
        """
        if self._closed:
            raise BusClosedError(
                f"EventBus({self.name}) 已关闭，不能再 subscribe()",
            )
        if not callable(handler):
            raise TypeError(f"handler 必须可调用，收到 {type(handler).__name__}")
        subscription = Subscription(self, topic, handler)
        self.subscriptions.append(subscription)
        if self._started:
            subscription._ensure_worker()
        logger.bind(bus=self.name, topic=_topic_name(topic)).debug(
            "新增订阅者，当前订阅数={}",
            len(self.subscriptions),
        )
        return subscription

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    async def publish(self, topic: str | EventKind, record: EventRecord) -> int:
        """投递一条事件记录给所有命中的订阅者（不阻塞）。

        Args:
            topic (`str | EventKind`): 主题；通常直接传 ``record.kind``。
                传 ``"*"`` 表示"无主题"，此时改用 ``record.kind`` 过滤
                （通配符是订阅方的写法，不该被发布方用来绕过过滤）。
            record (`EventRecord`): 事件记录。

        Returns:
            `int`: 实际入队的投递数（= 命中的订阅者数）。

        Raises:
            BusClosedError: 总线已关闭。
        """
        if self._closed:
            raise BusClosedError(
                f"EventBus({self.name}) 已关闭，不能再 publish()；"
                "请检查 async 上下文的退出顺序",
            )
        self.published += 1
        delivered = 0
        # 发布方传 "*" 表示"这是一条无主题的事件"，此时用记录自己的 kind 过滤
        # （通配符是**订阅方**的写法，发布方不该用它来绕过过滤）。
        match_topic: str | EventKind = (
            record.kind if topic == WILDCARD_TOPIC else topic
        )
        for subscription in list(self.subscriptions):
            if not subscription.active or not subscription.matches(match_topic):
                continue
            subscription._ensure_worker()
            try:
                subscription._queue.put_nowait(record)
            except asyncio.QueueFull:
                subscription.dropped += 1
                self.dropped += 1
                logger.bind(
                    bus=self.name,
                    session_id=record.session_id,
                    topic=_topic_name(subscription.topic),
                ).warning(
                    "订阅者队列已满（max_queue={}），丢弃第 {} 条事件；"
                    "订阅者处理速度跟不上发布速度",
                    self.max_queue,
                    subscription.dropped,
                )
                continue
            delivered += 1
        self.delivered += delivered
        return delivered

    async def drain(self) -> None:
        """等所有订阅者把队列里已入队的记录处理完（不关闭总线）。

        测试与脚本收尾时用它把"已经投出去但还没处理完"的事件消化掉，
        否则你会在 ``aclose()`` 之前读到偏小的 :attr:`delivered`。
        """
        for subscription in list(self.subscriptions):
            await subscription._queue.join()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动总线：为已登记的订阅者补建 worker 任务（幂等）。"""
        if self._closed:
            raise BusClosedError(f"EventBus({self.name}) 已关闭，不能再 start()")
        self._started = True
        for subscription in self.subscriptions:
            subscription._ensure_worker()
        logger.bind(bus=self.name, subscriptions=len(self.subscriptions)).debug(
            "EventBus 已启动",
        )

    async def aclose(self) -> None:
        """关闭总线：让 worker 处理完存量后退出（幂等）。

        关闭后 :meth:`publish` / :meth:`subscribe` 一律抛 :class:`BusClosedError`。
        """
        if self._closed:
            return
        self._closed = True
        self._started = False
        subscriptions, self.subscriptions = self.subscriptions, []
        retired, self._retired = self._retired, []
        for subscription in subscriptions:
            subscription.active = False
        for subscription in subscriptions + retired:
            await subscription._drain_and_stop()
        logger.bind(bus=self.name).debug(
            "EventBus 已关闭：published={} delivered={} dropped={} errors={}",
            self.published,
            self.delivered,
            self.dropped,
            self._errors,
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def errors(self) -> int:
        """订阅者异常累计次数（契约 §3.3 要求暴露）。

        Returns:
            `int`: 异常次数。
        """
        return self._errors

    @property
    def error_samples(self) -> list[str]:
        """最近若干条错误样本（``"订阅者名: 异常类型: 消息"``），便于排查。

        Returns:
            `list[str]`: 最多 8 条。
        """
        return list(self._error_samples)

    @property
    def closed(self) -> bool:
        """总线是否已关闭。

        Returns:
            `bool`: 已关闭为 ``True``。
        """
        return self._closed

    def stats(self) -> dict[str, int]:
        """把总线健康度压成一个 dict，方便直接塞进日志/指标。

        Returns:
            `dict[str, int]`: ``published`` / ``delivered`` / ``dropped`` /
            ``errors`` / ``subscriptions`` 五个键。
        """
        return {
            "published": self.published,
            "delivered": self.delivered,
            "dropped": self.dropped,
            "errors": self._errors,
            "subscriptions": len(self.subscriptions),
        }

    def _record_error(self, subscription: Subscription, exc: BaseException) -> None:
        """记录一次订阅者异常（内部）。

        Args:
            subscription (`Subscription`): 出错的订阅者。
            exc (`BaseException`): 异常。
        """
        self._errors += 1
        sample = (
            f"{_handler_name(subscription.handler)}: "
            f"{type(exc).__name__}: {exc}"
        )
        self._error_samples.append(sample)
        del self._error_samples[:-8]
        logger.bind(bus=self.name, topic=_topic_name(subscription.topic)).warning(
            "订阅者抛异常（已吞掉，累计 {} 次）: {}",
            self._errors,
            sample,
        )


def _as_kind_or_none(topic: str | EventKind) -> EventKind | None:
    """把主题归一成 :class:`EventKind` 供过滤使用。

    ``"*"`` 是订阅方的通配写法，在 :func:`topic_matches` 里按字符串处理，
    这里直接返回 ``None``（订阅方自己的 ``"*"`` 由 ``topic_matches`` 命中）。
    非法主题同样返回 ``None``，而不是抛异常 —— 过滤阶段抛出会打断发布方。

    Args:
        topic (`str | EventKind`): 主题。

    Returns:
        `EventKind | None`: 归一后的种类；无法归一为 ``None``。
    """
    if topic == WILDCARD_TOPIC:
        return None
    if isinstance(topic, EventKind):
        return topic
    try:
        return EventKind(topic)
    except ValueError:
        return None


def _topic_name(topic: str | EventKind) -> str:
    """主题的可读文本（日志用）。

    Args:
        topic (`str | EventKind`): 主题。

    Returns:
        `str`: 可读文本。
    """
    return topic.value if isinstance(topic, EventKind) else str(topic)


def _handler_name(handler: Handler) -> str:
    """订阅者的可读名字。

    Args:
        handler (`Handler`): 订阅者。

    Returns:
        `str`: ``"类名.方法名"`` 或函数的 ``__qualname__``。
    """
    owner = getattr(handler, "__self__", None)
    # 可调用**实例**（实现了 ``__call__`` 的类实例，订阅者最常见的写法）
    # 既没有 ``__qualname__`` 也没有 ``__name__``，直接 repr 出来是一串内存地址，
    # 排障时毫无信息量；退化成类名（``Recorder``）才有用。
    name = (
        getattr(handler, "__qualname__", None)
        or getattr(handler, "__name__", None)
        or type(handler).__name__
    )
    if owner is not None:
        # 绑定方法的 ``__qualname__`` 已经带了类名（``Recorder.__call__``），
        # 再拼一次会变成 ``Recorder.Recorder.__call__``。
        prefix = f"{type(owner).__name__}."
        return name if name.startswith(prefix) else f"{prefix}{name}"
    return str(name)
