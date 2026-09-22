# -*- coding: utf-8 -*-
"""侦察代码片段 5：事件的序列化 / 反序列化（TypeAdapter over AgentEvent 联合类型）"""
from pydantic import TypeAdapter
from agentscope.event import AgentEvent, TextBlockDeltaEvent, ToolResultEndEvent, EventType
from agentscope.message import ToolResultState

# 事件是纯 pydantic model，序列化用 model_dump / model_dump_json，
# 反序列化因为 AgentEvent 是 union TypeAlias，必须用 TypeAdapter。
ev = TextBlockDeltaEvent(reply_id="r1", block_id="b1", delta="hi")
d = ev.model_dump(mode="json")
print("序列化:", d)
print("type 字段的真实类型:", type(d["type"]).__name__, "->", repr(d["type"]))

adapter = TypeAdapter(AgentEvent)
back = adapter.validate_python(d)
print("反序列化类型:", type(back).__name__, "| 相等:", back == ev)

ev2 = ToolResultEndEvent(reply_id="r1", tool_call_id="tc1", state=ToolResultState.SUCCESS)
d2 = ev2.model_dump(mode="json")
b2 = adapter.validate_python(d2)
print("\n", d2)
print("反序列化:", type(b2).__name__, "| state 类型:", type(b2.state).__name__, repr(b2.state))
print("注意：EventBase use_enum_values=True，所以 state 落成裸字符串 'success'")

# 未知 type 会报错（union 无 discriminator fallback）
try:
    adapter.validate_python({"type": "NOPE", "reply_id": "r"})
except Exception as e:
    print("\n未知事件类型 -> ", type(e).__name__)
