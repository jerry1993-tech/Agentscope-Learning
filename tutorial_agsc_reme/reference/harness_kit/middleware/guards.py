# -*- coding: utf-8 -*-
"""护栏中间件：循环保护 + 输入护栏（契约 §3.8，第 8 讲）。

**为什么"重复工具调用"是最该拦的那件事？**

AgentScope 的 reasoning-acting 循环有 ``max_iters`` 上限
（``third_party/agentscope/src/agentscope/agent/_agent.py`` 的 reply 参数），
所以它**不会死循环**。但"同一轮里连着调十次同一个工具、传一模一样的参数"
是完全合法的：模型只是卡住了，每次调用都真的花钱。
``max_iters`` 要到第 N 轮才生效，而这里在第 3 次调用时就能喊停。

契约 §3.8 只要求了这一条（``max_repeat_tool_calls``）。本实现另外补了两类
输入护栏，因为它们在真实接入 LLM 的第一天就会遇到：

1. **注入模式**（``injection_patterns``）—— 用户输入里出现
   "ignore all previous instructions" 这类模板句。注意：
   **正则匹配不是安全边界**，它只用于"提高攻击成本 + 打点告警"，
   真正的防线是权限系统（AgentScope 的 ``PermissionEngine``）与工具白名单。
   这句话必须写进正文，不能让读者以为加了正则可以不做权限。
2. **禁用话题**（``forbidden_topics``）—— 合规 / 业务红线，
   命中的处理动作由 ``action`` 决定。

**输出侧**只做工具结果的长度上限（``max_tool_result_chars``）：一次
``cat`` 一个 20MB 的日志文件，会把后面所有轮次的 prompt 都撑爆。
真正"审查模型说了什么"属于评测层（第 10 讲）的职责，不该塞进中间件。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Literal, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, TypeAdapter, field_validator

from harness_kit.middleware.base import HarnessMiddleware, call_next_stream

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.agent import Agent
    from agentscope.message import Msg

__all__ = [
    "GuardPattern",
    "GuardTrippedError",
    "GuardsMiddleware",
    "default_injection_patterns",
]


@lru_cache(maxsize=256)
def _compiled(regex: str) -> re.Pattern[str]:
    """编译并缓存正则。

    Args:
        regex (`str`): 正则文本。

    Returns:
        `re.Pattern[str]`: 编译结果。
    """
    return re.compile(regex, re.IGNORECASE)


class GuardTrippedError(RuntimeError):
    """护栏被触发（契约 §3.8）。

    Args:
        message (`str`): 人类可读的说明。
        rule (`str`): 触发的规则名，便于告警分组。
        detail (`str | None`): 额外细节（命中的片段、重复次数等）。
    """

    def __init__(
        self,
        message: str,
        *,
        rule: str = "",
        detail: str | None = None,
    ) -> None:
        """初始化。

        Args:
            message (`str`): 说明。
            rule (`str`): 规则名。
            detail (`str | None`): 细节。
        """
        super().__init__(message)
        self.rule = rule
        self.detail = detail


class GuardPattern(BaseModel):
    """一条命名正则护栏。

    Args:
        name (`str`): 规则名。
        regex (`str`): 正则文本，匹配时**忽略大小写**。

    Raises:
        ValueError: 正则为空或非法。

    Example:
        >>> GuardPattern(name="x", regex="ignore previous").search("IGNORE PREVIOUS")
        <re.Match object; span=(0, 15), match='IGNORE PREVIOUS'>
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """规则名。"""
    regex: str
    """正则文本，忽略大小写。"""

    @field_validator("regex")
    @classmethod
    def _check_regex(cls, value: str) -> str:
        """构造时编译一次。

        Args:
            value (`str`): 正则文本。

        Returns:
            `str`: 原样返回。

        Raises:
            ValueError: 正则为空或非法。
        """
        if not value:
            raise ValueError("GuardPattern.regex 不能为空")
        try:
            _compiled(value)
        except re.error as exc:
            raise ValueError(f"非法正则 {value!r}: {exc}") from exc
        return value

    def search(self, text: str) -> "re.Match[str] | None":
        """在文本里找第一个命中。

        Args:
            text (`str`): 待检查文本。

        Returns:
            `re.Match[str] | None`: 命中对象；未命中为 ``None``。
        """
        return _compiled(self.regex).search(text)


