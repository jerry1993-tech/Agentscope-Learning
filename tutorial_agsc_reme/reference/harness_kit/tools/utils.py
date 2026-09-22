# -*- coding: utf-8 -*-
"""工具层的三个「脏活」帮手：schema 收紧、参数修复、结果截断。

这三件事 AgentScope 都**已经做了**，但都不是以「可单独调用的公开函数」形式
给出的，所以这里的实现是**薄包装 + 补足**，不是重写：

============================ ==================================================
本模块                         底层真实实现
============================ ==================================================
:func:`repair_arguments`      ``agentscope._utils._common._json_loads_with_repair``
                              （``third_party/agentscope/src/agentscope/
                              _utils/_common.py:95``），**私有函数**，但
                              ``Toolkit.call_tool`` 自己也在用它
                              （``.../tool/_toolkit.py:34`` 的 import），
                              因此依赖它是安全的
:func:`ensure_strict_schema`  无底层实现 —— AgentScope 直接把工具函数签名
                              生成的 schema 原样发给模型
:func:`summarize_tool_result` 无底层实现 —— AgentScope 不做结果截断，
                              上下文膨胀得自己管
============================ ==================================================

**为什么需要 :func:`repair_arguments`**：模型返回的工具参数是**字符串形式的
JSON**（``ToolCallBlock.input``），它不是可靠的机器产物 —— 实测里能见到的坏
形状包括：外层包了 ```` ```json ```` 代码块、用了单引号、尾随逗号、少一个右
花括号（被 ``max_tokens`` 截断）。``_json_loads_with_repair`` 底层用
``json_repair`` 库，已经能吃掉其中大半；本模块在它失败后再补几个**确定性**
的字符串级修补，全部失败才抛 :class:`ArgumentRepairError`。
"""

from __future__ import annotations

import ast
import copy
import json
import re
from typing import Any, Iterable

from loguru import logger

from agentscope._utils._common import _json_loads_with_repair
from agentscope.exception import ToolJSONDecodeError

__all__ = [
    "ArgumentRepairError",
    "ensure_strict_schema",
    "repair_arguments",
    "summarize_tool_result",
]

_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON|python)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL,
)
"""剥掉外层 markdown 代码块围栏。模型极爱这么干。"""

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
"""尾随逗号：``{"a": 1,}`` / ``[1, 2,]``。JSON 不允许，Python 允许。"""

_SMART_QUOTES = {
    "“": '"',  # “
    "”": '"',  # ”
    "‘": "'",  # ‘
    "’": "'",  # ’
}
"""中文环境里模型经常输出弯引号当 JSON 引号。"""

_JSON_STRICT_DUMPS = {"ensure_ascii": False, "allow_nan": False}


class ArgumentRepairError(ValueError):
    """参数字符串无法被修复成 dict。

    调用方（工具实现）应当把它转成 ``ToolChunk(state=ToolResultState.ERROR)``
    **而不是**让异常冒泡 —— 契约 §3.5 明确要求：

        「仍失败 → ArgumentRepairError（工具应把它变成
        ToolChunk(state=ERROR) 而不是抛异常）」

    为什么必须这样：异常冒泡会打断 Agent Loop 的工具执行阶段，而模型**看不
    到**异常内容，下一轮它会一模一样地再犯一次。把错误写回 tool result，模型
    才有机会自我纠正。

    Attributes:
        raw (`str`): 原始参数字符串（已截断到 500 字符，避免日志爆炸）。
        attempts (`list[str]`): 依次尝试过的修复手段名。
    """

    def __init__(
        self,
        message: str,
        *,
        raw: str = "",
        attempts: Iterable[str] = (),
    ) -> None:
        """构造错误。

        Args:
            message (`str`): 人读错误信息。
            raw (`str`): 原始参数字符串。
            attempts (`Iterable[str]`): 尝试过的修复手段名。
        """
        super().__init__(message)
        self.raw = raw[:500]
        self.attempts = list(attempts)


