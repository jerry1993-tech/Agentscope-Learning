"""02_agentscope_agent_loop 侦察：max_iters 耗尽路径（ExceedMaxItersEvent）。

max_iters=1 且任务本来需要两次工具调用：
- 第 1 轮 Reasoning -> Acting(工具) 之后 cur_iter 变成 1
- 第 2 轮 _next_action 走到 "cur_iter == max_iters" 分支，
  注入 "Summarize the work" hint 并强制 tool_choice="none"
- 模型吐出纯文本 final_msg，下一轮 _next_action 走
  "cur_iter > max_iters" 分支 -> 发 ExceedMaxItersEvent + ReplyEndEvent(exceed_max_iters)
"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import Msg, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def get_secret_number() -> str:
    """Get the secret number.

    Returns:
        `str`: the secret number.
    """
    return "7"


async def multiply_by_two(number: int) -> str:
    """Multiply the given integer by two.

    Args:
        number (`int`): the integer to multiply.

    Returns:
        `str`: the doubled value.
    """
    return str(number * 2)


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=512),
    )
    agent = Agent(
        name="assistant",
        system_prompt="你需要用工具完成任务，不要凭空编造数字。",
        model=model,
        toolkit=Toolkit(
            tools=[
                FunctionTool(get_secret_number, is_read_only=True),
                FunctionTool(multiply_by_two, is_read_only=True),
            ],
        ),
        react_config=ReActConfig(max_iters=1),  # 故意设成 1
    )

    async for item in agent.reply_stream(
        UserMsg(
            "user",
            "先调用 get_secret_number，再调用 multiply_by_two 翻倍，最后告诉我结果。",
        ),
    ):
        name = type(item).__name__
        if isinstance(item, Msg):
            print(f"Msg text={item.get_text_content()!r} "
                  f"reason={item.finished_reason}")
        elif name == "HintBlockEvent":
            print(f"{name} hint={item.hint!r}")
        elif name == "ReplyEndEvent":
            print(f"{name} reason={item.finished_reason}")
        elif name == "ExceedMaxItersEvent":
            print(f"{name} (deprecated, 仅为向后兼容)")
        else:
            print(name)

    print()
    print("state.cur_iter =", agent.state.cur_iter,
          "(max_iters =", agent.react_config.max_iters, ")")


asyncio.run(main())
