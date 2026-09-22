# -*- coding: utf-8 -*-
"""服务层（第 20 讲）：把装好的 Harness 暴露成一个 HTTP 服务。

对外只有两个东西值得记：

- :func:`harness_kit.service.app.create_harness_app` —— 按契约 §3.20 的签名
  返回一个配好的 ``FastAPI`` 实例（**不**自己监听端口）；
- ``harness_kit/webui/index.html`` —— 单文件调试 UI，由 ``GET /`` 送出。

**默认端口是 18420**（契约铁律：≥ 18000，见
:data:`harness_kit.service.app.DEFAULT_PORT`）。本仓库的学习/验证纪律是
"不要长期占用端口"，所以任何验证脚本都应当：起服务 → 打几个请求 → 关掉，
并且用 :mod:`harness_kit.cli` 的 ``serve`` 而不是手工 ``uvicorn``，
那样至少关闭路径是统一的。

与 ``harness_kit.observe`` / ``harness_kit.eval`` 一致，这里不做立即重导出：
``fastapi`` 不是每个讲次都需要，包一被 import 就拉进整套 Web 依赖没有必要。
"""

from typing import TYPE_CHECKING, Any

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ChatService",
    "ChatSession",
    "StoreEventBus",
    "create_harness_app",
    "describe_routes",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "DEFAULT_HOST": ("harness_kit.service.app", "DEFAULT_HOST"),
    "DEFAULT_PORT": ("harness_kit.service.app", "DEFAULT_PORT"),
    "ChatService": ("harness_kit.service.app", "ChatService"),
    "ChatSession": ("harness_kit.service.app", "ChatSession"),
    "StoreEventBus": ("harness_kit.service.app", "StoreEventBus"),
    "create_harness_app": ("harness_kit.service.app", "create_harness_app"),
    "describe_routes": ("harness_kit.service.app", "describe_routes"),
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
    from harness_kit.service.app import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        ChatService,
        ChatSession,
        StoreEventBus,
        create_harness_app,
        describe_routes,
    )
