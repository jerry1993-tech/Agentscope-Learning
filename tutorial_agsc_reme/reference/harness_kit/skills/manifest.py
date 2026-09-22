# -*- coding: utf-8 -*-
"""技能元数据：SKILL.md 的 front matter 解析与校验。

AgentScope 自带的 ``LocalSkillLoader``（``third_party/agentscope/src/agentscope/skill/_local_loader.py:16``）
已经能读 ``SKILL.md`` 并把 front matter 解析出来，但它只保留 4 个字段：

.. code-block:: python

    # third_party/agentscope/src/agentscope/skill/_local_loader.py:75-85
    skill = Skill(
        name=str(name),
        description=str(description),
        dir=skill_root,
        markdown=content.content,
        updated_at=updated_at,
    )

``version`` / ``tags`` / 租户 / 依赖声明 / 配套脚本清单**全部被丢弃**，
而且缺 ``name`` 或 ``description`` 时只是打一条 warning 后**静默跳过**
（同文件 ``:70``）。生产里这三点都不能接受：

1. 版本丢失 ⇒ 无法做技能灰度与回滚；
2. 静默跳过 ⇒ 技能写错了没人知道，线上表现为"模型突然不会用了"；
3. 无法声明依赖 ⇒ 技能之间的先后顺序只能靠人工记忆。

本模块因此**不重写**加载逻辑，只在 AgentScope 的 front matter 之上补一层
**强校验 + 扩展字段**的 ``SkillManifest``。真正的目录扫描仍由
:mod:`harness_kit.skills.loader` 里继承的 ``LocalSkillLoader`` 负责。

front matter 的解析直接复用 ReMe / AgentScope 都依赖的 ``python-frontmatter``
（``frontmatter.load`` / ``frontmatter.loads``），不自己写 YAML 头解析。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import frontmatter
from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

__all__ = [
    "FRONTMATTER_FILENAME",
    "SkillManifest",
    "SkillManifestError",
    "load_manifest",
    "parse_manifest_text",
    "parse_tags",
]

FRONTMATTER_FILENAME: str = "SKILL.md"
"""技能清单文件名。与 AgentScope 的 ``LocalSkillLoader`` 保持一致
（``third_party/agentscope/src/agentscope/skill/_local_loader.py:39``）。"""

_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_]*(?:-[a-z0-9]+)*$")
_VERSION_RE: re.Pattern[str] = re.compile(
    r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$",
)

_RESERVED_TENANT: str = "*"
"""front matter 里写 ``tenants: ["*"]`` 表示对所有租户开放。"""


class SkillManifestError(ValueError):
    """SKILL.md 的 front matter 不合法。

    与 AgentScope 的"打一条 warning 然后静默跳过"相反：harness_kit 的所有
    配置错误一律**向上抛**，让 ``scripts/00_smoke.py`` 这类体检脚本能在
    启动阶段就发现，而不是等到 Agent 少了一个技能才发现。
    """


def parse_tags(value: Any) -> list[str]:
    """把 front matter 里五花八门的 tag 写法归一成 ``list[str]``。

    支持 ``["a", "b"]``、``"a, b"``、``"a b"``、``None`` 四种写法 —— YAML 里
    写标量是常见手误，这里容忍它而不是报错。

    Args:
        value (`Any`): front matter 解析出来的原始值。

    Returns:
        `list[str]`: 去重且保持出现顺序的 tag 列表。

    Raises:
        SkillManifestError: 值既不是字符串也不是字符串列表。
    """
    if value is None:
        return []

    items: list[str]
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            if not isinstance(item, str):
                raise SkillManifestError(
                    f"tags 里的元素必须是字符串，收到 {type(item).__name__}: {item!r}",
                )
            items.append(item)
    else:
        raise SkillManifestError(
            f"tags 必须是字符串或字符串列表，收到 {type(value).__name__}: {value!r}",
        )

    seen: dict[str, None] = {}
    for item in items:
        text = item.strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


class SkillManifest(BaseModel):
    """``SKILL.md`` 的 front matter 模型。

    契约 §3.6 规定的 6 个字段（``name`` / ``description`` / ``version`` /
    ``tags`` / ``path`` / ``body``）在前，harness_kit 为"版本、启用开关、
    按租户加载、依赖声明"追加的 5 个字段在后 —— 全部带默认值，因此既有的
    最简 ``SKILL.md``（只有 ``name`` / ``description``）仍然合法。

    Example:
        >>> manifest = parse_manifest_text(   # doctest: +SKIP
        ...     "---\\nname: code_review\\ndescription: 代码审查\\n---\\n正文",
        ...     path=Path("/tmp/code_review/SKILL.md"),
        ... )
        >>> manifest.summary
        'code_review: 代码审查'
    """

    model_config = ConfigDict(frozen=True)

    # ---- 契约 §3.6 规定的字段 ----

    name: str
    """技能名，同时是 ``Toolkit`` 里 SkillViewer 的键。必须匹配
    ``^[a-z0-9][a-z0-9_]*(-[a-z0-9]+)*$`` —— 用连字符而不是空格，
    因为它会出现在模型读到的 XML 标签里。"""

    description: str
    """一句话说明"什么时候该用这个技能"。第一层渐进披露只给这一行，
    所以要写成触发条件而不是功能罗列。"""

    version: str = "0.1.0"
    """语义化版本，用于灰度/回滚。"""

    tags: list[str] = Field(default_factory=list)
    """检索标签，供 :meth:`harness_kit.skills.loader.HarnessSkillLoader.select`
    按标签挑技能。"""

    path: Path
    """``SKILL.md`` 的绝对路径。"""

    body: str
    """``SKILL.md`` 的正文（front matter 之后的部分）。第二层渐进披露时才注入。"""

    # ---- harness_kit 追加的字段 ----

    enabled: bool = True
    """启用开关。``False`` 时技能仍然可被 :meth:`get` 读到，但不会出现在
    :meth:`HarnessSkillLoader.list_skills` 与技能索引里。"""

    tenants: list[str] = Field(default_factory=list)
    """租户白名单。空列表等价于 ``["*"]``（所有租户可见）。"""

    requires: list[str] = Field(default_factory=list)
    """依赖的其它技能名。加载顺序按它做拓扑排序；缺失或成环直接报错。"""

    scripts: list[str] = Field(default_factory=list)
    """配套脚本路径，**相对本技能目录**。技能正文里让模型去执行它们，
    路径由 :meth:`script_paths` 解析成绝对路径。"""

    license: str | None = None
    """可选许可标识，例如 ``"MIT"``、``"internal"``。"""

    source: str = "<memory>"
    """诊断用：这份 manifest 从哪来（文件路径或 ``"<memory>"``）。"""

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """校验技能名格式。

        Args:
            value (`str`): front matter 里的 ``name``。

        Returns:
            `str`: 原值。

        Raises:
            SkillManifestError: 名字为空或不匹配命名规则。
        """
        if not value or not value.strip():
            raise SkillManifestError("SKILL.md 的 name 不能为空")
        if not _NAME_RE.match(value):
            raise SkillManifestError(
                f"技能名 {value!r} 不合法：必须匹配 {_NAME_RE.pattern}"
                "（小写字母/数字/下划线，可用连字符分段）",
            )
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        """校验描述非空。

        Args:
            value (`str`): front matter 里的 ``description``。

        Returns:
            `str`: 去掉首尾空白的描述。

        Raises:
            SkillManifestError: 描述为空。
        """
        text = value.strip()
        if not text:
            raise SkillManifestError(
                "SKILL.md 的 description 不能为空 —— 它是第一层渐进披露的"
                "全部内容，缺了它模型无从判断何时该用这个技能",
            )
        return text

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        """校验版本号形如语义化版本。

        Args:
            value (`str`): front matter 里的 ``version``。

        Returns:
            `str`: 原值。

        Raises:
            SkillManifestError: 不匹配 ``MAJOR.MINOR.PATCH``。
        """
        if not _VERSION_RE.match(value):
            raise SkillManifestError(
                f"技能版本 {value!r} 不合法：要求形如 1.2.3（可带 -rc1 / +build）",
            )
        return value

    @field_validator("tags", "tenants", "scripts", "requires", mode="before")
    @classmethod
    def _coerce_str_list(cls, value: Any) -> list[str]:
        """把标量/列表统一成 ``list[str]``。

        Args:
            value (`Any`): front matter 原始值。

        Returns:
            `list[str]`: 归一后的列表。
        """
        return parse_tags(value)

    @field_validator("requires")
    @classmethod
    def _check_requires(cls, value: list[str]) -> list[str]:
        """校验依赖名格式，并去掉自依赖。

        Args:
            value (`list[str]`): 依赖的技能名列表。

        Returns:
            `list[str]`: 去重后的依赖名。

        Raises:
            SkillManifestError: 依赖名不匹配命名规则。
        """
        result: dict[str, None] = {}
        for name in value:
            if not _NAME_RE.match(name):
                raise SkillManifestError(
                    f"requires 里的 {name!r} 不是合法的技能名"
                    f"（要求匹配 {_NAME_RE.pattern}）",
                )
            result.setdefault(name, None)
        return list(result)

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def summary(self) -> str:
        """只给 ``name`` + ``description``，用于第一层渐进披露。

        Returns:
            `str`: 形如 ``"code_review: 对 diff 做结构化代码审查"``。
        """
        return f"{self.name}: {self.description}"

    @property
    def dir(self) -> Path:
        """本技能所在目录（``SKILL.md`` 的父目录）。

        Returns:
            `Path`: 技能目录。
        """
        return self.path.parent

    @property
    def script_paths(self) -> list[Path]:
        """把 :attr:`scripts` 解析成绝对路径。

        Returns:
            `list[Path]`: 每个配套脚本的绝对路径。
        """
        return [(self.dir / script).resolve() for script in self.scripts]

    @property
    def tenant_scope(self) -> list[str]:
        """返回实际生效的租户白名单。

        Returns:
            `list[str]`: 空 ``tenants`` 归一为 ``["*"]``。
        """
        return self.tenants or [_RESERVED_TENANT]

    def applies_to(self, tenant: str | None) -> bool:
        """判断本技能对某个租户是否可见。

        Args:
            tenant (`str | None`): 租户 id；``None`` 表示"不区分租户"的调用者，
                此时只有 ``*`` 技能可见。

        Returns:
            `bool`: 是否可见。
        """
        scope = self.tenant_scope
        if _RESERVED_TENANT in scope:
            return True
        if tenant is None:
            return False
        return tenant in scope

    def matches(self, *, tags: list[str] | None = None, query: str = "") -> bool:
        """按标签 / 关键词判断技能是否命中。

        Args:
            tags (`list[str] | None`): 需要全部命中的标签。
            query (`str`): 对 ``name`` / ``description`` / ``tags`` 做大小写
                不敏感的子串匹配；空串表示不筛。

        Returns:
            `bool`: 是否命中。
        """
        if tags:
            owned = set(self.tags)
            if not set(tags).issubset(owned):
                return False
        if query:
            needle = query.lower()
            haystack = " ".join(
                [self.name, self.description, *self.tags],
            ).lower()
            if needle not in haystack:
                return False
        return True

    def missing_scripts(self) -> list[Path]:
        """返回声明了但磁盘上不存在的配套脚本。

        Returns:
            `list[Path]`: 缺失的脚本绝对路径；为空表示全部存在。
        """
        return [path for path in self.script_paths if not path.is_file()]

    def render_full(self) -> str:
        """渲染第二层渐进披露用的完整正文（含 front matter 摘要头）。

        Returns:
            `str`: 带 ``# skill: <name>`` 标题的正文。
        """
        return f"# skill: {self.name} (v{self.version})\n\n{self.body.strip()}\n"


def parse_manifest_text(
    text: str,
    *,
    path: Path,
    source: str | None = None,
) -> SkillManifest:
    """解析一段 ``SKILL.md`` 文本（含 front matter）。

    不读文件，纯粹做"文本 → manifest"，因此可以脱离磁盘单测。

    Args:
        text (`str`): ``SKILL.md`` 全文。
        path (`Path`): 该文本对应的文件路径（写进 ``SkillManifest.path``）。
        source (`str | None`): 诊断用的来源标注，默认取 ``str(path)``。

    Returns:
        `SkillManifest`: 校验通过的技能元数据。

    Raises:
        SkillManifestError: 缺 front matter、缺必填字段或字段格式不合法。
    """
    try:
        post = frontmatter.loads(text)
    except Exception as exc:  # frontmatter 解析失败时给一条可读的错误
        raise SkillManifestError(
            f"{source or path} 的 front matter 不是合法 YAML：{exc}",
        ) from exc

    meta: dict[str, Any] = dict(post.metadata or {})
    if not meta:
        raise SkillManifestError(
            f"{source or path} 缺少 front matter —— SKILL.md 必须以 "
            "``---`` 包裹的 YAML 头开始，且至少含 name / description",
        )

    unknown_required = [key for key in ("name", "description") if key not in meta]
    if unknown_required:
        raise SkillManifestError(
            f"{source or path} 的 front matter 缺少必填字段 {unknown_required}；"
            "AgentScope 的 LocalSkillLoader 在这种情况下会静默跳过整个技能",
        )

    payload: dict[str, Any] = {
        "name": str(meta["name"]).strip(),
        "description": str(meta["description"]),
        "path": Path(path),
        "body": post.content,
        "source": source or str(path),
    }
    for key in (
        "version",
        "tags",
        "enabled",
        "tenants",
        "requires",
        "scripts",
        "license",
    ):
        if key in meta and meta[key] is not None:
            payload[key] = meta[key]

    try:
        return SkillManifest(**payload)
    except ValidationError as exc:
        # pydantic 会把 field_validator 里抛的 ValueError 包成 ValidationError，
        # 这里把它拆回来，让报错信息是"技能名不合法"而不是一串 schema 噪音。
        for error in exc.errors():
            underlying = (error.get("ctx") or {}).get("error")
            if isinstance(underlying, SkillManifestError):
                raise underlying from exc
        raise SkillManifestError(
            f"{source or path} 的技能元数据校验失败：{exc.errors()}",
        ) from exc
    except SkillManifestError:
        raise


def load_manifest(skill_dir: Path | str) -> SkillManifest:
    """从技能目录读 ``SKILL.md`` 并解析成 :class:`SkillManifest`。

    Args:
        skill_dir (`Path | str`): 含 ``SKILL.md`` 的目录。

    Returns:
        `SkillManifest`: 校验通过的技能元数据。

    Raises:
        FileNotFoundError: 目录或 ``SKILL.md`` 不存在。
        SkillManifestError: 内容不合法。
    """
    directory = Path(skill_dir).expanduser().resolve()
    md_path = directory / FRONTMATTER_FILENAME
    if not md_path.is_file():
        raise FileNotFoundError(
            f"{directory} 下找不到 {FRONTMATTER_FILENAME}"
            "（相对路径请先按 Settings.resolve 锚定到仓库根）",
        )

    text = md_path.read_text(encoding="utf-8")
    manifest = parse_manifest_text(text, path=md_path)
    logger.debug(
        "已解析技能 {}(v{}) <- {}",
        manifest.name,
        manifest.version,
        md_path,
    )
    return manifest
