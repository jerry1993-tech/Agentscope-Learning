# -*- coding: utf-8 -*-
"""第 14 讲的 pytest：结构化输出 / 自我批判 / prompt 装配。

五条纪律（延续第 13 讲，但重点不同）：

1. **0 次 LLM 调用**。三条链路的分支全部靠
   :class:`~harness_kit.models.adapters.echo.EchoChatModel` 的**脚本**驱动：
   它能精确控制「第几次调用说什么、调哪个工具、传什么参数」。真实模型
   给出的是「这次它恰好这样」，不是契约 —— 契约只能靠脚本化的假模型钉住。
   真实调用在 ``scripts/14_reasoning.py --live``（3 次 deepseek-flash）。
2. **每条「失败路径」都要有一条测试**。本讲三个模块的价值几乎全在失败路径上：
   模型没调工具、字段越界、被迫降级 ``tool_choice``、前缀漂移、批判失败。
   正向路径各一条就够，失败路径一条都不能少。
3. **provider 行为要能离线复现**。``BadRequestEcho`` 复刻的是真实发生过的 400
   （``Thinking mode does not support this tool_choice``）。没有它，「forced →
   auto」这条降级分支就只能靠真实调用去撞，撞不到就永远测不着。
4. **「不变」比「会变」更难测，也更值钱**。``compute_fingerprint`` 的核心性质是
   「渲染多少次、注入多少 hint 都不变」，所以断言写成
   ``fp_before == fp_after`` 而不是「等于某个常量」—— 后者会把实现细节抄进测试。
5. **同一个 ``PromptAssembler`` 不要跨测试复用**。它**有状态**（基线 + 漂移记录），
   共享会制造「测试顺序变了就红」的幽灵失败。每条测试都新建一个。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson14_reasoning.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, field_validator

from agentscope.agent import Agent, ReActConfig
from agentscope.formatter import DeepSeekChatFormatter
from agentscope.message import (
    AssistantMsg,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    UserMsg,
)
from agentscope.permission import PermissionContext
from agentscope.tool import Toolkit

from harness_kit.models.adapters.echo import EchoChatModel
from harness_kit.reasoning import (
    PROMPT_VERSION,
    SECTION_ORDER,
    CallStructured,
    CritiqueLoop,
    CritiqueResult,
    CritiqueVerdict,
    PromptAssembler,
    PromptSection,
    StructuredOutputError,
    StructuredOutputTool,
    VolatileSectionError,
    render_tool_list,
)
from harness_kit.reasoning import prompt as prompt_module

# ======================================================================
# 夹具与公共件
# ======================================================================


class CodeReview(BaseModel):
    """本讲所有结构化输出测试的目标结构。"""

    summary: str = Field(description="一句话结论")
    issues: list[str] = Field(description="问题清单")
    score: float = Field(ge=0.0, le=1.0, description="质量分")


class Blank(BaseModel):
    """空 schema：用于「模型只回文本」这类不依赖字段的分支。"""


def emit_turn(payload: dict | None, *, name: str = "emit_result") -> dict:
    """造一个「模型调了工具」的脚本回合。

    Args:
        payload (`dict | None`): 工具实参。
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


def verdict_turn(score: float, passed: bool, issues: list[str]) -> dict:
    """造一个「批判者交判据」的脚本回合。

    Args:
        score (`float`): 质量分。
        passed (`bool`): 是否通过。
        issues (`list[str]`): 问题清单。

    Returns:
        `dict`: ``EchoChatModel`` 的脚本项。
    """
    return emit_turn(
        {"score": score, "passed": passed, "issues": issues, "suggestion": ""},
        name="emit_critique",
    )


