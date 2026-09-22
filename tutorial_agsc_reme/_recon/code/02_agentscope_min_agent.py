"""AgentScope 2.0.8 最小 Agent：DeepSeekChatModel + Agent + reply()。"""
import asyncio
import os
import sys
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

import agentscope  # noqa: E402
from agentscope.agent import Agent  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402

print("agentscope", agentscope.__version__, agentscope.__file__)


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,  # 最小样例先关流式，方便直接拿完整 Msg
        parameters=DeepSeekChatModel.Parameters(max_tokens=512),
    )
    agent = Agent(
        name="assistant",
        system_prompt="你是一个简洁的中文助手，每次只回答一句话。",
        model=model,
    )
    msg = UserMsg("user", "用一句中文解释什么是 Agent Harness。")
    reply = await agent.reply(msg)
    print("=== reply.role:", reply.role)
    print("=== reply.name:", reply.name)
    print("=== reply text:", reply.get_text_content())
    print("=== usage:", reply.usage)
    print("=== len(agent.state.context):", len(agent.state.context))


asyncio.run(main())