_PATTERNS_ADAPTER: TypeAdapter[list[GuardPattern]] = TypeAdapter(
    list[GuardPattern],
)
"""把 ``list[dict]``（Profile / YAML 里的形态）校验成 ``list[GuardPattern]``。

理由与 ``harness_kit/middleware/redact.py`` 里的同名适配器一致：
``__init__`` 的参数绕过了 pydantic 的字段校验，而
``harness_kit.registry`` 的 ``_spec_class_adapter`` 是 ``target(**spec.params)``。
"""


def default_injection_patterns() -> list[GuardPattern]:
    """一组常见的 prompt injection 模板句（中英双语）。

    **它不是安全边界**，只是"提高攻击成本 + 让告警有东西可看"。
    任何一条都能被改写绕过（换词、加空格、base64），
    真正的防线是工具权限与最小授权。

    Returns:
        `list[GuardPattern]`: 新构造的规则列表。
    """
    return [
        GuardPattern(
            name="ignore_instructions",
            regex=(
                r"(?:ignore|disregard|forget)\s+"
                r"(?:all\s+|any\s+|the\s+)?"
                r"(?:previous|prior|above|earlier|foregoing)\s+"
                r"(?:instructions?|prompts?|rules?|messages?)"
            ),
        ),
        GuardPattern(
            name="ignore_instructions_zh",
            regex=r"(?:忽略|无视|忘记)(?:之前|以上|前面|先前)的?(?:所有|全部)?(?:指令|规则|提示|要求)",
        ),
        GuardPattern(
            name="reveal_system_prompt",
            regex=(
                r"(?:reveal|show|print|repeat|输出|显示|打印|告诉我)"
                r"[^.\n]{0,16}"
                r"(?:system\s*prompt|your\s+instructions|initial\s+prompt|系统提示|系统指令)"
            ),
        ),
        GuardPattern(
            name="role_override",
            regex=(
                r"(?:you\s+are\s+now|from\s+now\s+on\s+you|"
                r"act\s+as\s+(?:a\s+)?(?:dan|jailbroken)|"
                r"你现在是|从现在起你是)"
            ),
        ),
        GuardPattern(
            name="developer_mode",
            regex=r"(?:developer|debug|god)\s*mode\s*(?:enabled|on|:)|开发者模式",
        ),
        GuardPattern(
            name="exfiltrate_secrets",
            regex=(
                r"(?:print|show|send|leak|exfiltrate|发送|泄露|输出)"
                r"[^.\n]{0,16}"
                r"(?:api[_\s-]?key|secret|password|token|\.env|凭据|密钥)"
            ),
        ),
    ]


