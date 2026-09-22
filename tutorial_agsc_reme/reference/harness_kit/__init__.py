# -*- coding: utf-8 -*-
"""harness_kit —— 建立在 AgentScope 2.0.8 与 ReMe 0.4.1.13 之上的企业级 Harness 装配层。

本包**不是**一个 Agent 内核。它只做三件事：

1. 装配：把 AgentScope 的 ``Agent`` / ``Toolkit`` / ``ChatModelBase`` / ``WorkspaceBase`` /
   ``PermissionEngine`` / ``MiddlewareBase`` 按声明式的 ``Profile`` / ``Bundle`` 组装起来；
2. 补齐：补上这两个库在真实生产场景里确实缺失的部分（不可变事件日志、事件总线、
   评测引擎、声明式 Profile、沙箱策略与配额等）；
3. 服务化：把装配结果暴露为 CLI / FastAPI / SSE / MCP Server。

包入口刻意保持**零重依赖**：``import harness_kit`` 只加载标准库。
重依赖（agentscope / reme / pydantic / loguru）通过 :pep:`562` 的模块级 ``__getattr__``
在首次访问对应名字时才导入，这样 ``scripts/00_smoke.py`` 的第一步（校验 ``reme`` 版本、
校验 ``sys.path``）不会被 AgentScope 的导入开销污染。
"""

from typing import TYPE_CHECKING, Any

__version__: str = "0.1.0"
"""harness_kit 的版本号。"""


def get_version() -> str:
    """返回 harness_kit 的版本号。

    Returns:
        `str`: 形如 ``"0.1.0"`` 的语义化版本字符串。
    """
    return __version__


# 对外仅导出这几个名字；其余子模块（config / events / session / memory / ...）
# 一律通过完整路径导入，例如 ``from harness_kit.config import load_profile``。
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "Settings": ("harness_kit.settings", "Settings"),
    "HarnessBuilder": ("harness_kit.config.builder", "HarnessBuilder"),
    "BuiltHarness": ("harness_kit.config.builder", "BuiltHarness"),
    "HarnessRegistry": ("harness_kit.registry", "HarnessRegistry"),
    "load_profile": ("harness_kit.config.loader", "load_profile"),
    "load_resolved_profile": (
        "harness_kit.config.loader",
        "load_resolved_profile",
    ),
    "resolve_profile": ("harness_kit.config.schema", "resolve_profile"),
    "Profile": ("harness_kit.config.schema", "Profile"),
    "Bundle": ("harness_kit.config.schema", "Bundle"),
    "ResolvedProfile": ("harness_kit.config.schema", "ResolvedProfile"),
    "EventRecord": ("harness_kit.events.types", "EventRecord"),
    "EventKind": ("harness_kit.events.types", "EventKind"),
}

__all__ = ["__version__", "get_version", *sorted(_LAZY_EXPORTS)]


def __getattr__(name: str) -> Any:
    """惰性导入对外导出的名字（:pep:`562`）。

    Args:
        name (`str`): 属性名，必须是 :data:`_LAZY_EXPORTS` 的键。

    Returns:
        `Any`: 目标对象。

    Raises:
        `AttributeError`: 名字不在导出表内。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}; "
            f"available: {sorted(_LAZY_EXPORTS)}",
        )

    module_path, attr = target
    # 局部导入避免在模块顶层引入 pydantic / agentscope
    from importlib import import_module

    module = import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让 ``dir(harness_kit)`` 同时列出惰性导出项。

    Returns:
        `list[str]`: 排序后的公开名字。
    """
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from harness_kit.config.builder import BuiltHarness, HarnessBuilder
    from harness_kit.config.loader import load_profile, load_resolved_profile
    from harness_kit.config.schema import (
        Bundle,
        Profile,
        ResolvedProfile,
        resolve_profile,
    )
    from harness_kit.events.types import EventKind, EventRecord
    from harness_kit.registry import HarnessRegistry
    from harness_kit.settings import Settings
