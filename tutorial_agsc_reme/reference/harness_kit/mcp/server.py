# -*- coding: utf-8 -*-
"""反向：把 harness_kit 的工具暴露成 MCP Server（契约 §3.7，第 7 讲）。

AgentScope 只有 MCP **客户端**（``third_party/agentscope/src/agentscope/mcp/_mcp_client.py``），
没有 server 侧 —— 这正是契约 §1.3 列的 6 个真实缺口之一
（``tutorial_agsc_reme/_contract.md:65``）。本模块补的就是它。

**铁律：必须 ``from mcp.server.fastmcp import FastMCP``。**

环境里同时装了独立包 ``fastmcp 4.0.5`` 与官方 SDK ``mcp 1.30.0``，两者不兼容：:

    ImportError: FastMCP server support is not installed.
    Install `fastmcp` or `fastmcp-slim[server]`.
    # 根因：fastmcp/server/server.py:33 -> No module named 'mcp.server.request_state'

``mcp.server.fastmcp.FastMCP`` 是 SDK 内置的那一份，签名
``run(transport: Literal['stdio','sse','streamable-http'] = 'stdio')``，
本环境的 1.30.0 上可直接用。详见
``tutorial_agsc_reme/_recon/10_agentscope_mcp_rag_skill.md:687-694``。

**为什么不用 ``@server.tool()`` 装饰器注册工具？**

``FastMCP`` 的 ``Tool.from_function`` 是用 ``inspect.signature`` + 类型注解
**反推** JSON schema（``mcp/server/fastmcp/tools/base.py`` 的 ``func_metadata``）。
而 harness_kit / AgentScope 的工具已经带着一份权威的 ``input_schema``
（``ToolBase.input_schema``，由 docstring 解析或 MCP ``inputSchema`` 透传而来），
反推会丢掉 ``$defs`` 与 ``anyOf``。所以这里走 SDK 的低层处理器
``Server.list_tools()`` / ``Server.call_tool()``（``mcp.server.lowlevel.server``），
把 ``input_schema`` **原样透传**。

代价：低层处理器是"一个请求类型一个 handler"，注册会**替换**掉 ``FastMCP``
构造函数里自己注册的那一个。因此 :func:`build_mcp_server` 是"全有或全无"的：
传了 ``tools`` 就不要再用 ``@server.tool()`` 装饰器注册别的工具。
"""

from __future__ import annotations

import inspect
from typing import Any, Sequence

import mcp.types as types
from loguru import logger
from mcp.server.fastmcp import FastMCP

from agentscope.message import ToolResultState
from agentscope.tool import ToolBase, ToolChunk

from harness_kit.mcp.registry import namespace_of, sanitize_tool_name

__all__ = [
    "DEFAULT_PORT",
    "build_mcp_server",
    "call_tool_once",
    "exposed_tool_names",
    "tool_chunk_to_text",
]

DEFAULT_PORT: int = 18100
"""默认端口。契约 §3.7 要求 ≥ 18000，避开常用端口区间。"""


def tool_chunk_to_text(chunk: ToolChunk) -> str:
    """把 ``ToolChunk`` 的 content 拼成纯文本。

    MCP 的 ``CallToolResult.content`` 支持 ``TextContent`` / ``ImageContent`` /
    ``EmbeddedResource`` 三类；``ToolChunk`` 的 content 是
    ``TextBlock | DataBlock``（``third_party/agentscope/src/agentscope/tool/_response.py:28``）。
    这里只做文本拼接；``DataBlock``（图片/音频）降级成一句占位说明 ——
    它需要 base64 重编码成 ``ImageContent``，属于后续增强。

    Args:
        chunk (`ToolChunk`): 工具调用的增量结果。

    Returns:
        `str`: 拼接后的文本。
    """
    parts: list[str] = []
    for block in chunk.content:
        if block.type == "text":
            parts.append(block.text)
        else:
            parts.append(f"<{block.type} block omitted>")
    return "\n".join(parts)


