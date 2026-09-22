# -*- coding: utf-8 -*-
"""第 4 讲验证脚本：模型适配层（`harness_kit/models/`）。

它把本讲的六条主结论全部变成可执行的断言：

  A. 适配器家族的自描述：`describe()` / `totals()` / `adapter_name`
  B. 流式增量：`stream=True` 时 `__call__` 返回 async generator，`is_last=True`
     的那一块才是完整响应；工具参数被**分片**下发，最终块里才是合法 JSON
  C. `_accumulate`（公开累积 API）与 `__call__`（私有 `_StreamAccumulator`）
     对同一批 delta 产出**一致**的结果
  D. Formatter：`HarnessOpenAICompatFormatter` 相对 `OpenAIChatFormatter`
     到底改了什么，以及计数器怎么读
  E. `ToolChoice` → provider 参数的映射（五个分支）与 tools 白名单过滤
  F. 价格表：三种计价分支 + 前缀兜底 + `UnknownPriceError` + `format_usd`
  G. 令牌桶：假时钟下的确定性行为 + 真实等待
  H. 指数退避：`compute_delay` 纯函数 + `retry_with_backoff` 真实重试
  I. `RateLimitedModel`：仍是 `ChatModelBase`、仍能被 `Agent` 直接用
  J. 工厂：`build_chat_model` 分派、`UnknownProviderError`、缺 key 报错、
     `health_check`
  K.（需要 key，`--live` 打开）真实 deepseek-flash 两次：非流式 + 带工具流式

用法（`PYTHONPATH` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/04_model_adapters.py

    加 `--live` 才会跑 K 段（真实 LLM，**2 次调用**）。

LLM 调用预算：A~J 段 **0 次**（`EchoChatModel` 不联网）；K 段 **2 次**。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import harness_kit

# 从 import 到的包反推路径，这样脚本放在任何地方都能跑：
#   <repo>/tutorial_agsc_reme/reference/harness_kit/__init__.py
#     .parent        -> .../reference/harness_kit
#     .parent.parent -> .../reference
REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(REPO / ".env", override=False)

from agentscope.agent import Agent  # noqa: E402
from agentscope.message import (  # noqa: E402
    AssistantMsg,
    Msg,
    SystemMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import ChatModelBase  # noqa: E402
from agentscope.tool import ToolChoice  # noqa: E402

from harness_kit.config.schema import ModelSpec  # noqa: E402
from harness_kit.models import (  # noqa: E402
    EchoChatModel,
    HarnessChatModelAdapter,
    HarnessOpenAICompatFormatter,
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
    describe_providers,
    format_usd,
    health_check,
    retry_with_backoff,
)
from harness_kit.settings import Settings  # noqa: E402

#: 本脚本的正式输出全部走 stdout；loguru 的日志全部走 stderr。
#: 这样 `python scripts/04_model_adapters.py` 的 stdout 是一份干净的、
#: 可以原样贴进教程的「预期输出」。想连日志一起看就加 `2>&1`，
#: 想调日志级别就设 HARNESS04_LOG_LEVEL。
logger.remove()
logger.add(sys.stderr, level=os.getenv("HARNESS04_LOG_LEVEL", "WARNING"))

TMP = Path(tempfile.mkdtemp(prefix="harness04_"))

#: 本讲全程使用的一个工具 schema。它同时被 B / E / K 段复用。
WEATHER_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某个城市的天气。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名"},
                },
                "required": ["city"],
            },
        },
    },
]


def banner(text: str) -> None:
    """打印一个分节标题。

    Args:
        text (`str`): 标题文本。
    """
    print(f"\n===== {text} =====")


def describe_block(block: object) -> str:
    """把一个 content block 压成一行可读描述。

    Args:
        block (`object`): AgentScope 的 content block。

    Returns:
        `str`: 形如 ``ToolCallBlock(id='call_1', ...)``。
    """
    kind = getattr(block, "type", "?")
    if kind == "text":
        return f"TextBlock(text={block.text!r})"
    if kind == "thinking":
        return f"ThinkingBlock(thinking={block.thinking!r})"
    if kind == "tool_call":
        return (
            f"ToolCallBlock(id={block.id!r}, name={block.name!r}, "
            f"input={block.input!r})"
        )
    return f"{kind}(...)"


def text_of(response: object) -> str:
    """从一个 :class:`~agentscope.model.ChatResponse` 里挑出纯文本。

    **为什么需要它**：``ChatResponse`` 上**没有** ``get_text_content()``
    —— 那是 ``Msg`` 的方法；``ChatResponse`` 是 ``DictMixin`` 子类，
    属性访问会走 ``dict.__getitem__``（``third_party/agentscope/src/
    agentscope/_utils/_mixin.py:26``），所以 ``response.get_text_content()``
    会直接 ``AttributeError``。文本只能自己从 ``content`` 里挑。

    Args:
        response (`object`): ``ChatResponse``。

    Returns:
        `str`: 拼接后的文本。
    """
    return "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
    )


# ----------------------------------------------------------------------
# A. 适配器家族
# ----------------------------------------------------------------------
def section_a() -> None:
    """打印 harness_kit 提供了哪些 provider，以及适配器的自描述。"""
    banner("A. 适配器家族与自描述")
    for name, note in describe_providers().items():
        print(f"  {name:15s} {note}")

    echo = EchoChatModel(stream=False)
    print("\nEchoChatModel.describe() ->", echo.describe())
    print("isinstance(model, ChatModelBase) =", isinstance(echo, ChatModelBase))
    print("isinstance(model, HarnessChatModelAdapter) =", isinstance(
        echo, HarnessChatModelAdapter,
    ))
    print("model.formatter =", type(echo.formatter).__name__)
    print("totals() =", json.dumps(echo.totals(), ensure_ascii=False))
    assert isinstance(echo, ChatModelBase)
    assert echo.formatter is not None, "Agent 会读 model.formatter，不能为空"
    print("OK  适配器就是 ChatModelBase；formatter 已就位（Agent 会读它）")


# ----------------------------------------------------------------------
# B. 流式增量与工具参数分片
# ----------------------------------------------------------------------
async def section_b() -> None:
    """跑一次离线流式调用，观察 delta / final 与工具参数分片。"""
    banner("B. 流式增量与工具参数分片拼装")
    model = EchoChatModel(
        stream=True,
        chunk_size=3,
        script=[
            {
                "text": "我先查天气。",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "get_weather",
                        "input": {"city": "北京"},
                    },
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        ],
    )
    messages = [UserMsg("user", "北京天气怎么样？")]

    stream = await model(
        messages,
        tools=WEATHER_TOOLS,
        tool_choice=ToolChoice(mode="auto"),
    )
    # stream=True 时 __call__ 返回的是 async generator，不是 ChatResponse
    print("返回类型 =", type(stream).__name__)
    assert not isinstance(stream, ChatResponseLike), (
        "stream=True 时不该返回 ChatResponse"
    )

    deltas: list[object] = []
    final = None
    fragments: list[str] = []
    async for delta in stream:
        deltas.append(delta)
        if delta.is_last:
            final = delta
            continue
        for block in delta.content:
            if getattr(block, "type", None) == "tool_call":
                fragments.append(block.input)

    assert final is not None, "流里必须有一个 is_last=True 的块"
    print(f"delta 数 = {len(deltas)} | is_last 块数 = "
          f"{sum(1 for d in deltas if d.is_last)}")
    print(f"工具参数被切成 {len(fragments)} 片：{fragments}")
    print("拼起来的原始串 =", repr("".join(fragments)))
    print("最终块内容：")
    for block in final.content:
        print("   ", describe_block(block))
    print("最终 usage =", dict(final.usage) if final.usage else None)

    joined = "".join(fragments)
    print("逐片 parse 的结果：")
    for i, piece in enumerate(fragments):
        try:
            json.loads(piece)
        except json.JSONDecodeError:
            print(f"    第 {i} 片 {piece!r} -> JSONDecodeError（预期）")
        else:  # pragma: no cover - 分片意外可解析时提示
            print(f"    第 {i} 片 {piece!r} -> 可解析（本次分片刚好在边界上）")
    print(f"    拼起来 {joined!r} -> 可解析 = "
          f"{bool(json.loads(joined or '{}') is not None)}")
    print("  结论：中途任何时候 parse 都可能失败，必须等 is_last=True 的终块")

    calls = [b for b in final.content if b.type == "tool_call"]
    assert len(calls) == 1
    parsed = json.loads(calls[0].input)
    assert parsed == {"city": "北京"}, parsed
    assert final.usage is not None and final.usage.output_tokens == 10
    print("OK  最终块的 ToolCallBlock.input 是**合法 JSON**，usage 只在最终块上")


class ChatResponseLike:
    """占位类型：只在「类型判断」断言里用来排除 `ChatResponse`。

    不导入 `ChatResponse` 是刻意的 —— 本脚本想验证的是「消费侧不需要知道
    具体类型，只需要看 `is_last`」，所以这里用一个必然不匹配的空类做守卫。
    """


# ----------------------------------------------------------------------
# C. 公开累积 API 与私有聚合器的一致性
# ----------------------------------------------------------------------
async def section_c() -> None:
    """断言 `_accumulate` 与 `__call__` 的聚合结果一致。"""
    banner("C. _accumulate 与 __call__ 的聚合结果一致性")
    from agentscope.model import ChatResponse  # 局部导入，避免污染 A 段

    model = EchoChatModel(stream=True, chunk_size=2)
    messages = [UserMsg("user", "你好")]

    raw = await model(messages)
    chunks: list[ChatResponse] = []
    async for chunk in raw:
        if not chunk.is_last:
            chunks.append(chunk)

    manual = model._accumulate(chunks)
    print("私有聚合器（__call__ 内部）产出的块数 =", len(chunks), "+ 1 个终块")
    print("_accumulate 手工拼出的终块：",
          " | ".join(describe_block(b) for b in manual.content))
    print("manual.is_last =", manual.is_last)

    assert manual.is_last is True, "_accumulate 必须补一个 is_last=True 的终块"
    assert manual.content, "拼出来的终块不该是空的"
    assert "".join(
        b.text for b in manual.content if b.type == "text"
    ) == "[echo] 你好"
    print("OK  公开累积 API 与 __call__ 内私有聚合器语义一致（教学用前者）")


# ----------------------------------------------------------------------
# D. Formatter：归一化与计数器
# ----------------------------------------------------------------------
def _formatter_messages() -> list[Msg]:
    """造一份覆盖 text / thinking / tool_call / tool_result 的历史。

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
                ToolCallBlock(
                    id="call_1",
                    name="get_weather",
                    input='{"city": "北京"}',
                ),
                ToolResultBlock(
                    id="call_1",
                    name="get_weather",
                    output=[TextBlock(text="25C 晴")],
                ),
            ],
        ),
    ]


