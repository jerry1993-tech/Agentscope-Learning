# -*- coding: utf-8 -*-
"""片段 2：用 AsyncSQLAlchemyStorage + SQLite 做一次完整的「会话落库 -> 进程重启 -> 断点续跑」。

运行:
    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
        tutorial_agsc_reme/_recon/code/s02_session_sqlite_roundtrip.py

需要 aiosqlite（`pip install aiosqlite`）。
"""
import asyncio
import os
import pathlib

from dotenv import load_dotenv

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    SessionConfig,
    UserOrigin,
)
from agentscope.credential import DeepSeekCredential
from agentscope.message import UserMsg
from agentscope.model import DeepSeekChatModel
from agentscope.state import AgentState

load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")

DB_PATH = pathlib.Path("/tmp/agentscope_session_demo.db")
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"
USER_ID = "user-1"


def build_agent(state: AgentState) -> Agent:
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
        context_config=ContextConfig(),
        react_config=ReActConfig(),
    )


def session_config() -> SessionConfig:
    return SessionConfig(
        workspace_id="ws-demo",
        name="demo session",
        chat_model_config=ChatModelConfig(
            type="deepseek_credential",
            credential_id="cred-1",
            model=os.environ["LLM_MODEL"],
            parameters={},
        ),
    )


async def phase_1_turn_one() -> tuple[str, str]:
    """第一段：建 session，跑一轮对话，落库。返回 (agent_id, session_id)。"""
    async with AsyncSQLAlchemyStorage(DB_URL, create_tables=True) as storage:
        # 注意：SQL 后端的 upsert_agent 返回的是 agent_id (str)，不是 record
        agent_id = await storage.upsert_agent(
            USER_ID,
            AgentRecord(
                user_id=USER_ID,
                data=AgentData(
                    name="demo_agent",
                    context_config=ContextConfig(),
                    react_config=ReActConfig(),
                ),
            ),
        )
        session = await storage.upsert_session(
            user_id=USER_ID,
            agent_id=agent_id,
            config=session_config(),
            origin=UserOrigin(),
        )
        print("[1] 新建 session:", session.id, "origin =", session.origin.type)

        agent = build_agent(session.state)

        # --- 用户输入：先落消息日志，再喂给 Agent Lopp ---
        user_msg = UserMsg(name="user", content="我叫小明，我的工位在 3 楼。")
        await storage.upsert_message(USER_ID, session.id, user_msg)
        reply = await agent.reply(user_msg)
        # 本 AgentScope 2.0.8 的写法：agent.reply 直接把 reply 追加进 state.context
        await storage.upsert_message(USER_ID, session.id, reply)
        await storage.update_session_state(
            USER_ID,
            agent_id,
            session.id,
            agent.state,
        )
        print("[2] 第 1 轮完成，reply =", reply.get_text_content())
        print("[2] state.context 条数 =", len(agent.state.context))
        return agent_id, session.id


async def phase_2_restart_and_resume(agent_id: str, session_id: str) -> None:
    """第二段：全新的 storage 实例 + 全新 Agent，从库里恢复后继续对话。"""
    async with AsyncSQLAlchemyStorage(DB_URL, create_tables=True) as storage:
        # 1) 读回会话记录（包含 AgentState）
        record = await storage.get_session(USER_ID, agent_id, session_id)
        assert record is not None
        print("[3] 恢复 session:", record.id, "creator =", record.config.name)
        print("[3] 恢复 state.context 条数 =", len(record.state.context))
        for m in record.state.context:
            print("     -", m.role, "|", m.name, "|", (m.get_text_content() or "")[:36])

        # 2) 读回消息日志（两种读法：全量 / 游标分页）
        msgs, has_more = await storage.list_messages(USER_ID, session_id, limit=50)
        print("[4] 消息日志条数 =", len(msgs), "has_more =", has_more)
        assert len(msgs) == len(record.state.context)

        page, has_more = await storage.list_messages(
            USER_ID,
            session_id,
            limit=1,
        )
        print("[4] 最后一页 =", [m.id for m in page], "has_more =", has_more)

        # 3) 用读回的 state 重建 Agent —— 断点续跑
        agent = build_agent(record.state)
        reply = await agent.reply(UserMsg(name="user", content="我的工位在哪一层？"))
        print("[5] 续跑 reply =", reply.get_text_content())
        print("[5] 续跑后 context 条数 =", len(agent.state.context))
        await storage.upsert_message(USER_ID, session_id, reply)
        await storage.update_session_state(
            USER_ID,
            agent_id,
            session_id,
            agent.state,
        )


async def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()  # 每次从空库开始，方便重复运行
    agent_id, session_id = await phase_1_turn_one()
    await phase_2_restart_and_resume(agent_id, session_id)


if __name__ == "__main__":
    asyncio.run(main())
