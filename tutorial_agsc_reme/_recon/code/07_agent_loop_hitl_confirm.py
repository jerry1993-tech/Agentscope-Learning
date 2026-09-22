"""02_agentscope_agent_loop 侦察：HITL 用户确认（RequireUserConfirmEvent）往返。

工具不设置 is_read_only 且不给 permission，PermissionEngine 在 DEFAULT 模式下
返回 ASK -> Agent 发 RequireUserConfirmEvent 并 park；调用方回传
UserConfirmResultEvent 后 Agent 继续执行。

跑法同上，使用 agentscope_reme_pip_env/bin/python。
"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.event import (  # noqa: E402
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def write_note(text: str) -> str:
    """Write a note into the notebook (a side-effecting tool).

    Args:
        text (`str`): the note content.

    Returns:
        `str`: confirmation message.
    """
    return f"note saved: {text}"


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,  # 关闭流式，事件更干净
        parameters=DeepSeekChatModel.Parameters(max_tokens=512),
    )
    agent = Agent(
        name="assistant",
        system_prompt="你是一个中文助手，需要写笔记时调用 write_note 工具。",
        model=model,
        # 注意：不传 is_read_only / permission，权限引擎默认 ASK
        toolkit=Toolkit(tools=[FunctionTool(write_note)]),
        react_config=ReActConfig(max_iters=4),
    )

    print("=" * 72)
    print("第一轮：期望 park 在 RequireUserConfirmEvent")
    print("=" * 72)
    pending: RequireUserConfirmEvent | None = None
    async for item in agent.reply_stream(
        UserMsg("user", "把「测试确认流程」这五个字记到笔记里。"),
    ):
        name = type(item).__name__
        if isinstance(item, Msg):
            print(f"  Msg  text={item.get_text_content()!r}")
        else:
            print(f"  {name}" + (
                f" tool_calls={[_.name for _ in item.tool_calls]}"
                if name == "RequireUserConfirmEvent"
                else ""
            ))
        if isinstance(item, RequireUserConfirmEvent):
            pending = item

    assert pending is not None, "没有收到 RequireUserConfirmEvent"
    tc = pending.tool_calls[0]
    print()
    print("park 的 tool_call:", tc.name, tc.input, "state=", tc.state)
    print("awaiting tool calls:", agent.state.get_awaiting_tool_calls("assistant"))

    print()
    print("=" * 72)
    print("第二轮：回传 UserConfirmResultEvent(confirmed=True) 继续执行")
    print("=" * 72)
    resume = UserConfirmResultEvent(
        reply_id=pending.reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=tc)],
    )
    async for item in agent.reply_stream(resume):
        name = type(item).__name__
        if isinstance(item, Msg):
            print(f"  Msg  text={item.get_text_content()!r}")
        elif name == "ToolResultEndEvent":
            print(f"  {name} state={item.state}")
        elif name == "ReplyEndEvent":
            print(f"  {name} reason={item.finished_reason}")
        else:
            print(f"  {name}")

    print()
    print("state.cur_iter =", agent.state.cur_iter)
    print("state.context[-1] blocks =",
          [type(b).__name__ for b in agent.state.context[-1].content])


asyncio.run(main())
