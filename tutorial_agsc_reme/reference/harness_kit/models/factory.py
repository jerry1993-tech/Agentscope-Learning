# -*- coding: utf-8 -*-
"""模型工厂：把 :class:`~harness_kit.config.schema.ModelSpec` 变成可用模型。

**它和 ``harness_kit.registry`` 的分工**（两处都在造模型，但不是重复实现）：

================================== ==================================================
入口                                用途
================================== ==================================================
``registry._build_deepseek_model``   Layer 0 的**按名装配**：Profile YAML 里写
``registry._build_openai_model``     ``provider: deepseek``，builder 走注册表拿到
                                     AgentScope **原生** ``DeepSeekChatModel`` /
                                     ``OpenAIChatModel``
本模块 :func:`build_chat_model`      **直接构造**：给一段 Python 代码（脚本、
                                     评测、单测）用的同步入口，走 harness_kit
                                     自己的 ``OpenAICompatChatModel``
================================== ==================================================

**两条路的模型类不同，这是刻意设计，不是不一致**：

- 走原生类 = 拿到 AgentScope 打磨过的产品级行为（audio、``max_completion_tokens``
  改名、reasoning 开关等），适合官方端点；
- 走 :class:`~harness_kit.models.adapters.openai_compat.OpenAICompatChatModel`
  = 拿到「最小、可控、只说 OpenAI 协议本身」的客户端 + 成本核算 +
  ``finish_reason`` 保留，适合自建端点与企业网关。

契约 §3.4 明确要求 :func:`build_chat_model` 在 ``deepseek`` / ``openai`` 上
**都**返回 OpenAI 兼容适配器，本模块照办；想让 Layer 0 也能拿到它，用
:func:`register_providers` 注册一个**新名字** ``openai_compat``
（而不是覆盖 ``deepseek``/``openai``，覆盖会改变已在别处验证过的行为）。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.message import UserMsg
from agentscope.model import ChatModelBase, ChatResponse, FinishedReason

from harness_kit.models.adapters.base import HarnessChatModelAdapter
from harness_kit.models.adapters.echo import EchoChatModel
from harness_kit.models.adapters.openai_compat import (
    OpenAICompatChatModel,
    OpenAICompatParameters,
)
from harness_kit.models.pricing import DEFAULT_PRICE_TABLE, PriceTable
from harness_kit.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from harness_kit.config.schema import ModelSpec
    from harness_kit.registry import HarnessRegistry

__all__ = [
    "DIRECT_PROVIDERS",
    "ModelHealth",
    "UnknownProviderError",
    "abuild_chat_model",
    "build_chat_model",
    "health_check",
    "register_providers",
]

ProviderName = Literal["deepseek", "openai", "echo", "openai_compat"]


class UnknownProviderError(ValueError):
    """``ModelSpec.provider`` 不在已知提供方列表里。契约 §3.4 规定它是 ``ValueError``。"""


class ModelHealth(BaseModel):
    """一次健康检查的结果。契约 §3.4。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(description="这次探测是否成功拿到响应。")
    """是否成功。"""
    latency_ms: float = Field(ge=0, description="端到端耗时（毫秒）。")
    """耗时（毫秒）。"""
    error: str | None = Field(
        default=None,
        description="失败原因；成功时为 ``None``。",
    )
    """失败原因。"""
    provider: str | None = Field(
        default=None,
        description="实际使用的 provider 名（便于排查「池子里是哪台机器慢」）。",
    )
    """provider 名。"""
    model: str | None = Field(default=None, description="模型名。")
    """模型名。"""
    text: str | None = Field(
        default=None,
        description="模型返回的文本（截断到 200 字符），便于人眼确认「它真的在答」。",
    )
    """返回文本片段。"""


def _resolve_api_key(
    spec: "ModelSpec",
    settings: Settings,
) -> str:
    """从 ``spec.api_key_env`` 指向的环境变量里取 key。

    与 ``registry._resolve_credential``（``.../registry.py:555``）读的是**同一个
    来源**（``Settings.environ_overlay()``），因此两条路拿到的凭据一致。

    Args:
        spec (`ModelSpec`): 模型声明。
        settings (`Settings`): 全局设置。

    Returns:
        `str`: API key（可能为空串 —— 由调用方决定要不要报错）。

    Raises:
        ValueError: ``spec.api_key_env`` 为空（配置写错了）。
    """
    if not spec.api_key_env:
        raise ValueError(
            "ModelSpec.api_key_env 不能为空；它是**环境变量名**，不是 key 本身",
        )
    return settings.environ_overlay().get(spec.api_key_env, "") or ""