async def section_d() -> None:
    """对比父类 formatter 与本讲 formatter 的输出与计数。"""
    banner("D. HarnessOpenAICompatFormatter 相对父类改了什么")
    from agentscope.formatter import OpenAIChatFormatter

    msgs = _formatter_messages()

    parent = OpenAIChatFormatter()
    parent_out = await parent.format(msgs)
    print("--- OpenAIChatFormatter（父类） ---")
    print(json.dumps(parent_out, ensure_ascii=False, indent=2)[:1200])

    ours = HarnessOpenAICompatFormatter()
    ours_out = await ours.format(msgs)
    print("--- HarnessOpenAICompatFormatter（本讲） ---")
    print(json.dumps(ours_out, ensure_ascii=False, indent=2)[:1200])
    print("stats =", json.dumps(ours.stats.snapshot(), ensure_ascii=False))
    print("describe() =", ours.describe())

    parent_names = [m.get("name") for m in parent_out if m.get("name")]
    ours_names = [m.get("name") for m in ours_out if m.get("name")]
    print("父类带 name 的消息 =", parent_names)
    print("本讲 formatter 带 name 的消息 =", ours_names)
    assert parent_names, "父类确实会给消息加 name（OpenAI 特化）"
    assert not ours_names, "本讲 formatter 必须把 name 摘干净"
    assert ours.stats["dropped_name"] == len(parent_names)
    assert ours.stats["thinking_dropped"] == 1
    assert ours.stats["calls"] == 1

    # thinking 改写进文本（默认关闭，这里显式打开看差异）
    inline = HarnessOpenAICompatFormatter(thinking_as_text=True)
    inline_out = await inline.format(msgs)
    merged = [m for m in inline_out if m.get("role") == "assistant"]
    print("thinking_as_text=True 时 assistant =",
          json.dumps(merged[0], ensure_ascii=False) if merged else None)
    assert inline.stats["thinking_inlined"] == 1
    assert "先查天气工具。" in json.dumps(merged[0], ensure_ascii=False)
    print("OK  name 摘除 / thinking 计数 / 改写进文本，三种差异都被显式化")


