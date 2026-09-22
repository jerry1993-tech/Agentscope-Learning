"""02_agentscope_agent_loop 侦察：UserInterruptEvent 中断 parked 的 reply。

第一轮 park 在 RequireUserConfirmEvent；第二轮不确认、直接回传
UserInterruptEvent -> Agent 走 _close_unfinished_tool_calls 收尾，
发 ReplyEndEvent(INTERRUPTED) + 兜底 AssistantMsg，不进入 ReAct 循环。
"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.event import RequireUserConfirmEvent, UserInterruptEvent  # noqa: E402
from agentscope.message import Msg, UserMsg, ToolResultState  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def delete_record(key: str) -> str:
    """Delete a record (side effect, requires confirmation).

    Args:
        key (`str`): the record key.

    Returns:
        `str`: confirmation.
    """
    return f"deleted {key}"


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
        system_prompt="需要删记录时调用 delete_record 工具。",
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(delete_record)]),
        react_config=ReActConfig(max_iters=4),
    )

    print("=== 第一轮：park ===")
    pending = None
    async for item in agent.reply_stream(
        UserMsg("user", "删除 key 为 k1 的记录。"),
    ):
        if isinstance(item, RequireUserConfirmEvent):
            pending = item
        print("  ", type(item).__name__)
    assert pending is not None
    print("  got RequireUserConfirmEvent, reply_id =", pending.reply_id)

    print()
    print("=== 第二轮：不确认，直接中断 ===")
    async for item in agent.reply_stream(
        UserInterruptEvent(reply_id=pending.reply_id),
    ):
        name = type(item).__name__
        if isinstance(item, Msg):
            print(f"   Msg text={item.get_text_content()!r} "
                  f"reason={item.finished_reason}")
        elif name == "ToolResultEndEvent":
            print(f"   {name} state={item.state}")
        elif name == "ToolResultTextDeltaEvent":
            print(f"   {name} delta={item.delta!r}")
        elif name == "ReplyEndEvent":
            print(f"   {name} reason={item.finished_reason}")
        else:
            print("  ", name)

    print()
    print("=== 最终 context 的 tool_result 状态 ===")
    for b in agent.state.context[-1].content:
        if type(b).__name__ == "ToolResultBlock":
            print("   ", b.id, b.state,
                  "(INTERRUPTED =", ToolResultState.INTERRUPTED, ")")


asyncio.run(main())
