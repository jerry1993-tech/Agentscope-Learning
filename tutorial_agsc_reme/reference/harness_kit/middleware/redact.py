# -*- coding: utf-8 -*-
"""敏感信息脱敏中间件（契约 §3.8，第 8 讲）。

**为什么必须在中间件层做，而不是在调用 API 之前做？**

因为要脱敏的文本散落在四个地方，而且形态不同：

1. system prompt —— 它是**字符串**，走 ``on_system_prompt``（7 个 hook 里唯一的
   transformer 型 hook，见 ``third_party/agentscope/src/agentscope/middleware/
   _base.py:264``；其余 6 个都是 onion 型）；
2. 用户输入 —— 它是 ``Msg``，内容在 ``msg.content``（``list[ContentBlock]``）里；
3. 工具返回值 —— 它是 ``ToolChunk`` / ``ToolResponse``，内容在 ``.content`` 里；
4. 模型输出 —— 它会流回 ``Msg`` 进入下一轮 context。

如果只在第 1 处做，第 2/3 处的 ``sk-...`` 会照样写进 ``AgentState`` 并被下一轮
发送出去 —— 而 ``AgentState`` 是要落盘做会话回放的（第 9 讲）。所以本中间件
覆盖 1/2/3 三处（第 4 处由 ``on_reply`` 的输入侧在下一轮兜住）。

**不可变优先**：所有改写都走 ``model_copy(update=...)``，
绝不 ``msg.content[0].text = ...`` —— ``Msg`` 与各 ``ContentBlock`` 都是 pydantic
模型，原地改会污染调用方手里的同一个对象（``on_reply`` 的 ``inputs`` 是
调用方传进来的引用），也会让 ``AgentState`` 的哈希/快照语义失效。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Iterable

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from harness_kit.middleware.base import HarnessMiddleware, call_next_stream

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.agent import Agent
    from agentscope.message import Msg

__all__ = [
    "RedactMiddleware",
    "RedactPattern",
    "default_patterns",
]

_MASK: str = "***"
"""默认替换串。"""


@lru_cache(maxsize=256)
def _compiled(regex: str) -> re.Pattern[str]:
    """编译并缓存正则（``RedactPattern`` 是 frozen 模型，不能存实例属性）。

    Args:
        regex (`str`): 正则文本。

    Returns:
        `re.Pattern[str]`: 编译结果。

    Raises:
        re.error: 正则非法。
    """
    return re.compile(regex)


class RedactPattern(BaseModel):
    """一条脱敏规则（契约 §3.8）。

    Args:
        name (`str`): 规则名，用于统计命中次数；同一中间件内不必唯一，
            但重名会让 :attr:`RedactMiddleware.hits` 合并计数。
        regex (`str`): 正则文本。构造时即编译，非法正则会立刻报错。
        replacement (`str`): 替换串，默认 ``"***"``。支持 ``\\g<name>`` 反向引用
            （``re.sub`` 的模板语法），例如 ``r"\\g<key>=***"`` 可以保留
            ``password=`` 这个键名而只打掉值。

    Raises:
        ValueError: 正则为空或无法编译。

    Example:
        >>> p = RedactPattern(name="token", regex=r"tok_[0-9a-f]{8}")
        >>> p.sub("a tok_deadbeef b")
        ('a *** b', 1)
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """规则名。"""
    regex: str
    """正则文本。"""
    replacement: str = _MASK
    """替换串，支持 ``re.sub`` 模板语法。"""

    @field_validator("regex")
    @classmethod
    def _check_regex(cls, value: str) -> str:
        """构造时编译一次，把非法正则挡在配置加载阶段。

        Args:
            value (`str`): 正则文本。

        Returns:
            `str`: 原样返回。

        Raises:
            ValueError: 正则为空或非法。
        """
        if not value:
            raise ValueError("RedactPattern.regex 不能为空")
        try:
            _compiled(value)
        except re.error as exc:
            raise ValueError(f"非法正则 {value!r}: {exc}") from exc
        return value

    def sub(self, text: str) -> tuple[str, int]:
        """对 ``text`` 执行一次替换。

        替换串按 ``re.sub`` 的**模板**语义解释（支持 ``\\g<name>`` / ``\\1``）；
        模板引用了解不存在或非法时，退回**字面量**语义 —— 用户写
        ``replacement="C:\\\\tmp"`` 这种带反斜杠的普通字符串时不会炸。

        Args:
            text (`str`): 待处理文本。

        Returns:
            `tuple[str, int]`: ``(替换后文本, 命中次数)``。
        """
        if not text:
            return text, 0
        try:
            new, count = _compiled(self.regex).subn(self.replacement, text)
        except (re.error, IndexError):  # 模板引用了不存在的组
            new, count = _compiled(self.regex).subn(
                lambda _m: self.replacement,
                text,
            )
        return new, count