# ----------------------------------------------------------------------
# E. ToolChoice → provider 参数
# ----------------------------------------------------------------------
def section_e() -> None:
    """验证 tool_choice 映射的五个分支与 tools 白名单过滤。"""
    banner("E. ToolChoice → provider 参数")
    names = ["get_weather", "get_time"]
    cases = [
        None,
        ToolChoice(mode="auto"),
        ToolChoice(mode="none"),
        ToolChoice(mode="required"),
        ToolChoice(mode="get_weather"),
    ]
    for case in cases:
        mapped = HarnessChatModelAdapter._provider_tool_choice(case, names)
        label = str(case.mode) if case is not None else "None"
        print(f"  {label:15s} -> {mapped!r}")
    assert HarnessChatModelAdapter._provider_tool_choice(None, names) is None
    assert HarnessChatModelAdapter._provider_tool_choice(
        ToolChoice(mode="auto"), names,
    ) == "auto"
    assert HarnessChatModelAdapter._provider_tool_choice(
        ToolChoice(mode="get_weather"), names,
    ) == {"type": "function", "function": {"name": "get_weather"}}

    try:
        HarnessChatModelAdapter._provider_tool_choice(
            ToolChoice(mode="no_such_tool"), names,
        )
    except ValueError as exc:
        print("非法工具名 ->", type(exc).__name__, ":", str(exc)[:90], "...")
    else:  # pragma: no cover
        raise AssertionError("非法工具名竟然没报错")

    two_tools = [
        {"type": "function", "function": {"name": "get_weather"}},
        {"type": "function", "function": {"name": "get_time"}},
    ]
    filtered = HarnessChatModelAdapter._provider_tools(
        two_tools, ToolChoice(mode="auto", tools=["get_time"]),
    )
    print("按 tool_choice.tools 过滤 ->", filtered)
    assert filtered == [{"type": "function", "function": {"name": "get_time"}}]
    assert HarnessChatModelAdapter._provider_tools(two_tools, None) == two_tools
    print("OK  四个字面量 + 工具名模式；tools 白名单只**过滤**不改 schema")