class BadRequestEcho(EchoChatModel):
    """模拟「思考模式拒绝强制 tool_choice」的 provider（离线可复现）。

    真实行为：``deepseek-flash`` 在思考模式下收到指名工具的 ``tool_choice``
    会直接 400，原文 ``Thinking mode does not support this tool_choice``。

    Attributes:
        rejected (`int`): 被拒绝的次数。
    """

    def __init__(self, **kwargs: Any) -> None:
        """初始化。"""
        super().__init__(**kwargs)
        self.rejected: int = 0

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Any:
        """指名工具的 ``tool_choice`` 一律 400。

        Args:
            model_name (`str`): 模型名。
            messages (`list[Msg]`): 输入消息。
            tools (`list[dict] | None`, optional): 工具 schema。
            tool_choice (`Any`, optional): 工具选择策略。
            **kwargs (`Any`): 透传。

        Returns:
            `Any`: ``ChatResponse`` 或异步生成器。

        Raises:
            openai.BadRequestError: ``tool_choice.mode`` 是具体工具名时。
        """
        mode = getattr(tool_choice, "mode", None)
        if isinstance(mode, str) and mode not in ("auto", "none", "required"):
            self.rejected += 1
            import httpx
            import openai

            raise openai.BadRequestError(
                "Thinking mode does not support this tool_choice",
                response=httpx.Response(
                    400,
                    request=httpx.Request("POST", "https://api.deepseek.com"),
                ),
                body=None,
            )
        return await super()._call_api(
            model_name=model_name,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )


class NeverFallbackEcho(BadRequestEcho):
    """provider 没声明「400 该降级」时的行为（钩子返回空元组）。"""

    @classmethod
    def _get_structured_output_fallback_exceptions(
        cls,
    ) -> tuple[type[Exception], ...]:
        """返回空元组：不降级。

        Returns:
            `tuple[type[Exception], ...]`: 空元组。
        """
        return ()


def make_agent(model: Any, *, name: str = "reviewer", system_prompt: str = "你是助手。") -> Agent:
    """造一个带 ``Toolkit`` 的 ``Agent``。

    Args:
        model (`Any`): 任意 ``ChatModelBase``。
        name (`str`): Agent 名。
        system_prompt (`str`): 系统提示词（可以直接喂 ``PromptAssembler.render()``）。

    Returns:
        `Agent`: Agent 实例。
    """
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=Toolkit(),
        react_config=ReActConfig(max_iters=3),
    )


def make_assembler() -> PromptAssembler:
    """造一个「两个稳定段落 + 一个 volatile 段落」的装配器。

    Returns:
        `PromptAssembler`: 装配器。
    """
    return PromptAssembler(
        sections=[
            PromptSection(key="role", title="角色", body="你是严谨的代码审查员。"),
            PromptSection(key="constraints", title="约束", body="只输出结论。"),
            PromptSection(key="budget", title="实时预算", body="剩余 3 次调用", volatile=True),
        ],
    )


# ======================================================================
# A · structured.py：CallStructured
# ======================================================================


def test_schema_must_be_a_model_class() -> None:
    """``schema`` 传 dict / 实例时必须立刻报错（而不是跑到一半才炸）。"""
    for bad in ({"type": "object"}, Blank()):
        with pytest.raises(ValueError, match="schema 必须是"):
            CallStructured(model=EchoChatModel(), schema=bad)  # type: ignore[arg-type]


async def test_run_rejects_empty_messages() -> None:
    """没有上下文就没有可抽取的东西 —— 空列表必须报错。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview)
    with pytest.raises(ValueError, match="messages 不能为空"):
        await caller.run([])


async def test_run_rejects_bad_max_attempts() -> None:
    """``max_attempts < 1`` 是配置错误，必须报错而不是静默按 1 跑。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview)
    with pytest.raises(ValueError, match="max_attempts 必须 >= 1"):
        await caller.run([UserMsg("user", "x")], max_attempts=0)


async def test_happy_path_returns_validated_instance() -> None:
    """正向路径：模型调了 ``emit_result``，拿到的是 pydantic 实例。"""
    model = EchoChatModel(
        script=[emit_turn({"summary": "s", "issues": ["i"], "score": 0.5})],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    result = await caller.run([UserMsg("user", "审查")])
    assert isinstance(result, CodeReview)
    assert (result.summary, result.issues, result.score) == ("s", ["i"], 0.5)
    assert caller.last_strategy == "forced"
    assert model.call_count == 1


def test_tools_payload_uses_semantic_name_and_drops_title() -> None:
    """工具名可由调用方指定；``title`` 被 ``_remove_title_field`` 清掉。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview, tool_name="emit_review")
    payload = caller.tools_payload()
    assert len(payload) == 1
    assert payload[0]["type"] == "function"
    assert payload[0]["function"]["name"] == "emit_review"
    assert "title" not in payload[0]["function"]["parameters"]
    # 每次读都返回新对象（原地修改的 _remove_title_field 不能吃掉缓存）
    assert caller.input_schema is not caller.input_schema


def test_tools_payload_falls_back_to_sdk_name() -> None:
    """``tool_name=None`` 时工具名固定为 SDK 的 ``generate_structured_output``。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview, tool_name=None)
    assert caller.tools_payload()[0]["function"]["name"] == "generate_structured_output"


