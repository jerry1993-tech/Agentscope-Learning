# -*- coding: utf-8 -*-
"""侦察代码片段 1：手工构造带 tool 的 Msg 列表 -> formatter 转 payload -> 反序列化回来断言一致"""
import asyncio, json

from agentscope.message import (
    Msg, UserMsg, AssistantMsg, SystemMsg,
    TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock,
    ToolCallState, ToolResultState, DataBlock, Base64Source, HintBlock, Usage,
)
from agentscope.formatter import DeepSeekChatFormatter

# ---------- 1) 手工构造 Msg 列表 ----------
sys_msg = SystemMsg(name="system", content="You are a helpful agent.")
user_msg = UserMsg(name="user", content="北京现在几点？请调用工具查询。", id="msg_user_001")

# 关键：AgentScope 2.x 把 ToolResultBlock 也放在 assistant 消息里（一次 reply 累积成一条消息）
assistant_msg = AssistantMsg(
    name="assistant",
    content=[
        ThinkingBlock(thinking="用户要的是当前时间，我应该调用 get_time 工具。", id="b_think_001"),
        ToolCallBlock(id="tc_001", name="get_time", input='{"city": "Beijing"}',
                      state=ToolCallState.ALLOWED),
        ToolResultBlock(id="tc_001", name="get_time", output="2026-09-21T16:00:00+08:00",
                        state=ToolResultState.SUCCESS, metadata={"elapsed_ms": 12}),
        TextBlock(text="北京时间 2026-09-21 16:00。", id="b_text_001"),
    ],
    id="msg_asst_001",
    usage=Usage(input_tokens=120, output_tokens=18),
)
msgs = [sys_msg, user_msg, assistant_msg]

# ---------- 2) formatter 转成 payload ----------
payload = asyncio.run(DeepSeekChatFormatter().format(msgs))
print("=== payload ===")
print(json.dumps(payload, ensure_ascii=False, indent=2))

# ---------- 3) 序列化 / 反序列化 ----------
raw = [m.model_dump() for m in msgs]
back = [Msg.model_validate(d) for d in raw]
assert back == msgs, "roundtrip failed"
print("\n=== roundtrip OK ===")
print("block types after roundtrip:", [type(b).__name__ for b in back[2].content])
print("block type tags:", [b.type for b in back[2].content])
print("\n=== assistant msg json ===")
print(back[2].model_dump_json(indent=2))