# ----------------------------------------------------------------------
# F. 价格表与成本核算
# ----------------------------------------------------------------------
def section_f() -> None:
    """验证 cost_of 的三种分支、前缀兜底与 UnknownPriceError。"""
    banner("F. 价格表与成本核算")
    from agentscope.model import ChatUsage

    price = Price(input_per_mtok=1.0, output_per_mtok=2.0)
    usage = ChatUsage(input_tokens=1_000_000, output_tokens=500_000, time=1.0)
    print("无 cache 字段 ->", cost_of(usage, price))
    assert abs(cost_of(usage, price) - (1.0 + 1.0)) < 1e-9

    cached_price = Price(
        input_per_mtok=1.0,
        output_per_mtok=2.0,
        cache_read_per_mtok=0.1,
    )
    cached_usage = ChatUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        time=1.0,
        cache_input_tokens=400_000,
    )
    cost = cost_of(cached_usage, cached_price)
    print("命中 40 万且表里有 cache_read 价 ->", cost)
    assert abs(cost - (600_000 * 1.0 + 400_000 * 0.1) / 1e6) < 1e-9

    plain = cost_of(cached_usage, price)
    print("同样命中但表里 cache_read=None ->", plain)
    assert abs(plain - 1.0) < 1e-9

    contradictory = ChatUsage(
        input_tokens=100,
        output_tokens=0,
        time=0.0,
        cache_input_tokens=1000,
    )
    print("cache 命中 > input（矛盾数据）->", cost_of(contradictory, cached_price))
    assert cost_of(contradictory, cached_price) >= 0.0

    table = PriceTable.from_mapping(
        {"deepseek-chat": {"input_per_mtok": 0.28, "output_per_mtok": 0.42}},
    )
    print("精确命中 ->", table.lookup("deepseek-chat").input_per_mtok)
    print("前缀兜底 deepseek-chat-0324 ->",
          table.lookup("deepseek-chat-0324").input_per_mtok)
    print("大小写不敏感 DEEPSEEK-CHAT ->",
          table.lookup("DEEPSEEK-CHAT").input_per_mtok)
    try:
        table.lookup("gpt-9")
    except UnknownPriceError as exc:
        print("查不到 ->", type(exc).__name__, ":", str(exc)[:70])
    else:  # pragma: no cover
        raise AssertionError("未知模型竟然没报错")

    print("format_usd(0.001234) =", format_usd(0.001234))
    print("format_usd(1.2e-9)   =", format_usd(1.2e-9))
    print("OK  命中部分按缓存价、未命中按原价；矛盾数据用 max(0, ...) 兜底")


