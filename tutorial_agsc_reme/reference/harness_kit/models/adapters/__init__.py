# -*- coding: utf-8 -*-
"""harness_kit 的模型适配器集合。

三个模块，三种用途：

- :mod:`~harness_kit.models.adapters.base` —— 写**新**适配器时的起点。
  继承 :class:`~harness_kit.models.adapters.base.HarnessChatModelAdapter`，
  只实现 ``_call_api``；
- :mod:`~harness_kit.models.adapters.openai_compat` —— 接任意 OpenAI 兼容
  端点（vLLM / SGLang / 企业网关）时的**成品**；
- :mod:`~harness_kit.models.adapters.echo` —— 离线测试与教学用的确定性模型。

三者都是 ``ChatModelBase`` 子类，因此都能直接塞进 ``Agent(model=...)``，
不需要任何额外包装 —— 这正是「基于 AgentScope 做扩展」而不是「另起一套」的
意义所在。
"""

from harness_kit.models.adapters.base import (
    HarnessChatModelAdapter,
    HarnessChatModelParameters,
    HarnessCredential,
    UsageTotals,
)
from harness_kit.models.adapters.echo import (
    EchoChatModel,
    EchoParameters,
    build_echo_model,
)
from harness_kit.models.adapters.openai_compat import (
    OpenAICompatChatModel,
    OpenAICompatParameters,
    build_openai_compat_model,
)

__all__ = [
    "EchoChatModel",
    "EchoParameters",
    "HarnessChatModelAdapter",
    "HarnessChatModelParameters",
    "HarnessCredential",
    "OpenAICompatChatModel",
    "OpenAICompatParameters",
    "UsageTotals",
    "build_echo_model",
    "build_openai_compat_model",
]