def _resolve_base_url(
    spec: "ModelSpec",
    settings: Settings,
) -> str | None:
    """从 ``spec.base_url_env`` 指向的环境变量里取 base url。

    Args:
        spec (`ModelSpec`): 模型声明。
        settings (`Settings`): 全局设置。

    Returns:
        `str | None`: base url；未配置时为 ``None``（交给 SDK 默认值）。
    """
    if not spec.base_url_env:
        return None
    return settings.environ_overlay().get(spec.base_url_env) or None


def _parameters_of(spec: "ModelSpec") -> OpenAICompatParameters:
    """把 ``ModelSpec`` 翻成 :class:`OpenAICompatParameters`。

    ``spec.extra`` 里认识的两个键被**取出**并转成具名参数
    （``top_p`` / ``thinking_enable``），剩下的原样进 ``extra_body``。

    为什么 ``thinking_enable`` 要单独处理而不是直接塞 ``extra_body``：
    ``extra_body`` 是原样透传，而 thinking 开关在不同 provider 上的**字段名
    与结构都不同**（DeepSeek 是 ``{"thinking": {"type": "disabled"}}``）；
    把它具名化之后，将来加一个 provider 的适配只需要改一处。

    Args:
        spec (`ModelSpec`): 模型声明。

    Returns:
        `OpenAICompatParameters`: 参数对象。

    Raises:
        ValueError: ``temperature`` / ``max_tokens`` 越界（pydantic 约束）。
    """
    extra = dict(spec.extra or {})
    return OpenAICompatParameters(
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        top_p=extra.pop("top_p", None),
        thinking_enable=extra.pop("thinking_enable", None),
        extra_body=extra,
    )


def _build_openai_compat(
    spec: "ModelSpec",
    settings: Settings,
    *,
    pricing: PriceTable | None,
) -> OpenAICompatChatModel:
    """构造 :class:`OpenAICompatChatModel`（deepseek / openai / 自建端点共用）。

    Args:
        spec (`ModelSpec`): 模型声明。
        settings (`Settings`): 全局设置。
        pricing (`PriceTable | None`): 价格表；``None`` 用内置示例表。

    Returns:
        `OpenAICompatChatModel`: 构造好的模型。

    Raises:
        ValueError: API key 未配置。
    """
    api_key = _resolve_api_key(spec, settings)
    if not api_key:
        raise ValueError(
            f"ModelSpec.api_key_env={spec.api_key_env!r} 指向的环境变量未定义或为空；"
            "请在 .env 里补上，或用 Settings(llm_api_key=...) 显式传入",
        )
    base_url = _resolve_base_url(spec, settings)
    model = OpenAICompatChatModel(
        model_name=spec.model_name,
        api_key=api_key,
        base_url=base_url,
        stream=spec.stream,
        parameters=_parameters_of(spec),
        pricing=pricing,
        timeout_s=spec.timeout_s,
    )
    logger.debug(
        "已构造 {}（model={} base_url={} stream={}）",
        model.describe(),
        spec.model_name,
        base_url or "<SDK 默认>",
        spec.stream,
    )
    return model


def build_chat_model(
    spec: "ModelSpec",
    *,
    settings: Settings,
    pricing: PriceTable | None = None,
) -> ChatModelBase:
    """按 ``spec.provider`` 分派，同步构造一个可用模型。契约 §3.4。

    分派表：

    ================== ==================================================
    ``spec.provider``   产出
    ================== ==================================================
    ``"deepseek"``      :class:`OpenAICompatChatModel`（DeepSeek 是
                        OpenAI 兼容端点，api.deepseek.com 实测通过）
    ``"openai"``        :class:`OpenAICompatChatModel`
    ``"openai_compat"`` :class:`OpenAICompatChatModel`（自建端点 / 企业网关）
    ``"echo"``          :class:`EchoChatModel`（离线，**不需要** API key）
    其它                 :class:`UnknownProviderError`
    ================== ==================================================

    Args:
        spec (`ModelSpec`): 模型声明。
        settings (`Settings`): 全局设置（凭据从这里解引用）。
        pricing (`PriceTable | None`): 价格表；``None`` 时用
            :data:`~harness_kit.models.pricing.DEFAULT_PRICE_TABLE`。

    Returns:
        `ChatModelBase`: 模型对象，可直接交给 ``Agent(model=...)``。

    Raises:
        UnknownProviderError: provider 不在分派表里。
        ValueError: 需要凭据的 provider 没拿到 API key。
    """
    provider = spec.provider
    table = pricing or DEFAULT_PRICE_TABLE

    if provider == "echo":
        model = EchoChatModel(
            model_name=spec.model_name or "echo",
            stream=spec.stream,
            pricing=table,
        )
        logger.debug("已构造离线模型 {}", model.describe())
        return model

    if provider in ("deepseek", "openai", "openai_compat"):
        return _build_openai_compat(spec, settings, pricing=table)

    raise UnknownProviderError(
        f"未知的 ModelSpec.provider={provider!r}；"
        f"可用：{sorted(DIRECT_PROVIDERS)}。"
        "若要接入自建端点，请用 provider='openai_compat'，"
        "并把端点地址配在 ModelSpec.base_url_env 指向的环境变量里。",
    )


