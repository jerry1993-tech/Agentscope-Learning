"""07_agentscope_middleware_hook 侦察：ReplyBudgetControlMiddleware 实测。

验证点：
1. 预算按 reply 累计（agent.state.middle_context[middleware_key][reply_id]）。
2. 预算耗尽后：往 context 尾部插 HintBlock 并强制 tool_choice="none"，
   模型不再调工具而是直接收尾。
3. 状态存在 AgentState.middle_context 里，中间件实例本身无状态
   （state/_state.py:295）。

源码：
  middleware/_budget.py:98  on_reply  （ReplyStartEvent 初始化 / ModelCallEndEvent 累加 / ReplyEndEvent 清理）
  middleware/_budget.py:150 on_reasoning（超预算时插 hint + 改 tool_choice）
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
from agentscope.event import ModelCallEndEvent, ReplyEndEvent, ReplyStartEvent  # noqa: E402
from agentscope.message import HintBlock, UserMsg  # noqa: E402
from agentscope.middleware import (  # noqa: E402
    MiddlewareBase,
    ReplyBudgetControlMiddleware,
)
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402

TOOL_CALLS: list[str] = []


class SpyMiddleware(MiddlewareBase):
    """插在预算中间件外层，观察 reasoning 前后的 tool_choice 与事件。"""

    def __init__(self) -> None:
        self.tool_choices: list[str] = []
        self.model_calls = 0

    async def on_reasoning(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tc = input_kwargs.get("tool_choice")
        self.tool_choices.append(str(getattr(tc, "mode", tc)))
        async for evt in next_handler(**input_kwargs):
            if isinstance(evt, ModelCallEndEvent):
                self.model_calls += 1
            yield evt


async def read_file(path: str) -> str:
    """Read a file from disk and return its content.

    Args:
        path (`str`): absolute path of the file to read.

    Returns:
        `str`: the file content.
    """
    TOOL_CALLS.append("read_file")
    return f"<content of {path}>"


async def list_dir(path: str) -> str:
    """List directory entries.

    Args:
        path (`str`): absolute path of the directory.

    Returns:
        `str`: newline-joined entries.
    """
    TOOL_CALLS.append("list_dir")
    return "a.txt\nb.txt"


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

    def build(middlewares: list, name: str) -> Agent:
        return Agent(
            name=name,
            system_prompt=(
                "你是文件助手。必须先用工具查看情况，再回答用户。"
                "有 read_file 和 list_dir 可用。"
            ),
            model=model,
            toolkit=Toolkit(
                tools=[
                    FunctionTool(read_file, is_read_only=True),
                    FunctionTool(list_dir, is_read_only=True),
                ],
            ),
            middlewares=middlewares,
            react_config=ReActConfig(max_iters=5),
        )

    print("=" * 74)
    print("A. 基线：不加预算中间件，看模型是否会调工具")
    print("=" * 74)
    TOOL_CALLS.clear()
    plain = build([], "baseline")
    msg = await plain.reply(
        UserMsg("user", "请用工具查看 /tmp 目录，然后告诉我文件名。"),
    )
    print("reply =", msg.get_text_content()[:100].replace("\n", " "))
    print("工具调用次数 =", len(TOOL_CALLS), TOOL_CALLS)
    print()

    print("=" * 74)
    print("B. token_budget=50（第一轮就超），看是否强制 tool_choice=none")
    print("=" * 74)
    TOOL_CALLS.clear()
    spy = SpyMiddleware()
    budget = ReplyBudgetControlMiddleware(
        token_budget=50,
        input_token_weight=1.0,
        output_token_weight=1.0,
    )
    # 预算中间件在前（更外层）：它就地改写 input_kwargs["tool_choice"] 后
    # 才把控制权交给 Spy，Spy 因此能看到被改成 "none" 之后的值。
    agent = build([budget, spy], "budgeted")
    print("分流结果:")
    print("  _reply_middlewares   =", [type(m).__name__ for m in agent._reply_middlewares])
    print("  _reasoning_middlewares =", [type(m).__name__ for m in agent._reasoning_middlewares])
    print()

    msg = await agent.reply(
        UserMsg("user", "请用工具查看 /tmp 目录，然后告诉我文件名。"),
    )
    print("reply =", msg.get_text_content()[:120].replace("\n", " "))
    print("工具调用次数 =", len(TOOL_CALLS), TOOL_CALLS)
    print("Spy 观察到的 tool_choice 序列 =", spy.tool_choices)
    print("Spy 观察到的模型调用次数 =", spy.model_calls)
    hints = [
        b.hint
        for m in agent.state.context
        for b in m.content
        if isinstance(b, HintBlock)
    ]
    print("context 里的 HintBlock 数 =", len(hints))
    for h in hints:
        print("   hint =", h[:90].replace("\n", " "))
    print("  >>> 预算中间件把 hint 直接写进 agent.state.context，不经过事件流")
    print()

    print("=" * 74)
    print("C. middle_context 的 key 与结构（state/_state.py:295）")
    print("=" * 74)
    print("  middle_context keys =", list(agent.state.middle_context.keys()))
    for k, v in agent.state.middle_context.items():
        print(f"    {k} -> {v}")
    print("  >>> ReplyEndEvent 已把该 reply_id 的计数清空，所以是空 dict")
    print()

    print("=" * 74)
    print("D. 手工构造一次 reply 内的中间态，验证记分公式")
    print("=" * 74)
    mw = ReplyBudgetControlMiddleware(
        token_budget=1000,
        input_token_weight=1.0,
        output_token_weight=3.0,
    )
    key = await mw.get_middleware_key()
    print("  get_middleware_key() =", repr(key))
    cost = mw.input_token_weight * 100 + mw.output_token_weight * 30
    print("  in=100 out=30 weight=(1,3) -> cost =", cost)
    print("  >>> cost = input_token_weight*input_tokens"
          " + output_token_weight*output_tokens（middleware/_budget.py:143）")


asyncio.run(main())
