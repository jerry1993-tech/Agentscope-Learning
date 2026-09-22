# -*- coding: utf-8 -*-
"""记忆文件的 front matter 规范：解析、渲染、字段归一、tag 规范化。

**ReMe 原生 front matter 的真值**

``third_party/ReMe/reme/schema/file_front_matter.py:8-14``：

.. code-block:: python

    class FileFrontMatter(BaseModel):
        model_config = ConfigDict(extra="allow")
        name: str = Field(default="", ...)
        description: str = Field(default="", ...)

也就是说**只有 ``name`` / ``description`` 是一等字段**，其余键一律进
``__pydantic_extra__``（``:16`` 的 ``model_extra`` 属性把它暴露出来）。
ReMe 的 tag_index 正是从这里取标签的：``tag_key`` 默认 ``"memory_tags"``
（``third_party/ReMe/reme/constants.py:34``），取法是
``node.front_matter.model_extra.get(self.tag_key)``
（``third_party/ReMe/reme/components/tag_index/local_tag_index.py:77-78``）。

**为什么 harness 侧要再包一层**

1. ReMe 的 ``_parse_front_matter`` 在解析失败时**静默吞掉**整个 front matter
   （``third_party/ReMe/reme/components/file_chunker/default_file_chunker.py:52-58``），
   返回空 ``FileFrontMatter()``。写记忆时一个 YAML 缩进错误会让 "标签没了、name 也没了"，
   而且不报错。harness 侧需要能**主动**发现这种问题。
2. 契约 §3.16 要求一个统一视图 :class:`FrontMatter`，把 "一等字段 / 扩展字段" 的分界线
   显式画出来，供教程讲解，也供 :mod:`harness_kit.memory.ingest` 写标签。
3. **只读解析器** ``split_front_matter`` 需要和 ReMe 的分隔符规则**逐字一致**，
   否则同一份文件两边的理解会漂移。ReMe 的规则是：
   ``text.startswith("---")`` 且找 ``"\n---"``（``default_file_chunker.py:44-49``），
   正文取分隔符之后并以 ``lstrip("\n")`` 去掉紧随的空行。

**tag 的规范化规则（写入侧）**

``local_tag_index.py:33-54`` 的 ``_normalize_tags``：

1. ``"_".join(str(item).split())`` —— 所有空白折叠成 ``_``；
2. 空串或长度 ``> max_tag_length``（默认 64，``constants.py:36``）→ 丢弃；
3. 必须至少含一个 ``isalnum()`` 字符 → 否则丢弃；
4. ``casefold()`` 归一化成小写；
5. 去重；
6. **每个文件最多 ``max_tags_per_file`` 个（默认 3，``constants.py:35``）**。

而查询侧 ``normalize_query_tags``（``:60-62``）传 ``limit=None``，
**不设上限** —— 写入限制 ≠ 查询限制，这是 ReMe 刻意的设计
（``:132-134`` 的注释："``max_tags_per_file`` constrains indexed documents,
not lookup expressions.  Truncating here would silently weaken AND queries"）。
harness 侧必须原样保留这条区分，不能"统一成 3 个"。
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_MAX_TAG_LENGTH",
    "DEFAULT_MAX_TAGS_PER_FILE",
    "DEFAULT_TAG_KEY",
    "FrontMatter",
    "FrontMatterError",
    "normalize_query_tags",
    "normalize_tags",
    "split_front_matter",
]

#: ReMe 存放记忆标签的 front matter 键（``third_party/ReMe/reme/constants.py:34``）。
DEFAULT_TAG_KEY: str = "memory_tags"

#: 单文件标签数上限（``third_party/ReMe/reme/constants.py:35``）。
DEFAULT_MAX_TAGS_PER_FILE: int = 3

#: 单个标签长度上限（``third_party/ReMe/reme/constants.py:36``）。
DEFAULT_MAX_TAG_LENGTH: int = 64

#: 一等字段名。其余全部进 ``extra``，与 ReMe 的 ``FileFrontMatter`` 对齐。
_FIRST_CLASS: tuple[str, ...] = ("name", "description")

#: ReMe 的分隔符探测：``default_file_chunker.py:44-49``。
_DELIM_RE: re.Pattern[str] = re.compile(r"^---\s*$", re.MULTILINE)


class FrontMatterError(ValueError):
    """front matter 语法错误（YAML 解析失败 / 结构不是 mapping）。"""


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """把 Markdown 文本拆成 ``(front matter dict, 正文)``。

    分隔符规则与 ``third_party/ReMe/reme/components/file_chunker/default_file_chunker.py:44-49``
    逐字一致，保证 harness 与 ReMe 对同一份文件的理解不会漂移：

    - 不以 ``---`` 开头 → front matter 为空，正文是全文（**不**去空白）；
    - 找不到 ``"\\n---"`` → 视为没有 front matter，正文是全文；
    - 找到 → 取 ``"---"`` 与 ``"\\n---"`` 之间的内容做 ``yaml.safe_load``，
      正文取第二个分隔符之后并 ``lstrip("\\n")``。

    与 ReMe 的唯一区别：**YAML 解析失败会抛** :class:`FrontMatterError`。
    ReMe 在那里是静默吞掉的，harness 侧需要一个"能炸"的版本，
    否则写坏的记忆标签永远没人知道。

    Args:
        text (`str`): 原始 Markdown 文本。

    Returns:
        `tuple[dict[str, Any], str]`: (front matter 字典, 正文)。没有 front matter
        时返回 ``({}, text)``。

    Raises:
        `FrontMatterError`: front matter 存在但 YAML 非法，或解析结果不是 mapping。
    """
    if not text.startswith("---"):
        return {}, text
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}, text
    raw = text[3:end_idx].strip()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise FrontMatterError(f"front matter YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise FrontMatterError(f"front matter 必须是 mapping，收到 {type(data).__name__}")
    return data, text[end_idx + 4 :].lstrip("\n")


def normalize_tags(
    value: object,
    *,
    max_tags: int = DEFAULT_MAX_TAGS_PER_FILE,
    max_length: int = DEFAULT_MAX_TAG_LENGTH,
) -> list[str]:
    """按 ReMe 的写入侧规则规范化标签（``local_tag_index.py:33-54``）。

    规则逐条见模块 docstring。这里的实现与 ReMe **完全等价**，
    目的是让 harness 在**写文件之前**就知道哪些标签会被丢掉 —— 而不是写完再去看索引里少了什么。

    Args:
        value (`object`): 待规范化的标签（``list[str | int]``）。
        max_tags (`int`): 保留几个，默认 3（ReMe 的 ``max_tags_per_file``）。
        max_length (`int`): 单标签长度上限，默认 64。

    Returns:
        `list[str]`: 规范化后的标签，保持输入顺序。
    """
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            continue
        raw = "_".join(str(item).split())
        if not raw or len(raw) > max_length:
            continue
        if not any(char.isalnum() for char in raw):
            continue
        canonical = raw.casefold()
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(canonical)
        if len(result) >= max_tags:
            break
    return result


def normalize_query_tags(value: object, *, max_length: int = DEFAULT_MAX_TAG_LENGTH) -> list[str]:
    """按 ReMe 的**查询侧**规则规范化标签（``local_tag_index.py:60-62``）。

    与 :func:`normalize_tags` 的唯一差别是**不截断条数**。
    这不是疏漏，是 ReMe 的刻意设计：``local_tag_index.py:132-134`` 的注释写明
    "``max_tags_per_file`` constrains indexed documents, not lookup expressions"。
    如果查询侧也只取前 3 个，AND 查询会被悄悄削弱，OR 查询会漏结果。

    Args:
        value (`object`): 查询标签。
        max_length (`int`): 单标签长度上限。

    Returns:
        `list[str]`: 规范化后的标签（条数不限）。
    """
    return normalize_tags(value, max_tags=1 << 30, max_length=max_length)


class FrontMatter(BaseModel):
    """ReMe ``FileFrontMatter`` 的 harness 侧视图（契约 §3.16）。

    刻意把 ``extra`` 做成一个**显式字段**而不是 pydantic 的 ``__pydantic_extra__``：
    教程里需要能一眼看出"哪些是一等字段、哪些是扩展"，而
    ``model_extra`` 是个属性、不在 ``model_fields`` 里，讲起来要多绕一层。
    :meth:`to_file_front_matter` 负责还原成 ReMe 的真实类型。

    Example::

        fm = FrontMatter.parse(text)
        fm.extra["memory_tags"] = ["ops", "Runbook"]
        Path(p).write_text(fm.render(body), encoding="utf-8")
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    """文档名（ReMe ``FileFrontMatter.name``）。``None`` 表示不写这个键。"""

    description: str | None = None
    """文档描述。``None`` 表示不写这个键。"""

    extra: dict[str, Any] = Field(default_factory=dict)
    """其余全部 front matter 键（``memory_tags`` / ``date`` / ``session_id`` ...）。"""

    # ------------------------------------------------------------------
    # 解析 / 渲染
    # ------------------------------------------------------------------
    @classmethod
    def parse(cls, text: str, *, strict: bool = True) -> tuple["FrontMatter", str]:
        """把 Markdown 文本解析成 ``(FrontMatter, 正文)``（契约 §3.16）。

        Args:
            text (`str`): 原始 Markdown。
            strict (`bool`): ``True`` 时 YAML 非法直接抛（推荐）；
                ``False`` 时退化成 ``({}, text)``，用于处理历史脏数据。

        Returns:
            `tuple[FrontMatter, str]`: 视图对象与正文。

        Raises:
            `FrontMatterError`: ``strict=True`` 且 front matter 非法。
        """
        try:
            data, body = split_front_matter(text)
        except FrontMatterError:
            if strict:
                raise
            logger.warning("front matter 解析失败，按无 front matter 处理")
            return cls(), text

        extra = {key: value for key, value in data.items() if key not in _FIRST_CLASS}
        return (
            cls(
                name=_as_optional_str(data.get("name")),
                description=_as_optional_str(data.get("description")),
                extra=extra,
            ),
            body,
        )

    @classmethod
    def from_file(cls, path: str | Any, *, strict: bool = True) -> tuple["FrontMatter", str]:
        """从文件读取并解析。

        Args:
            path (`str | Any`): 文件路径。
            strict (`bool`): 同 :meth:`parse`。

        Returns:
            `tuple[FrontMatter, str]`: 视图对象与正文。
        """
        from pathlib import Path

        text = Path(path).read_text(encoding="utf-8")
        return cls.parse(text, strict=strict)

    def render(self, body: str, *, ensure_trailing_newline: bool = True) -> str:
        """渲染成完整的 Markdown 文本（契约 §3.16）。

        渲染规则对齐 ``third_party/ReMe/reme/steps/file_io/write.py:80-86``：

        - front matter 为空 → 直接返回正文（**不**加 ``---`` 块）；
        - 否则用 ``yaml.safe_dump(allow_unicode=True, sort_keys=False)`` 输出，
          保证中文不被转义、键序稳定（键序稳定对 KV Cache 与 diff 都重要）；
        - 末尾补一个换行（ReMe 的 write step 也这么做）。

        Args:
            body (`str`): 正文。
            ensure_trailing_newline (`bool`): 是否补尾部换行。

        Returns:
            `str`: 可落盘的完整文本。
        """
        merged: dict[str, Any] = {}
        if self.name is not None:
            merged["name"] = self.name
        if self.description is not None:
            merged["description"] = self.description
        merged.update(self.extra)

        if not merged:
            text = body
        else:
            dumped = yaml.safe_dump(
                merged,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
            text = f"---\n{dumped}---\n\n{body.lstrip(chr(10))}"
        if ensure_trailing_newline and not text.endswith("\n"):
            text += "\n"
        return text

    # ------------------------------------------------------------------
    # tag 便捷方法
    # ------------------------------------------------------------------
    def tags(self, *, key: str = DEFAULT_TAG_KEY) -> list[str]:
        """读取规范化后的标签（查询语义，不截断）。

        Args:
            key (`str`): 标签键名，默认 ``memory_tags``。

        Returns:
            `list[str]`: 规范化后的标签。
        """
        return normalize_query_tags(self.extra.get(key))

    def set_tags(self, value: object, *, key: str = DEFAULT_TAG_KEY) -> list[str]:
        """写入标签并返回**实际生效**的部分（写入语义，最多 3 个）。

        这个方法会在丢弃了任何标签时打 warning —— "我以为打了 5 个标签、
        实际只生效 3 个" 是教学里最容易踩的坑之一。

        Args:
            value (`object`): 待写入的标签。
            key (`str`): 标签键名。

        Returns:
            `list[str]`: 实际写入的标签。
        """
        requested = value if isinstance(value, list) else []
        effective = normalize_tags(value)
        dropped = len([item for item in requested if isinstance(item, (str, int))]) - len(effective)
        if dropped > 0:
            logger.warning(
                "标签写入被裁剪：请求 {} 个，实际生效 {} 个（ReMe 限制每文件最多 {} 个、"
                "单标签最长 {}、必须含字母数字）",
                len(requested),
                len(effective),
                DEFAULT_MAX_TAGS_PER_FILE,
                DEFAULT_MAX_TAG_LENGTH,
            )
        if effective:
            self.extra[key] = effective
        else:
            self.extra.pop(key, None)
        return effective

    # ------------------------------------------------------------------
    # 与 ReMe 类型互转
    # ------------------------------------------------------------------
    def to_file_front_matter(self) -> Any:
        """转成 ReMe 原生的 ``FileFrontMatter``。

        Returns:
            `Any`: ``reme.schema.FileFrontMatter`` 实例。

        Raises:
            `MemoryUnavailableError`: ``reme`` 不可导入。
        """
        from .client import _require_reme

        _require_reme()
        from reme.schema import FileFrontMatter

        payload: dict[str, Any] = dict(self.extra)
        if self.name is not None:
            payload["name"] = self.name
        if self.description is not None:
            payload["description"] = self.description
        return FileFrontMatter(**payload)

    @classmethod
    def from_file_front_matter(cls, value: Any) -> "FrontMatter":
        """从 ReMe 原生 ``FileFrontMatter`` 转回来。

        Args:
            value (`Any`): ``FileFrontMatter`` 实例（或任何有 ``name`` /
                ``description`` / ``model_extra`` 的对象）。

        Returns:
            `FrontMatter`: harness 侧视图。
        """
        extra = dict(getattr(value, "model_extra", None) or {})
        return cls(
            name=_as_optional_str(getattr(value, "name", None)),
            description=_as_optional_str(getattr(value, "description", None)),
            extra=extra,
        )

    def fingerprint(self) -> str:
        """front matter 的稳定指纹（幂等写入的判定依据）。

        ``sort_keys`` 很关键：dict 顺序不同但内容相同的两条 front matter
        应该得到同一个指纹，否则幂等判断会误判成"变了"。

        Returns:
            `str`: ``sha256`` 十六进制摘要。
        """
        import hashlib
        import json

        payload = {"name": self.name, "description": self.description, "extra": self.extra}
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _as_optional_str(value: Any) -> str | None:
    """把字段值规整成 ``str | None``（空串视为 ``None``）。

    Args:
        value (`Any`): 原始值。

    Returns:
        `str | None`: 规整结果。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None
