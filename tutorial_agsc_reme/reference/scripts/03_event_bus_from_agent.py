# -*- coding: utf-8 -*-
"""第 3 讲验证脚本之二：**真实 Agent** 的事件流 → 事件总线 → JSONL 落盘。

与 `03_events_and_state.py` 的分工：那一份的事件流是**手工构造**的（0 次 LLM，
用来把契约里的每条不变式钉死）；这一份跑的是**真家伙** ——
真正 ``agentscope.agent.Agent`` + 真正 ``DeepSeekChatModel`` + 真正 ``Toolkit``，
一路 ``reply_stream`` 打进来，看 ``StreamTranslator`` 能不能把真实事件流
翻译成契约要求的记录。

它顺带实证了本讲最值钱的一个结论：
**一次 ``reply_stream`` 会停下来（park）**。工具的权限默认是 ``ASK``
（``third_party/agentscope/src/agentscope/tool/_adapters.py:132``），
所以第一次调用只走到 ``RequireUserConfirmEvent`` 就结束了流；
把 ``UserConfirmResultEvent`` 再喂回 ``reply_stream`` 才会继续跑工具。
事件总线因此必须能**跨两次调用**保持 seq 连续 —— 这就是
``StreamTranslator.seek()`` 存在的理由。

用法（``PYTHONPATH`` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/03_event_bus_from_agent.py

LLM 调用预算：**2 次**（每次 reply 迭代一次模型调用），低于单脚本 6 次上限。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import harness_kit
from loguru import logger

REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=False)

from agentscope.agent import Agent  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.event import ConfirmResult, UserConfirmResultEvent  # noqa: E402
from agentscope.message import TextBlock, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, ToolChunk, Toolkit  # noqa: E402

from harness_kit.events import EventBus, EventRecord  # noqa: E402
from harness_kit.events.translate import StreamTranslator  # noqa: E402
from harness_kit.session.models import (  # noqa: E402
    SessionEvent,
    check_seq_invariants,
    next_seq,
)

SESSION = "sess_real_agent"
OUT = Path("/tmp/lesson03_real_events.jsonl")


def get_time(city: str) -> ToolChunk:
    """查询指定城市的当前时间。

    Args:
        city (`str`): 城市名，例如 "Beijing"。

    Returns:
        `ToolChunk`: 一句话说明时间。
    """
    # 固定值：本脚本要的是"事件形状"，不是真实时钟（真实时区查询属于第 7 讲的工具）。
    return ToolChunk(
        content=[TextBlock(text=f"{city} 当前时间为 2026-09-22 09:30（UTC+8）。")],
        state="success",
    )


class JsonlSink:
    """订阅者：把每条事件记录追加写进一个 JSONL 文件（立即 fsync 语义）。"""

    def __init__(self, path: Path) -> None:
        """初始化。

        Args:
            path (`Path`): 输出文件路径。
        """
        self.path = path
        self.records: list[EventRecord] = []
        self._handle = path.open("w", encoding="utf-8")

    async def __call__(self, record: EventRecord) -> None:
        """落地一条记录。

        Args:
            record (`EventRecord`): 事件记录。
        """
        self.records.append(record)
        self._handle.write(SessionEvent.wrap(record).to_json_line() + "\n")
        self._handle.flush()

    def close(self) -> None:
        """关闭文件句柄。"""
        self._handle.close()


def build_agent(api_key: str, base_url: str, model_name: str) -> Agent:
    """用**官方** AgentScope 组件装出一个 Agent（不自己写任何 Loop / 模型 / 工具）。

    用到的真实 API：

    - ``agentscope.agent.Agent`` —— ``third_party/agentscope/src/agentscope/agent/_agent.py:120``
    - ``agentscope.model.DeepSeekChatModel`` —— ``third_party/agentscope/src/agentscope/model/_deepseek/_model.py:26``
    - ``agentscope.credential.DeepSeekCredential`` —— ``third_party/agentscope/src/agentscope/credential/_deepseek.py``
    - ``agentscope.tool.FunctionTool`` —— ``third_party/agentscope/src/agentscope/tool/_adapters.py:36``
    - ``agentscope.tool.Toolkit`` —— ``third_party/agentscope/src/agentscope/tool/_toolkit.py:88``

    Args:
        api_key (`str`): API Key（从环境变量读，不硬编码）。
        base_url (`str`): OpenAI 兼容端点。
        model_name (`str`): 模型名。

    Returns:
        `Agent`: 装配好的 Agent。
    """
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(api_key=api_key, base_url=base_url),
        model=model_name,
        stream=True,
        client_kwargs={"timeout": 60.0},
    )
    # FunctionTool 默认权限是 ASK（tool/_adapters.py:132），所以这里不传
    # permission —— 本脚本就是要看"停下来等人确认"这条路径。
    toolkit = Toolkit(tools=[FunctionTool(get_time)])
    return Agent(
        name="Friday",
        system_prompt=(
            "你是助手。用户问时间时，**必须**调用 get_time 工具，"
            "不要凭记忆回答。回答保持一句话。"
        ),
        model=model,
        toolkit=toolkit,
    )


async def main() -> int:
    """跑一遍真实的 park → 确认 → 继续。

    Returns:
        `int`: 全部通过返回 0。
    """
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("跳过：.env 里没有 OPENAI_API_KEY（本脚本需要真实模型）")
        return 0

    base_url = os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com"
    model_name = os.getenv("LLM_MODEL") or "deepseek-chat"
    print("模型:", model_name, "| 端点:", base_url)

    agent = build_agent(api_key, base_url, model_name)
    agent.state.session_id = SESSION

    bus = EventBus()
    sink = JsonlSink(OUT)
    bus.subscribe("*", sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=SESSION)

    # ---- 第一段：用户提问 → Agent 想调工具 → 停下来等确认 -------------------
    question = "现在北京几点？请调用 get_time 工具。"
    translator.note_input(question)
    print("\n--- 第一段 reply_stream(UserMsg) ---")
    first = await asyncio.wait_for(
        translator.consume(agent.reply_stream(UserMsg("user", question))),
        timeout=180,
    )
    await bus.drain()
    print("产出记录:", first, "| 读入事件:", translator.records_seen, "| 跳过:", translator.skipped)
    print("Agent 是否停在待确认:", agent.state.has_awaiting_tool_calls(agent.name))

    pending = [
        block
        for block in agent.state.get_unfinished_tool_calls(agent.name)
    ]
    if not pending:
        print("模型这次没有请求工具（权限路径没走到），直接结束")
        records = list(sink.records)
    else:
        pending_call = pending[0]
        print("待确认工具调用:", pending_call.id, pending_call.name, "| 状态:", pending_call.state)
        assert pending_call.state == "asking", f"期望 asking，实际 {pending_call.state}"

        # ---- 第二段：把确认结果喂回去，流从断点继续 -------------------------
        # 注意：``UserConfirmResultEvent`` 是**输入**不是**输出**，AgentScope
        # 不会把它再回吐成一条事件 —— 事件流里只有 ask、没有 allow。所以
        # "用户批准了"这条审计记录只能由调用方显式报账。
        print("\n--- 第二段 reply_stream(UserConfirmResultEvent) ---")
        # 人工确认的"答案"不在事件流里，由调用方补一条记录（见
        # harness_kit/events/translate.py:StreamTranslator.note_confirmation）。
        # 放在 resume **之前**：决策比工具执行先发生，seq 的顺序才对得上。
        await translator.note_confirmation(
            tool_names=[pending_call.name],
            confirmed=True,
            reason="脚本里扮演用户的人点了「同意」",
        )
        second = await asyncio.wait_for(
            translator.consume(
                agent.reply_stream(
                    UserConfirmResultEvent(
                        reply_id=pending_call.id,
                        confirm_results=[
                            ConfirmResult(confirmed=True, tool_call=pending_call),
                        ],
                    ),
                ),
            ),
            timeout=180,
        )
        await bus.drain()
        print("产出记录:", second, "| 累计读入事件:", translator.records_seen)
        records = list(sink.records)

    await bus.aclose()
    sink.close()

    # ---- 审计记录 -----------------------------------------------------------
    print("\n--- 事件记录（seq 跨两段连续）---")
    for record in records:
        payload = json.dumps(record.payload, ensure_ascii=False)
        if len(payload) > 110:
            payload = payload[:110] + "..."
        print(f"  seq={record.seq:02d} {record.kind.value:12s} {payload}")

    kinds = [record.kind.value for record in records]
    print("\n记录条数:", len(records), "| 种类序列:", kinds)
    print("translator.stats():", translator.stats())

    check_seq_invariants(records, expected_session_id=SESSION)
    assert next_seq(records) == len(records), "seq 必须无洞"
    assert kinds.count("reply_start") == 1, "一次 reply 只应有一条 reply_start"
    assert kinds[-1] == "reply_end", "最后一条必须是 reply_end"
    assert "tool_call" in kinds and "tool_result" in kinds, "这次对话应该真的调了工具"
    assert "model_call" in kinds

    behaviours = [
        record.payload["behavior"]
        for record in records
        if record.kind.value == "permission"
    ]
    print("PERMISSION 行为序列:", behaviours, "（ask 来自事件流，allow 来自调用方报账）")
    assert behaviours == ["ask", "allow"], "撤销 note_confirmation 就只剩 ask —— 这正是要补的洞"

    # ---- JSONL 往返 ---------------------------------------------------------
    lines = [line for line in OUT.read_text(encoding="utf-8").splitlines() if line]
    back = [SessionEvent.model_validate(json.loads(line)) for line in lines]
    check_seq_invariants(back, expected_session_id=SESSION)
    print("\n落盘文件:", OUT, "| 行数:", len(lines), "| 字节:", OUT.stat().st_size)
    print("JSONL 往返无损:", back == [SessionEvent.wrap(r) for r in records])
    assert back == [SessionEvent.wrap(r) for r in records]

    final = agent.state.context[-1]
    print("\nAgentState 尾部消息:", final.role, "| 块:", [b.type for b in final.content])
    assert final.role == "assistant"

    print("\n全部断言通过：真实事件流 → 事件总线 → JSONL，seq 跨 park 连续")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
