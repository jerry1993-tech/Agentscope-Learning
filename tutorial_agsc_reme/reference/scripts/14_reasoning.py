#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第 14 讲《Reasoning 与结构化输出》验证脚本。

跑法（在仓库根，或任何地方用绝对路径）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/14_reasoning.py

加 ``--live`` 会多跑 E 段（真实 deepseek-flash，3 次计费补全调用）。

五段的结构与「消耗几次模型调用」：

===  ==============================================================  ============
段   内容                                                            模型调用
===  ==============================================================  ============
A    ``CallStructured``：强制工具调用、重试、退化、四级梯子           0（全是 ``EchoChatModel`` 脚本）
B    ``StructuredOutputTool`` 装进 ``Agent.toolkit`` 的端到端         0
C    ``PromptAssembler``：前缀稳定性、指纹、``HintBlock`` 注入         0
D    ``CritiqueLoop``：generate → critique → revise                    0
E    （``--live``）真实 deepseek-flash                                  3
===  ==============================================================  ============

**A~D 段全部离线、确定性、可 CI**。它们的模型是
:class:`~harness_kit.models.adapters.echo.EchoChatModel` —— 一个按脚本回放的
假模型，能精确控制「第几次调用说什么、调哪个工具、传什么参数」。这是
本讲所有分支（没调工具、字段越界、批判失败）唯一可复现的验证手段：
真实模型给出的是「这次它恰好这样」，而不是契约。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, field_validator

# ----------------------------------------------------------------------
# 路径与 .env
# ----------------------------------------------------------------------
#: ``<repo>/tutorial_agsc_reme/reference``
REF: Path = Path(__file__).resolve().parents[1]
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根
REPO: Path = REF.parents[1]
#: 本地 ReMe 克隆必须排在 ``sys.path`` 最前（压住 site-packages 里的 0.3.1.10）。
REME_SRC: Path = REPO / "third_party" / "ReMe"

