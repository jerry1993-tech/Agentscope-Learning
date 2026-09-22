# -*- coding: utf-8 -*-
"""令牌桶限流与指数退避重试。

生产里模型调用会撞两类墙：**配额墙**（RPM / TPM 限流）与**瞬时故障**（超时、
连接重置、5xx）。AgentScope 的 :class:`~agentscope.model.ChatModelBase` 自己
处理了第二类的**壳**（``third_party/agentscope/src/agentscope/model/_base.py:206``
的 ``for attempt in range(self.max_retries + 1)`` 循环），但有两件事它不做：

1. **它不主动限流**。它只会在失败后 ``await asyncio.sleep(self.retry_delay)``
   重试，重试间隔是**固定值**（``_base.py:239``），没有指数退避、没有抖动，
   也没有「本地排队」的概念 —— 并发 20 个 Agent 时它们会同时打满一个端点。
2. **它只重试白名单异常**，白名单由子类的 ``_get_retryable_exceptions()``
   给出（``_base.py:100``）。自定义适配器如果忘了覆写它，默认是「一个都不重试」
   （``_base.py:109`` 返回空元组）。

本模块补的正是这两块，且**不重写 Agent Loop**：

- :class:`TokenBucket`：本地令牌桶，把「调用速率」压在配额以内；
- :class:`RateLimitedModel`：``ChatModelBase`` 的**包装器**（装饰器模式），
  在 ``_call_api`` 之前 ``acquire``，因此它仍然是一个合法的
  :class:`~agentscope.model.ChatModelBase` —— 可以直接交给 ``Agent(model=...)``；
- :func:`retry_with_backoff`：指数退避 + 抖动的重试装饰器，用来包住
  ``_call_api`` 里「裸调 SDK」的那一段；
- :func:`compute_delay`：纯函数，把退避算法单独暴露出来，**可以单测而不用真的睡**。

已知边界（诚实说明）：``RateLimitedModel`` 的 ``acquire`` 只覆盖「发起请求」，
不覆盖「消费流」。流式调用中，``_call_api`` 返回 async generator 时，
真正的网络 IO 发生在消费者 ``async for`` 的时候；此时若中途失败，
异常会抛在 Agent 的消费点而不是 ``__call__`` 的重试循环里 —— 这一点
AgentScope 原生实现同样如此（``_base.py:260`` 的 ``_stream`` 只处理
``asyncio.CancelledError``）。要覆盖「流中途断开」，需要在消费侧另加一层
重放策略，那是第 9 讲（会话事件溯源与断点续跑）的地盘。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    TypeVar,
)

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice

__all__ = [
    "RateLimitedModel",
    "RetryPolicy",
    "TokenBucket",
    "compute_delay",
    "retry_with_backoff",
]

R = TypeVar("R")


class TokenBucket:
    """令牌桶：以 ``rate`` 个/秒的速率补充，最多攒 ``capacity`` 个。

    典型用法是把 ``rate`` 设成 provider 的 RPM/60，把 ``capacity`` 设成
    「允许的瞬时突发量」：

    .. code-block:: python

        bucket = TokenBucket(rate=60 / 60, capacity=5)  # 60 RPM，允许 5 连发
        await bucket.acquire()

    Args:
        rate (`float`): 每秒补充的令牌数，必须为正。
        capacity (`int`): 桶容量（最大瞬时突发），必须为正整数。
        clock (`Callable[[], float] | None`): 时钟函数，默认
            :func:`time.monotonic`。可注入假时钟做确定性测试。

    Raises:
        ValueError: ``rate <= 0`` 或 ``capacity < 1``。
    """

    def __init__(
        self,
        *,
        rate: float,
        capacity: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"TokenBucket.rate 必须为正，收到 {rate}")
        if capacity < 1:
            raise ValueError(f"TokenBucket.capacity 必须 >= 1，收到 {capacity}")

        self.rate = float(rate)
        self.capacity = int(capacity)

        self._clock = clock or time.monotonic
        self._tokens = float(capacity)
        self._updated = self._clock()
        self._lock = asyncio.Lock()

        self.waited_s: float = 0.0
        """累计等待秒数（可观测性用：限流到底让请求慢了多少）。"""
        self.acquired: int = 0
        """累计成功取走的令牌数。"""

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _refill(self) -> None:
        """按流逝时间补令牌（惰性补，不需要后台线程）。"""
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        if elapsed <= 0:
            return
        self._updated = now
        self._tokens = min(
            float(self.capacity),
            self._tokens + elapsed * self.rate,
        )

    @property
    def available(self) -> float:
        """当前桶里的令牌数（已按当前时刻补充）。

        Returns:
            `float`: 可用令牌数，``0 <= available <= capacity``。
        """
        self._refill()
        return self._tokens

    def take_nowait(self, tokens: int = 1) -> bool:
        """不等待地尝试取令牌。

        Args:
            tokens (`int`): 需要的令牌数。

        Returns:
            `bool`: 取到返回 ``True``，不足返回 ``False``（不改变桶状态）。
        """
        self._validate_tokens(tokens)
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            self.acquired += tokens
            return True
        return False

    @staticmethod
    def _validate_tokens(tokens: int) -> None:
        """校验请求的令牌数。

        Args:
            tokens (`int`): 需要的令牌数。

        Raises:
            ValueError: 非正数。
        """
        if tokens < 1:
            raise ValueError(f"acquire 的 tokens 必须 >= 1，收到 {tokens}")

    # ------------------------------------------------------------------
    # 取令牌
    # ------------------------------------------------------------------
    async def acquire(self, tokens: int = 1) -> None:
        """取 ``tokens`` 个令牌，不足就等到够为止。

        单次请求需要的令牌数超过桶容量时**直接放行**（并打 warning）——
        否则会死等一个永远凑不出来的数，属于典型的「限流把自己锁死」。

        Args:
            tokens (`int`): 需要的令牌数，默认 1（= 一次模型调用）。

        Raises:
            ValueError: ``tokens < 1``。
        """
        self._validate_tokens(tokens)

        if tokens > self.capacity:
            logger.warning(
                "TokenBucket.acquire(tokens={}) 超过桶容量 {}，本次直接放行；"
                "请把 capacity 调到不小于单次请求的令牌数",
                tokens,
                self.capacity,
            )
            self.acquired += tokens
            return

        async with self._lock:
            started = self._clock()
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self.acquired += tokens
                    self.waited_s += self._clock() - started
                    return
                missing = tokens - self._tokens
                await asyncio.sleep(missing / self.rate)


class RetryPolicy(BaseModel):
    """指数退避重试策略。

    ``delay = min(max_delay, base_delay * multiplier ** (attempt - 1))``，
    再乘一个 ``[1 - jitter, 1 + jitter]`` 区间的随机因子。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(
        default=4,
        ge=1,
        description="总尝试次数（含第一次）；``1`` 表示不重试。",
    )

    base_delay: float = Field(
        default=0.5,
        ge=0,
        description="第一次重试前的等待秒数。",
    )

    max_delay: float = Field(
        default=8.0,
        ge=0,
        description="单次等待的上限秒数。",
    )

    multiplier: float = Field(
        default=2.0,
        ge=1,
        description="每次失败后的等待倍数。",
    )

    jitter: float = Field(
        default=0.25,
        ge=0,
        le=1,
        description=(
            "抖动比例：``0`` 表示确定性退避（便于单测），``0.25`` 表示"
            "实际等待在 ``0.75x ~ 1.25x`` 之间随机。多个进程同时重试时，"
            "抖动是避免它们再次同步打满端点的关键。"
        ),
    )

    retry_on: tuple[type[BaseException], ...] = Field(
        default=(Exception,),
        description=(
            "触发重试的异常类型。注意 ``asyncio.CancelledError`` 继承自"
            "``BaseException`` 而不是 ``Exception``，因此默认**不会**被吞掉 —— "
            "取消必须能穿透重试层。"
        ),
    )


