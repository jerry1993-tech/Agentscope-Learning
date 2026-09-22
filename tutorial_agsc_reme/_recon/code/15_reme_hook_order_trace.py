"""Recon 15 / 用一个探针 middleware 打印 hook 调用顺序与检索任务状态。"""
import asyncio, os, shutil, logging
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")
from agentscope.credential import DeepSeekCredential
from agentscope.model import DeepSeekChatModel
from agentscope.agent import Agent
from agentscope.message import UserMsg
from agentscope.middleware import ReMeMiddleware, MiddlewareBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.middleware._longterm_memory._reme import _config
from typing import AsyncGenerator, Callable

logging.getLogger("reme").setLevel(logging.ERROR)
_config._dream_steps = lambda: [
    {"backend": "dream_extract_step", "file_catalog": "dream", "scan_days": 2, "max_units": 5},
    {"backend": "dream_integrate_step"},
    {"backend": "dream_finish_step", "file_catalog": "dream"},
]

class Probe(MiddlewareBase):
    """打印 on_reply / on_reasoning 的进出顺序（放在 ReMe 之后，即内层）。"""
    def __init__(self, mw): self.mw, self.n = mw, 0
    async def on_reply(self, agent, input_kwargs, next_handler) -> AsyncGenerator:
        print("  >> on_reply ENTER ; session =", agent.state.session_id)
        async for it in next_handler(**input_kwargs):
            yield it
        print("  >> on_reply EXIT ; retrieval tasks =", list(self.mw._retrieval_tasks))
    async def on_reasoning(self, agent, input_kwargs, next_handler) -> AsyncGenerator:
        self.n += 1
        t = self.mw._retrieval_tasks.get(agent.state.session_id)
        print(f"     .. on_reasoning #{self.n}  task={'None' if t is None else ('done' if t.done() else 'PENDING')}")
        async for it in next_handler(**input_kwargs):
            yield it


from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from typing import Any
class _T(ToolBase):
    name: str = "noop"
    description: str = "Return the string 'ok'. Always call this when the user asks you to 'count'."
    input_schema: dict = {"type":"object","properties":{"x":{"type":"string"}},"required":["x"]}
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    is_external_tool: bool = False
    is_mcp: bool = False
    async def check_permissions(self,*_a: Any,**_k: Any) -> PermissionDecision:
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="local")
    async def __call__(self, x: str, **kw: Any) -> ToolChunk:
        return ToolChunk(content=[TextBlock(type="text", text=f"ok:{x}")])

async def main():
    ws = "/tmp/recon15/ws_trace"
    shutil.rmtree(ws, ignore_errors=True)
    chat = DeepSeekChatModel(credential=DeepSeekCredential(api_key=os.environ["OPENAI_API_KEY"],
                              base_url=os.environ["OPENAI_BASE_URL"]),
                             model=os.environ["LLM_MODEL"], stream=True)
    mw = ReMeMiddleware(workspace_dir=ws,
                        parameters=ReMeMiddleware.Parameters(chat_model=chat, mode="static_control", top_k=5))
    try:
        a1 = Agent(name="a", system_prompt="terse", model=chat, toolkit=Toolkit(),
                   middlewares=[mw, Probe(mw)], state=AgentState(session_id="s1"))
        await a1.reply(UserMsg("bob", "Remember permanently: my badge is ZX-7741."))
        await mw._run_job("reindex")

        for label, prompt in [("SINGLE-SHOT", "What is my badge number?"),
                              ("MULTI-STEP", "Call the noop tool with x=1, then tell me my badge number.")]:
            print(f"\n=== {label} ===")
            a = Agent(name="a", system_prompt="terse", model=chat, toolkit=Toolkit(tools=[_T()]),
                      middlewares=[mw, Probe(mw)], state=AgentState(session_id="s2"))
            await a.reply(UserMsg("bob", prompt))
            notes = [m for m in a.state.context if getattr(m, "name", None) == "memory"]
            print("  -> injected notes:", len(notes))
    finally:
        await mw.close()

asyncio.run(main())