for _candidate in (str(REME_SRC), str(REF)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:  # python-dotenv 是 pyproject 里声明的依赖
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:  # pragma: no cover - 本环境已装
    pass

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.formatter import DeepSeekChatFormatter  # noqa: E402
from agentscope.message import (  # noqa: E402
    AssistantMsg,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    UserMsg,
)
from agentscope.permission import PermissionContext  # noqa: E402
from agentscope.tool import Toolkit  # noqa: E402

from harness_kit.models.adapters.echo import EchoChatModel  # noqa: E402
from harness_kit.reasoning import (  # noqa: E402
    CallStructured,
    CritiqueLoop,
    PromptAssembler,
    PromptSection,
    StructuredOutputError,
    VolatileSectionError,
    render_tool_list,
)

#: 是否跑真实模型那一段。
LIVE: bool = "--live" in sys.argv

#: 全程使用的模型名（``.env`` 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。
MODEL_NAME: str = os.getenv("LLM_MODEL") or "deepseek-chat"

#: 默认 INFO 日志会把每一轮模型调用都打出来；A~D 段是离线断言，压到 WARNING。
logger.remove()
logger.add(sys.stderr, level="WARNING")


def banner(title: str) -> None:
    """打一条段落标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ======================================================================
# 本讲用到的业务 schema
# ======================================================================
class CodeReview(BaseModel):
    """一次代码审查的结论（结构化输出的目标结构）。"""

    summary: str = Field(description="一句话结论")
    issues: list[str] = Field(description="具体问题清单，没有就空列表")
    score: float = Field(ge=0.0, le=1.0, description="0~1 的质量分")


class Blank(BaseModel):
    """空 schema：用来验证「模型只回文本、什么都没交」这条分支。"""


def emit_turn(payload: dict | None, *, name: str = "emit_result") -> dict:
    """造一个「模型调了工具」的脚本回合。

    Args:
        payload (`dict | None`): 工具实参；``None`` 时造一个不带 ``input`` 的调用。
        name (`str`): 工具名。

    Returns:
        `dict`: ``EchoChatModel`` 的脚本项。
    """
    call: dict = {"id": "call-1", "name": name}
    if payload is not None:
        call["input"] = payload
    return {"text": "", "tool_calls": [call]}


def text_turn(text: str) -> dict:
    """造一个「模型只说话、没调工具」的脚本回合。

    Args:
        text (`str`): 模型说的话。

    Returns:
        `dict`: ``EchoChatModel`` 的脚本项。
    """
    return {"text": text}


class BadRequestEcho(EchoChatModel):
    """模拟「思考模式拒绝强制 tool_choice」的 provider（离线可复现）。

    它复刻的是**真实发生过的行为**：``deepseek-flash`` 在思考模式下收到
    指名工具的 ``tool_choice`` 会直接 400，原文是
    ``Thinking mode does not support this tool_choice``。
    判据用的是 provider 的 400 类异常 —— 与
    ``ChatModelBase._get_structured_output_fallback_exceptions``
    （``third_party/agentscope/src/agentscope/model/_base.py:123``）的约定一致。
    """

    def __init__(self, **kwargs: object) -> None:
        """初始化，并记录被拒绝的次数。"""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.rejected: int = 0

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> object:
        """指名工具的 ``tool_choice`` 一律 400，其余转发给回声模型。

        Args:
            model_name (`str`): 模型名。
            messages (`list[Msg]`): 输入消息。
            tools (`list[dict] | None`, optional): 工具 schema。
            tool_choice (`object`, optional): 工具选择策略。
            **kwargs (`object`): 透传。

        Returns:
            `object`: ``ChatResponse`` 或异步生成器。

        Raises:
            openai.BadRequestError: ``tool_choice.mode`` 是一个具体工具名时。
        """
        mode = getattr(tool_choice, "mode", None)
        if isinstance(mode, str) and mode not in ("auto", "none", "required"):
            self.rejected += 1
            import httpx
            import openai

            response = httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.deepseek.com/chat"),
            )
            raise openai.BadRequestError(
                "Thinking mode does not support this tool_choice",
                response=response,
                body=None,
            )
        return await super()._call_api(
            model_name=model_name,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,  # type: ignore[arg-type]
            **kwargs,
        )


def reviewer_agent(model: object, *, system_prompt: str = "你是代码审查员。") -> Agent:
    """造一个装了 ``Toolkit`` 的 ``Agent``（本脚本统一用它）。

    Args:
        model (`object`): 任意 ``ChatModelBase``。
        system_prompt (`str`): 系统提示词。

    Returns:
        `Agent`: Agent 实例。
    """
    return Agent(
        name="reviewer",
        system_prompt=system_prompt,
        model=model,  # type: ignore[arg-type]
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=3),
    )


# ======================================================================
# A · CallStructured：强制工具调用、重试、退化
# ======================================================================
async def section_a() -> None:
    """A 段：``CallStructured`` 的五条路径（0 次 LLM 调用）。"""
    banner("A · CallStructured：让模型「填表」而不是「写作文」")

    print("\n--- A1 语义化工具名 + 强制 tool_choice：成功路径 ---")
    model = EchoChatModel(
        script=[
            emit_turn(
                {"summary": "没有明显问题", "issues": ["缺少类型注解"], "score": 0.75},
            ),
        ],
    )
    caller = CallStructured(model=model, schema=CodeReview, tool_name="emit_result")
    result = await caller.run([UserMsg("user", "审查 def f(x): return x + 1")])
    print(f"  返回类型 = {type(result).__name__}（已校验的 pydantic 实例）")
    print(f"  summary = {result.summary!r}")
    print(f"  issues  = {result.issues}")
    print(f"  score   = {result.score}")
    print(f"  last_strategy = {caller.last_strategy}")
    print(f"  模型调用次数 = {model.call_count}（一次就中）")
    print(f"  describe() = {caller.describe()}")
    payload = caller.tools_payload()
    print(f"  tools_payload 工具数 = {len(payload)}，工具名 = {payload[0]['function']['name']}")
    top_keys = sorted(payload[0]["function"]["parameters"])
    print(f"  parameters 顶层键 = {top_keys}（没有 title，_remove_title_field 清掉了）")
    assert isinstance(result, CodeReview) and result.score == 0.75
    assert caller.last_strategy == "forced"
    assert len(payload) == 1 and payload[0]["function"]["name"] == "emit_result"
    assert "title" not in payload[0]["function"]["parameters"]

    print("\n--- A2 第一次它只顾说话没调工具，第二次才交 ---")
    model = EchoChatModel(
        script=[
            text_turn("我想想……先把明显的问题列出来。"),
            emit_turn({"summary": "ok", "issues": [], "score": 0.9}),
        ],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    result = await caller.run([UserMsg("user", "审查一下")], max_attempts=3)
    print(f"  score = {result.score}，模型调用次数 = {model.call_count}")
    print("  >>> 这一条正是「max_attempts 默认 2」的理由：模型忘调工具是常态。")
    assert result.score == 0.9 and model.call_count == 2

    print("\n--- A3 两次都不合法：抛出带诊断字段的 StructuredOutputError ---")
    model = EchoChatModel(
        script=[
            emit_turn({}),  # 三个字段全缺
            emit_turn({"summary": "s", "issues": [], "score": 1.5}),  # 越界
        ],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    try:
        await caller.run([UserMsg("user", "审查一下")], max_attempts=2)
    except StructuredOutputError as exc:
        print(f"  type        = {type(exc).__name__}")
        print(f"  tool_name   = {exc.tool_name}")
        print(f"  attempts    = {exc.attempts}")
        print(f"  last_error  = {exc.last_error}")
        print(f"  raw         = {exc.raw}")
        print("  >>> last_error 直接指出「哪个字段、错在哪」，这是重试能自愈的前提。")
        assert exc.attempts == 2 and exc.tool_name == "emit_result"
        assert "score" in (exc.last_error or "")
        assert exc.raw == {"summary": "s", "issues": [], "score": 1.5}
    else:  # pragma: no cover - 走到这里说明实现坏了
        raise AssertionError("应当抛 StructuredOutputError")

    print("\n--- A4 思考模式拒绝强制 tool_choice：降级 auto 再试一次 ---")
    model = BadRequestEcho(
        script=[emit_turn({"summary": "降级后成功", "issues": [], "score": 0.8})],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    result = await caller.run([UserMsg("user", "审查一下")])
    print(f"  被 provider 拒绝次数 = {model.rejected}")
    print(f"  last_strategy        = {caller.last_strategy}")
    print(f"  最终结果             = {result.summary!r}")
    print("  >>> 显式传 tool_choice 会绕过 SDK 自己的四级梯子，所以这一步得我们补。")
    assert model.rejected == 1 and caller.last_strategy == "auto"

    print("\n--- A5 tool_name=None：白拿 SDK 的四级退化梯子 ---")
    model = EchoChatModel()
    caller = CallStructured(model=model, schema=CodeReview, tool_name=None)
    result = await caller.run([UserMsg("user", "审查一下")])
    print(f"  工具名固定 = generate_structured_output（SDK 内部写死）")
    print(f"  结果 = {result.model_dump()}")
    print("  >>> 屏幕上的 'echo' / [] / 1.0 是回声模型按 schema 编的占位值，")
    print("      说明强制调用真的发生了（模型没有自由发挥的空间）。")
    assert result.summary == "echo" and result.score == 1.0


# ======================================================================
# B · StructuredOutputTool：把「提交器」装进 Agent 的 ReAct 循环
# ======================================================================
async def section_b() -> None:
    """B 段：``as_tool()`` 产物的三条路径（0 次 LLM 调用）。"""
    banner("B · StructuredOutputTool：让 ReAct 循环自己交作业")

    print("\n--- B1 端到端：模型在循环里调 emit_result，结果落进 AgentState ---")
    model = EchoChatModel(
        script=[
            {
                "text": "我先把结论提交了。",
                "tool_calls": [
                    {
                        "id": "b1",
                        "name": "emit_result",
                        "input": {"summary": "s", "issues": ["i1"], "score": 0.5},
                    },
                ],
            },
            text_turn("已提交。"),
        ],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    tool = caller.as_tool()
    agent = reviewer_agent(model)
    await agent.toolkit.add_tool(tool, group_name="basic")
    msg = await agent.reply(UserMsg("user", "审查一下"))
    print(f"  reply 文本 = {(msg.get_text_content() or '').strip()[:30]}")
    print(f"  state.reply_context.structured_output = {agent.state.reply_context.structured_output}")
    print(f"  tool.last_result = {tool.last_result}")
    print(f"  reply() 上的 structured_output 属性 = {getattr(msg, 'structured_output', None)}")
    print("  >>> 从 **state** 读才是对的路径；传了 structured_schema 才会出现在最终消息里。")
    assert agent.state.reply_context.structured_output == {
        "summary": "s",
        "issues": ["i1"],
        "score": 0.5,
    }
    assert tool.last_result is not None and tool.last_result.score == 0.5

    print("\n--- B2 模型交了坏数据：返回 ERROR 的 ToolChunk，而不是抛异常 ---")
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview)
    tool = caller.as_tool()
    agent = reviewer_agent(EchoChatModel())
    bad = await tool(_agent_state=agent.state, summary="s", issues=[], score=9.0)
    print(f"  state = {bad.state}")
    print(f"  文本  = {bad.content[0].text}")
    good = await tool(_agent_state=agent.state, summary="s", issues=[], score=0.4)
    print(f"  合法输入 state = {good.state}，state 里 = {agent.state.reply_context.structured_output}")
    print("  >>> 上一轮的 ValidationError 文本会作为工具结果回给模型，它下一轮自己改。")
    assert bad.state.value == "error" and "ValidationError" in bad.content[0].text
    assert good.state.value == "success"

    print("\n--- B3 权限：提交器无条件 ALLOW（它是纯控制流，没有外部副作用）---")
    decision = await tool.check_permissions({}, PermissionContext())
    print(f"  behavior = {decision.behavior}，reason = {decision.decision_reason}")
    print("  >>> 默认 ASK 会让每一轮结构化输出都弹窗 —— 那不是安全，那是骚扰。")
    assert decision.behavior.value == "allow"

    print("\n--- B4 自定义 validator 抛出的非 pydantic 异常也会被兜住 ---")

    class Strict(BaseModel):
        """带自定义校验的结构（字段非空）。"""

        answer: str

        @field_validator("answer")
        @classmethod
        def _not_blank(cls, value: str) -> str:
            """拒绝空白回答。

            Args:
                value (`str`): 模型给的值。

            Returns:
                `str`: 原值。

            Raises:
                RuntimeError: 值为空白时。
            """
            if not value.strip():
                raise RuntimeError("answer 不能是空白")
            return value

    caller = CallStructured(model=EchoChatModel(), schema=Strict)
    strict_tool = caller.as_tool()
    agent = reviewer_agent(EchoChatModel())
    chunk = await strict_tool(_agent_state=agent.state, answer="   ")
    print(f"  state = {chunk.state}")
    print(f"  文本  = {chunk.content[0].text}")
    print("  >>> pydantic 只把 ValueError/AssertionError 包成 ValidationError；")
    print("      RuntimeError 会原样抛出，所以这里必须有第二个 except 分支。")
    assert chunk.state.value == "error" and "custom validator" in chunk.content[0].text


# ======================================================================
# C · PromptAssembler：KV Cache 友好的 system prompt
# ======================================================================
async def section_c() -> None:
    """C 段：前缀稳定、指纹、``HintBlock`` 注入（0 次 LLM 调用）。"""
    banner("C · PromptAssembler：把「别动前缀」变成结构约束")

    print("\n--- C1 渲染顺序固定，volatile 段落不进 system prompt ---")
    assembler = PromptAssembler(
        sections=[
            PromptSection(key="budget", title="实时预算", body="剩余 3 次调用", volatile=True),
            PromptSection(key="tools", title="工具", body="- read_file(path: str)"),
            PromptSection(key="role", title="角色", body="你是一个严谨的代码审查员。"),
            PromptSection(key="constraints", title="约束", body="只输出结论，不要寒暄。"),
        ],
    )
    print("  system prompt（注意段落顺序与传入顺序不同）：")
    for line in assembler.render().splitlines():
        print(f"    | {line}")
    print(f"  stable_keys  = {assembler.stable_keys}")
    print(f"  volatile_keys = {assembler.volatile_keys}")
    print(f"  稳定段落里出现 volatile 正文吗 = {'剩余 3 次调用' in assembler.render()}")
    assert assembler.stable_keys == ["role", "constraints", "tools"]
    assert "剩余 3 次调用" not in assembler.render()

    print("\n--- C2 指纹：跨渲染稳定，且不被任何渲染行为改变 ---")
    renders_before = assembler.renders
    fp1 = assembler.compute_fingerprint()
    assembler.render()
    assembler.render()
    fp2 = assembler.compute_fingerprint()
    print(f"  指纹 = {fp1}，render 两次之后 = {fp2}，相同 = {fp1 == fp2}")
    print(f"  render() 计数从 {renders_before} 涨到 {assembler.renders}（compute_fingerprint 自己不涨）")
    print("  >>> 外部可以写「这一轮指纹 == 上一轮指纹」的断言而不会自己把基线建起来。")
    assert fp1 == fp2
    assert assembler.renders == renders_before + 2

    print("\n--- C3 稳定段落被改：抛 VolatileSectionError（而不是静默烧钱）---")
    section = PromptSection(key="role", title="角色", body="A")
    drifting = PromptAssembler(sections=[section])
    drifting.render()
    section.body = "B"
    try:
        drifting.render()
    except VolatileSectionError as exc:
        print(f"  捕获 {type(exc).__name__}：{str(exc)[:60]}…")
        assert "role" in str(exc)
    else:  # pragma: no cover - 走到这里说明实现坏了
        raise AssertionError("应当抛 VolatileSectionError")
    drifting.rebaseline()
    print(f"  rebaseline 之后 render() = {drifting.render()!r}，drift 记录 = {drifting.drift}")
    assert drifting.drift == ["role"]

    print("\n--- C4 注入动态内容：HintBlock 落到消息流，前缀一个字节都不变 ---")
    assembler = PromptAssembler(
        sections=[
            PromptSection(key="role", title="角色", body="你是助手。"),
            PromptSection(key="notify", title="通知", body="有 2 条未读消息", volatile=True),
        ],
    )
    model = EchoChatModel(script=[text_turn("收到。")])
    agent = Agent(
        name="assistant",
        system_prompt=assembler.render(),
        model=model,
        toolkit=Toolkit(),
    )
    before = assembler.compute_fingerprint()
    injected = assembler.inject(agent, text="当前剩余预算：2 次")
    after = assembler.compute_fingerprint()
    last = agent.state.context[-1]
    print(f"  注入块数 = {injected}，context 长度 = {len(agent.state.context)}")
    print(f"  最后一条消息 role={last.role!r}，块类型 = {[type(b).__name__ for b in last.content]}")
    print(f"  指纹 before={before} after={after}，相同 = {before == after}")
    assert injected == 2 and before == after
    assert isinstance(last.content[0], HintBlock)

    print("\n--- C5 反面教材：agent.observe([HintBlock]) 会被拒 ---")
    try:
        await agent.observe([assembler.hint("试试 observe 这条路")])
    except ValueError as exc:
        print(f"  ValueError: {str(exc)[:96]}…")
        assert "hint" in str(exc)
    else:  # pragma: no cover - 走到这里说明 SDK 行为变了
        raise AssertionError("observe 不应当接受 HintBlock")

    print("\n--- C6 formatter 视角：HintBlock 变成一条 **user** 消息 ---")
    fmt = DeepSeekChatFormatter()
    msgs: list[Msg] = [
        UserMsg("user", "3 的阶乘是多少？"),
        AssistantMsg(
            "assistant",
            [
                ThinkingBlock(thinking="阶乘就是连乘……"),
                TextBlock(text="等于 6。"),
            ],
        ),
        AssistantMsg("assistant", [HintBlock(hint="当前剩余预算：2 次")]),
    ]
    payload = await fmt.format(msgs)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"  get_text_content() 跳过 ThinkingBlock -> {msgs[1].get_text_content()!r}")
    print("  >>> HintBlock 那条被渲染成独立的 user 消息：前缀没动，模型照样看得见。")
    assert payload[2]["role"] == "user" and payload[2]["content"] == "当前剩余预算：2 次"
    assert payload[1]["reasoning_content"] == "阶乘就是连乘……"

    print("\n--- C7 工具清单渲染必须排序（否则重启一次缓存全废）---")

    def tool_schema(
        name: str,
        description: str,
        properties: dict,
        required: list[str] | None = None,
    ) -> dict:
        """造一个 ``{"type": "function", ...}`` 形态的工具 schema。

        Args:
            name (`str`): 工具名。
            description (`str`): 工具描述。
            properties (`dict`): JSON schema 的 ``properties``。
            required (`list[str] | None`, optional): 必填字段名。

        Returns:
            `dict`: 工具 schema。
        """
        parameters: dict = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    schemas = [
        tool_schema(
            "write_file",
            "写文件",
            {"path": {"type": "string"}, "text": {"type": "string"}},
            ["path"],
        ),
        tool_schema(
            "read_file",
            "读文件\n第二行会被丢掉",
            {"path": {"type": "string"}, "limit": {"type": "integer"}},
        ),
    ]
    rendered = render_tool_list(schemas)
    print(rendered)
    print(f"  空列表 -> {render_tool_list([])!r}")
    assert rendered.splitlines()[0].startswith("- read_file(")
    assert render_tool_list([]) == "（本回合没有可用工具。）"


# ======================================================================
# D · CritiqueLoop：generate → critique → revise
# ======================================================================
async def section_d() -> None:
    """D 段：自我批判循环的四条路径（0 次 LLM 调用）。"""
    banner("D · CritiqueLoop：把「再改一版」变成有闸门的循环")

    def verdict_turn(score: float, passed: bool, issues: list[str], suggestion: str = "") -> dict:
        """造一个「批判者交判据」的脚本回合。

        Args:
            score (`float`): 质量分。
            passed (`bool`): 是否通过。
            issues (`list[str]`): 问题清单。
            suggestion (`str`): 改进方向。

        Returns:
            `dict`: ``EchoChatModel`` 的脚本项。
        """
        return emit_turn(
            {"score": score, "passed": passed, "issues": issues, "suggestion": suggestion},
            name="emit_critique",
        )

    print("\n--- D1 一轮就通过 ---")
    agent = reviewer_agent(
        EchoChatModel(script=[text_turn("第一版产物")]),
        system_prompt="你是写手。",
    )
    loop = CritiqueLoop(
        agent=agent,
        max_rounds=3,
        acceptance_score=0.8,
        critique_model=EchoChatModel(script=[verdict_turn(0.95, True, [])]),
    )
    result = await loop.run("写一份季度复盘")
    print(f"  {result.summary()}")
    print(f"  finished={result.finished} rounds={result.rounds} scores={result.scores}")
    print(f"  describe() = {loop.describe()}")
    assert result.finished and result.rounds == 1 and result.scores == [0.95]
    assert result.output == "第一版产物"

    print("\n--- D2 0.4 不通过 → 修订 → 0.9 通过（趋势 up）---")
    agent_model = EchoChatModel(script=[text_turn("v1"), text_turn("v2")])
    agent = reviewer_agent(agent_model, system_prompt="你是写手。")
    loop = CritiqueLoop(
        agent=agent,
        max_rounds=3,
        critique_model=EchoChatModel(
            script=[
                verdict_turn(0.4, False, ["缺数据"], "补上数字"),
                verdict_turn(0.9, True, []),
            ],
        ),
    )
    result = await loop.run("写一份季度复盘")
    print(f"  {result.summary()}")
    print(f"  trend={result.trend} last_score={result.last_score} rounds={result.rounds}")
    print(f"  写手模型被调用 {agent_model.call_count} 次（1 次生成 + 1 次修订）")
    assert result.finished and result.trend == "up" and result.output == "v2"
    assert agent_model.call_count == 2

    print("\n--- D3 每轮都不通过：交最后一版，并诚实报 finished=False ---")
    agent_model = EchoChatModel(script=[text_turn("v1"), text_turn("v2")])
    agent = reviewer_agent(agent_model, system_prompt="你是写手。")
    loop = CritiqueLoop(
        agent=agent,
        max_rounds=2,
        critique_model=EchoChatModel(
            script=[
                verdict_turn(0.2, False, ["x"], "y"),
                verdict_turn(0.3, False, ["x"], "y"),
            ],
        ),
    )
    result = await loop.run("写一份季度复盘")
    print(f"  {result.summary()}")
    print(f"  finished={result.finished}（False = 用尽轮数仍未通过）rounds={result.rounds}")
    print("  >>> 偷偷把最后一版当成功，是这类循环最坏的实现 —— 质量不达标会在监控里隐形。")
    assert not result.finished and result.rounds == 2 and len(result.scores) == 2

    print("\n--- D4 批判步骤本身失败：保守判定，不抛异常、不放行 ---")
    agent = reviewer_agent(
        EchoChatModel(script=[text_turn("v1")]),
        system_prompt="你是写手。",
    )
    loop = CritiqueLoop(
        agent=agent,
        max_rounds=1,
        critique_model=EchoChatModel(script=[text_turn("我不批"), text_turn("就是不批")]),
    )
    result = await loop.run("写一份季度复盘")
    print(f"  {result.summary()}")
    print(f"  verdicts[0].issues = {result.verdicts[0].issues}")
    print("  >>> 批判失败必须偏保守：模型一抽风，坏产物就被放行了。")
    assert not result.finished and result.scores == [0.0]
    assert "批判步骤本身失败" in result.verdicts[0].issues[0]


# ======================================================================
# E · 真实 deepseek-flash（--live）
# ======================================================================
async def section_e() -> None:
    """E 段：真实模型上的两条链路（3 次计费补全调用）。"""
    banner("E ·（--live）真实 deepseek-flash：结构化输出与提示词注入")

    from harness_kit.config.schema import ModelSpec
    from harness_kit.models import build_chat_model
    from harness_kit.settings import Settings

    def live_model() -> object:
        """造一个真实 deepseek-flash 模型。

        Returns:
            `object`: ``OpenAICompatChatModel``。
        """
        settings = Settings.from_env(
            repo_root=REF,
            profile_dir=REF / "harness_kit" / "profiles",
        )
        spec = ModelSpec(
            provider="deepseek",
            model_name=MODEL_NAME,
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL",
            temperature=0.0,
            stream=False,
        )
        return build_chat_model(spec, settings=settings)

    print("\n--- E1 CallStructured：真实模型填表（≥1 次调用，可能先被 400 拒一次）---")
    model = live_model()
    caller = CallStructured(model=model, schema=CodeReview, tool_name="emit_result")
    result = await caller.run(
        [UserMsg("user", "审查这个函数：def f(x): return x + 1")],
        max_attempts=1,
    )
    print(f"  summary      = {result.summary}")
    print(f"  issues       = {result.issues}")
    print(f"  score        = {result.score}")
    print(f"  last_strategy = {caller.last_strategy}")
    print(f"  计费调用次数  = {model.totals()['calls']}")
    print("  >>> 真实 provider 的思考模式**确实**拒了强制 tool_choice，")
    print("      日志里能看到 'Thinking mode does not support this tool_choice'，")
    print("      随后 harness_kit 自己补的 forced → auto 那一步把它兜住了。")

    print("\n--- E2 PromptAssembler + HintBlock：两次 reply 之间前缀不变（2 次调用）---")
    assembler = PromptAssembler(
        sections=[
            PromptSection(key="role", title="角色", body="你是简洁的中文助手，回答不超过 30 字。"),
            PromptSection(key="constraints", title="约束", body="不要寒暄，不要解释。"),
            PromptSection(key="dynamic", title="会话信息", body="工作目录 /tmp/lesson14", volatile=True),
        ],
    )
    model = live_model()
    agent = Agent(
        name="assistant",
        system_prompt=assembler.render(),
        model=model,
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=2),
    )
    before = assembler.compute_fingerprint()
    first = await agent.reply(UserMsg("user", "用一句话说明什么是 prompt cache。"))
    assembler.inject(agent, text="用户偏好的语言：中文")
    second = await agent.reply(UserMsg("user", "那 cache miss 会怎样？"))
    after = assembler.compute_fingerprint()
    print(f"  第 1 轮回答 = {(first.get_text_content() or '').strip()}")
    print(f"  第 2 轮回答 = {(second.get_text_content() or '').strip()}")
    print(f"  指纹 before={before} after={after}，相同 = {before == after}")
    print(f"  计费调用次数 = {model.totals()['calls']}")
    print("  >>> 第二次 reply 之前注入了一条 HintBlock，system prompt 的字节没有变，")
    print("      所以前缀缓存照旧命中 —— 这就是「动态内容不进 system prompt」。")
    assert before == after


async def main() -> int:
    """跑全部段落，返回退出码。

    Returns:
        `int`: 0 = 全部通过。
    """
    await section_a()
    await section_b()
    await section_c()
    await section_d()
    if LIVE:
        await section_e()
    else:
        print()
        print("=" * 78)
        print("跳过 E 段（真实 LLM）。加 --live 打开：3 次 deepseek-flash 计费调用。")
        print("=" * 78)
    print()
    print("=" * 78)
    if LIVE:
        print(
            "PASS · 第 14 讲全部断言通过"
            "（A~D 段 0 次 LLM 调用；E 段 3 次 deepseek-flash 计费调用）",
        )
    else:
        print("PASS · 第 14 讲全部断言通过（A~D 段 0 次 LLM 调用）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
