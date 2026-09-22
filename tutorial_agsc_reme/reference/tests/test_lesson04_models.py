# -*- coding: utf-8 -*-
"""第 4 讲的 pytest（交付物之一）：把模型适配层的六条契约钉成可回归的断言。

为什么模型层尤其需要单测：这一层的每个方法都长得"看起来对"——
``_provider_tool_choice`` 少一个分支、``cost_of`` 漏掉缓存价、
``RateLimitedModel`` 忘了抄 ``formatter``，**都不会让代码报错**，
只会让账单算少、让 Agent 在某个只有生产才走的路径上 ``AttributeError``。
本文件把这些"沉默的错"变成红的。

用法（``tests/conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/``
塞进 ``sys.path``，所以不设 ``PYTHONPATH`` 也能跑；这里显式写出来是为了与
另外两个脚本一致）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson04_models.py -v

LLM 调用预算：**0 次**。所有测试都用 :class:`EchoChatModel` 或纯函数，
不联网、不需要 API key。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from agentscope.agent import Agent
from agentscope.message import (
    AssistantMsg,
    Msg,
    SystemMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage, FinishedReason
from agentscope.tool import ToolChoice

from harness_kit.config.schema import ModelSpec
from harness_kit.models import (
    DEFAULT_PRICE_TABLE,
    EchoChatModel,
    HarnessChatModelAdapter,
    HarnessOpenAICompatFormatter,
    NormalizeStats,
    OpenAICompatChatModel,
    Price,
    PriceTable,
    RateLimitedModel,
    RetryPolicy,
    TokenBucket,
    UnknownPriceError,
    UnknownProviderError,
    build_chat_model,
    compute_delay,
    cost_of,
    default_price_table,
    describe_providers,
    format_usd,
    health_check,
    retry_with_backoff,
)
from harness_kit.settings import Settings

#: 全程复用的工具 schema，与验证脚本 ``scripts/04_model_adapters.py`` 一致。
WEATHER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某个城市的天气。",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def weather_script() -> list[dict]:
    """一段「先说一句、再调一次工具」的回声脚本。

    Returns:
        `list[dict]`: 可交给 :class:`EchoChatModel` 的 ``script``。
    """
    return [
        {
            "text": "我先查天气。",
            "tool_calls": [
                {"id": "call_1", "name": "get_weather", "input": {"city": "北京"}},
            ],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        },
    ]


class _Boom(Exception):
    """测试用的自定义异常（``retry_on`` 白名单里要能指名它）。"""


# ======================================================================
# 1. 继承关系与「唯一覆写点」纪律
# ======================================================================
def test_echo_is_a_chat_model_base() -> None:
    """适配器必须就是 ``ChatModelBase`` —— 否则 ``Agent(model=...)`` 类型不符。"""
    model = EchoChatModel(stream=False)
    assert isinstance(model, ChatModelBase)
    assert isinstance(model, HarnessChatModelAdapter)


def test_adapter_only_overrides_call_api() -> None:
    """适配器绝不复写 ``__call__``（契约 §3.4 的铁律）。

    复写 ``__call__`` 会把 ``ChatModelBase`` 里的重试循环、CancelledError →
    INTERRUPTED 的翻译、``_StreamAccumulator`` 聚合三件事全部丢掉。
    这里直接从 ``__dict__`` 查：只有定义了这个名字的类才在自己的
    ``__dict__`` 里留痕。
    """
    for cls in (HarnessChatModelAdapter, EchoChatModel, OpenAICompatChatModel):
        assert "__call__" not in cls.__dict__, f"{cls.__name__} 不应定义 __call__"
    assert "_call_api" in HarnessChatModelAdapter.__dict__
    assert "_call_api" in EchoChatModel.__dict__
    assert "_call_api" in OpenAICompatChatModel.__dict__


def test_adapter_has_formatter() -> None:
    """``Agent`` 会读 ``model.formatter``（``agent/_agent.py:2066``），不能为空。"""
    model = EchoChatModel(stream=False)
    assert model.formatter is not None
    assert isinstance(EchoChatModel(stream=False).totals(), dict)


def test_describe_mentions_key_fields() -> None:
    """``describe()`` 是排查线上的第一手信息，必须带 adapter / model / stream。"""
    text = EchoChatModel(stream=False).describe()
    assert "echo" in text and "stream=False" in text


# ======================================================================
# 2. 流式聚合与工具参数分片
# ======================================================================
async def test_stream_returns_async_generator() -> None:
    """``stream=True`` 时 ``__call__`` 返回 async generator 而不是 ``ChatResponse``。"""
    model = EchoChatModel(stream=True)
    result = await model([UserMsg("user", "你好")])
    assert not isinstance(result, ChatResponse)
    assert hasattr(result, "__aiter__")


async def test_stream_final_chunk_is_last_and_unique() -> None:
    """流里必须**恰好**有一个 ``is_last=True`` 的终块，且它是最后一块。"""
    model = EchoChatModel(stream=True, chunk_size=3)
    chunks = [chunk async for chunk in await model([UserMsg("user", "你好")])]
    assert sum(1 for c in chunks if c.is_last) == 1
    assert chunks[-1].is_last is True


async def test_tool_arguments_arrive_in_fragments() -> None:
    """工具参数是**分片**下发的；只有终块里的 ``input`` 才是完整合法 JSON。"""
    model = EchoChatModel(stream=True, chunk_size=3, script=weather_script())
    stream = await model(
        [UserMsg("user", "北京天气？")],
        tools=[WEATHER_TOOL],
        tool_choice=ToolChoice(mode="auto"),
    )

    fragments: list[str] = []
    final: ChatResponse | None = None
    async for chunk in stream:
        if chunk.is_last:
            final = chunk
            continue
        for block in chunk.content:
            if getattr(block, "type", None) == "tool_call":
                fragments.append(block.input)

    assert final is not None
    assert len(fragments) > 1, "回声模型应把工具参数切成多片"
    calls = [b for b in final.content if b.type == "tool_call"]
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert json.loads(calls[0].input) == {"city": "北京"}
    # 每一片单独 parse 都应当失败 —— 这正是「必须等终块」的证据
    for piece in fragments[:-1] or fragments:
        with pytest.raises(json.JSONDecodeError):
            json.loads(piece)


async def test_usage_only_on_final_chunk() -> None:
    """usage 只落在终块上（中途每个 chunk 都带 usage 会让聚合器反复覆写）。"""
    model = EchoChatModel(stream=True, script=weather_script())
    chunks = [
        chunk
        async for chunk in await model(
            [UserMsg("user", "北京天气？")],
            tools=[WEATHER_TOOL],
            tool_choice=ToolChoice(mode="auto"),
        )
    ]
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_tokens == 20
    assert chunks[-1].usage.output_tokens == 10


async def test_accumulate_matches_call() -> None:
    """公开累积 API ``_accumulate`` 与 ``__call__`` 内的私有聚合器语义一致。"""
    model = EchoChatModel(stream=True, chunk_size=2)
    chunks = [c async for c in await model([UserMsg("user", "你好")])]
    deltas = [c for c in chunks if not c.is_last]
    manual = model._accumulate(deltas)
    assert manual.is_last is True
    assert "".join(
        b.text for b in manual.content if b.type == "text"
    ) == "[echo] 你好"


async def test_finished_reason_is_completed_on_normal_run() -> None:
    """正常结束时 ``finished_reason`` 是 ``completed``。

    注意它与 provider 的 ``finish_reason`` 不是一回事：AgentScope 只认
    ``interrupted`` / ``completed`` 两个值（``model/_model_response.py:22``）。
    """
    model = EchoChatModel(stream=False)
    response = await model([UserMsg("user", "你好")])
    assert response.finished_reason == FinishedReason.COMPLETED


async def test_cancellation_becomes_interrupted() -> None:
    """取消被翻译成 ``INTERRUPTED`` 的空响应，而**不是**抛出去。

    这就是 ``health_check`` 必须额外检查 ``finished_reason`` 的原因 ——
    超时在这条链路上看起来是「成功但内容为空」。

    做法：手动驱动 async generator，把 ``CancelledError`` 扔回它挂起的地方
    （``agen.athrow``），观察 ``ChatModelBase._stream()``
    （``third_party/agentscope/src/agentscope/model/_base.py:283``）的反应。
    这比用 ``asyncio.wait_for`` 可靠：``wait_for`` 会把生成器一起取消掉，
    那块被 yield 出来的终块会连同异常一起丢掉。
    """
    model = EchoChatModel(stream=True, chunk_size=1)
    agen = await model([UserMsg("user", "一段足够长的输出" * 20)])

    first = await agen.__anext__()
    assert first.is_last is False

    final = await agen.athrow(asyncio.CancelledError())
    assert final.is_last is True
    assert final.finished_reason == FinishedReason.INTERRUPTED
    # 终块里是**已经拼到一半**的内容（``_StreamAccumulator.build()`` 的产物），
    # 不是完整回答 —— 所以内容非空、但不等于完整回显。
    partial = "".join(b.text for b in final.content if b.type == "text")
    assert partial and partial.startswith("[")
    assert len(partial) < len("[echo] 一段足够长的输出" * 20)


async def test_cancelled_call_api_returns_empty_interrupted() -> None:
    """另一条取消路径：``_call_api`` 自己抛 ``CancelledError``，
    ``__call__`` 直接返回**空**的 ``INTERRUPTED`` 响应
    （``third_party/agentscope/src/agentscope/model/_base.py:219``）。"""

    class _CancelledModel(EchoChatModel):
        """每次调用都"被取消"的模型。"""

        async def _call_api(  # type: ignore[override]
            self,
            model_name: str,
            messages: list[Msg],
            tools: list[dict] | None = None,
            tool_choice: ToolChoice | None = None,
            **kwargs: object,
        ) -> ChatResponse:
            """直接抛取消。

            Raises:
                asyncio.CancelledError: 每次调用。
            """
            raise asyncio.CancelledError()

    response = await _CancelledModel(stream=False)([UserMsg("user", "你好")])
    assert response.is_last is True
    assert response.finished_reason == FinishedReason.INTERRUPTED
    assert response.content == []


async def test_wait_for_cancellation_propagates() -> None:
    """真被 ``asyncio.wait_for`` 掐断时，取消确实会传进生成器并终止消费。"""
    model = EchoChatModel(stream=True, chunk_size=1)

    async def _consume() -> None:
        """慢慢消费整条流。

        Returns:
            `None`: 无返回值。
        """
        async for _ in await model([UserMsg("user", "一段足够长的输出" * 20)]):
            await asyncio.sleep(0.05)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_consume(), timeout=0.01)


# ======================================================================
# 3. Formatter 归一化与计数器
# ======================================================================
def _history() -> list[Msg]:
    """一段覆盖 text / thinking / tool_call / tool_result 的历史。

    Returns:
        `list[Msg]`: 消息列表。
    """
    return [
        SystemMsg("system", [TextBlock(text="你是助手。")]),
        UserMsg("alice", [TextBlock(text="北京天气？")]),
        AssistantMsg(
            "agent",
            [
                ThinkingBlock(thinking="先查天气工具。"),
                TextBlock(text="我来查。"),
                ToolCallBlock(id="call_1", name="get_weather", input='{"city": "北京"}'),
                ToolResultBlock(
                    id="call_1",
                    name="get_weather",
                    output=[TextBlock(text="25C 晴")],
                ),
            ],
        ),
    ]


async def test_formatter_drops_name_fields() -> None:
    """本讲 formatter 默认摘掉所有 ``name``（自建端点常对它做正则校验）。"""
    formatter = HarnessOpenAICompatFormatter()
    out = await formatter.format(_history())
    assert all("name" not in message for message in out)
    assert formatter.stats["dropped_name"] > 0


async def test_formatter_counts_thinking_dropped() -> None:
    """父类静默丢弃 thinking；本讲 formatter 至少把它记进计数器。"""
    formatter = HarnessOpenAICompatFormatter()
    await formatter.format(_history())
    assert formatter.stats["thinking_dropped"] == 1
    assert formatter.stats["calls"] == 1


async def test_formatter_can_inline_thinking() -> None:
    """``thinking_as_text=True`` 时思考文本被拼进 assistant 的 content。"""
    formatter = HarnessOpenAICompatFormatter(thinking_as_text=True)
    out = await formatter.format(_history())
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert "先查天气工具。" in json.dumps(assistant, ensure_ascii=False)
    assert formatter.stats["thinking_inlined"] == 1
    assert formatter.stats["thinking_dropped"] == 0


async def test_formatter_keeps_tool_call_id() -> None:
    """``tool_call_id`` 是 OpenAI 协议的硬要求，任何归一化都不能动它。"""
    formatter = HarnessOpenAICompatFormatter()
    out = await formatter.format(_history())
    tool_messages = [m for m in out if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"


def test_normalize_stats_keys_are_zero_initialized() -> None:
    """所有计数键在构造时就为 0，避免 ``stats["x"] += n`` 在空 dict 上 KeyError。"""
    stats = NormalizeStats()
    assert set(stats) == set(NormalizeStats._KEYS)
    assert all(value == 0 for value in stats.values())
    stats.bump("calls", 3)
    assert stats["calls"] == 3
    assert stats.snapshot()["calls"] == 3


async def test_formatter_is_installed_on_the_model_by_default() -> None:
    """``OpenAICompatChatModel`` 默认挂本讲 formatter（而不是 AgentScope 的）。"""
    model = OpenAICompatChatModel(
        model_name="x",
        api_key="sk-not-a-real-key",
        base_url="https://example.invalid",
    )
    assert isinstance(model.formatter, HarnessOpenAICompatFormatter)


# ======================================================================
# 4. ToolChoice → provider 参数
# ======================================================================
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("auto", "auto"),
        ("none", "none"),
        ("required", "required"),
    ],
)
def test_tool_choice_literals(mode: str, expected: str) -> None:
    """三个字面量原样透传。"""
    assert (
        HarnessChatModelAdapter._provider_tool_choice(
            ToolChoice(mode=mode),
            ["get_weather"],
        )
        == expected
    )


def test_tool_choice_none_means_omit_field() -> None:
    """``tool_choice=None`` 表示**不发送**该字段，而不是发 ``"auto"``。"""
    assert HarnessChatModelAdapter._provider_tool_choice(None, []) is None


def test_tool_choice_named_tool_maps_to_function_object() -> None:
    """``mode="<工具名>"`` 翻成 ``{"type": "function", ...}``。"""
    mapped = HarnessChatModelAdapter._provider_tool_choice(
        ToolChoice(mode="get_weather"),
        ["get_weather"],
    )
    assert mapped == {"type": "function", "function": {"name": "get_weather"}}


def test_tool_choice_unknown_tool_raises() -> None:
    """工具名不在可用列表里必须**提前**报错，而不是让端点返回 400。"""
    with pytest.raises(ValueError, match="no_such_tool"):
        HarnessChatModelAdapter._provider_tool_choice(
            ToolChoice(mode="no_such_tool"),
            ["get_weather"],
        )


def test_provider_tools_filters_by_whitelist() -> None:
    """``tool_choice.tools`` 只**过滤**工具数组，不改 schema。"""
    tools = [
        {"type": "function", "function": {"name": "get_weather"}},
        {"type": "function", "function": {"name": "get_time"}},
    ]
    filtered = HarnessChatModelAdapter._provider_tools(
        tools,
        ToolChoice(mode="auto", tools=["get_time"]),
    )
    assert filtered == [{"type": "function", "function": {"name": "get_time"}}]
    assert HarnessChatModelAdapter._provider_tools(tools, None) == tools


# ======================================================================
# 5. 价格表与成本核算
# ======================================================================
def test_cost_of_without_cache() -> None:
    """无缓存命中：``input * in_price + output * out_price``。"""
    price = Price(input_per_mtok=1.0, output_per_mtok=2.0)
    usage = ChatUsage(input_tokens=1_000_000, output_tokens=500_000, time=0.0)
    assert cost_of(usage, price) == pytest.approx(2.0)


def test_cost_of_with_cache_read_price() -> None:
    """命中部分按 ``cache_read_per_mtok`` 计价，未命中部分按原价。"""
    price = Price(input_per_mtok=1.0, output_per_mtok=2.0, cache_read_per_mtok=0.1)
    usage = ChatUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        time=0.0,
        cache_input_tokens=400_000,
    )
    assert cost_of(usage, price) == pytest.approx((600_000 * 1.0 + 400_000 * 0.1) / 1e6)


def test_cost_of_cache_read_none_falls_back_to_full_price() -> None:
    """表里没给 cache 价时，命中部分按普通输入价算（与契约 §3.4 一致）。"""
    price = Price(input_per_mtok=1.0, output_per_mtok=2.0)
    usage = ChatUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        time=0.0,
        cache_input_tokens=400_000,
    )
    assert cost_of(usage, price) == pytest.approx(1.0)


def test_cost_of_never_negative_on_contradictory_usage() -> None:
    """某些兼容实现会给出 ``cache_input_tokens > input_tokens``，不能算成负数。"""
    price = Price(input_per_mtok=1.0, output_per_mtok=2.0, cache_read_per_mtok=0.1)
    usage = ChatUsage(
        input_tokens=100,
        output_tokens=0,
        time=0.0,
        cache_input_tokens=1000,
    )
    assert cost_of(usage, price) >= 0.0


def test_price_table_prefix_fallback() -> None:
    """精确匹配失败时按最长前缀兜底（会记 warning，生产别依赖）。"""
    table = PriceTable.from_mapping(
        {"deepseek-chat": {"input_per_mtok": 0.28, "output_per_mtok": 0.42}},
    )
    assert table.lookup("deepseek-chat").input_per_mtok == 0.28
    assert table.lookup("deepseek-chat-0324").input_per_mtok == 0.28
    assert table.lookup("DEEPSEEK-CHAT").input_per_mtok == 0.28


def test_price_table_unknown_raises() -> None:
    """查不到必须抛 ``UnknownPriceError``（``KeyError`` 子类）。"""
    table = PriceTable.from_mapping(
        {"deepseek-chat": {"input_per_mtok": 0.28, "output_per_mtok": 0.42}},
    )
    with pytest.raises(UnknownPriceError):
        table.lookup("gpt-9")


def test_default_price_table_covers_harness_models() -> None:
    """内置表必须覆盖本环境实际用的模型名，否则账本永远是 0。"""
    assert DEFAULT_PRICE_TABLE.lookup("deepseek-flash").input_per_mtok > 0
    assert default_price_table().lookup("openai/gpt-4o-mini").output_per_mtok > 0


def test_price_is_frozen() -> None:
    """``Price`` 是 frozen 的：改它必须报错，避免误改共享的价格表。"""
    price = Price(input_per_mtok=1.0, output_per_mtok=2.0)
    with pytest.raises(Exception):
        price.input_per_mtok = 9.0  # type: ignore[misc]


def test_format_usd_handles_tiny_values() -> None:
    """小额用科学计数法补一列，避免全部打印成 ``$0.000000``。"""
    assert format_usd(0.001234) == "$0.001234"
    assert "e-09" in format_usd(1.2e-9)


def test_totals_start_at_zero() -> None:
    """还没调用过时账本全零 —— ``cost_pretty`` 也必须是可打印的 ``$0.000000``。"""
    model = EchoChatModel(stream=False)
    assert model.totals()["calls"] == 0
    assert model.totals()["cost_pretty"] == "$0.000000"
    assert model.totals()["last_finish_reason"] is None
    assert model.last_finish_reason is None


async def test_echo_calls_are_counted_in_totals() -> None:
    """回声模型的 usage 也被记进账本（``echo`` 无价 → ``cost_usd`` 保持 0）。"""
    model = EchoChatModel(stream=False)
    for _ in range(2):
        await model([UserMsg("user", "你好")])
    totals = model.totals()
    assert totals["calls"] == 2
    assert totals["output_tokens"] > 0
    assert totals["cost_usd"] == 0.0


# ======================================================================
# 6. 令牌桶
# ======================================================================
class _FakeClock:
    """可手动推进的假时钟（让令牌桶的测试完全确定性）。"""

    def __init__(self) -> None:
        self.t: float = 0.0

    def __call__(self) -> float:
        """读当前时间。

        Returns:
            `float`: 假时间。
        """
        return self.t

    def advance(self, seconds: float) -> None:
        """推进时间。

        Args:
            seconds (`float`): 秒数。
        """
        self.t += seconds


def test_token_bucket_starts_full() -> None:
    """桶初始是满的（允许一段瞬时突发）。"""
    bucket = TokenBucket(rate=1.0, capacity=3)
    assert bucket.available == 3.0


def test_token_bucket_refills_lazily_and_caps() -> None:
    """惰性补充；补充量封顶在 ``capacity``。"""
    clock = _FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=4, clock=clock)
    assert bucket.take_nowait(4) is True
    assert bucket.available == 0.0
    clock.advance(1.0)
    assert bucket.available == pytest.approx(2.0)
    clock.advance(100.0)
    assert bucket.available == 4.0


def test_token_bucket_take_nowait_is_non_destructive_on_failure() -> None:
    """取不到时不改变桶状态（否则会"偷走"令牌）。

    注意不能用 ``== 0.0`` 断言：默认时钟是 ``time.monotonic``，两次读之间
    总会流逝几微秒，惰性补充会让 ``available`` 变成一个极小的正数。
    """
    bucket = TokenBucket(rate=1.0, capacity=1)
    assert bucket.take_nowait() is True
    assert bucket.available == pytest.approx(0.0, abs=1e-3)
    assert bucket.take_nowait() is False
    assert bucket.available == pytest.approx(0.0, abs=1e-3)


@pytest.mark.parametrize("tokens", [0, -1])
def test_token_bucket_rejects_non_positive(tokens: int) -> None:
    """``tokens < 1`` 属于调用方错误，直接抛。"""
    bucket = TokenBucket(rate=1.0, capacity=1)
    with pytest.raises(ValueError):
        bucket.take_nowait(tokens)


@pytest.mark.parametrize(("rate", "capacity"), [(0, 1), (-1, 1), (1, 0)])
def test_token_bucket_rejects_bad_construction(rate: float, capacity: int) -> None:
    """``rate <= 0`` 或 ``capacity < 1`` 都是配置错误。"""
    with pytest.raises(ValueError):
        TokenBucket(rate=rate, capacity=capacity)


async def test_token_bucket_acquire_waits() -> None:
    """令牌不足时 ``acquire`` 真的等待（实测 20/s → 约 0.05s）。

    **必须用真实时钟**：假时钟不会自己走，``acquire`` 里的
    ``await asyncio.sleep(missing / self.rate)`` 会永远等下去 ——
    这一条正是本文件第一版写成死循环的原因（验收时被 pytest 超时抓到）。
    """
    bucket = TokenBucket(rate=20.0, capacity=1)
    assert bucket.take_nowait() is True
    assert bucket.available < 0.1

    started = time.monotonic()
    await asyncio.wait_for(bucket.acquire(), timeout=2.0)
    elapsed = time.monotonic() - started
    assert 0.02 < elapsed < 1.0, elapsed
    assert bucket.waited_s > 0.0
    assert bucket.acquired == 2


async def test_token_bucket_acquire_is_immediate_when_full() -> None:
    """桶里有令牌时 ``acquire`` 不该等（快路径）。"""
    bucket = TokenBucket(rate=1.0, capacity=5)
    started = time.monotonic()
    await bucket.acquire(3)
    assert time.monotonic() - started < 0.05
    assert bucket.acquired == 3


async def test_token_bucket_acquire_passes_oversized_requests() -> None:
    """单次请求超过桶容量时直接放行（否则会死等一个永远凑不出的数）。"""
    bucket = TokenBucket(rate=1.0, capacity=2)
    await asyncio.wait_for(bucket.acquire(5), timeout=1.0)
    assert bucket.acquired == 5


# ======================================================================
# 7. 指数退避
# ======================================================================
def test_compute_delay_is_deterministic_without_jitter() -> None:
    """``jitter=0`` 时退避序列完全确定，便于断言。"""
    policy = RetryPolicy(
        max_attempts=6,
        base_delay=0.5,
        max_delay=8.0,
        multiplier=2.0,
        jitter=0.0,
    )
    assert [compute_delay(i, policy) for i in range(1, 7)] == [
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
    ]


def test_compute_delay_jitter_stays_in_band() -> None:
    """抖动落在 ``[1-j, 1+j]`` 区间，且确实随机（不同进程不会同步重试）。"""
    policy = RetryPolicy(base_delay=1.0, max_delay=1.0, jitter=0.5)
    samples = [compute_delay(1, policy) for _ in range(20)]
    assert all(0.5 <= s <= 1.5 for s in samples)
    assert len(set(samples)) > 1


def test_compute_delay_rejects_bad_attempt() -> None:
    """``attempt`` 从 1 开始。"""
    with pytest.raises(ValueError):
        compute_delay(0, RetryPolicy())


async def test_retry_with_backoff_recovers() -> None:
    """前两次失败、第三次成功 → 返回结果，不抛异常。"""
    calls = {"n": 0}

    @retry_with_backoff(max_attempts=4, base_delay=0.001, jitter=0.0)
    async def flaky() -> str:
        """前两次失败。

        Returns:
            `str`: ``"ok"``。

        Raises:
            ConnectionError: 前两次调用。
        """
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("故意失败")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


async def test_retry_with_backoff_reraises_last_error() -> None:
    """次数用尽后**原样**抛出最后一个异常，不吞不包装。"""
    seen: list[tuple[int, str, float]] = []

    @retry_with_backoff(
        max_attempts=3,
        base_delay=0.001,
        jitter=0.0,
        on_retry=lambda attempt, exc, delay: seen.append(
            (attempt, type(exc).__name__, round(delay, 6)),
        ),
    )
    async def always_fail() -> None:
        """永远失败。

        Raises:
            _Boom: 每次调用。
        """
        raise _Boom("永远失败")

    with pytest.raises(_Boom):
        await always_fail()
    assert [item[0] for item in seen] == [1, 2], "3 次尝试 = 2 次重试回调"


async def test_retry_with_backoff_respects_retry_on() -> None:
    """不在白名单里的异常**不重试**，立刻穿透。"""
    calls = {"n": 0}

    @retry_with_backoff(
        max_attempts=5,
        base_delay=0.001,
        jitter=0.0,
        retry_on=(ValueError,),
    )
    async def wrong_type() -> None:
        """抛一个不在白名单里的异常。

        Raises:
            _Boom: 每次调用。
        """
        calls["n"] += 1
        raise _Boom("不在白名单")

    with pytest.raises(_Boom):
        await wrong_type()
    assert calls["n"] == 1, "不在白名单里就不该重试"


async def test_retry_with_backoff_lets_cancellation_through() -> None:
    """``CancelledError`` 继承 ``BaseException``，默认白名单 ``(Exception,)`` 不吞它。"""
    calls = {"n": 0}

    @retry_with_backoff(max_attempts=5, base_delay=0.001, jitter=0.0)
    async def cancelled() -> None:
        """每次都被取消。

        Raises:
            asyncio.CancelledError: 每次调用。
        """
        calls["n"] += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cancelled()
    assert calls["n"] == 1


# ======================================================================
# 8. RateLimitedModel
# ======================================================================
def test_rate_limited_model_is_a_chat_model_base() -> None:
    """包装器必须仍是 ``ChatModelBase``，否则不能交给 ``Agent``。"""
    wrapped = RateLimitedModel(
        EchoChatModel(stream=False),
        TokenBucket(rate=10.0, capacity=5),
    )
    assert isinstance(wrapped, ChatModelBase)
    assert "__call__" not in RateLimitedModel.__dict__
    assert "_call_api" in RateLimitedModel.__dict__


def test_rate_limited_model_copies_formatter_from_inner() -> None:
    """``formatter`` 必须从内层抄上来 —— 否则 ``Agent`` 会 ``AttributeError``。"""
    inner = EchoChatModel(stream=False)
    wrapped = RateLimitedModel(inner, TokenBucket(rate=10.0, capacity=5))
    assert wrapped.formatter is inner.formatter
    assert wrapped.model == inner.model
    assert wrapped.stream == inner.stream


def test_rate_limited_model_rejects_non_chat_model() -> None:
    """包错类型要立刻报错，而不是等到调用时才炸。"""
    with pytest.raises(TypeError):
        RateLimitedModel("not-a-model", TokenBucket(rate=1.0, capacity=1))  # type: ignore[arg-type]


async def test_rate_limited_model_forwards_and_counts() -> None:
    """转发内层 ``_call_api`` 并记账；``unwrap()`` 拿回内层。"""
    inner = EchoChatModel(stream=False)
    wrapped = RateLimitedModel(inner, TokenBucket(rate=1000.0, capacity=10))
    response = await wrapped([UserMsg("user", "限流测试")])
    assert any(b.type == "text" for b in response.content)
    assert wrapped.stats.calls == 1
    assert wrapped.stats.tokens == 1
    assert wrapped.unwrap() is inner


async def test_rate_limited_model_works_inside_agent() -> None:
    """最硬的证据：``Agent(model=wrapped)`` 能跑完一轮。"""
    inner = EchoChatModel(stream=False)
    wrapped = RateLimitedModel(inner, TokenBucket(rate=1000.0, capacity=10))
    agent = Agent(
        name="limited-agent",
        system_prompt="你是限流演示用的助手。",
        model=wrapped,
    )
    reply = await agent.reply(UserMsg("user", "你好"))
    assert "[echo] 你好" in reply.get_text_content()
    assert wrapped.stats.calls == 1


async def test_rate_limited_model_delegates_count_tokens() -> None:
    """``count_tokens`` 必须委托内层，而不是走基类的粗估。"""
    inner = EchoChatModel(stream=False)
    wrapped = RateLimitedModel(inner, TokenBucket(rate=10.0, capacity=5))
    n = await wrapped.count_tokens([UserMsg("user", "你好")], None)
    expected = await inner.count_tokens([UserMsg("user", "你好")], None)
    assert n == expected > 0


# ======================================================================
# 9. 工厂
# ======================================================================
def test_describe_providers_lists_echo() -> None:
    """``describe_providers()`` 是「有哪些 provider」的程序化答案。"""
    providers = describe_providers()
    assert "echo" in providers and "openai_compat" in providers


def test_build_chat_model_echo_needs_no_key(settings: Settings) -> None:
    """``provider="echo"`` 完全离线，不需要任何 API key。"""
    model = build_chat_model(
        ModelSpec(provider="echo", model_name="echo", stream=False),
        settings=settings,
    )
    assert isinstance(model, EchoChatModel)


def test_build_chat_model_deepseek_returns_compat_adapter(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约 §3.4：``build_chat_model`` 在 deepseek 上也返回 OpenAI 兼容适配器。

    ``_resolve_api_key`` 读的是 ``Settings.environ_overlay()``
    （``harness_kit/settings.py:252``），它以 ``os.environ`` 为底，
    所以 monkeypatch 一个假 key 就能构造出客户端，**不会**发生任何网络请求
    （``OpenAICompatChatModel.__init__`` 只建 client，不发请求）。
    """
    monkeypatch.setenv("HARNESS04_TEST_KEY", "sk-fake-for-unit-test")
    spec = ModelSpec(
        provider="deepseek",
        model_name="deepseek-chat",
        api_key_env="HARNESS04_TEST_KEY",
        base_url_env="",
    )
    model = build_chat_model(spec, settings=settings)
    assert isinstance(model, OpenAICompatChatModel)
    assert model.model == "deepseek-chat"
    assert isinstance(model.formatter, HarnessOpenAICompatFormatter)


