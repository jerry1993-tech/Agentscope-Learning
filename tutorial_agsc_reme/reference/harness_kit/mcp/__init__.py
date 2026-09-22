# -*- coding: utf-8 -*-
"""harness_kit 的 MCP 层（契约 §3.7，第 7 讲）。

三个模块，三条不同的路：

- :mod:`harness_kit.mcp.registry` —— **声明 → 连接**：把 Profile 里的
  ``MCPServerSpec`` 翻译成 AgentScope 的 ``MCPClient`` 并连上；
- :mod:`harness_kit.mcp.adapter` —— **连上 → 可用**：把远端工具收进
  ``Toolkit`` 的 ``mcp`` 工具组，并把重名/命名空间讲清楚；
- :mod:`harness_kit.mcp.server` —— **反向**：把 harness_kit 的工具暴露成
  MCP Server（AgentScope 只有客户端，这一侧是契约 §1.3 列的真实缺口）。

``MCPClient`` 的三种 transport 全部基于 AgentScope 原生实现，
harness_kit 不重写任何一种：

- ``stdio`` → ``StdioMCPConfig``（``third_party/agentscope/src/agentscope/mcp/_config.py:9``），
  必须 ``is_stateful=True``；
- ``sse`` → ``HttpMCPConfig`` + url 路径以 ``/sse`` 结尾
  （``.../mcp/_mcp_client.py:223-229`` 的 ``_is_sse`` 判定）；
- ``streamable_http`` → ``HttpMCPConfig`` + 非 ``/sse`` 路径。
"""

from typing import TYPE_CHECKING

from harness_kit.mcp.adapter import (
    ToolNameConflictError,
    collect_tools,
    inject_into_toolkit,
    list_remote_tools,
    to_tool_base,
)
from harness_kit.mcp.registry import (
    MCPServerRegistry,
    MCPServerSpec,
    TRANSPORTS,
    build_mcp_clients,
    close_mcp_clients,
    connect_mcp_clients,
    namespace_of,
    namespaced_tool_name,
    sanitize_tool_name,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    pass

__all__ = [
    "TRANSPORTS",
    "MCPServerRegistry",
    "MCPServerSpec",
    "ToolNameConflictError",
    "build_mcp_clients",
    "build_mcp_server",
    "close_mcp_clients",
    "collect_tools",
    "connect_mcp_clients",
    "inject_into_toolkit",
    "list_remote_tools",
    "namespace_of",
    "namespaced_tool_name",
    "sanitize_tool_name",
    "to_tool_base",
]


def __getattr__(name: str) -> object:
    """惰性导出 :func:`~harness_kit.mcp.server.build_mcp_server`。

    ``mcp.server`` 会 ``import mcp.server.fastmcp``，而后者在 import 期就要求
    SDK 的 server 依赖齐全。纯客户端场景（只连别人的 MCP server）不该被这套
    依赖拖累，所以 server 侧只在真的用 ``build_mcp_server`` 时导入。

    Args:
        name (`str`): 属性名。

    Returns:
        `object`: 目标对象。

    Raises:
        AttributeError: 名字不在本模块的导出表里。
    """
    if name == "build_mcp_server":
        from harness_kit.mcp.server import build_mcp_server

        globals()[name] = build_mcp_server
        return build_mcp_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
