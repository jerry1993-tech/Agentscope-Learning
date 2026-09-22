"""07_agentscope_middleware_hook 侦察：中间件链的嵌套顺序实测。

验证点：
1. 多个中间件的执行顺序 = middlewares 列表顺序，第一个是「最外层」。
2. 只实现部分 hook 的中间件只出现在对应的链里（is_implemented 过滤）。
3. 每次 hook 触发时该链被重新走一遍，各条链互相独立。

结论来自 agent/_agent.py 的 execute_chain(index) 递归实现：
    index=0 的中间件包住 index=1，index=1 包住 index=2 ... 链尾是 _xxx_impl。
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
from agentscope.message import UserMsg  # noqa: E402
from agentscope.middleware import MiddlewareBase  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import Toolkit  # noqa: E402

LOG: list[str] = []
DEPTH = {"n": 0}


def _mark(name: str, phase: str, hook: str) -> None:
    LOG.append(f"{'  ' * DEPTH['n']}[{name}] {hook}:{phase}")


def make_middleware(name: str, hooks: set[str]) -> MiddlewareBase:
    """动态生成一个只实现 `hooks` 里那几个 hook 的中间件实例。

    这样能真实演示 agent/_agent.py:218-240 的 is_implemented() 过滤：
    未实现的方法仍是 MiddlewareBase 上的定义，因此不会进入对应的链。
    """
    namespace: dict[str, Any] = {"__init__": lambda self: None}

    if "on_reply" in hooks:

        async def on_reply(self, agent, input_kwargs, next_handler):
            _mark(name, "enter", "on_reply")
            DEPTH["n"] += 1
            try:
                async for item in next_handler(**input_kwargs):
                    yield item
            finally:
                DEPTH["n"] -= 1
                _mark(name, "exit ", "on_reply")

        namespace["on_reply"] = on_reply

    if "on_reasoning" in hooks:

        async def on_reasoning(self, agent, input_kwargs, next_handler):
            _mark(name, "enter", "on_reasoning")
            DEPTH["n"] += 1
            try:
                async for item in next_handler(**input_kwargs):
                    yield item
            finally:
                DEPTH["n"] -= 1
                _mark(name, "exit ", "on_reasoning")

        namespace["on_reasoning"] = on_reasoning

    if "on_model_call" in hooks:

        async def on_model_call(self, agent, input_kwargs, next_handler):
            _mark(name, "enter", "on_model_call")
            DEPTH["n"] += 1
            try:
                return await next_handler(**input_kwargs)
            finally:
                DEPTH["n"] -= 1
                _mark(name, "exit ", "on_model_call")

        namespace["on_model_call"] = on_model_call

    cls = type(f"MW_{name}", (MiddlewareBase,), namespace)
    return cls()


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=128),
    )

    # A 只挂 on_model_call；B 挂全部三个
    a = make_middleware("A", {"on_model_call"})
    b = make_middleware("B", {"on_reply", "on_reasoning", "on_model_call"})

    agent = Agent(
        name="assistant",
        system_prompt="你是中文助手，回答一句话即可。",
        model=model,
        toolkit=Toolkit(tools=[]),
        middlewares=[a, b],
        react_config=ReActConfig(max_iters=2),
    )

    print("分流结果（middlewares=[A, B]，A 只实现 on_model_call）:")
    for attr in (
        "_reply_middlewares",
        "_reasoning_middlewares",
        "_check_permission_middlewares",
        "_acting_middlewares",
        "_model_call_middlewares",
        "_system_prompt_middlewares",
        "_compress_context_middlewares",
    ):
        print(f"  {attr:32s} = {[type(m).__name__ for m in getattr(agent, attr)]}")
    print()

    msg = await agent.reply(UserMsg("user", "说一句问候语。"))
    print("reply =", msg.get_text_content()[:50])
    print()
    print("调用顺序日志（缩进 = 嵌套深度）:")
    for i, line in enumerate(LOG, 1):
        print(f"  {i:02d}. {line}")
    print()
    print("结论：")
    print("  - on_reply / on_reasoning 链只有 B（A 没实现这两个 hook）")
    print("  - on_model_call 链是 [A, B]：A 先 enter、B 后 enter，exit 反序")
    print("  - A 在 on_model_call 上是最外层 -> 它 'after' 的日志最后打印")


asyncio.run(main())