_PATTERNS_ADAPTER: TypeAdapter[list[RedactPattern]] = TypeAdapter(
    list[RedactPattern],
)
"""把"``list[dict]``"（Profile 里的形态）校验成 ``list[RedactPattern]``。

``__init__`` 的参数不走 pydantic 的字段校验（本类不是 pydantic 模型），
而 ``harness_kit.registry`` 的 ``_spec_class_adapter`` 是
``target(**spec.params)`` —— YAML 里写的是纯 dict。没有这一步，
``RedactMiddleware(patterns=[{"name": ..., "regex": ...}])`` 会静默拿到一批
dict，直到第一次脱敏才炸
``AttributeError: 'dict' object has no attribute 'sub'``。"""


def default_patterns() -> list[RedactPattern]:
    """一组开箱即用的常见凭据 / 个人信息规则。

    **不要把它当成合规清单**：它覆盖的是"最常被误贴进日志和 prompt 的东西"，
    真实部署必须按本组织的数据分级再加规则。

    Returns:
        `list[RedactPattern]`: 新构造的规则列表（每次调用返回新对象，
        调用方可以自由增删）。
    """
    return [
        RedactPattern(name="openai_key", regex=r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
        RedactPattern(
            name="anthropic_key",
            regex=r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b",
        ),
        RedactPattern(
            name="aws_access_key",
            regex=r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        ),
        RedactPattern(
            name="bearer_token",
            regex=r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{12,}={0,2}",
        ),
        RedactPattern(
            name="private_key_block",
            regex=(
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
                r"[\s\S]*?"
                r"-----END [A-Z ]*PRIVATE KEY-----"
            ),
        ),
        # URL 里的 user:password@ —— 用 lookbehind 避免吃掉 scheme
        RedactPattern(
            name="url_credentials",
            regex=r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)",
        ),
        RedactPattern(
            name="password_kv",
            regex=(
                r"(?P<key>(?i:password|passwd|pwd|secret|api_key|token))"
                r"\s*[=:]\s*[^\s,;'\"]{4,}"
            ),
            replacement=r"\g<key>=***",
        ),
        RedactPattern(
            name="email",
            regex=r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        ),
        RedactPattern(name="cn_phone", regex=r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        RedactPattern(
            name="cn_id_card",
            regex=r"(?<!\d)\d{17}[\dXx](?!\d)",
        ),
    ]


class RedactMiddleware(HarnessMiddleware):
    """敏感信息脱敏（契约 §3.8）。

    Args:
        patterns (`list[RedactPattern] | None`): 规则列表；``None`` 时用
            :func:`default_patterns`。传空列表表示"什么都不脱敏"（只统计不处理），
            这种配置在调试"到底哪条规则误伤了"时有用。也接受
            ``list[dict]``（Profile / YAML 里的形态），会被校验成 ``RedactPattern``。
        redact_system_prompt (`bool`): 是否处理 ``on_system_prompt``，默认 ``True``。
        redact_inputs (`bool`): 是否在处理 ``on_reply`` 时改写输入 ``Msg``，默认 ``True``。
        redact_tool_results (`bool`): 是否改写 ``on_acting`` 产出的工具结果，默认 ``True``。
        redact_tool_input (`bool`): 是否改写工具**入参**（``ToolCallBlock.input``，
            一个 JSON 字符串），默认 ``False`` —— 改写它可能破坏 JSON 结构，
            只在"确实会把凭据当参数传出去"的场景开。

    Example:
        >>> mw = RedactMiddleware()                                # doctest: +SKIP
        >>> mw.redact_text("key=sk-abcdefghijklmnopqrst")
        'key=***'
    """

    def __init__(
        self,
        *,
        patterns: list[RedactPattern] | None = None,
        redact_system_prompt: bool = True,
        redact_inputs: bool = True,
        redact_tool_results: bool = True,
        redact_tool_input: bool = False,
    ) -> None:
        """初始化。

        Args:
            patterns (`list[RedactPattern] | None`): 见类文档。
            redact_system_prompt (`bool`): 见类文档。
            redact_inputs (`bool`): 见类文档。
            redact_tool_results (`bool`): 见类文档。
            redact_tool_input (`bool`): 见类文档。
        """
        if patterns is None:
            self.patterns: list[RedactPattern] = default_patterns()
        else:
            # 见 _PATTERNS_ADAPTER 的注释：这里必须过一遍校验，
            # 否则 Profile 传进来的 dict 会一路裸奔到 sub() 才炸。
            self.patterns = _PATTERNS_ADAPTER.validate_python(patterns)
        self.redact_system_prompt = redact_system_prompt
        self.redact_inputs = redact_inputs
        self.redact_tool_results = redact_tool_results
        self.redact_tool_input = redact_tool_input

        self.hits: dict[str, int] = {p.name: 0 for p in self.patterns}
        """每条规则的累计命中次数，供看板 / 告警使用。"""

    # ------------------------------------------------------------------
    # 纯函数部分
    # ------------------------------------------------------------------
    def redact_text(self, text: str, *, counter: dict[str, int] | None = None) -> str:
        """对一段文本跑完所有规则。

        Args:
            text (`str`): 待处理文本。
            counter (`dict[str, int] | None`): 命中计数累加到哪里；
                ``None`` 时累加到 :attr:`hits`。

        Returns:
            `str`: 处理后的文本；无命中时**原样返回同一个对象**。
        """
        if not text:
            return text
        sink = self.hits if counter is None else counter
        out = text
        for pattern in self.patterns:
            out, count = pattern.sub(out)
            if count:
                sink[pattern.name] = sink.get(pattern.name, 0) + count
        return out

    def redact_block(self, block: Any) -> Any:
        """改写单个 ``ContentBlock``（不可变：走 ``model_copy``）。

        Args:
            block (`Any`): ``TextBlock`` / ``ThinkingBlock`` / ``ToolResultBlock`` /
                其他 ``ContentBlock``。

        Returns:
            `Any`: 改写后的块；无需改动时返回**原对象**（便于上层做 `is` 判断短路）。
        """
        block_type = getattr(block, "type", None)
        if block_type == "text":
            new_text = self.redact_text(block.text)
            if new_text != block.text:
                return block.model_copy(update={"text": new_text})
            return block
        if block_type == "thinking":
            new_text = self.redact_text(block.thinking)
            if new_text != block.thinking:
                return block.model_copy(update={"thinking": new_text})
            return block
        if block_type == "tool_result":
            output = block.output
            if isinstance(output, str):
                new_output: Any = self.redact_text(output)
            else:
                new_output = [self.redact_block(item) for item in output]
            if new_output != output:
                return block.model_copy(update={"output": new_output})
            return block
        if block_type == "tool_call":
            if not self.redact_tool_input:
                return block
            new_input = self.redact_text(block.input)
            if new_input != block.input:
                return block.model_copy(update={"input": new_input})
            return block
        # data / hint / 未知类型：DataBlock 是二进制，不碰
        return block

    def redact_message(self, msg: "Msg") -> "Msg":
        """改写一条 ``Msg``。

        Args:
            msg (`Msg`): 原始消息。

        Returns:
            `Msg`: 改写后的消息；无命中时返回**原对象**。
        """
        new_blocks = [self.redact_block(block) for block in msg.content]
        if all(new is old for new, old in zip(new_blocks, msg.content)):
            return msg
        # 注意 Msg 上有 model_validator(mode="after") 校验 role/content 组合；
        # pydantic v2 的 model_copy 默认不重新校验，所以这里不会因改写而失败。
        return msg.model_copy(update={"content": new_blocks})

    def redact_value(self, value: Any) -> Any:
        """按类型分派：``Msg`` / ``Msg`` 列表 / ``ToolChunk`` / ``ToolResponse``。

        Args:
            value (`Any`): 任意值。

        Returns:
            `Any`: 改写后的值；类型不认识时原样返回。
        """
        if value is None:
            return None
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        # Msg 与 ToolChunk / ToolResponse 都有 content: list[block]
        content = getattr(value, "content", None)
        if isinstance(content, list) and content and hasattr(
            content[0],
            "type",
        ):
            new_blocks = [self.redact_block(block) for block in content]
            if all(new is old for new, old in zip(new_blocks, content)):
                return value
            return value.model_copy(update={"content": new_blocks})
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def snapshot(self) -> dict[str, int]:
        """返回命中统计的副本。

        Returns:
            `dict[str, int]`: 规则名 → 命中次数。
        """
        return dict(self.hits)

    def total_hits(self) -> int:
        """全部规则的命中总次数。

        Returns:
            `int`: 总命中次数。
        """
        return sum(self.hits.values())

    def describe(self) -> dict[str, Any]:
        """覆写基类摘要，把规则名也带上。

        Returns:
            `dict[str, Any]`: ``{"middleware", "hooks", "patterns"}``。
        """
        return {
            **super().describe(),
            "patterns": [p.name for p in self.patterns],
        }

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    async def on_system_prompt(self, agent: "Agent", current_prompt: str) -> str:
        """transformer hook：脱敏 system prompt。

        Args:
            agent (`Agent`): 当前 Agent。
            current_prompt (`str`): 上游产出的 prompt。

        Returns:
            `str`: 脱敏后的 prompt。
        """
        if not self.redact_system_prompt:
            return current_prompt
        new_prompt = self.redact_text(current_prompt)
        if new_prompt != current_prompt:
            logger.bind(
                middleware=self.name(),
                agent=agent.name,
                where="system_prompt",
                hits=self.total_hits(),
            ).debug("system prompt 已脱敏")
        return new_prompt

    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """onion hook：脱敏本轮输入，再透传事件流。

        Args:
            agent (`Agent`): 发起 reply 的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``inputs``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的事件。
        """
        if self.redact_inputs and "inputs" in input_kwargs:
            input_kwargs = {
                **input_kwargs,
                "inputs": self.redact_value(input_kwargs["inputs"]),
            }
        async for event in call_next_stream(next_handler, input_kwargs):
            yield event

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """onion hook：脱敏工具调用结果（``ToolChunk`` / ``ToolResponse``）。

        Args:
            agent (`Agent`): 执行工具调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``tool_call``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 脱敏后的 ``ToolChunk`` / ``ToolResponse``。
        """
        masked = 0
        async for item in call_next_stream(next_handler, input_kwargs):
            if self.redact_tool_results:
                before = self.total_hits()
                item = self.redact_value(item)
                masked += self.total_hits() - before
            yield item
        if masked:
            logger.bind(
                middleware=self.name(),
                agent=agent.name,
                tool=getattr(input_kwargs.get("tool_call"), "name", "<unknown>"),
                hits=masked,
            ).info("工具结果已脱敏 {} 处", masked)


def redact_text(text: str, patterns: Iterable[RedactPattern]) -> str:
    """一次性脱敏的便捷函数（不经过中间件）。

    Args:
        text (`str`): 待处理文本。
        patterns (`Iterable[RedactPattern]`): 规则。

    Returns:
        `str`: 处理后的文本。

    Example:
        >>> redact_text("AKIAIOSFODNN7EXAMPLE", default_patterns())
        '***'
    """
    out = text
    for pattern in patterns:
        out, _ = pattern.sub(out)
    return out
