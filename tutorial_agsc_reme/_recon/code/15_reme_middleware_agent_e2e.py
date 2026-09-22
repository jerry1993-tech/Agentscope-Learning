"""Recon 15 / 端到端: AgentScope Agent + ReMeMiddleware (real DeepSeek LLM).

场景: session-1 说出一个持久偏好 -> 中间件自动写回 ReMe;
     reindex 让写入立刻可检索;
     session-2 (全新 Agent, 空 context) 问一个必须用到该记忆的问题。
"""
import asyncio, os, shutil, logging
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")

from agentscope.credential import DeepSeekCredential
from agentscope.model import DeepSeekChatModel
from agentscope.agent import Agent
from agentscope.message import UserMsg
from agentscope.middleware import ReMeMiddleware
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolBase, ToolChunk
from agentscope.message import TextBlock
from agentscope.middleware._longterm_memory._reme import _config

logging.getLogger("reme").setLevel(logging.ERROR)

# ---- ReMe 0.4.1.13 compat shim (see report) --------------------------
_config._dream_steps = lambda: [
    {"backend": "dream_extract_step", "file_catalog": "dream", "scan_days": 2, "max_units": 5},
    {"backend": "dream_integrate_step"},
    {"backend": "dream_finish_step", "file_catalog": "dream"},
]
# ---------------------------------------------------------------------

WS = "/tmp/recon15/ws_agent"
shutil.rmtree(WS, ignore_errors=True)
MODE = "both"

async def main():
    chat = DeepSeekChatModel(
        credential=DeepSeekCredential(api_key=os.environ["OPENAI_API_KEY"],
                                      base_url=os.environ["OPENAI_BASE_URL"]),
        model=os.environ["LLM_MODEL"], stream=True)

    mw = ReMeMiddleware(workspace_dir=WS,
                        parameters=ReMeMiddleware.Parameters(chat_model=chat, mode=MODE, top_k=5))
    tools = await mw.list_tools()
    print("middleware tools:", [t.name for t in tools])

    def build(sid):
        return Agent(name="assistant",
                     system_prompt=("You are a helpful data-analysis assistant. Be concise. "
                                    "When the request may depend on a durable fact from a past "
                                    "session (a preference, a name, a prior decision), you MUST "
                                    "call the memory_search tool first."),
                     model=chat, toolkit=Toolkit(tools=list(tools)),
                     middlewares=[mw], state=AgentState(session_id=sid))

    try:
        # ---------- SESSION 1: state a durable preference ----------
        a1 = build("session-1")
        r1 = await a1.reply(UserMsg("alice", "Hi! My name is Alice, I'm based in Hangzhou. "
                                               "For every chart I always want matplotlib in dark mode."))
        print("\n[SESSION-1 assistant]", (r1.get_text_content() or "")[:200])
        await mw._run_job("reindex")
        persisted = await mw._search("user chart preference and location", limit=20)
        print("[SESSION-1 written back]", len(persisted), "chunk(s) now searchable")

        # ---------- SESSION 2: fresh agent, empty context ----------
        a2 = build("session-2")
        q = ("I need a bar chart of monthly sales. Which plotting library and which theme "
             "should you use for me, and what city am I based in?")
        print("\n[SESSION-2 user]", q)
        r2 = await a2.reply(UserMsg("alice", q))
        ans = r2.get_text_content() or ""
        print("[SESSION-2 assistant]", ans)

        note = [m for m in a2.state.context if getattr(m, "name", None) == "memory"]
        print("\n[static path] injected memory notes:", len(note))
        for m in note:
            for b in m.get_content_blocks("hint"):
                print("   ", b.hint[:300].replace("\n", " | "))

        low = ans.lower()
        print("\n=== VERDICT ===")
        print("  mentions matplotlib :", "matplotlib" in low)
        print("  mentions dark       :", "dark" in low)
        print("  mentions hangzhou   :", "hangzhou" in low)
        print("  mentions alice      :", "alice" in low)
    finally:
        await mw.close()

asyncio.run(main())
