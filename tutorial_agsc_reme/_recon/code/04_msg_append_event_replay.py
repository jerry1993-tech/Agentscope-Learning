# -*- coding: utf-8 -*-
"""侦察代码片段 2：用真实事件流重建 Msg（Msg.append_event = 事件溯源回放）"""
from agentscope.event import (
    ReplyStartEvent, ReplyEndEvent, TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent,
    ThinkingBlockStartEvent, ThinkingBlockDeltaEvent, ThinkingBlockEndEvent,
    ToolCallStartEvent, ToolCallDeltaEvent, ToolCallEndEvent,
    ToolResultStartEvent, ToolResultTextDeltaEvent, ToolResultEndEvent,
    RequireUserConfirmEvent, UserConfirmResultEvent, ConfirmResult,
    ModelCallEndEvent, DataBlockStartEvent, DataBlockDeltaEvent, DataBlockEndEvent,
)
from agentscope.message import Msg, AssistantMsg, TextBlock, ToolCallBlock, ToolCallState, ToolResultState, Base64Source, DataBlock
from agentscope.types import ReplyFinishedReason
from agentscope.model import FinishedReason

RID = "reply_001"
events = [
    ReplyStartEvent(session_id="s1", reply_id=RID, name="assistant"),
    ModelCallEndEvent(reply_id=RID, input_tokens=100, output_tokens=10),
    ThinkingBlockStartEvent(reply_id=RID, block_id="b_think"),
    ThinkingBlockDeltaEvent(reply_id=RID, block_id="b_think", delta="我在想"),
    ThinkingBlockDeltaEvent(reply_id=RID, block_id="b_think", delta="要不要调工具"),
    ThinkingBlockEndEvent(reply_id=RID, block_id="b_think"),
    TextBlockStartEvent(reply_id=RID, block_id="b_text"),
    TextBlockDeltaEvent(reply_id=RID, block_id="b_text", delta="好，我查一下。"),
    TextBlockEndEvent(reply_id=RID, block_id="b_text"),
    ToolCallStartEvent(reply_id=RID, tool_call_id="tc1", tool_call_name="get_time"),
    ToolCallDeltaEvent(reply_id=RID, tool_call_id="tc1", delta='{"city":'),
    ToolCallDeltaEvent(reply_id=RID, tool_call_id="tc1", delta=' "Beijing"}'),
    ToolCallEndEvent(reply_id=RID, tool_call_id="tc1"),
    # 权限系统 ASK -> 用户点确认
    RequireUserConfirmEvent(reply_id=RID, tool_calls=[
        ToolCallBlock(id="tc1", name="get_time", input='{"city": "Beijing"}')]),
    UserConfirmResultEvent(reply_id=RID, confirm_results=[
        ConfirmResult(confirmed=True,
                      tool_call=ToolCallBlock(id="tc1", name="get_time", input='{"city": "Beijing"}'))]),
    # 工具执行结果流
    ToolResultStartEvent(reply_id=RID, tool_call_id="tc1", tool_call_name="get_time"),
    ToolResultTextDeltaEvent(reply_id=RID, tool_call_id="tc1", delta="2026-09-21 16:00"),
    ToolResultEndEvent(reply_id=RID, tool_call_id="tc1", state=ToolResultState.SUCCESS,
                       metadata={"elapsed_ms": 8}),
    # 二进制 data block（base64 分片，各自独立 padding）
    DataBlockStartEvent(reply_id=RID, block_id="b_data", media_type="image/png"),
    DataBlockDeltaEvent(reply_id=RID, block_id="b_data", media_type="image/png", data="aGVs"),
    DataBlockDeltaEvent(reply_id=RID, block_id="b_data", media_type="image/png", data="bG8="),
    DataBlockEndEvent(reply_id=RID, block_id="b_data"),
    ModelCallEndEvent(reply_id=RID, input_tokens=150, output_tokens=20),
    ReplyEndEvent(session_id="s1", reply_id=RID, finished_reason=ReplyFinishedReason.COMPLETED),
]

msg = Msg(id=RID, name="assistant", role="assistant", content=[])
for ev in events:
    msg.append_event(ev)

print("blocks:", [(b.type, getattr(b, "text", None) or getattr(b, "thinking", None)
                   or getattr(b, "state", None) or getattr(b, "name", None)) for b in msg.content])
print("usage:", msg.usage)
print("finished_at:", msg.finished_at, "finished_reason:", msg.finished_reason)
tc = msg.get_content_blocks("tool_call")[0]
print("tool_call state:", tc.state, "| input:", tc.input)
tr = msg.get_content_blocks("tool_result")[0]
print("tool_result state:", tr.state, "| metadata:", tr.metadata)
db = msg.get_content_blocks("data")[0]
import base64
print("data block bytes:", base64.b64decode(db.source.data), "| media:", db.source.media_type)

assert tc.state == ToolCallState.FINISHED       # ToolResultEnd 会把配对的 call 翻成 finished
assert msg.usage.input_tokens == 250 and msg.usage.output_tokens == 30
assert base64.b64decode(db.source.data) == b"hello"
assert msg.finished_reason == ReplyFinishedReason.COMPLETED
print("\n=== 全部断言通过 ===")
