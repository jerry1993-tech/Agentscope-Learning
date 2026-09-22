"""02_agentscope_agent_loop 侦察：死循环防护 _get_repeated_tool_error + observe()。

工具永远抛异常，模型会反复用同样的参数重试；连续 3 次同样的失败
（injection_config.tool_retries_limit 默认 3）后，Agent 注入 tool-error hint。
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

calls = {"n": 0}


async def broken_lookup(key: str) -> str:
    """Look up a record by key. This tool is broken on purpose.

    Args:
        key (`str`): the record key.

    Returns:
        `str`: never returns, always raises.
    """
    calls["n"] += 1
    raise RuntimeError("backend unavailable: connection refused")


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
        system_prompt=(
            "你必须用 broken_lookup 工具去查 record 的 key 为 'k1'。"
            "严格规则：只要工具返回错误，你必须再次调用 broken_lookup，"
            "参数必须完全一样（key='k1'），不许改写参数、不许换工具、"
            "不许给用户任何解释，直到工具成功为止。"
        ),
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(broken_lookup, is_read_only=True)]),
        react_config=ReActConfig(max_iters=8),
    )

    print("=" * 72)
    print("observe() 先塞一条外部观察消息")
    print("=" * 72)
    await agent.observe(UserMsg("user", "系统提示：上游服务刚刚重启过。"))
    print("context 长度 =", len(agent.state.context),
          "roles =", [m.role for m in agent.state.context])

    print()
    print("=" * 72)
    print("reply_stream：观察 tool-error hint 是否被注入")
    print("=" * 72)
    hint_seen = 0
    async for item in agent.reply_stream(
        UserMsg("user", "帮我查一下 key 为 k1 的记录。"),
    ):
        name = type(item).__name__
        if isinstance(item, Msg):
            print(f"Msg text={item.get_text_content()!r}")
        elif name == "HintBlockEvent":
            hint_seen += 1
            text = item.hint if isinstance(item.hint, str) else str(item.hint)
            if "tool-error" in text:
                print(f"HINT(tool-error) = {text!r}")
            else:
                print(f"HINT(other) = {text[:80]!r}")
        elif name == "ReplyEndEvent":
            print(f"{name} reason={item.finished_reason}")
        else:
            print(name)

    print()
    print("工具被调用次数 =", calls["n"])
    print("state.cur_iter =", agent.state.cur_iter)
    print("检测函数直接返回 =", agent._get_repeated_tool_error())


asyncio.run(main())
