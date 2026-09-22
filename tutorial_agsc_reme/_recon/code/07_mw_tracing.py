"""07_agentscope_middleware_hook 侦察：TracingMiddleware + OpenTelemetry 实测。

关键发现（源码 vs 文档不一致）：
  middleware/_tracing/_trace.py:61 和 :121 的 docstring 说「``setup_tracing``
  被调用过才生效」，但 setup_tracing 这个函数在 agentscope 2.0.8 里
  **根本不存在**（全仓库 grep 只命中这两处 docstring）。
  真实生效条件是：进程里注册了 opentelemetry-sdk 的 TracerProvider
  （_check_tracing_enabled() 用 isinstance 判断，见 _trace.py:59）。

验证点：
1. 不装 TracerProvider 时，TracingMiddleware 直接短路，几乎零开销。
2. 装了 TracerProvider（+ InMemorySpanExporter）后，产生 3 类 span：
   agent 的 invoke_agent / LLM 的 chat / 工具的 execute_tool。
3. span 名字与属性由 _extractor.py 里的 _get_*_span_name / _get_*_attributes 决定。
"""
import asyncio
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.middleware import MiddlewareBase, TracingMiddleware  # noqa: E402
from agentscope.middleware._tracing._trace import (  # noqa: E402
    _check_tracing_enabled,
)
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402


async def get_time() -> str:
    """Return the current date.

    Returns:
        `str`: the date string.
    """
    return "2026-09-21"


class CountingMiddleware(MiddlewareBase):
    """与 TracingMiddleware 同链的对照组，用来证明 tracing 不改变行为。"""

    def __init__(self) -> None:
        self.reply_events = 0

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async for evt in next_handler(**input_kwargs):
            self.reply_events += 1
            yield evt


def build_agent(mws: list, name: str) -> Agent:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=200),
    )
    return Agent(
        name=name,
        system_prompt="你是中文助手。先调用 get_time 工具，再回答用户。",
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(get_time, is_read_only=True)]),
        middlewares=mws,
        react_config=ReActConfig(max_iters=3),
    )


async def main() -> None:
    print("=" * 74)
    print("A. 未注册 TracerProvider：_check_tracing_enabled() 应为 False")
    print("=" * 74)
    print("  provider =", otel_trace.get_tracer_provider())
    print("  _check_tracing_enabled() =", _check_tracing_enabled())
    counter = CountingMiddleware()
    agent0 = build_agent([TracingMiddleware(), counter], "no-trace")
    msg0 = await agent0.reply(UserMsg("user", "现在几号？"))
    print("  reply =", msg0.get_text_content()[:60])
    print("  同链的 CountingMiddleware 收到事件数 =", counter.reply_events)
    print("  >>> 短路路径：docstring 里的 setup_tracing 不存在，但逻辑仍然正确")
    print()

    print("=" * 74)
    print("B. 注册 InMemorySpanExporter 后重跑")
    print("=" * 74)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    print("  _check_tracing_enabled() =", _check_tracing_enabled())
    print()

    exporter.clear()
    counter2 = CountingMiddleware()
    agent1 = build_agent([TracingMiddleware(), counter2], "traced")
    msg1 = await agent1.reply(UserMsg("user", "现在几号？"))
    print("  reply =", msg1.get_text_content()[:60])
    print("  同链的 CountingMiddleware 收到事件数 =", counter2.reply_events)
    print()

    spans = exporter.get_finished_spans()
    print(f"  产生 span 数 = {len(spans)}")
    for i, s in enumerate(spans, 1):
        parent = s.parent.span_id if s.parent else None
        print(f"  {i:02d}. name={s.name!r} kind={s.kind.name} "
              f"parent={parent} status={s.status.status_code.name}")
        for k, v in sorted(s.attributes.items()):
            print(f"        {k} = {str(v)[:70]}")
    print()

    print("=" * 74)
    print("C. TraceBuilder / 属性来源（middleware/_tracing/_extractor.py）")
    print("=" * 74)
    from agentscope.middleware._tracing._attributes import SpanAttributes  # noqa: E402

    print("  SpanAttributes 常量:")
    for name in dir(SpanAttributes):
        if name.isupper() and not name.startswith("_"):
            print(f"    {name:44s} = {getattr(SpanAttributes, name)!r}")


asyncio.run(main())
