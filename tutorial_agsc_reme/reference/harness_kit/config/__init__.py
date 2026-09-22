# -*- coding: utf-8 -*-
"""harness_kit 的配置层：声明式 Profile / Bundle 的模型、装载与装配。

三个文件的职责边界（契约 §二）：

- :mod:`harness_kit.config.schema` —— pydantic 模型 + 合并算法（纯函数，可独立单测）；
- :mod:`harness_kit.config.loader` —— 磁盘 IO：YAML 读取、``${VAR}`` 插值、按名查找；
- :mod:`harness_kit.config.builder` —— 把 :class:`ResolvedProfile` 变成真实的
  AgentScope 运行对象（``ChatModelBase`` / ``Toolkit`` / ``WorkspaceBase`` /
  ``PermissionEngine`` / ``Agent``）。
"""

from harness_kit.config.builder import (
    BuildContext,
    BuiltHarness,
    HarnessBuilder,
    build_from_profile,
)
from harness_kit.config.loader import (
    ConfigCycleError,
    ConfigError,
    ConfigInterpolationError,
    ConfigNotFoundError,
    ConfigParseError,
    discover,
    interpolate_env,
    load_bundle,
    load_profile,
    load_resolved_profile,
    load_yaml,
)
from harness_kit.config.schema import (
    AgentSpec,
    AppendList,
    Bundle,
    MCPSpec,
    MemorySpec,
    MiddlewareSpec,
    ModelSpec,
    PermissionSpec,
    Profile,
    ResolvedProfile,
    SkillsSpec,
    ToolsSpec,
    WorkspaceSpec,
    merge_dicts,
    resolve_profile,
)

__all__ = [
    "AgentSpec",
    "AppendList",
    "BuildContext",
    "BuiltHarness",
    "Bundle",
    "ConfigCycleError",
    "ConfigError",
    "ConfigInterpolationError",
    "ConfigNotFoundError",
    "ConfigParseError",
    "HarnessBuilder",
    "MCPSpec",
    "MemorySpec",
    "MiddlewareSpec",
    "ModelSpec",
    "PermissionSpec",
    "Profile",
    "ResolvedProfile",
    "SkillsSpec",
    "ToolsSpec",
    "WorkspaceSpec",
    "build_from_profile",
    "discover",
    "interpolate_env",
    "load_bundle",
    "load_profile",
    "load_resolved_profile",
    "load_yaml",
    "merge_dicts",
    "resolve_profile",
]