async def test_retry_when_model_never_calls_the_tool() -> None:
    """第一次只回文本 → 第二次补交 —— 这正是默认 ``max_attempts=2`` 的理由。"""
    model = EchoChatModel(
        script=[text_turn("我先想想。"), emit_turn({"summary": "ok", "issues": [], "score": 0.9})],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    result = await caller.run([UserMsg("user", "审查")], max_attempts=3)
    assert result.score == 0.9
    assert model.call_count == 2


async def test_exhausted_attempts_raise_with_diagnostics() -> None:
    """用尽尝试次数后抛 ``StructuredOutputError``，且带上四个可诊断字段。"""
    model = EchoChatModel(
        script=[
            emit_turn({}),
            emit_turn({"summary": "s", "issues": [], "score": 1.5}),
        ],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    with pytest.raises(StructuredOutputError) as info:
        await caller.run([UserMsg("user", "审查")], max_attempts=2)
    exc = info.value
    assert exc.tool_name == "emit_result"
    assert exc.attempts == 2
    assert "score" in (exc.last_error or "")
    assert exc.raw == {"summary": "s", "issues": [], "score": 1.5}


async def test_model_call_failure_is_not_retried() -> None:
    """模型调用本身失败（网络/鉴权）不重试第二次「格式」，直接抛。"""
    model = EchoChatModel(fail_times=1, max_retries=0)
    caller = CallStructured(model=model, schema=CodeReview)
    with pytest.raises(StructuredOutputError) as info:
        await caller.run([UserMsg("user", "审查")], max_attempts=3)
    assert info.value.attempts == 1
    assert "调用模型时失败" in str(info.value)
    assert model.call_count == 1


async def test_run_dict_returns_json_safe_dict() -> None:
    """``run_dict`` 直接给可落盘的 dict（省掉调用方的 ``model_dump``）。"""
    model = EchoChatModel(
        script=[emit_turn({"summary": "s", "issues": [], "score": 0.3})],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    out = await caller.run_dict([UserMsg("user", "审查")])
    assert out == {"summary": "s", "issues": [], "score": 0.3}


async def test_forced_tool_choice_falls_back_to_auto() -> None:
    """provider 拒了强制 ``tool_choice`` → 降级 ``auto`` 再试一次并记在 ``last_strategy``。"""
    model = BadRequestEcho(
        script=[emit_turn({"summary": "降级成功", "issues": [], "score": 0.8})],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    result = await caller.run([UserMsg("user", "审查")])
    assert model.rejected == 1
    assert caller.last_strategy == "auto"
    assert result.summary == "降级成功"


async def test_no_fallback_when_provider_declares_nothing() -> None:
    """provider 没有声明「400 该降级」时，异常原样抛出，不做第二次调用。"""
    model = NeverFallbackEcho(script=[emit_turn({"summary": "s", "issues": [], "score": 0.8})])
    caller = CallStructured(model=model, schema=CodeReview)
    with pytest.raises(StructuredOutputError) as info:
        await caller.run([UserMsg("user", "审查")])
    assert model.rejected == 1
    assert info.value.attempts == 1


async def test_native_ladder_when_tool_name_is_none() -> None:
    """``tool_name=None`` 走 SDK 的四级梯子（工具名写死为 generate_structured_output）。"""
    model = EchoChatModel()
    caller = CallStructured(model=model, schema=CodeReview, tool_name=None)
    result = await caller.run([UserMsg("user", "审查")])
    # 回声模型按 schema 造占位实参：字符串 -> "echo"，数字 -> 1.0
    assert result.summary == "echo"
    assert result.score == 1.0


def test_describe_mentions_tool_and_schema() -> None:
    """``describe`` 是给日志/CLI 用的一行摘要。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview, tool_name="emit_x")
    text = caller.describe()
    assert "emit_x" in text and "CodeReview" in text and "forced" in text


# ======================================================================
# B · structured.py：StructuredOutputTool
# ======================================================================


async def test_as_tool_keeps_tool_name() -> None:
    """``as_tool`` 产物的名字与调用器一致，且是可重复装的独立实例。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview, tool_name="emit_review")
    first, second = caller.as_tool(), caller.as_tool()
    assert isinstance(first, StructuredOutputTool)
    assert first.name == "emit_review"
    assert first is not second
    assert first.last_result is None


async def test_tool_permission_is_unconditional_allow() -> None:
    """提交器是纯控制流：无条件 ALLOW，且写出理由（契约第 9 条）。"""
    tool = CallStructured(model=EchoChatModel(), schema=CodeReview).as_tool()
    decision = await tool.check_permissions({}, PermissionContext())
    assert decision.behavior.value == "allow"
    assert decision.decision_reason == "harness_kit.reasoning.structured.StructuredOutputTool"


async def test_tool_call_writes_structured_output_into_state() -> None:
    """校验通过：结果同时写进 ``state.reply_context.structured_output`` 与 ``last_result``。"""
    agent = make_agent(EchoChatModel())
    tool = CallStructured(model=EchoChatModel(), schema=CodeReview).as_tool()
    chunk = await tool(_agent_state=agent.state, summary="s", issues=["i"], score=0.5)
    assert chunk.state.value == "success"
    assert agent.state.reply_context.structured_output == {
        "summary": "s",
        "issues": ["i"],
        "score": 0.5,
    }
    assert tool.last_result is not None and tool.last_result.summary == "s"


async def test_tool_call_returns_error_chunk_on_validation_error() -> None:
    """校验失败返回 ERROR 的 ``ToolChunk``（模型下一轮能自己改），而不是抛异常。"""
    agent = make_agent(EchoChatModel())
    tool = CallStructured(model=EchoChatModel(), schema=CodeReview).as_tool()
    chunk = await tool(_agent_state=agent.state, summary="s", issues=[], score=9.0)
    assert chunk.state.value == "error"
    assert "ValidationError" in chunk.content[0].text
    assert "score" in chunk.content[0].text
    assert agent.state.reply_context.structured_output is None
    assert tool.last_result is None


async def test_tool_call_survives_custom_validator_exception() -> None:
    """自定义 validator 抛出的非 pydantic 异常也要被兜成 ERROR chunk。"""

    class Strict(BaseModel):
        """字段非空。"""

        answer: str

        @field_validator("answer")
        @classmethod
        def _not_blank(cls, value: str) -> str:
            """拒绝空白。

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

    agent = make_agent(EchoChatModel())
    tool = CallStructured(model=EchoChatModel(), schema=Strict).as_tool()
    chunk = await tool(_agent_state=agent.state, answer="   ")
    assert chunk.state.value == "error"
    assert "custom validator" in chunk.content[0].text


async def test_agent_reply_writes_structured_output_e2e() -> None:
    """端到端：模型在 ReAct 循环里调 ``emit_result``，结果落进 ``AgentState``。"""
    model = EchoChatModel(
        script=[
            {
                "text": "我先把结论提交了。",
                "tool_calls": [
                    {
                        "id": "b1",
                        "name": "emit_result",
                        "input": {"summary": "s", "issues": [], "score": 0.5},
                    },
                ],
            },
            text_turn("已提交。"),
        ],
    )
    caller = CallStructured(model=model, schema=CodeReview)
    agent = make_agent(model)
    await agent.toolkit.add_tool(caller.as_tool(), group_name="basic")
    msg = await agent.reply(UserMsg("user", "审查"))
    assert agent.state.reply_context.structured_output == {
        "summary": "s",
        "issues": [],
        "score": 0.5,
    }
    # 没传 structured_schema，所以最终消息上不会有 structured_output —— 从 state 读才对
    assert getattr(msg, "structured_output", None) is None


async def test_structured_output_tool_is_visible_to_the_model() -> None:
    """工具真的进了 ``basic`` 组（否则模型永远调不到它，且不会报错）。"""
    caller = CallStructured(model=EchoChatModel(), schema=CodeReview, tool_name="emit_review")
    agent = make_agent(EchoChatModel())
    await agent.toolkit.add_tool(caller.as_tool(), group_name="basic")
    names = [
        _["function"]["name"]
        for _ in await agent.toolkit.get_tool_schemas(
            agent.state.tool_context.activated_groups,
        )
    ]
    assert "emit_review" in names


# ======================================================================
# C · prompt.py：PromptAssembler
# ======================================================================


def test_section_key_is_normalized() -> None:
    """key 会做归一化（小写、空格/连字符转下划线）。"""
    section = PromptSection(key="  Role-Name  ", title="T", body="B")
    assert section.key == "role_name"
    assert section.order_index == len(SECTION_ORDER)  # 未知 key 垫底


def test_section_rejects_empty_key_and_title() -> None:
    """空 key / 空标题都会破坏前缀结构，必须拒绝。"""
    with pytest.raises(ValueError, match="key 不能为空"):
        PromptSection(key="   ", title="T", body="B")
    with pytest.raises(ValueError, match="title 不能为空"):
        PromptSection(key="role", title="  ", body="B")


def test_duplicate_key_is_rejected_twice() -> None:
    """构造与 ``add`` 两条路都不许重复 key（重复 = 渲染结果取决于顺序）。"""
    with pytest.raises(ValueError, match="段落 key 重复"):
        PromptAssembler(
            sections=[
                PromptSection(key="role", title="A", body="1"),
                PromptSection(key="role", title="B", body="2"),
            ],
        )
    assembler = make_assembler()
    with pytest.raises(ValueError, match="已存在"):
        assembler.add(PromptSection(key="role", title="A", body="1"))


def test_render_follows_section_order_not_input_order() -> None:
    """渲染顺序由 ``SECTION_ORDER`` 决定，与传入顺序无关（前缀才可能稳定）。"""
    assembler = PromptAssembler(
        sections=[
            PromptSection(key="tools", title="工具", body="T"),
            PromptSection(key="role", title="角色", body="R"),
            PromptSection(key="constraints", title="约束", body="C"),
        ],
    )
    assert assembler.stable_keys == ["role", "constraints", "tools"]
    assert assembler.render() == "## 角色\nR\n\n## 约束\nC\n\n## 工具\nT"


def test_volatile_section_never_enters_the_system_prompt() -> None:
    """volatile 段落只走 ``hints()``，不进 ``render()``。"""
    assembler = make_assembler()
    assert "剩余 3 次调用" not in assembler.render()
    hints = assembler.hints()
    assert len(hints) == 1
    assert isinstance(hints[0], HintBlock)
    assert "[实时预算]" in hints[0].hint
    assert "剩余 3 次调用" in assembler.dynamic_text()


def test_hint_rejects_empty_text() -> None:
    """空 hint 会变成一条空 user 消息，白占 token 还干扰模型。"""
    assembler = make_assembler()
    with pytest.raises(ValueError, match="不能为空"):
        assembler.hint("   ")


def test_add_drop_and_missing_section() -> None:
    """``add`` / ``drop`` / ``section`` 的基本语义与报错。"""
    assembler = make_assembler()
    assembler.add(PromptSection(key="memory", title="记忆", body="M"))
    assert "memory" in assembler.stable_keys
    assert assembler.drop("memory") is True
    assert assembler.drop("memory") is False
    with pytest.raises(KeyError, match="没有段落"):
        assembler.section("memory")


def test_fingerprint_is_stable_across_renders_and_hints() -> None:
    """指纹在多次 render 与多次 ``hint()`` 之后都不变。"""
    assembler = make_assembler()
    before = assembler.compute_fingerprint()
    assembler.render()
    assembler.render()
    assembler.hint("临时文本")
    assembler.hints()
    assert assembler.compute_fingerprint() == before


def test_fingerprint_does_not_touch_state() -> None:
    """``compute_fingerprint`` 不记录基线、不递增 ``renders``（否则严格模式永不报警）。"""
    assembler = make_assembler()
    assert assembler.renders == 0
    assembler.compute_fingerprint()
    assert assembler.renders == 0
    assert assembler.drift == []


def test_fingerprint_changes_when_stable_body_changes() -> None:
    """稳定段落正文一变，指纹必须变（它是缓存键）。"""
    section = PromptSection(key="role", title="角色", body="A")
    assembler = PromptAssembler(sections=[section])
    before = assembler.compute_fingerprint()
    section.body = "B"
    assert assembler.compute_fingerprint() != before


def test_fingerprint_covers_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PROMPT_VERSION`` 参与指纹：改了渲染格式必须改版本号。"""
    assembler = make_assembler()
    before = assembler.compute_fingerprint()
    monkeypatch.setattr(prompt_module, "PROMPT_VERSION", PROMPT_VERSION + ".changed")
    assert assembler.compute_fingerprint() != before


def test_render_raises_on_stable_drift() -> None:
    """稳定段落漂移 → ``VolatileSectionError``（把静默的性能问题变成响亮的报错）。"""
    section = PromptSection(key="role", title="角色", body="A")
    assembler = PromptAssembler(sections=[section])
    assembler.render()
    section.body = "B"
    with pytest.raises(VolatileSectionError, match="role"):
        assembler.render()
    assert assembler.drift == ["role"]


def test_render_non_strict_records_drift() -> None:
    """``strict=False`` 时只记录不抛错，漂移仍然被记下来供观测。"""
    section = PromptSection(key="role", title="角色", body="A")
    assembler = PromptAssembler(sections=[section])
    assembler.render()
    section.body = "B"
    assert assembler.render(strict=False) == "## 角色\nB"
    assert assembler.drift == ["role"]


def test_rebaseline_accepts_new_baseline() -> None:
    """``rebaseline`` 之后不再报错（会话边界 / 上下文压缩后的正确姿势）。"""
    section = PromptSection(key="role", title="角色", body="A")
    assembler = PromptAssembler(sections=[section])
    assembler.render()
    section.body = "B"
    assembler.rebaseline()
    assert assembler.render() == "## 角色\nB"


async def test_inject_into_agent_keeps_fingerprint() -> None:
    """注入 HintBlock 只动消息流，system prompt 的指纹一个字节都不变。"""
    assembler = make_assembler()
    agent = make_agent(
        EchoChatModel(script=[text_turn("收到。")]),
        system_prompt=assembler.render(),
    )
    before = assembler.compute_fingerprint()
    injected = assembler.inject(agent, text="当前剩余预算：2 次")
    assert injected == 2  # 1 个 volatile 段落 + 1 段临时文本
    assert assembler.compute_fingerprint() == before
    last = agent.state.context[-1]
    hints = [block for block in last.content if isinstance(block, HintBlock)]
    assert [block.hint for block in hints] == [
        "[实时预算]\n剩余 3 次调用",
        "当前剩余预算：2 次",
    ]


def test_inject_requires_an_agent_like_object() -> None:
    """拿错对象（没有 ``state.append_context``）必须报错，静默跳过才是危险的。"""
    assembler = make_assembler()
    with pytest.raises(AttributeError, match="state.append_context"):
        assembler.inject(object(), text="x")


def test_inject_rejects_empty_text() -> None:
    """``inject(text="")`` 是空 hint，必须报错。"""
    assembler = make_assembler()
    with pytest.raises(ValueError, match="不能为空"):
        assembler.inject(make_agent(EchoChatModel()), text="   ")


async def test_observe_rejects_hint_block() -> None:
    """反面教材：``agent.observe([HintBlock])`` 会被入参校验拒掉。"""
    assembler = make_assembler()
    agent = make_agent(EchoChatModel())
    with pytest.raises(ValueError, match="type='hint'"):
        await agent.observe([assembler.hint("试试 observe 这条路")])


async def test_formatter_turns_hint_block_into_user_message() -> None:
    """formatter 视角：HintBlock → 独立 user 消息；ThinkingBlock → ``reasoning_content``。"""
    msgs: list[Msg] = [
        UserMsg("user", "问题"),
        AssistantMsg(
            "assistant",
            [ThinkingBlock(thinking="内心戏"), TextBlock(text="答案")],
        ),
        AssistantMsg("assistant", [HintBlock(hint="当前剩余预算：2 次")]),
    ]
    payload = await DeepSeekChatFormatter().format(msgs)
    assert payload[1]["content"] == "答案"
    assert payload[1]["reasoning_content"] == "内心戏"
    assert payload[2] == {"role": "user", "content": "当前剩余预算：2 次"}
    assert msgs[1].get_text_content() == "答案"


def test_render_tool_list_sorts_and_summarizes() -> None:
    """工具清单必须排序 + 一行一个；空列表要有占位说明而不是空串。"""
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "写文件\n第二行不要",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                },
            },
        },
    ]
    text = render_tool_list(schemas)
    assert text.splitlines() == [
        "- read_file(path: string?, limit: integer?): 读文件",
        "- write_file(path: string, text: string?): 写文件",
    ]
    assert render_tool_list([]) == "（本回合没有可用工具。）"


