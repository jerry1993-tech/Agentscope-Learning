# -*- coding: utf-8 -*-
"""06 侦察验证脚本 F：外部工具 (AskUser) / 两段式参数修复 / jsonschema 校验
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_external_and_validation.py
"""
import asyncio
import json
import logging

import jsonschema

from agentscope._utils._common import _json_loads_with_repair
from agentscope.exception import ToolJSONDecodeError
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionContext
from agentscope.state import AgentState
from agentscope.tool import (
    AskUser,
    FunctionTool,
    ParamsBase,
    ToolChunk,
    Toolkit,
)

logging.disable(logging.INFO)


class P(ParamsBase):
    count: int


def need_int(count: int) -> str:
    """Record an int.

    Args:
        count (int): The number to record.
    """
    return f"got {count!r} ({type(count).__name__})"


async def main() -> None:
    toolkit = Toolkit(tools=[FunctionTool(need_int)])
    state = AgentState()

    print("=" * 70)
    print("[1] STAGE 1: schema-guided repair inside Toolkit.call_tool")
    for raw in ['{"count": "42"}', "{count: '42',", '{"count": "many"}']:
        try:
            fixed = _json_loads_with_repair(raw, need_int_schema :=
                                            toolkit.tool_groups[0]
                                            .tools[0].input_schema)
            print(f"   {raw!r:22s} -> {fixed}")
        except ToolJSONDecodeError as e:
            print(f"   {raw!r:22s} -> ToolJSONDecodeError")

    print("=" * 70)
    print("[2] Toolkit.call_tool ALONE does not validate: the tool still runs")
    tc = ToolCallBlock(id="c1", name="need_int",
                       input='{"count": "many"}')
    async for c in toolkit.call_tool(tc, state):
        if isinstance(c, ToolChunk):
            print("   ->", c.content[0].text, "/", c.state)

    print("=" * 70)
    print("[3] STAGE 2: jsonschema.validate, done by the Agent loop")
    schema = toolkit.tool_groups[0].tools[0].input_schema
    for raw in ['{"count": "many"}', '{"count": 42}', '{}']:
        parsed = _json_loads_with_repair(raw, schema)
        try:
            jsonschema.validate(parsed, schema)
            print(f"   {raw!r:22s} OK")
        except jsonschema.ValidationError as e:
            print(f"   {raw!r:22s} -> Input validation failed for tool "
                  f"'need_int': {e.message} (at {e.json_path})")

    print("=" * 70)
    print("[4] a broken JSON string is turned into a ToolJSONDecodeError")
    tc2 = ToolCallBlock(id="c2", name="need_int", input="not json at all")
    async for c in toolkit.call_tool(tc2, state):
        if isinstance(c, ToolChunk):
            print("   ->", c.content[0].text[:120].replace("\n", " "),
                  "/", c.state)

    print("=" * 70)
    print("[5] external tools: AskUser never executes locally")
    ask = AskUser()
    print("   is_external_tool =", ask.is_external_tool)
    print("   is_state_injected =", ask.is_state_injected)
    print("   metadata_schema keys =",
          sorted(ask.metadata_schema.get("properties", {})))
    try:
        await ask.call(questions=[{"question": "Q?", "options": [
            {"label": "A"}, {"label": "B"}]}])
    except RuntimeError as e:
        print("   AskUser.call() raises RuntimeError:", e)

    print("=" * 70)
    print("[6] check_external_result validates the reply metadata")
    from agentscope.message import ToolResultBlock, ToolResultState
    good = ToolResultBlock(
        id="x", name="AskUser",
        output="ok",
        state=ToolResultState.SUCCESS,
        metadata={"answers": [{"question": "Q?", "selected": ["A"],
                               "other": None}]},
    )
    await ask.check_external_result(good)
    print("   well-formed metadata -> accepted")
    bad = ToolResultBlock(id="x", name="AskUser", output="ok",
                          state=ToolResultState.SUCCESS, metadata={})
    try:
        await ask.check_external_result(bad)
    except jsonschema.ValidationError as e:
        print("   missing 'answers' ->", e.message)

    print("=" * 70)
    print("[7] the AskUser schema the model sees")
    print(json.dumps(AskUser().input_schema, ensure_ascii=False)[:400])

    print("=" * 70)
    print("[8] custom FunctionTool defaults to ASK")
    d = await FunctionTool(need_int).check_permissions({},
                                                       PermissionContext())
    print("   ", d.behavior, "-", d.message)
    ask_d = await ask.check_permissions({}, PermissionContext())
    print("    AskUser ->", ask_d.behavior, "-", ask_d.message)


if __name__ == "__main__":
    asyncio.run(main())
