# -*- coding: utf-8 -*-
"""片段 3：用 fakeredis 跑 RedisStorage（会话 + 消息日志）与 RedisMessageBus（事件回放日志）。

片段 3a 故意不依赖真实 Redis server，用 fakeredis 替换底层 client。
真实生产环境把 FakeRedis 换成 redis.asyncio 连接即可，代码路径完全一致。

运行:
    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
        tutorial_agsc_reme/_recon/code/s03_redis_session_and_eventlog.py

需要 fakeredis（`pip install fakeredis`）与 redis（`pip install redis`）。
"""
import asyncio
from typing import Any

import fakeredis.aioredis

from agentscope.app.message_bus import MessageBusKeys, RedisMessageBus
from agentscope.app.storage import (
    ChatModelConfig,
    RedisStorage,
    ScheduleOrigin,
    SessionConfig,
)
from agentscope.state import AgentState
from agentscope.message import UserMsg, AssistantMsg


class FakeRedisStorage(RedisStorage):
    """把 __aenter__ 里的连接池替换成 fakeredis 的内存 client。"""

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fake = client

    async def __aenter__(self) -> "FakeRedisStorage":
        self._client = self._fake
        return self

    async def aclose(self) -> None:
        self._client = None


class FakeRedisBus(RedisMessageBus):
    """RedisMessageBus 的 fakeredis 版本。"""

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fake = client

    async def __aenter__(self) -> "FakeRedisBus":
        self._client = self._fake
        return self

    async def aclose(self) -> None:
        self._client = None


def session_config() -> SessionConfig:
    return SessionConfig(
        workspace_id="ws-1",
        name="redis demo",
        chat_model_config=ChatModelConfig(
            type="deepseek_credential",
            credential_id="cred-1",
            model="deepseek-chat",
            parameters={},
        ),
    )


async def part_a_storage(client: Any) -> None:
    """3a: RedisStorage 的会话记录 + 消息 List。"""
    print("=" * 70)
    print("3a. RedisStorage 会话 & 消息")
    print("=" * 70)
    async with FakeRedisStorage(client) as storage:
        session = await storage.upsert_session(
            user_id="user-1",
            agent_id="agent-1",
            config=session_config(),
            state=AgentState(),
            origin=ScheduleOrigin(schedule_id="sch-1"),
        )
        print("[a1] session id =", session.id, "| origin =", session.origin.type)

        # 写一条用户消息 + 一条助手消息
        await storage.upsert_message("user-1", session.id, UserMsg(name="u", content="hi"))
        await storage.upsert_message("user-1", session.id, AssistantMsg(name="a", content="hello"))

        msgs, has_more = await storage.list_messages("user-1", session.id)
        print("[a2] list_messages ->", [(m.role, m.get_text_content()) for m in msgs], has_more)

        # 会话状态单独更新（hot path）
        state = AgentState(summary="用户打了招呼")
        await storage.update_session_state("user-1", "agent-1", session.id, state)
        record = await storage.get_session("user-1", "agent-1", session.id)
        print("[a3] 回读 summary =", record.state.summary)

        # 展示真实键空间
        keys = sorted(await client.keys("agentscope:*"))
        print("[a4] 生成的 Redis key:")
        for k in keys:
            print("      ", k)
        print("[a5] 消息 List 长度 =", await client.llen(
            storage.key_config.messages.format(user_id="user-1", session_id=session.id)
        ))

        # 按 schedule 反查会话
        by_sch = await storage.list_sessions_by_schedule("user-1", "sch-1")
        print("[a6] list_sessions_by_schedule ->", [s.id for s in by_sch])


async def part_b_eventlog(client: Any) -> None:
    """3b: 事件回放日志（Mode C replay log）—— 事件溯源的落点。"""
    print()
    print("=" * 70)
    print("3b. RedisMessageBus 事件回放日志")
    print("=" * 70)
    async with FakeRedisBus(client) as bus:
        sid = "sess-evt-1"
        key = MessageBusKeys.session_events(sid)
        print("[b1] replay log key =", key)
        print("[b2] 回放长度上限 SESSION_REPLAY_MAX_LEN =",
              MessageBusKeys.SESSION_REPLAY_MAX_LEN)

        # 模拟一次 reply 产生的事件流
        e1 = await bus.log_append(key, {"type": "REPLY_START", "reply_id": "r1"},
                                  max_len=MessageBusKeys.SESSION_REPLAY_MAX_LEN)
        e2 = await bus.log_append(key, {"type": "TEXT_BLOCK_DELTA", "delta": "你"},
                                  max_len=MessageBusKeys.SESSION_REPLAY_MAX_LEN)
        e3 = await bus.log_append(key, {"type": "TEXT_BLOCK_DELTA", "delta": "好"},
                                  max_len=MessageBusKeys.SESSION_REPLAY_MAX_LEN)
        e4 = await bus.log_append(key, {"type": "REPLY_END", "reply_id": "r1"},
                                  max_len=MessageBusKeys.SESSION_REPLAY_MAX_LEN)
        print("[b3] entry ids =", [e1, e2, e3, e4])

        # 新订阅者从头发送前回放：since=None
        replay = await bus.log_read(key, max_count=100)
        print("[b4] 全量回放 =")
        for entry_id, payload in replay:
            print("      ", entry_id, payload)

        # 断线重连：带游标 (since) 续读，只拿到新事件
        incremental = await bus.log_read(key, since=e2, max_count=100)
        print("[b5] since=e2 增量 =", [p["type"] for _, p in incremental])

        # log_trim(None) 清空整条日志 —— ChatService 每轮 reply 结束都会这么做
        await bus.log_trim(key)
        after = await bus.log_read(key, max_count=100)
        print("[b6] log_trim 后日志条数 =", len(after))


async def main() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await part_a_storage(client)
    await part_b_eventlog(client)


if __name__ == "__main__":
    asyncio.run(main())
