# -*- coding: utf-8 -*-
"""06 侦察验证脚本 E：真实 LLM 端到端工具调用
  LLM 返回 tool_call -> Agent._execute_tool_call -> Toolkit.call_tool
  -> FunctionTool -> ToolResponse -> ToolResultBlock 回填上下文

需要 .env 里的 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL（deepseek-flash）。
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_llm_tool_call_e2e.py
"""
import asyncio
import os
import sys
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

import agentscope  # noqa: E402
from agentscope.agent import Agent  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import ToolResultBlock, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.permission import (  # noqa: E402
    PermissionBehavior,
    PermissionDecision,
)
from agentscope.state import AgentState  # noqa: E402
from agentscope.tool import FunctionTool, ToolChunk, Toolkit  # noqa: E402

CALLS = []

# 关键：自定义 FunctionTool 的默认权限是 ASK（要人工确认）。
# 无人值守脚本里必须显式给 ALLOW，否则 Agent 会停在
# "I'm waiting for your permission..."。
ALLOW = PermissionDecision(
    behavior=PermissionBehavior.ALLOW,
    message="Demo: auto-allow.",
)


def get_weather(city: str) -> str:
    """Look up the current weather of a city.

    Args:
        city (str): The English name of the city, e.g. "Beijing".
    """
    CALLS.append(city)
    return f"{city}: 24C, clear, humidity 40%"


async def get_time(city: str) -> str:
    """Look up the local time of a city.

    Args:
        city (str): The English name of the city.
    """
    CALLS.append(city)
    return f"{city}: 2026-09-21 17:00 (CST)"


async def main() -> None:
    print("agentscope", agentscope.__version__)

    toolkit = Toolkit(
        tools=[
            FunctionTool(get_weather, permission=ALLOW),
            FunctionTool(get_time, permission=ALLOW),
        ],
    )

    # 证实 Toolkit 交给模型的 schema 长什么样
    for s in await toolkit.get_tool_schemas():
        print("  schema:", s["function"]["name"], s["function"]["parameters"])

    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=1024),
    )

    # Toolkit 在 Agent 上被自动转成 tools= 传给模型
    agent = Agent(
        name="assistant",
        system_prompt=(
            "你是一个助手。需要天气或时间时必须调用工具，"
            "不要编造数据。全部信息拿到后用一句中文总结。"
        ),
        model=model,
        toolkit=toolkit,
    )

    reply = await agent.reply(
        UserMsg("user", "北京现在的天气和时间分别是什么？"),
    )

    print("=" * 70)
    print("tool actually executed with:", CALLS)
    print("=" * 70)
    print("reply text:", reply.get_text_content())

    print("=" * 70)
    print("full context written back by the agent loop:")
    for msg in agent.state.context:
        for block in msg.get_content_blocks():
            if isinstance(block, ToolResultBlock):
                print(f"  ToolResultBlock(name={block.name!r}, "
                      f"state={block.state!r}) -> {block.output!r}")


if __name__ == "__main__":
    asyncio.run(main())
