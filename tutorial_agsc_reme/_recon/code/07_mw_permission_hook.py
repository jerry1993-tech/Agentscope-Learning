"""07_agentscope_middleware_hook 侦察：on_check_permission hook 实测。

验证点：
1. on_check_permission 是「唯一一个返回单个对象（不是 async generator）」
   的 onion hook（middleware/_base.py:170）。
2. 中间件可以：委托（next_handler(**input_kwargs)）/ 替换 decision /
   完全绕过内置引擎（不调 next_handler）。
3. 链上收到的是 tool_call / tool_input 的 deepcopy
   （agent/_agent.py:2377-2379），改它们不会影响真实调用。
4. 中间件的 decision 会被 Agent 消费：DENY 时工具不会真的执行。
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
from agentscope.permission import (  # noqa: E402
    PermissionBehavior,
    PermissionDecision,
)
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402

EXECUTED: list[str] = []
SEEN: list[str] = []


class DenyAllToolMiddleware(MiddlewareBase):
    """不调 next_handler，直接给 DENY —— 完全绕过内置权限引擎。"""

    async def on_check_permission(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> PermissionDecision:
        tool_call = input_kwargs["tool_call"]
        SEEN.append(
            f"tool_call.name={tool_call.name} "
            f"tool_input={input_kwargs['tool_input']} "
            f"tool_class={type(input_kwargs['tool']).__name__}",
        )
        print(f"   [mw] on_check_permission tool={tool_call.name} -> 直接 DENY")
        print("   [mw] 不调用 next_handler，内置引擎被绕过")
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message="blocked by reconnaissance middleware",
            decision_reason="custom middleware policy",
        )


class DelegateAndLogMiddleware(MiddlewareBase):
    """委托给下游拿到 decision，再把这个 decision 替换掉。"""

    def __init__(self, override_to: PermissionBehavior | None) -> None:
        self.override_to = override_to

    async def on_check_permission(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Any],
    ) -> PermissionDecision:
        decision = await next_handler(**input_kwargs)
        print(
            f"   [mw] 委托结果 tool={input_kwargs['tool_call'].name} "
            f"-> {decision.behavior.value} "
            f"(reason={decision.decision_reason})",
        )
        if self.override_to is not None and self.override_to is not decision.behavior:
            print(f"   [mw] 把 decision 从 {decision.behavior.value} "
                  f"替换成 {self.override_to.value}")
            return PermissionDecision(
                behavior=self.override_to,
                message="overridden by recon middleware",
                decision_reason="middleware policy override",
            )
        return decision


async def write_note(path: str, text: str) -> str:
    """Write a note to a file.

    Args:
        path (`str`): target path.
        text (`str`): content to write.

    Returns:
        `str`: confirmation.
    """
    EXECUTED.append(f"write_note({path}, {text})")
    return f"wrote {len(text)} bytes to {path}"


def build(mws: list) -> Agent:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=200),
    )
    return Agent(
        name="assistant",
        system_prompt=(
            "你是助手。必须调用 write_note 工具把用户的话写到 /tmp/n.txt。"
        ),
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(write_note)]),
        middlewares=mws,
        react_config=ReActConfig(max_iters=3),
    )


async def main() -> None:
    print("=" * 74)
    print("A1. 纯委托：不改 decision（DEFAULT 模式会 fallback 到 ASK -> 走 HITL）")
    print("=" * 74)
    EXECUTED.clear()
    agent = build([DelegateAndLogMiddleware(override_to=None)])
    msg = await agent.reply(
        UserMsg("user", "把 'hello' 写到 /tmp/n.txt。"),
    )
    print("  reply =", msg.get_text_content()[:100].replace("\n", " "))
    print("  实际执行的工具调用 =", EXECUTED, "  <- 空：ASK 需要用户确认")
    print()

    print("=" * 74)
    print("A2. 委托 + 替换：把内置引擎给的 ASK 改成 ALLOW")
    print("=" * 74)
    EXECUTED.clear()
    agent_allow = build(
        [DelegateAndLogMiddleware(override_to=PermissionBehavior.ALLOW)],
    )
    msg_allow = await agent_allow.reply(
        UserMsg("user", "把 'hello' 写到 /tmp/n.txt。"),
    )
    print("  reply =", msg_allow.get_text_content()[:100].replace("\n", " "))
    print("  实际执行的工具调用 =", EXECUTED)
    print()

    print("=" * 74)
    print("B. 拦截型中间件：不调 next_handler，直接 DENY")
    print("=" * 74)
    EXECUTED.clear()
    SEEN.clear()
    agent2 = build([DenyAllToolMiddleware()])
    msg2 = await agent2.reply(
        UserMsg("user", "把 'hello' 写到 /tmp/n.txt。"),
    )
    print("  reply =", msg2.get_text_content()[:160].replace("\n", " "))
    print("  实际执行的工具调用 =", EXECUTED, "  <- 空，工具被拦下")
    print("  中间件看到的内容:")
    for s in SEEN:
        print("   ", s[:130])
    print("  >>> input_kwargs['tool_input'] 是解析并校验过的 dict；")
    print("      input_kwargs['tool'] 是 ToolBase 实例；")
    print("      链上拿到的是 deepcopy，改动不影响真实调用")


asyncio.run(main())
