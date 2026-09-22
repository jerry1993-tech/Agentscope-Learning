# -*- coding: utf-8 -*-
"""技能加载器：在 AgentScope 的 ``LocalSkillLoader`` 之上补版本 / 开关 / 租户 / 依赖。

**继承关系（这是本模块最重要的设计决定）**::

    agentscope.skill.SkillLoaderBase          # 抽象基类：只要 list_skills()
      └── agentscope.skill.LocalSkillLoader   # 真实的目录扫描 + front matter 解析
            └── HarnessSkillLoader            # 本模块：manifest 索引 + 过滤 + 依赖

``HarnessSkillLoader`` **不重写任何文件扫描逻辑**：``SKILL.md`` 的发现规则、
mtime 缓存、并发读取全部沿用 ``LocalSkillLoader``
（``third_party/agentscope/src/agentscope/skill/_local_loader.py:100-172``，
即 ``list_skills`` 这**一个方法**的全文 —— 该文件到 172 行结束）。
我们只做两件 AgentScope 没做的事：

1. **多一层索引**：把 ``SKILL.md`` 解析成 :class:`~harness_kit.skills.manifest.SkillManifest`，
   于是 ``version`` / ``tags`` / ``tenants`` / ``requires`` / ``scripts`` 不再被丢弃；
2. **多一层过滤**：``enabled`` 开关与租户白名单在 :meth:`list_skills` 里真正生效 ——
   这一步是必须的，因为 ``Toolkit`` 只认 ``list_skills()``
   （``third_party/agentscope/src/agentscope/tool/_toolkit.py:394-429`` 的
   ``_get_available_skills`` 就是逐个 loader 调 ``list_skills()``），
   拿到什么就往系统提示词里塞什么。

已知坑（实测）：``LocalSkillLoader(directory, scan_subdir=False)`` 默认**不递归**
子目录，而 harness_kit 的内置技能是 ``builtin/code_review/SKILL.md`` 这种两级结构，
所以本类的 ``scan_subdir`` 默认值是 ``True``，与契约 §3.6 一致。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from loguru import logger

from agentscope.skill import LocalSkillLoader

from harness_kit.skills.manifest import (
    FRONTMATTER_FILENAME,
    SkillManifest,
    SkillManifestError,
    load_manifest,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.skill import Skill

    from harness_kit.config.schema import SkillsSpec
    from harness_kit.settings import Settings

__all__ = [
    "HarnessSkillLoader",
    "SkillDependencyError",
    "UnknownSkillError",
    "build_skill_instruction_template",
]

_INDEX_HEADER: str = (
    "<skill-index>\n"
    "下列技能是**说明书**而不是工具，不能直接调用。要用某个技能，"
    "必须先调用 skill_viewer 读取它的完整内容，再照做。\n"
)
"""技能索引的固定开头。措辞刻意与 AgentScope 的 ``DEFAULT_SKILL_INSTRUCTION``
（``third_party/agentscope/src/agentscope/tool/_toolkit.py:51-62``）对齐：
那里也强调 "Skills are NOT tools"。"""


class UnknownSkillError(KeyError):
    """按名取技能时未命中（契约 §3.6）。"""


class SkillDependencyError(ValueError):
    """技能依赖声明不成立：引用了不存在的技能，或依赖成环。"""


class HarnessSkillLoader(LocalSkillLoader):
    """带 manifest 索引的技能加载器。

    Args:
        directory (`Path`): 技能根目录，其下的每个含 ``SKILL.md`` 的子目录是一个技能。
        scan_subdir (`bool`, optional): 是否递归子目录，默认 ``True``。
        tenant (`str | None`, optional): 当前租户 id；``None`` 表示不区分租户，
            此时只有 ``tenants`` 为空或含 ``"*"`` 的技能可见。
        enabled (`list[str] | None`, optional): 显式启用名单（``SkillsSpec.enabled``
            的来源）。``None`` 或空列表表示"不加限制，由每个技能自己的 ``enabled``
            字段决定"；非空时**只有**名单里的技能可用，且名单里出现未知名字会报错。
        strict (`bool`, optional): ``True`` 时目录不存在 / ``SKILL.md`` 不合法
            直接抛异常；``False`` 时降级为 warning 并跳过。默认 ``True``。
        strict_enabled (`bool`, optional): ``True`` 时 ``enabled`` 名单里出现
            未知技能名会抛 :class:`UnknownSkillError`；默认 ``False``（只 warning）,
            因为名单常常是"先写后补"的增量配置。
        require_scripts (`bool`, optional): ``True`` 时校验 ``scripts`` 声明的
            文件确实存在，缺失即报错（生产环境建议打开，避免模型照着执行一个
            不存在的脚本）。

    Example:
        >>> loader = HarnessSkillLoader(  # doctest: +SKIP
        ...     Path("./harness_kit/skills/builtin"),
        ... )
        >>> [m.name for m in loader.load_manifests()]
        ['code_review', 'commit_convention']
        >>> loader.get("code_review").version
        '1.0.0'
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        scan_subdir: bool = True,
        tenant: str | None = None,
        enabled: list[str] | None = None,
        strict: bool = True,
        require_scripts: bool = False,
        strict_enabled: bool = False,
    ) -> None:
        """构造加载器，并按需做一次目录存在性检查。

        Raises:
            FileNotFoundError: ``strict=True`` 且目录不存在。
        """
        resolved = Path(directory).expanduser().resolve()
        if not resolved.is_dir() and strict:
            raise FileNotFoundError(
                f"技能目录 {resolved} 不存在"
                "（相对路径请先按 Settings.resolve 锚定到仓库根，"
                "或显式传 strict=False 以容忍空目录）",
            )

        super().__init__(directory=str(resolved), scan_subdir=scan_subdir)

        self.tenant: str | None = tenant
        self.enabled: list[str] = list(enabled or [])
        self.strict: bool = strict
        self.require_scripts: bool = require_scripts
        self.strict_enabled: bool = strict_enabled
        self.directory_path: Path = resolved

        self._manifests: list[SkillManifest] | None = None
        self._index_by_name: dict[str, SkillManifest] = {}

    # ------------------------------------------------------------------
    # 构造入口
    # ------------------------------------------------------------------
    @classmethod
    def from_spec(
        cls,
        spec: "SkillsSpec",
        *,
        settings: "Settings | None" = None,
        tenant: str | None = None,
    ) -> "HarnessSkillLoader":
        """按 ``SkillsSpec`` 构造加载器（``directories`` 只取第一个目录）。

        ``SkillsSpec`` 允许声明多个目录，而 ``AgentScope`` 的 ``Toolkit`` 接受
        ``list[SkillLoaderBase]`` —— 多目录时请对每个目录各构造一个 loader，
        见 :func:`loaders_from_spec`。

        Args:
            spec (`SkillsSpec`): 技能声明。
            settings (`Settings | None`): 用于锚定相对路径；``None`` 时按当前
                工作目录解析。
            tenant (`str | None`): 租户 id。

        Returns:
            `HarnessSkillLoader`: 单个目录的加载器。

        Raises:
            ValueError: ``spec.directories`` 为空。
        """
        loaders = loaders_from_spec(spec, settings=settings, tenant=tenant)
        if not loaders:
            raise ValueError(
                "SkillsSpec.directories 为空，无法构造 HarnessSkillLoader",
            )
        return loaders[0]

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def load_manifests(
        self,
        *,
        tenant: str | None = None,
        include_disabled: bool = False,
    ) -> list[SkillManifest]:
        """扫描磁盘并返回（过滤后的）技能元数据列表。契约 §3.6 的公开入口。

        Args:
            tenant (`str | None`): 覆盖构造时的租户；``None`` 表示沿用
                构造参数（若构造参数也是 ``None`` 即"不区分租户"）。
            include_disabled (`bool`): ``True`` 时连 ``enabled: false`` 或不在
                ``enabled`` 名单里的技能一并返回（诊断用）。

        Returns:
            `list[SkillManifest]`: 按技能名排序的 manifest 列表。

        Raises:
            SkillManifestError: 某个 ``SKILL.md`` 不合法（``strict=True``）。
            FileNotFoundError: ``require_scripts=True`` 且声明的脚本缺失。
            UnknownSkillError: ``enabled`` 名单里出现了磁盘上不存在的技能名。
        """
        manifests = self._index()
        effective_tenant = self.tenant if tenant is None else tenant

        selected: list[SkillManifest] = []
        dropped: list[tuple[SkillManifest, str]] = []
        for manifest in manifests:
            if not manifest.applies_to(effective_tenant):
                dropped.append(
                    (manifest, f"租户 {effective_tenant!r} 不在 {manifest.tenant_scope}"),
                )
                continue
            if not include_disabled:
                if not manifest.enabled:
                    dropped.append((manifest, "enabled=false"))
                    continue
                if self.enabled and manifest.name not in self.enabled:
                    dropped.append((manifest, "不在 SkillsSpec.enabled 名单里"))
                    continue
            selected.append(manifest)

        if not include_disabled:
            self._assert_enabled_names_exist(manifests)

        if dropped:
            logger.bind(
                skill_dir=str(self.directory_path),
                dropped={m.name: why for m, why in dropped},
            ).debug("技能过滤：{}", [(m.name, why) for m, why in dropped])

        return selected

    def manifests(
        self,
        *,
        tenant: str | None = None,
        include_disabled: bool = False,
    ) -> list[SkillManifest]:
        """:meth:`load_manifests` 的别名（读起来更顺的入口）。

        Args:
            tenant (`str | None`): 见 :meth:`load_manifests`。
            include_disabled (`bool`): 见 :meth:`load_manifests`。

        Returns:
            `list[SkillManifest]`: manifest 列表。
        """
        return self.load_manifests(
            tenant=tenant,
            include_disabled=include_disabled,
        )

    def get(self, name: str) -> SkillManifest:
        """按技能名取 manifest。契约 §3.6 的公开入口。

        Args:
            name (`str`): 技能名（不是目录名 —— 以 front matter 的 ``name`` 为准）。

        Returns:
            `SkillManifest`: 命中的 manifest。**不受租户/开关过滤影响**，
            这样诊断脚本能看到"存在但被禁用"的技能。

        Raises:
            UnknownSkillError: 技能不存在。
        """
        self._index()
        try:
            return self._index_by_name[name]
        except KeyError as exc:
            raise UnknownSkillError(
                f"技能 {name!r} 不存在；{self.directory_path} 下可用的技能: "
                f"{sorted(self._index_by_name)}",
            ) from exc

    def get_body(self, name: str) -> str:
        """取技能的完整正文（第二层渐进披露）。

        Args:
            name (`str`): 技能名。

        Returns:
            `str`: 带版本标题的正文。

        Raises:
            UnknownSkillError: 技能不存在。
        """
        return self.get(name).render_full()

    def select(
        self,
        *,
        tags: list[str] | None = None,
        query: str = "",
        tenant: str | None = None,
    ) -> list[SkillManifest]:
        """按标签 / 关键词筛选技能（在租户与开关过滤之后再做一层）。

        Args:
            tags (`list[str] | None`): 需要全部命中的标签。
            query (`str`): 对 name/description/tags 做子串匹配。
            tenant (`str | None`): 见 :meth:`load_manifests`。

        Returns:
            `list[SkillManifest]`: 命中列表。
        """
        return [
            manifest
            for manifest in self.load_manifests(tenant=tenant)
            if manifest.matches(tags=tags, query=query)
        ]

    def index_text(self, *, tenant: str | None = None) -> str:
        """生成"技能索引"文本，供 ``Toolkit`` 注入系统提示词。契约 §3.6 的公开入口。

        这是**第一层渐进披露**：每行只给 ``name`` + ``description`` + ``dir``，
        正文不进提示词，模型需要时再调 ``skill_viewer`` 去读。相比把全部正文塞进
        system prompt，token 开销从"技能总量"降到"技能条数"。

        Args:
            tenant (`str | None`): 见 :meth:`load_manifests`。

        Returns:
            `str`: 索引文本；没有任何可用技能时返回空字符串。
        """
        manifests = self.load_manifests(tenant=tenant)
        if not manifests:
            return ""

        lines: list[str] = [_INDEX_HEADER]
        for manifest in manifests:
            tag_text = f" tags={','.join(manifest.tags)}" if manifest.tags else ""
            lines.append(
                f"- {manifest.summary} (v{manifest.version}{tag_text}) "
                f"dir={manifest.dir}\n",
            )
        lines.append("</skill-index>")
        return "".join(lines)

    # ------------------------------------------------------------------
    # 依赖
    # ------------------------------------------------------------------
    def resolve_order(self, *, tenant: str | None = None) -> list[str]:
        """按 ``requires`` 做拓扑排序，返回技能加载顺序。

        依赖只在本 loader 的可见技能集合内解析。引用集合外的技能 → 报错，
        而不是"当成外部依赖忽略" —— 后者会让技能在线上突然少一半能力。

        Args:
            tenant (`str | None`): 见 :meth:`load_manifests`。

        Returns:
            `list[str]`: 先被依赖者在前。

        Raises:
            SkillDependencyError: 依赖缺失或成环。
        """
        manifests = self.load_manifests(tenant=tenant)
        known = {m.name for m in manifests}

        for manifest in manifests:
            for dep in manifest.requires:
                if dep not in known:
                    raise SkillDependencyError(
                        f"技能 {manifest.name!r} 依赖 {dep!r}，但该技能不在"
                        f"当前可见集合里（可见: {sorted(known)}）",
                    )

        by_name = {m.name: m for m in manifests}
        ordered: list[str] = []
        # 0 = 未访问，1 = 在栈上（用于检测环），2 = 已完成
        marks: dict[str, int] = {name: 0 for name in known}
        stack: list[str] = []

        def _visit(name: str) -> None:
            """深度优先拓扑访问。

            Args:
                name (`str`): 当前技能名。

            Raises:
                SkillDependencyError: 发现环。
            """
            mark = marks[name]
            if mark == 2:
                return
            if mark == 1:
                cycle = " -> ".join([*stack[stack.index(name) :], name])
                raise SkillDependencyError(f"技能依赖成环: {cycle}")

            marks[name] = 1
            stack.append(name)
            for dep in by_name[name].requires:
                _visit(dep)
            stack.pop()
            marks[name] = 2
            ordered.append(name)

        for name in sorted(known):
            _visit(name)
        return ordered

    def enabled_names(self, *, tenant: str | None = None) -> list[str]:
        """返回当前可见的技能名列表。

        Args:
            tenant (`str | None`): 见 :meth:`load_manifests`。

        Returns:
            `list[str]`: 技能名（按 :meth:`resolve_order` 的顺序）。
        """
        return self.resolve_order(tenant=tenant)

    # ------------------------------------------------------------------
    # AgentScope 原生接口
    # ------------------------------------------------------------------
    async def list_skills(self) -> list["Skill"]:
        """列出技能，按 ``enabled`` / 租户过滤后的结果。覆盖原生实现。

        ``Toolkit._get_available_skills`` 只认这个入口
        （``third_party/agentscope/src/agentscope/tool/_toolkit.py:394``），
        所以过滤必须发生在这里才能真的起作用。

        Returns:
            `list[Skill]`: AgentScope 的 ``Skill`` 对象（原生 dataclass）。

        Raises:
            SkillManifestError: 目录里有不合法且 ``strict=True`` 的 ``SKILL.md``。
        """
        native = await super().list_skills()
        visible = {m.name for m in self.load_manifests()}
        kept = [skill for skill in native if skill.name in visible]
        if len(kept) != len(native):
            logger.bind(
                skill_dir=str(self.directory_path),
                hidden=[_.name for _ in native if _.name not in visible],
            ).info(
                "技能过滤生效：{} -> {} 个",
                len(native),
                len(kept),
            )
        return kept

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _index(self, *, refresh: bool = False) -> list[SkillManifest]:
        """扫描磁盘并缓存 manifest 列表。

        Args:
            refresh (`bool`): 强制重新扫描（忽略缓存）。

        Returns:
            `list[SkillManifest]`: 全部 manifest（未经租户/开关过滤）。
        """
        if self._manifests is not None and not refresh:
            return self._manifests

        found: list[SkillManifest] = []
        for skill_dir in self._discover_skill_dirs():
            try:
                manifest = load_manifest(skill_dir)
            except (SkillManifestError, FileNotFoundError) as exc:
                if self.strict:
                    raise
                logger.warning("跳过不合法的技能目录 {}: {}", skill_dir, exc)
                continue

            missing = manifest.missing_scripts()
            if missing:
                message = (
                    f"技能 {manifest.name!r} 声明的配套脚本不存在: "
                    f"{[str(_) for _ in missing]}"
                )
                if self.require_scripts or self.strict:
                    raise FileNotFoundError(message)
                logger.warning(message)

            found.append(manifest)

        found.sort(key=lambda m: m.name)
        self._manifests = found
        self._index_by_name = {m.name: m for m in found}
        return found

    def _discover_skill_dirs(self) -> list[Path]:
        """找出目录下所有含 ``SKILL.md`` 的子目录。

        规则与 ``LocalSkillLoader._find_skill_dirs`` 完全一致
        （``third_party/agentscope/src/agentscope/skill/_local_loader.py:113-127``）：
        根目录自身的 ``SKILL.md`` 也算一个技能；``scan_subdir`` 为真时递归。

        Returns:
            `list[Path]`: 技能目录（已排序，保证结果稳定）。
        """
        root = self.directory_path
        if not root.is_dir():
            logger.warning("技能目录 {} 不存在", root)
            return []

        dirs: list[Path] = []
        if (root / FRONTMATTER_FILENAME).is_file():
            dirs.append(root)
        if self.scan_subdir:
            for current, child_dirs, filenames in os.walk(root):
                child_dirs.sort()
                if Path(current) == root:
                    continue
                if FRONTMATTER_FILENAME in filenames:
                    dirs.append(Path(current))
        return sorted(set(dirs))

    def _assert_enabled_names_exist(self, manifests: Iterable[SkillManifest]) -> None:
        """检查 ``enabled`` 名单里有没有拼错的技能名。

        ``strict_enabled=False``（默认）时只打 warning：``SkillsSpec.enabled``
        常常是"先写好名单、技能稍后补上"的增量式配置，直接抛会让 Profile
        无法启动。但它一定会被记进日志 —— 技能静默消失是最难查的一类故障。

        Args:
            manifests (`Iterable[SkillManifest]`): 磁盘上的全部 manifest。

        Raises:
            UnknownSkillError: ``strict_enabled=True`` 且名单里有不存在的技能名。
        """
        if not self.enabled:
            return
        known = {m.name for m in manifests}
        unknown = [name for name in self.enabled if name not in known]
        if not unknown:
            return

        message = (
            f"SkillsSpec.enabled 里的 {unknown} 在 {self.directory_path} "
            f"下不存在；该目录可用技能: {sorted(known)}"
        )
        if self.strict_enabled:
            raise UnknownSkillError(message)
        logger.bind(
            skill_dir=str(self.directory_path),
            enabled=self.enabled,
            unknown=unknown,
        ).warning(message)


