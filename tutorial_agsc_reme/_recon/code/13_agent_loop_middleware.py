"""02_agentscope_agent_loop 侦察：中间件 7 个挂载点实测。

MiddlewareBase 的 7 个 hook：
onion 模式 -> on_reply / on_reasoning / on_check_permission / on_acting /
              on_model_call / on_compress_context
transformer 模式 -> on_system_prompt
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
from agentscope.message import Msg, UserMsg  # noqa: E402
from agentscope.middleware import MiddlewareBase  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402

LOG: list[str] = []


class TraceMiddleware(MiddlewareBase):
    """在 7 个挂载点上打日志的中间件。"""

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        LOG.append("on_reply: before")
        async for item in next_handler(**input_kwargs):
            yield item
        LOG.append("on_reply: after")

    async def on_reasoning(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        LOG.append(f"on_reasoning: before tool_choice={input_kwargs.get('tool_choice')}")
        async for item in next_handler(**input_kwargs):
            yield item
        LOG.append("on_reasoning: after")

    async def on_check_permission(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> Any:
        LOG.append(f"on_check_permission: {input_kwargs['tool_call'].name}")
        decision = await next_handler(**input_kwargs)
        LOG.append(f"on_check_permission: -> {decision.behavior}")
        return decision

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        LOG.append(f"on_acting: {input_kwargs['tool_call'].name}")
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> Any:
        LOG.append(
            f"on_model_call: model={input_kwargs['current_model'].model} "
            f"msgs={len(input_kwargs['messages'])} "
            f"tools={len(input_kwargs['tools'])}",
        )
        return await next_handler(**input_kwargs)

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        LOG.append("on_system_prompt: +<trace>")
        return current_prompt + "\n<trace>injected by middleware</trace>"

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> None:
        LOG.append("on_compress_context")
        await next_handler(**input_kwargs)


async def echo(text: str) -> str:
    """Echo back.

    Args:
        text (`str`): text.

    Returns:
        `str`: the same text.
    """
    return text


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=256),
    )
    agent = Agent(
        name="assistant",
        system_prompt="你是中文助手。必须调用 echo 工具回显用户输入。",
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(echo, is_read_only=True)]),
        middlewares=[TraceMiddleware()],
        react_config=ReActConfig(max_iters=3),
    )
    msg = await agent.reply(UserMsg("user", "把 'hello' 回显给我。"))
    print("reply text =", msg.get_text_content())
    print()
    print("中间件调用日志（按顺序）:")
    for i, line in enumerate(LOG, 1):
        print(f"  {i:02d}. {line}")
    print()
    print("中间件分流结果:")
    print("  _reply_middlewares            =", [type(m).__name__ for m in agent._reply_middlewares])
    print("  _reasoning_middlewares        =", [type(m).__name__ for m in agent._reasoning_middlewares])
    print("  _check_permission_middlewares =", [type(m).__name__ for m in agent._check_permission_middlewares])
    print("  _acting_middlewares           =", [type(m).__name__ for m in agent._acting_middlewares])
    print("  _model_call_middlewares       =", [type(m).__name__ for m in agent._model_call_middlewares])
    print("  _system_prompt_middlewares    =", [type(m).__name__ for m in agent._system_prompt_middlewares])
    print("  _compress_context_middlewares =", [type(m).__name__ for m in agent._compress_context_middlewares])


asyncio.run(main())