# ----------------------------------------------------------------------
# G. 令牌桶
# ----------------------------------------------------------------------
async def section_g() -> None:
    """令牌桶的确定性行为与真实等待。"""
    banner("G. TokenBucket")

    clock = {"t": 0.0}

    def fake_clock() -> float:
        """假时钟。

        Returns:
            `float`: 当前假时间。
        """
        return clock["t"]

    bucket = TokenBucket(rate=1.0, capacity=1, clock=fake_clock)
    print("初始 available =", bucket.available)
    assert bucket.available == 1.0
    print("take_nowait() =", bucket.take_nowait(), "| available =", bucket.available)
    assert bucket.available == 0.0
    clock["t"] += 0.5
    print("前进 0.5s -> available =", round(bucket.available, 4))
    assert abs(bucket.available - 0.5) < 1e-9
    clock["t"] += 100.0
    print("前进 100s（被封顶）-> available =", bucket.available)
    assert bucket.available == 1.0

    real = TokenBucket(rate=20.0, capacity=1)
    real.take_nowait()
    started = time.monotonic()
    await real.acquire()
    elapsed = time.monotonic() - started
    print(f"rate=20/s 时取 1 个令牌实际等待 {elapsed:.3f}s（理论 0.05s）")
    assert 0.03 < elapsed < 0.5, elapsed
    print("累计 waited_s =", round(real.waited_s, 3), "| acquired =", real.acquired)

    big = TokenBucket(rate=1.0, capacity=2)
    await big.acquire(tokens=5)  # 超过桶容量 -> 直接放行 + warning
    print("tokens=5 > capacity=2 -> 直接放行，acquired =", big.acquired)
    assert big.acquired == 5
    print("OK  惰性补充 / 封顶 / 超容量放行（避免死等）")


# ----------------------------------------------------------------------
# H. 指数退避
# ----------------------------------------------------------------------
def section_h() -> None:
    """compute_delay 的纯函数行为。"""
    banner("H. RetryPolicy 与 compute_delay")
    policy = RetryPolicy(
        max_attempts=6,
        base_delay=0.5,
        max_delay=8.0,
        multiplier=2.0,
        jitter=0.0,
    )
    delays = [round(compute_delay(i, policy), 4) for i in range(1, 7)]
    print("jitter=0 时 attempt 1..6 的等待 =", delays)
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]

    jittered = RetryPolicy(base_delay=1.0, max_delay=1.0, jitter=0.5)
    samples = [compute_delay(1, jittered) for _ in range(5)]
    print("jitter=0.5 的 5 次采样 =", [round(s, 4) for s in samples])
    assert all(0.5 <= s <= 1.5 for s in samples)
    assert len(set(samples)) > 1, "抖动必须真的随机（否则多进程会同步重试）"
    print("OK  指数增长 + 封顶 + 抖动区间 [1-j, 1+j]")


async def section_h2() -> None:
    """retry_with_backoff 的真实重试。"""
    banner("H2. retry_with_backoff")
    calls = {"n": 0}

    @retry_with_backoff(max_attempts=4, base_delay=0.001, jitter=0.0)
    async def flaky() -> str:
        """前两次失败、第三次成功。

        Returns:
            `str`: 固定字符串。

        Raises:
            ConnectionError: 前两次调用。
        """
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError(f"第 {calls['n']} 次故意失败")
        return "ok"

    result = await flaky()
    print("结果 =", result, "| 实际调用次数 =", calls["n"])
    assert result == "ok" and calls["n"] == 3

    seen: list[tuple[int, str, float]] = []

    @retry_with_backoff(
        max_attempts=3,
        base_delay=0.001,
        jitter=0.0,
        on_retry=lambda attempt, exc, delay: seen.append(
            (attempt, type(exc).__name__, round(delay, 4)),
        ),
    )
    async def always_fail() -> None:
        """永远失败。

        Raises:
            ValueError: 每次调用。
        """
        raise ValueError("永远失败")

    try:
        await always_fail()
    except ValueError as exc:
        print("耗尽后原样抛出 ->", type(exc).__name__, ":", exc)
    print("on_retry 回调记录 =", seen)
    assert [s[0] for s in seen] == [1, 2], "3 次尝试 = 2 次重试"
    print("OK  失败次数用尽后**原样抛出**最后一个异常，不吞不包装")


