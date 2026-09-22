# -*- coding: utf-8 -*-
"""侦察脚本 09：验证 ChatModelBase 的各个「契约」——
count_tokens / generate_structured_output / tool_choice 校验 /
ModelCard 目录 / 重试白名单 / 中断处理。

运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
    /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/09_model_contracts.py
"""
import asyncio
import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(
    "/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env",
)

from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import Msg, TextBlock, UserMsg  # noqa: E402
from agentscope.model import (  # noqa: E402
    ChatModelBase,
    DeepSeekChatModel,
    FinishedReason,
)
from agentscope.tool import ToolChoice  # noqa: E402


class Weather(BaseModel):
    """结构化输出的 schema。"""

    city: str = Field(description="城市名")
    temperature_c: int = Field(description="摄氏温度")
    condition: str = Field(description="天气状况")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Query the weather of a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]


def make_model(stream: bool = False) -> DeepSeekChatModel:
    return DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=stream,
        parameters=DeepSeekChatModel.Parameters(temperature=0.0),
    )


async def main() -> None:
    model = make_model()

    # ---------------- 1. 抽象接口清单 ----------------
    print("=" * 78)
    print("1. ChatModelBase 的公开契约")
    print("=" * 78)
    print("__call__             :", ChatModelBase.__call__.__qualname__)
    print("_call_api (abstract) :", ChatModelBase._call_api.__qualname__)
    print("count_tokens         :", ChatModelBase.count_tokens.__qualname__)
    print(
        "generate_structured_output :",
        ChatModelBase.generate_structured_output.__qualname__,
    )
    print("list_models (classmethod)   :", ChatModelBase.list_models.__qualname__)
    print(
        "可重试异常白名单(deepseek) =",
        [c.__name__ for c in DeepSeekChatModel._get_retryable_exceptions()],
    )
    print(
        "结构化输出 fallback 异常   =",
        [
            c.__name__
            for c in DeepSeekChatModel._get_structured_output_fallback_exceptions()
        ],
    )
    print("禁用 thinking 的 kwargs    =", model._get_disable_thinking_kwargs())

    # ---------------- 2. count_tokens ----------------
    print("\n" + "=" * 78)
    print("2. count_tokens：字节数 / 4 的粗估")
    print("=" * 78)
    msgs = [
        UserMsg(name="user", content=[TextBlock(text="你好，世界")]),
    ]
    print("短消息 messages：", await model.count_tokens(msgs, None))
    print("长消息(1000 个汉字)：", await model.count_tokens(
        [UserMsg(name="user", content=[TextBlock(text="测" * 1000)])],
        None,
    ))
    print("带工具 schema：", await model.count_tokens(msgs, TOOLS))
    # 汉字算出：1000*3 字节 / 4 = 750，说明它用的是 UTF-8 字节数而不是 tokenizer
    print("提示：中文的 UTF-8 是 3 字节/字，所以 1000 字 -> 3000/4 = 750")

    # ---------------- 3. ModelCard 目录 ----------------
    print("\n" + "=" * 78)
    print("3. list_models()：从 _models/*.yaml 读出的模型卡")
    print("=" * 78)
    for card in DeepSeekChatModel.list_models():
        print(
            f"  {card.name:<22} status={card.status:<10} "
            f"ctx={card.context_size:<9} out={card.output_size:<8} "
            f"in={card.input_types}",
        )
    print("当前 .env 里配的 LLM_MODEL =", os.environ["LLM_MODEL"])
    print(
        "注意：模型卡里没有 deepseek-flash 这个名字，但 API 实际接受它。"
        "模型卡是「给前端下拉框用的目录」，不是可用模型的权威清单。",
    )

    # ---------------- 4. tool_choice 校验 ----------------
    print("\n" + "=" * 78)
    print("4. tool_choice 的本地校验（不发请求）")
    print("=" * 78)
    try:
        model._validate_tool_choice(ToolChoice(mode="not_exist"), TOOLS)
    except ValueError as e:
        print("非法工具名 ->", type(e).__name__, ":", e)
    model._validate_tool_choice(ToolChoice(mode="get_weather"), TOOLS)
    print("合法工具名 -> 通过")
    print(
        "_format_tools(mode='get_weather') =",
        model._format_tools(
            TOOLS,
            ToolChoice(mode="get_weather"),
        )[1],
    )
    print(
        "_format_tools(mode='auto') =",
        model._format_tools(TOOLS, ToolChoice(mode="auto"))[1],
    )

    # ---------------- 5. generate_structured_output ----------------
    print("\n" + "=" * 78)
    print("5. generate_structured_output：走 forced tool_choice 的 fallback 阶梯")
    print("=" * 78)
    structured = await model.generate_structured_output(
        messages=[
            UserMsg(
                name="user",
                content=[TextBlock(text="北京现在 25 度，晴天。请结构化输出。")],
            ),
        ],
        structured_model=Weather,
    )
    print("返回类型 =", type(structured).__name__)
    print("content =", structured.content)
    print("id =", structured.id[:8], "| type =", structured.type)
    print("usage =", dict(structured.usage) if structured.usage else None)
    print("model_validate 通过 =", Weather.model_validate(structured.content))
    print("json schema =", Weather.model_json_schema())

    # ---------------- 6. 空 messages 的报错契约 ----------------
    print("\n" + "=" * 78)
    print("6. 边界：空 messages")
    print("=" * 78)
    try:
        await model.generate_structured_output(
            messages=[],
            structured_model=Weather,
        )
    except ValueError as e:
        print("ValueError:", e)
    print("FinishedReason 枚举 =", [_.value for _ in FinishedReason])

    # ---------------- 7. 中断：CancelledError -> INTERRUPTED ----------------
    print("\n" + "=" * 78)
    print("7. 取消一次流式调用，观察 FinishedReason.INTERRUPTED")
    print("=" * 78)
    stream_model = make_model(stream=True)
    cut = asyncio.Event()
    counters = {"delta": 0, "last": 0, "reason": None}

    async def consume() -> None:
        stream = await stream_model(
            messages=[
                UserMsg(
                    name="user",
                    content=[TextBlock(text="请写一篇 800 字的散文。")],
                ),
            ],
        )
        async for delta in stream:
            if delta.is_last:
                counters["last"] += 1
                counters["reason"] = delta.finished_reason
            else:
                counters["delta"] += 1
                if counters["delta"] == 3:
                    cut.set()

    task = asyncio.create_task(consume())
    await cut.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    raised = None
    try:
        await task
    except asyncio.CancelledError:
        raised = "CancelledError 冒泡到了外层"
    print("await task 的结果 =", raised or "正常返回，没有抛 CancelledError")
    print("已经收到的 delta 数 =", counters["delta"], "| is_last 块数 =",
          counters["last"])
    print("finish reason 记录 =", counters["reason"])
    print(
        "结论（实测）：CancelledError 被 ChatModelBase._stream 里的 "
        "`except asyncio.CancelledError` 吞掉了，它把已经攒到的内容"
        "build 成一个 is_last=True、finished_reason=interrupted 的块 yield "
        "出去，然后生成器正常结束。消费者因此拿到一个「干净收尾」而不是异常。",
    )


if __name__ == "__main__":
    asyncio.run(main())
