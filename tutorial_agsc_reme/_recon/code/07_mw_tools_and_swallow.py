"""07_agentscope_middleware_hook 侦察：middleware 提供 tool + on_reply 吞掉 ReplyEndEvent。

验证点：
1. MiddlewareBase.list_tools() 是中间件向 Agent 注入工具的唯一通道。
   RAGMiddleware（agentic 模式）返回 1 个工具；ReMeMiddleware 按 mode 返回
   memory_search；base 默认返回 []。
   Agent 侧收集点在 app/_service/_toolkit.py:231-233（服务化场景）
   —— 裸 Agent 不会自动收集，需要手工 Toolkit(tools=await mw.list_tools())。
2. on_reply 吞掉 ReplyEndEvent（收到但不 yield）会强制再走一轮
   reasoning-acting（agent/_agent.py:902-905, 954-960）。
"""
import asyncio
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.event import ReplyEndEvent, ReplyStartEvent  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.middleware import (  # noqa: E402
    MiddlewareBase,
    RAGMiddleware,
    ReplyBudgetControlMiddleware,
    TracingMiddleware,
    TTSMiddleware,
)
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import Toolkit  # noqa: E402


class SwallowOnceMiddleware(MiddlewareBase):
    """吞掉第一次 ReplyEndEvent，逼 Agent 多走一轮。"""

    def __init__(self) -> None:
        self.swallowed = 0
        self.rounds = 0

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async for evt in next_handler(**input_kwargs):
            if isinstance(evt, ReplyStartEvent):
                self.rounds += 1
            if isinstance(evt, ReplyEndEvent) and self.swallowed == 0:
                self.swallowed += 1
                print("   [swallow] 收到第 1 个 ReplyEndEvent，不 yield 它")
                continue  # 吞掉：不 yield -> agent 认为 reply 还没结束
            yield evt


async def main() -> None:
    print("=" * 74)
    print("A. 中间件 list_tools() 契约")
    print("=" * 74)

    class PlainMw(MiddlewareBase):
        pass

    print("  MiddlewareBase().list_tools()           =",
          await PlainMw().list_tools())

    rag = RAGMiddleware(knowledge_bases=[], parameters=None)
    rag_tools = await rag.list_tools()
    print("  RAGMiddleware(默认 agentic).list_tools() =",
          [type(t).__name__ + ":" + getattr(t, "name", "?") for t in rag_tools])

    rag_static = RAGMiddleware(
        knowledge_bases=[],
        parameters=RAGMiddleware.Parameters(mode="static"),
    )
    print("  RAGMiddleware(mode=static).list_tools()  =",
          await rag_static.list_tools())

    print("  RAGMiddleware.Parameters 字段 =",
          list(RAGMiddleware.Parameters.model_fields))

    for mw, label in (
        (PlainMw(), "MiddlewareBase"),
        (ReplyBudgetControlMiddleware(token_budget=1), "budget"),
        (TracingMiddleware(), "tracing"),
    ):
        print(f"  {label}.get_middleware_key() =",
              repr(await mw.get_middleware_key()))
    print()

    print("=" * 74)
    print("B. on_reply 吞 ReplyEndEvent -> 强制多走一轮")
    print("=" * 74)
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=128),
    )
    sw = SwallowOnceMiddleware()
    agent = Agent(
        name="assistant",
        system_prompt="你是中文助手，每次只回答一句话。",
        model=model,
        toolkit=Toolkit(tools=[]),
        middlewares=[sw],
        react_config=ReActConfig(max_iters=4),
    )
    msg = await agent.reply(UserMsg("user", "说一句问候语。"))
    print("  reply =", msg.get_text_content()[:80].replace("\n", " "))
    print("  吞掉的 ReplyEndEvent 数 =", sw.swallowed)
    print("  >>> agent 因拿不到 ReplyEndEvent 而再走一轮 reasoning-acting")
    print()


asyncio.run(main())
