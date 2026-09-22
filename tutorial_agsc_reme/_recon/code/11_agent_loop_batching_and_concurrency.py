"""02_agentscope_agent_loop 侦察：_batch_tool_calls 分批规则 + 并发执行结果顺序。

不调用 LLM：
- _batch_tool_calls 只看 tool.is_concurrency_safe / 是否注册
- _execute_concurrent_tool_calls 用 asyncio.Queue + sentinel 收敛，
  所以事件顺序由各工具完成的先后决定，而不是按 tool_calls 列表顺序。
"""
import asyncio
import os
import time
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import ToolCallBlock, ToolCallState  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def slow_read(name: str, seconds: float = 0.3) -> str:
    """A read-only (concurrency safe) tool that sleeps.

    Args:
        name (`str`): label.
        seconds (`float`): how long to sleep.

    Returns:
        `str`: the label.
    """
    await asyncio.sleep(seconds)
    return f"read:{name}"


async def write_file(path: str, content: str) -> str:
    """A side-effecting (NOT concurrency safe) tool.

    Args:
        path (`str`): target path.
        content (`str`): content.

    Returns:
        `str`: confirmation.
    """
    await asyncio.sleep(0.05)
    return f"write:{path}={content}"


def make_agent() -> Agent:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
    )
    return Agent(
        name="assistant",
        system_prompt="test",
        model=model,
        toolkit=Toolkit(
            tools=[
                # is_read_only=True -> PermissionEngine read-only 快速通道 ALLOW
                FunctionTool(slow_read, is_read_only=True,
                             is_concurrency_safe=True),
                # 非 read-only 且非 concurrency safe
                FunctionTool(
                    write_file,
                    is_read_only=True,          # 只为跳过权限 ASK
                    is_concurrency_safe=False,
                ),
            ],
        ),
    )


def tc(call_id: str, name: str, raw: str) -> ToolCallBlock:
    return ToolCallBlock(
        id=call_id, name=name, input=raw, state=ToolCallState.PENDING,
    )


async def main() -> None:
    agent = make_agent()

    print("=== 1. 分批规则 ===")
    calls = [
        tc("c1", "slow_read", '{"name": "a"}'),
        tc("c2", "slow_read", '{"name": "b"}'),
        tc("c3", "write_file", '{"path": "/tmp/x", "content": "1"}'),
        tc("c4", "write_file", '{"path": "/tmp/y", "content": "2"}'),
        tc("c5", "unknown_tool", "{}"),   # 未注册 -> 当并发工具
        tc("c6", "slow_read", '{"name": "c"}'),
    ]
    batches = await agent._batch_tool_calls(calls)
    for i, b in enumerate(batches):
        print(f"  batch[{i}] type={b.type} "
              f"calls={[_.name for _ in b.tool_calls]}")

    print()
    print("=== 2. 并发执行：事件完成顺序由工具耗时决定 ===")
    agent = make_agent()
    # a 睡 0.4s，b 睡 0.05s -> b 的结果先出
    calls = [
        tc("c1", "slow_read", '{"name": "a-slow", "seconds": 0.4}'),
        tc("c2", "slow_read", '{"name": "b-fast", "seconds": 0.05}'),
    ]
    # 让 tool_call 进入上下文，_update_tool_call_state 才能找到它
    from agentscope.message import Msg

    agent.state.context.append(
        Msg(id=agent.state.reply_id, role="assistant", name="assistant",
            content=list(calls)),
    )
    order = []
    t0 = time.perf_counter()
    async for evt in agent._execute_concurrent_tool_calls(calls):
        n = type(evt).__name__
        if n == "ToolResultTextDeltaEvent":
            order.append((evt.tool_call_id, evt.delta))
        if n == "ToolResultEndEvent":
            order.append((evt.tool_call_id, f"<end {evt.state}>"))
    print("  事件顺序:", order)
    print(f"  耗时 {time.perf_counter() - t0:.3f}s（并发应远小于 0.45s）")

    print()
    print("=== 3. 顺序执行：严格按列表顺序 ===")
    agent = make_agent()
    calls = [
        tc("c1", "write_file", '{"path": "/tmp/x", "content": "1"}'),
        tc("c2", "write_file", '{"path": "/tmp/y", "content": "2"}'),
    ]
    agent.state.context.append(
        Msg(id=agent.state.reply_id, role="assistant", name="assistant",
            content=list(calls)),
    )
    order = []
    async for evt in agent._execute_sequential_tool_calls(calls):
        n = type(evt).__name__
        if n == "ToolResultTextDeltaEvent":
            order.append((evt.tool_call_id, evt.delta))
    print("  事件顺序:", order)

    print()
    print("=== 4. 未注册工具 -> tool not found，走 _handle_error_tool_call ===")
    agent = make_agent()
    calls = [tc("c9", "unknown_tool", "{}")]
    agent.state.context.append(
        Msg(id=agent.state.reply_id, role="assistant", name="assistant",
            content=list(calls)),
    )
    async for evt in agent._execute_sequential_tool_calls(calls):
        n = type(evt).__name__
        if n == "ToolResultTextDeltaEvent":
            print("  delta =", evt.delta[:120])
        elif n == "ToolResultEndEvent":
            print("  state =", evt.state)


asyncio.run(main())
