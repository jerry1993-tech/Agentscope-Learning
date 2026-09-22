# -*- coding: utf-8 -*-
"""第 3 讲的 pytest（交付物之一）：把「消息 / 块 / 事件 / 状态 / 总线」钉成回归断言。

为什么单测要再写一遍（脚本已经跑过一遍了）：

1. **脚本是给人看的，测试是给 CI 看的**。脚本打印一大堆东西、人眼确认；
   测试只有"过/不过"，任何一步退化都会立刻红。
2. **本讲有几条"上游一升级就会悄悄失效"的假设**，最典型的是
   :func:`test_event_type_coverage_is_total`：`HANDLED ∪ IGNORED` 必须**恰好**
   等于 ``EventType`` 全集。AgentScope 哪天加了第 29 种事件，我们的审计口径
   就会出现"事件发生了、日志里没有"的黑洞 —— 这个测试就是那个报警器。
3. **seq 不变式**必须是可回归的：它是第 9 讲会话恢复的地基，
   一旦允许空洞，恢复出来的状态就会缺一段，而且**不会报错**。

用法（`tests/conftest.py` 已经把 `third_party/ReMe` 与 `reference/` 塞进
`sys.path`，所以不设 `PYTHONPATH` 也能跑）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest tests/test_lesson03_events.py -v

LLM 调用预算：**1 次**（只有一个测试真的打模型，其余全程离线）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import typing
from collections.abc import AsyncGenerator

import pytest
from agentscope.event import (
    AgentEvent,
    CustomEvent,
    EventType,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import (
    AssistantMsg,
    HintBlock,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.state import AgentState
from agentscope.types import ReplyFinishedReason
from pydantic import TypeAdapter, ValidationError

from harness_kit.events import EventBus, EventKind, EventRecord
from harness_kit.events.bus import BusClosedError
from harness_kit.events.translate import (
    HANDLED_EVENT_TYPES,
    IGNORED_EVENT_TYPES,
    MEMORY_HIT_CUSTOM_NAME,
    StreamTranslator,
)
from harness_kit.events.types import PAYLOAD_FIELDS, WILDCARD_TOPIC, topic_matches
from harness_kit.session.models import (
    INPUT_PREVIEW_LIMIT,
    SessionEvent,
    SessionInvariantError,
    SessionMeta,
    SessionSnapshot,
    check_seq_invariants,
    next_seq,
    preview,
    restore_state,
    snapshot_from_state,
    tail_events,
)

SESSION = "sess_test03"
RID = "reply_test03"
AGENT_NAME = "Friday"
TOOL_INPUT = '{"city": "Beijing"}'


def tool_result_input_digest() -> str:
    """入参摘要的期望值（与 ``translate._digest`` 同口径）。

    Returns:
        `str`: ``sha1(TOOL_INPUT)[:16]``。
    """
    return hashlib.sha1(TOOL_INPUT.encode("utf-8")).hexdigest()[:16]


class Recorder:
    """把事件记录收进列表的订阅者（可选抛异常 / 变慢）。"""

    def __init__(self, *, boom: bool = False, delay: float = 0.0) -> None:
        """初始化。

        Args:
            boom (`bool`): 是否在每条事件上抛异常。
            delay (`float`): 每条事件的处理耗时。
        """
        self.seen: list[EventRecord] = []
        self.boom = boom
        self.delay = delay

    async def __call__(self, record: EventRecord) -> None:
        """处理一条事件。

        Args:
            record (`EventRecord`): 事件记录。

        Raises:
            RuntimeError: ``boom=True`` 时抛出。
        """
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("订阅者坏了（这是故意的）")
        self.seen.append(record)


def record(kind: EventKind, seq: int) -> EventRecord:
    """造一条最小可用的记录。

    Args:
        kind (`EventKind`): 事件种类。
        seq (`int`): 序号。

    Returns:
        `EventRecord`: 记录（payload 补齐契约必备字段，避免 warning）。
    """
    payload = {name: None for name in PAYLOAD_FIELDS.get(kind, ())}
    payload["data"] = {}
    return EventRecord(session_id=SESSION, seq=seq, kind=kind, payload=payload)


# ======================================================================
# A. 块与两条状态轴
# ======================================================================
def test_two_orthogonal_state_axes() -> None:
    """``ToolCallState``（流程）与 ``ToolResultState``（结果）互不推导。"""
    assert [s.value for s in ToolCallState] == [
        "pending",
        "asking",
        "allowed",
        "submitted",
        "finished",
    ]
    assert [s.value for s in ToolResultState] == [
        "success",
        "error",
        "interrupted",
        "denied",
        "running",
    ]
    # 流程已经走到终点，结果依然可以是错
    call = ToolCallBlock(
        id="tc",
        name="boom",
        input="{}",
        state=ToolCallState.FINISHED,
    )
    result = ToolResultBlock(
        id="tc",
        name="boom",
        output="err",
        state=ToolResultState.ERROR,
    )
    assert call.state == "finished" and result.state == "error"


def test_block_state_is_bare_string() -> None:
    """``use_enum_values=True`` 让 ``block.state`` 是裸字符串（``==`` 仍成立）。"""
    call = ToolCallBlock(id="tc", name="t", input="{}", state=ToolCallState.ASKING)
    assert isinstance(call.state, str)
    assert not isinstance(call.state, ToolCallState)
    assert call.state == ToolCallState.ASKING == "asking"


def test_user_message_rejects_tool_result() -> None:
    """``UserMsg`` 只接受 text / data 块；tool_result 必须待在 assistant 消息里。"""
    with pytest.raises(ValidationError):
        UserMsg(
            name="user",
            content=[ToolResultBlock(id="tc", name="t", output="x")],
        )


def test_get_text_content_returns_none_without_text() -> None:
    """没有文本块时 ``get_text_content()`` 返回 ``None``（不是空串）。"""
    msg = AssistantMsg(name="a", content=[HintBlock(hint="x")])
    assert msg.get_text_content() is None


# ======================================================================
# B. 事件 → 消息的折叠
# ======================================================================
def synthetic_events() -> list[object]:
    """一条「思考 → 工具调用 → 工具结果 → 文本」的最小事件序列。

    Returns:
        `list[object]`: 事件对象列表。
    """
    return [
        ReplyStartEvent(session_id=SESSION, reply_id=RID, name=AGENT_NAME),
        ModelCallStartEvent(reply_id=RID, model_name="deepseek-flash"),
        ModelCallEndEvent(reply_id=RID, input_tokens=10, output_tokens=2),
        ToolCallStartEvent(reply_id=RID, tool_call_id="tc1", tool_call_name="get_time"),
        ToolCallDeltaEvent(reply_id=RID, tool_call_id="tc1", delta=TOOL_INPUT),
        ToolCallEndEvent(reply_id=RID, tool_call_id="tc1"),
        ToolResultStartEvent(reply_id=RID, tool_call_id="tc1", tool_call_name="get_time"),
        ToolResultTextDeltaEvent(reply_id=RID, tool_call_id="tc1", delta="16:00"),
        ToolResultEndEvent(
            reply_id=RID,
            tool_call_id="tc1",
            state=ToolResultState.SUCCESS,
        ),
        TextBlockStartEvent(reply_id=RID, block_id="b1"),
        TextBlockDeltaEvent(reply_id=RID, block_id="b1", delta="查到了。"),
        TextBlockEndEvent(reply_id=RID, block_id="b1"),
        ReplyEndEvent(
            session_id=SESSION,
            reply_id=RID,
            finished_reason=ReplyFinishedReason.COMPLETED,
        ),
    ]


def test_append_event_folds_stream_into_one_message() -> None:
    """``Msg.append_event`` 把事件流折叠成一条 assistant 消息。"""
    msg = Msg(id=RID, name=AGENT_NAME, role="assistant", content=[])
    for event in synthetic_events():
        msg.append_event(event)

    assert [b.type for b in msg.content] == ["tool_call", "tool_result", "text"]
    call = msg.get_content_blocks("tool_call")[0]
    result = msg.get_content_blocks("tool_result")[0]
    # TOOL_RESULT_END 会把配对的 ToolCallBlock 一并关掉
    assert call.state == ToolCallState.FINISHED
    assert call.input == TOOL_INPUT
    assert result.state == ToolResultState.SUCCESS
    # 逐字增量折叠进 output 的 TextBlock 里（不是裸字符串）
    assert [block.text for block in result.output] == ["16:00"]
    assert msg.finished_reason == ReplyFinishedReason.COMPLETED
    assert msg.usage is not None and msg.usage.input_tokens == 10


def test_append_event_accumulates_usage() -> None:
    """多次 ``MODEL_CALL_END`` 累加到同一份 ``usage``（一条 reply 一次汇总）。"""
    msg = Msg(id=RID, name=AGENT_NAME, role="assistant", content=[])
    msg.append_event(ModelCallEndEvent(reply_id=RID, input_tokens=100, output_tokens=1))
    msg.append_event(ModelCallEndEvent(reply_id=RID, input_tokens=200, output_tokens=2))
    assert msg.usage is not None
    assert msg.usage.input_tokens == 300
    assert msg.usage.output_tokens == 3


def test_append_event_skips_foreign_reply_id() -> None:
    """``reply_id`` 对不上的事件被跳过（不会串到别的 reply 里）。"""
    msg = Msg(id=RID, name=AGENT_NAME, role="assistant", content=[])
    msg.append_event(TextBlockStartEvent(reply_id="other", block_id="b1"))
    msg.append_event(TextBlockDeltaEvent(reply_id="other", block_id="b1", delta="x"))
    assert msg.content == []


def test_custom_event_has_no_reply_id_so_cannot_fold() -> None:
    """``CustomEvent`` 没有 ``reply_id`` 字段，``append_event`` 会 ``AttributeError``。

    这是"事件流 ≠ 消息流"的一个硬证据：``CustomEvent`` 是服务层信号，
    不该被折叠进对话上下文。
    """
    event = CustomEvent(reply_id=RID, name="x", value={"a": 1})  # reply_id 被 pydantic 忽略
    assert not hasattr(event, "reply_id")
    msg = Msg(id=RID, name=AGENT_NAME, role="assistant", content=[])
    with pytest.raises(AttributeError):
        msg.append_event(event)


# ======================================================================
# C. 事件 serde 与口径盘点
# ======================================================================
def test_agent_event_roundtrip_needs_type_adapter() -> None:
    """``AgentEvent`` 是 TypeAlias，不能 ``model_validate``，必须 ``TypeAdapter``。"""
    adapter: TypeAdapter = TypeAdapter(AgentEvent)
    source = TextBlockDeltaEvent(reply_id=RID, block_id="b1", delta="hi")
    dumped = source.model_dump(mode="json")
    assert dumped["type"] == "TEXT_BLOCK_DELTA"
    assert isinstance(dumped["type"], str)
    assert adapter.validate_python(dumped) == source
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "NO_SUCH_EVENT"})


def test_event_type_coverage_is_total() -> None:
    """审计口径的两种集合必须**恰好**覆盖 ``EventType`` 全集（28 = 12 + 16）。

    这是给"AgentScope 升级加了新事件"准备的报警器：一旦不成立，
    就说明有新事件从我们的日志口径里漏掉了。
    """
    all_types = {item.value for item in EventType}
    handled = set(HANDLED_EVENT_TYPES)
    ignored = set(IGNORED_EVENT_TYPES)
    assert not (handled & ignored)
    assert handled | ignored == all_types
    assert len(all_types) == 28
    assert len(typing.get_args(AgentEvent)) == 28
    assert len(handled) == 12 and len(ignored) == 16


def test_payload_contract_covers_all_kinds() -> None:
    """9 种 ``EventKind`` 都必须有 payload 必备字段表。"""
    assert set(PAYLOAD_FIELDS) == set(EventKind)
    for kind, fields in PAYLOAD_FIELDS.items():
        assert fields, f"{kind} 的必备字段表不能为空"


def test_topic_matching() -> None:
    """``*`` 命中全部；其余按相等匹配；字符串与枚举等价。"""
    assert topic_matches(WILDCARD_TOPIC, EventKind.CUSTOM)
    assert topic_matches("tool_call", EventKind.TOOL_CALL)
    assert topic_matches(EventKind.TOOL_CALL, EventKind.TOOL_CALL)
    assert not topic_matches("tool_call", EventKind.MEMORY_HIT)


# ======================================================================
# D. AgentState
# ======================================================================
def test_append_context_merges_within_one_reply() -> None:
    """同一次 reply 内多次 ``append_context`` 合并成一条消息（按 id 去重）。"""
    state = AgentState(session_id=SESSION)
    state.reply_id = RID
    state.append_context(AGENT_NAME, [TextBlock(text="A")])
    state.append_context(AGENT_NAME, [TextBlock(text="B")])
    assert len(state.context) == 1
    assert state.context[-1].id == RID
    # 块之间用换行拼接（不是直接相接）——这是 AgentScope 的既定行为
    assert state.context[-1].get_text_content() == "A\nB"


def test_agent_state_roundtrip_is_lossless() -> None:
    """``AgentState`` 是唯一持久化边界：JSON 往返必须无损。"""
    state = AgentState(session_id=SESSION)
    state.reply_id = RID
    state.append_context(AGENT_NAME, [TextBlock(text="hi")])
    again = AgentState.model_validate(state.model_dump(mode="json"))
    assert again == state
    assert again.session_id == SESSION


def test_has_awaiting_tool_calls_is_derived_not_stored() -> None:
    """HITL 断点从 ``context`` 尾部推导，没有独立的 flag 字段。"""
    state = AgentState(session_id=SESSION)
    assert not state.has_awaiting_tool_calls(AGENT_NAME)
    state.reply_id = RID
    state.append_context(
        AGENT_NAME,
        [
            ToolCallBlock(
                id="tc1",
                name="delete_file",
                input='{"path": "/tmp/x"}',
                state=ToolCallState.ASKING,
            ),
        ],
    )
    assert state.has_awaiting_tool_calls(AGENT_NAME)
    assert [b.id for b in state.get_unfinished_tool_calls(AGENT_NAME)] == ["tc1"]


def test_legacy_state_payload_migrates() -> None:
    """旧格式（少字段）的 state JSON 仍然能被 ``model_validate`` 接住。"""
    legacy = AgentState.model_validate(
        {"session_id": SESSION, "reply_id": "r-old", "cur_iter": 3},
    )
    assert legacy.reply_id == "r-old"
    assert legacy.cur_iter == 3


def test_snapshot_roundtrip_and_invariant() -> None:
    """``SessionSnapshot`` 校验 ``agent_state`` 带 ``session_id``。"""
    state = AgentState(session_id=SESSION)
    snapshot = snapshot_from_state(state, seq=4)
    assert snapshot.seq == 4 and not snapshot.is_empty
    assert restore_state(snapshot) == state
    with pytest.raises(ValidationError):
        SessionSnapshot(session_id=SESSION, seq=0, agent_state={"context": []})
    assert SessionSnapshot(session_id=SESSION, seq=-1, agent_state={"session_id": SESSION}).is_empty


# ======================================================================
# E. EventBus
# ======================================================================
async def test_bus_delivers_to_matching_subscribers_only() -> None:
    """过滤按 kind 生效；``*`` 收全部；返回值为投递数。"""
    bus = EventBus()
    everything, only_tools = Recorder(), Recorder()
    bus.subscribe(WILDCARD_TOPIC, everything)
    bus.subscribe(EventKind.TOOL_CALL, only_tools)
    await bus.start()
    assert await bus.publish(EventKind.TOOL_CALL, record(EventKind.TOOL_CALL, 0)) == 2
    assert await bus.publish(EventKind.MODEL_CALL, record(EventKind.MODEL_CALL, 1)) == 1
    await bus.drain()
    assert [r.kind for r in everything.seen] == [
        EventKind.TOOL_CALL,
        EventKind.MODEL_CALL,
    ]
    assert [r.kind for r in only_tools.seen] == [EventKind.TOOL_CALL]
    await bus.aclose()


async def test_bus_swallows_subscriber_exception() -> None:
    """坏订阅者被吞掉并计数，不影响其它订阅者、不影响发布方。"""
    bus = EventBus()
    good, broken = Recorder(), Recorder(boom=True)
    bus.subscribe(WILDCARD_TOPIC, good)
    bus.subscribe(EventKind.TOOL_CALL, broken)
    await bus.start()
    await bus.publish(EventKind.TOOL_CALL, record(EventKind.TOOL_CALL, 0))
    await bus.drain()
    assert bus.errors == 1
    assert len(good.seen) == 1
    assert "RuntimeError" in bus.error_samples[0]
    await bus.aclose()


async def test_bus_drops_when_queue_full() -> None:
    """队列满时丢**最新**并计入 dropped（观测旁路不该阻塞主流程）。"""
    bus = EventBus(max_queue=1)
    slow = Recorder(delay=0.05)
    bus.subscribe(WILDCARD_TOPIC, slow)
    await bus.start()
    assert await bus.publish(EventKind.CUSTOM, record(EventKind.CUSTOM, 0)) == 1
    assert await bus.publish(EventKind.CUSTOM, record(EventKind.CUSTOM, 1)) == 0
    assert bus.dropped == 1
    assert bus.stats()["errors"] == 0
    await bus.aclose()


async def test_bus_rejects_publish_after_close() -> None:
    """关闭后 ``publish`` / ``subscribe`` 一律抛 ``BusClosedError``。"""
    bus = EventBus()
    await bus.start()
    await bus.aclose()
    with pytest.raises(BusClosedError):
        await bus.publish(EventKind.CUSTOM, record(EventKind.CUSTOM, 0))
    with pytest.raises(BusClosedError):
        bus.subscribe(WILDCARD_TOPIC, Recorder())
    await bus.aclose()  # 幂等


async def test_bus_reclaims_worker_of_unsubscribed_handler() -> None:
    """注销后 worker 由 ``aclose`` 回收（否则事件循环关闭时报孤儿任务）。"""
    bus = EventBus()
    subscription = bus.subscribe(WILDCARD_TOPIC, Recorder())
    await bus.start()
    subscription.unsubscribe()
    assert await bus.publish(EventKind.CUSTOM, record(EventKind.CUSTOM, 0)) == 0
    await bus.aclose()
    assert subscription._task is not None and subscription._task.done()


async def test_bus_max_queue_must_be_positive() -> None:
    """``max_queue`` 小于 1 直接拒绝（配置错误要在构造期暴露）。"""
    with pytest.raises(ValueError):
        EventBus(max_queue=0)


# ======================================================================
# F. StreamTranslator
# ======================================================================
async def synthetic_stream() -> AsyncGenerator[object, None]:
    """把 :func:`synthetic_events` 的序列包成异步流。

    Yields:
        `object`: 事件对象。
    """
    for event in synthetic_events():
        yield event


async def test_translator_produces_hole_free_records() -> None:
    """翻译器产出的记录：seq 从 0 起无洞、payload 合契约、kind 顺序正确。"""
    bus = EventBus()
    sink = Recorder()
    bus.subscribe(WILDCARD_TOPIC, sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)
    translator.note_input("北京现在几点？")
    produced = await translator.consume(synthetic_stream())
    await bus.drain()

    assert produced == len(sink.seen)
    assert [r.seq for r in sink.seen] == list(range(len(sink.seen)))
    for item in sink.seen:
        assert item.missing_payload_fields() == []
        assert item.session_id == SESSION
    assert [r.kind for r in sink.seen] == [
        EventKind.REPLY_START,
        EventKind.MODEL_CALL,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.REPLY_END,
    ]
    assert sink.seen[0].payload["input_preview"] == "北京现在几点？"
    assert sink.seen[2].payload["tool_input_digest"] == tool_result_input_digest()
    assert sink.seen[3].payload["state"] == "success"
    assert sink.seen[3].payload["error"] is None
    assert sink.seen[4].payload["iterations"] == 1
    assert sink.seen[4].payload["tool_calls"] == 1
    await bus.aclose()


async def test_translator_skips_deltas_without_consuming_seq() -> None:
    """被跳过的事件不占 seq —— 否则落盘产物就有洞。"""
    bus = EventBus()
    sink = Recorder()
    bus.subscribe(WILDCARD_TOPIC, sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)
    await translator.consume(synthetic_stream())
    await bus.drain()
    assert translator.records_seen == 13
    assert translator.skipped == 8
    assert translator.next_seq == len(sink.seen)
    await bus.aclose()


async def test_translator_truncates_input_preview() -> None:
    """``input_preview`` 截断到 500 字符 + ``...``。"""
    bus = EventBus()
    sink = Recorder()
    bus.subscribe(WILDCARD_TOPIC, sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)
    translator.note_input("长" * (INPUT_PREVIEW_LIMIT + 10))
    await translator.consume(synthetic_stream())
    await bus.drain()
    preview_text = sink.seen[0].payload["input_preview"]
    assert len(preview_text) == INPUT_PREVIEW_LIMIT + 3
    assert preview_text.endswith("...")
    await bus.aclose()


async def test_translator_requires_session_id() -> None:
    """空 ``session_id`` 直接拒绝。"""
    with pytest.raises(ValueError):
        StreamTranslator(EventBus(), session_id="")


async def test_translator_seek_cannot_go_backwards() -> None:
    """``seek`` 只能向前，回退会让新事件与老事件重号。"""
    translator = StreamTranslator(EventBus(), session_id=SESSION)
    translator.seek(10)
    assert translator.next_seq == 10
    with pytest.raises(ValueError):
        translator.seek(3)


async def test_translator_maps_memory_hit_custom_event() -> None:
    """``CustomEvent(name="memory_hit")`` 走 :data:`MEMORY_HIT_CUSTOM_NAME` 约定。"""
    bus = EventBus()
    sink = Recorder()
    bus.subscribe(EventKind.MEMORY_HIT, sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)
    await translator.consume(
        _one_event_stream(
            CustomEvent(
                reply_id=RID,
                name=MEMORY_HIT_CUSTOM_NAME,
                value={"query": "q", "chunk_ids": ["a"], "kept": ["a"], "tokens": 7},
            ),
        ),
    )
    await bus.drain()
    assert len(sink.seen) == 1
    assert sink.seen[0].kind == EventKind.MEMORY_HIT
    assert sink.seen[0].payload["tokens"] == 7
    await bus.aclose()


async def test_note_confirmation_fills_the_audit_gap() -> None:
    """HITL 的"批准"不在事件流里，由调用方补记录；关闭的总线上补不上，也不抛。"""
    bus = EventBus()
    sink = Recorder()
    bus.subscribe(WILDCARD_TOPIC, sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)
    await translator.note_confirmation(tool_names=["get_time"], confirmed=True)
    await bus.drain()
    assert [r.payload["behavior"] for r in sink.seen] == ["allow"]
    assert sink.seen[0].kind == EventKind.PERMISSION
    await bus.aclose()
    # 关闭之后：返回 None，且**不占号**（seq 不出现空洞）
    before = translator.next_seq
    assert await translator.note_confirmation(tool_names=["get_time"], confirmed=False) is None
    assert translator.next_seq == before
    assert translator.errors == 1


async def test_translator_rolls_back_seq_on_publish_failure() -> None:
    """投递失败要退回已取的号：落盘产物里不允许有洞。"""
    bus = EventBus()
    translator = StreamTranslator(bus, session_id=SESSION)  # 没有订阅者，也没 start
    await bus.aclose()
    assert await translator.consume(synthetic_stream()) == 0
    assert translator.errors > 0
    assert translator.next_seq == 0


async def test_sse_frame_is_stateless_and_passes_deltas_through() -> None:
    """``to_sse_frame`` 是静态方法：原样透传每条事件，``Msg`` 返回 ``None``。"""
    event = TextBlockDeltaEvent(reply_id=RID, block_id="b1", delta="你")
    frame = StreamTranslator.to_sse_frame(event)
    assert frame is not None
    assert frame["event"] == "TEXT_BLOCK_DELTA"
    assert frame["data"]["delta"] == "你"
    assert StreamTranslator.to_sse_frame(UserMsg(name="user", content="hi")) is None


def _one_event_stream(event: object) -> AsyncGenerator[object, None]:
    """把单个事件包成异步流。

    Args:
        event (`object`): 事件对象。

    Returns:
        `AsyncGenerator[object, None]`: 只 yield 一次。
    """

    async def _gen() -> AsyncGenerator[object, None]:
        yield event

    return _gen()


# ======================================================================
# G. session/models
# ======================================================================
def test_check_seq_invariants_rejects_gap_and_duplicate() -> None:
    """seq 必须从 0 起、严格递增、无洞。"""
    events = [SessionEvent.wrap(record(EventKind.CUSTOM, i)) for i in range(3)]
    check_seq_invariants(events, expected_session_id=SESSION)
    with pytest.raises(SessionInvariantError):
        check_seq_invariants(events[:2] + events[2:], expected_session_id="other")
    broken = [events[0], SessionEvent.wrap(record(EventKind.CUSTOM, 5))]
    with pytest.raises(SessionInvariantError):
        check_seq_invariants(broken)


def test_event_record_is_frozen() -> None:
    """``EventRecord`` 一经 append 永不修改（``frozen=True``）。"""
    item = record(EventKind.CUSTOM, 0)
    with pytest.raises(ValidationError):
        item.seq = 9  # type: ignore[misc]


def test_session_event_jsonl_roundtrip() -> None:
    """落盘形态 ``SessionEvent`` 的单行 JSON 往返无损。"""
    events = [SessionEvent.wrap(record(EventKind.CUSTOM, i)) for i in range(2)]
    lines = [item.to_json_line() for item in events]
    assert all("\n" not in line for line in lines)
    back = [SessionEvent.model_validate(json.loads(line)) for line in lines]
    assert back == events
    assert back[1].seq == 1 and back[1].kind == EventKind.CUSTOM


def test_session_meta_rejects_reversed_time_and_naive_datetime() -> None:
    """卡片校验：``updated_at >= created_at`` 且必须 tz-aware。"""
    from datetime import datetime, timedelta, timezone

    meta = SessionMeta.create(SESSION, profile_name="default")
    assert meta.event_count == 0
    touched = meta.touched(event_count=3)
    assert touched.event_count == 3 and touched.updated_at >= meta.updated_at
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        SessionMeta(
            session_id=SESSION,
            profile_name="default",
            created_at=now + timedelta(hours=1),
            updated_at=now,
        )
    with pytest.raises(ValidationError):
        SessionMeta(
            session_id=SESSION,
            profile_name="default",
            created_at=datetime(2026, 1, 1),
            updated_at=now,
        )


def test_next_seq_and_tail_events() -> None:
    """``next_seq`` / ``tail_events`` 是会话恢复的两个坐标。"""
    events = [SessionEvent.wrap(record(EventKind.CUSTOM, i)) for i in range(4)]
    assert next_seq(events) == 4
    assert next_seq([]) == 0
    assert [item.seq for item in tail_events(events, after_seq=1)] == [2, 3]
    assert preview("a" * 600) == "a" * INPUT_PREVIEW_LIMIT + "..."


# ======================================================================
# H. 真实 Agent（1 次 LLM 调用；没有 key 就跳过）
# ======================================================================
async def test_real_agent_parks_on_permission_then_records(llm_env: dict[str, str]) -> None:
    """真模型 + 真 Toolkit：第一次 ``reply_stream`` 停在待确认，且事件被记下来。

    这个测试同时钉住两条**实测事实**：

    1. ``FunctionTool`` 默认权限是 ``ASK``（``tool/_adapters.py:132``），
       所以流会 park 在 ``RequireUserConfirmEvent``，**不会**有 ``REPLY_END``；
    2. HITL 的确认结果是**输入**，事件流里只有 ``ask`` 没有 ``allow`` ——
       所以"批准"那条审计记录必须由调用方补（``note_confirmation``）。
    """
    from agentscope.agent import Agent
    from agentscope.credential import DeepSeekCredential
    from agentscope.model import DeepSeekChatModel
    from agentscope.tool import FunctionTool, ToolChunk, Toolkit

    def get_time(city: str) -> ToolChunk:
        """查询指定城市的当前时间。

        Args:
            city (`str`): 城市名。

        Returns:
            `ToolChunk`: 一句话说明时间。
        """
        return ToolChunk(
            content=[TextBlock(text=f"{city} 当前时间为 2026-09-22 09:30。")],
            state="success",
        )

    agent = Agent(
        name="Friday",
        system_prompt="用户问时间时必须调用 get_time 工具，回答保持一句话。",
        model=DeepSeekChatModel(
            credential=DeepSeekCredential(
                api_key=llm_env["api_key"],
                base_url=llm_env["base_url"],
            ),
            model=llm_env["model"],
            stream=True,
            client_kwargs={"timeout": 60.0},
        ),
        toolkit=Toolkit(tools=[FunctionTool(get_time)]),
    )
    agent.state.session_id = SESSION

    bus = EventBus()
    sink = Recorder()
    bus.subscribe(WILDCARD_TOPIC, sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)
    translator.note_input("现在北京几点？请调用 get_time 工具。")
    await asyncio.wait_for(
        translator.consume(
            agent.reply_stream(UserMsg("user", "现在北京几点？请调用 get_time 工具。")),
        ),
        timeout=180,
    )
    await bus.drain()

    kinds = [r.kind for r in sink.seen]
    assert EventKind.REPLY_START in kinds
    assert EventKind.TOOL_CALL in kinds
    assert EventKind.MODEL_CALL in kinds
    assert EventKind.PERMISSION in kinds, "默认权限是 ASK，必须出现 permission 记录"
    assert EventKind.REPLY_END not in kinds, "park 住的流不该有 reply_end"
    assert agent.state.has_awaiting_tool_calls(agent.name)
    assert [r.seq for r in sink.seen] == list(range(len(sink.seen)))
    assert translator.skipped > 0, "逐字增量不落审计记录"

    # 补上"用户批准"这一条：这就是 note_confirmation 存在的理由。
    # 必须在 aclose 之前 —— 关掉的总线上投不进去（会返回 None 并计入 errors）。
    assert await translator.note_confirmation(
        tool_names=[agent.state.get_unfinished_tool_calls(agent.name)[0].name],
        confirmed=True,
    ) is not None
    await bus.drain()
    assert [r.payload["behavior"] for r in sink.seen if r.kind == EventKind.PERMISSION] == [
        "ask",
        "allow",
    ]
    await bus.aclose()