def compute_delay(attempt: int, policy: RetryPolicy, *, rng: random.Random | None = None) -> float:
    """计算第 ``attempt`` 次失败后的等待秒数（纯函数，便于单测）。

    Args:
        attempt (`int`): 已经失败的次数，从 1 开始。
        policy (`RetryPolicy`): 退避策略。
        rng (`random.Random | None`): 随机源；``None`` 时用模块级 ``random``。

    Returns:
        `float`: 等待秒数，``0 <= delay <= max_delay * (1 + jitter)``。
    """
    if attempt < 1:
        raise ValueError(f"attempt 必须 >= 1，收到 {attempt}")
    raw = policy.base_delay * (policy.multiplier ** (attempt - 1))
    capped = min(policy.max_delay, raw)
    if policy.jitter <= 0:
        return max(0.0, capped)
    source = rng or random
    factor = 1.0 + source.uniform(-policy.jitter, policy.jitter)
    return max(0.0, capped * factor)


def retry_with_backoff(
    *,
    policy: RetryPolicy | None = None,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    jitter: float | None = None,
    retry_on: tuple[type[BaseException], ...] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """指数退避重试装饰器（只装饰**真异步**函数）。

    与 AgentScope 自带重试的关系：两者**可以叠加**，且叠加是合理的 ——
    ``ChatModelBase.__call__`` 负责「同一次调用的快速重试」（固定延迟、白名单异常），
    本装饰器负责「更慢、更久、带抖动」的兜底。但要注意叠加会把总尝试次数相乘，
    所以用它包 ``_call_api`` 时建议把 ``RetryPolicy.max_attempts`` 设小
    （2~3），把长期重试留给外层。

    Args:
        policy (`RetryPolicy | None`): 完整策略；给了它就忽略后面的零散覆盖参数。
        max_attempts (`int | None`): 覆盖总尝试次数。
        base_delay (`float | None`): 覆盖首轮等待。
        max_delay (`float | None`): 覆盖等待上限。
        jitter (`float | None`): 覆盖抖动比例。
        retry_on (`tuple[type[BaseException], ...] | None`): 覆盖异常白名单。
        on_retry (`Callable[[int, BaseException, float], None] | None`):
            每次重试前的回调 ``(attempt, error, delay)``，用于打日志 / 上报指标。

    Returns:
        `Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]`:
            装饰器。

    Example:
        >>> @retry_with_backoff(max_attempts=3, base_delay=0.01, jitter=0)
        ... async def flaky() -> str:
        ...     return "ok"
        >>> import asyncio
        >>> asyncio.run(flaky())
        'ok'
    """
    if policy is None:
        base = RetryPolicy()
        policy = RetryPolicy(
            max_attempts=max_attempts or base.max_attempts,
            base_delay=base.base_delay if base_delay is None else base_delay,
            max_delay=base.max_delay if max_delay is None else max_delay,
            multiplier=base.multiplier,
            jitter=base.jitter if jitter is None else jitter,
            retry_on=base.retry_on if retry_on is None else retry_on,
        )
    # retry_on 是 pydantic 的 tuple 字段，取出后放进闭包，避免装饰器每次都读属性
    retry_on_types: tuple[type[BaseException], ...] = tuple(policy.retry_on)

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """把 ``func`` 包成带退避重试的版本。

        Args:
            func (`Callable[..., Awaitable[Any]]`): 异步函数。

        Returns:
            `Callable[..., Awaitable[Any]]`: 包装后的异步函数。
        """

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """带退避重试地调用 ``func``。

            Args:
                *args (`Any`): 透传位置参数。
                **kwargs (`Any`): 透传关键字参数。

            Returns:
                `Any`: ``func`` 的返回值。

            Raises:
                BaseException: 最后一次失败的异常原样抛出。
            """
            last_error: BaseException | None = None
            for attempt in range(1, policy.max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_on_types as exc:  # type: ignore[misc]
                    last_error = exc
                    if attempt >= policy.max_attempts:
                        break
                    delay = compute_delay(attempt, policy)
                    if on_retry is not None:
                        on_retry(attempt, exc, delay)
                    logger.warning(
                        "{} 第 {}/{} 次失败（{}: {}），{:.2f}s 后重试",
                        getattr(func, "__name__", repr(func)),
                        attempt,
                        policy.max_attempts,
                        type(exc).__name__,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

            assert last_error is not None
            raise last_error

        wrapper.__name__ = getattr(func, "__name__", "wrapped")
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


@dataclass
class RateLimitStats:
    """限流包装器的记账（每次 :meth:`RateLimitedModel._call_api` 累加）。"""

    calls: int = 0
    """经过限流的模型调用次数。"""

    waited_s: float = 0.0
    """累计等待秒数。"""

    tokens: int = 0
    """累计消耗的令牌数。"""


class RateLimitedModel(ChatModelBase):
    """把任意 :class:`~agentscope.model.ChatModelBase` 包成「先取令牌再调用」。

    为什么是**继承** ``ChatModelBase`` 而不是简单包一层代理对象：Agent 的类型
    约定就是 ``ChatModelBase``（``Agent(model=...)`` 会直接用它的 ``__call__`` /
    ``count_tokens`` / ``generate_structured_output``）。继承 + 只覆写
    ``_call_api`` 是唯一能同时保住「限流」与「所有原生能力」的写法 ——
    这也正是契约 §3.4 强调「绝不覆写 ``__call__``」的原因：
    ``__call__`` 里装着 20 行重试与流式聚合逻辑，覆写等于把它们全丢掉。

    用到的真实 API：

    - ``ChatModelBase.__init__(credential, model, parameters, stream,
      max_retries, retry_delay, context_size)``
      ``third_party/agentscope/src/agentscope/model/_base.py:62``
    - 抽象方法 ``_call_api``
      ``third_party/agentscope/src/agentscope/model/_base.py:293``

    Args:
        inner (`ChatModelBase`): 被包装的真实模型。
        bucket (`TokenBucket`): 令牌桶。
        tokens_per_call (`int`): 每次调用消耗的令牌数，默认 1。

    Example:
        >>> wrapped = RateLimitedModel(inner_model, bucket)  # doctest: +SKIP
        >>> await wrapped(messages)                          # doctest: +SKIP
    """

    def __init__(
        self,
        inner: ChatModelBase,
        bucket: TokenBucket,
        *,
        tokens_per_call: int = 1,
    ) -> None:
        """构造限流包装器。

        Args:
            inner (`ChatModelBase`): 被包装的模型。
            bucket (`TokenBucket`): 令牌桶。
            tokens_per_call (`int`): 每次调用消耗的令牌数。

        Raises:
            TypeError: ``inner`` 不是 :class:`ChatModelBase`。
        """
        if not isinstance(inner, ChatModelBase):
            raise TypeError(
                f"RateLimitedModel 只能包装 ChatModelBase，收到 "
                f"{type(inner).__name__}",
            )
        # 把内层的运行期属性原样抄上来：Agent 会读 model.stream / model.context_size，
        # 以及 count_tokens 时的 model.model 名字。
        super().__init__(
            credential=inner.credential,
            model=inner.model,
            parameters=inner.parameters,
            stream=inner.stream,
            max_retries=inner.max_retries,
            retry_delay=inner.retry_delay,
            context_size=inner.context_size,
        )
        # 基类 __init__ 不建 formatter（它只存在于各具体模型的 __init__ 里，
        # 见 .../model/_openai_chat/_model.py:163）。而 Agent 会读
        # model.formatter（.../agent/_agent.py:2066），所以必须从内层抄一份过来，
        # 否则 Agent(model=RateLimitedModel(...)) 会 AttributeError。
        self.formatter = getattr(inner, "formatter", None)
        # 字段要自己赋值（基类不是 dataclass，不会自动建）
        self.inner = inner
        self.bucket = bucket
        self.tokens_per_call = tokens_per_call
        self.stats = RateLimitStats()

    # ------------------------------------------------------------------
    # 委托
    # ------------------------------------------------------------------
    @classmethod
    def _get_retryable_exceptions(cls) -> tuple[type[Exception], ...]:
        """默认不重试（真正的白名单在内层模型上）。

        Returns:
            `tuple[type[Exception], ...]`: 空元组。
        """
        return ()

    def _inner_retryable(self) -> tuple[type[Exception], ...]:
        """取内层模型的白名单，让外层的重试循环也认得它。

        Returns:
            `tuple[type[Exception], ...]`: 内层 ``_get_retryable_exceptions()``
            的结果；取不到时返回空元组。
        """
        getter = getattr(self.inner, "_get_retryable_exceptions", None)
        if getter is None:  # pragma: no cover - ChatModelBase 一定有
            return ()
        return tuple(getter())

    async def count_tokens(
        self,
        messages: list[Msg],
        tools: list[dict] | None,
    ) -> int:
        """委托内层模型数 token（保留它的精确实现，如 tiktoken）。

        Args:
            messages (`list[Msg]`): 输入消息。
            tools (`list[dict] | None`): 工具 schema。

        Returns:
            `int`: token 数。
        """
        return await self.inner.count_tokens(messages, tools)

    def unwrap(self) -> ChatModelBase:
        """取出被包装的模型（测试里常用它断言内层真的被调用了）。

        Returns:
            `ChatModelBase`: 内层模型。
        """
        return self.inner

    async def aclose(self) -> None:
        """尽力释放内层模型（若它有 ``aclose``）。"""
        closer = getattr(self.inner, "aclose", None)
        if callable(closer):
            outcome = closer()
            if asyncio.iscoroutine(outcome):
                await outcome

    # ------------------------------------------------------------------
    # 覆写点
    # ------------------------------------------------------------------
    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """先取令牌，再把请求原样转发给内层的 ``_call_api``。

        注意这里**故意**调用 ``inner._call_api`` 而不是
        ``await inner(...)``：``inner(...)`` 会再跑一遍内层的重试与流式聚合，
        造成双重包装；重试与聚合由本类的 ``__call__``（继承自
        ``ChatModelBase``）统一负责。

        Args:
            model_name (`str`): 模型名（用 ``self.model``，与内层一致）。
            messages (`list[Msg]`): 输入消息。
            tools (`list[dict] | None`, optional): 工具 schema。
            tool_choice (`ToolChoice | None`, optional): 工具选择。
            **kwargs (`Any`): 透传给内层。

        Returns:
            `ChatResponse | AsyncGenerator[ChatResponse, None]`: 内层返回值
            （非流式是 ``ChatResponse``，流式是 async generator）。

        Raises:
            asyncio.CancelledError: 取消会**穿透**限流等待，不会被吞掉。
        """
        del model_name  # 以 self.model 为准，保证内外层用同一个模型名
        started = time.monotonic()
        await self.bucket.acquire(self.tokens_per_call)
        waited = time.monotonic() - started

        self.stats.calls += 1
        self.stats.waited_s += waited
        self.stats.tokens += self.tokens_per_call

        return await self.inner._call_api(
            self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

    def retryable_exceptions(self) -> tuple[type[Exception], ...]:
        """暴露内层白名单，供上层自建重试时复用。

        Returns:
            `tuple[type[Exception], ...]`: 异常类型元组。
        """
        return self._inner_retryable()
