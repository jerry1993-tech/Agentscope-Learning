# -*- coding: utf-8 -*-
"""侦察代码片段 3：AgentState 的字段、append_context、序列化与「awaiting tool call」判定"""
import json
from agentscope.state import AgentState, ReplyContext, TaskContext, ToolContext, Task
from agentscope.message import AssistantMsg, UserMsg, TextBlock, ToolCallBlock, ToolResultBlock, ToolCallState, ToolResultState

state = AgentState(session_id="sess_001")
print("session_id:", state.session_id)
print("reply_id (property -> reply_context):", state.reply_id)
print("顶层字段:", list(state.model_dump().keys()))

# 一次 reply 累积成一条 assistant 消息
state.append_context("Friday", [TextBlock(text="你好，")])
state.append_context("Friday", [TextBlock(text="我是助手。")])
print("\ncontext 长度:", len(state.context), "| role:", state.context[0].role,
      "| id == reply_id:", state.context[0].id == state.reply_id)
print("合并后的文本:", state.context[0].get_text_content())

# ASKING 状态 -> has_awaiting_tool_calls
state.append_context("Friday", [
    ToolCallBlock(id="tc1", name="delete_file", input="{}", state=ToolCallState.ASKING),
])
print("has_awaiting_tool_calls:", state.has_awaiting_tool_calls("Friday"))
print("get_unfinished_tool_calls:", [b.name for b in state.get_unfinished_tool_calls("Friday")])

# 序列化
d = state.model_dump()
s = state.model_dump_json()
back = AgentState.model_validate(json.loads(s))
assert back == state
print("\n=== AgentState roundtrip OK ===")
print("summary:", repr(back.summary))
print("middle_context:", back.middle_context)
print("tasks_context:", back.tasks_context.model_dump())
print("tool_context.activated_groups:", back.tool_context.activated_groups)

# 旧格式兼容：顶层 reply_id / cur_iter 迁移进 reply_context
legacy = {"session_id": "s2", "reply_id": "r-legacy", "cur_iter": 7, "context": []}
migrated = AgentState.model_validate(legacy)
print("\n迁移后 reply_id:", migrated.reply_id, "cur_iter:", migrated.cur_iter)
assert migrated.reply_id == "r-legacy" and migrated.cur_iter == 7
print("=== legacy 迁移 OK ===")

# Task 数据结构
t = Task(subject="写报告", description="写一份侦察报告", metadata={})
print("\nTask:", t.model_dump())