def _balance_brackets(text: str) -> str:
    """补齐缺失的右括号 / 右方括号（截断场景的救命手段）。

    只在**字符串之外**计数，避免把 ``{"a": "}"}`` 里的花括号算进去。

    Args:
        text (`str`): 待修补的文本。

    Returns:
        `str`: 补全后的文本；原本就平衡时原样返回。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if in_string:
        text += '"'
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"
    return text


def _candidate_fixups(raw: str) -> list[tuple[str, str]]:
    """列出所有「确定性的字符串级修补」及其结果。

    Args:
        raw (`str`): 原始参数字符串。

    Returns:
        `list[tuple[str, str]]`: ``(手段名, 修补后文本)``，按尝试顺序。
    """
    candidates: list[tuple[str, str]] = []

    text = raw.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group("body").strip()
        candidates.append(("strip_code_fence", text))

    smart = text
    for source, target in _SMART_QUOTES.items():
        smart = smart.replace(source, target)
    if smart != text:
        candidates.append(("normalize_smart_quotes", smart))
        text = smart

    no_trailing = _TRAILING_COMMA_RE.sub(r"\1", text)
    if no_trailing != text:
        candidates.append(("drop_trailing_commas", no_trailing))
        text = no_trailing

    balanced = _balance_brackets(text)
    if balanced != text:
        candidates.append(("balance_brackets", balanced))

    return candidates


def _try_literal_eval(text: str) -> dict[str, Any] | None:
    """用 ``ast.literal_eval`` 再试一次（吃单引号、Python 字面量）。

    这是一条**只在字符串层修补都失败后**才走的路 —— 它比 ``json.loads``
    宽松得多（能解析单引号、``True``/``None``），也因此更危险。所以：
    ``True`` → ``true``、``None`` → ``null`` 的转换必须做，否则得到的
    dict 里有 Python 对象，后面 ``json.dumps`` 会炸。

    Args:
        text (`str`): 待解析文本。

    Returns:
        `dict[str, Any] | None`: 解析结果；不是 dict 或解析失败时 ``None``。
    """
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return None


def repair_arguments(
    raw: str,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把模型给的参数字符串解析成 dict，尽量不失败。

    流程（每一步失败才走下一步）：

    1. 空串 / 纯空白 → ``{}``（无参工具的合法输入，不该报错）；
    2. ``agentscope._utils._common._json_loads_with_repair(raw, schema)``
       —— 底层 ``json_repair`` 库，且会按 ``schema`` 修**类型**
       （如把 ``"42"`` 修成 ``42``）；
    3. 本模块的确定性字符串修补（去代码围栏 / 弯引号 / 尾逗号 / 补括号），
       每修一次就重试第 2 步；
    4. ``ast.literal_eval`` 兜底；
    5. 全失败 → :class:`ArgumentRepairError`。

    Args:
        raw (`str`): 参数字符串，通常是 ``ToolCallBlock.input``。
        schema (`dict[str, Any] | None`): 工具入参 JSON schema；``None``
            时跳过类型修复，只做语法修复。

    Returns:
        `dict[str, Any]`: 解析出的参数。

    Raises:
        ArgumentRepairError: 所有手段都失败。
    """
    if raw is None or not raw.strip():
        return {}

    attempts: list[str] = []

    def _try(text: str) -> dict[str, Any] | None:
        """跑一次 ``_json_loads_with_repair``。

        Args:
            text (`str`): 待解析文本。

        Returns:
            `dict[str, Any] | None`: 成功时的结果。
        """
        try:
            result = _json_loads_with_repair(text, schema)
        except ToolJSONDecodeError:
            return None
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("json 修复时出现非预期异常：{}: {}", type(exc).__name__, exc)
            return None
        return result if isinstance(result, dict) else None

    attempts.append("json_loads_with_repair")
    result = _try(raw)
    if result is not None:
        return result

    for name, fixed in _candidate_fixups(raw):
        attempts.append(name)
        result = _try(fixed)
        if result is not None:
            logger.debug("参数修复成功，手段={}", name)
            return result

    literal_source = _candidate_fixups(raw)
    literal_text = (
        literal_source[-1][1] if literal_source else raw.strip()
    )
    attempts.append("ast_literal_eval")
    result = _try_literal_eval(literal_text)
    if result is not None:
        logger.debug("参数修复成功，手段=ast_literal_eval")
        return result

    raise ArgumentRepairError(
        "工具参数无法解析为 JSON 对象。请**重新生成**完整、合法的 JSON 参数，"
        "不要用代码块包裹，也不要使用单引号。"
        f"原始片段：{raw[:200]!r}",
        raw=raw,
        attempts=attempts,
    )


