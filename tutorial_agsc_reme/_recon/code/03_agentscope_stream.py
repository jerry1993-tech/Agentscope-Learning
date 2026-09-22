"""AgentScope 2.0.8 流式 reply：reply_stream() 拿到增量事件。"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import Msg, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=True,
        parameters=DeepSeekChatModel.Parameters(max_tokens=512),
    )
    agent = Agent(name="assistant", system_prompt="只回答一句话。", model=model)
    print("--- 事件类型序列 ---")
    seen: list[str] = []
    async for item in agent.reply_stream(UserMsg("user", "什么是 ReAct？")):
        if isinstance(item, Msg):
            seen.append(f"Msg(role={item.role})")
        else:
            seen.append(type(item).__name__)
    for s in seen:
        print("  ", s)


asyncio.run(main())