async def call_tool_once(tool: ToolBase, arguments: dict[str, Any]) -> ToolChunk:
    """调用一个 ``ToolBase`` 并归一成单个 ``ToolChunk``。

    ``ToolBase.__call__`` 有两种形态：非流式工具返回 ``ToolChunk``，
    流式工具返回 ``AsyncGenerator[ToolChunk, None]``
    （``third_party/agentscope/src/agentscope/tool/_base.py:236-250``）。
    这里把后者累积成"最后一块"，MCP 侧只需要一个结果。

    Args:
        tool (`ToolBase`): 目标工具。
        arguments (`dict[str, Any]`): 关键字参数。

    Returns:
        `ToolChunk`: 合并后的结果。

    Raises:
        TypeError: 参数与 ``input_schema`` 不匹配（由工具自身的校验抛出）。
    """
    result = await tool(**arguments)
    if inspect.isasyncgen(result):
        last: ToolChunk | None = None
        async for chunk in result:
            last = chunk
        if last is None:
            return ToolChunk(content=[], state=ToolResultState.RUNNING)
        return last
    return result


def _build_handlers(
    fastmcp: FastMCP,
    tools: list[ToolBase],
    *,
    namespace: str | None,
) -> dict[str, ToolBase]:
    """在 ``FastMCP`` 的低层 server 上注册 list/call 两个处理器。

    Args:
        fastmcp (`FastMCP`): 已构造的 FastMCP 实例。
        tools (`list[ToolBase]`): 待暴露的工具。
        namespace (`str | None`): 非空时给每个工具名加 ``{namespace}__`` 前缀。

    Returns:
        `dict[str, ToolBase]`: 暴露名 → 工具对象。

    Raises:
        ValueError: 工具重名。
    """
    exposed: dict[str, ToolBase] = {}
    for tool in tools:
        name = tool.name
        if namespace:
            name = f"{namespace_of(namespace)}__{sanitize_tool_name(name)}"
        if name in exposed:
            raise ValueError(
                f"暴露的工具名重复: {name!r}；请用 namespace 前缀区分",
            )
        exposed[name] = tool

    lowlevel = fastmcp._mcp_server  # noqa: SLF001 - 见模块 docstring 的取舍说明

    @lowlevel.list_tools()
    async def _list_tools() -> list[types.Tool]:
        """告诉客户端有哪些工具。``inputSchema`` 原样透传。

        额外把 ``ToolBase.is_read_only`` 映射成 MCP 的 ``annotations.readOnlyHint``
        —— 客户端侧 ``MCPTool`` 正是靠这个字段判定 ``is_read_only``
        （``third_party/agentscope/src/agentscope/tool/_adapters.py:271-274``），
        而只读工具的默认权限是 ``ALLOW`` 而不是 ``ASK``（同文件 ``:307-310``）。
        不传这个字段，只读工具在客户端会被当成需要人工确认的危险操作。

        Returns:
            `list[types.Tool]`: MCP 工具描述列表。
        """
        out: list[types.Tool] = []
        for name, tool in exposed.items():
            annotations = (
                types.ToolAnnotations(readOnlyHint=True)
                if tool.is_read_only
                else None
            )
            out.append(
                types.Tool(
                    name=name,
                    description=tool.description,
                    inputSchema=tool.input_schema,
                    annotations=annotations,
                ),
            )
        return out

    @lowlevel.call_tool()
    async def _call_tool(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        """执行一次工具调用，把 ``ToolChunk`` 转成 MCP 结果。

        Args:
            tool_name (`str`): 客户端请求的工具名。
            arguments (`dict[str, Any]`): 参数。

        Returns:
            `types.CallToolResult`: MCP 结果；工具报错时 ``isError=True``。
        """
        tool = exposed.get(tool_name)
        if tool is None:
            logger.warning("客户端请求了未暴露的工具: {}", tool_name)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"Unknown tool {tool_name!r}; "
                        f"available: {sorted(exposed)}",
                    ),
                ],
                isError=True,
            )
        try:
            chunk = await call_tool_once(tool, dict(arguments))
        except Exception as exc:  # noqa: BLE001 - 必须转成协议层错误而不是崩连接
            logger.exception("工具 {} 执行失败", tool_name)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"{type(exc).__name__}: {exc}",
                    ),
                ],
                isError=True,
            )

        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=tool_chunk_to_text(chunk)),
            ],
            isError=chunk.state == ToolResultState.ERROR,
        )

    return exposed