# ======================================================================
# D · critique.py：CritiqueLoop
# ======================================================================


def test_critique_constructor_guards() -> None:
    """``max_rounds`` 与 ``acceptance_score`` 的边界都要挡住。"""
    agent = make_agent(EchoChatModel())
    with pytest.raises(ValueError, match="max_rounds 必须 >= 1"):
        CritiqueLoop(agent=agent, max_rounds=0)
    with pytest.raises(ValueError, match="acceptance_score 必须在"):
        CritiqueLoop(agent=agent, acceptance_score=1.5)


async def test_critique_rejects_empty_task() -> None:
    """空任务不跑（否则批判毫无意义）。"""
    loop = CritiqueLoop(agent=make_agent(EchoChatModel()))
    with pytest.raises(ValueError, match="task 不能为空"):
        await loop.run("   ")


def test_critique_verdict_is_strict() -> None:
    """判据 schema：越界分数被拒、未知字段被拒、空 issues 条目被清掉。"""
    with pytest.raises(Exception):
        CritiqueVerdict(score=1.5, passed=True)
    with pytest.raises(Exception):
        CritiqueVerdict(score=0.5, passed=True, extra_field=1)  # type: ignore[call-arg]
    verdict = CritiqueVerdict(score=0.5, passed=False, issues=["  ", "缺数据", ""])
    assert verdict.issues == ["缺数据"]


