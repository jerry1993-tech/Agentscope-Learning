# -*- coding: utf-8 -*-
"""工具包契约：清单、基类、以及「多包如何拼成一个 Toolkit」。

**它补的是 AgentScope 没有的那一层**。AgentScope 提供的是**工具级**的抽象
（``ToolBase`` / ``FunctionTool`` / ``ToolGroup`` / ``Toolkit``），但没有
「**一包工具**」这个概念。缺了这一层，装配 30 个以上工具时会出三种事故：

1. **重名冲突静默发生**。``Toolkit.add_tool`` 只在**工具组**层面查重名
   （``third_party/agentscope/src/agentscope/tool/_tool_group.py``），
   跨组同名工具会被 LLM 的 ``tools`` 数组同时收到两个同名 schema ——
   provider 要么报错，要么按第一个执行。本模块用
   :func:`assert_unique_tool_names` 在装配期就拦下来。
2. **依赖顺序靠人记**。「repo 包需要 builtin 包提供的 backend」这种关系
   在 YAML 里是隐式的，写反了要等运行期才炸。本模块用
   :attr:`ToolPackManifest.requires` + :func:`resolve_pack_order` 拓扑排序。
3. **危险工具没有统一标记**。哪些工具需要权限引擎显式放行（写文件、跑命令）
   散落在各工具定义里。本模块用 :attr:`ToolPackManifest.dangerous_tools`
   把它变成**可声明、可审计**的一份清单。

**注意与 ``harness_kit.registry`` 的分工**（这不是重复实现）：

- ``registry._builtin_tool_pack`` 是 Layer 0 的**直连工厂**，签名
  ``async (spec, ctx) -> list[ToolBase]``，供 ``HarnessBuilder`` 直接调用；
- 本模块的 :class:`ToolPackBase` 是**给人和 YAML 看的**包协议（带清单、
  分组、依赖、描述），:meth:`ToolPackBase.build_tools` 的签名与直连工厂
  一致，因此两者可以互相包装 —— ``builtin_pack.build_builtin_pack`` 就是
  这样一个适配器。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.tool import ToolBase, ToolGroup, Toolkit

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from harness_kit.config.schema import ToolsSpec

__all__ = [
    "DuplicateToolNameError",
    "PackResolutionError",
    "ToolPackBase",
    "ToolPackManifest",
    "assert_unique_tool_names",
    "resolve_pack_order",
]

BASIC_GROUP: str = "basic"
"""AgentScope 的保留组名。

