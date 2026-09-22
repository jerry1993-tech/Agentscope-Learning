# -*- coding: utf-8 -*-
"""``HarnessChatModelAdapter`` —— 自定义模型适配器的公共底座。

**为什么需要它**：AgentScope 2.0.8 已经内置 10 个 provider 的适配器
（``third_party/agentscope/src/agentscope/model/__init__.py``：Anthropic /
DashScope / DeepSeek / Gemini / Ollama / OpenAI(Chat) / OpenAI(Responses) /
xAI / Moonshot / Volcengine），但它们各自独立实现，公共逻辑靠**复制**而非
继承传播 —— 例如「从 ``usage`` 里取缓存命中数」这段，DeepSeek 读
``prompt_cache_hit_tokens``（``.../model/_deepseek/_model.py:276``），
OpenAI 读 ``prompt_tokens_details.cached_tokens``
（``.../model/_openai_chat/_model.py:356``），两处代码形状几乎一样。
企业里接一个自建推理服务（vLLM / SGLang / TGI / 内部网关）时，你要么再抄一遍，
要么有一个基类。本模块就是那个基类。

**继承关系与唯一的覆写点**（这是本模块最重要的一条纪律）：

``ChatModelBase`` 里只有 ``_call_api`` 是 ``@abstractmethod``
（``third_party/agentscope/src/agentscope/model/_base.py:293``），而
``__call__``（``_base.py:182``）**不是**抽象方法，它内部装着三件重活：

1. ``for attempt in range(self.max_retries + 1)`` 的重试循环（``:208``）；
2. 把 ``asyncio.CancelledError`` 翻译成 ``finished_reason=INTERRUPTED`` 的
   正常终止块（``:219``、``:283``）；
3. ``_StreamAccumulator`` 流式聚合（``:260``，实现在
   ``model/_utils.py:199``），它顺手把「只有 usage 没有 choices」的载体块
   吸收掉、不暴露给消费者（``:270`` 的注释）。

覆写 ``__call__`` 等于把这三件事全部自己再写一遍 —— 契约 §3.4 因此把它定为
**铁律**：只覆写 ``_call_api``，绝不覆写 ``__call__``。

**真实验证过的两条事实**（本环境实测，见验证脚本）：

- ``ChatResponse`` / ``ChatUsage`` 是 ``DictMixin`` 子类，**本身就是 dict**
  （``third_party/agentscope/src/agentscope/_utils/_mixin.py:5``），
  所以 ``dict(usage)`` 能用，但**没有** ``to_dict()`` 方法；
- ``ChatResponse`` 上**没有** ``get_text_content()``（那是 ``Msg`` 的方法），
  文本要自己从 ``content`` 里挑 ``TextBlock``。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    ClassVar,
    Iterable,
    Literal,
)

from loguru import logger
from pydantic import ConfigDict, Field, SecretStr

from agentscope.credential import CredentialBase
from agentscope.formatter import FormatterBase, OpenAIChatFormatter
from agentscope.message import Msg
from agentscope.model import (
    ChatModelBase,
    ChatResponse,
    ChatUsage,
    FinishedReason,
)
from agentscope.tool import ToolChoice

from harness_kit.models.pricing import (
    DEFAULT_PRICE_TABLE,
    Price,
    PriceTable,
    cost_of,
    format_usd,
)

__all__ = [
    "HarnessChatModelAdapter",
    "HarnessChatModelParameters",
    "HarnessCredential",
    "UsageTotals",
]

_TOOL_CHOICE_LITERAL_MODES: frozenset[str] = frozenset(
    {"auto", "none", "required"},
)
"""``ToolChoice.mode`` 的三个字面量取值；其余取值一律当作「具体工具名」
（``third_party/agentscope/src/agentscope/tool/_types.py:178``）。"""


class HarnessCredential(CredentialBase):
    """harness_kit 自定义适配器使用的通用凭据。

    AgentScope 的 ``CredentialBase``（``third_party/agentscope/src/agentscope/
    credential/_base.py:15``）只要求子类提供 ``api_key`` / ``base_url`` 之类
    的字段；``get_chat_model_class()`` 是给
    ``CredentialFactory``（``credential/_factory.py``）反向查类用的，本 adapter
    不参与那个注册表，所以直接返回基类适配器。
    """

    model_config = ConfigDict(title="Harness Adapter Credential")

    type: Literal["harness_credential"] = "harness_credential"
    """凭据类型判别式。"""

    api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key；空串表示「本适配器不需要鉴权」（如本地自建端点）。",
    )
    """API key，``SecretStr`` 保证它不会被打印进日志。"""

    base_url: str | None = Field(
        default=None,
        description="OpenAI 兼容端点根地址，例如 ``https://api.deepseek.com``。",
    )
    """端点根地址；``None`` 时用 SDK 自己的默认值。"""

    @classmethod
    def get_chat_model_class(cls) -> type[ChatModelBase]:
        """返回消费此凭据的模型类。

        Returns:
            `type[ChatModelBase]`: :class:`HarnessChatModelAdapter`。
        """
        return HarnessChatModelAdapter


class HarnessChatModelParameters(ChatModelBase.Parameters):
    """适配器通用参数（各 provider 的参数并集里最常用的那几个）。

    ``ChatModelBase.Parameters``（``third_party/agentscope/src/agentscope/
    model/_base.py:40``）是一个**空的** pydantic 模型，注释写着 "Each subclass
    should implement this inner class to define its parameters"。这里给出一个
    够用的默认实现，子类可继续继承扩展（例如加 ``reasoning_effort``）。
    """

    max_tokens: int | None = Field(
        default=None,
        gt=0,
        description="单次输出上限；``None`` 交给 provider 默认值。",
    )

    temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        description="采样温度。",
    )

    top_p: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="nucleus 采样阈值。",
    )

    thinking_enable: bool | None = Field(
        default=None,
        description=(
            "是否开启 thinking。``None`` 表示**不发送**任何 thinking 开关，"
            "完全交给 provider 默认行为 —— 这是最安全的选择：不同兼容端点"
            "对未知字段的容忍度差别很大，实测 DeepSeek 收 "
            r"``extra_body={\"thinking\": {\"type\": \"disabled\"}}`` 正常，"
            "而某些自建端点会直接 400。"
        ),
    )

    extra_body: dict[str, Any] = Field(
        default_factory=dict,
        description="原样塞进请求体 ``extra_body`` 的 provider 私有字段。",
    )


class UsageTotals(dict):
    """累计用量账本（``dict`` 子类，便于直接 ``json.dumps``）。

    字段：``calls`` / ``input_tokens`` / ``output_tokens`` /
    ``cache_input_tokens`` / ``seconds`` / ``cost_usd``。
    """


class HarnessChatModelAdapter(ChatModelBase, ABC):
    """harness_kit 所有自定义模型的公共底座（**只覆写 ``_call_api``**）。

    它把「每个 provider 都要重写一遍」的东西一次写好：

    ============================== ===============================================
    能力                              方法
    ============================== ===============================================
    消息体转换（``Msg`` → dict）        :meth:`_format_messages` + ``self.formatter``
    流式增量累积                       :meth:`_accumulate`
    OpenAI 风格 usage 解析             :meth:`_usage_from_provider`
    ``ToolChoice`` → provider 参数     :meth:`_provider_tool_choice`
    价格核算与账本                     :meth:`cost_of` / :meth:`totals`
    可重试异常白名单                   :meth:`_get_retryable_exceptions`
    ============================== ===============================================

    子类只需要实现 :meth:`_call_api`。

    Args:
        model_name (`str`): 模型名（例如 ``deepseek-flash``）。
        stream (`bool`): 是否流式，默认 ``True``。
        pricing (`PriceTable | None`): 价格表；``None`` 时用
            :data:`~harness_kit.models.pricing.DEFAULT_PRICE_TABLE`。
        credential (`CredentialBase | None`): 凭据；``None`` 时用
            :class:`HarnessCredential` 按 ``api_key`` / ``base_url`` 现造。
        parameters (`HarnessChatModelParameters | None`): 采样参数；``None``
            时用该类的默认实例。
        api_key (`str | SecretStr | None`): 便捷入口，等价于传
            ``credential=HarnessCredential(api_key=...)``。
        base_url (`str | None`): 便捷入口，端点地址。
        max_retries (`int`): 交给 ``ChatModelBase.__call__`` 的快速重试次数。
        retry_delay (`float`): 快速重试的固定间隔（秒）。
        context_size (`int`): 上下文窗口，供上下文压缩使用。
        **kwargs (`Any`): 具体适配器自己的额外参数；基类不消费，原样存进
            :attr:`extra_kwargs`，子类可读。

    Raises:
        ValueError: ``model_name`` 为空。
    """

    adapter_name: ClassVar[str] = "harness-adapter"
    """适配器名，出现在日志与 ``totals()`` 里。"""

    formatter_factory: ClassVar[Callable[[], FormatterBase]] = (
        OpenAIChatFormatter
    )
    """默认 formatter 的构造器；子类覆写它来换掉消息体格式。

    **为什么这件事必须做**：AgentScope 的 ``Agent`` 会直接读
    ``self.model.formatter``（``third_party/agentscope/src/agentscope/
    agent/_agent.py:2066``，用于判断模型能吃哪些媒体类型），而 ``formatter``
    是在**每个具体模型类的 ``__init__`` 里**赋值的，**不在**
    ``ChatModelBase`` 上 —— 例如 ``.../model/_openai_chat/_model.py:163``、
    ``.../model/_deepseek/_model.py:127``。一个没有 ``.formatter`` 的
    ``ChatModelBase`` 子类能通过类型检查、能单独调用，但一进 ``Agent`` 就
    ``AttributeError``。这是「照契约写适配器」最容易漏的一步。
    """

    def __init__(
        self,
        *,
        model_name: str,
        stream: bool = True,
        pricing: PriceTable | None = None,
        credential: CredentialBase | None = None,
        parameters: HarnessChatModelParameters | None = None,
        formatter: FormatterBase | None = None,
        api_key: str | SecretStr | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        context_size: int = 65536,
        **kwargs: Any,
    ) -> None:
        """初始化公共底座。"""
        if not model_name:
            raise ValueError("HarnessChatModelAdapter 需要非空的 model_name")

        if credential is None:
            credential = HarnessCredential(
                api_key=api_key if api_key is not None else "",
                base_url=base_url,
            )
        if parameters is None:
            parameters = self.Parameters()

        super().__init__(
            credential=credential,
            model=model_name,
            parameters=parameters,
            stream=stream,
            max_retries=max_retries,
            retry_delay=retry_delay,
            context_size=context_size,
        )

        self.formatter: FormatterBase = (
            formatter or self.formatter_factory()
        )
        """消息格式化器；``Agent`` 会读它（见 :attr:`formatter_factory`）。"""

        self.price_table: PriceTable = pricing or DEFAULT_PRICE_TABLE
        self.extra_kwargs: dict[str, Any] = dict(kwargs)
        self.total_usage: UsageTotals = UsageTotals(
            calls=0,
            input_tokens=0,
            output_tokens=0,
            cache_input_tokens=0,
            seconds=0.0,
            cost_usd=0.0,
        )
        self.last_finish_reason: str | None = None
        """provider 原始 ``finish_reason`` 的最近一次观测值。

        为什么要单独存一份：``ChatResponse.metadata`` **不会**被流式聚合器
        搬运 —— ``_StreamAccumulator.build()``
        （``third_party/agentscope/src/agentscope/model/_utils.py:274-280``）
        只搬 ``content`` / ``id`` / ``usage`` / ``finished_reason``，
        而 ``finished_reason`` 只有 ``interrupted`` / ``completed`` 两个取值
        （``model/_model_response.py:22``），**provider 的**
        ``finish_reason``（``stop`` / ``length`` / ``tool_calls`` /
        ``content_filter``）在 AgentScope 里从头到尾没被读过。
        生产上 ``length``（被截断）与 ``content_filter``（被风控）必须能被
        上层看见，所以这里记在实例上，并在非流式响应里同时写进 ``metadata``。
        """

    # ------------------------------------------------------------------
    # 必须由子类实现
    # ------------------------------------------------------------------
    @abstractmethod
    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """调用底层 API（子类实现）。

        契约（与 ``ChatModelBase._call_api`` 完全一致）：

        - ``self.stream`` 为 ``True`` 时返回 **async generator**，
          每个元素 ``is_last=False``；**不要**自己补 ``is_last=True``
          的最终块，基类 ``__call__`` 会用 ``_StreamAccumulator`` 补；
        - ``self.stream`` 为 ``False`` 时返回单个 ``is_last=True`` 的
          ``ChatResponse``；
        - usage 只在**最后一个**（或唯一的）chunk 上给，别每个 chunk 都带，
          否则聚合器会反复覆写。

        Args:
            model_name (`str`): 模型名。
            messages (`list[Msg]`): 输入消息（先用 formatter 转成 provider 格式）。
            tools (`list[dict] | None`, optional): 工具 JSON schema。
            tool_choice (`ToolChoice | None`, optional): 工具选择。
            **kwargs (`Any`): provider 额外参数。

        Returns:
            `ChatResponse | AsyncGenerator[ChatResponse, None]`: 见上。
        """

    # ------------------------------------------------------------------
    # 公共能力
    # ------------------------------------------------------------------
    async def _format_messages(self, messages: list[Msg]) -> list[dict[str, Any]]:
        """把 ``Msg`` 列表转成给 HTTP 请求体用的消息列表。

        这一步**必须在 ``_call_api`` 里做**：``ChatModelBase.__call__`` 传给
        ``_call_api`` 的 ``messages`` 是 ``list[Msg]``（AgentScope 的内部
        表示），而 OpenAI SDK 只接受 ``list[dict]``。内置模型的 ``_call_api``
        第一行就是 ``await self.formatter.format(messages)``
        （``third_party/agentscope/src/agentscope/model/_openai_chat/_model.py:221``、
        ``.../model/_deepseek/_model.py:185``）。漏掉它会在 SDK 的
        ``json`` 序列化阶段抛一个和「消息格式」毫无关系的 TypeError。

        Args:
            messages (`list[Msg]`): AgentScope 消息。

        Returns:
            `list[dict[str, Any]]`: provider 消息体。
        """
        return await self.formatter.format(messages)

    @classmethod
    def _get_retryable_exceptions(cls) -> tuple[type[Exception], ...]:
        """白名单：网络抖动 / 超时 / 限流 / 5xx 值得重试，参数错误不值得。

        实现照抄 AgentScope 的**惰性导入**约定
        （``third_party/agentscope/src/agentscope/model/_base.py:100`` 的
        docstring："SDK exception types should be imported lazily inside the
        override so the SDK stays an optional dependency"），这样没装 openai
        也能 import 本模块。

        Returns:
            `tuple[type[Exception], ...]`: 可重试异常类型；没装 openai 时为空。
        """
        try:
            import openai
        except ImportError:  # pragma: no cover - 本环境已装 openai
            return ()
        return (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )

    def _accumulate(self, chunks: Iterable[ChatResponse]) -> ChatResponse:
        """把一个调用的所有流式增量拼成一个完整响应。

        直接复用 AgentScope 自己的累积能力
        ``ChatResponse.append_chat_response``（``third_party/agentscope/src/
        agentscope/model/_model_response.py:245``）：它按 block id 归并
        ``TextBlock.text`` / ``ThinkingBlock.thinking`` / ``ToolCallBlock.input``
        （**字符串拼接，不做 JSON 解析** —— 解析推迟到真正执行工具时）、
        并把 ``DataBlock`` 的 base64 分片按媒体类型合并；``usage`` 取最后一次
        非空的值。

        与 ``ChatModelBase.__call__`` 内部用的 ``_StreamAccumulator`` 的区别：
        后者是 O(n) 实现，为长工具参数（10 万字符级）优化
        （``model/_utils.py:203`` 的注释），但它是**私有**的；
        ``append_chat_response`` 是公开 API 且 O(n²)。教学场景用公开 API，
        生产热路径交给 ``__call__`` 里的私有实现 —— 两者结果一致。

        Args:
            chunks (`Iterable[ChatResponse]`): 增量块（``is_last=False`` 的）。

        Returns:
            `ChatResponse`: ``is_last=True`` 的完整响应；``finished_reason``
            沿用最后一个 ``INTERRUPTED`` 的块（若有）。
        """
        acc = ChatResponse(content=[], is_last=True)
        last_interrupt: FinishedReason | None = None
        for chunk in chunks:
            if chunk.finished_reason is FinishedReason.INTERRUPTED:
                last_interrupt = FinishedReason.INTERRUPTED
            acc.append_chat_response(chunk)
        if last_interrupt is not None:
            acc.finished_reason = last_interrupt
        return acc

    @staticmethod
    def _provider_tool_choice(
        tool_choice: ToolChoice | None,
        tool_names: list[str],
    ) -> str | dict[str, Any] | None:
        """把 :class:`~agentscope.tool.ToolChoice` 翻成 OpenAI 风格参数。

        映射规则（与 ``_deepseek/_model.py`` 的 ``_format_tools`` 一致）：

        ============================== ===================================
        ``ToolChoice``                  OpenAI 参数
        ============================== ===================================
        ``None``                        ``None``（不传该字段）
        ``mode="auto"``                 ``"auto"``
        ``mode="none"``                 ``"none"``
        ``mode="required"``             ``"required"``
        ``mode="<工具名>"``              ``{"type": "function",
                                        "function": {"name": "<工具名>"}}``
        ============================== ===================================

        为什么不把「强制某个工具」翻成 ``{"type": "function", ...}`` 之外的做法：
        ``ToolChoice`` 的 docstring 明确写了 —— 优先用 ``mode=<tool_name>``
        而不是 ``tools=["<tool_name>"]``，因为后者会改动发给模型的 tools 数组、
        让 **prompt cache 失效**（``third_party/agentscope/src/agentscope/
        tool/_types.py:195``）。

        Args:
            tool_choice (`ToolChoice | None`): AgentScope 的工具选择。
            tool_names (`list[str]`): 当前可用的工具名（用于提前报错）。

        Returns:
            `str | dict[str, Any] | None`: 可直接塞进请求体的值。

        Raises:
            ValueError: ``mode`` 既不是三个字面量之一，也不在 ``tool_names`` 里。
        """
        if tool_choice is None:
            return None

        mode = tool_choice.mode
        if mode in _TOOL_CHOICE_LITERAL_MODES:
            return mode
        if mode not in tool_names:
            raise ValueError(
                f"tool_choice.mode={mode!r} 既不是 "
                f"{sorted(_TOOL_CHOICE_LITERAL_MODES)}，也不在可用工具 "
                f"{sorted(tool_names)} 里；"
                "（third_party/agentscope/src/agentscope/tool/_types.py:178）",
            )
        return {"type": "function", "function": {"name": mode}}

    @staticmethod
    def _provider_tools(
        tools: list[dict] | None,
        tool_choice: ToolChoice | None,
    ) -> list[dict] | None:
        """按 ``tool_choice.tools`` 过滤工具列表（``None`` 时原样返回）。

        Args:
            tools (`list[dict] | None`): 工具 schema 列表。
            tool_choice (`ToolChoice | None`): 工具选择。

        Returns:
            `list[dict] | None`: 过滤后的列表。
        """
        if not tools or tool_choice is None or not tool_choice.tools:
            return tools
        keep = set(tool_choice.tools)
        return [
            schema
            for schema in tools
            if schema.get("function", {}).get("name") in keep
        ]

    @staticmethod
    def _usage_from_provider(
        raw_usage: Any,
        *,
        started: datetime,
    ) -> ChatUsage | None:
        """把 OpenAI 风格的 ``usage`` 对象解析成 :class:`ChatUsage`。

        一次吃掉三个 provider 方言的差异（这正是「适配器」该干的事）：

        ============================== ======================================
        源字段                           落到 ``ChatUsage``
        ============================== ======================================
        ``prompt_tokens``               ``input_tokens``
        ``completion_tokens``           ``output_tokens``
        ``prompt_cache_hit_tokens``     ``cache_input_tokens``（DeepSeek 专有）
        ``prompt_tokens_details.        ``cache_input_tokens``（OpenAI 专有）
        cached_tokens``
        ``cache_write_tokens``          ``cache_creation_input_tokens``
        （``prompt_tokens_details`` 内，少数实现才有）
        ============================== ======================================

        实测锚点（本环境 deepseek-flash）：一个带 6 个 chunk 的流式请求里，
        ``prompt_cache_hit_tokens=0`` 而 ``prompt_tokens_details.cached_tokens=0``；
        紧接着的第二次同前缀非流式请求两者都是 ``128``。可见两个字段在本
        provider 上**等价**，但别家不一定，所以两个都读、以显式字段优先。

        Args:
            raw_usage (`Any`): SDK 的 usage 对象（``None`` 表示这次没有 usage）。
            started (`datetime`): 请求开始时间，用于填 ``ChatUsage.time``。

        Returns:
            `ChatUsage | None`: 解析结果；``raw_usage`` 为 ``None`` 时返回 ``None``。
        """
        if raw_usage is None:
            return None

        details = getattr(raw_usage, "prompt_tokens_details", None)
        cache_hit = getattr(raw_usage, "prompt_cache_hit_tokens", None)
        if cache_hit is None and details is not None:
            cache_hit = getattr(details, "cached_tokens", None)
        cache_write = getattr(details, "cache_write_tokens", None) if details else None

        return ChatUsage(
            input_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
            output_tokens=int(
                getattr(raw_usage, "completion_tokens", 0) or 0,
            ),
            time=(datetime.now() - started).total_seconds(),
            # DictMixin 让 ChatUsage 本质是 dict，字段可以少给；这里显式给全
            cache_input_tokens=int(cache_hit or 0),
            cache_creation_input_tokens=int(cache_write or 0),
        )

    # ------------------------------------------------------------------
    # 账本
    # ------------------------------------------------------------------
    def price_of(self) -> Price:
        """取当前模型的价格。

        Returns:
            `Price`: 价格。

        Raises:
            UnknownPriceError: 价格表里没有该模型。
        """
        return self.price_table.lookup(self.model)

    def cost_of(self, usage: ChatUsage | None) -> float | None:
        """算一次调用的费用。

        Args:
            usage (`ChatUsage | None`): 用量；``None`` 时返回 ``None``。

        Returns:
            `float | None`: 美元金额；价格表查不到该模型时返回 ``None``
            （并打一条 warning，而不是抛异常 —— 记账失败不该拖垮主流程）。
        """
        if usage is None:
            return None
        try:
            price = self.price_of()
        except KeyError as exc:
            logger.warning(
                "价格表里没有 {}，本次调用不记账（{}）",
                self.model,
                exc,
            )
            return None
        return cost_of(usage, price)

    def _track_usage(self, usage: ChatUsage) -> float | None:
        """把一次用量累加进 :attr:`total_usage`。

        Args:
            usage (`ChatUsage`): 用量。

        Returns:
            `float | None`: 本次费用。
        """
        cost = self.cost_of(usage)
        self.total_usage["calls"] += 1
        self.total_usage["input_tokens"] += int(usage.input_tokens or 0)
        self.total_usage["output_tokens"] += int(usage.output_tokens or 0)
        self.total_usage["cache_input_tokens"] += int(
            getattr(usage, "cache_input_tokens", 0) or 0,
        )
        self.total_usage["seconds"] += float(getattr(usage, "time", 0.0) or 0.0)
        if cost is not None:
            self.total_usage["cost_usd"] += cost
        return cost

    def totals(self) -> dict[str, Any]:
        """返回累计账本的可打印视图。

        Returns:
            `dict[str, Any]`: 含 ``adapter`` / ``model`` / ``calls`` /
            token 数 / ``cost_usd`` / ``cost_pretty``。
        """
        snapshot: dict[str, Any] = {
            "adapter": self.adapter_name,
            "model": self.model,
            **dict(self.total_usage),
            "last_finish_reason": self.last_finish_reason,
        }
        snapshot["cost_pretty"] = format_usd(
            float(snapshot.get("cost_usd", 0.0)),
        )
        return snapshot

    def describe(self) -> str:
        """一行摘要（日志 / CLI 用）。

        Returns:
            `str`: 形如 ``openai_compat[deepseek-flash] stream=True``。
        """
        return (
            f"{self.adapter_name}[{self.model}] "
            f"stream={self.stream} "
            f"ctx={self.context_size} "
            f"formatter={type(self.formatter).__name__} "
            f"params={json.dumps(self.parameters.model_dump(exclude_none=True), ensure_ascii=False)}"
        )

    # ------------------------------------------------------------------
    # 结构化输出
    # ------------------------------------------------------------------
    @classmethod
    def _get_structured_output_fallback_exceptions(
        cls,
    ) -> tuple[type[Exception], ...]:
        """强制工具调用被 provider 拒绝（400）时，允许降级到下一个策略。

        见 ``ChatModelBase.generate_structured_output``
        （``third_party/agentscope/src/agentscope/model/_base.py:457``）的
        四段式阶梯：``forced`` → ``auto`` → ``no_think`` → ``none``。

        Returns:
            `tuple[type[Exception], ...]`: ``openai.BadRequestError``。
        """
        try:
            import openai
        except ImportError:  # pragma: no cover - 本环境已装 openai
            return ()
        return (openai.BadRequestError,)

    # ------------------------------------------------------------------
    # 请求体构造（子类复用）
    # ------------------------------------------------------------------
    def _sampling_kwargs(self) -> dict[str, Any]:
        """把 :attr:`parameters` 里「有值才发」的字段抽成请求参数。

        Returns:
            `dict[str, Any]`: ``max_tokens`` / ``temperature`` / ``top_p``
            中非 ``None`` 的项，外加 ``extra_body``（若非空）。
        """
        params: HarnessChatModelParameters = self.parameters  # type: ignore[assignment]
        payload: dict[str, Any] = {}
        for name in ("max_tokens", "temperature", "top_p"):
            value = getattr(params, name, None)
            if value is not None:
                payload[name] = value
        extra_body = dict(getattr(params, "extra_body", {}) or {})
        if extra_body:
            payload["extra_body"] = extra_body
        return payload

    def _thinking_extra_body(self) -> dict[str, Any]:
        """thinking 开关的 ``extra_body`` 片段（``None`` 时返回空 dict）。

        Returns:
            `dict[str, Any]`: 形如
            ``{"thinking": {"type": "disabled"}}``；未显式设置时为空。
        """
        params: HarnessChatModelParameters = self.parameters  # type: ignore[assignment]
        if params.thinking_enable is None:
            return {}
        return {
            "thinking": {
                "type": "enabled" if params.thinking_enable else "disabled",
            },
        }

    def _merge_extra_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把 thinking 开关合并进 ``payload["extra_body"]``（深合并一层）。

        Args:
            payload (`dict[str, Any]`): 已含 sampling 参数的请求体。

        Returns:
            `dict[str, Any]`: 原地修改并返回 ``payload``。
        """
        thinking = self._thinking_extra_body()
        if not thinking:
            return payload
        merged = dict(payload.get("extra_body") or {})
        merged.update(thinking)
        payload["extra_body"] = merged
        return payload

    def __repr__(self) -> str:
        """调试表示。

        Returns:
            `str`: 与 :meth:`describe` 相同。
        """
        return self.describe()