def loaders_from_spec(
    spec: "SkillsSpec",
    *,
    settings: "Settings | None" = None,
    tenant: str | None = None,
) -> list[HarnessSkillLoader]:
    """把 ``SkillsSpec.directories`` 全部转成 :class:`HarnessSkillLoader`。

    ``AgentScope`` 的 ``Toolkit`` 接受 ``list[SkillLoaderBase]``
    （``third_party/agentscope/src/agentscope/tool/_toolkit.py:91``），
    所以多目录是原生支持的，这里只是把 ``SkillsSpec`` 的四个字段
    （``directories`` / ``scan_subdir`` / ``enabled`` / 租户）映射过去。

    Args:
        spec (`SkillsSpec`): 技能声明。
        settings (`Settings | None`): 用于锚定相对路径。
        tenant (`str | None`): 租户 id。

    Returns:
        `list[HarnessSkillLoader]`: 每个目录一个加载器；``directories`` 为空
        时返回空列表。
    """
    loaders: list[HarnessSkillLoader] = []
    for directory in spec.directories:
        resolved = (
            settings.resolve(directory) if settings is not None else Path(directory)
        )
        loaders.append(
            HarnessSkillLoader(
                resolved,
                scan_subdir=spec.scan_subdir,
                tenant=tenant,
                enabled=spec.enabled,
            ),
        )
    return loaders