``Toolkit.__init__`` 在 ``tool_groups`` 里看到 ``"basic"`` 会直接
``ValueError``（``third_party/agentscope/src/agentscope/tool/_toolkit.py:114``），
因为它自己已经建了一个 ``basic`` 组来装 ``tools`` 参数。
所以 :meth:`ToolPackBase.build_toolkit` 里永远不把 ``"basic"`` 塞进
``tool_groups``，而是让不带组的工具留在 ``Toolkit(tools=...)`` 里。
"""


class DuplicateToolNameError(ValueError):
    """同一批工具里出现了重名工具。"""

    def __init__(self, names: Iterable[str]) -> None:
        """构造错误。

        Args:
            names (`Iterable[str]`): 重复的工具名。
        """
        self.names = sorted(set(names))
        super().__init__(
            f"工具名重复：{self.names}；同名的两个工具会让 LLM 的 tools "
            "数组出现两份同名 schema，provider 的行为未定义。"
            "请改名，或用 ToolsSpec.disabled 关掉其中一个。",
        )


class PackResolutionError(ValueError):
    """工具包依赖无法解析（缺包 / 成环 / 自依赖）。"""


class ToolPackManifest(BaseModel):
    """一个工具包的清单。契约 §5.4。

    Attributes:
        name (`str`): 包名，``ToolsSpec.packs`` 里写的就是它。
        version (`str`): 版本，默认 ``"0.1.0"``。
        description (`str`): 人读描述，会出现在 ``describe()`` 与日志里。
        requires (`list[str]`): 依赖的其它包名；装配时会被拓扑排序。
        groups (`dict[str, list[str]]`): ``组名 -> 工具名``，是**默认**分组；
            ``ToolsSpec.groups`` 可覆盖 / 追加。
        tags (`list[str]`): 自由标签（``fs`` / ``net`` / ``dangerous`` …），
            供上层做策略筛选。
        dangerous_tools (`list[str]`): 需要权限引擎显式放行的工具名。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """包名。"""
    version: str = "0.1.0"
    """版本。"""
    description: str = ""
    """人读描述。"""
    requires: list[str] = Field(default_factory=list)
    """依赖的其它包名。"""
    groups: dict[str, list[str]] = Field(default_factory=dict)
    """默认分组：组名 → 工具名列表。"""
    tags: list[str] = Field(default_factory=list)
    """自由标签。"""
    dangerous_tools: list[str] = Field(default_factory=list)
    """需权限引擎显式放行的工具名。"""

    def group_of(self, tool_name: str) -> str | None:
        """查一个工具属于哪个组。

        Args:
            tool_name (`str`): 工具名。

        Returns:
            `str | None`: 组名；不在任何组时 ``None``（表示归 ``basic``）。
        """
        for group, members in self.groups.items():
            if tool_name in members:
                return group
        return None

    def summary(self) -> str:
        """一行摘要。

        Returns:
            `str`: 形如 ``builtin@0.1.0 (6 tools, 3 groups)``。
        """
        total = sum(len(members) for members in self.groups.values())
        return (
            f"{self.name}@{self.version} "
            f"({total} tools, {len(self.groups)} groups)"
        )


