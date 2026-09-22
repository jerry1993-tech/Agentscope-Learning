# -*- coding: utf-8 -*-
"""06 侦察验证脚本 B：
1) ToolGroup + 内置元工具 reset_tools（LLM 自助装卸工具组）
2) SKILL.md 渐进式加载（LocalSkillLoader -> SkillViewer -> get_skill_instructions）
   附：scan_subdir 默认 False 的坑
3) ToolMiddleware 洋葱模型
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_tool_group_and_skill.py
"""
import asyncio
import json
import logging
import os
import tempfile

from agentscope.message import ToolCallBlock
from agentscope.skill import LocalSkillLoader
from agentscope.state import AgentState
from agentscope.tool import (
    FunctionTool,
    ToolChunk,
    ToolGroup,
    ToolMiddlewareBase,
    Toolkit,
)

logging.disable(logging.INFO)

SKILL_MD = """---
name: pdf-report
description: Generate a PDF report from the current data.
---

# PDF report skill

Step 1: read the data with the Read tool.
Step 2: run `python gen.py` inside this skill directory.
"""


def web_search(query: str) -> str:
    """Search the web.

    Args:
        query (str): The query string.
    """
    return f"fake web result for {query}"


def run_sql(sql: str) -> str:
    """Run a SQL statement.

    Args:
        sql (str): The SQL to execute.
    """
    return f"fake sql result for {sql}"


class LoggingMiddleware(ToolMiddlewareBase):
    """Print the tool inputs before and after execution."""

    async def on_tool_call(self, tool, input_kwargs, next_handler):
        print(f"   [mw] before {tool.name} {input_kwargs}")
        async for chunk in next_handler(**input_kwargs):
            yield chunk
        print(f"   [mw] after  {tool.name}")


def write_skill(root: str, name: str = "pdf-report") -> str:
    """Write a SKILL.md into ``root/<name>/SKILL.md`` and return that dir."""
    skill_dir = os.path.join(root, name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w",
              encoding="utf-8") as f:
        f.write(SKILL_MD)
    return skill_dir


async def main() -> None:
    parent = tempfile.mkdtemp(prefix="as_skills_parent_")
    sub = write_skill(parent, "pdf-report")

    # ---------------------------------------------------------- 工具组
    sql_tool = FunctionTool(run_sql, middlewares=[LoggingMiddleware()])
    toolkit = Toolkit(
        tools=[FunctionTool(web_search)],
        tool_groups=[
            ToolGroup(
                name="sql",
                description="SQL database access.",
                instructions="Always run SELECT before UPDATE.",
                tools=[sql_tool],
            ),
            ToolGroup(
                name="skillgroup",
                description="Skills for specialised tasks.",
                # 只扫一层目录（默认 scan_subdir=False）：必须给 SKILL.md
                # 所在的目录本身
                skills_or_loaders=[sub],
            ),
            ToolGroup(
                name="skillgroup_scan",
                description="Skills found by scanning sub-directories.",
                skills_or_loaders=[
                    LocalSkillLoader(directory=parent, scan_subdir=True),
                ],
            ),
        ],
    )

    print("=" * 70)
    print("[1] default: only the 'basic' group is active")
    for s in await toolkit.get_tool_schemas():
        print("  ", s["function"]["name"])

    print("=" * 70)
    print("[2] the meta tool's schema is generated from the group list:")
    for s in await toolkit.get_tool_schemas(["sql", "skillgroup"]):
        if s["function"]["name"] == "reset_tools":
            print(json.dumps(s["function"]["parameters"], indent=2))

    print("=" * 70)
    print("[3] calling an inactive tool -> ToolGroupInactiveError hint")
    state = AgentState()
    tc = ToolCallBlock(id="c1", name="run_sql",
                       input=json.dumps({"sql": "select 1"}))
    async for chunk in toolkit.call_tool(tc, state):
        if isinstance(chunk, ToolChunk):
            print("  ", chunk.content[0].text, "/", chunk.state)

    print("=" * 70)
    print("[4] agent calls reset_tools to activate 'sql'")
    tc2 = ToolCallBlock(id="c2", name="reset_tools",
                        input=json.dumps({"sql": True, "skillgroup": False}))
    async for chunk in toolkit.call_tool(tc2, state):
        if isinstance(chunk, ToolChunk):
            print("   state.tool_context.activated_groups =",
                  state.tool_context.activated_groups)
            print("   result:", chunk.content[0].text[:120].replace("\n", " "))

    print("=" * 70)
    print("[5] now run_sql works, and the middleware wraps it")
    async for chunk in toolkit.call_tool(tc, state):
        if isinstance(chunk, ToolChunk):
            print("  ", chunk.content[0].text, "/", chunk.state)

    print("=" * 70)
    print("[6] SkillViewer becomes visible only when a skill is active")
    tc3 = ToolCallBlock(id="c3", name="reset_tools",
                        input=json.dumps({"sql": False, "skillgroup": True}))
    async for chunk in toolkit.call_tool(tc3, state):
        if isinstance(chunk, ToolChunk):
            print("   activated =", state.tool_context.activated_groups)
    for s in await toolkit.get_tool_schemas(state.tool_context.activated_groups):
        print("   visible tool:", s["function"]["name"])

    print("=" * 70)
    print("[7] skill instructions injected into the system prompt")
    print(await toolkit.get_skill_instructions(
        state.tool_context.activated_groups))

    print("=" * 70)
    print("[8] SkillViewer reads the SKILL.md body on demand")
    tc4 = ToolCallBlock(id="c4", name="Skill",
                        input=json.dumps({"skill": "pdf-report"}))
    async for chunk in toolkit.call_tool(tc4, state):
        if isinstance(chunk, ToolChunk):
            print("  ", chunk.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