def build_skill_instruction_template(
    disclosure: Literal["index", "full"] = "index",
) -> str:
    """生成 ``Toolkit(skill_instruction_template=...)`` 用的 Jinja2 模板。

    ``AgentScope`` 的渐进披露只有一层（只给 name/description/dir），模板常量在
    ``third_party/agentscope/src/agentscope/tool/_toolkit.py:51-62``（``DEFAULT_SKILL_INSTRUCTION``）。
    harness_kit 的 ``SkillsSpec.disclosure`` 支持 ``"full"`` —— 把正文直接铺进
    系统提示词，适合"技能只有两三个、想省一次工具调用"的场景。

    模板变量由 ``Toolkit.get_skill_instructions`` 注入
    （``third_party/agentscope/src/agentscope/tool/_toolkit.py:455-470``）：
    ``skills``（``Skill`` 对象序列）与 ``skill_viewer``（内置查看工具名）。

    Args:
        disclosure (`Literal["index", "full"]`): ``"index"`` 只给摘要，
            ``"full"`` 连正文一起给。

    Returns:
        `str`: Jinja2 模板字符串。
    """
    if disclosure == "full":
        return (
            "<agent-skills>\n"
            "以下技能是操作说明书，不是工具；直接照做即可，"
            "也可以用 {{ skill_viewer }} 重新读取。\n"
            "{% for skill in skills %}"
            "<skill>\n<name>{{ skill.name }}</name>\n"
            "<dir>{{ skill.dir }}</dir>\n"
            "<content>\n{{ skill.markdown }}\n</content>\n"
            "</skill>{% endfor %}\n"
            "</agent-skills>"
        )
    return (
        "<agent-skills>\n"
        "Skills 是说明书而不是工具，不能直接调用。要用某个技能，"
        "必须先调用 `{{ skill_viewer }}` 读取它的完整内容，再照做。\n"
        "{% for skill in skills %}"
        "<skill>\n<name>{{ skill.name }}</name>\n"
        "<description>{{ skill.description }}</description>\n"
        "<dir>{{ skill.dir }}</dir>\n"
        "</skill>{% endfor %}\n"
        "</agent-skills>"
    )