class ToolPackBase(ABC):
    """一个「生产工具包」= 若干 :class:`ToolBase` + 清单 + 装配钩子。

    子类只需要做两件事：类属性 :attr:`manifest` 写清单，实现
    :meth:`build_tools`。:meth:`build_toolkit` 有可用默认实现。

    Example:
        >>> class MyPack(ToolPackBase):
        ...     manifest = ToolPackManifest(name="my", groups={"x": ["t"]})
        ...     async def build_tools(self, spec):
        ...         return [my_tool]
    """

    manifest: ToolPackManifest
    """本包的清单；子类必须覆盖为**实例**或类属性。"""

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------
    @abstractmethod
    async def build_tools(self, spec: "ToolsSpec") -> list[ToolBase]:
        """造出本包的全部工具。

        Args:
            spec (`ToolsSpec`): 工具声明（``packs`` / ``groups`` /
                ``disabled`` / ``max_result_chars``）。

        Returns:
            `list[ToolBase]`: 工具实例列表。
        """

    # ------------------------------------------------------------------
    # 可选覆写
    # ------------------------------------------------------------------
    def build_context(self) -> dict[str, Any]:
        """返回本包的「装配上下文」，供 :meth:`build_toolkit` 传给包内工具。

        默认返回空 dict。子类若需要把 ``workdir`` / ``backend`` 之类的东西
        暴露给上层（例如让 ``repo_pack`` 复用 ``builtin_pack`` 的 backend），
        覆写它。**这是本协议相对契约 §3.5 的扩展**，理由是：契约给的
        ``build_tools(spec)`` 签名里没有 ``ctx``，但真实装配需要 ``workdir``；
        用这个钩子传递，不需要改动任何已有签名。

        Returns:
            `dict[str, Any]`: 任意键值对。
        """
        return {}

    # ------------------------------------------------------------------
    # 默认实现
    # ------------------------------------------------------------------
    def merged_groups(self, spec: "ToolsSpec") -> dict[str, list[str]]:
        """合并清单默认分组与 ``ToolsSpec.groups``。

        ``ToolsSpec.groups`` 优先：同名组的成员被**整体替换**（而不是并集），
        这样 YAML 能真正「关掉」清单里的某个默认分组。

        Args:
            spec (`ToolsSpec`): 工具声明。

        Returns:
            `dict[str, list[str]]`: 组名 → 工具名。
        """
        merged: dict[str, list[str]] = {
            name: list(members) for name, members in self.manifest.groups.items()
        }
        for name, members in (spec.groups or {}).items():
            if name == BASIC_GROUP:
                raise ValueError(
                    f"工具组名 {BASIC_GROUP!r} 是 AgentScope 的保留组名，"
                    "不能在 ToolsSpec.groups 里使用；"
                    "（third_party/agentscope/src/agentscope/tool/_toolkit.py:114）",
                )
            merged[name] = list(members)
        return merged

    async def build_toolkit(self, spec: "ToolsSpec") -> Toolkit:
        """按契约：``build_tools`` → ``Toolkit(tools=..., tool_groups=...)``。

        组装规则：

        - 清单 / spec 里**没有**出现的工具 → 留在 ``Toolkit(tools=...)``
          的 ``basic`` 组；
        - 出现在某个组里的工具 → 放进对应的 ``ToolGroup``；
        - 组名 ``"basic"`` 一律拒绝（AgentScope 保留）；
        - 组内出现了本包没有的工具名 → 打 warning，**不报错**
          （那个名字可能来自另一个包，由 ``build_multi_pack_toolkit``
          统一处理）；
        - ``ToolsSpec.disabled`` 里的工具在本方法内就被摘掉。

        Args:
            spec (`ToolsSpec`): 工具声明。

        Returns:
            `Toolkit`: 装配好的工具集。

        Raises:
            DuplicateToolNameError: 本包产出了重名工具。
            ValueError: 出现了保留组名 ``"basic"``。
        """
        tools = await self.build_tools(spec)
        assert_unique_tool_names(tools)

        disabled = set(spec.disabled or [])
        kept: list[ToolBase] = []
        removed: list[str] = []
        for tool in tools:
            if tool.name in disabled:
                removed.append(tool.name)
                continue
            kept.append(tool)
        if removed:
            logger.info("{} 按 ToolsSpec.disabled 摘掉工具 {}", self.manifest.name, removed)

        groups = self.merged_groups(spec)
        available = {tool.name for tool in kept}
        grouped: dict[str, list[ToolBase]] = {}
        for group_name, members in groups.items():
            if group_name == BASIC_GROUP:
                raise ValueError(
                    "不能把 'basic' 放进 tool_groups："
                    "third_party/agentscope/src/agentscope/tool/_toolkit.py:114",
                )
            picked = [tool for tool in kept if tool.name in set(members)]
            missing = set(members) - available
            if missing:
                logger.warning(
                    "工具组 {} 里声明了本包没有的工具 {}（可能属于其它包）",
                    group_name,
                    sorted(missing),
                )
            if picked:
                grouped[group_name] = picked

        grouped_names = {
            tool.name for members in grouped.values() for tool in members
        }
        basic_tools = [tool for tool in kept if tool.name not in grouped_names]

        tool_groups = [
            ToolGroup(
                name=name,
                description=self._group_description(name, members),
                tools=members,
            )
            for name, members in grouped.items()
        ]
        logger.debug(
            "{} 装配完成：basic={} groups={}",
            self.manifest.name,
            [tool.name for tool in basic_tools],
            {name: [t.name for t in members] for name, members in grouped.items()},
        )
        return Toolkit(tools=basic_tools, tool_groups=tool_groups)

    def _group_description(
        self,
        name: str,
        members: Sequence[ToolBase],
    ) -> str:
        """生成工具组的描述。

        ``ToolGroup`` 要求非 ``basic`` 的组**必须有** description，
        否则 :class:`ValueError`（``third_party/agentscope/src/agentscope/
        tool/_tool_group.py:66``）。这份描述会出现在 meta tool ``ResetTools``
        给模型看的结果里，所以必须有信息量 —— 这里用「组名 + 成员工具名」
        拼一句，够模型判断「什么时候该激活这个组」。

        子类可以覆写它，给出更贴近业务的措辞。

        Args:
            name (`str`): 组名。
            members (`Sequence[ToolBase]`): 组内工具。

        Returns:
            `str`: 组描述。
        """
        return (
            f"{name} 工具组，来自工具包 {self.manifest.name}；"
            f"包含工具：{', '.join(tool.name for tool in members)}。"
            f"当任务需要这些能力时激活本组。"
        )

    def describe(self) -> str:
        """一行摘要，契约 §3.5。

        Returns:
            `str`: 形如 ``builtin@0.1.0 (6 tools, 3 groups) [fs, shell]``。
        """
        manifest = self.manifest
        tags = f" [{', '.join(manifest.tags)}]" if manifest.tags else ""
        requires = (
            f" requires={manifest.requires}" if manifest.requires else ""
        )
        return f"{manifest.summary()}{tags}{requires}"

    def tool_names(self) -> list[str]:
        """清单里声明的全部工具名（去重排序）。

        Returns:
            `list[str]`: 工具名列表。
        """
        return sorted(
            {
                name
                for members in self.manifest.groups.values()
                for name in members
            }
            | set(self.manifest.dangerous_tools),
        )


