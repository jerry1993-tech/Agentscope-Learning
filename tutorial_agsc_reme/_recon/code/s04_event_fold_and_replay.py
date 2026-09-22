# -*- coding: utf-8 -*-
"""片段 4：AgentScope 的「事件溯源」真身 —— 流式事件 fold 成 Msg，事件日志可回放重建。

结论（源码依据见报告）：AgentScope 的 assistant 消息并不是「先有消息再有事件」，
而是**事件流 fold 出消息**（Msg.append_event，message/_base.py:244）。
会话恢复靠的是 fold 结果（state.context / messages 表），
事件流本身只活在 Redis Stream 里、且每轮 reply 结束就被 log_trim 清空。

运行:
    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
        tutorial_agsc_reme/_recon/code/s04_event_fold_and_replay.py
"""
import asyncio
from typing import Any

import fakeredis.aioredis

from agentscope.app.message_bus import MessageBusKeys, RedisMessageBus
from agentscope.app._bus_ops import publish_session_event
from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from agentscope.message import AssistantMsg
from agentscope.types import ReplyFinishedReason


class FakeRedisBus(RedisMessageBus):
    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fake = client

    async def __aenter__(self) -> "FakeRedisBus":
        self._client = self._fake
        return self

    async def aclose(self) -> None:
        self._client = None


def make_event_stream(reply_id: str, session_id: str) -> list:
    """模拟 Agent Loop 在一次 reply 中吐出的事件序列。"""
    block_id = "blk-1"
    return [
        ReplyStartEvent(session_id=session_id, reply_id=reply_id, name="demo_agent"),
        TextBlockStartEvent(reply_id=reply_id, block_id=block_id),
        TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta="你"),
        TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta="好"),
        TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta="呀"),
        TextBlockEndEvent(reply_id=reply_id, block_id=block_id),
        ReplyEndEvent(
            session_id=session_id,
            reply_id=reply_id,
            finished_reason=ReplyFinishedReason.COMPLETED,
        ),
    ]


async def main() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    session_id = "sess-fold-1"
    reply_id = "reply-1"
    events = make_event_stream(reply_id, session_id)

    async with FakeRedisBus(client) as bus:
        # ---------- 1. 一边 fold 一边广播（ChatService.run 的真实做法）----------
        persisted = AssistantMsg(id=reply_id, name="demo_agent", content=[])
        for evt in events:
            persisted.append_event(evt)
            await publish_session_event(
                bus,
                session_id,
                evt.model_dump(mode="json"),
            )

        print("[1] fold 出的消息文本 =", repr(persisted.get_text_content()))
        print("[1] finished_reason =", persisted.finished_reason)
        print("[1] content blocks =", [b.type for b in persisted.content])

        # ---------- 2. 从 Redis Stream 里读回事件日志并重新 fold ----------
        key = MessageBusKeys.session_events(session_id)
        entries = await bus.log_read(key, max_count=1000)
        print("[2] 回放日志条数 =", len(entries))
        print("[2] 事件类型序列 =", [p["type"] for _, p in entries])

        rebuilt = AssistantMsg(id=reply_id, name="demo_agent", content=[])
        for _entry_id, payload in entries:
            # payload 是 model_dump 过的 dict —— 生产代码里 SSE 直接把它丢给前端
            if payload.get("reply_id") != reply_id:
                continue
            rebuilt.append_event(_rehydrate(payload))
        print("[2] 回放 fold 出的文本 =", repr(rebuilt.get_text_content()))
        print("[2] 与原始消息一致 =", rebuilt.get_text_content()
              == persisted.get_text_content())

        # ---------- 3. 破坏性证据：log_trim 之后事件流就没了 ----------
        await bus.log_trim(key)
        print("[3] log_trim 后回放日志条数 =",
              len(await bus.log_read(key, max_count=1000)))
        print("[3] 但 fold 结果仍在内存/DB 里 =",
              repr(persisted.get_text_content()))


def _rehydrate(payload: dict):
    """把回放日志里的 dict 还原成事件对象（真实代码里 SSE 不需要这一步）。"""
    from agentscope.event import EventType

    mapping = {
        EventType.REPLY_START: ReplyStartEvent,
        EventType.TEXT_BLOCK_START: TextBlockStartEvent,
        EventType.TEXT_BLOCK_DELTA: TextBlockDeltaEvent,
        EventType.TEXT_BLOCK_END: TextBlockEndEvent,
        EventType.REPLY_END: ReplyEndEvent,
    }
    return mapping[payload["type"]].model_validate(payload)


if __name__ == "__main__":
    asyncio.run(main())