def build_mcp_server(
    name: str,
    *,
    tools: list[ToolBase],
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    version: str = "1.0.0",
    instructions: str | None = None,
    namespace: str | None = None,
) -> Any:
    """把 harness_kit 的工具暴露成 MCP Server（契约 §3.7）。

    Args:
        name (`str`): server 名，写进 MCP 握手信息。
        tools (`list[ToolBase]`): 待暴露的工具；它们的 ``input_schema``
            会原样出现在 ``tools/list`` 的响应里。
        host (`str`): HTTP/SSE 传输的监听地址（stdio 传输忽略）。
        port (`int`): HTTP/SSE 传输的端口，默认 18100（契约要求 ≥ 18000）。
        version (`str`): server 版本，写进握手信息。
        instructions (`str | None`): 可选的 server 级使用说明。
        namespace (`str | None`): 非空时给每个工具名加 ``{namespace}__`` 前缀，
            避免与客户端的本地工具撞名。

    Returns:
        `Any`: ``mcp.server.fastmcp.FastMCP`` 实例。调用方决定怎么跑：
        ``server.run("stdio")``（给 AgentScope 的 ``StdioMCPConfig`` 用）或
        ``server.run("streamable-http")``（给 ``HttpMCPConfig`` 用）。

    Raises:
        ValueError: ``tools`` 为空，或工具名重复。

    Example:
        >>> from agentscope.tool import FunctionTool              # doctest: +SKIP
        >>> server = build_mcp_server("harness", tools=[FunctionTool(my_fn)])
        >>> server.run("stdio")                                   # doctest: +SKIP
    """
    if not tools:
        raise ValueError(
            "build_mcp_server 至少要暴露一个工具；空列表会让客户端看到一个"
            "没有任何能力的 server",
        )
    if port < 18000:
        raise ValueError(
            f"端口 {port} 违反契约 §3.7 的端口规则（必须 ≥ 18000），"
            "以免与开发机上的常用服务冲突",
        )

    fastmcp = FastMCP(
        name,
        instructions=instructions,
        host=host,
        port=port,
    )
    # FastMCP 的构造函数没有 version 参数，版本号在低层 Server 上
    # （mcp/server/lowlevel/server.py 的 Server.__init__ 第二个位置参数）。
    # 握手信息读的就是这个字段，所以这里直接写它。
    fastmcp._mcp_server.version = version  # noqa: SLF001

    exposed = _build_handlers(fastmcp, tools, namespace=namespace)
    # 记在实例上，供 exposed_tool_names() 与验证脚本 introspection 用。
    # **两个都要记**：``exposed`` 的 key 才是真正在 ``tools/list`` 里
    # 发给客户端的名字（带 namespace 前缀），value 是源工具对象。
    # 只记 value 的话，``build_mcp_server(namespace="verify")`` 暴露出去的
    # ``verify__calc`` 会被诊断函数报成 ``calc``。
    fastmcp._harness_exposed_tools = list(exposed.values())  # noqa: SLF001
    fastmcp._harness_exposed_names = list(exposed)  # noqa: SLF001
    logger.bind(
        server=name,
        tools=sorted(exposed),
        host=host,
        port=port,
    ).info("MCP server 已装配：{} 个工具", len(exposed))
    return fastmcp


def exposed_tool_names(server: Any) -> list[str]:
    """读出 :func:`build_mcp_server` 装配出的**对线名字**列表（诊断用）。

    返回的是 ``tools/list`` 真正发给客户端的名字：传了 ``namespace`` 时
    形如 ``verify__calc``，否则就是 ``tool.name``。

    Args:
        server (`Any`): ``build_mcp_server`` 的返回值。

    Returns:
        `list[str]`: 暴露的工具名；不是本模块造出来的 server 时返回空列表。
    """
    names: Sequence[str] | None = getattr(server, "_harness_exposed_names", None)
    if names is not None:
        return list(names)
    tools: Sequence[Any] = getattr(server, "_harness_exposed_tools", [])
    return [getattr(_, "name", str(_)) for _ in tools]
