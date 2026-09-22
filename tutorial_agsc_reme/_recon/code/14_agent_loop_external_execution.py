"""02_agentscope_agent_loop 侦察：外部执行（RequireExternalExecutionEvent）往返。

把工具的 is_external_tool 置 True，Agent 不会自己执行它，而是发
RequireExternalExecutionEvent 并 park；调用方执行完把结果通过
ExternalExecutionResultEvent 回传，Agent 写入 context 继续 ReAct。
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
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
)
from agentscope.message import (  # noqa: E402
    Msg,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def call_remote_api(endpoint: str) -> str:
    """Call a remote API that only the host environment can reach.

    Args:
        endpoint (`str`): the API endpoint.

    Returns:
        `str`: never executed in-process.
    """
    raise AssertionError("should never run in-process")


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
    tool = FunctionTool(call_remote_api, is_read_only=True)
    tool.is_external_tool = True  # 关键：标记为外部执行
    agent = Agent(
        name="assistant",
        system_prompt="需要访问远端时必须调用 call_remote_api。",
        model=model,
        toolkit=Toolkit(tools=[tool]),
        react_config=ReActConfig(max_iters=4),
    )

    print("=== 第一轮：park 在 RequireExternalExecutionEvent ===")
    pending = None
    async for item in agent.reply_stream(
        UserMsg("user", "调用远端 API 的 /v1/ping 端点。"),
    ):
        print("  ", type(item).__name__)
        if isinstance(item, RequireExternalExecutionEvent):
            pending = item
    assert pending is not None
    tc = pending.tool_calls[0]
    print("  tool_call:", tc.name, tc.input, "state =", tc.state)

    print()
    print("=== 第二轮：外部执行完，回传结果 ===")
    async for item in agent.reply_stream(
        ExternalExecutionResultEvent(
            reply_id=pending.reply_id,
            execution_results=[
                ToolResultBlock(
                    id=tc.id,
                    name=tc.name,
                    output="pong: 200 OK",
                    state=ToolResultState.SUCCESS,
                ),
            ],
        ),
    ):
        name = type(item).__name__
        if isinstance(item, Msg):
            print(f"   Msg text={item.get_text_content()!r}")
        elif name == "ToolResultEndEvent":
            print(f"   {name} state={item.state}")
        elif name == "ReplyEndEvent":
            print(f"   {name} reason={item.finished_reason}")
        else:
            print("  ", name)

    print()
    print("最终 context 块 =",
          [type(b).__name__ for b in agent.state.context[-1].content])


asyncio.run(main())
