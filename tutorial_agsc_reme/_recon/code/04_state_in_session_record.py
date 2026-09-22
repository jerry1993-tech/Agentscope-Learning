# -*- coding: utf-8 -*-
"""侦察代码片段 6：SessionRecord 里嵌入 AgentState，整条会话就是一个 JSON 文档"""
import json
from agentscope.app.storage._model._session import SessionRecord, SessionConfig
from agentscope.state import AgentState
from agentscope.message import UserMsg

state = AgentState(session_id="sess_001")
state.append_context("Friday", [])
state.context.insert(0, UserMsg("user", "你好"))

rec = SessionRecord(user_id="u1", agent_id="a1", config=SessionConfig(workspace_id="ws1"), state=state)
d = rec.model_dump(mode="json")
print("SessionRecord 顶层字段:", list(d.keys()))
print("state 内字段:", list(d["state"].keys()))
print("state.context[0]:", json.dumps(d["state"]["context"][0], ensure_ascii=False))
back = SessionRecord.model_validate(d)
print("roundtrip OK:", back == rec)
print("\n整个 state 的 JSON 长度:", len(json.dumps(d["state"], ensure_ascii=False)))