def test_critique_result_trend_and_summary() -> None:
    """``trend`` / ``last_score`` / ``summary`` 三个观测字段的语义。"""
    empty = CritiqueResult(output="x", rounds=0, finished=False)
    assert empty.last_score is None and empty.trend == "none"
    up = CritiqueResult(output="x", rounds=2, finished=True, scores=[0.4, 0.9])
    assert up.trend == "up" and up.last_score == 0.9
    down = CritiqueResult(output="x", rounds=2, finished=False, scores=[0.9, 0.4])
    assert down.trend == "down"
    flat = CritiqueResult(output="x", rounds=2, finished=False, scores=[0.5, 0.5])
    assert flat.trend == "flat"
    text = up.summary(limit=5)
    assert text.startswith("[finished] 2 轮 scores=[0.4, 0.9] 趋势=up")
    assert text.endswith("x")


async def test_loop_finishes_in_one_round_when_passed() -> None:
    """一轮通过：不再修订，``rounds=1``。"""
    agent_model = EchoChatModel(script=[text_turn("v1"), text_turn("不该被用到")])
    loop = CritiqueLoop(
        agent=make_agent(agent_model, name="writer"),
        max_rounds=3,
        critique_model=EchoChatModel(script=[verdict_turn(0.95, True, [])]),
    )
    result = await loop.run("写一份复盘")
    assert result.finished is True
    assert result.rounds == 1
    assert result.scores == [0.95]
    assert result.output == "v1"
    assert agent_model.call_count == 1


