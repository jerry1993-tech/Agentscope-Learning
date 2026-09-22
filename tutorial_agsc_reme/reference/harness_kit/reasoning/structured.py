# -*- coding: utf-8 -*-
"""结构化输出：让模型「填表」而不是「写作文，再正则去抠」（契约 §3.14，第 14 讲）。

**为什么不能用「解析自由文本」**：``json.loads`` + 正则兜底是所有
「prompt 里说请输出 JSON」方案的宿命 —— 模型会加解释、会写成 markdown 代码块、
会多个逗号，而每一次「解析失败重试」都是一次真金白银的模型调用，且**失败率
随 schema 复杂度上升**。正确做法是让**解码过程本身**受约束：注册一个函数工具，
schema 就是这个工具的 ``parameters``，然后用 ``tool_choice`` 强制模型调用它。
模型没有「不按格式输出」的自由 —— 它的输出空间被 API 层的 function-calling
约束住了。

**AgentScope 已经把这件事做完了**，本模块**不重写**任何一个字节的调用逻辑：

- ``ChatModelBase.generate_structured_output``
  （``third_party/agentscope/src/agentscope/model/_base.py:457``）实现了
  **四级退化梯子**：``forced``（强制 tool_choice）→ ``auto`` → ``no_think``
  （关思考 + 强制）→ ``none``。每一级内部还会按 ``max_retries`` 重试；
  只有 ``StructuredOutputError`` 与「provider 拒绝强制 tool_choice」这类
  错误才会降到下一级。
- ``_call_api_with_structured_output``（同文件 ``:595``）负责拼工具、注入
  ``<system-reminder>You MUST call ...</system-reminder>``、拼流式增量、
  **并用 pydantic / jsonschema 校验**返回的实参。
- Agent 内部那条路走的是 ``_GenerateStructuredOutput``
  （``.../agent/_structured_output_tool.py:42``）：它被挂进 ``toolkit``
  （``.../agent/_agent.py:1118``），并用 ``ToolChoice(mode=...)`` 强制调用
  （``:3626``）。

那本模块存在的意义是什么？**``generate_structured_output`` 把工具名写死成
``"generate_structured_output"``**（``model/_base.py:625`` 的
``func_name = "generate_structured_output"``），调用方改不了。真实系统里工具名
要出现在 trace、审计日志、以及「一回合内要填好几张不同的表」的场景里，用一个
语义化的名字（``emit_result`` / ``emit_plan`` / ``emit_critique``）很重要。
所以本模块提供**两条路**：

=========================================== ============================================================
:meth:`CallStructured.run`（``tool_name`` 是 走 ``ChatModelBase.__call__`` + ``ToolChoice(mode=<tool_name>)``
语义化名字，默认如此）                  —— 契约 §3.14 要的正是这条：「不改 ``tools``，只改
                                        ``tool_choice``，因为改 schema 列表会让 prompt cache 失效」
                                        （``.../tool/_types.py:199`` 的 docstring 原文）。
:meth:`CallStructured.run`（``tool_name=None``）  直接委托给 SDK 的
                                        ``generate_structured_output``，白拿那四级退化梯子。
:meth:`CallStructured.as_tool`           把「提交器」做成 ``ToolBase`` 装进 Agent 的 ``toolkit``，
                                        让模型的 ReAct 循环自己调 —— 与
                                        ``_GenerateStructuredOutput`` 同构，写到同一个
                                        ``state.reply_context.structured_output``。
=========================================== ============================================================

**语义化工具名这条路的真实限制**：思考模型（DeepSeek 的 thinking 模式就是）
会**直接 400 拒掉强制 ``tool_choice``**，报 ``Thinking mode does not support
this tool_choice``。SDK 的退化梯子本来能兜住这种情况，但**显式传了
``tool_choice`` 就会绕过整条梯子**（``model/_base.py:505`` 的
``strategies = (("explicit", {}, user_tool_choice),)``）。所以本模块在
:meth:`CallStructured._call_once` 里自己补了「forced → auto」这一步，判据复用
SDK 的 ``_get_structured_output_fallback_exceptions()`` 钩子
（``model/_base.py:123``）。多花的这一次调用记在
:attr:`CallStructured.last_strategy` 上。

**两条路的失败语义**：都没拿到合法结构时抛本模块的
:class:`StructuredOutputError`（``RuntimeError`` 子类，契约 §3.14 指定）。
注意与 ``agentscope.exception.StructuredOutputError`` 区分：那是 SDK 内部的
*开发者错误*类型，用途是「触发退化梯子的下一级」，本模块在 ``tool_name=None``
时捕获它并转成自己的类型 —— 因为对调用方来说，「四级梯子全试完还是失败」
是一个**业务失败**（这轮任务做不成），不是一个开发期 bug。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Type

from loguru import logger
from pydantic import BaseModel, ValidationError

from agentscope.exception import (
    StructuredOutputError as _SDKStructuredOutputError,
)
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolChoice, ToolChunk
from agentscope.tool._utils import _remove_title_field

__all__ = [
    "STRUCTURED_TOOL_INSTRUCTION",
    "CallStructured",
    "StructuredOutputError",
    "StructuredOutputTool",
]

STRUCTURED_TOOL_INSTRUCTION = (
    "<system-reminder>Now you **MUST** call the tool named "
    "'{tool_name}' to deliver your result. DON'T do anything else."
    "</system-reminder>"
)
"""注入的强制指令。**与 SDK 同款措辞**（``model/_base.py:639`` 那段），
刻意不改：措辞是调过的，「MUST + DON'T do anything else」对强制的服从率明显
比客气的说法高。"""


class StructuredOutputError(RuntimeError):
    """模型没能产出符合 schema 的结构化输出（契约 §3.14）。

    携带三个可诊断字段，而不是只丢一句字符串 —— 「为什么失败」决定了下一步是
    改 prompt、改 schema、还是换模型：

    Attributes:
        tool_name (`str`): 被强制调用的工具名。
        attempts (`int`): 实际尝试次数。
        last_error (`str | None`): 最后一次失败的原因。
        raw (`dict | None`): 最后一次拿到的原始实参（如果有），便于人肉比对。
    """

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        attempts: int = 0,
        last_error: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """构造错误。

        Args:
            message (`str`): 人读说明。
            tool_name (`str`): 被强制的工具名。
            attempts (`int`): 尝试次数。
            last_error (`str | None`): 最后一次失败原因。
            raw (`dict | None`): 最后一次的原始实参。
        """
        self.tool_name = tool_name
        self.attempts = attempts
        self.last_error = last_error
        self.raw = raw
        super().__init__(message)


def _as_input_dict(value: Any) -> dict[str, Any] | None:
    """把 ``ToolCallBlock.input`` 归一成 dict。

    ``input`` 可能是 dict，也可能是**字符串**（provider 原样回传的 JSON 文本）
    —— SDK 的 ``_call_api_with_structured_output`` 用
    ``_json_loads_with_repair``（``model/_base.py:703``）处理这种情况，
    说明两种形态都真实存在。这里用 ``json.loads``，解析不了就返回 ``None``
    （调用方会把它当作「这次没成功」，而不是崩掉）。

    Args:
        value (`Any`): ``ToolCallBlock.input``。

    Returns:
        `dict[str, Any] | None`: 解析出的 dict；解析不出为 ``None``。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class StructuredOutputTool(ToolBase):
    """装进 ``Toolkit`` 的「提交器」（``CallStructured.as_tool()`` 的产物）。

    与 SDK 的 ``_GenerateStructuredOutput``（``.../agent/_structured_output_tool.py:42``）
    同构，差别只有三点，都是有意的：

    1. **名字由调用方给**（``emit_result`` 之类），而不是写死的
       ``GenerateStructuredOutput``；
    2. **校验用的是构造时的 ``schema``**，而不是从
       ``state.reply_context.structured_schema`` 读 —— 前者在「同一个 Agent
       一回合要填多张表」时不会串台；
    3. **校验失败返回 ERROR 的 ``ToolChunk`` 而不是抛异常**
       （与 SDK 一致）：模型能看到错误、下一轮自己改，比整轮崩掉强。

    Attributes:
        last_result (`BaseModel | None`): 最近一次校验通过的实例（观测用）。
    """

    is_state_injected = True
    """需要 ``AgentState``：提交结果要写进 ``state.reply_context.structured_output``。"""

    is_concurrency_safe = True
    """并发调用安全：每次调用只写自己的实例字段与 state 的一个字段。"""

    is_read_only = True
    """只读：不碰文件系统、不改输入。"""

    def __init__(
        self,
        *,
        schema: Type[BaseModel],
        tool_name: str = "emit_result",
        description: str | None = None,
    ) -> None:
        """构造提交器。

        Args:
            schema (`Type[BaseModel]`): 结果的结构（pydantic 模型类）。
            tool_name (`str`, defaults to ``"emit_result"``): 工具名。
            description (`str | None`, optional): 工具描述。``None`` 时用默认文案。
        """
        super().__init__()
        self.name = tool_name
        self.schema = schema
        self.description = description or (
            "Submit your final result through this tool.\n\n"
            "The input schema IS the required structure of your result. "
            "Fill every field. Once you call this tool your reply ends and "
            "the submitted object is delivered to the caller.\n\n"
            "Call it as soon as you have enough information — do not keep "
            "polishing the wording."
        )
        self.last_result: BaseModel | None = None
        self.input_schema: dict[str, Any] = _remove_title_field(
            schema.model_json_schema(),
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """无条件 ALLOW。

        提交器是**纯控制流**（与交接工具同理，见
        :mod:`harness_kit.multiagent.handoff`）：它不读写文件、不跑命令，
        它的唯一作用是「把模型已经想好的东西落到 state 里」。默认 ASK 会让
        每一次结构化输出都弹一次确认 —— 而结构化输出在流水线里是每轮都发生的，
        那不是安全，那是骚扰。用户配的 DENY 规则优先级更高。

        Args:
            tool_input (`dict[str, Any]`): 未使用。
            context (`PermissionContext`): 未使用。

        Returns:
            `PermissionDecision`: ``ALLOW``。
        """
        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=(
                f"``{self.name}`` 只把结构化结果写进 agent state，"
                "无外部副作用，无需确认。"
            ),
            decision_reason="harness_kit.reasoning.structured.StructuredOutputTool",
        )

    async def call(self, _agent_state: AgentState, **kwargs: Any) -> ToolChunk:
        """校验并记录结果。

        参数名 ``_agent_state`` 是 AgentScope 的注入约定
        （``ToolBase.is_state_injected`` 的 docstring：注入的参数名固定为
        ``_agent_state``），不能改成别的名字。

        Args:
            _agent_state (`AgentState`): 由 Agent 注入。
            **kwargs (`Any`): 模型给出的字段，按 schema 展开成关键字参数。

        Returns:
            `ToolChunk`: 成功时 SUCCESS；校验失败时 **ERROR** 且文本里带
            逐字段的错误，让模型下一轮能自己修。
        """
        try:
            validated = self.schema.model_validate(kwargs)
        except ValidationError as exc:
            message = "; ".join(
                f"{'.'.join(str(_) for _ in err['loc']) or '<root>'}: "
                f"{err['msg']}"
                for err in exc.errors()
            )
            logger.warning("结构化输出校验失败：{}", message)
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"ValidationError: the input does not match the "
                            f"required structure — {message}. "
                            "Call this tool again with a corrected input."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # 自定义 validator 里 raise ValueError 是很常见的写法
            # （SDK 也这么兜，见 _structured_output_tool.py:116-124）。
            logger.warning("结构化输出校验抛异常：{}", exc)
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "ValidationError: your input was rejected by a "
                            f"custom validator — {exc}."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )

        self.last_result = validated
        _agent_state.reply_context.structured_output = validated.model_dump(
            mode="json",
        )
        logger.info(
            "结构化输出已提交：{} （{} 字段）",
            self.name,
            len(validated.model_dump()),
        )
        return ToolChunk(
            content=[
                TextBlock(text="Structured output generated successfully."),
            ],
            state=ToolResultState.SUCCESS,
            is_last=True,
        )


