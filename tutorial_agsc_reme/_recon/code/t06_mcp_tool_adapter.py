# -*- coding: utf-8 -*-
"""06 侦察验证脚本 G：MCPTool 适配器（用内存内 MCP server，不需要起进程/网络）
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_mcp_tool_adapter.py
"""
import asyncio
import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionContext
from agentscope.state import AgentState
from agentscope.tool import MCPTool, ToolChunk, Toolkit

logging.disable(logging.INFO)

server = FastMCP("demo-server")


@server.tool()
def echo(text: str) -> str:
    """Echo the given text back.

    Args:
        text: the text to echo.
    """
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers.

    Args:
        a: first operand.
        b: second operand.
    """
    return a + b


async def main() -> None:
    async with create_connected_server_and_client_session(
        server._mcp_server,
    ) as session:
        await session.initialize()

        raw_tools = (await session.list_tools()).tools
        print("=" * 70)
        print("[1] raw MCP tools announced by the server")
        for t in raw_tools:
            print("   ", t.name, "|", (t.description or "").splitlines()[0])

        tools = [
            MCPTool(mcp_name="demo", tool=t, session=session)
            for t in raw_tools
        ]

        print("=" * 70)
        print("[2] MCPTool rewrites the name as mcp__<server>__<tool>")
        for t in tools:
            print(f"   {t.name:20s} is_mcp={t.is_mcp} "
                  f"is_read_only={t.is_read_only}")

        print("=" * 70)
        print("[3] the schema is passed through untouched ($defs preserved)")
        print(json.dumps(tools[0].input_schema, indent=2))

        print("=" * 70)
        print("[4] direct call -> ToolChunk")
        chunk = await tools[0].call(text="hello mcp")
        print("   ", chunk.content[0].text, "/", chunk.state)

        print("=" * 70)
        print("[5] through the Toolkit, using the mangled name")
        toolkit = Toolkit(tools=tools)
        state = AgentState()
        tc = ToolCallBlock(
            id="m1",
            name="mcp__demo__add",
            input=json.dumps({"a": 2, "b": 40}),
        )
        async for c in toolkit.call_tool(tc, state):
            if isinstance(c, ToolChunk):
                print("   ", c.content[0].text, "/", c.state)

        print("=" * 70)
        print("[6] MCP tools can never be state-injected")
        tc2 = ToolCallBlock(
            id="m2",
            name="mcp__demo__echo",
            input=json.dumps({"text": "state?"}),
        )
        async for c in toolkit.call_tool(tc2, state):
            if isinstance(c, ToolChunk):
                print("   ", c.content[0].text, "/", c.state)
        print("   is_state_injected =", tools[0].is_state_injected)

        print("=" * 70)
        print("[7] default permission for an MCP tool is ASK")
        d = await tools[0].check_permissions({}, PermissionContext())
        print("   ", d.behavior, "-", d.message)


if __name__ == "__main__":
    asyncio.run(main())
