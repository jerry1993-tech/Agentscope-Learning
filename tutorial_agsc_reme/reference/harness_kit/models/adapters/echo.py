# -*- coding: utf-8 -*-
"""``EchoChatModel`` —— 不联网的确定性模型，用于离线测试与教学演示。

**为什么它是生产件而不是玩具**：harness_kit 的 registry 把 ``"echo"`` 注册为
一个**一等 provider**（``harness_kit/registry.py`` 的懒加载条目
``("harness_kit.models.adapters.echo", ("build_echo_model", "EchoChatModel"))``）。
它能做到三件真实模型做不到的事：

1. **离线跑通 Agent Loop**：单元测试里不需要网络、不需要 key、不会 flaky；
2. **可控地触发工具调用**：给一段「脚本」，精确控制第几轮说什么、调哪个工具、
   传什么参数 —— 这是测「权限被拒 / 参数校验失败 / 工具报错」这类分支的唯一
   可靠手段；
3. **伪造 usage**：让定价与账本逻辑在没有真实 token 消耗时也能被断言。

它**不是**用来替代真实模型的：它不理解语义，只按规则回放。

脚本格式（``script`` 参数）是一个列表，每个元素代表**一次调用**：

.. code-block:: python

    script = [
        # 第 1 次调用：说一句话 + 调一个工具
        {
            "text": "我先查一下天气。",
            "tool_calls": [
                {"id": "call-1", "name": "get_weather", "input": {"city": "北京"}},
            ],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        },
        # 第 2 次调用：只用文字收尾
        {"text": "北京晴。"},
    ]

脚本耗尽后，模型退化回「回声」行为：把最后一条 user 消息原样回显。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, AsyncGenerator, ClassVar, Iterable

from loguru import logger

from agentscope._utils._common import _generate_id
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage
from agentscope.tool import ToolChoice

from harness_kit.models.adapters.base import (
    HarnessChatModelAdapter,
    HarnessChatModelParameters,
    HarnessCredential,
)

__all__ = ["EchoChatModel", "EchoParameters", "build_echo_model"]


class EchoParameters(HarnessChatModelParameters):
    """回声模型参数：全部无意义，保留只为接口对齐。"""


def _dummy_value(schema: dict[str, Any]) -> Any:
    """按 JSON schema 造一个占位实参。

    只为让 ``script`` 没写 ``input`` 时的工具调用能跑起来 —— 类型对不对
    由被调工具的校验器决定，这里不负责。

    Args:
        schema (`dict[str, Any]`): 属性的 JSON schema。

    Returns:
        `Any`: 占位值。
    """
    kind = schema.get("type")
    if kind == "string":
        return schema.get("enum", ["echo"])[0]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "echo"


def _default_input(tool_schema: dict[str, Any]) -> dict[str, Any]:
    """从工具 schema 里造一份最小可用的实参。

    Args:
        tool_schema (`dict[str, Any]`): ``{"type": "function", "function": {...}}``。

    Returns:
        `dict[str, Any]`: ``required`` 字段全给占位值。
    """
    fn = tool_schema.get("function", {})
    params = fn.get("parameters") or {}
    properties = params.get("properties") or {}
    required = params.get("required") or list(properties)
    return {
        name: _dummy_value(properties.get(name, {}))
        for name in required
    }


class EchoChatModel(HarnessChatModelAdapter):
    """确定性回声模型（离线测试 / 教学）。

    Args:
        model_name (`str`): 模型名，默认 ``"echo"``。价格表里查不到它，
            因此 :meth:`~harness_kit.models.adapters.base
            .HarnessChatModelAdapter.cost_of` 会记一条 warning 并返回 ``None``
            —— 这是**故意的**，正好覆盖「价格缺失」这条分支。
        script (`Iterable[dict[str, Any]] | None`): 见模块 docstring。
        stream (`bool`): 是否走流式路径。默认 ``True``：流式路径会经过基类
            ``__call__`` 的 ``_StreamAccumulator``，是更完备的测试路径。
        chunk_size (`int`): 流式时每次吐几个字符，用来验证增量拼装。
        fail_times (`int`): 前 N 次调用直接抛 ``ConnectionError``，用于测
            :func:`~harness_kit.models.ratelimit.retry_with_backoff` 与
            基类重试。
        **kwargs (`Any`): 透传基类。

    Raises:
        ValueError: ``script`` 里的元素不是 ``dict``。
    """

    adapter_name: ClassVar[str] = "echo"

    def __init__(
        self,
        *,
        model_name: str = "echo",
        script: Iterable[dict[str, Any]] | None = None,
        stream: bool = True,
        chunk_size: int = 4,
        fail_times: int = 0,
        **kwargs: Any,
    ) -> None:
        """初始化。"""
        script_list = list(script or [])
        for item in script_list:
            if not isinstance(item, dict):
                raise ValueError(
                    f"script 的每个元素必须是 dict，收到 {type(item).__name__}",
                )

        super().__init__(
            model_name=model_name,
            stream=stream,
            parameters=kwargs.pop("parameters", None) or EchoParameters(),
            credential=kwargs.pop("credential", None) or HarnessCredential(),
            pricing=kwargs.pop("pricing", None),
            max_retries=kwargs.pop("max_retries", 0),
            retry_delay=kwargs.pop("retry_delay", 0.0),
            context_size=kwargs.pop("context_size", 8192),
            **kwargs,
        )
        self.script: list[dict[str, Any]] = script_list
        self.chunk_size = max(1, int(chunk_size))
        self.fail_times = int(fail_times)
        self.call_count = 0
        """已经发生的调用次数（含失败）。用于测重试。"""

    # ------------------------------------------------------------------
    def _next_turn(
        self,
        messages: list[Msg],
        tools: list[dict] | None,
        tool_choice: ToolChoice | None,
    ) -> dict[str, Any]:
        """决定这一次调用产出什么。

        Args:
            messages (`list[Msg]`): 输入消息。
            tools (`list[dict] | None`): 工具 schema。
            tool_choice (`ToolChoice | None`): 工具选择。

        Returns:
            `dict[str, Any]`: 形如
            ``{"text": ..., "tool_calls": [...], "usage": {...}}``。
        """
        if self.script:
            return self.script.pop(0)

        # 脚本耗尽：如果给了工具且还没有工具结果，就调第一个工具
        has_tool_result = any(
            getattr(block, "type", None) == "tool_result"
            for msg in messages
            for block in (msg.content or [])
        )
        wants_tool = (
            tools
            and not has_tool_result
            and (tool_choice is not None and tool_choice.mode != "none")
        )
        if wants_tool:
            mode = tool_choice.mode
            named = [
                schema
                for schema in tools
                if schema.get("function", {}).get("name") == mode
            ]
            chosen = (named or tools)[0]
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": f"echo-call-{self.call_count}",
                        "name": chosen.get("function", {}).get("name", "unknown"),
                        "input": _default_input(chosen),
                    },
                ],
            }

        last_text = self._last_user_text(messages)
        return {
            "text": f"[echo] {last_text}",
            "usage": {
                "input_tokens": max(1, len(last_text)),
                "output_tokens": len(last_text) + 6,
            },
        }

    @staticmethod
    def _last_user_text(messages: list[Msg]) -> str:
        """取最后一条 user 消息里的纯文本。

        Args:
            messages (`list[Msg]`): 输入消息。

        Returns:
            `str`: 拼接后的文本；没有 user 消息时为空串。
        """
        for msg in reversed(messages):
            if getattr(msg, "role", None) != "user":
                continue
            parts = [
                getattr(block, "text", "")
                for block in (msg.content or [])
                if getattr(block, "type", None) == "text"
            ]
            if parts:
                return "".join(parts)
        return ""

    def _usage_of(self, turn: dict[str, Any], started: datetime) -> ChatUsage | None:
        """按 ``turn["usage"]`` 造一个 :class:`ChatUsage`。

        Args:
            turn (`dict[str, Any]`): 脚本项。
            started (`datetime`): 调用开始时间。

        Returns:
            `ChatUsage | None`: 用量；未声明时为 ``None``。
        """
        raw = turn.get("usage")
        if raw is None:
            return None
        return ChatUsage(
            input_tokens=int(raw.get("input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
            time=(datetime.now() - started).total_seconds(),
            cache_input_tokens=int(raw.get("cache_input_tokens", 0)),
            cache_creation_input_tokens=int(
                raw.get("cache_creation_input_tokens", 0),
            ),
        )

    def _blocks_of(self, turn: dict[str, Any]) -> list[Any]:
        """把脚本项转成 block 列表。

        Args:
            turn (`dict[str, Any]`): 脚本项。

        Returns:
            `list[Any]`: TextBlock / ThinkingBlock / ToolCallBlock。
        """
        blocks: list[Any] = []
        if turn.get("thinking"):
            blocks.append(ThinkingBlock(thinking=str(turn["thinking"])))
        if turn.get("text"):
            blocks.append(TextBlock(text=str(turn["text"])))
        for call in turn.get("tool_calls") or []:
            raw_input = call.get("input", {})
            if isinstance(raw_input, str):
                payload = raw_input
            else:
                payload = json.dumps(raw_input, ensure_ascii=False)
            blocks.append(
                ToolCallBlock(
                    id=call.get("id") or _generate_id(),
                    name=call["name"],
                    input=payload,
                ),
            )
        return blocks

    # ------------------------------------------------------------------
    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """产出脚本规定的内容。"""
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ConnectionError(
                f"echo 模型第 {self.call_count} 次调用按 fail_times 故意失败",
            )

        started = datetime.now()
        turn = self._next_turn(messages, tools, tool_choice)
        blocks = self._blocks_of(turn)
        usage = self._usage_of(turn, started)
        response_id = _generate_id()

        if not self.stream:
            if usage is not None:
                self._track_usage(usage)
            return ChatResponse(
                content=blocks,
                is_last=True,
                id=response_id,
                usage=usage,
                metadata={"echo_script_turn": dict(turn)},
            )

        async def _gen() -> AsyncGenerator[ChatResponse, None]:
            """把 blocks 切成增量块吐出去。"""
            for block in blocks:
                if isinstance(block, TextBlock):
                    for i in range(0, len(block.text), self.chunk_size):
                        yield ChatResponse(
                            content=[
                                TextBlock(
                                    text=block.text[i : i + self.chunk_size],
                                    id=block.id,
                                ),
                            ],
                            is_last=False,
                            id=response_id,
                        )
                elif isinstance(block, ThinkingBlock):
                    for i in range(0, len(block.thinking), self.chunk_size):
                        yield ChatResponse(
                            content=[
                                ThinkingBlock(
                                    thinking=block.thinking[
                                        i : i + self.chunk_size
                                    ],
                                    id=block.id,
                                ),
                            ],
                            is_last=False,
                            id=response_id,
                        )
                else:
                    # 工具调用参数照真实 provider 的做法**分两片**下发，
                    # 专门用来验证累积逻辑真的在拼字符串。
                    payload = block.input
                    mid = max(1, len(payload) // 2)
                    for piece in (payload[:mid], payload[mid:]):
                        if not piece:
                            continue
                        yield ChatResponse(
                            content=[
                                ToolCallBlock(
                                    id=block.id,
                                    name=block.name,
                                    input=piece,
                                ),
                            ],
                            is_last=False,
                            id=response_id,
                        )
            if usage is not None:
                self._track_usage(usage)
                yield ChatResponse(
                    content=[],
                    is_last=False,
                    id=response_id,
                    usage=usage,
                )

        logger.debug("echo 模型回放一轮，script 剩余 {}", len(self.script))
        return _gen()


def build_echo_model(spec: Any, ctx: Any = None) -> EchoChatModel:
    """按 :class:`~harness_kit.config.schema.ModelSpec` 构造回声模型。

    注册进 Layer 0 的工厂（``harness_kit.registry`` 里 ``"echo"`` 的懒加载
    条目指向本函数与 :class:`EchoChatModel`）。

    Args:
        spec (`Any`): ``ModelSpec``；只读 ``model`` / ``stream``。
        ctx (`Any`, optional): ``BuildContext``；不消费。

    Returns:
        `EchoChatModel`: 构造好的模型。
    """
    return EchoChatModel(
        model_name=getattr(spec, "model", "echo") or "echo",
        stream=bool(getattr(spec, "stream", True)),
    )