class CallStructured:
    """结构化输出调用器（契约 §3.14）。

    Args:
        model (`ChatModelBase`): 真实模型实例（``DeepSeekChatModel`` /
            ``EchoChatModel`` 皆可 —— 本类只依赖基类接口）。
        schema (`Type[BaseModel]`): 结果结构。
        tool_name (`str | None`, defaults to ``"emit_result"``): 强制调用的
            工具名。``None`` 表示**改用 SDK 原生的四级退化梯子**
            （``generate_structured_output``），此时工具名固定为
            ``"generate_structured_output"``。

    Raises:
        ValueError: ``schema`` 不是 pydantic 模型类（传了 dict 或实例）。
    """

    def __init__(
        self,
        *,
        model: ChatModelBase,
        schema: Type[BaseModel],
        tool_name: str | None = "emit_result",
    ) -> None:
        """初始化，并把 schema 编译成工具 schema。"""
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise ValueError(
                "schema 必须是 pydantic BaseModel 的**子类**（传类，不传实例或 "
                f"dict），收到 {schema!r}。"
                "要交 JSON schema 请直接用 model.generate_structured_output。",
            )
        self.model = model
        self.schema = schema
        self.tool_name = tool_name
        self.last_strategy: str = "forced"
        """最近一次调用实际用的 ``tool_choice`` 策略：``forced`` 或 ``auto``。

        思考模型（DeepSeek thinking 模式）会 400 拒掉强制工具选择，此时本类会
        降级成 ``auto`` 再发一次 —— 这次额外的 HTTP 调用**会计进 provider 账单**，
        所以要能从对象上读出来（见 :meth:`describe`）。
        """

    # ==================================================================
    # schema
    # ==================================================================
    @property
    def input_schema(self) -> dict[str, Any]:
        """工具 schema（用于 ``tools=[...]``）。

        **每次都重新生成、且先 deepcopy**：``_remove_title_field``
        （``.../tool/_utils.py:11``）是**原地修改**传入 dict 的，
        而 ``model_json_schema()`` 每次返回新对象，所以这里安全；
        但绝不能缓存同一个 dict 反复改。

        Returns:
            `dict[str, Any]`: JSON schema。
        """
        return _remove_title_field(self.schema.model_json_schema())

    def tools_payload(self) -> list[dict[str, Any]]:
        """``ChatModelBase.__call__(tools=...)`` 要的那个列表。

        Returns:
            `list[dict[str, Any]]`: 只含一个 function 工具。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": self.tool_name or "generate_structured_output",
                    "description": (
                        "Deliver the final result. The input schema is the "
                        "required structure of the result."
                    ),
                    "parameters": self.input_schema,
                },
            },
        ]

    # ==================================================================
    # 主入口
    # ==================================================================
    async def run(
        self,
        messages: list[Msg],
        *,
        max_attempts: int = 2,
        **kwargs: Any,
    ) -> BaseModel:
        """跑一次结构化输出，返回**已校验**的模型实例。

        Args:
            messages (`list[Msg]`): 上下文。**列表非空**，否则接口直接报错
                （与 SDK 一致：``model/_base.py:512`` 的空列表 ValueError）。
            max_attempts (`int`, defaults to `2`): 最多试几次。
                **为什么默认 2 而不是 1**：模型偶尔会忘了调工具、或者少填一个
                Required 字段，第二次带上错误信息重试的修复率很高；而试 3 次
                以上的边际收益急剧下降，纯粹是在烧 token。
                注意 ``max_attempts`` **不覆盖**瞬态网络错误 —— 那由
                ``ChatModelBase.__call__`` 自己的 ``max_retries`` 负责，两套
                重试不要叠成乘法。
            **kwargs (`Any`): 透传给模型调用（例如 ``temperature``）。

        Returns:
            `BaseModel`: ``self.schema`` 的实例。

        Raises:
            ValueError: ``messages`` 为空，或 ``max_attempts < 1``。
            StructuredOutputError: 用尽尝试次数仍没拿到合法结构。
                **注意**：provider 拒绝强制 ``tool_choice`` 时本方法会降级成
                ``auto`` **多发一次**（见 :meth:`_call_once`），所以失败时实际
                调用次数可能比 ``max_attempts`` 多；成功那次用的策略记在
                :attr:`last_strategy` 上。
        """
        if not messages:
            raise ValueError("messages 不能为空：没有上下文就没有可抽取的东西。")
        if max_attempts < 1:
            raise ValueError(f"max_attempts 必须 >= 1，收到 {max_attempts}。")

        if self.tool_name is None:
            return await self._run_native(messages, **kwargs)

        last_error: str | None = None
        raw: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                payload = await self._call_once(messages, **kwargs)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # 模型调用本身失败（网络/鉴权）：不重试第二次「格式」，直接抛。
                raise StructuredOutputError(
                    f"调用模型时失败：{type(exc).__name__}: {exc}",
                    tool_name=self.tool_name or "",
                    attempts=attempt,
                    last_error=str(exc),
                ) from exc

            raw = payload
            if payload is None:
                last_error = (
                    f"模型没有调用 `{self.tool_name}` 工具（可能只回了文本）。"
                )
            else:
                try:
                    validated = self.schema.model_validate(payload)
                except ValidationError as exc:
                    last_error = "; ".join(
                        f"{'.'.join(str(_) for _ in err['loc']) or '<root>'}: "
                        f"{err['msg']}"
                        for err in exc.errors()
                    )
                else:
                    logger.info(
                        "结构化输出成功：{} 第 {} 次尝试",
                        self.tool_name,
                        attempt,
                    )
                    return validated

            logger.warning(
                "结构化输出第 {}/{} 次失败：{}",
                attempt,
                max_attempts,
                last_error,
            )

        raise StructuredOutputError(
            f"用尽 {max_attempts} 次尝试仍未拿到符合 `{self.schema.__name__}` "
            f"的结构化输出（工具 `{self.tool_name}`）。最后一次原因：{last_error}",
            tool_name=self.tool_name or "",
            attempts=max_attempts,
            last_error=last_error,
            raw=raw,
        )

    async def run_dict(
        self,
        messages: list[Msg],
        *,
        max_attempts: int = 2,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """同 :meth:`run`，但返回 ``model_dump(mode="json")`` 后的 dict。

        契约 §3.14 的接口是 ``run() -> BaseModel``；这个方法是为了
        「结果要直接落盘成 JSON」的场景（第 9 讲的会话日志、第 12 讲的重规划），
        省掉调用方每次都写一遍 ``.model_dump(mode="json")``。

        Args:
            messages (`list[Msg]`): 上下文。
            max_attempts (`int`, defaults to `2`): 见 :meth:`run`。
            **kwargs (`Any`): 透传。

        Returns:
            `dict[str, Any]`: JSON 可序列化的 dict。
        """
        result = await self.run(messages, max_attempts=max_attempts, **kwargs)
        return result.model_dump(mode="json")

    # ==================================================================
    # Agent 内路径
    # ==================================================================
    def as_tool(self) -> StructuredOutputTool:
        """造一个能装进 ``Agent.toolkit`` 的提交器。

        用途是「模型在**完整 ReAct 循环里**（可以查资料、调工具）最后提交
        结构」。用法与**结果从哪读**（这一点必须说清，否则很容易找个空值）：

        ```python
        tool = CallStructured(model=m, schema=Plan).as_tool()
        await agent.toolkit.add_tool(tool, group_name="basic")
        await agent.reply(inputs=...)                      # 不要传 structured_schema
        plan = agent.state.reply_context.structured_output  # dict，已校验过
        ```

        **注意不要同时传 ``structured_schema``**：Agent 收到它之后会自己挂一个
        ``_GenerateStructuredOutput``（``.../agent/_agent.py:1118``）并用
        ``ToolChoice`` 强推它（``:3626``）—— 而 ``tool_choice`` 只能指向一个
        工具名，于是模型只会调 SDK 那个提交器，你的 ``emit_result`` 永远收不到
        东西（表现为「装了工具但 state 里一直没值」这种很难查的静默失效）。

        **也不要指望 ``reply().structured_output``**：SDK 只在
        ``state.reply_context.structured_schema is not None`` 时才把它拷进最终
        消息（``.../agent/_agent.py:3554`` 的 ``required and satisfied`` 分支）。
        本工具写的是 ``state.reply_context.structured_output``，所以**从 state
        读**才是对的路径；传 ``structured_schema`` 才让最终消息带上它。

        Returns:
            `StructuredOutputTool`: 本 schema 的提交器（新实例，可重复装）。
        """
        return StructuredOutputTool(
            schema=self.schema,
            tool_name=self.tool_name or "generate_structured_output",
        )

    # ==================================================================
    # 观测
    # ==================================================================
    def describe(self) -> str:
        """一行描述（调试用）。

        Returns:
            `str`: 形如 ``"CallStructured(emit_result, schema=Plan, 3 fields)"``。
        """
        return (
            f"CallStructured({self.tool_name or 'generate_structured_output'}, "
            f"schema={self.schema.__name__}, "
            f"model={getattr(self.model, 'model', type(self.model).__name__)}, "
            f"last_strategy={self.last_strategy})"
        )

    # ==================================================================
    # 内部
    # ==================================================================
    async def _call_once(
        self,
        messages: list[Msg],
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """一次模型调用，抽回工具实参。

        **只改 ``tool_choice``，不动 ``tools``**：契约 §3.14 明确要求这一点，
        依据是 ``.../tool/_types.py:199`` 的 docstring ——
        ``ToolChoice(mode="<tool_name>")`` 在**不改变 schema 列表**的前提下
        强制单个工具调用；而 ``tools=["<tool_name>"]`` 会把转发给模型的 schema
        列表改掉，**让 prompt cache 全部失效**。在多轮 agent 循环里，缓存失效
        的代价远大于一次工具选择 —— 输入 token 会以全价重算。

        **强制失败时降级为 ``auto`` 重试一次**（真实踩坑）：思考模型会直接
        400 拒掉强制 ``tool_choice`` —— DeepSeek 的原话是
        ``Thinking mode does not support this tool_choice``。SDK 自己在
        ``ChatModelBase.generate_structured_output`` 里就有这条退化链
        （``model/_base.py:457`` 的 ``forced`` → ``auto`` → ``no_think`` →
        ``none``），但那条链只在**没有显式 ``tool_choice``** 时才生效
        （``:505`` 的 ``strategies = (("explicit", ...),)`` 会绕过整条链）；
        本方法要指定自定义工具名，必须自己带上「forced → auto」这一步。

        判定「该不该降级」**复用 SDK 的钩子**
        ``_get_structured_output_fallback_exceptions()``（``model/_base.py:123``），
        而不是硬编码 ``openai.BadRequestError``：harness_kit 的适配器已经把它
        覆写成「provider 的 400 类异常」（``models/adapters/base.py:644``），
        换成别的 provider 时这里跟着自动生效。钩子返回空元组时**不降级**
        （与 SDK 的「其它异常直接抛」一致）。

        Args:
            messages (`list[Msg]`): 上下文。
            **kwargs (`Any`): 透传给 ``ChatModelBase.__call__``。

        Returns:
            `dict[str, Any] | None`: 工具实参；模型没调这个工具时为 ``None``。
        """
        self.last_strategy = "forced"
        try:
            response = await self._invoke(
                messages,
                ToolChoice(mode=self.tool_name or "emit_result"),
                **kwargs,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if not self._should_fall_back(exc):
                raise
            logger.warning(
                "强制 tool_choice 被 provider 拒绝（{}: {}），"
                "降级为 auto 重试一次。",
                type(exc).__name__,
                str(exc)[:120],
            )
            self.last_strategy = "auto"
            response = await self._invoke(
                messages,
                ToolChoice(mode="auto"),
                **kwargs,
            )

        for block in response.content:
            if isinstance(block, ToolCallBlock) and block.name == self.tool_name:
                return _as_input_dict(block.input)
        return None

    async def _invoke(
        self,
        messages: list[Msg],
        tool_choice: ToolChoice,
        **kwargs: Any,
    ) -> ChatResponse:
        """发一次 ``ChatModelBase.__call__`` 并把流收干。

        Args:
            messages (`list[Msg]`): 上下文。
            tool_choice (`ToolChoice`): 本次的工具选择策略。
            **kwargs (`Any`): 透传。

        Returns:
            `ChatResponse`: 终态响应。
        """
        res = await self.model(
            messages=list(messages),
            tools=self.tools_payload(),
            tool_choice=tool_choice,
            **kwargs,
        )
        if isinstance(res, ChatResponse):
            return res
        # 流式：``__call__`` 会把增量攒成一个 ``is_last=True`` 的终态 chunk
        # （``model/_base.py:266-289`` 的 ``_stream()``），所以取最后一块。
        return await _drain(res)

    def _should_fall_back(self, exc: BaseException) -> bool:
        """这个异常是否值得降级 ``tool_choice`` 重试一次。

        Args:
            exc (`BaseException`): ``__call__`` 抛出的异常。

        Returns:
            `bool`: provider 的「请求形状被拒」类异常为 ``True``。
        """
        hook = getattr(
            type(self.model),
            "_get_structured_output_fallback_exceptions",
            None,
        )
        if hook is None:  # pragma: no cover - ChatModelBase 一定有这个方法
            return False
        types = tuple(hook())
        return bool(types) and isinstance(exc, types)

    async def _run_native(
        self,
        messages: list[Msg],
        **kwargs: Any,
    ) -> BaseModel:
        """``tool_name=None`` 时走 SDK 原生的四级退化梯子。

        Args:
            messages (`list[Msg]`): 上下文。
            **kwargs (`Any`): 透传。

        Returns:
            `BaseModel`: 已校验的实例。

        Raises:
            StructuredOutputError: 四级策略全失败。
        """
        try:
            response = await self.model.generate_structured_output(
                list(messages),
                self.schema,
                **kwargs,
            )
        except _SDKStructuredOutputError as exc:
            raise StructuredOutputError(
                f"SDK 的结构化输出四级策略（forced/auto/no_think/none）"
                f"全部失败：{exc}",
                tool_name="generate_structured_output",
                attempts=4,
                last_error=str(exc),
            ) from exc
        return self.schema.model_validate(response.content)


async def _drain(
    stream: Any,
) -> ChatResponse:
    """把流式响应收干，返回最后一块（``is_last=True`` 那块）。

    单独抽出来是为了让 :meth:`CallStructured._call_once` 的两种形状摆在
    一起：``ChatModelBase.__call__`` 的返回**可能是 ``ChatResponse``，
    也可能是 ``AsyncGenerator``**（``model/_base.py:188``）。忘了判这一下是
    最常见的踩坑点 —— 直接对生成器取 ``.content`` 会得到 ``AttributeError``。

    Args:
        stream (`Any`): ``ChatResponse`` 的异步生成器。

    Returns:
        `ChatResponse`: 最后一块。

    Raises:
        RuntimeError: 生成器一块都没吐。
    """
    last: ChatResponse | None = None
    async for chunk in stream:
        last = chunk
    if last is None:
        raise RuntimeError("模型流式响应为空：一块 chunk 都没有。")
    return last
