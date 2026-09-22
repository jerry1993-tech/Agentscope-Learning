"""Recon 15 / 小片段验证: 纯函数 + HintBlock 渲染 + 工具/模式行为。"""
import asyncio, os
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")

from agentscope.message import UserMsg, TextBlock
from agentscope.middleware._longterm_memory._reme._utils import (
    _extract_query_text, _extract_memory_texts)
from agentscope.middleware._longterm_memory._reme._middleware import (
    ReMeMiddleware, _MEMORY_MSG_NAME, _MEMORY_SECTION_HEADER, _TOOL_INSTRUCTIONS)

print("== A. _extract_query_text ==")
print(" single UserMsg ->", repr(_extract_query_text(UserMsg("u", "hello"))))
print(" list          ->", repr(_extract_query_text([UserMsg("u","a"), UserMsg("u","b")])))
print(" None          ->", repr(_extract_query_text(None)))

print("\n== B. _extract_memory_texts ==")
print(" real ReMe shape ->", _extract_memory_texts(
    {"metadata": {"results": [{"text": "a"}, {"text": "b"}]}}))
print(" plain strings   ->", _extract_memory_texts(["x", "y"]))
print(" garbage         ->", _extract_memory_texts({"metadata": {"results": "nope"}}))

print("\n== C. _build_memory_message 渲染成模型消息 ==")
msg = ReMeMiddleware._build_memory_message(["alice prefers dark-mode matplotlib"])
print(" container type :", type(msg).__name__, "| role =", msg.role, "| name =", msg.name)
print(" blocks         :", [type(b).__name__ for b in msg.content])
from agentscope.formatter import DeepSeekChatFormatter
async def _render():
    fmt = DeepSeekChatFormatter()
    out = await fmt.format([UserMsg("alice", "what theme?"), msg])
    for m in out:
        print("  ->", {k: (str(v)[:130]) for k, v in m.items() if k in ("role", "content")})
asyncio.run(_render())

print("\n== D. on_system_prompt 按 mode 分支 ==")
async def check():
    for mode in ("static_control", "agent_control", "both"):
        mw = ReMeMiddleware(parameters=ReMeMiddleware.Parameters(mode=mode))
        p = await mw.on_system_prompt(None, "BASE")
        tools = [t.name for t in await mw.list_tools()]
        print(f"  mode={mode:15s} prompt_has_nudge={'Long-term memory' in p!s:5s} tools={tools}")
asyncio.run(check())

print("\n== E. Parameters 校验: 非法 mode 被 pydantic 拒绝 ==")
try:
    ReMeMiddleware.Parameters(mode="garbage")
except Exception as e:
    print("  ValidationError:", str(e).splitlines()[1].strip())
