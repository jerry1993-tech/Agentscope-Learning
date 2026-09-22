# -*- coding: utf-8 -*-
"""统一结构化日志中间件（契约 §3.8，第 8 讲）。

挂三个 hook，只观察不改写：

- ``on_reply`` —— 一次 reply 的开始/结束、事件条数、最终文本长度；
- ``on_acting`` —— 每次工具调用的名字、参数摘要、结果状态与字符数；
- ``on_model_call`` —— 每次模型调用的消息条数/工具数，以及 ``usage`` 里的
  ``input_tokens`` / ``output_tokens``。

**为什么用 loguru 的 ``bind`` 而不是 f-string 拼接？**
生产里日志要进 ELK / Loki，结构化字段可以直接筛；拼进消息体的字段只能全文搜。
本模块把 ``session_id`` / ``agent`` / ``tool`` / ``iter`` 等全部放进 ``bind``，
消息体只保留人类读的那一句。

token 用量的读取位置有个真实的坑（``_recon/code/07_mw_custom_token_logger.py``
里踩过）：``ChatResponse`` 上**没有** ``input_tokens`` 属性，用量挂在
``response.usage``（``ChatUsage``，``third_party/agentscope/src/agentscope/
model/_model_usage.py:10``）上；``getattr(response, "input_tokens", None)``
会静默拿到 ``None`` 而不会报错。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from loguru import logger as _loguru_logger

from harness_kit.middleware.base import HarnessMiddleware, call_next

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from loguru import Logger

    from agentscope.agent import Agent

__all__ = ["LoggingMiddleware"]

_PREVIEW_SEPARATOR: str = "…"
"""截断预览时的省略标记。"""

_FALSEY: frozenset[str] = frozenset({"", "0", "false", "no", "off", "none"})
"""``_as_bool`` 认作假值的字符串（大小写不敏感）。"""


def _as_bool(value: Any) -> bool:
    """把 Profile 里可能写成字符串的布尔值归一。

    ``bool("false")`` 是 ``True`` —— 这是配置系统里最经典的静默 bug，
    所以这里显式处理字符串。

    Args:
        value (`Any`): 原始值。

    Returns:
        `bool`: 归一后的布尔值。
    """
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    return bool(value)


def _preview(value: Any, *, max_chars: int) -> str:
    """把任意值压成一行短预览。

    Args:
        value (`Any`): 待预览的值（字符串、dict、列表等）。
        max_chars (`int`): 最大字符数；``<= 0`` 表示不截断。

    Returns:
        `str`: 单行预览文本。
    """
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 极端不可序列化对象
            text = repr(value)
    text = " ".join(text.split())
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + _PREVIEW_SEPARATOR
    return text


def _usage_of(response: Any) -> tuple[int, int]:
    """从 ``ChatResponse`` 里安全地取出 token 用量。

    Args:
        response (`Any`): ``on_model_call`` 的返回值（可能是 ``ChatResponse``
            也可能是 ``AsyncGenerator``）。

    Returns:
        `tuple[int, int]`: ``(input_tokens, output_tokens)``；取不到时为 ``(0, 0)``。
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


