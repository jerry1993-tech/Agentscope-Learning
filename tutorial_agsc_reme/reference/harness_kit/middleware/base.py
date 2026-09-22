# -*- coding: utf-8 -*-
"""中间件基类与组合辅助（契约 §3.8，第 8 讲）。

**铁律：绝不重写 execute_chain。**

AgentScope 的中间件链是 ``Agent._reply`` / ``_reasoning`` / ``_acting`` /
``_call_model`` 内部的**局部嵌套函数** ``execute_chain(index)``：

.. code-block:: python

    # third_party/agentscope/src/agentscope/agent/_agent.py:3336（on_model_call 的链）
    async def execute_chain(index=0, current_model=model, messages=messages, ...):
        if index >= len(self._model_call_middlewares):
            return await current_model(messages=messages, tools=tools, ...)
        mw = self._model_call_middlewares[index]
        input_kwargs = {"current_model": ..., "messages": ..., "tools": ..., "tool_choice": ...}

        async def next_handler(**kwargs):
            return await execute_chain(index + 1, **{**input_kwargs, **kwargs})

        return await mw.on_model_call(agent=self, input_kwargs=input_kwargs, next_handler=next_handler)

它在函数内部闭包捕获了 ``self`` / ``model`` / ``messages`` 等状态，
**不可 import、不可复用、不可替换**。任何"自己实现一条链"的尝试都会
绕开 Agent 的 ``is_implemented`` 过滤（``.../agent/_agent.py:218-241``）、
``ReplyStartEvent`` / ``ReplyEndEvent`` 的事件流契约、以及
``middle_context`` 的状态持久化（``.../state/_state.py:294``）。

所以 harness_kit 只做两件事：

1. 把**每个 hook 的 onion 协议**封装成一对可复用的辅助函数
   （:func:`call_next_stream` / :func:`call_next`），让写中间件的人不必
   自己记 ``async for ... yield`` 的样板；
2. 把**链的可见性**做出来（:func:`implemented_hooks` / :func:`filter_by_hook`
   / :func:`onion_order`），让"我挂的这个中间件到底有没有生效"这个问题
   在构造后就能回答，而不是靠日志猜。

7 个 hook 的精确签名见 ``third_party/agentscope/src/agentscope/middleware/_base.py``：
``on_reply:68`` / ``on_reasoning:101`` / ``on_acting:124`` / ``on_check_permission:170``
/ ``on_model_call:213`` / ``on_compress_context:241`` / ``on_system_prompt:264``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, Iterable

from loguru import logger

from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.tool import ToolBase

__all__ = [
    "HOOK_NAMES",
    "STREAM_HOOKS",
    "VALUE_HOOKS",
    "HarnessMiddleware",
    "call_next",
    "call_next_stream",
    "filter_by_hook",
    "implemented_hooks",
    "onion_order",
]

HOOK_NAMES: tuple[str, ...] = (
    "on_reply",
    "on_reasoning",
    "on_acting",
    "on_check_permission",
    "on_model_call",
    "on_compress_context",
    "on_system_prompt",
)
"""Agent 在构造时会逐个 hook 调 ``is_implemented`` 做分流
（``third_party/agentscope/src/agentscope/agent/_agent.py:218-241``），
这 7 个就是分流用的名字。"""

STREAM_HOOKS: frozenset[str] = frozenset(
    {"on_reply", "on_reasoning", "on_acting"},
)
"""返回 ``AsyncGenerator`` 的 hook。``on_acting`` 虽然也返回生成器，
但它 yield 的是 ``ToolChunk | ToolResponse`` 而不是事件。"""

VALUE_HOOKS: frozenset[str] = frozenset(
    {"on_check_permission", "on_model_call", "on_compress_context"},
)
"""返回"单个值"（需要 ``await``）的 hook。"""


async def call_next_stream(
    next_handler: Callable[..., AsyncGenerator[Any, None]],
    input_kwargs: dict[str, Any],
) -> AsyncGenerator[Any, None]:
    """onion 协议的"原样透传"样板。

    写一个只做旁路观察的中间件时，最正确的写法就是"把下游的事件一个不少地
    yield 出去"：:

        async def on_reply(self, agent, input_kwargs, next_handler):
            async for evt in call_next_stream(next_handler, input_kwargs):
                yield evt

    刻意不提供"收集完再一次性 yield"的变体 —— 那会破坏 AgentScope 的
    流式语义（``ReplyEndEvent`` 必须及时逃逸出整条链，
    ``.../middleware/_base.py:74-80`` 明确写了这条契约）。

    Args:
        next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。
        input_kwargs (`dict[str, Any]`): 要往下传的关键字参数。

    Yields:
        `Any`: 下游产生的每个事件。
    """
    async for item in next_handler(**input_kwargs):
        yield item


async def call_next(
    next_handler: Callable[..., Awaitable[Any]],
    input_kwargs: dict[str, Any],
) -> Any:
    """onion 协议的"原样透传"，用于 ``on_check_permission`` / ``on_model_call``
    这类返回单个值的 hook。

    Args:
        next_handler (`Callable[..., Awaitable[Any]]`): 链上的下一环。
        input_kwargs (`dict[str, Any]`): 要往下传的关键字参数。

    Returns:
        `Any`: 下游的返回值。
    """
    return await next_handler(**input_kwargs)


class HarnessMiddleware(MiddlewareBase):
    """``MiddlewareBase`` 的公共基类（契约 §3.8）。

    **本类刻意不实现任何 hook 方法。** 一旦在这里定义了 ``on_reply``，
    ``is_implemented("on_reply")`` 对所有子类都会返回 ``True``，
    而 Agent 的分流是在构造时一次性做的（``.../agent/_agent.py:218``），
    于是每个子类都会被塞进 7 条链里，然后在运行时抛
    ``RuntimeError: XxxMiddleware does not implement on_reply``
    （``.../middleware/_base.py:91``）。这是个静默的坑，务必别踩。

    Example:
        >>> class MyMw(HarnessMiddleware):                 # doctest: +SKIP
        ...     async def on_reply(self, agent, input_kwargs, next_handler):
        ...         async for evt in call_next_stream(next_handler, input_kwargs):
        ...             yield evt
        >>> MyMw().is_implemented("on_reply"), MyMw().is_implemented("on_acting")
        (True, False)
    """

    def is_implemented(self, hook: str) -> bool:
        """判断某个 hook 是否被本类（或它的子类）真正实现。

        语义与 ``MiddlewareBase.is_implemented`` 完全相同
        （``third_party/agentscope/src/agentscope/middleware/_base.py:55``）：
        用**恒等比较**判断子类有没有覆盖基类方法，而不是调用一次看它抛不抛
        —— 后者每构造一个 Agent 就要跑 7 次 ``try/except``。

        Args:
            hook (`str`): hook 名，取值见 :data:`HOOK_NAMES`。

        Returns:
            `bool`: 是否被覆盖。
        """
        base_method = getattr(MiddlewareBase, hook, None)
        sub_method = getattr(type(self), hook, None)
        return base_method is not sub_method

    def name(self) -> str:
        """中间件的稳定标识。

        默认返回类名。它与 ``get_middleware_key()`` 的区别：
        ``get_middleware_key`` 是 ``AgentState.middle_context`` 里的**状态键**
        （``.../middleware/_base.py:296``，需要跨进程稳定），本方法只用于
        日志与可观测，可以在子类里改得更短。

        Returns:
            `str`: 类名。
        """
        return type(self).__name__

    def implemented_hooks(self) -> list[str]:
        """列出本中间件真正进了哪几条链。

        Returns:
            `list[str]`: 按 :data:`HOOK_NAMES` 顺序排列的 hook 名。
        """
        return [hook for hook in HOOK_NAMES if self.is_implemented(hook)]

    def describe(self) -> dict[str, Any]:
        """返回一行可观测摘要。

        Returns:
            `dict[str, Any]`: ``{"middleware": ..., "hooks": [...]}``。
        """
        return {"middleware": self.name(), "hooks": self.implemented_hooks()}

    def log(self, **fields: Any) -> Any:
        """返回一个绑定了中间件标识的 loguru logger。

        Args:
            **fields (`Any`): 额外绑定的结构化字段。

        Returns:
            `Any`: ``logger.bind(...)`` 的结果。
        """
        return logger.bind(middleware=self.name(), **fields)


def implemented_hooks(middleware: MiddlewareBase) -> list[str]:
    """列出任意中间件的已实现 hook；对非 harness_kit 中间件也适用。

    Args:
        middleware (`MiddlewareBase`): 中间件实例。

    Returns:
        `list[str]`: hook 名列表。
    """
    return [hook for hook in HOOK_NAMES if middleware.is_implemented(hook)]


def filter_by_hook(
    middlewares: Iterable[MiddlewareBase],
    hook: str,
) -> list[MiddlewareBase]:
    """按 hook 过滤出会进那条链的中间件，复刻 Agent 的分流规则。

    用途：在把中间件交给 ``Agent`` 之前先自查一遍。
    例如 ``profile.middleware`` 里写了 ``guards``，但那个实现只包了
    ``on_acting`` —— 用本函数一眼就能看出来。

    Args:
        middlewares (`Iterable[MiddlewareBase]`): 中间件列表。
        hook (`str`): hook 名。

    Returns:
        `list[MiddlewareBase]`: 会进该链的中间件，保持原顺序。
    """
    return [mw for mw in middlewares if mw.is_implemented(hook)]


def onion_order(
    middlewares: Iterable[MiddlewareBase],
    hook: str,
) -> list[str]:
    """给出该链的"进入顺序"，也就是嵌套顺序。

    ``execute_chain`` 是 ``index=0`` 包住 ``index=1`` 包住 …… 的递归实现
    （``third_party/agentscope/src/agentscope/agent/_agent.py:3336``），
    所以**列表里第一个是最外层**：它先 enter、最后 exit。

    Args:
        middlewares (`Iterable[MiddlewareBase]`): 中间件列表。
        hook (`str`): hook 名。

    Returns:
        `list[str]`: 进入顺序的中间件名。
    """
    return [type(mw).__name__ for mw in filter_by_hook(middlewares, hook)]


def tool_schemas(tools: list["ToolBase"]) -> list[dict[str, Any]]:
    """把 ``ToolBase`` 列表转成"名字 + 描述"的摘要，便于日志。

    Args:
        tools (`list[ToolBase]`): 工具列表。

    Returns:
        `list[dict[str, Any]]`: 每项含 ``name`` / ``read_only``。
    """
    return [
        {"name": tool.name, "read_only": bool(tool.is_read_only)} for tool in tools
    ]
