# -*- coding: utf-8 -*-
"""06 侦察验证脚本 H：ToolBase 子类四种实现方式的调用分派
  - call() 是 async def  -> 返回 ToolChunk（协程）
  - call() 是 async def + yield -> 异步生成器流
  - 中间件把两种形状统一成一条流
  - __call__ 只接受关键字参数
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_tool_protocol_dispatch.py
"""
import asyncio
import inspect
from typing import Any, AsyncGenerator

from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk, ToolMiddlewareBase


class AllowMixin:
    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW,
                                  message="ok")


class SingleTool(AllowMixin, ToolBase):
    """call() is a coroutine returning one ToolChunk."""

    name = "single"
    description = "one chunk"
    input_schema = {"type": "object", "properties": {},
                    "required": []}
    is_concurrency_safe = True
    is_read_only = True

    async def call(self) -> ToolChunk:
        return ToolChunk(content=[TextBlock(text="single")])


class StreamTool(AllowMixin, ToolBase):
    """call() is an async generator function."""

    name = "stream"
    description = "many chunks"
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_concurrency_safe = True
    is_read_only = True

    async def call(self) -> AsyncGenerator[ToolChunk, None]:
        for i in range(3):
            yield ToolChunk(content=[TextBlock(text=f"part{i} ")])


class NamingMiddleware(ToolMiddlewareBase):
    async def on_tool_call(self, tool, input_kwargs, next_handler):
        async for chunk in next_handler(**input_kwargs):
            chunk.content[0].text = f"<{tool.name}>{chunk.content[0].text}"
            yield chunk


async def main() -> None:
    print("=" * 70)
    print("[1] inspect says: which shape is each call()?")
    for cls in (SingleTool, StreamTool):
        print(f"   {cls.__name__}.call isasyncgenfunction = "
              f"{inspect.isasyncgenfunction(cls.call)}"
              f" | __call__ iscoroutinefunction = "
              f"{inspect.iscoroutinefunction(cls.__call__)}")

    print("=" * 70)
    print("[2] ToolBase.__call__ returns 'ToolChunk | AsyncGenerator'")
    for tool in (SingleTool(), StreamTool()):
        res = await tool()
        print(f"   {tool.name}: type = {type(res).__name__}, "
              f"is AsyncGenerator = {isinstance(res, AsyncGenerator)}")
        texts = []
        if isinstance(res, AsyncGenerator):
            async for chunk in res:
                texts.append(chunk.content[0].text)
        else:
            texts.append(res.content[0].text)
        print(f"      -> {texts}")

    print("=" * 70)
    print("[3] WITHOUT middlewares the two shapes stay different;")
    print("    WITH middlewares __call__ returns ONE async generator")
    for tool in (SingleTool(middlewares=[NamingMiddleware()]),
                 StreamTool(middlewares=[NamingMiddleware()])):
        res = await tool()
        print(f"   {tool.name}: type = {type(res).__name__}")
        texts = []
        async for chunk in res:
            texts.append(chunk.content[0].text)
        print(f"      -> {texts}")

    print("=" * 70)
    print("[4] onion order: the FIRST registered middleware is the OUTERMOST")
    order = []

    class M1(ToolMiddlewareBase):
        async def on_tool_call(self, tool, input_kwargs, next_handler):
            order.append("m1-pre")
            async for c in next_handler(**input_kwargs):
                yield c
            order.append("m1-post")

    class M2(ToolMiddlewareBase):
        async def on_tool_call(self, tool, input_kwargs, next_handler):
            order.append("m2-pre")
            async for c in next_handler(**input_kwargs):
                yield c
            order.append("m2-post")

    _res = await SingleTool(middlewares=[M1(), M2()])()
    async for _ in _res:
        pass
    print("   order =", order)

    print("=" * 70)
    print("[5] a middleware may REWRITE the kwargs before the tool sees them")
    seen = {}

    class Rewrite(ToolMiddlewareBase):
        async def on_tool_call(self, tool, input_kwargs, next_handler):
            kw = dict(input_kwargs)
            kw["x"] = "rewritten-by-middleware"
            async for c in next_handler(**kw):
                yield c

    class Echo(AllowMixin, ToolBase):
        name = "echo"
        description = "echo x"
        input_schema = {"type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": ["x"]}
        is_concurrency_safe = True
        is_read_only = True

        async def call(self, x: str) -> ToolChunk:
            seen["x"] = x
            return ToolChunk(content=[TextBlock(text=x)])

    _res2 = await Echo(middlewares=[Rewrite()])(x="original")
    async for _ in _res2:
        pass
    print("   the tool received:", seen["x"])

    print("=" * 70)
    print("[6] __call__ rejects positional args loudly")
    try:
        await SingleTool().__call__("oops")
    except TypeError as e:
        print("   TypeError:", e)

    print("=" * 70)
    print("[7] call() on an external tool raises RuntimeError")
    class Ext(AllowMixin, ToolBase):
        name = "ext"
        description = "external"
        input_schema = {"type": "object", "properties": {}, "required": []}
        is_external_tool = True

    try:
        await Ext()()
    except RuntimeError as e:
        print("   RuntimeError:", e)


if __name__ == "__main__":
    asyncio.run(main())