class LoggingMiddleware(HarnessMiddleware):
    """结构化日志中间件（契约 §3.8）。

    Args:
        logger (`Logger | None`): 自定义 loguru logger（例如已经 ``bind`` 过
            ``service`` / ``env`` 的那一个）；``None`` 时用全局 ``logger``。
        level (`str`): 进入/退出日志的级别，默认 ``"INFO"``。
        max_preview_chars (`int`): 入参/出参预览的最大字符数，默认 200；
            ``0`` 表示不截断（不推荐，工具结果可能几十 KB）。
        log_tool_input (`bool`): 是否打印工具入参预览，默认 ``True``。
            涉及隐私的部署里应关掉，改用 :class:`~harness_kit.middleware.redact.RedactMiddleware`。

    Example:
        >>> from agentscope.agent import Agent                     # doctest: +SKIP
        >>> agent = Agent(..., middlewares=[LoggingMiddleware(level="DEBUG")])
    """

    def __init__(
        self,
        *,
        logger: "Logger | None" = None,
        level: str = "INFO",
        max_preview_chars: int = 200,
        log_tool_input: bool = True,
    ) -> None:
        """初始化。

        Args:
            logger (`Logger | None`): 见类文档。
            level (`str`): 日志级别。
            max_preview_chars (`int`): 预览长度上限。
            log_tool_input (`bool`): 是否打印工具入参。
        """
        self._logger = logger or _loguru_logger
        # Profile / YAML 里这些字段可能是字符串或 "false"，而
        # harness_kit.registry 的 _spec_class_adapter 是 ``target(**spec.params)``，
        # 不做类型转换 —— 这里显式归一。
        self.level = str(level).upper()
        self.max_preview_chars = int(max_preview_chars)
        self.log_tool_input = _as_bool(log_tool_input)

        self.reply_count: int = 0
        self.event_count: int = 0
        self.tool_calls: int = 0
        self.model_calls: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _bound(self, **fields: Any) -> Any:
        """绑定结构化字段。

        Args:
            **fields (`Any`): 字段。

        Returns:
            `Any`: loguru logger。
        """
        return self._logger.bind(middleware=self.name(), **fields)

    def snapshot(self) -> dict[str, int]:
        """返回累计计数快照，供评测/看板消费。

        Returns:
            `dict[str, int]`: 各计数器。
        """
        return {
            "replies": self.reply_count,
            "events": self.event_count,
            "tool_calls": self.tool_calls,
            "model_calls": self.model_calls,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
        }

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """记录一次 reply 的起止与事件量。

        Args:
            agent (`Agent`): 发起 reply 的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``inputs``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的事件。
        """
        inputs = input_kwargs.get("inputs")
        self.reply_count += 1
        events = 0
        self._bound(
            agent=agent.name,
            reply_no=self.reply_count,
            input_preview=_preview(inputs, max_chars=self.max_preview_chars),
        ).log(self.level, "reply #{} 开始", self.reply_count)
        try:
            async for event in next_handler(**input_kwargs):
                events += 1
                self.event_count += 1
                yield event
        except Exception as exc:
            self._bound(
                agent=agent.name,
                reply_no=self.reply_count,
                events=events,
                error=f"{type(exc).__name__}: {exc}",
            ).error("reply #{} 异常终止", self.reply_count)
            raise
        self._bound(
            agent=agent.name,
            reply_no=self.reply_count,
            events=events,
        ).log(self.level, "reply #{} 结束（{} 个事件）", self.reply_count, events)

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """记录一次工具调用的入参与结果。

        Args:
            agent (`Agent`): 执行工具调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``tool_call``（``ToolCallBlock``）。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的 ``ToolChunk`` / ``ToolResponse``。
        """
        tool_call = input_kwargs.get("tool_call")
        tool_name = getattr(tool_call, "name", "<unknown>")
        call_id = getattr(tool_call, "id", "")
        self.tool_calls += 1

        fields: dict[str, Any] = {
            "agent": agent.name,
            "tool": tool_name,
            "call_id": call_id,
            "tool_call_no": self.tool_calls,
        }
        if self.log_tool_input:
            fields["input_preview"] = _preview(
                getattr(tool_call, "input", None),
                max_chars=self.max_preview_chars,
            )
        self._bound(**fields).log(self.level, "工具调用 {} 开始", tool_name)

        chunks = 0
        last: Any = None
        try:
            async for item in next_handler(**input_kwargs):
                chunks += 1
                last = item
                yield item
        except Exception as exc:
            self._bound(
                **{
                    **fields,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            ).error("工具 {} 抛异常", tool_name)
            raise

        state = getattr(last, "state", None)
        chars = sum(
            len(getattr(block, "text", "") or "")
            for block in (getattr(last, "content", None) or [])
        )
        self._bound(
            **{
                **fields,
                "state": str(state),
                "chunks": chunks,
                "result_chars": chars,
            },
        ).log(self.level, "工具 {} 结束（{}）", tool_name, state)

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Any],
    ) -> Any:
        """记录一次模型调用的输入规模与 token 用量。

        返回值的形态有两种（``ChatResponse`` 或 ``AsyncGenerator[ChatResponse, None]``，
        见 ``third_party/agentscope/src/agentscope/middleware/_base.py:213``），
        流式时必须包一层生成器才能在最后一个 chunk 上读到 ``usage``。

        Args:
            agent (`Agent`): 发起调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``messages`` / ``tools`` / ``current_model``。
            next_handler (`Callable[..., Any]`): 链上的下一环。

        Returns:
            `Any`: 原样透传的 ``ChatResponse`` 或它的异步生成器。
        """
        self.model_calls += 1
        model = input_kwargs.get("current_model")
        messages = input_kwargs.get("messages") or []
        tools = input_kwargs.get("tools") or []
        base_fields: dict[str, Any] = {
            "agent": agent.name,
            "model": getattr(model, "model", "<unknown>"),
            "call_no": self.model_calls,
            "messages": len(messages),
            "tools": len(tools),
        }
        self._bound(**base_fields).log(
            self.level,
            "模型调用 #{} 开始（{} 条消息 / {} 个工具）",
            self.model_calls,
            len(messages),
            len(tools),
        )

        result = await call_next(next_handler, input_kwargs)
        if hasattr(result, "__aiter__"):
            return self._wrap_stream(result, base_fields)

        self._record_usage(base_fields, result)
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[Any, None],
        base_fields: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        """包住流式响应：透传每个 chunk，并在最后一个 chunk 上记 token。

        Args:
            stream (`AsyncGenerator[Any, None]`): 下游的流。
            base_fields (`dict[str, Any]`): 已绑定的结构化字段。

        Yields:
            `Any`: 原样透传的 ``ChatResponse`` chunk。
        """
        async for chunk in stream:
            self._record_usage(base_fields, chunk, only_if_present=True)
            yield chunk

    def _record_usage(
        self,
        base_fields: dict[str, Any],
        response: Any,
        *,
        only_if_present: bool = False,
    ) -> None:
        """累计并打印 token 用量。

        Args:
            base_fields (`dict[str, Any]`): 已绑定的结构化字段。
            response (`Any`): ``ChatResponse``。
            only_if_present (`bool`): ``True`` 时 ``usage`` 为 ``None`` 就静默跳过
                （流式响应的中间 chunk 没有 usage）。
        """
        input_tokens, output_tokens = _usage_of(response)
        if only_if_present and input_tokens == 0 and output_tokens == 0:
            return
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self._bound(
            **{
                **base_fields,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cum_input_tokens": self.total_input_tokens,
                "cum_output_tokens": self.total_output_tokens,
            },
        ).log(
            self.level,
            "模型调用 #{} 结束（in={} out={}）",
            base_fields["call_no"],
            input_tokens,
            output_tokens,
        )
