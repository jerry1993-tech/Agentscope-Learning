"""02_agentscope_agent_loop 侦察：ThinkingBlock* 与 DataBlock* 事件怎么才会出现。

Part A: DeepSeek thinking 模式 -> ThinkingBlockStart/Delta/End
Part B: 工具返回 DataBlock -> ToolResultDataDeltaEvent（直接单测 _convert_tool_chunk_to_event）
"""
import asyncio
import base64
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import (  # noqa: E402
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    UserMsg,
)
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import Toolkit  # noqa: E402

# 1x1 透明 PNG
PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


async def part_a() -> None:
    print("=== Part A: thinking 模式 ===")
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=True,
        parameters=DeepSeekChatModel.Parameters(
            max_tokens=1024,
            thinking_enable=True,
            reasoning_effort="high",
        ),
    )
    agent = Agent(
        name="assistant",
        system_prompt="你是一个简洁的中文助手。",
        model=model,
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=2),
    )
    kinds = []
    async for item in agent.reply_stream(UserMsg("user", "3 的阶乘是多少？")):
        kinds.append(type(item).__name__)
    # 去重压缩连续重复
    compact = []
    for k in kinds:
        if not compact or compact[-1][0] != k:
            compact.append([k, 1])
        else:
            compact[-1][1] += 1
    print("  事件序列:", [(k, c) for k, c in compact])


async def part_b() -> None:
    print()
    print("=== Part B: 工具返回 DataBlock -> ToolResultDataDeltaEvent ===")
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
    )
    agent = Agent(
        name="assistant",
        system_prompt="test",
        model=model,
        toolkit=Toolkit(),
    )
    block = DataBlock(
        source=Base64Source(data=PNG_1PX, media_type="image/png"),
        name="pixel.png",
    )
    async for evt in agent._convert_tool_chunk_to_event("call-1", [block]):
        print(" ", type(evt).__name__,
              "media_type =", getattr(evt, "media_type", None),
              "data 长度 =", len(getattr(evt, "data", "") or ""))

    # 文本块走另一条分支
    async for evt in agent._convert_tool_chunk_to_event(
        "call-2", [TextBlock(text="hello")],
    ):
        print(" ", type(evt).__name__, "delta =", getattr(evt, "delta", None))

    # 纯字符串也走 text delta
    async for evt in agent._convert_tool_chunk_to_event("call-3", "plain"):
        print(" ", type(evt).__name__, "delta =", getattr(evt, "delta", None))


async def main() -> None:
    await part_a()
    await part_b()


asyncio.run(main())
