# -*- coding: utf-8 -*-
"""``OpenAICompatChatModel`` —— 面向任意 OpenAI 兼容端点的生产级适配器。

**它解决的问题**：AgentScope 内置的 ``OpenAIChatModel``
（``third_party/agentscope/src/agentscope/model/_openai_chat/_model.py``）
是一个**产品级**实现，它会自动发送 audio 配置、把 ``max_tokens`` 改名成
``max_completion_tokens``、把 reasoning 开关注入请求体 —— 这些行为对
``api.openai.com`` 是对的，对一个自建推理服务（vLLM / SGLang / 内部网关）
往往是 400 的来源。企业里接自建端点需要的是「最小、可控、只说 OpenAI 协议
本身」的客户端，而**不是**再抄一遍产品级实现。

因此本适配器：

1. **只发协议内的字段**，不猜测 provider 私有开关（通过
   ``HarnessChatModelParameters.thinking_enable`` 显式开关）；
2. **多方言 usage 解析**（DeepSeek 的 ``prompt_cache_hit_tokens`` 与
   OpenAI 的 ``prompt_tokens_details.cached_tokens`` 都认，见
   :meth:`~harness_kit.models.adapters.base.HarnessChatModelAdapter._usage_from_provider`）；
3. **保留 provider 的 ``finish_reason``**（AgentScope 全链路都没读它，
   而 ``length`` / ``content_filter`` 在生产上必须可见）；
4. **自带成本核算**（调用结束顺手把这次的钱记进 ``total_usage``）。

**实测锚点**（本环境，2026-09-21，deepseek-flash @ api.deepseek.com）：

- 流式：``stream=True`` + ``stream_options={"include_usage": True}`` 时，
  最后一个 chunk 的 ``choices`` 为空、只带 ``usage``；
- 工具调用参数是**分片**下发的，同一 ``index`` 出现两次、第二次只带
  ``arguments`` 的续片；
- 非流式：``finish_reason="tool_calls"``，``prompt_cache_hit_tokens=128``；
- ``reasoning_content`` 字段在非流式响应里可能为 ``None``。

用法：

.. code-block:: python

    from harness_kit.models.adapters.openai_compat import (
        OpenAICompatChatModel,
    )

    import os

    model = OpenAICompatChatModel(
        model_name="deepseek-flash",
        api_key=os.getenv("OPENAI_API_KEY"),   # 永远不要写死 key
        base_url="https://api.deepseek.com",
    )
    await model(messages)          # 直接可用，就是一个 AgentScope 模型
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any, AsyncGenerator, ClassVar

from loguru import logger

from agentscope._utils._common import _generate_id
from agentscope.message import (
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)
from agentscope.model import ChatResponse, ChatUsage
from agentscope.tool import ToolChoice

from harness_kit.models.adapters.base import (
    HarnessChatModelAdapter,
    HarnessChatModelParameters,
    HarnessCredential,
)
from harness_kit.models.formatter import HarnessOpenAICompatFormatter

__all__ = [
    "OpenAICompatChatModel",
    "OpenAICompatParameters",
    "build_openai_compat_model",
]


class OpenAICompatParameters(HarnessChatModelParameters):
    """OpenAI 兼容端点的参数。

    比基类多一个 ``parallel_tool_calls``：多数自建端点在并发工具调用上
    行为不一致（尤其早期 vLLM），显式关掉能省掉一类偶发问题。
    """

    parallel_tool_calls: bool | None = None
    """``None`` = 不发送该字段（走 provider 默认）；``False`` = 显式关闭并发工具调用。"""

    presence_penalty: float | None = None
    """存在惩罚。"""

    frequency_penalty: float | None = None
    """频率惩罚。"""


class OpenAICompatChatModel(HarnessChatModelAdapter):
    """面向任意 OpenAI 兼容 ``/chat/completions`` 端点的适配器。

    Args:
        model_name (`str`): 模型名，例如 ``deepseek-flash``。
        api_key (`str | SecretStr | None`): API key。
        base_url (`str | None`): 端点根地址，例如 ``https://api.deepseek.com``。
            传 ``None`` 时用 openai SDK 的默认值（即官方端点）。
        stream (`bool`): 是否流式，默认 ``True``。
        parameters (`OpenAICompatParameters | None`): 采样参数。
        organization (`str | None`): 透传给 SDK 的组织标识。
        default_headers (`dict[str, str] | None`): 透传给 SDK 的额外请求头
            （很多企业网关用自定义 header 做租户路由）。
        timeout_s (`float | None`): 单次 HTTP 请求超时（秒）。``None`` 用
            SDK 默认值。**注意它与 ``max_retries`` 不是一回事**：超时是
            「这一次 HTTP 请求等多久」，重试是「失败后还试几次」。
        client (`Any | None`): 直接注入一个已构造的 ``openai.AsyncClient``
            （便于测试 / 复用连接池）；给了它时 ``api_key`` / ``base_url``
            / ``organization`` / ``default_headers`` 全部忽略。
        pricing (`PriceTable | None`): 价格表。
        max_retries (`int`): 基类快速重试次数，默认 ``3``。
        retry_delay (`float`): 快速重试间隔，默认 ``1.0`` 秒。
        context_size (`int`): 上下文窗口，默认 ``65536``。
        **kwargs (`Any`): 传给 :class:`HarnessChatModelAdapter` 的其他参数。

    Raises:
        RuntimeError: 未安装 ``openai`` 包。
    """

    adapter_name: ClassVar[str] = "openai_compat"

    formatter_factory: ClassVar[Any] = HarnessOpenAICompatFormatter
    """默认用 harness_kit 自己的 formatter（摘 ``name``、补 ``null`` content）。

    AgentScope 的 ``OpenAIChatFormatter`` 是面向 api.openai.com 调的；接
    DeepSeek / 自建端点时那三处差异需要被抹平，见
    :mod:`harness_kit.models.formatter`。
    """

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str | Any | None = None,
        base_url: str | None = None,
        stream: bool = True,
        parameters: OpenAICompatParameters | None = None,
        organization: str | None = None,
        default_headers: dict[str, str] | None = None,
        client: Any | None = None,
        pricing: Any | None = None,
        timeout_s: float | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        context_size: int = 65536,
        **kwargs: Any,
    ) -> None:
        """构造客户端并完成基类初始化。"""
        if client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - 本环境已装
                raise RuntimeError(
                    "OpenAICompatChatModel 需要 openai 包：pip install openai",
                ) from exc

            credential = HarnessCredential(
                api_key=api_key if api_key is not None else "",
                base_url=base_url,
            )
            client_kwargs: dict[str, Any] = {
                "api_key": credential.api_key.get_secret_value(),
            }
            if credential.base_url:
                client_kwargs["base_url"] = credential.base_url
            if organization is not None:
                client_kwargs["organization"] = organization
            if default_headers:
                client_kwargs["default_headers"] = dict(default_headers)
            if timeout_s is not None:
                client_kwargs["timeout"] = timeout_s
            client = openai.AsyncClient(**client_kwargs)

        super().__init__(
            model_name=model_name,
            stream=stream,
            pricing=pricing,
            parameters=parameters or OpenAICompatParameters(),
            max_retries=max_retries,
            retry_delay=retry_delay,
            context_size=context_size,
            **kwargs,
        )
        self.client = client
        """底层 ``openai.AsyncClient``；要换 base_url 就整个换掉它。"""

    # ------------------------------------------------------------------
    # 请求体
    # ------------------------------------------------------------------
    def _request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
        tool_choice: ToolChoice | None,
    ) -> dict[str, Any]:
        """组装 ``chat.completions.create`` 的参数。

        参数名里的 ``messages`` 已经是 **formatter 转换后的 dict 列表**，
        不是 ``Msg`` 列表 —— 这一层不负责格式化。

        Args:
            messages (`list[dict[str, Any]]`): 已经格式化好的 provider 消息体。
            tools (`list[dict] | None`): 工具 schema。
            tool_choice (`ToolChoice | None`): 工具选择。

        Returns:
            `dict[str, Any]`: 可直接展开进 ``create(**kwargs)`` 的字典。
        """
        params: OpenAICompatParameters = self.parameters  # type: ignore[assignment]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": self.stream,
        }
        payload.update(self._sampling_kwargs())

        if params.presence_penalty is not None:
            payload["presence_penalty"] = params.presence_penalty
        if params.frequency_penalty is not None:
            payload["frequency_penalty"] = params.frequency_penalty

        if self.stream:
            # 没有它，流式响应里拿不到 usage，成本核算直接失效。
            # 实测：DeepSeek 与官方 OpenAI 都支持；少数老网关不认会直接报错，
            # 那种情况请把 stream 设成 False（或用 non_stream_usage_only 思路，
            # 即干脆关流式）。
            payload["stream_options"] = {"include_usage": True}

        fmt_tools = self._provider_tools(tools, tool_choice)
        if fmt_tools:
            payload["tools"] = fmt_tools
            if params.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = params.parallel_tool_calls

        fmt_choice = self._provider_tool_choice(
            tool_choice,
            [t.get("function", {}).get("name", "") for t in (fmt_tools or [])],
        )
        if fmt_choice is not None:
            payload["tool_choice"] = fmt_choice

        return self._merge_extra_body(payload)

    # ------------------------------------------------------------------
    # 唯一的抽象方法实现
    # ------------------------------------------------------------------
    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """调用 OpenAI 兼容端点。

        ``self.stream`` 为真时返回 async generator（增量块，不补终块），
        否则返回单个 ``is_last=True`` 的 :class:`ChatResponse`。

        Args:
            model_name (`str`): 模型名（基类传入 ``self.model``）。
            messages (`list[Msg]`): 输入消息（AgentScope 的 ``Msg`` 对象）。
            tools (`list[dict] | None`, optional): 工具 schema。
            tool_choice (`ToolChoice | None`, optional): 工具选择。
            **kwargs (`Any`): 覆盖请求体字段（如 ``temperature=0``）。

        Returns:
            `ChatResponse | AsyncGenerator[ChatResponse, None]`: 见上。
        """
        formatted = await self._format_messages(messages)
        payload = self._request_kwargs(formatted, tools, tool_choice)
        payload.update(kwargs)
        start = datetime.now()

        logger.debug(
            "{} 发起请求 model={} stream={} msgs={} tools={}",
            self.adapter_name,
            payload.get("model"),
            payload.get("stream"),
            len(formatted),
            len(payload.get("tools") or []),
        )

        response = await self.client.chat.completions.create(**payload)

        if self.stream:
            return self._parse_stream(start, response)
        # **必须 await**：``_parse_completion`` 是 ``async def``。少一个 await
        # 会把「一个 coroutine 对象」当成 ``ChatResponse`` 交给基类
        # ``__call__``，而 ``__call__``（``.../model/_base.py:257``）看到
        # 非 ``ChatResponse`` 就当成 async generator 去 ``async for``，
        # 于是报 ``TypeError: 'async for' requires an object with __aiter__
        # method``，同时那条**真实发出的 HTTP 请求的结果被整个丢掉**
        # （验收脚本抓到的第二个真实 bug）。
        return await self._parse_completion(start, response)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    async def _parse_stream(
        self,
        start: datetime,
        response: Any,
    ) -> AsyncGenerator[ChatResponse, None]:
        """解析流式响应。

        **block id 的选择**：``ToolCallBlock.id`` 用 provider 给的
        ``tool_call.id``（DeepSeek / OpenAI 都在第一个分片里下发），
        只在它为空时退回本地生成的 ``<response_id>-tool-<index>``。

        为什么不学 AgentScope 那样把 call id 塞进 block 的额外字段：
        **实测行不通** ——
        ``ToolCallBlock`` 的 ``model_config`` 只有 ``use_enum_values=True``
        （``third_party/agentscope/src/agentscope/message/_block.py:141``），
        **没有** ``extra="allow"``（``ThinkingBlock`` 才有，见同文件 ``:36``）。
        所以 ``append_tool_call(..., call_id=...)`` 会直接抛
        ``ValueError: "ToolCallBlock" object has no field "call_id"``。
        验证脚本里把这条当作一个真实的**边界分支**跑过。

        工具调用的 ``name`` 只在第一个分片出现，后续分片为空，因此这里缓存
        ``index -> (call_id, name)``（与 ``.../_openai_chat/_model.py:435``
        的 ``tool_call_mapping`` 同一手法）。

        Args:
            start (`datetime`): 请求开始时间。
            response (`Any`): SDK 的 ``AsyncStream``。

        Yields:
            `ChatResponse`: 增量块。
        """
        response_id: str = _generate_id()
        text_id: str = _generate_id()
        thinking_id: str = _generate_id()
        # index -> (call_id, name)，两者都只在第一个分片里出现
        index_to_call: "OrderedDict[int, tuple[str, str]]" = OrderedDict()
        usage: ChatUsage | None = None
        # usage 是否已经"发出去过"。见本函数末尾的补发逻辑说明。
        usage_surfaced: bool = False
        finish_reason: str | None = None

        async with response as stream:
            async for chunk in stream:
                response_id = getattr(chunk, "id", None) or response_id

                if getattr(chunk, "usage", None):
                    usage = self._usage_from_provider(chunk.usage, started=start)

                if not chunk.choices:
                    # 只有 usage 的收尾块。基类 __call__ 会吸收它；
                    # 这里也照发，保证 __call__ 与直接消费本 generator 的
                    # 调用方（如 generate_structured_output）行为一致。
                    if usage is not None:
                        usage_surfaced = True
                        yield ChatResponse(
                            content=[],
                            is_last=False,
                            id=response_id,
                            usage=usage,
                        )
                    continue

                choice = chunk.choices[0]
                fr = getattr(choice, "finish_reason", None)
                if fr:
                    finish_reason = fr
                    self.last_finish_reason = fr

                delta = choice.delta
                delta_res = ChatResponse(
                    content=[],
                    is_last=False,
                    id=response_id,
                    usage=usage,
                )

                # 思考内容：DeepSeek 用 reasoning_content，部分网关用 reasoning
                thinking = getattr(delta, "reasoning_content", None)
                if not isinstance(thinking, str):
                    thinking = getattr(delta, "reasoning", None)
                if isinstance(thinking, str) and thinking:
                    delta_res.append_thinking(
                        block_id=thinking_id,
                        thinking=thinking,
                    )

                text = getattr(delta, "content", None) or ""
                if text:
                    delta_res.append_text(block_id=text_id, text=text)

                for tool_call in getattr(delta, "tool_calls", None) or []:
                    index = tool_call.index
                    fn = getattr(tool_call, "function", None)
                    name = getattr(fn, "name", None) if fn else None
                    args = getattr(fn, "arguments", None) if fn else None
                    call_id = getattr(tool_call, "id", None)

                    if index not in index_to_call:
                        index_to_call[index] = (
                            call_id or f"{response_id}-tool-{index}",
                            name or "unknown",
                        )
                    block_id, stored_name = index_to_call[index]
                    delta_res.append_tool_call(
                        block_id=block_id,
                        name=name or stored_name,
                        input=args or "",
                    )

                if delta_res.content:
                    if delta_res.usage is not None:
                        usage_surfaced = True
                    yield delta_res

        # ------------------------------------------------------------------
        # 补发 usage 载体块（真实 bug 修复，2026-09-21 验证脚本抓到）
        # ------------------------------------------------------------------
        # DeepSeek / 多数 OpenAI 兼容网关把 usage 挂在**带 finish_reason 的
        # 那一块**上，而那一块的 delta 是空的 —— 上面的代码会把它建成
        # ``delta_res``，但 ``if delta_res.content`` 为假，于是这块连同刚
        # 解析出来的 ``usage`` 一起被丢掉。后果是消费者（包括
        # ``harness_kit.middleware.logging.LoggingMiddleware`` 与
        # ``BudgetMiddleware``）读 ``response.usage`` 永远是 ``None``，
        # 而适配器自己的日志里 usage 却清清楚楚 —— 典型的"日志有、数据无"。
        #
        # 修法沿用 AgentScope 基类的既有约定：补发一个 content 为空的
        # **载体块**，``ChatModelBase.__call__`` 的 ``_stream()`` 会把它的
        # usage 吸收进 ``_StreamAccumulator``，最终在 ``is_last=True`` 的
        # ``build()`` 上暴露给上层（
        # ``third_party/agentscope/src/agentscope/model/_base.py:262-288``）。
        if usage is not None and not usage_surfaced:
            yield ChatResponse(
                content=[],
                is_last=False,
                id=response_id,
                usage=usage,
            )

        if finish_reason:
            logger.debug(
                "{} 流式结束 finish_reason={} usage={}",
                self.adapter_name,
                finish_reason,
                dict(usage) if usage else None,
            )
            if usage is not None:
                self._track_usage(usage)

    async def _parse_completion(
        self,
        start: datetime,
        response: Any,
    ) -> ChatResponse:
        """解析非流式响应。

        Args:
            start (`datetime`): 请求开始时间。
            response (`Any`): SDK 的 ``ChatCompletion``。

        Returns:
            `ChatResponse`: ``is_last=True`` 的完整响应。
        """
        blocks: list[Any] = []
        finish_reason: str | None = None
        call_ids: list[str] = []

        if response.choices:
            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                self.last_finish_reason = finish_reason

            reasoning = getattr(choice.message, "reasoning_content", None)
            if not isinstance(reasoning, str):
                reasoning = getattr(choice.message, "reasoning", None)
            if isinstance(reasoning, str) and reasoning:
                blocks.append(ThinkingBlock(thinking=reasoning))

            if choice.message.content:
                blocks.append(TextBlock(text=choice.message.content))

            for tool_call in choice.message.tool_calls or []:
                call_ids.append(tool_call.id)
                blocks.append(
                    ToolCallBlock(
                        id=tool_call.id,
                        name=tool_call.function.name,
                        input=tool_call.function.arguments or "{}",
                    ),
                )

        usage = self._usage_from_provider(
            getattr(response, "usage", None),
            started=start,
        )
        if usage is not None:
            self._track_usage(usage)

        return ChatResponse(
            content=blocks,
            is_last=True,
            id=getattr(response, "id", None) or _generate_id(),
            usage=usage,
            metadata={
                "provider_finish_reason": finish_reason,
                "tool_call_ids": call_ids,
            },
        )


def build_openai_compat_model(
    spec: Any,
    ctx: Any = None,
) -> OpenAICompatChatModel:
    """按 :class:`~harness_kit.config.schema.ModelSpec` 构造适配器。

    这是 :func:`harness_kit.models.factory.register_providers` 注册进
    Layer 0 注册表的工厂函数，签名必须与
    ``harness_kit.registry._LazyFactory`` 的约定一致（``spec`` 位置参数 +
    可选 ``ctx``）。

    Args:
        spec (`Any`): 至少含 ``model`` / ``api_key`` / ``base_url`` /
            ``temperature`` / ``max_tokens`` / ``stream`` / ``timeout_s``
            / ``extra`` 属性的对象（``ModelSpec`` 即可）。
        ctx (`Any`, optional): ``BuildContext``；本工厂不消费。

    Returns:
        `OpenAICompatChatModel`: 构造好的模型。
    """
    extra = dict(getattr(spec, "extra", None) or {})
    parameters = OpenAICompatParameters(
        temperature=getattr(spec, "temperature", None),
        max_tokens=getattr(spec, "max_tokens", None),
        top_p=extra.pop("top_p", None),
        thinking_enable=extra.pop("thinking_enable", None),
        extra_body=extra,
    )
    return OpenAICompatChatModel(
        model_name=getattr(spec, "model"),
        api_key=getattr(spec, "api_key", None),
        base_url=getattr(spec, "base_url", None),
        stream=bool(getattr(spec, "stream", True)),
        parameters=parameters,
        context_size=int(getattr(spec, "context_size", 65536) or 65536),
    )