async def test_loop_stops_when_score_reaches_acceptance() -> None:
    """``passed=False`` 但分数够高时也停：两者是「或」关系。"""
    loop = CritiqueLoop(
        agent=make_agent(EchoChatModel(script=[text_turn("v1")]), name="writer"),
        max_rounds=3,
        acceptance_score=0.8,
        critique_model=EchoChatModel(script=[verdict_turn(0.85, False, ["小瑕疵"])]),
    )
    result = await loop.run("写一份复盘")
    assert result.finished is True and result.rounds == 1


async def test_loop_revises_until_passing() -> None:
    """0.4 不通过 → 修订 → 0.9 通过：写手被调用两次，趋势 up。"""
    agent_model = EchoChatModel(script=[text_turn("v1"), text_turn("v2")])
    loop = CritiqueLoop(
        agent=make_agent(agent_model, name="writer"),
        max_rounds=3,
        critique_model=EchoChatModel(
            script=[verdict_turn(0.4, False, ["缺数据"]), verdict_turn(0.9, True, [])],
        ),
    )
    result = await loop.run("写一份复盘")
    assert result.finished is True
    assert result.output == "v2"
    assert result.trend == "up"
    assert agent_model.call_count == 2


async def test_loop_reports_unfinished_when_rounds_exhausted() -> None:
    """轮数用尽仍未通过：交最后一版，但 ``finished=False``（不伪装成功）。"""
    agent_model = EchoChatModel(script=[text_turn("v1"), text_turn("v2")])
    loop = CritiqueLoop(
        agent=make_agent(agent_model, name="writer"),
        max_rounds=2,
        critique_model=EchoChatModel(
            script=[verdict_turn(0.2, False, ["x"]), verdict_turn(0.3, False, ["x"])],
        ),
    )
    result = await loop.run("写一份复盘")
    assert result.finished is False
    assert result.rounds == 2
    assert result.output == "v2"
    assert len(result.verdicts) == 2