class GuardsMiddleware(HarnessMiddleware):
    """循环保护 + 输入护栏（契约 §3.8）。

    Args:
        max_repeat_tool_calls (`int`): 连续相同的 ``(tool_name, input)`` 允许的
            最大次数，默认 3；超过即触发。设为 ``0`` 表示"同一工具同参数
            只能调一次"，设为负数表示关闭本项检查。
        max_input_chars (`int`): 单轮输入字符数上限，默认 20000；
            ``0`` 或负数表示关闭。
        injection_patterns (`Sequence[GuardPattern] | None`): 注入模式;
            ``None`` 时用 :func:`default_injection_patterns`。
        forbidden_topics (`Sequence[str] | None`): 禁用词，大小写不敏感的子串匹配。
        max_tool_result_chars (`int`): 单次工具结果字符数上限，默认 200000；
            ``0`` 或负数表示关闭。
        action (`Literal["raise", "warn"]`): 触发时的动作，默认 ``"raise"``
            （抛 :class:`GuardTrippedError`）。``"warn"`` 只打 warning 日志并计数，
            适合"先观测一周再上闸"的灰度期。

    Example:
        >>> mw = GuardsMiddleware(max_repeat_tool_calls=2, action="warn")  # doctest: +SKIP
        >>> mw.trip_count                                               # doctest: +SKIP
        0
    """

    def __init__(
        self,
        *,
        max_repeat_tool_calls: int = 3,
        max_input_chars: int = 20000,
        injection_patterns: Sequence[GuardPattern] | None = None,
        forbidden_topics: Sequence[str] | None = None,
        max_tool_result_chars: int = 200000,
        action: Literal["raise", "warn"] = "raise",
    ) -> None:
        """初始化。

        Args:
            max_repeat_tool_calls (`int`): 见类文档。
            max_input_chars (`int`): 见类文档。
            injection_patterns (`Sequence[GuardPattern] | None`): 见类文档。
            forbidden_topics (`Sequence[str] | None`): 见类文档。
            max_tool_result_chars (`int`): 见类文档。
            action (`Literal["raise", "warn"]`): 见类文档。

        Raises:
            ValueError: ``action`` 取值非法。
        """
        if action not in ("raise", "warn"):
            raise ValueError(
                f"action 只能是 'raise' 或 'warn'，收到 {action!r}",
            )
        self.max_repeat_tool_calls = int(max_repeat_tool_calls)
        self.max_input_chars = int(max_input_chars)
        if injection_patterns is None:
            self.injection_patterns: list[GuardPattern] = (
                default_injection_patterns()
            )
        else:
            # Profile 传进来的是 dict；见 _PATTERNS_ADAPTER 的注释
            self.injection_patterns = _PATTERNS_ADAPTER.validate_python(
                injection_patterns,
            )
        self.forbidden_topics: list[str] = [
            str(topic).lower() for topic in (forbidden_topics or [])
        ]
        self.max_tool_result_chars = int(max_tool_result_chars)
        self.action = action

        self.trip_count: int = 0
        """触发总次数。"""
        self.trips: dict[str, int] = {}
        """按规则名分组的触发次数。"""
        self._last_tool_key: str | None = None
        self._repeat: int = 0

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    def _trip(self, rule: str, message: str, *, detail: str | None = None) -> None:
        """按 ``action`` 处理一次触发。

        Args:
            rule (`str`): 规则名。
            message (`str`): 说明。
            detail (`str | None`): 细节。

        Raises:
            GuardTrippedError: ``action="raise"``。
        """
        self.trip_count += 1
        self.trips[rule] = self.trips.get(rule, 0) + 1
        logger.bind(
            middleware=self.name(),
            rule=rule,
            detail=detail,
            action=self.action,
            trips=self.trip_count,
        ).warning("护栏触发：{}", message)
        if self.action == "raise":
            raise GuardTrippedError(message, rule=rule, detail=detail)

    def snapshot(self) -> dict[str, Any]:
        """返回触发统计。

        Returns:
            `dict[str, Any]`: ``{"trips": 总数, "by_rule": {...}}``。
        """
        return {"trips": self.trip_count, "by_rule": dict(self.trips)}

    def reset(self) -> None:
        """清零重复调用检测状态（统计量保留）。"""
        self._last_tool_key = None
        self._repeat = 0

    # ------------------------------------------------------------------
    # 检查函数（纯逻辑，便于单测）
    # ------------------------------------------------------------------
    def check_text(self, text: str, *, where: str) -> None:
        """对一段文本跑长度 / 注入 / 禁用词三项检查。

        Args:
            text (`str`): 待检查文本。
            where (`str`): 来源标识（``"input"`` / ``"tool_result"``），进日志。

        Raises:
            GuardTrippedError: ``action="raise"`` 且命中。
        """
        if not text:
            return
        limit = self.max_input_chars if where == "input" else self.max_tool_result_chars
        if limit and limit > 0 and len(text) > limit:
            self._trip(
                "max_chars",
                f"{where} 长度 {len(text)} 超过上限 {limit}",
                detail=where,
            )
        if where != "input":
            return
        for pattern in self.injection_patterns:
            match = pattern.search(text)
            if match is not None:
                self._trip(
                    f"injection:{pattern.name}",
                    f"输入命中注入模式 {pattern.name}",
                    detail=match.group(0)[:80],
                )
        lowered = text.lower()
        for topic in self.forbidden_topics:
            if topic in lowered:
                self._trip(
                    "forbidden_topic",
                    f"输入命中禁用话题 {topic!r}",
                    detail=topic,
                )

    @staticmethod
    def canonical_tool_key(tool_name: str, raw_input: Any) -> str:
        """把 ``(工具名, 入参)`` 归一成一个可比较的字符串。

        入参在 ``ToolCallBlock.input`` 里是"模型逐字吐出来的 JSON 字符串"
        （``third_party/agentscope/src/agentscope/message/_block.py:151``），
        同一份参数可能因为空格 / 键顺序不同而字符串不等。所以这里先按 JSON
        解析再按 key 排序重排；解析失败（流式截断等）则退回"去空白后的原文"。

        Args:
            tool_name (`str`): 工具名。
            raw_input (`Any`): 入参（JSON 字符串或已解析的 dict）。

        Returns:
            `str`: 归一化后的键。
        """
        payload = raw_input
        if isinstance(raw_input, str):
            try:
                payload = json.loads(raw_input)
            except (TypeError, ValueError):
                payload = " ".join(raw_input.split())
        try:
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 极端不可序列化
            canonical = repr(payload)
        return f"{tool_name}::{canonical}"

    def observe_tool_call(self, tool_name: str, raw_input: Any) -> int:
        """记录一次工具调用并返回当前的连续重复次数。

        Args:
            tool_name (`str`): 工具名。
            raw_input (`Any`): 入参。

        Returns:
            `int`: 连续重复次数（首次为 1）。
        """
        key = self.canonical_tool_key(tool_name, raw_input)
        if key == self._last_tool_key:
            self._repeat += 1
        else:
            self._last_tool_key = key
            self._repeat = 1
        return self._repeat

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """onion hook：进入 reply 前检查输入，并重置重复计数。

        Args:
            agent (`Agent`): 发起 reply 的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``inputs``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的事件。

        Raises:
            GuardTrippedError: ``action="raise"`` 且输入触发护栏。
        """
        self.reset()
        for text in _iter_texts(input_kwargs.get("inputs")):
            self.check_text(text, where="input")
        async for event in call_next_stream(next_handler, input_kwargs):
            yield event

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """onion hook：数重复调用，并检查工具结果长度。

        Args:
            agent (`Agent`): 执行工具调用的 Agent。
            input_kwargs (`dict[str, Any]`): 含 ``tool_call``。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一环。

        Yields:
            `Any`: 原样透传的 ``ToolChunk`` / ``ToolResponse``。

        Raises:
            GuardTrippedError: ``action="raise"`` 且触发护栏。
        """
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "<unknown>"))
        repeat = self.observe_tool_call(tool_name, getattr(tool_call, "input", None))
        if (
            self.max_repeat_tool_calls >= 0
            and repeat > self.max_repeat_tool_calls
        ):
            self._trip(
                "repeat_tool_call",
                f"工具 {tool_name} 以相同参数连续调用 {repeat} 次，"
                f"超过上限 {self.max_repeat_tool_calls}",
                detail=self._last_tool_key,
            )
        logger.bind(
            middleware=self.name(),
            agent=agent.name,
            tool=tool_name,
            repeat=repeat,
        ).debug("工具调用重复计数")

        async for item in call_next_stream(next_handler, input_kwargs):
            if self.max_tool_result_chars > 0:
                for block in getattr(item, "content", None) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and len(text) > self.max_tool_result_chars:
                        self._trip(
                            "max_tool_result_chars",
                            f"工具 {tool_name} 的结果长度 {len(text)} "
                            f"超过上限 {self.max_tool_result_chars}",
                            detail=tool_name,
                        )
            yield item


def _iter_texts(value: Any) -> list[str]:
    """把 ``inputs`` 里的文本都抽出来（``Msg`` / 列表 / 裸字符串）。

    Args:
        value (`Any`): ``on_reply`` 的 ``inputs``。

    Returns:
        `list[str]`: 文本列表；抽不出来时为空列表。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_iter_texts(item))
        return out
    getter = getattr(value, "get_text_content", None)
    if callable(getter):
        text = getter()
        return [text] if isinstance(text, str) else []
    return []