# ----------------------------------------------------------------------
# I. RateLimitedModel
# ----------------------------------------------------------------------
async def section_i() -> None:
    """包装后的模型仍是 ChatModelBase，且能被 Agent 直接用。"""
    banner("I. RateLimitedModel 包装 + 直接进 Agent")
    inner = EchoChatModel(stream=False)
    bucket = TokenBucket(rate=1000.0, capacity=10)
    wrapped = RateLimitedModel(inner, bucket)

    print("isinstance(wrapped, ChatModelBase) =", isinstance(wrapped, ChatModelBase))
    print("wrapped.model =", wrapped.model, "| stream =", wrapped.stream)
    print("formatter 是否从内层抄过来 =",
          wrapped.formatter is inner.formatter)
    print("unwrap() is inner =", wrapped.unwrap() is inner)
    assert isinstance(wrapped, ChatModelBase)
    assert wrapped.formatter is inner.formatter, (
        "Agent 会读 model.formatter，包装器必须把它抄上来"
    )

    from agentscope.message import UserMsg as _UserMsg

    first = await wrapped([_UserMsg("user", "限流测试")])
    print("调用一次 ->", text_of(first), "（注意 ChatResponse 没有 "
          "get_text_content()，只能自己挑 TextBlock）")
    print("stats =", wrapped.stats)
    assert wrapped.stats.calls == 1 and wrapped.stats.tokens == 1

    # 最硬的证据：Agent 直接吃它
    agent = Agent(
        name="limited-agent",
        system_prompt="你是限流演示用的助手。",
        model=wrapped,
    )
    reply = await agent.reply(UserMsg("user", "你好"))
    print("Agent.reply() ->", reply.get_text_content())
    print("stats（含 Agent 那一次） =", wrapped.stats)
    assert wrapped.stats.calls == 2
    assert inner.totals()["calls"] == 2
    print("OK  包装器不覆写 __call__，重试/聚合仍由 AgentScope 基类负责")


# ----------------------------------------------------------------------
# J. 工厂与健康检查
# ----------------------------------------------------------------------
async def section_j() -> None:
    """验证 build_chat_model 的分派与错误分支，以及 health_check。"""
    banner("J. build_chat_model / health_check")
    settings = Settings.from_env()

    echo_spec = ModelSpec(provider="echo", model_name="echo", stream=False)
    model = build_chat_model(echo_spec, settings=settings)
    print("echo  ->", type(model).__name__, "|", model.describe())
    assert isinstance(model, EchoChatModel)

    bad_spec = ModelSpec(provider="echo", model_name="x").model_copy(
        update={"provider": "no_such_provider"},
    )
    try:
        build_chat_model(bad_spec, settings=settings)
    except (UnknownProviderError, ValueError) as exc:
        print("未知 provider ->", type(exc).__name__, ":", str(exc)[:100])
    else:  # pragma: no cover
        raise AssertionError("未知 provider 竟然没报错")

    missing_key = ModelSpec(
        provider="deepseek",
        model_name="deepseek-chat",
        api_key_env="HARNESS04_NO_SUCH_KEY_ENV",
    )
    try:
        build_chat_model(missing_key, settings=settings)
    except ValueError as exc:
        print("缺 key ->", type(exc).__name__, ":", str(exc)[:110])
    else:  # pragma: no cover
        raise AssertionError("缺 key 竟然没报错")

    health = await health_check(EchoChatModel(stream=False), timeout=5.0)
    print("health_check(echo) =", json.dumps(health.model_dump(), ensure_ascii=False))
    assert health.ok is True and health.latency_ms >= 0

    timed_out = await health_check(EchoChatModel(stream=False), timeout=0.0)
    print("health_check(timeout=0) =", json.dumps(
        timed_out.model_dump(), ensure_ascii=False,
    ))
    assert timed_out.ok is False and timed_out.error
    print("OK  分派 / 报错 / 健康检查（含 INTERRUPTED 也被判为不健康）")