DIRECT_PROVIDERS: dict[str, Callable[..., ChatModelBase]] = {
    "deepseek": _build_openai_compat,
    "openai": _build_openai_compat,
    "openai_compat": _build_openai_compat,
    "echo": lambda spec, settings, *, pricing=None: EchoChatModel(
        model_name=spec.model_name or "echo",
        stream=spec.stream,
        pricing=pricing,
    ),
}
"""provider 名 → 构造器。

与 :func:`build_chat_model` 里的 ``if`` 链表达的是**同一份事实**，冗余存在
是为了让「有哪些 provider」可以被程序查询（``describe_providers()``、
报错信息、CLI 补全），而不必读源码。
"""


def describe_providers() -> dict[str, str]:
    """列出可用 provider 及其一句话说明。

    Returns:
        `dict[str, str]`: provider 名 → 说明。
    """
    return {
        "deepseek": "DeepSeek 官方端点（OpenAI 兼容），走 harness_kit 适配器",
        "openai": "OpenAI 官方端点，走 harness_kit 适配器",
        "openai_compat": "任意 OpenAI 兼容端点（vLLM / SGLang / 企业网关）",
        "echo": "完全离线、确定性回放，用于测试与教学（无需 API key）",
    }


# ======================================================================
# 注册进 Layer 0
# ======================================================================
def register_providers(registry: "HarnessRegistry") -> list[str]:
    """把 harness_kit 的 provider 注册进 Layer 0 注册表。

    **只注册新名字，绝不覆盖已有条目**：

    - ``openai_compat`` 是新增的，注册它是安全的；
    - ``deepseek`` / ``openai`` **已经**由 ``registry._register_models``
      登记为「造原生类」的工厂（``.../registry.py:766``）。覆盖它们会改变
      已在别处验证过的行为，所以这里显式跳过并打一条 debug 日志。
    - ``echo`` 是惰性登记（指向 :mod:`harness_kit.models.adapters.echo`），
      本函数不碰它。

    Args:
        registry (`HarnessRegistry`): 目标注册表（未冻结）。

    Returns:
        `list[str]`: 本次真正注册的 provider 名。

    Raises:
        RegistryFrozenError: 注册表已冻结（由 ``register_model`` 抛出）。
    """
    registered: list[str] = []

    async def _openai_compat_factory(
        spec: "ModelSpec",
        ctx: Any = None,
    ) -> ChatModelBase:
        """``openai_compat`` 的 Layer 0 工厂。

        Args:
            spec (`ModelSpec`): 模型声明。
            ctx (`BuildContext | None`, optional): 装配上下文，提供 settings。

        Returns:
            `ChatModelBase`: 模型。
        """
        settings = ctx.settings if ctx is not None else Settings.from_env()
        return _build_openai_compat(spec, settings, pricing=DEFAULT_PRICE_TABLE)

    if "openai_compat" not in registry.names("model"):
        registry.register_model("openai_compat", _openai_compat_factory)
        registered.append("openai_compat")

    for name in ("deepseek", "openai"):
        if name in registry.names("model"):
            logger.debug("provider {} 已由注册表提供原生实现，跳过覆盖", name)
    return registered


async def abuild_chat_model(
    spec: "ModelSpec",
    *,
    settings: Settings,
    registry: "HarnessRegistry | None" = None,
) -> ChatModelBase:
    """异步版 :func:`build_chat_model`（走 Layer 0 注册表）。

    存在的理由：``HarnessBuilder`` 的装配是异步的，而 :func:`build_chat_model`
    按契约是同步的。这里给出桥接，让「直接构造」与「按名装配」两条路都能被
    同一段代码调用。

    Args:
        spec (`ModelSpec`): 模型声明。
        settings (`Settings`): 全局设置。
        registry (`HarnessRegistry | None`): 注册表；``None`` 时用
            ``HarnessRegistry.default()``。

    Returns:
        `ChatModelBase`: 模型。

    Raises:
        UnknownComponentError: provider 未在注册表登记。
    """
    if registry is None:
        from harness_kit.registry import HarnessRegistry

        registry = HarnessRegistry.default()
    factory = registry.get("model", spec.provider)
    return await factory(spec, None)


