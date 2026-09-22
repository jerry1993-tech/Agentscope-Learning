# -*- coding: utf-8 -*-
"""侦察代码片段 7：内容块的边界行为（extra 字段、枚举落字符串、角色校验、HintBlock 时间戳默认值）。

运行:
    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
        tutorial_agsc_reme/_recon/code/04_blk_edges.py
"""
import json
import warnings

from pydantic import ValidationError

from agentscope.message import (
    Msg, UserMsg, AssistantMsg, SystemMsg, HintBlock, ThinkingBlock,
    TextBlock, ToolCallBlock, ToolCallState, ToolResultBlock, ToolResultState,
)

# 1) ThinkingBlock extra="allow"：Anthropic 的 signature 可以塞进任意字段
tb = ThinkingBlock(thinking="...", signature="abc123", redacted_thinking_data="xxx")
print("1) ThinkingBlock extra 字段:", tb.model_dump())
print("   往返后 signature 还在:", ThinkingBlock.model_validate(tb.model_dump()).signature)

# 2) ToolCallBlock/ToolResultBlock 的 use_enum_values=True：state 落成裸字符串
tcb = ToolCallBlock(id="tc1", name="f", input="{}", state=ToolCallState.ASKING)
print("\n2) tool_call state 字段类型:", type(tcb.state).__name__, repr(tcb.state))
print("   state == ToolCallState.ASKING ?", tcb.state == ToolCallState.ASKING)
trb = ToolResultBlock(id="tc1", name="f", output="ok", state=ToolResultState.SUCCESS)
print("   tool_result state 类型:", type(trb.state).__name__, repr(trb.state))

# 3) HintBlock.finished_at 有 default_factory（其它块都是 None）
hb = HintBlock(hint="hi")
print("\n3) HintBlock.finished_at 默认值:", hb.finished_at, "| created_at:", hb.created_at)
print("   TextBlock.finished_at 默认值:", TextBlock(text="x").finished_at)

# 4) 角色约束：user/system 只收 text(+data)
try:
    UserMsg("u", [ToolCallBlock(id="t", name="f", input="{}")])
except ValidationError as e:
    print("\n4) user 消息塞 tool_call -> ValidationError:", str(e).splitlines()[1].strip())
try:
    SystemMsg("s", [TextBlock(text="a"), HintBlock(hint="b")])
except ValidationError as e:
    print("   system 消息塞 hint -> ValidationError:", str(e).splitlines()[1].strip())
print("   assistant 消息随便塞:", [b.type for b in AssistantMsg(
    "a", [TextBlock(text="x"), ToolCallBlock(id="t", name="f", input="{}")]).content])

# 5) 反序列化时未知块类型会被拒绝
try:
    Msg.model_validate({"name": "a", "role": "assistant", "id": "m1",
                        "content": [{"type": "unknown_block", "foo": 1}]})
except ValidationError as e:
    print("\n5) 未知块类型 -> ValidationError:", str(e).splitlines()[1].strip())

# 6) 已废弃的事件类型仍然可用但会产生 DeprecationWarning
from agentscope.event import ExceedMaxItersEvent, ReplyEndReason
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    ExceedMaxItersEvent(reply_id="r", name="agent")
    ReplyEndReason.COMPLETED
    print("\n6) 废弃告警条数:", len(w))
    for x in w:
        print("   -", str(x.message)[:80])