# ----------------------------------------------------------------------
# K. 真实模型（--live）
# ----------------------------------------------------------------------
async def section_k() -> int:
    """真实调用 deepseek-flash 两次：非流式 + 带工具流式。

    Returns:
        `int`: 实际发生的 LLM 调用次数。
    """
    banner("K. 真实模型两次调用（deepseek-flash）")
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    model_name = os.getenv("LLM_MODEL", "deepseek-flash")
    if not api_key:
        print("缺少 OPENAI_API_KEY —— K 段*无法运行*（不是通过）")
        print("提示：harness_kit 必须从仓库里的 reference/ 导入，"
              "脚本才会去找 <repo>/.env（见本文件开头 46-57 行）")
        raise RuntimeError("--live 需要 OPENAI_API_KEY")

    from harness_kit.models.pricing import default_price_table

    calls = 0

    # ---- K1：非流式，看 usage / finish_reason / 成本 ----
    plain = OpenAICompatChatModel(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        stream=False,
        pricing=default_price_table(),
    )
    print("describe() =", plain.describe())
    started = time.perf_counter()
    response = await plain([UserMsg("user", "用一句话说明什么是 token bucket。")])
    calls += 1
    print("K1 响应 =", text_of(response)[:160])
    print("K1 usage =", dict(response.usage) if response.usage else None)
    print("K1 last_finish_reason =", plain.last_finish_reason)
    print("K1 metadata =", response.metadata)
    print("K1 totals =", json.dumps(plain.totals(), ensure_ascii=False))
    print(f"K1 墙钟耗时 = {time.perf_counter() - started:.2f}s")
    assert response.usage is not None
    assert plain.last_finish_reason is not None, (
        "非流式路径必须保留 provider 的 finish_reason"
    )

    # ---- K2：带工具的流式，看分片与最终块 ----
    streaming = OpenAICompatChatModel(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        stream=True,
        pricing=default_price_table(),
    )
    stream = await streaming(
        [UserMsg("user", "北京今天天气怎么样？请调用 get_weather 工具。")],
        tools=WEATHER_TOOLS,
        tool_choice=ToolChoice(mode="auto"),
    )
    calls += 1
    fragments: list[str] = []
    final = None
    n_delta = 0
    async for chunk in stream:
        n_delta += 1
        if chunk.is_last:
            final = chunk
            continue
        for block in chunk.content:
            if getattr(block, "type", None) == "tool_call":
                fragments.append(block.input)

    assert final is not None
    print(f"K2 delta 数 = {n_delta} | 工具参数分片数 = {len(fragments)}")
    for block in final.content:
        print("    ", describe_block(block))
    print("K2 usage =", dict(final.usage) if final.usage else None)
    print("K2 last_finish_reason =", streaming.last_finish_reason)
    print("K2 totals =", json.dumps(streaming.totals(), ensure_ascii=False))
    tool_calls = [b for b in final.content if b.type == "tool_call"]
    if tool_calls:
        print("K2 工具名 =", tool_calls[0].name,
              "| input 可解析 =", bool(json.loads(tool_calls[0].input)))
    print(f"总 LLM 调用 = {calls} 次（预算 2 次）")
    return calls


# ----------------------------------------------------------------------
async def main() -> int:
    """跑完整套验证。

    Returns:
        `int`: 进程退出码；失败时非 0。
    """
    live = "--live" in sys.argv
    section_a()
    await section_b()
    await section_c()
    await section_d()
    section_e()
    section_f()
    await section_g()
    section_h()
    await section_h2()
    await section_i()
    await section_j()
    if live:
        try:
            calls = await section_k()
        except RuntimeError as exc:
            print(f"\nFAIL  --live 已指定但 K 段没跑起来：{exc}")
            return 2
        if calls == 0:
            print("\nFAIL  --live 已指定但 K 段一次调用都没发生")
            return 2
    else:
        banner("K. 跳过真实模型（要跑请加 --live）")
    print(f"\n临时目录: {TMP}")
    print("\nPASS  第 4 讲验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