async def test_critique_failure_is_conservative() -> None:
    """批判步骤本身失败 → 保守判定（``score=0`` / ``passed=False``），不抛异常、不放行。"""
    loop = CritiqueLoop(
        agent=make_agent(EchoChatModel(script=[text_turn("v1")]), name="writer"),
        max_rounds=1,
        critique_model=EchoChatModel(script=[text_turn("我不批"), text_turn("就是不批")]),
    )
    result = await loop.run("写一份复盘")
    assert result.finished is False
    assert result.scores == [0.0]
    assert "批判步骤本身失败" in result.verdicts[0].issues[0]


async def test_critique_uses_its_own_model() -> None:
    """批判可以用另一个（更便宜的）模型：``critique_model`` 与 ``agent.model`` 解耦。"""
    agent_model = EchoChatModel(script=[text_turn("v1")])
    critique_model = EchoChatModel(script=[verdict_turn(0.95, True, [])])
    loop = CritiqueLoop(
        agent=make_agent(agent_model, name="writer"),
        max_rounds=1,
        critique_model=critique_model,
    )
    await loop.run("写一份复盘")
    assert agent_model.call_count == 1
    assert critique_model.call_count == 1
    assert loop.critique_model is critique_model
    assert "writer" in loop.describe()


async def test_text_of_skips_thinking_blocks() -> None:
    """产物取的是 ``get_text_content()``：思考块不能当成产物交给批判者。"""
    model = EchoChatModel(
        script=[{"thinking": "一大段内心戏", "text": "真正的产物"}],
    )
    loop = CritiqueLoop(
        agent=make_agent(model, name="writer"),
        max_rounds=1,
        critique_model=EchoChatModel(script=[verdict_turn(0.95, True, [])]),
    )
    result = await loop.run("写一份复盘")
    assert result.output == "真正的产物"


async def test_text_of_placeholder_when_no_text_at_all() -> None:
    """模型只调了工具没说话时，产物是占位说明而不是空串。"""
    model = EchoChatModel(script=[{"text": "", "tool_calls": []}])
    loop = CritiqueLoop(
        agent=make_agent(model, name="writer"),
        max_rounds=1,
        critique_model=EchoChatModel(script=[verdict_turn(0.95, True, [])]),
    )
    result = await loop.run("写一份复盘")
    assert result.output.startswith("（模型这一版没有产出文本内容")


def test_blank_schema_has_empty_required() -> None:
    """``Blank`` 只用来验证「不需要任何字段」的边界；这里钉住它的形状。"""
    assert Blank.model_json_schema()["properties"] == {}
    assert Path(__file__).exists()
