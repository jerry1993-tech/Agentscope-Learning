import asyncio, os, json
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")
from agentscope.agent import Agent
from agentscope.model import DeepSeekChatModel
from agentscope.credential import DeepSeekCredential
from agentscope.tool import Toolkit, FunctionTool
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import Msg, UserMsg

def get_time(city: str) -> str:
    """查询某个城市的当前时间。

    Args:
        city (str): 城市名。
    """
    return f"{city}: 2026-09-21 16:00:00+08:00"

async def main():
    agent = Agent(
        name="Friday", system_prompt="You are a helpful assistant.",
        model=DeepSeekChatModel(
            credential=DeepSeekCredential(api_key=os.environ["OPENAI_API_KEY"]),
            model=os.environ["LLM_MODEL"], stream=True),
        toolkit=Toolkit(tools=[FunctionTool(get_time, permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW, message="read-only"))]),
    )
    events, final = [], None
    async for item in agent.reply_stream(UserMsg("user", "北京现在几点？用工具查。"), yield_final_msg=True):
        if isinstance(item, Msg): final = item
        else: events.append(item)

    ctx_msg = agent.state.context[-1]
    rebuilt = Msg(id=events[0].reply_id, name="Friday", role="assistant", content=[])
    for e in events: rebuilt.append_event(e)

    for name, m in (("context[-1]", ctx_msg), ("rebuilt", rebuilt), ("yielded Msg", final)):
        print(f"{name:12s} id={m.id[:8]} blocks={[b.type for b in m.content]} usage={m.usage}")

    a, b = ctx_msg.model_dump(), rebuilt.model_dump()
    diff = [k for k in a if a[k] != b[k]]
    print("\ncontext[-1] vs rebuilt 差异字段:", diff)
    # 忽略时间戳/自动 id 后比较块结构
    norm = lambda d: [{k: v for k, v in blk.items() if k not in ("created_at", "finished_at")} for blk in d]
    import json as _j
    for i,(x,y) in enumerate(zip(norm(a["content"]), norm(b["content"]))):
        if x!=y:
            print(f"block {i} ({x.get(chr(116)+chr(121)+chr(112)+chr(101))}) diff:")
            print("  ctx:", _j.dumps(x, ensure_ascii=False)[:400])
            print("  new:", _j.dumps(y, ensure_ascii=False)[:400])
asyncio.run(main())
