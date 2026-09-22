# -*- coding: utf-8 -*-
"""侦察脚本 08：完全离线地对比 DeepSeek / OpenAI / DashScope 三个 formatter
对同一份 Msg 列表的输出，并演示 _StreamAccumulator 的拼装逻辑。

无需任何 API key，纯本地运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
    /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/08_formatter_compare.py
"""
import asyncio
import base64
import json

from agentscope.formatter import (
    DashScopeChatFormatter,
    DashScopeMultiAgentFormatter,
    DeepSeekChatFormatter,
    OpenAIChatFormatter,
    OpenAIMultiAgentFormatter,
)
from agentscope.message import (
    AssistantMsg,
    Base64Source,
    DataBlock,
    HintBlock,
    Msg,
    SystemMsg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import ChatResponse, FinishedReason
from agentscope.model._utils import _StreamAccumulator


def build_messages() -> list[Msg]:
    """构造一份覆盖全部 block 类型的对话历史。"""
    # 1x1 红色 PNG 的 base64
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
        "nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    return [
        SystemMsg(name="system", content=[TextBlock(text="你是助手。")]),
        UserMsg(name="alice", content=[TextBlock(text="北京天气?")]),
        AssistantMsg(
            name="agent",
            content=[
                ThinkingBlock(thinking="先查天气工具。"),
                TextBlock(text="我来查。"),
                ToolCallBlock(
                    id="call_1",
                    name="get_weather",
                    input='{"city": "北京"}',
                ),
                # 关键：ToolResultBlock 和 ToolCallBlock 在**同一条 assistant
                # 消息**里（AgentScope 的 AgentState.append_context 就是这么做的），
                # 所以 formatter 必须在遇到 tool_result 时「把之前的 content 先
                # flush 成一条 assistant 消息」，再单独发 tool 消息。
                ToolResultBlock(
                    id="call_1",
                    name="get_weather",
                    output=[
                        TextBlock(text="25C 晴"),
                        DataBlock(
                            source=Base64Source(
                                data=png_b64,
                                media_type="image/png",
                            ),
                            name="weather.png",
                        ),
                    ],
                ),
            ],
        ),
        UserMsg(
            name="user",
            content=[
                TextBlock(text="再看这张图"),
                DataBlock(
                    source=Base64Source(
                        data=png_b64,
                        media_type="image/png",
                    ),
                    name="fig.png",
                ),
            ],
        ),
        # HintBlock 是 Harness 内部注入的「提示」，formatter 要把它变成一条
        # 独立的 user 消息（assistant 消息里可以放 HintBlock）
        AssistantMsg(
            name="system-reminder",
            content=[
                HintBlock(hint="<system-reminder>还剩 3 轮</system-reminder>"),
            ],
        ),
    ]


async def main() -> None:
    msgs = build_messages()

    for formatter in (
        DeepSeekChatFormatter(),
        OpenAIChatFormatter(),
        DashScopeChatFormatter(),
    ):
        print("\n" + "#" * 78)
        print("#", type(formatter).__name__)
        print("#  input_types =", formatter.input_types)
        print("#  supported_input_media_types =",
              formatter.supported_input_media_types)
        print("#" * 78)
        payload = await formatter.format(msgs)
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
        print("--- 共", len(payload), "条 provider message")

    # ---------------- 多 Agent formatter ----------------
    print("\n" + "#" * 78)
    print("# 多 Agent 场景：agent_message 被压进 <history> 标签")
    print("#" * 78)
    multi = [
        SystemMsg(name="system", content=[TextBlock(text="你是 Bob。")]),
        UserMsg(name="alice", content=[TextBlock(text="你好")]),
        AssistantMsg(name="bob", content=[TextBlock(text="你好呀")]),
        UserMsg(name="alice", content=[TextBlock(text="天气?")]),
    ]
    for f in (OpenAIMultiAgentFormatter(), DashScopeMultiAgentFormatter()):
        print("\n--", type(f).__name__)
        print(json.dumps(await f.format(multi), ensure_ascii=False, indent=2))

    # ---------------- StreamAccumulator 离线演示 ----------------
    print("\n" + "#" * 78)
    print("# _StreamAccumulator：把增量 delta 拼成最终 ChatResponse")
    print("#" * 78)
    acc = _StreamAccumulator()
    deltas = [
        ChatResponse(
            content=[TextBlock(id="t1", text="你")],
            is_last=False,
            id="resp",
        ),
        ChatResponse(
            content=[
                TextBlock(id="t1", text="好"),
                ThinkingBlock(id="k1", thinking="想"),
            ],
            is_last=False,
            id="resp",
        ),
        ChatResponse(
            content=[
                ThinkingBlock(id="k1", thinking="一下"),
                ToolCallBlock(id="c1", name="get_weather", input='{"ci'),
            ],
            is_last=False,
            id="resp",
        ),
        ChatResponse(
            content=[ToolCallBlock(id="c1", name="", input='ty": "北京"}')],
            is_last=False,
            id="resp",
        ),
        ChatResponse(content=[], is_last=False, id="resp"),  # 空载体 chunk
    ]
    for d in deltas:
        acc.append_chat_response(d)
    final = acc.build()
    print("最终 ChatResponse.is_last =", final.is_last)
    print("finished_reason =", final.finished_reason)
    for b in final.content:
        print("  -", type(b).__name__, "id=", b.id, "|", b.model_dump())
    print("\n注意：最后那个空 content 的 delta 被吸收了 metadata，但没有产生任何块。")
    print("FinishedReason 取值 =", list(FinishedReason))


if __name__ == "__main__":
    asyncio.run(main())
