"""Recon 15 / static_control 的两步回合: 证明注入只在 >=2 个 reasoning step 时落地。"""
import asyncio, os, shutil, logging, time
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
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.middleware._longterm_memory._reme import _config
from typing import Any

logging.getLogger("reme").setLevel(logging.ERROR)
_config._dream_steps = lambda: [
    {"backend": "dream_extract_step", "file_catalog": "dream", "scan_days": 2, "max_units": 5},
    {"backend": "dream_integrate_step"},
    {"backend": "dream_finish_step", "file_catalog": "dream"},
]

class RowCountTool(ToolBase):
    """一个本地工具: 让 Agent 必然多走一步 reasoning, 给后台检索留出时间。"""
    name: str = "lookup_row_count"
    description: str = "Return the row count of a named internal table."
    input_schema: dict = {"type": "object",
                          "properties": {"table": {"type": "string", "description": "table name"}},
                          "required": ["table"]}
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    is_external_tool: bool = False
    is_mcp: bool = False
    async def check_permissions(self, *_a: Any, **_k: Any) -> PermissionDecision:
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="local read-only")
    async def __call__(self, table: str, **kw: Any) -> ToolChunk:
        return ToolChunk(content=[TextBlock(type="text", text=f"{table} has 4210 rows.")])

async def main():
    ws = "/tmp/recon15/ws_static2"
    shutil.rmtree(ws, ignore_errors=True)
    chat = DeepSeekChatModel(
        credential=DeepSeekCredential(api_key=os.environ["OPENAI_API_KEY"],
                                      base_url=os.environ["OPENAI_BASE_URL"]),
        model=os.environ["LLM_MODEL"], stream=True)
    mw = ReMeMiddleware(workspace_dir=ws,
                        parameters=ReMeMiddleware.Parameters(chat_model=chat, mode="static_control", top_k=5))
    try:
        a1 = Agent(name="assistant", system_prompt="You are terse.",
                   model=chat, toolkit=Toolkit(), middlewares=[mw],
                   state=AgentState(session_id="s1"))
        await a1.reply(UserMsg("bob", "Remember permanently: my employee badge is ZX-7741."))
        await mw._run_job("reindex")

        t0 = time.perf_counter()
        await mw._search("employee badge number", limit=5)
        print(f"[timing] one ReMe search job (BM25-only) = {time.perf_counter()-t0:.3f}s")

        a2 = Agent(name="assistant", system_prompt="You are terse. Use tools when asked.",
                   model=chat, toolkit=Toolkit(tools=[RowCountTool()]), middlewares=[mw],
                   state=AgentState(session_id="s2"))
        await a2.reply(UserMsg("bob", "How many rows does the table 'orders' have, and "
                                        "what is my employee badge number?"))
        notes = [m for m in a2.state.context if getattr(m, "name", None) == "memory"]
        print("[static_control 2-step] injected memory notes =", len(notes))
        for m in notes:
            for b in m.get_content_blocks("hint"):
                print("   note:", b.hint.replace("\n", " | ")[:260])
        ans = (a2.state.context[-1].get_text_content() or "")
        print("[final answer]", ans.replace("\n", " ")[:300])
        print("-> correct badge ZX-7741 :", "ZX-7741" in ans)
    finally:
        await mw.close()

asyncio.run(main())