def ensure_strict_schema(
    schema: dict[str, Any],
    *,
    additional_properties: bool = True,
    require_all: bool = False,
) -> dict[str, Any]:
    """收紧 JSON schema，降低模型「乱填参数」的概率。

    做三件事（**递归**，且深拷贝，绝不改原对象）：

    1. 每个 ``type == "object"`` 的节点补 ``"additionalProperties": False``；
    2. 每个 object 节点保证有 ``"required"`` 键（缺失则补 ``[]``）；
    3. ``require_all=True`` 时，把 ``required`` 补成全部 properties 的键。

    **默认 ``require_all=False`` 是刻意的**：把可选参数变成必填，会让模型
    在「用户没说要排序」时也硬编一个 ``sort=...``，实测弊大于利。真要强制，
    请在工具定义里用 ``typing.Literal`` / ``Annotated[..., Field(...)]``
    表达（AgentScope 的 ``FunctionTool`` 会从签名生成 schema，
    见 ``third_party/agentscope/src/agentscope/tool/_adapters.py:96``）。

    Args:
        schema (`dict[str, Any]`): 原始 JSON schema。
        additional_properties (`bool`): 是否补 ``additionalProperties: False``。
            默认 ``True``。
        require_all (`bool`): 是否把所有 properties 变成必填。默认 ``False``。

    Returns:
        `dict[str, Any]`: 收紧后的新 schema。

    Raises:
        TypeError: ``schema`` 不是 dict。
    """
    if not isinstance(schema, dict):
        raise TypeError(
            f"ensure_strict_schema 需要 dict，收到 {type(schema).__name__}",
        )

    def _walk(node: Any) -> Any:
        """递归处理一个 schema 节点。

        Args:
            node (`Any`): 当前节点。

        Returns:
            `Any`: 处理后的节点。
        """
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if not isinstance(node, dict):
            return node

        result = {key: _walk(value) for key, value in node.items()}
        is_object = result.get("type") == "object" or "properties" in result

        if is_object:
            if additional_properties and "additionalProperties" not in result:
                result["additionalProperties"] = False
            properties = result.get("properties")
            if isinstance(properties, dict):
                if "required" not in result:
                    result["required"] = []
                if require_all and isinstance(result["required"], list):
                    result["required"] = sorted(properties)
        return result

    return _walk(copy.deepcopy(schema))


def summarize_tool_result(
    text: str,
    *,
    max_chars: int,
    head_ratio: float = 0.6,
    marker: str = "\n...[已截断 {dropped} 字符]...\n",
) -> str:
    """把过长的工具结果截断成「头 + 尾」，中间插一段显式标记。

    为什么留尾巴而不是只留头：**错误信息几乎总在尾部**（编译错误、测试失败的
    汇总、traceback 的最后一帧）。只留头会让模型看不到最关键的一行。

    为什么标记里要写**丢了多少字符**：模型需要知道自己没看到全部内容，才能
    决定「要不要分段再读一次」；静默截断会让它以为文件就这么短。

    Args:
        text (`str`): 工具输出原文。
        max_chars (`int`): 允许的最大字符数；``<= 0`` 表示不截断。
        head_ratio (`float`): 头部占比，``0.6`` 表示头 60% 尾 40%。
        marker (`str`): 截断标记模板，含 ``{dropped}`` 占位。

    Returns:
        `str`: 截断后的文本；未超限时**原样返回**（不做任何改动）。

    Raises:
        ValueError: ``head_ratio`` 不在 ``(0, 1)`` 区间。
    """
    if not 0 < head_ratio < 1:
        raise ValueError(f"head_ratio 必须在 (0, 1) 之间，收到 {head_ratio}")

    if max_chars <= 0 or len(text) <= max_chars:
        return text

    head_len = int(max_chars * head_ratio)
    tail_len = max_chars - head_len
    dropped = len(text) - head_len - tail_len
    return (
        text[:head_len]
        + marker.format(dropped=dropped)
        + (text[-tail_len:] if tail_len else "")
    )
