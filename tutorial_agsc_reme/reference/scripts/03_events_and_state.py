# -*- coding: utf-8 -*-
"""第 3 讲验证脚本之一：消息 / 块 / 事件 / 状态 / 事件总线，**全程 0 次 LLM 调用**。

它把本讲的八条主结论全部变成可执行的断言：

  A. 六种内容块 + ``ToolCallState`` / ``ToolResultState`` 两条正交的状态轴
  B. ``Msg.append_event`` 把事件流折叠成一条消息（4 条易错行为）
  C. 事件的序列化 / 反序列化（``AgentEvent`` 是 TypeAlias，必须 ``TypeAdapter``）
     与"28 种事件我们的日志口径覆盖了几种"的盘点
  D. ``AgentState``：字段、``append_context`` 的"一次 reply = 一条消息"、
     序列化往返、HITL 断点判定、旧格式迁移
  E. ``EventBus``：订阅过滤、投递计数、订阅者异常被吞掉、队列满丢弃、关闭后拒收
  F. ``StreamTranslator``：合成事件流 → ``EventRecord`` 流（seq 无洞、payload 合契约）
  G. ``session/models``：落盘形态与 seq 不变式（含违约必抛）
  H. 把事件记录真的写成一个 JSONL 文件再读回来

用法（``PYTHONPATH`` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/03_events_and_state.py

LLM 调用预算：**0 次**。所有事件都是手工构造的（构造点与真实主循环一致：
``third_party/agentscope/src/agentscope/agent/_agent.py`` 里 yield 的就是这些类）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import typing
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import harness_kit
from loguru import logger

# 从 import 到的包反推路径，这样脚本放在任何地方都能跑：
#   <repo>/tutorial_agsc_reme/reference/harness_kit/__init__.py
REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent
sys.path.insert(0, str(REF))

from agentscope.event import (  # noqa: E402
    AgentEvent,
    ConfirmResult,
    CustomEvent,
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventType,
    HintBlockEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from agentscope.message import (  # noqa: E402
    AssistantMsg,
    Base64Source,
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.state import AgentState  # noqa: E402
from agentscope.types import ReplyFinishedReason  # noqa: E402
from pydantic import TypeAdapter, ValidationError  # noqa: E402

from harness_kit.events import EventBus, EventKind, EventRecord  # noqa: E402
from harness_kit.events.translate import (  # noqa: E402
    HANDLED_EVENT_TYPES,
    IGNORED_EVENT_TYPES,
    MEMORY_HIT_CUSTOM_NAME,
    StreamTranslator,
)
from harness_kit.events.types import PAYLOAD_FIELDS  # noqa: E402
from harness_kit.session.models import (  # noqa: E402
    SessionEvent,
    SessionInvariantError,
    SessionMeta,
    SessionSnapshot,
    check_seq_invariants,
    next_seq,
    preview,
    restore_state,
    snapshot_from_state,
)

SESSION = "sess_lesson03"
RID = "reply_lesson03"
AGENT_NAME = "Friday"
TMP = Path(tempfile.mkdtemp(prefix="harness03_"))

#: 合成事件流里收集到的真实事件对象（供 B 段折叠使用）。
COLLECTED: list[object] = []

#: 合成流的"剧本"：``(事件工厂, yield 之后的停顿秒数)``。
#: **事件在 yield 的那一刻才构造** —— 因为 ``EventBase.created_at`` 是构造时
#: 取的（``event/_event.py:77``），提前构造会让所有时间戳挤在一起，
#: ``MODEL_CALL`` 的 latency_ms 就永远是 0。
SCRIPT: list[tuple[str, Callable[[], object], float]] = [
    ("reply_start", lambda: ReplyStartEvent(session_id=SESSION, reply_id=RID, name=AGENT_NAME), 0.0),
    ("model_call_start", lambda: ModelCallStartEvent(reply_id=RID, model_name="deepseek-flash"), 0.02),
    (
        "model_call_end",
        lambda: ModelCallEndEvent(reply_id=RID, input_tokens=786, output_tokens=59),
        0.0,
    ),
    ("hint_block", lambda: HintBlockEvent(reply_id=RID, block_id="b_hint", hint="<system-reminder>now</system-reminder>"), 0.0),
    ("thinking_start", lambda: ThinkingBlockStartEvent(reply_id=RID, block_id="b_think"), 0.0),
    ("thinking_delta", lambda: ThinkingBlockDeltaEvent(reply_id=RID, block_id="b_think", delta="先查时间"), 0.0),
    ("thinking_end", lambda: ThinkingBlockEndEvent(reply_id=RID, block_id="b_think"), 0.0),
    ("text_start", lambda: TextBlockStartEvent(reply_id=RID, block_id="b_text"), 0.0),
    ("text_delta", lambda: TextBlockDeltaEvent(reply_id=RID, block_id="b_text", delta="我查一下。"), 0.0),
    ("text_end", lambda: TextBlockEndEvent(reply_id=RID, block_id="b_text"), 0.0),
    ("tool_call_start", lambda: ToolCallStartEvent(reply_id=RID, tool_call_id="tc1", tool_call_name="get_time"), 0.0),
    ("tool_call_delta", lambda: ToolCallDeltaEvent(reply_id=RID, tool_call_id="tc1", delta='{"city":'), 0.0),
    ("tool_call_delta", lambda: ToolCallDeltaEvent(reply_id=RID, tool_call_id="tc1", delta=' "Beijing"}'), 0.0),
    ("tool_call_end", lambda: ToolCallEndEvent(reply_id=RID, tool_call_id="tc1"), 0.0),
    (
        "require_user_confirm",
        lambda: RequireUserConfirmEvent(
            reply_id=RID,
            tool_calls=[ToolCallBlock(id="tc1", name="get_time", input='{"city": "Beijing"}')],
        ),
        0.0,
    ),
    (
        "user_confirm_result",
        lambda: UserConfirmResultEvent(
            reply_id=RID,
            confirm_results=[
                ConfirmResult(
                    confirmed=True,
                    tool_call=ToolCallBlock(id="tc1", name="get_time", input='{"city": "Beijing"}'),
                ),
            ],
        ),
        0.0,
    ),
    (
        "tool_result_start",
        lambda: ToolResultStartEvent(reply_id=RID, tool_call_id="tc1", tool_call_name="get_time"),
        0.0,
    ),
    (
        "tool_result_text_delta",
        lambda: ToolResultTextDeltaEvent(reply_id=RID, tool_call_id="tc1", delta="2026-09-21 16:00"),
        0.0,
    ),
    (
        "tool_result_end",
        lambda: ToolResultEndEvent(
            reply_id=RID,
            tool_call_id="tc1",
            state=ToolResultState.SUCCESS,
            metadata={"elapsed_ms": 8},
        ),
        0.0,
    ),
    ("data_start", lambda: DataBlockStartEvent(reply_id=RID, block_id="b_data", media_type="image/png"), 0.0),
    ("data_delta", lambda: DataBlockDeltaEvent(reply_id=RID, block_id="b_data", media_type="image/png", data="aGVs"), 0.0),
    ("data_delta", lambda: DataBlockDeltaEvent(reply_id=RID, block_id="b_data", media_type="image/png", data="bG8="), 0.0),
    ("data_end", lambda: DataBlockEndEvent(reply_id=RID, block_id="b_data"), 0.0),
    (
        "custom_memory_hit",
        lambda: CustomEvent(
            reply_id=RID,
            name=MEMORY_HIT_CUSTOM_NAME,
            value={"query": "北京 时间", "chunk_ids": ["a1b2"], "kept": ["a1b2"], "tokens": 42},
        ),
        0.0,
    ),
    (
        "reply_end",
        lambda: ReplyEndEvent(
            session_id=SESSION,
            reply_id=RID,
            finished_reason=ReplyFinishedReason.COMPLETED,
        ),
        0.0,
    ),
]

TOOL_INPUT = '{"city": "Beijing"}'
TOOL_INPUT_DIGEST = hashlib.sha1(TOOL_INPUT.encode()).hexdigest()[:16]


def banner(text: str) -> None:
    """打印一段分节标题。

    Args:
        text (`str`): 标题文本。
    """
    print(f"\n===== {text} =====")


async def synthetic_stream() -> AsyncGenerator[object, None]:
    """产出与真实 reply 同形的事件流（构造即 yield，时间戳才是真的）。

    Yields:
        `object`: 一个 ``AgentEvent``。
    """
    for _name, factory, pause in SCRIPT:
        event = factory()
        COLLECTED.append(event)
        yield event
        if pause:
            await asyncio.sleep(pause)


# ----------------------------------------------------------------------
# A. 六种块与两条状态轴
# ----------------------------------------------------------------------
def section_a() -> None:
    banner("A. 六种内容块 + ToolCallState / ToolResultState")

    blocks = [
        TextBlock(text="纯文本"),
        ThinkingBlock(thinking="思维链"),
        DataBlock(source=Base64Source(data="aGVsbG8=", media_type="image/png")),
        HintBlock(hint="系统注入", source='{"label": "System"}'),
        ToolCallBlock(id="tc_a", name="get_time", input='{"city": "Beijing"}', state=ToolCallState.ALLOWED),
        ToolResultBlock(id="tc_a", name="get_time", output="16:00", state=ToolResultState.SUCCESS),
    ]
    print("块类型序列:", [b.type for b in blocks])

    print("ToolCallState      :", [s.value for s in ToolCallState])
    print("ToolResultState    :", [s.value for s in ToolResultState])

    # 两条正交轴的证据：流程走到终态，结果仍然可以对错
    done_but_failed = ToolResultBlock(id="tc_x", name="boom", output="err", state=ToolResultState.ERROR)
    call_closed = ToolCallBlock(id="tc_x", name="boom", input="{}", state=ToolCallState.FINISHED)
    print("流程态 =", call_closed.state, "| 结果态 =", done_but_failed.state)
    assert call_closed.state == ToolCallState.FINISHED
    assert done_but_failed.state == ToolResultState.ERROR

    # use_enum_values=True：落下来是裸字符串（_block.py:141 / :198）
    print("block.state 的真实类型:", type(call_closed.state).__name__, repr(call_closed.state))
    assert isinstance(call_closed.state, str)
    assert call_closed.state == "finished" and call_closed.state == ToolCallState.FINISHED

    # ThinkingBlock 的 extra="allow"：厂商私有字段透传
    tb = ThinkingBlock(thinking="", signature="abc123")
    assert ThinkingBlock.model_validate(tb.model_dump()).signature == "abc123"
    print("ThinkingBlock 透传私有字段 signature:", ThinkingBlock.model_validate(tb.model_dump()).signature)

    # 角色-内容校验：tool_result 不能进 user 消息（_base.py:33-39）
    try:
        UserMsg(name="user", content=[ToolResultBlock(id="tc_a", name="get_time", output="x")])
    except ValidationError as exc:
        print("user 消息塞 tool_result ->", type(exc).__name__, ":", exc.errors()[0]["msg"])
    else:  # pragma: no cover - 上游改了校验才会走到
        raise AssertionError("user 消息竟然接受了 ToolResultBlock，契约假设已失效")

    # get_text_content 在没有文本时返回 None（不是空串）
    assert AssistantMsg(name="a", content=[HintBlock(hint="x")]).get_text_content() is None
    print("只有 HintBlock 的消息 get_text_content() ->", AssistantMsg(name="a", content=[HintBlock(hint="x")]).get_text_content())


# ----------------------------------------------------------------------
# B. 事件 → 消息的折叠
# ----------------------------------------------------------------------
def section_b(events: list[object]) -> list[object]:
    banner("B. Msg.append_event：事件流折叠成一条消息")

    # 折叠的第一道门槛是 reply_id：``append_event`` 开头就比对 ``event.reply_id``
    # （``message/_base.py:264``），而 ``CustomEvent`` **没有** ``reply_id`` 字段
    # （``event/_event.py:534`` 起：只有 ``name`` / ``value``）。
    foldable = [e for e in events if hasattr(e, "reply_id")]
    custom = [e for e in events if not hasattr(e, "reply_id")]
    msg = Msg(id=RID, name=AGENT_NAME, role="assistant", content=[])
    for event in foldable:
        msg.append_event(event)

    print("可折叠事件:", len(foldable), "| 不可折叠（无 reply_id）:", [type(e).__name__ for e in custom])
    for event in custom:
        try:
            msg.append_event(event)
        except AttributeError as exc:
            print("  CustomEvent 折叠 ->", type(exc).__name__, ":", exc)
        else:  # pragma: no cover - 上游给 CustomEvent 补了 reply_id 才会走到
            raise AssertionError("CustomEvent 竟然能被 append_event 消费")
    assert custom, "剧本里必须有一条 CustomEvent 才能验证这条边界"

    print("折叠出", len(foldable), "条事件 ->", len(msg.content), "个块:", [b.type for b in msg.content])
    print("usage            :", msg.usage)
    print("finished_reason  :", msg.finished_reason)

    call = msg.get_content_blocks("tool_call")[0]
    result = msg.get_content_blocks("tool_result")[0]
    data = msg.get_content_blocks("data")[0]

    assert call.state == ToolCallState.FINISHED, "ToolResultEnd 必须顺带关掉配对的 ToolCallBlock"
    assert msg.usage is not None and msg.usage.input_tokens == 786
    assert base64.b64decode(data.source.data) == b"hello", "分片 base64 必须先解码再拼字节"
    assert msg.finished_reason == ReplyFinishedReason.COMPLETED
    print("tool_call.state  :", call.state, "| input:", call.input)
    print("tool_result.state:", result.state, "| metadata:", result.metadata)
    print("data block bytes :", base64.b64decode(data.source.data), "| media:", data.source.media_type)

    # 事件只带 reply_id，不带块对象；块是折叠器现场 new 出来的
    print("事件对象里没有 content 字段:", "content" not in events[8].model_dump())
    assert "content" not in events[8].model_dump()

    # usage 是**累加**的（一条 ReAct 循环里 N 次模型调用 → 一份 usage）。
    # 本轮剧本只放了一次 MODEL_CALL_END，所以这里手工再折一条来验证累加语义。
    msg.append_event(ModelCallEndEvent(reply_id=RID, input_tokens=100, output_tokens=5))
    print("再折一条 MODEL_CALL_END 后 usage:", msg.usage)
    assert msg.usage.input_tokens == 886 and msg.usage.output_tokens == 64
    return list(msg.content)


# ----------------------------------------------------------------------
# C. 事件的序列化与盘点
# ----------------------------------------------------------------------
def section_c() -> None:
    banner("C. 事件 serde（TypeAdapter）+ 事件类型盘点")

    event_types = [item.value for item in EventType]
    print("EventType 成员数         :", len(event_types))
    print("AgentEvent 联合类型成员数:", len(typing.get_args(AgentEvent)))

    adapter: TypeAdapter = TypeAdapter(AgentEvent)
    source = TextBlockDeltaEvent(reply_id=RID, block_id="b1", delta="hi")
    dumped = source.model_dump(mode="json")
    print("序列化:", {k: dumped[k] for k in ("type", "reply_id", "block_id", "delta")})
    print("type 字段的真实类型:", type(dumped["type"]).__name__, "->", repr(dumped["type"]))
    restored = adapter.validate_python(dumped)
    assert type(restored) is TextBlockDeltaEvent and restored == source

    try:
        adapter.validate_python({"type": "NOPE", "reply_id": RID})
    except ValidationError:
        print("未知 type 反序列化 -> ValidationError")
    else:  # pragma: no cover - 上游加了 fallback 才会走到
        raise AssertionError("未知事件类型竟然能反序列化成功")

    handled = set(HANDLED_EVENT_TYPES)
    ignored = set(IGNORED_EVENT_TYPES)
    print("落记录的事件类型         :", len(handled), "种")
    print("刻意跳过的事件类型       :", len(ignored), "种")
    assert not (handled & ignored), "两种集合不能有交集"
    assert handled | ignored == set(event_types), (
        "事件类型盘点出现缺口 —— AgentScope 升级加了新事件，"
        "必须回到 harness_kit/events/translate.py 更新映射表"
    )
    print("盘点结论: 12 + 16 = 28，与 Agentscope 2.0.8 的 EventType 一一对应")


# ----------------------------------------------------------------------
# D. AgentState
# ----------------------------------------------------------------------
def section_d() -> None:
    banner("D. AgentState：字段 / 累积 / 序列化 / HITL 判定")

    state = AgentState(session_id=SESSION)
    print("顶层字段:", list(state.model_dump().keys()))
    state.reply_id = RID

    state.append_context(AGENT_NAME, [HintBlock(hint="注入的运行时状态")])
    state.append_context(AGENT_NAME, [TextBlock(text="你好，"), TextBlock(text="我是助手。")])
    print("两次 append_context 之后 context 长度:", len(state.context))
    assert len(state.context) == 1, "一次 reply 只产出一条 assistant 消息"

    tail = state.context[-1]
    print("尾部消息 id == reply_id:", tail.id == state.reply_id, "| 角色:", tail.role)
    print("合并后的文本:", tail.get_text_content())
    assert tail.id == state.reply_id and tail.role == "assistant"

    # HITL：断点状态完全从 context 尾部推导，没有独立 flag
    state.append_context(
        AGENT_NAME,
        [ToolCallBlock(id="tc_hitl", name="delete_file", input='{"path": "/tmp/x"}', state=ToolCallState.ASKING)],
    )
    print("has_awaiting_tool_calls:", state.has_awaiting_tool_calls(AGENT_NAME))
    print("get_unfinished_tool_calls:", [b.id for b in state.get_unfinished_tool_calls(AGENT_NAME)])
    assert state.has_awaiting_tool_calls(AGENT_NAME)
    assert [b.id for b in state.get_unfinished_tool_calls(AGENT_NAME)] == ["tc_hitl"]

    # 序列化边界：整份 AgentState（含完整 context）就是一个 JSON blob
    blob = state.model_dump(mode="json")
    again = AgentState.model_validate(blob)
    assert again == state, "AgentState 往返必须无损"
    print("AgentState → JSON → AgentState 无损，JSON 长度:", len(state.model_dump_json()))

    legacy = AgentState.model_validate({"session_id": SESSION, "reply_id": "r-legacy", "cur_iter": 7})
    print("旧格式迁移: reply_id =", legacy.reply_id, "| cur_iter =", legacy.cur_iter)
    assert legacy.reply_id == "r-legacy" and legacy.cur_iter == 7

    # 快照 = AgentState 的序列化切片（第 9 讲会把它落盘）
    snapshot = snapshot_from_state(state, seq=5)
    print("SessionSnapshot: seq =", snapshot.seq, "| 恢复出的 session_id =", restore_state(snapshot).session_id)
    assert restore_state(snapshot).session_id == SESSION


# ----------------------------------------------------------------------
# E. EventBus
# ----------------------------------------------------------------------
class Recorder:
    """一个订阅者：把事件记进列表，可选地抛异常或变慢。"""

    def __init__(self, *, boom: bool = False, delay: float = 0.0) -> None:
        """初始化。

        Args:
            boom (`bool`): 是否在第一次收到事件时抛异常。
            delay (`float`): 每条事件的处理耗时（秒）。
        """
        self.seen: list[EventRecord] = []
        self.boom = boom
        self.delay = delay

    async def __call__(self, record: EventRecord) -> None:
        """处理一条事件。

        Args:
            record (`EventRecord`): 事件记录。

        Raises:
            RuntimeError: ``boom=True`` 时故意抛出。
        """
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("订阅者坏了（这是故意的）")
        self.seen.append(record)


async def section_e() -> None:
    banner("E. EventBus：订阅 / 过滤 / 异常隔离 / 背压 / 关闭")

    bus = EventBus()
    everything = Recorder()
    only_tools = Recorder()
    broken = Recorder(boom=True)
    bus.subscribe("*", everything)
    bus.subscribe(EventKind.TOOL_CALL, only_tools)
    bus.subscribe("tool_call", broken)
    await bus.start()
    print("订阅数:", len(bus.subscriptions))

    def make(kind: EventKind, seq: int) -> EventRecord:
        return EventRecord(session_id=SESSION, seq=seq, kind=kind, payload={"name": kind.value, "data": {}})

    await bus.publish(EventKind.TOOL_CALL, make(EventKind.TOOL_CALL, 0))
    await bus.publish(EventKind.MODEL_CALL, make(EventKind.MODEL_CALL, 1))
    await bus.drain()

    print("通配订阅者收到:", [r.kind.value for r in everything.seen])
    print("tool_call 订阅者收到:", [r.kind.value for r in only_tools.seen])
    print("bus.stats():", bus.stats())
    assert len(everything.seen) == 2 and len(only_tools.seen) == 1
    assert bus.errors == 1, "坏订阅者必须被吞掉并计入 errors"
    print("error_samples:", bus.error_samples)

    # 背压：max_queue=1 时连发两条，第二条被丢弃并计数
    tight = EventBus(max_queue=1)
    slow = Recorder(delay=0.05)
    tight.subscribe("*", slow)
    await tight.start()
    first = await tight.publish(EventKind.CUSTOM, make(EventKind.CUSTOM, 0))
    second = await tight.publish(EventKind.CUSTOM, make(EventKind.CUSTOM, 1))
    print(f"max_queue=1 连发两条：第一条投递数={first}，第二条投递数={second}，dropped={tight.dropped}")
    assert (first, second, tight.dropped) == (1, 0, 1)
    await tight.aclose()
    await bus.aclose()

    try:
        await bus.publish(EventKind.CUSTOM, make(EventKind.CUSTOM, 99))
    except Exception as exc:
        print("关闭后 publish ->", type(exc).__name__)
    else:  # pragma: no cover - 语义变了才会走到
        raise AssertionError("关闭后的总线竟然还接受 publish")

    # 注销之后不再收到事件
    loose = EventBus()
    sub = loose.subscribe("*", everything)
    await loose.start()
    sub.unsubscribe()
    assert await loose.publish(EventKind.CUSTOM, make(EventKind.CUSTOM, 0)) == 0
    print("unsubscribe 之后投递数:", 0)
    await loose.aclose()
    # 注销只是把它从订阅表里摘掉，worker 还活着；aclose 必须把它一起收掉，
    # 否则事件循环关闭时会报 "Task was destroyed but it is pending!"
    assert sub._task is not None and sub._task.done(), "注销后的 worker 必须被 aclose 回收"
    print("注销后的 worker 在 aclose 时被回收:", sub._task.done())


# ----------------------------------------------------------------------
# F. StreamTranslator
# ----------------------------------------------------------------------
async def section_f() -> list[EventRecord]:
    banner("F. StreamTranslator：AgentEvent 流 → EventRecord 流")

    bus = EventBus()
    sink = Recorder()
    bus.subscribe("*", sink)
    await bus.start()

    translator = StreamTranslator(bus, session_id=SESSION)
    translator.note_input("北京现在几点？请调用 get_time 工具，并说明数据来源。" * 20)
    produced = await translator.consume(synthetic_stream())
    await bus.drain()

    print("读入事件数:", translator.records_seen, "| 产出记录数:", produced, "| 跳过:", translator.skipped)
    print("按类分布:", translator.stats()["counts"])
    print()
    for record in sink.seen:
        payload = json.dumps(record.payload, ensure_ascii=False)
        print(f"  seq={record.seq:02d} {record.kind.value:12s} {payload}")

    # 不变式 1：seq 从 0 开始、严格递增、无洞
    seqs = [r.seq for r in sink.seen]
    assert seqs == list(range(len(seqs))), f"seq 有洞或未从 0 开始: {seqs}"
    # 契约 §5.2：每种 kind 的 payload 必备字段
    for record in sink.seen:
        assert record.missing_payload_fields() == [], f"{record.kind} 缺字段"
    assert all(r.session_id == SESSION for r in sink.seen)
    assert [r.kind for r in sink.seen] == [
        EventKind.REPLY_START,
        EventKind.MODEL_CALL,
        EventKind.CUSTOM,  # HintBlockEvent 走逃生舱
        EventKind.TOOL_CALL,
        EventKind.PERMISSION,
        EventKind.PERMISSION,
        EventKind.TOOL_RESULT,
        EventKind.MEMORY_HIT,
        EventKind.REPLY_END,
    ]

    # 注意：同一种 kind 可能出多条（PERMISSION 出了 ask + allow 两条）。
    # 用 setdefault 取**首条**，否则后一条会把前一条覆盖掉。
    by_kind: dict[EventKind, EventRecord] = {}
    for record in sink.seen:
        by_kind.setdefault(record.kind, record)
    assert by_kind[EventKind.TOOL_CALL].payload["tool_input_digest"] == TOOL_INPUT_DIGEST
    assert by_kind[EventKind.TOOL_RESULT].payload["chars"] == len("2026-09-21 16:00")
    assert by_kind[EventKind.TOOL_RESULT].payload["state"] == "success"
    assert by_kind[EventKind.TOOL_RESULT].payload["error"] is None
    assert by_kind[EventKind.MODEL_CALL].payload["prompt_tokens"] == 786
    assert by_kind[EventKind.MODEL_CALL].payload["latency_ms"] > 0, "latency 靠两条事件时间戳相减"
    assert by_kind[EventKind.REPLY_END].payload["iterations"] == 1
    assert by_kind[EventKind.REPLY_END].payload["tool_calls"] == 1
    assert by_kind[EventKind.REPLY_START].payload["input_preview"].endswith("...")
    assert len(by_kind[EventKind.REPLY_START].payload["input_preview"]) == 503
    assert by_kind[EventKind.MEMORY_HIT].payload["tokens"] == 42
    permissions = [r for r in sink.seen if r.kind == EventKind.PERMISSION]
    assert [r.payload["behavior"] for r in permissions] == ["ask", "allow"]
    assert permissions[0].payload["tool_name"] == "get_time"
    print("\nToolCall 摘要 ==", TOOL_INPUT_DIGEST, "(与 sha1('{\"city\": \"Beijing\"}')[:16] 一致)")
    print("REPLY_START.input_preview 长度 ==", len(by_kind[EventKind.REPLY_START].payload["input_preview"]))
    print("PERMISSION 两条:", [(r.payload["behavior"], r.payload["tool_name"]) for r in permissions])

    # F2：SSE 投影 —— 逐字增量原样透传，Msg 返回 None
    frames = []
    for event in COLLECTED:
        frame = StreamTranslator.to_sse_frame(event)
        if frame is not None:
            frames.append(frame)
    print("SSE 帧数:", len(frames), "| 前 3 帧的事件名:", [f["event"] for f in frames[:3]])
    assert len(frames) == len(COLLECTED), "事件流里每一条都应该能出一帧"
    assert StreamTranslator.to_sse_frame(UserMsg(name="user", content="hi")) is None

    await bus.aclose()
    return list(sink.seen)


# ----------------------------------------------------------------------
# G. session/models
# ----------------------------------------------------------------------
def section_g(records: list[EventRecord]) -> list[SessionEvent]:
    banner("G. session/models：落盘形态与 seq 不变式")

    events = [SessionEvent.wrap(record) for record in records]
    print("SessionEvent 示例:", events[0].to_json_line()[:160], "...")
    assert events[0].v == 1 and events[0].seq == 0 and events[-1].kind == EventKind.REPLY_END
    print("next_seq(events) =", next_seq(events))
    assert next_seq(events) == len(events)

    check_seq_invariants(events, expected_session_id=SESSION)
    print("check_seq_invariants: 通过")

    tampered = list(events)
    tampered[1] = SessionEvent.wrap(events[1].record.model_copy(update={"seq": 99}))
    try:
        check_seq_invariants(tampered, expected_session_id=SESSION)
    except SessionInvariantError as exc:
        print("人为改坏 seq ->", type(exc).__name__, ":", exc)
    else:  # pragma: no cover - 不变式失效才会走到
        raise AssertionError("seq 不变式没有被强制")

    # EventRecord 不可变（frozen=True）
    try:
        records[0].seq = 123  # type: ignore[misc]
    except ValidationError as exc:
        print("改 EventRecord.seq ->", type(exc).__name__, ":", exc.errors()[0]["msg"])
    else:  # pragma: no cover - frozen 失效才会走到
        raise AssertionError("EventRecord 竟然可以被就地修改")

    meta = SessionMeta.create(SESSION, profile_name="default", tags=["lesson03"])
    print("SessionMeta:", meta.model_dump(mode="json"))
    touched = meta.touched(event_count=len(events))
    assert touched.event_count == len(events) and touched.updated_at >= meta.updated_at
    try:
        SessionMeta(session_id=SESSION, profile_name="default", created_at=meta.updated_at, updated_at=meta.created_at)
    except ValidationError as exc:
        print("时间倒挂 ->", type(exc).__name__, ":", exc.errors()[0]["msg"])
    print("preview('a'*600) 长度 =", len(preview("a" * 600)))
    assert len(preview("a" * 600)) == 503
    assert PAYLOAD_FIELDS[EventKind.REPLY_END] == ("reply_id", "iterations", "tool_calls")
    return events


# ----------------------------------------------------------------------
# H. 落盘
# ----------------------------------------------------------------------
def section_h(events: list[SessionEvent]) -> None:
    banner("H. 把事件记录写成 JSONL 再读回来")

    path = TMP / f"{SESSION}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.to_json_line() + "\n")

    back = [
        SessionEvent.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    check_seq_invariants(back, expected_session_id=SESSION)
    print("文件:", path)
    print("行数:", len(back), "| 字节数:", path.stat().st_size)
    print("读回第一条的 kind:", back[0].kind.value, "| 最后一条:", back[-1].kind.value)
    assert back == events, "JSONL 往返必须无损（EventRecord 是纯 pydantic 模型）"
    print("JSONL 往返无损（含 UTC tz-aware 时间戳）")


async def main() -> int:
    """跑完八个分节。

    Returns:
        `int`: 全部通过返回 0。
    """
    # 默认的 loguru sink 会把每条 DEBUG 都打到 stderr 上；本脚本只保留
    # WARNING 及以上（也就是"真的出事了"的那些），输出才好读。
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    print("harness_kit 版本:", harness_kit.__version__)
    print("临时目录:", TMP)

    section_a()
    section_c()
    section_d()
    await section_e()
    records = await section_f()
    events = section_g(records)
    section_h(events)
    # B 段放在最后跑：它要用 F 段收集到的、带真实时间戳的事件序列
    section_b(list(COLLECTED))

    banner("全部断言通过")
    print("A 块与状态轴 | B 事件折叠 | C 事件 serde 与盘点 | D AgentState")
    print("E 事件总线 | F 翻译器 | G 会话模型 | H 落盘 —— 0 次 LLM 调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
