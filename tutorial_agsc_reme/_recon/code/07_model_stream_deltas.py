# -*- coding: utf-8 -*-
"""侦察脚本 07：直接调用 AgentScope 的 ChatModel 做一次带工具的流式调用，
打印每个 delta 的类型、块 id、以及工具调用参数是如何被分片拼装的。

运行（必须用绝对路径的解释器）：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
    /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/07_model_stream_deltas.py
"""
import asyncio
import os

from dotenv import load_dotenv

load_dotenv(
    "/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env",
)

from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import TextBlock, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import ToolChoice  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Query the weather of a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "city name"},
                },
                "required": ["city"],
            },
        },
    },
]


def describe(block) -> str:
    """把一个 content block 压缩成一行可读描述。"""
    kind = block.type
    bid = block.id[:8]
    if kind == "text":
        return f"TextBlock(id={bid!r}, text={block.text!r})"
    if kind == "thinking":
        return f"ThinkingBlock(id={bid!r}, thinking={block.thinking!r})"
    if kind == "tool_call":
        return (
            f"ToolCallBlock(id={bid!r}, name={block.name!r}, "
            f"input={block.input!r})"
        )
    return f"{kind}(id={bid!r})"


async def main() -> None:
    credential = DeepSeekCredential(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
    )
    model = DeepSeekChatModel(
        credential=credential,
        model=os.environ["LLM_MODEL"],
        stream=True,
        parameters=DeepSeekChatModel.Parameters(
            temperature=0.0,
            thinking_enable=True,
        ),
    )
    print("=" * 78)
    print("model =", model.model, "| formatter =", type(model.formatter).__name__)
    print("stream =", model.stream, "| max_retries =", model.max_retries)
    print("context_size =", model.context_size)
    print("=" * 78)

    messages = [
        UserMsg(
            name="user",
            content=[TextBlock(text="北京今天天气怎么样？请调用工具查询。")],
        ),
    ]

    # ---------------- 流式：逐 delta 观察 ----------------
    print("\n########## 流式调用（stream=True） ##########")
    stream = await model(
        messages=messages,
        tools=TOOLS,
        tool_choice=ToolChoice(mode="auto"),
    )

    n_delta = 0
    n_final = 0
    tool_arg_fragments: list[str] = []
    async for delta in stream:
        n_delta += 1
        if delta.is_last:
            n_final += 1
            tag = "FINAL"
        else:
            tag = "delta"
        print(
            f"\n--- [{tag}] #{n_delta} id={delta.id[:8]!r} "
            f"usage={'yes' if delta.usage else 'none'} "
            f"blocks={len(delta.content)}",
        )
        for block in delta.content:
            print("       ", describe(block))
            if block.type == "tool_call" and not delta.is_last:
                # 只统计真正的增量分片；FINAL 块里的是已经拼好的完整参数
                tool_arg_fragments.append(block.input)
        if delta.usage:
            # ChatUsage 继承自 DictMixin(dict)，所以它本身就是个 dict
            print("        usage:", dict(delta.usage))

    print("\n总 delta 数 =", n_delta, "| is_last=True 的块数 =", n_final)
    print("工具参数被切成", len(tool_arg_fragments), "片")
    print("拼装后的完整参数 =", "".join(tool_arg_fragments))

    # ---------------- 非流式：一次拿到完整响应 ----------------
    print("\n########## 非流式调用（stream=False） ##########")
    model.stream = False
    resp = await model(
        messages=messages,
        tools=TOOLS,
        tool_choice=ToolChoice(mode="auto"),
    )
    print("返回类型 =", type(resp).__name__)
    print("is_last =", resp.is_last, "| finished_reason =", resp.finished_reason)
    for block in resp.content:
        print("       ", describe(block))
    print("usage =", dict(resp.usage))


if __name__ == "__main__":
    asyncio.run(main())