# ======================================================================
# 健康检查
# ======================================================================
async def health_check(
    model: ChatModelBase,
    *,
    timeout: float = 10.0,
    prompt: str = "ping",
) -> ModelHealth:
    """对着模型发一次**最小**的真实请求，判断它是否可用。契约 §3.4。

    为什么不用 ``count_tokens`` 之类的方法代替：那类方法不碰网络，**模型端点
    挂了它照样返回成功**。健康检查的全部价值就在于「真的走一次网络」。

    它处理了 ``ChatModelBase`` 的两种返回形态：``stream=True`` 时 ``__call__``
    返回 async generator（必须消费完才会真正发请求并关闭连接），``stream=False``
    时直接返回 :class:`ChatResponse`。

    Args:
        model (`ChatModelBase`): 待检查的模型。
        timeout (`float`): 总超时（秒），默认 ``10.0``。
        prompt (`str`): 探测用的用户消息，默认 ``"ping"``。

    Returns:
        `ModelHealth`: 检查结果。**本函数不抛异常** —— 失败被翻译成
        ``ok=False`` + ``error``，这样调用方可以直接把它塞进报告里。
    """
    started = time.perf_counter()
    provider = getattr(model, "adapter_name", None)
    model_name = getattr(model, "model", None)

    async def _probe() -> str:
        """发一次请求，返回响应文本。

        **为什么还要检查 ``finished_reason``**：``ChatModelBase.__call__``
        把 ``asyncio.CancelledError`` 翻译成
        ``ChatResponse(content=[], is_last=True, finished_reason=INTERRUPTED)``
        （``third_party/agentscope/src/agentscope/model/_base.py:219``）而**不是**
        往外抛。于是「超时被取消」在上层看起来是一次**成功但内容为空**的调用 ——
        不检查这一点，健康检查会把一个挂掉的端点报成 ``ok=True``。
        （这正是本模块第一版的行为，验收脚本抓到后补上的。）

        Returns:
            `str`: 响应文本（可能为空串）。

        Raises:
            RuntimeError: 响应被标记为 ``INTERRUPTED``（超时 / 取消）。
        """
        result = await model([UserMsg("user", prompt)])
        if isinstance(result, ChatResponse):
            if result.finished_reason == FinishedReason.INTERRUPTED:
                raise RuntimeError(
                    "响应 finished_reason=INTERRUPTED：请求被超时或取消"
                    "（ChatModelBase.__call__ 会把取消吞成空响应）",
                )
            # 非流式：__call__ 直接返回一个 is_last=True 的 ChatResponse
            return "".join(
                getattr(block, "text", "")
                for block in result.content
                if getattr(block, "type", None) == "text"
            )

        text_parts: list[str] = []
        async for chunk in result:  # type: ignore[union-attr]
            if getattr(chunk, "finished_reason", None) == (
                FinishedReason.INTERRUPTED
            ):
                raise RuntimeError(
                    "流式响应 finished_reason=INTERRUPTED：请求被超时或取消",
                )
            for block in getattr(chunk, "content", []):
                if getattr(block, "type", None) == "text":
                    text_parts.append(getattr(block, "text", ""))
        return "".join(text_parts)

    try:
        text = await asyncio.wait_for(_probe(), timeout=timeout)
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - started) * 1000
        return ModelHealth(
            ok=False,
            latency_ms=latency,
            error=f"健康检查超时（>{timeout:.1f}s）",
            provider=provider,
            model=model_name,
        )
    except Exception as exc:  # noqa: BLE001 - 健康检查必须吃掉所有异常
        latency = (time.perf_counter() - started) * 1000
        return ModelHealth(
            ok=False,
            latency_ms=latency,
            error=f"{type(exc).__name__}: {exc}",
            provider=provider,
            model=model_name,
        )

    latency = (time.perf_counter() - started) * 1000
    return ModelHealth(
        ok=True,
        latency_ms=latency,
        error=None,
        provider=provider,
        model=model_name,
        text=text[:200] if text else "",
    )


def _is_adapter(model: ChatModelBase) -> bool:
    """判断一个模型是不是 harness_kit 自己的适配器。

    Args:
        model (`ChatModelBase`): 模型对象。

    Returns:
        `bool`: 是则为 ``True``。
    """
    return isinstance(model, HarnessChatModelAdapter)
