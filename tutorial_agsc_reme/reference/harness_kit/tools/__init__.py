# -*- coding: utf-8 -*-
"""harness_kit 的工具层：工具包契约、生产工具包、参数修复。

模块归属（契约 §二）：

- :mod:`harness_kit.tools.pack` —— ``ToolPackManifest`` / ``ToolPackBase``，
  以及多包拓扑装配；
- :mod:`harness_kit.tools.builtin_pack` —— ``BuiltinToolPack``：AgentScope
  原生文件/命令工具 + 只读的 ``Now`` / ``Calc``；
- :mod:`harness_kit.tools.repo_pack` —— ``RepoToolPack``：仓库概览、代码搜索、
  精确替换、跑测试；
- :mod:`harness_kit.tools.utils` —— JSON schema 收紧、工具参数修复、结果截断。

:func:`build_toolkit` 是本层的**唯一入口**：给一批包和一份 ``ToolsSpec``，
拿一个装好的 ``Toolkit``。
"""

from typing import TYPE_CHECKING, Sequence

from harness_kit.tools.builtin_pack import BuiltinToolPack, build_builtin_pack
from harness_kit.tools.pack import (
    BASIC_GROUP,
    DuplicateToolNameError,
    PackResolutionError,
    ToolPackBase,
    ToolPackManifest,
    assert_unique_tool_names,
    build_multi_pack_toolkit,
    resolve_pack_order,
)
from harness_kit.tools.repo_pack import RepoToolPack, build_repo_pack
from harness_kit.tools.utils import (
    ArgumentRepairError,
    ensure_strict_schema,
    repair_arguments,
    summarize_tool_result,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from agentscope.tool import Toolkit

    from harness_kit.config.schema import ToolsSpec

__all__ = [
    "BASIC_GROUP",
    "ArgumentRepairError",
    "BuiltinToolPack",
    "DuplicateToolNameError",
    "PackResolutionError",
    "RepoToolPack",
    "ToolPackBase",
    "ToolPackManifest",
    "assert_unique_tool_names",
    "build_builtin_pack",
    "build_multi_pack_toolkit",
    "build_repo_pack",
    "build_toolkit",
    "ensure_strict_schema",
    "repair_arguments",
    "resolve_pack_order",
    "summarize_tool_result",
]


async def build_toolkit(
    spec: "ToolsSpec",
    packs: Sequence[ToolPackBase],
) -> "Toolkit":
    """把多个工具包装配成一个 ``Toolkit``。契约 §二 里 ``tools/__init__.py`` 的入口。

    内部直接委托 :func:`~harness_kit.tools.pack.build_multi_pack_toolkit`，
    它负责：拓扑排序 → 收齐工具 → 跨包重名检查 → 按组合并 → 构造 ``Toolkit``。

    **``packs`` 只接受 ``ToolPackBase`` 实例，不接受字符串**，这是刻意的：
    字符串需要先经 ``HarnessRegistry.get("tool_pack", name)`` 求值，而那条路
    拿到的是 ``list[ToolBase]``（见 ``.../registry.py:779`` 的
    ``_builtin_tool_pack``），**拿不到清单里的分组与依赖信息**。想要「按名字
    装配」，请走 :meth:`harness_kit.config.builder.HarnessBuilder` —— 它才是
    负责名字解析的那一层。这里只负责「给我包，我给你 Toolkit」。

    Args:
        spec (`ToolsSpec`): 工具声明（分组、禁用、结果长度上限）。
        packs (`Sequence[ToolPackBase]`): 工具包实例，顺序无所谓。

    Returns:
        `Toolkit`: 装配好的工具集。

    Raises:
        TypeError: ``packs`` 里混进了字符串或非 ``ToolPackBase`` 对象。
        DuplicateToolNameError: 跨包工具重名。
        PackResolutionError: 依赖缺失或成环。
        ValueError: ``ToolsSpec`` 里的组名用了保留名 ``"basic"``。
    """
    for pack in packs:
        if not isinstance(pack, ToolPackBase):
            raise TypeError(
                f"build_toolkit 只接受 ToolPackBase 实例，收到 "
                f"{type(pack).__name__}（{pack!r}）。"
                "按名字装配请走 HarnessBuilder。",
            )
    return await build_multi_pack_toolkit(packs, spec)