# ======================================================================
# 多包装配
# ======================================================================
def assert_unique_tool_names(tools: Sequence[ToolBase]) -> None:
    """断言一批工具里没有重名。

    Args:
        tools (`Sequence[ToolBase]`): 工具列表。

    Raises:
        DuplicateToolNameError: 出现重名。
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for tool in tools:
        if tool.name in seen:
            duplicates.append(tool.name)
        seen.add(tool.name)
    if duplicates:
        raise DuplicateToolNameError(duplicates)


def resolve_pack_order(packs: Sequence[ToolPackBase]) -> list[ToolPackBase]:
    """按 ``manifest.requires`` 做拓扑排序（被依赖者在前）。

    用 DFS + 三色标记，能同时检出**缺失依赖**与**成环**，并在错误信息里指出
    具体是哪条链 —— 依赖错误最难查的时候就是「只告诉你有环」。

    Args:
        packs (`Sequence[ToolPackBase]`): 工具包实例。

    Returns:
        `list[ToolPackBase]`: 排序后的包（依赖在前）。

    Raises:
        PackResolutionError: 依赖的包没给、或依赖成环。
    """
    by_name: dict[str, ToolPackBase] = {}
    for pack in packs:
        name = pack.manifest.name
        if name in by_name:
            raise PackResolutionError(f"工具包 {name!r} 被传入了两次")
        by_name[name] = pack

    ordered: list[ToolPackBase] = []
    state: dict[str, int] = {}  # 0=未访问 1=在栈上 2=已完成
    trail: list[str] = []

    def _visit(name: str) -> None:
        """深度优先访问一个包。

        Args:
            name (`str`): 包名。

        Raises:
            PackResolutionError: 缺失依赖或成环。
        """
        marker = state.get(name, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = " -> ".join([*trail[trail.index(name) :], name])
            raise PackResolutionError(
                f"工具包依赖成环：{cycle}；请打破 ToolPackManifest.requires",
            )

        pack = by_name.get(name)
        if pack is None:
            chain = " -> ".join([*trail, name])
            raise PackResolutionError(
                f"工具包依赖缺失：{chain}；请把 {name!r} 也加进 ToolsSpec.packs，"
                "或从 requires 里去掉它",
            )

        state[name] = 1
        trail.append(name)
        for dependency in pack.manifest.requires:
            _visit(dependency)
        trail.pop()
        state[name] = 2
        ordered.append(pack)

    for pack in packs:
        _visit(pack.manifest.name)
    return ordered


async def build_multi_pack_toolkit(
    packs: Sequence[ToolPackBase],
    spec: "ToolsSpec",
) -> Toolkit:
    """把多个包合成**一个** Toolkit（跨包重名检查也在这里做）。

    为什么不能各自 ``build_toolkit`` 再合并：``Toolkit`` 没有「合并两个
    Toolkit」的公开 API，而 ``Toolkit.add_tool`` 是 ``async`` 且加进去的工具
    一律落进 ``basic`` 组（``.../_toolkit.py:640``），会丢掉分组信息。
    所以这里**先把所有工具收齐、按组名归并，再一次性构造 Toolkit**。

    Args:
        packs (`Sequence[ToolPackBase]`): 工具包实例（顺序无所谓，内部会
            拓扑排序）。
        spec (`ToolsSpec`): 工具声明。

    Returns:
        `Toolkit`: 装配好的工具集。

    Raises:
        DuplicateToolNameError: 跨包出现同名工具。
        PackResolutionError: 依赖无法解析。
        ValueError: 出现保留组名 ``"basic"``。
    """
    ordered = resolve_pack_order(packs)

    all_tools: list[ToolBase] = []
    merged_groups: dict[str, list[str]] = {}
    for pack in ordered:
        tools = await pack.build_tools(spec)
        all_tools.extend(tools)
        for group_name, members in pack.merged_groups(spec).items():
            bucket = merged_groups.setdefault(group_name, [])
            bucket.extend(members)

    assert_unique_tool_names(all_tools)

    disabled = set(spec.disabled or [])
    kept = [tool for tool in all_tools if tool.name not in disabled]
    dropped = sorted({tool.name for tool in all_tools if tool.name in disabled})
    if dropped:
        logger.info("按 ToolsSpec.disabled 摘掉工具 {}", dropped)

    available = {tool.name for tool in kept}
    grouped: dict[str, list[ToolBase]] = {}
    for group_name, members in merged_groups.items():
        if group_name == BASIC_GROUP:
            raise ValueError(
                "不能把 'basic' 放进 tool_groups："
                "third_party/agentscope/src/agentscope/tool/_toolkit.py:114",
            )
        picked = [tool for tool in kept if tool.name in set(members)]
        missing = set(members) - available
        if missing:
            logger.warning(
                "工具组 {} 声明的工具不存在（可能被 disabled 摘掉了）：{}",
                group_name,
                sorted(missing),
            )
        if picked:
            grouped[group_name] = picked

    grouped_names = {
        tool.name for members in grouped.values() for tool in members
    }
    basic_tools = [tool for tool in kept if tool.name not in grouped_names]

    tool_groups = [
        ToolGroup(name=name, description=_default_group_description(name, members), tools=members)
        for name, members in grouped.items()
    ]

    logger.debug(
        "多包装配完成 packs={} basic={} groups={}",
        [pack.manifest.name for pack in ordered],
        [tool.name for tool in basic_tools],
        {name: [t.name for t in members] for name, members in grouped.items()},
    )
    return Toolkit(tools=basic_tools, tool_groups=tool_groups)


def _default_group_description(name: str, members: Sequence[ToolBase]) -> str:
    """为跨包合并出来的工具组生成描述。

    Args:
        name (`str`): 组名。
        members (`Sequence[ToolBase]`): 组内工具。

    Returns:
        `str`: 组描述（会展示给模型）。
    """
    return (
        f"{name} 工具组，包含工具：{', '.join(tool.name for tool in members)}。"
        "当任务需要这些能力时激活本组。"
    )


def accepts_build_context(factory: Any) -> bool:
    """判断一个工厂是否接受 ``ctx`` 参数（与 registry 的策略保持一致）。

    ``harness_kit.registry.accepts_build_context`` 是同样的判断
    （``.../registry.py:1063``）；这里再实现一份是为了让 ``tools`` 子包
    **不依赖** ``registry`` 模块（避免 ``harness_kit.tools`` 反向 import
    ``harness_kit.registry`` 造成循环）。

    Args:
        factory (`Any`): 可调用对象。

    Returns:
        `bool`: 签名里出现 ``ctx`` 时为 ``True``。
    """
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建可调用
        return False
    return "ctx" in parameters
