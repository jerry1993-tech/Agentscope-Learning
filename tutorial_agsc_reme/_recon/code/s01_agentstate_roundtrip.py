# -*- coding: utf-8 -*-
"""片段 1：AgentState 序列化 -> 落盘 -> 读回 -> 继续对话（纯库，不需要 SQL/Redis）。

运行:
    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
        tutorial_agsc_reme/_recon/code/s01_agentstate_roundtrip.py
"""
import asyncio
import json
import os
import pathlib

from dotenv import load_dotenv

from agentscope.agent import Agent
from agentscope.credential import DeepSeekCredential
from agentscope.model import DeepSeekChatModel
from agentscope.message import UserMsg
from agentscope.state import AgentState

load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")

STATE_FILE = pathlib.Path("/tmp/agentscope_state_demo.json")


def build_agent(state: AgentState) -> Agent:
    """用同一份 state 组装 Agent —— 换汤不换药，就是这个模式让会话可续跑。"""
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
    )
    return Agent(
        name="demo_agent",
        system_prompt="你是一个只会用一句话回答的用户助手。",
        model=model,
        state=state,
    )


async def main() -> None:
    # ---------- 第一轮会话 ----------
    agent = build_agent(AgentState())
    print("[1] 新建 session_id =", agent.state.session_id)

    reply1 = await agent.reply(UserMsg(name="user", content="我叫小明，请记住。"))
    print("[1] reply1 =", reply1.get_text_content())

    # ---------- 落盘 ----------
    STATE_FILE.write_text(
        agent.state.model_dump_json(indent=2),
        encoding="utf-8",
    )
    print("[2] 已落盘:", STATE_FILE, "字节数", STATE_FILE.stat().st_size)
    print("[2] 落盘字段:", sorted(json.loads(STATE_FILE.read_text()).keys()))

    # ---------- 读回 ----------
    raw = STATE_FILE.read_text(encoding="utf-8")
    restored = AgentState.model_validate_json(raw)
    print("[3] 读回 session_id =", restored.session_id)
    print("[3] 读回 context 条数 =", len(restored.context))
    for m in restored.context:
        print("     -", m.role, "|", m.name, "|", (m.get_text_content() or "")[:40])
    assert restored.session_id == agent.state.session_id
    assert len(restored.context) == len(agent.state.context)

    # ---------- 继续对话 ----------
    agent2 = build_agent(restored)
    reply2 = await agent2.reply(UserMsg(name="user", content="我叫什么名字？"))
    print("[4] reply2 =", reply2.get_text_content())
    print("[4] 续跑后 context 条数 =", len(agent2.state.context))


if __name__ == "__main__":
    asyncio.run(main())
