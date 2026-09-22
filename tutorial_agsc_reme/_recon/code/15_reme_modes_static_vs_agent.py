"""Recon 15 / 分别验证 static_control 与 agent_control 两条检索路径。

static_control: 没有 memory_search 工具可用 -> 答对只可能来自自动注入。
agent_control : 没有自动注入 -> 答对只可能来自 Agent 自己调用 memory_search。
"""
import asyncio, os, shutil, logging, sys
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")

from agentscope.credential import DeepSeekCredential
from agentscope.model import DeepSeekChatModel
from agentscope.agent import Agent
from agentscope.message import UserMsg
from agentscope.middleware import ReMeMiddleware
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.middleware._longterm_memory._reme import _config
from agentscope.event import ToolCallStartEvent, ToolResultEndEvent, ToolResultTextDeltaEvent

logging.getLogger("reme").setLevel(logging.ERROR)
_config._dream_steps = lambda: [
    {"backend": "dream_extract_step", "file_catalog": "dream", "scan_days": 2, "max_units": 5},
    {"backend": "dream_integrate_step"},
    {"backend": "dream_finish_step", "file_catalog": "dream"},
]

PREF = ("Remember this permanently: my name is Bob, my employee badge is ZX-7741, "
        "and I always want every report exported as PDF landscape.")

async def run(mode: str, ws: str):
    shutil.rmtree(ws, ignore_errors=True)
    chat = DeepSeekChatModel(
        credential=DeepSeekCredential(api_key=os.environ["OPENAI_API_KEY"],
                                      base_url=os.environ["OPENAI_BASE_URL"]),
        model=os.environ["LLM_MODEL"], stream=True)
    mw = ReMeMiddleware(workspace_dir=ws,
                        parameters=ReMeMiddleware.Parameters(chat_model=chat, mode=mode, top_k=5))
    tools = await mw.list_tools()

    def build(sid):
        return Agent(name="assistant",
                     system_prompt=("You are a terse reporting assistant. Answer only from what you "
                                    "actually know; never invent a badge number. If you need a durable "
                                    "fact from a past session, use the memory_search tool."),
                     model=chat, toolkit=Toolkit(tools=list(tools)),
                     middlewares=[mw], state=AgentState(session_id=sid))
    try:
        a1 = build("s1")
        await a1.reply(UserMsg("bob", PREF))
        await mw._run_job("reindex")

        a2 = build("s2")
        tool_used = []
        async for ev in a2.reply_stream(inputs=UserMsg("bob", "What is my employee badge number?")):
            if isinstance(ev, ToolCallStartEvent):
                tool_used.append(ev.tool_call_name)
        final = a2.state.context[-1]
        ans = final.get_text_content() or ""
        injected = [m for m in a2.state.context if getattr(m, "name", None) == "memory"]
        print(f"\n########## mode = {mode} ##########")
        print("  tools available to agent :", [t.name for t in tools] or "(none)")
        print("  tools the agent called   :", tool_used or "(none)")
        print("  static memory notes injected:", len(injected))
        print("  final answer:", ans.replace("\n", " ")[:300])
        print("  -> correct badge ZX-7741 :", "ZX-7741" in ans)
    finally:
        await mw.close()

async def main():
    await run("static_control", "/tmp/recon15/ws_static")
    await run("agent_control", "/tmp/recon15/ws_agentctl")

asyncio.run(main())
