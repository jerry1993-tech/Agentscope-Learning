# -*- coding: utf-8 -*-
"""06 侦察验证脚本 A：
1) 自定义同步工具 + 异步工具 -> 自动生成 JSON schema
2) 用 Toolkit 真实调用它们（走 call_tool，产出 ToolChunk 流 + ToolResponse）
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_tool_schema_and_call.py
"""
import asyncio
import json
from typing import Annotated, AsyncGenerator, Literal

from pydantic import BaseModel, Field

from agentscope.state import AgentState
from agentscope.message import ToolCallBlock
from agentscope.tool import FunctionTool, ToolResponse, Toolkit


# ---------------------------------------------------------------- 工具 1
def add_numbers(
    a: int,
    b: int,
    mode: Literal["sum", "product"] = "sum",
) -> str:
    """Add or multiply two integers.

    A tiny synchronous tool used to demonstrate automatic schema
    extraction from the function signature + docstring.

    Args:
        a (int): The first operand.
        b (int): The second operand.
        mode (Literal["sum", "product"]): Which operation to apply.
    """
    if mode == "sum":
        return f"{a} + {b} = {a + b}"
    return f"{a} * {b} = {a * b}"


# ---------------------------------------------------------------- 工具 2
class SearchParams(BaseModel):
    """The parameters of the async search tool."""

    query: str = Field(description="The keywords to search for.")
    top_k: Annotated[int, Field(ge=1, le=10, description="How many hits.")] = 3
    tags: list[str] = Field(default_factory=list, description="Tags filter.")


async def fake_search(
    query: str,
    top_k: int = 3,
    tags: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """Search the (fake) knowledge base and stream the hits.

    An async-generator tool: every ``yield`` becomes one
    :class:`~agentscope.tool.ToolChunk`, which the Toolkit accumulates
    into a single :class:`~agentscope.tool.ToolResponse`.

    Args:
        query (str): The keywords to search for.
        top_k (int): How many hits to return.
        tags (list[str] | None): Optional tag filter.
    """
    tags = tags or []
    for i in range(min(top_k, 3)):
        yield f"[hit {i + 1}] {query} (tags={tags})\n"


async def main() -> None:
    sync_tool = FunctionTool(add_numbers)
    async_tool = FunctionTool(fake_search, input_schema=SearchParams)

    print("=" * 70)
    print("[1] sync tool schema (auto from signature + docstring)")
    print(json.dumps(sync_tool.input_schema, ensure_ascii=False, indent=2))
    print("description =", repr(sync_tool.description))

    print("=" * 70)
    print("[2] async tool schema (from a Pydantic model)")
    print(json.dumps(async_tool.input_schema, ensure_ascii=False, indent=2))

    toolkit = Toolkit(tools=[sync_tool, async_tool])
    state = AgentState()

    print("=" * 70)
    print("[3] schemas the LLM would see:")
    for schema in await toolkit.get_tool_schemas():
        fn = schema["function"]
        print(f"  - {fn['name']}: {fn['description'].splitlines()[0]}")
        print(f"    required = {fn['parameters'].get('required')}")

    print("=" * 70)
    print("[4] call the SYNC tool via toolkit.call_tool")
    tc = ToolCallBlock(
        id="call_1",
        name="add_numbers",
        input=json.dumps({"a": 3, "b": 4, "mode": "sum"}),
    )
    async for chunk in toolkit.call_tool(tc, state):
        if isinstance(chunk, ToolResponse):
            print("   FINAL ToolResponse:",
                  [b.text for b in chunk.content], chunk.state)
        else:
            print("   chunk:", [b.text for b in chunk.content], chunk.state)

    print("=" * 70)
    print("[5] call the ASYNC-GENERATOR tool via toolkit.call_tool")
    tc2 = ToolCallBlock(
        id="call_2",
        name="fake_search",
        input=json.dumps({"query": "harness", "top_k": 2, "tags": ["a"]}),
    )
    async for chunk in toolkit.call_tool(tc2, state):
        if isinstance(chunk, ToolResponse):
            print("   FINAL ToolResponse:",
                  [b.text for b in chunk.content], chunk.state)
        else:
            print("   chunk:", [b.text for b in chunk.content], chunk.state)

    print("=" * 70)
    print("[6] argument repair: model sends WRONG TYPES / broken JSON")
    tc3 = ToolCallBlock(
        id="call_3",
        name="add_numbers",
        input='{"a": "3", "b": "4", "mode": "product"}',
    )
    async for chunk in toolkit.call_tool(tc3, state):
        if isinstance(chunk, ToolResponse):
            print("   FINAL:", [b.text for b in chunk.content], chunk.state)

    tc4 = ToolCallBlock(id="call_4", name="nonexistent_tool", input="{}")
    async for chunk in toolkit.call_tool(tc4, state):
        if isinstance(chunk, ToolResponse):
            print("   not-found FINAL:", [b.text for b in chunk.content],
                  chunk.state)


if __name__ == "__main__":
    asyncio.run(main())
