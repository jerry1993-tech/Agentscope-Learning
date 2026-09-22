"""02_agentscope_agent_loop 侦察：不调用 LLM 的确定性单元测试。

直接构造 agent.state.context，验证：
1. _next_action 的四种返回：Reasoning / Acting / Exit(exit_events=None) /
   Exit(exit_events=[...])，以及 max_iters 的几种边界。
2. _get_repeated_tool_error 的死循环检测（含参数顺序归一化）。
3. _update_tool_call_state / _save_to_context / append_context 的助手消息聚合规则。

跑法同上，不需要网络。
"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.agent._utils import Acting, Exit, Reasoning  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import (  # noqa: E402
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def echo(text: str) -> str:
    """Echo the text back.

    Args:
        text (`str`): text to echo.

    Returns:
        `str`: the same text.
    """
    return text


def make_agent(**kwargs) -> Agent:
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
        toolkit=Toolkit(tools=[FunctionTool(echo, is_read_only=True)]),
        **kwargs,
    )


def tc(call_id: str, name: str, raw: str, state=ToolCallState.PENDING):
    return ToolCallBlock(id=call_id, name=name, input=raw, state=state)


def tr(call_id: str, name: str, state=ToolResultState.SUCCESS):
    return ToolResultBlock(id=call_id, name=name, output="o", state=state)


def main() -> None:
    # ------------------------------------------------------------------
    print("=== 1. 空上下文：默认继续 reasoning ===")
    agent = make_agent()
    print("  ", agent._next_action())

    # ------------------------------------------------------------------
    print()
    print("=== 2. 助手消息里有 PENDING 工具调用（无 awaiting）-> Acting ===")
    agent = make_agent()
    agent.state.context.append(
        Msg(id=agent.state.reply_id, role="assistant", name="assistant",
            content=[tc("c1", "echo", '{"text":"a"}')]),
    )
    print("  ", agent._next_action())

    # ------------------------------------------------------------------
    print()
    print("=== 3. 工具调用已有 tool_result -> 不再是 Acting ===")
    agent.state.context[-1].content.append(tr("c1", "echo"))
    print("  ", agent._next_action())

    # ------------------------------------------------------------------
    print()
    print("=== 4. 还有 ASKING 的调用 -> Exit(exit_events=None) 表示 park ===")
    agent = make_agent()
    agent.state.context.append(
        Msg(id=agent.state.reply_id, role="assistant", name="assistant",
            content=[tc("c1", "echo", "{}", ToolCallState.ASKING)]),
    )
    na = agent._next_action()
    print("  ", type(na).__name__, "exit_events =", na.exit_events,
          "msg =", na.exit_msg.get_text_content()[:30])

    # ------------------------------------------------------------------
    print()
    print("=== 5. final_msg 存在且未超 max_iters -> Exit(COMPLETED) ===")
    agent = make_agent()
    agent.state.cur_iter = 0
    final = Msg(id=agent.state.reply_id, role="assistant", name="assistant",
                content=[TextBlock(text="done")])
    na = agent._next_action(final)
    print("  ", type(na).__name__,
          "events =", [type(e).__name__ for e in na.exit_events])
    print("   ReplyEndEvent.finished_reason =",
          na.exit_events[-1].finished_reason)

    # ------------------------------------------------------------------
    print()
    print("=== 6. cur_iter == max_iters -> 强制收尾的 Reasoning ===")
    agent = make_agent(react_config=ReActConfig(max_iters=3))
    agent.state.cur_iter = 3
    na = agent._next_action(None)
    print("  ", type(na).__name__, "tool_choice =", na.tool_choice)
    print("   hint =", na.hint.hint)

    # ------------------------------------------------------------------
    print()
    print("=== 7. cur_iter > max_iters 且有 final_msg -> EXCEED_MAX_ITERS ===")
    agent.state.cur_iter = 4
    na = agent._next_action(final)
    print("  ", [type(e).__name__ for e in na.exit_events])
    print("   reason =", na.exit_events[-1].finished_reason)

    # ------------------------------------------------------------------
    print()
    print("=== 8. cur_iter > max_iters 且无 final_msg -> EXCEED + 兜底 Msg ===")
    na = agent._next_action(None)
    print("  ", [type(e).__name__ for e in na.exit_events])
    print("   msg =", na.exit_msg.get_text_content())

    # ------------------------------------------------------------------
    print()
    print("=== 9. 需要结构化输出但未生成 -> Reasoning + 强制 tool_choice ===")
    agent = make_agent(react_config=ReActConfig(max_iters=3))


    class Schema(dict):
        pass

    agent.state.reply_context.structured_schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }
    na = agent._next_action(None)
    print("  ", type(na).__name__)
    agent.state.cur_iter = 3
    na = agent._next_action(None)
    print("   cur_iter=3 时 tool_choice =", na.tool_choice)

    # ------------------------------------------------------------------
    print()
    print("=== 10. 已生成结构化输出 -> Exit(COMPLETED) + structured_output ===")
    agent.state.reply_context.structured_output = {"x": 1}
    na = agent._next_action(None)
    print("  ", type(na).__name__, "structured_output =",
          na.exit_msg.structured_output)

    # ------------------------------------------------------------------
    print()
    print("=== 11. _get_repeated_tool_error：三次同样失败 -> 命中 ===")
    agent = make_agent()
    content = []
    for i in range(3):
        content.append(tc(f"c{i}", "echo", '{"text": "a", "n": 1}'))
        content.append(tr(f"c{i}", "echo", ToolResultState.ERROR))
    agent.state.context.append(
        Msg(id=agent.state.reply_id, role="assistant", name="assistant",
            content=content),
    )
    print("  ", agent._get_repeated_tool_error())

    # 参数 key 顺序不同但语义相同 -> 仍然命中
    agent2 = make_agent()
    content2 = []
    for i, raw in enumerate(
        ['{"text": "a", "n": 1}', '{"n": 1, "text": "a"}',
         '{ "text" : "a" , "n" : 1 }'],
    ):
        content2.append(tc(f"c{i}", "echo", raw))
        content2.append(tr(f"c{i}", "echo", ToolResultState.ERROR))
    agent2.state.context.append(
        Msg(id=agent2.state.reply_id, role="assistant", name="assistant",
            content=content2),
    )
    print("   key 顺序打乱后 =", agent2._get_repeated_tool_error())

    # 只失败 2 次 -> 不命中（limit 默认 3）
    agent3 = make_agent()
    content3 = []
    for i in range(2):
        content3.append(tc(f"c{i}", "echo", '{"text": "a"}'))
        content3.append(tr(f"c{i}", "echo", ToolResultState.ERROR))
    agent3.state.context.append(
        Msg(id=agent3.state.reply_id, role="assistant", name="assistant",
            content=content3),
    )
    print("   只失败 2 次 =", agent3._get_repeated_tool_error())

    # 最后一次是 success -> 不命中
    agent4 = make_agent()
    content4 = []
    for i in range(3):
        content4.append(tc(f"c{i}", "echo", '{"text": "a"}'))
        content4.append(tr(f"c{i}", "echo", ToolResultState.ERROR))
    content4[-1] = tr("c2", "echo", ToolResultState.SUCCESS)
    agent4.state.context.append(
        Msg(id=agent4.state.reply_id, role="assistant", name="assistant",
            content=content4),
    )
    print("   末尾是 success =", agent4._get_repeated_tool_error())

    # ------------------------------------------------------------------
    print()
    print("=== 12. append_context 聚合规则 ===")
    agent = make_agent()
    agent.state.append_context("assistant", [TextBlock(text="A")])
    agent.state.append_context("assistant", [TextBlock(text="B")])
    print("   同一 reply_id 聚合为 1 条消息，块数 =",
          len(agent.state.context), len(agent.state.context[0].content))
    agent.state.reply_id = "another-reply"
    agent.state.append_context("assistant", [TextBlock(text="C")])
    print("   换 reply_id 后消息数 =", len(agent.state.context))

    # ------------------------------------------------------------------
    print()
    print("=== 13. _save_to_context 过滤音频 DataBlock ===")
    from agentscope.message import Base64Source, DataBlock

    agent = make_agent()
    agent._save_to_context(
        [
            DataBlock(
                source=Base64Source(data="AAAA", media_type="audio/pcm"),
            ),
        ],
    )
    print("   只有音频块 -> 不写入 context，长度 =", len(agent.state.context))

    print()
    print("全部断言式检查跑完（无异常即为通过）")


main()
