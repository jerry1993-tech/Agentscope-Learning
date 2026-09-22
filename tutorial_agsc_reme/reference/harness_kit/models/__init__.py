# -*- coding: utf-8 -*-
"""harness_kit 的模型层：适配器、格式化器、价格与限流。

模块归属：

- :mod:`harness_kit.models.factory` —— 按 ``ModelSpec`` 造模型 + 健康检查；
- :mod:`harness_kit.models.adapters.base` —— ``HarnessChatModelAdapter``，
  所有自定义适配器的公共底座（**只覆写 ``_call_api``**）；
- :mod:`harness_kit.models.adapters.openai_compat` —— 面向任意 OpenAI 兼容
  端点的生产适配器；
- :mod:`harness_kit.models.adapters.echo` —— 离线确定性适配器；
- :mod:`harness_kit.models.formatter` —— ``FormatterBase`` 子类，吸收
  provider 之间的消息体差异；
- :mod:`harness_kit.models.pricing` —— 价格表与成本核算；
- :mod:`harness_kit.models.ratelimit` —— 令牌桶与指数退避重试。

**关于导入开销**：本 ``__init__`` 会 eager import 上面全部模块，因此
``import harness_kit.models`` 会连带把 ``agentscope.model`` / ``agentscope.tool``
拉进来。这是刻意的 —— 模型层脱离 AgentScope 没有任何意义。``import harness_kit``
（包根）仍然只加载标准库，:pep:`562` 的惰性 ``__getattr__`` 不变。
"""

from harness_kit.models.adapters.base import (
    HarnessChatModelAdapter,
    HarnessChatModelParameters,
    HarnessCredential,
)
from harness_kit.models.adapters.echo import EchoChatModel, build_echo_model
from harness_kit.models.adapters.openai_compat import (
    OpenAICompatChatModel,
    OpenAICompatParameters,
    build_openai_compat_model,
)
from harness_kit.models.factory import (
    DIRECT_PROVIDERS,
    ModelHealth,
    UnknownProviderError,
    abuild_chat_model,
    build_chat_model,
    describe_providers,
    health_check,
    register_providers,
)
from harness_kit.models.formatter import (
    HarnessOpenAICompatFormatter,
    NormalizeStats,
)
from harness_kit.models.pricing import (
    DEFAULT_PRICE_TABLE,
    EXAMPLE_PRICES,
    Price,
    PriceTable,
    UnknownPriceError,
    cost_of,
    default_price_table,
    format_usd,
    usage_to_row,
)
from harness_kit.models.ratelimit import (
    RateLimitedModel,
    RateLimitStats,
    RetryPolicy,
    TokenBucket,
    compute_delay,
    retry_with_backoff,
)

__all__ = [
    "DEFAULT_PRICE_TABLE",
    "DIRECT_PROVIDERS",
    "EXAMPLE_PRICES",
    "EchoChatModel",
    "HarnessChatModelAdapter",
    "HarnessChatModelParameters",
    "HarnessCredential",
    "HarnessOpenAICompatFormatter",
    "ModelHealth",
    "NormalizeStats",
    "OpenAICompatChatModel",
    "OpenAICompatParameters",
    "Price",
    "PriceTable",
    "RateLimitStats",
    "RateLimitedModel",
    "RetryPolicy",
    "TokenBucket",
    "UnknownPriceError",
    "UnknownProviderError",
    "abuild_chat_model",
    "build_chat_model",
    "build_echo_model",
    "build_openai_compat_model",
    "compute_delay",
    "cost_of",
    "default_price_table",
    "describe_providers",
    "format_usd",
    "health_check",
    "register_providers",
    "retry_with_backoff",
    "usage_to_row",
]