def test_build_chat_model_unknown_provider_raises(settings: Settings) -> None:
    """未知 provider 抛 ``UnknownProviderError``（``ValueError`` 子类）。"""
    spec = ModelSpec(provider="echo", model_name="x").model_copy(
        update={"provider": "no_such_provider"},
    )
    with pytest.raises(UnknownProviderError):
        build_chat_model(spec, settings=settings)


def test_build_chat_model_missing_key_raises(settings: Settings) -> None:
    """需要凭据的 provider 没拿到 key 时必须报错（而不是构造一个必然 401 的客户端）。"""
    spec = ModelSpec(
        provider="deepseek",
        model_name="deepseek-chat",
        api_key_env="HARNESS04_NO_SUCH_KEY_ENV",
    )
    with pytest.raises(ValueError, match="HARNESS04_NO_SUCH_KEY_ENV"):
        build_chat_model(spec, settings=settings)


async def test_health_check_ok_on_echo() -> None:
    """健康检查对活着的模型返回 ``ok=True`` + 延迟 + 文本片段。"""
    health = await health_check(EchoChatModel(stream=False), timeout=5.0)
    assert health.ok is True
    assert health.error is None
    assert health.latency_ms >= 0
    assert health.text == "[echo] ping"


async def test_health_check_flags_interrupted_as_unhealthy() -> None:
    """``ChatModelBase.__call__`` 把超时吞成 ``INTERRUPTED`` 空响应 ——
    健康检查必须把它判成不健康，否则会把挂掉的端点报成 ``ok=True``。"""
    health = await health_check(EchoChatModel(stream=False), timeout=0.0)
    assert health.ok is False
    assert health.error


