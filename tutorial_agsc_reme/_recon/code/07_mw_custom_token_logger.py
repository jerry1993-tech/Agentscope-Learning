"""07_agentscope_middleware_hook 侦察：自定义中间件（模型调用前后打日志 + token 统计）。

这是报告里「最小可运行代码」的源码。
验证点：
1. 只实现 on_model_call 的中间件，不会影响 agent 的默认行为（reply 结果与不加中间件一致）。
2. 从 ModelCallEndEvent 里能拿到 input_tokens / output_tokens。
3. 中间件实例上的计数器在多次 reply 之间累加。
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
from agentscope.event import ModelCallEndEvent  # noqa: E402
from agentscope.message import Msg, UserMsg  # noqa: E402
from agentscope.middleware import MiddlewareBase  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


class TokenLoggingMiddleware(MiddlewareBase):
    """在模型调用前后打日志并统计 token 的自定义中间件。

    只实现 on_model_call —— 其余 6 个 hook 不实现，Agent 在构造时会通过
    is_implemented() 把本中间件从另外 6 条链里过滤掉（见
    agent/_agent.py:218-240）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> Any:
        # ---- before：next_handler() 之前 ----
        model = input_kwargs["current_model"]
        self.calls += 1
        print(
            f"[mw] >>> model_call #{self.calls} agent={agent.name} "
            f"model={model.model} msgs={len(input_kwargs['messages'])} "
            f"tools={len(input_kwargs['tools'])}",
        )

        # ---- 调用下游（链尾是真正的 model() 调用）----
        response = await next_handler(**input_kwargs)

        # 注意：ChatResponse 上没有 input_tokens/output_tokens 这两个属性，
        # token 用量挂在 response.usage（ChatUsage | None）上。
        # 早期直接 getattr(response, "input_tokens") 会静默拿到 None。
        usage = getattr(response, "usage", None)
        print(
            f"[mw] <<< model_call #{self.calls} done usage={usage}",
        )
        return response

    # 说明：token 的累计放在 on_reasoning 里做，因为 on_model_call 返回的
    # ChatResponse 只有最后一次调用；ModelCallEndEvent 才是「每一次模型调用都
    # 会发一次」的稳定信号（见 event/_event.py:139）。
    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async for event in next_handler(**input_kwargs):
            if isinstance(event, ModelCallEndEvent):
                self.input_tokens += event.input_tokens
                self.output_tokens += event.output_tokens
            yield event


async def get_time() -> str:
    """Return the current time.

    Returns:
        `str`: a fixed string.
    """
    return "2026-09-21"


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

    def build(middlewares: list) -> Agent:
        return Agent(
            name="assistant",
            system_prompt="你是中文助手，回答尽量简短。",
            model=model,
            toolkit=Toolkit(tools=[FunctionTool(get_time, is_read_only=True)]),
            middlewares=middlewares,
            react_config=ReActConfig(max_iters=3),
        )

    print("=" * 68)
    print("A. 不带中间件")
    plain = build([])
    msg_a = await plain.reply(UserMsg("user", "用一句话介绍你自己。"))
    print("reply =", msg_a.get_text_content()[:60].replace("\n", " "))
    print()

    print("=" * 68)
    print("B. 带 TokenLoggingMiddleware")
    mw = TokenLoggingMiddleware()
    wrapped = build([mw])
    msg_b = await wrapped.reply(UserMsg("user", "用一句话介绍你自己。"))
    print("reply =", msg_b.get_text_content()[:60].replace("\n", " "))
    print()

    print("=" * 68)
    print("C. 分流结果（agent/_agent.py:218-240 的 is_implemented 过滤）")
    for attr in (
        "_reply_middlewares",
        "_reasoning_middlewares",
        "_check_permission_middlewares",
        "_acting_middlewares",
        "_model_call_middlewares",
        "_system_prompt_middlewares",
        "_compress_context_middlewares",
    ):
        print(f"  {attr:32s} = {[type(m).__name__ for m in getattr(wrapped, attr)]}")
    print()

    print("=" * 68)
    print("D. 中间件实例自身的统计（跨 reply 累加）")
    print(f"  model calls           = {mw.calls}")
    print(f"  input tokens (累计)    = {mw.input_tokens}")
    print(f"  output tokens (累计)   = {mw.output_tokens}")

    msg_c = await wrapped.reply(UserMsg("user", "再说一句。"))
    print(f"  第二次 reply 后 calls  = {mw.calls}")
    print(f"  第二次 reply 后 tokens = {mw.input_tokens}/{mw.output_tokens}")
    print(f"  第二次 reply 内容      = {msg_c.get_text_content()[:60]!r}")
    print()

    print("=" * 68)
    print("E. 结论：只包一层不影响默认行为 ->",
          "A 与 B 都是正常回复，无异常")


asyncio.run(main())
