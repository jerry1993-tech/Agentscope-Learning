# -*- coding: utf-8 -*-
"""代码助手 Demo：ReMe 索引 + AgentScope 运行时 + harness_kit 治理。

三个模块各管一段，可以单独跑：

=================== ==========================================================
模块                职责
=================== ==========================================================
:mod:`agent`        Profile → ``HarnessBuilder`` → 装好的 :class:`CodeAssistant`
:mod:`ingest_repo`  渲染代码文件 → 灌进 ReMe 工作区（写入侧）
:mod:`main`         端到端：索引 → 提问 → 核对引用（``python -m ...main``）
=================== ==========================================================

与其余子包一致，这里不做立即重导出：``agent`` 会拉进 ``agentscope``，
而只想知道"Demo 有几个文件"的调用方不该为此付一次 import 开销。
"""

from typing import TYPE_CHECKING, Any

__all__ = [
    "AGENT_NAME",
    "CodeAssistant",
    "build_code_assistant",
    "default_settings",
    "demo_profile",
    "ingest_repo",
    "reference_root",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AGENT_NAME": ("harness_kit.demo.code_assistant.agent", "AGENT_NAME"),
    "CodeAssistant": ("harness_kit.demo.code_assistant.agent", "CodeAssistant"),
    "build_code_assistant": (
        "harness_kit.demo.code_assistant.agent",
        "build_code_assistant",
    ),
    "default_settings": ("harness_kit.demo.code_assistant.agent", "default_settings"),
    "demo_profile": ("harness_kit.demo.code_assistant.agent", "demo_profile"),
    "reference_root": ("harness_kit.demo.code_assistant.agent", "reference_root"),
    "ingest_repo": ("harness_kit.demo.code_assistant.ingest_repo", "ingest_repo"),
}


def __getattr__(name: str) -> Any:
    """惰性导入（:pep:`562`）。

    Args:
        name (`str`): 属性名。

    Returns:
        `Any`: 目标对象。

    Raises:
        `AttributeError`: 名字不在导出表内。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}; available: {sorted(_LAZY_EXPORTS)}",
        )
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让 ``dir()`` 同时列出惰性导出项。

    Returns:
        `list[str]`: 排序后的公开名字。
    """
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from harness_kit.demo.code_assistant.agent import (
        AGENT_NAME,
        CodeAssistant,
        build_code_assistant,
        default_settings,
        demo_profile,
        reference_root,
    )
    from harness_kit.demo.code_assistant.ingest_repo import ingest_repo