async def test_health_check_never_raises() -> None:
    """健康检查**不抛异常**，失败被翻译成 ``ok=False``。"""

    class _DeadModel(ChatModelBase):
        """每次都抛异常的模型（模拟端点彻底挂掉）。"""

        def __init__(self) -> None:
            """构造最小可用的 ``ChatModelBase``。"""
            from agentscope.credential import CredentialBase
            from pydantic import SecretStr

            class _Cred(CredentialBase):
                """占位凭据。"""

                api_key: SecretStr = SecretStr("")

            super().__init__(
                credential=_Cred(),
                model="dead",
                parameters=ChatModelBase.Parameters(),
            )
            self.formatter = HarnessOpenAICompatFormatter()

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):  # type: ignore[no-untyped-def]
            """永远失败。

            Raises:
                ConnectionError: 每次调用。
            """
            raise ConnectionError("端点不可达")

    health = await health_check(_DeadModel(), timeout=5.0)
    assert health.ok is False
    assert "ConnectionError" in (health.error or "")


def test_model_health_forbids_extra_fields() -> None:
    """``ModelHealth`` 用 ``extra="forbid"``，防止报告结构悄悄漂移。"""
    from pydantic import ValidationError

    from harness_kit.models.factory import ModelHealth

    with pytest.raises(ValidationError):
        ModelHealth(ok=True, latency_ms=1.0, unknown_field=1)  # type: ignore[call-arg]
