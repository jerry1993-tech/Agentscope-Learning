# -*- coding: utf-8 -*-
"""Profile / Bundle 的 pydantic v2 模型与**合并算法**。

AgentScope 自身的装配是硬编码的（``third_party/agentscope/src/agentscope/app/_app.py:78``
的 ``create_app`` 把 model / toolkit / middleware 直接写死在函数体里），没有声明式入口。
本模块补上这个缺口：用 ``Bundle``（能力包）+ ``Profile``（场景）两层 YAML 描述装配结果，
再由 :func:`resolve_profile` 合并冻结成 :class:`ResolvedProfile`。

合并规则的唯一真值（契约 §6.2）：

============ ==========================================================
情形         规则
============ ==========================================================
map vs map   深合并，递归下去
叶子冲突      override 胜
list vs list  默认**整体替换**（不做元素级合并）
list 追加     在 YAML 里写 ``!append`` 标签，如 ``packs: !append [repo]``
override=null **删除**继承来的键（用于关掉某个继承能力）
只 base 有    保留
只 override有 新增
extends 成环  :class:`~harness_kit.config.loader.ConfigCycleError`
bundle 不存在 :class:`~harness_kit.config.loader.ConfigNotFoundError`
============ ==========================================================

``explain()`` 能逐字段回答"这个值来自哪个文件"，靠的是 :func:`_merge` 在合并过程中
顺手记录的 :attr:`ResolvedProfile.source_map`（``点号路径 -> 来源标签``）。
"""

from __future__ import annotations

import copy
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, Mapping

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

__all__ = [
    "AgentSpec",
    "AppendList",
    "Bundle",
    "MCP_SERVER_SPEC_CLASS",
    "MCPServerSpec",
    "MCPSpec",
    "MemorySpec",
    "MiddlewareSpec",
    "ModelSpec",
    "PermissionSpec",
    "Profile",
    "ResolvedProfile",
    "SANDBOX_POLICY_CLASS",
    "SandboxPolicy",
    "SkillsSpec",
    "ToolsSpec",
    "WorkspaceSpec",
    "merge_dicts",
    "resolve_profile",
]


# ======================================================================
# 外部归属类型的解析
# ======================================================================
# 契约 §二 把 SandboxPolicy 归给第 10 讲（harness_kit/sandbox/policy.py）、
# 把 MCPServerSpec 归给第 7 讲（harness_kit/mcp/registry.py）。但本模块的
# WorkspaceSpec.policy / MCPSpec.servers 又必须引用它们。于是：
#   - 若那两个模块已存在（第 7 / 第 10 讲交付后），直接复用**它们的类**，全局只有一个真值；
#   - 若尚未交付，退回本文件里的**同构占位类**，保证第 1 / 第 2 讲的 Profile YAML 仍能校验通过。
# 占位类只提供字段（YAML schema），不提供行为；行为一律由归属讲次提供。
def _resolve_external_type(
    module_path: str,
    attr: str,
    fallback: type[BaseModel],
) -> type[BaseModel]:
    """优先使用归属模块里的真实类，缺失时退回占位类。

    Args:
        module_path (`str`): 归属模块的点号路径。
        attr (`str`): 类名。
        fallback (`type[BaseModel]`): 占位类。

    Returns:
        `type[BaseModel]`: 实际生效的类。
    """
    try:
        module = import_module(module_path)
    except ImportError:
        return fallback
    resolved = getattr(module, attr, None)
    if isinstance(resolved, type) and issubclass(resolved, BaseModel):
        return resolved
    return fallback


class _FallbackSandboxPolicy(BaseModel):
    """``harness_kit.sandbox.policy.SandboxPolicy`` 的同构占位类（第 10 讲交付后自动让位）。

    字段与契约 §3.10 逐字一致。``workspace_root`` 在契约里是必填，但
    ``profiles/research.yaml`` 的 ``policy:`` 块并没有写它 —— 这个矛盾由
    :meth:`WorkspaceSpec._fill_policy_root` 在解析期补上（用 ``WorkspaceSpec.root`` 填）。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_root: Path = Path("./.harness/workspace")
    read_paths: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    deny_paths: list[str] = Field(
        default_factory=lambda: [".git/", ".env", "**/*.pem"],
    )
    network: Literal["none", "allowlist", "full"] = "none"
    network_allowlist: list[str] = Field(default_factory=list)
    cpu: float = 1.0
    memory_mb: int = 1024
    pids: int = 128
    timeout_s: int = 60
    max_output_bytes: int = 1_000_000


class _FallbackMCPServerSpec(BaseModel):
    """``harness_kit.mcp.registry.MCPServerSpec`` 的同构占位类（第 7 讲交付后自动让位）。

    字段与契约 §3.7 逐字一致，``extra="forbid"``。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    namespace: str | None = None


SANDBOX_POLICY_CLASS: type[BaseModel] = _resolve_external_type(
    "harness_kit.sandbox.policy",
    "SandboxPolicy",
    _FallbackSandboxPolicy,
)
"""当前生效的 ``SandboxPolicy`` 类（真实类或占位类）。"""

MCP_SERVER_SPEC_CLASS: type[BaseModel] = _resolve_external_type(
    "harness_kit.mcp.registry",
    "MCPServerSpec",
    _FallbackMCPServerSpec,
)
"""当前生效的 ``MCPServerSpec`` 类（真实类或占位类）。"""

# 供 pydantic 在解析注解时取用（注解用的是下面这两个名字）
SandboxPolicy = SANDBOX_POLICY_CLASS
MCPServerSpec = MCP_SERVER_SPEC_CLASS


# ======================================================================
# 各 Spec
# ======================================================================
class ModelSpec(BaseModel):
    """模型层的声明。"""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepseek", "openai", "echo"] = "deepseek"
    """模型提供方，决定走哪个 :class:`~harness_kit.registry.HarnessRegistry` 工厂。

    **有默认值不是随手写的**：``Profile.model`` 在合并语义里是"这一个 Profile 自己
    写的那部分覆盖"，契约 §6.3 的 ``research.yaml`` 就只写了 ``model.temperature``。
    如果这里把 ``provider`` / ``model_name`` 定成必填，那一层 Profile 在校验阶段
    （早于任何合并）就会直接 ``ValidationError``，契约 §6.3 的示例根本装载不了
    （已实测）。默认值 ``deepseek`` / ``deepseek-chat`` 与
    :attr:`ResolvedProfile.model` 的 ``default_factory`` 完全一致，也与
    :func:`resolve_profile` 里那条"合并后没有 model 段就用 deepseek/deepseek-chat"
    的 warning 说的是同一件事 —— 这里只是把同一套回退提前到层级别。
    """

    model_name: str = "deepseek-chat"
    """模型名，如 ``deepseek-flash``。默认值与 :attr:`ResolvedProfile.model` 一致。"""

    api_key_env: str = "LLM_API_KEY"
    """**环境变量名**（不是明文 key），由 builder 在装配时解引用。"""

    base_url_env: str = "LLM_BASE_URL"
    """base url 的环境变量名。"""

    temperature: float = 0.0
    """采样温度。"""

    max_tokens: int | None = None
    """单次输出上限；``None`` 交给 provider 默认值。"""

    stream: bool = True
    """是否开启流式输出，直接传给 ``ChatModelBase.__init__(stream=...)``。"""

    timeout_s: float = 60.0
    """单次请求超时（秒），透传给 ``openai.AsyncClient(timeout=...)``。"""

    extra: dict[str, Any] = Field(default_factory=dict)
    """透传给 provider 的额外参数（例如 ``thinking_enable`` / ``reasoning_effort``）。"""


class ToolsSpec(BaseModel):
    """工具层的声明。"""

    model_config = ConfigDict(extra="forbid")

    packs: list[str] = Field(default_factory=list)
    """工具包名列表，逐个走 :meth:`HarnessRegistry.get` 的 ``tool_pack`` 类目。"""

    groups: dict[str, list[str]] = Field(default_factory=dict)
    """``工具组名 -> 工具名列表``，用于把工具塞进非 ``basic`` 的组，
    交给 ``Toolkit(tool_groups=[ToolGroup(...)])``。"""

    disabled: list[str] = Field(default_factory=list)
    """按工具名禁用（装配完成后从 Toolkit 里摘掉）。"""

    max_result_chars: int = 8000
    """单个工具结果回灌进上下文前的最大字符数。"""


class SkillsSpec(BaseModel):
    """技能层的声明。"""

    model_config = ConfigDict(extra="forbid")

    directories: list[str] = Field(default_factory=list)
    """技能目录，交给
    :func:`harness_kit.skills.loaders_from_spec` 造出的
    :class:`harness_kit.skills.HarnessSkillLoader`（第 6 讲交付物；
    它是 :class:`agentscope.skill.LocalSkillLoader` 的子类）。"""

    enabled: list[str] = Field(default_factory=list)
    """启用的技能名；空列表表示目录里的全部技能。"""

    scan_subdir: bool = True
    """是否扫描子目录。已知坑：``LocalSkillLoader`` 默认 ``scan_subdir=False``，
    子目录里的 ``SKILL.md`` 会被漏掉。"""

    disclosure: Literal["index", "full"] = "index"
    """渐进披露层级：``index`` 只把技能索引塞进 prompt，``full`` 直接塞全文。"""


class MCPSpec(BaseModel):
    """MCP 层的声明。"""

    model_config = ConfigDict(extra="forbid")

    servers: list[MCPServerSpec] = Field(default_factory=list)  # type: ignore[valid-type]
    """要连接的 MCP server 声明。"""

    group: str = "mcp"
    """这些 MCP 提供的工具注册进哪个工具组。"""


class MiddlewareSpec(BaseModel):
    """单个中间件的声明。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """中间件名，走 :meth:`HarnessRegistry.get` 的 ``middleware`` 类目。"""

    params: dict[str, Any] = Field(default_factory=dict)
    """构造参数，原样传给工厂。"""


class WorkspaceSpec(BaseModel):
    """工作区 / 沙箱的声明。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["local", "docker"] = "local"
    """后端种类。"""

    root: str = "./.harness/workspace"
    """工作区根目录（相对路径按 ``Settings.repo_root`` 解析）。"""

    policy: SandboxPolicy | None = None  # type: ignore[valid-type]
    """沙箱策略；``None`` 表示不启用策略层。"""

    @model_validator(mode="before")
    @classmethod
    def _fill_policy_root(cls, data: Any) -> Any:
        """把 ``policy.workspace_root`` 缺省值补成 ``root``。

        契约 §3.10 的 ``SandboxPolicy.workspace_root`` 是必填字段，而 §6.3 的
        ``research.yaml`` 并没有在 ``policy:`` 块里写它 —— 用 ``WorkspaceSpec.root``
        回填即可让两边自洽，且真实 ``SandboxPolicy`` 一交付就直接受益。

        Args:
            data (`Any`): 原始输入。

        Returns:
            `Any`: 回填后的输入。
        """
        if not isinstance(data, dict):
            return data
        policy = data.get("policy")
        if not isinstance(policy, dict):
            return data
        if policy.get("workspace_root") is not None:
            return data
        if "root" not in data:
            return data
        patched = dict(data)
        patched["policy"] = {**policy, "workspace_root": data["root"]}
        return patched


class PermissionSpec(BaseModel):
    """权限层的声明。"""

    model_config = ConfigDict(extra="forbid")

    mode: str = "default"
    """映射到 ``agentscope.permission.PermissionMode`` 的 5 个值之一
    （``default`` / ``accept_edits`` / ``explore`` / ``bypass`` / ``dont_ask``）。"""

    rule_files: list[str] = Field(default_factory=list)
    """规则文件路径列表，由第 11 讲的 ``RuleSet`` 装载成 ``PermissionRule``。"""

    audit_path: str = "./.harness/audit.jsonl"
    """权限决策审计日志落盘路径。"""

    hitl_timeout_s: float = 300.0
    """HITL 确认超时（秒）。"""


class MemorySpec(BaseModel):
    """ReMe 长期记忆层的声明。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """是否装配 ReMe。"""

    workspace_root: str = "./.harness/reme"
    """ReMe 工作区根目录。"""

    embedding_dimensions: int | None = None
    """``None`` 表示不装配 embedding 组件（此时向量检索答空但 ``success=True``，
    这是**合法**状态，不是错误）。"""

    catalog: str = "default"
    """文件目录（catalog）名。"""

    mode: Literal["static_control", "agent_control", "both"] = "static_control"
    """检索触发方式。"""

    top_k: int = 5
    """召回条数。"""

    min_score: float = 0.0
    """分数下限。"""

    inject_budget_tokens: int = 1200
    """注入上下文的记忆内容 token 预算。"""

    jobs: list[str] = Field(default_factory=list)
    """要用的 ReMe job 名（``search`` / ``auto_memory`` / ...）。"""


class AgentSpec(BaseModel):
    """Agent 本身的声明。"""

    model_config = ConfigDict(extra="forbid")

    name: str = "harness-agent"
    """``Agent(name=...)``。"""

    sys_prompt: str = ""
    """``Agent(system_prompt=...)``。"""

    max_iters: int = 20
    """映射到 ``ReActConfig(max_iters=...)``。"""

    parallel_tool_calls: bool = True
    """并行工具调用。

    **契约偏离（已核实）**：AgentScope 2.0.8 的 ``Agent`` / ``ReActConfig`` 里
    **没有** ``parallel_tool_calls``（``third_party/agentscope/src/agentscope/agent/_config.py``
    全文 grep 无此字段）；它只存在于两个模型参数类：
    ``third_party/agentscope/src/agentscope/model/_openai_chat/_model.py:87`` 与
    ``.../model/_dashscope/_model.py:90``。因此 builder 的策略是：
    模型参数类有该字段就设进去，没有就打一条 warning 并忽略。
    """

    enable_hitl: bool = True
    """是否允许 HITL（Human-in-the-loop）确认。

    **契约偏离（已核实）**：AgentScope 没有 ``enable_hitl`` 开关；HITL 是
    ``PermissionEngine`` 产出 ``ASK`` 决策后 Agent 自然 ``yield``
    ``RequireUserConfirmEvent``（``third_party/agentscope/src/agentscope/event/_event.py:443``）
    的行为。因此 builder 把 ``enable_hitl=False`` 翻译成
    ``PermissionMode.DONT_ASK``（把所有 ASK 转成 DENY，见
    ``third_party/agentscope/src/agentscope/permission/_types.py:18`` 的文档表），
    这样无人值守场景不会挂起等确认。
    """


_SPEC_FIELDS: tuple[str, ...] = (
    "model",
    "tools",
    "skills",
    "mcp",
    "middleware",
    "workspace",
    "permission",
    "memory",
    "agent",
)
"""``ResolvedProfile`` 里被合并的 9 个字段，顺序即 ``explain()`` 的打印顺序。"""


class _RawOverlayMixin(BaseModel):
    """让 ``own_overlay()`` 返回**未经 pydantic 校验的原样 YAML 片段**。

    **为什么必须有这一层**：契约 §6.2 承诺 ``packs: !append [repo]`` 是"追加"，
    但 :class:`AppendList` 是 ``list`` 的子类，而 pydantic 校验 ``packs: list[str]``
    时会**新建一个普通 list**，标记当场就丢了（已实测：
    ``ToolsSpec(packs=AppendList(["repo"])).packs`` 的 ``type`` 是 ``list``）。
    于是 :func:`_merge` 拿到的是一段普通列表 → 走了"整体替换"分支 →
    ``!append`` 静默退化成覆盖，``coding.yaml`` 的 ``packs`` 从
    ``['builtin', 'repo']`` 变成 ``['repo']``，**不报任何错**。

    修法不是在每个 Spec 字段上挂校验器（那要改十几处，还得跟着字段增删走），
    而是把"合并的输入"换成 :func:`~harness_kit.config.loader.load_yaml` 出来的
    原始片段 —— 那里 :class:`AppendList` 由 YAML 构造器直接产出
    （``harness_kit/config/loader.py:90`` 的 ``_construct_append``），
    一路到 :func:`_merge` 都没被 pydantic 碰过。

    ``exclude_unset=True`` 的语义由此天然保留：YAML 里没写的键，原始片段里就没有。
    """

    model_config = ConfigDict(extra="forbid")

    _raw_overlay: dict[str, Any] | None = PrivateAttr(default=None)
    """装载时留下的原样 YAML 片段；直接 ``Profile(...)`` 构造出来的对象是 ``None``。"""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Any:
        """校验一个 YAML 片段，并把原样片段挂在私有属性上。

        Args:
            payload (`Mapping[str, Any]`): 已经做过 ``${VAR}`` 插值的 YAML 顶层映射。

        Returns:
            `Any`: 校验通过的对象（``Profile`` 或 ``Bundle``）。

        Raises:
            `pydantic.ValidationError`: 字段不符合契约。
        """
        instance = cls.model_validate(payload)
        overlay = {
            key: value
            for key, value in payload.items()
            if key not in _NON_OVERLAY_KEYS
        }
        instance._raw_overlay = copy.deepcopy(dict(overlay))
        return instance

    def own_overlay(self) -> dict[str, Any]:
        """返回"本对象自己的覆盖"字典，供合并算法消费。

        优先返回 :meth:`from_payload` 留下的**原样片段**（``!append`` 标记因此
        能活着走到 :func:`_merge`）；对象是直接构造出来的（没有原样片段）时，
        退回 ``model_dump(exclude_unset=True)`` —— 那条路丢掉 ``!append`` 标记，
        所以只作为兜底，装载路径一律走 :meth:`from_payload`。

        Returns:
            `dict[str, Any]`: 去掉 ``name`` / ``description`` / ``extends`` / ``bundles``
            之后的覆盖字典。
        """
        if self._raw_overlay is not None:
            return copy.deepcopy(self._raw_overlay)
        return self.model_dump(
            exclude_unset=True,
            exclude=set(_NON_OVERLAY_KEYS),
        )


_NON_OVERLAY_KEYS: tuple[str, ...] = ("name", "description", "extends", "bundles")
"""不参与合并的键：它们是 Profile 的元信息，不是能力声明。"""


class Bundle(_RawOverlayMixin):
    """能力包：一组可以跨 Profile 复用的装配片段。"""

    name: str
    description: str = ""
    model: ModelSpec | None = None
    tools: ToolsSpec | None = None
    skills: SkillsSpec | None = None
    mcp: MCPSpec | None = None
    middleware: list[MiddlewareSpec] = Field(default_factory=list)
    workspace: WorkspaceSpec | None = None
    permission: PermissionSpec | None = None
    memory: MemorySpec | None = None
    agent: AgentSpec | None = None


class Profile(_RawOverlayMixin):
    """场景 Profile：``extends`` 一个父 Profile，叠加若干 Bundle，再写自己的覆盖。"""

    name: str
    description: str = ""
    extends: str | None = None
    """单继承：父 Profile 的名字或 YAML 路径。"""

    bundles: list[str] = Field(default_factory=list)
    """按名引用的 Bundle，从左到右合并，后者胜。"""

    model: ModelSpec | None = None
    tools: ToolsSpec | None = None
    skills: SkillsSpec | None = None
    mcp: MCPSpec | None = None
    middleware: list[MiddlewareSpec] = Field(default_factory=list)
    workspace: WorkspaceSpec | None = None
    permission: PermissionSpec | None = None
    memory: MemorySpec | None = None
    agent: AgentSpec | None = None


class ResolvedProfile(BaseModel):
    """合并并冻结后的结果。所有 Spec 字段都有默认值，不会再被修改。"""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    source_chain: list[str] = Field(default_factory=list)
    """合并来源链，形如 ``["profile:default", "bundle:repo", "profile:coding"]``。"""

    source_map: dict[str, str] = Field(default_factory=dict)
    """``点号路径 -> 来源标签``，由合并过程记录，供 :meth:`explain` 使用。"""

    model: ModelSpec = Field(
        default_factory=lambda: ModelSpec(
            provider="deepseek",
            model_name="deepseek-chat",
        ),
    )
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    skills: SkillsSpec = Field(default_factory=SkillsSpec)
    mcp: MCPSpec = Field(default_factory=MCPSpec)
    middleware: list[MiddlewareSpec] = Field(default_factory=list)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    permission: PermissionSpec = Field(default_factory=PermissionSpec)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    agent: AgentSpec = Field(default_factory=AgentSpec)

    def _sources_of(self, path: str) -> list[str]:
        """返回 ``path`` 这棵子树上所有贡献过的来源标签。

        Args:
            path (`str`): 点号路径。

        Returns:
            `list[str]`: 去重排序后的来源标签；没有任何贡献者时返回 ``["default"]``。
        """
        hits = {
            label
            for key, label in self.source_map.items()
            if key == path or key.startswith(f"{path}.")
        }
        return sorted(hits) if hits else ["default"]

    def _source_of_leaf(self, path: str) -> str:
        """返回某个叶子路径的来源标签（最长前缀匹配）。

        Args:
            path (`str`): 点号路径。

        Returns:
            `str`: 来源标签；没有记录时返回 ``"default"``。
        """
        best_key = ""
        for key in self.source_map:
            if key == path or path.startswith(f"{key}."):
                if len(key) > len(best_key):
                    best_key = key
        return self.source_map.get(best_key, "default")

    def explain(self) -> str:
        """逐字段打印"这个值来自哪个文件"。

        Returns:
            `str`: 多行文本，可直接作为 ``harness-kit profile explain`` 的输出。
        """
        lines: list[str] = [
            f"profile: {self.name}",
            f"description: {self.description}",
            f"source_chain: {' -> '.join(self.source_chain)}",
        ]
        for field_name in _SPEC_FIELDS:
            value = getattr(self, field_name)
            sources = ", ".join(self._sources_of(field_name))
            lines.append(f"\n[{field_name}] 来自: {sources}")
            flattened = _flatten(value, field_name)
            if not flattened:
                lines.append("    (空)")
                continue
            for path, text in flattened:
                lines.append(f"    {path} = {text}   # {self._source_of_leaf(path)}")
        return "\n".join(lines)


# ======================================================================
# 合并算法
# ======================================================================
class AppendList(list[Any]):
    """带 YAML ``!append`` 标签的 list。

    :func:`merge_dicts` 遇到它会做"追加"而不是"整体替换"。
    例：``packs: !append [repo]`` 会把 ``repo`` 追加到继承来的 ``packs`` 之后。
    """


class _Delete:
    """内部哨兵：表示"这个键要删掉"。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<DELETE>"


_DELETE = _Delete()


def _flatten(value: Any, prefix: str) -> list[tuple[str, str]]:
    """把 pydantic 模型 / 嵌套容器摊平成 ``(点号路径, 值文本)`` 列表。

    Args:
        value (`Any`): 待摊平的对象。
        prefix (`str`): 路径前缀。

    Returns:
        `list[tuple[str, str]]`: 叶子路径与紧凑的取值文本。
    """
    if isinstance(value, BaseModel):
        return _flatten(value.model_dump(mode="json"), prefix)
    if isinstance(value, Mapping):
        out: list[tuple[str, str]] = []
        for key, item in value.items():
            out.extend(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, (list, tuple)):
        if not value:
            return [(prefix, "[]")]
        out = []
        for index, item in enumerate(value):
            item_path = f"{prefix}[{index}]"
            if isinstance(item, (Mapping, list, tuple, BaseModel)):
                out.extend(_flatten(item, item_path))
            else:
                out.append((item_path, repr(item)))
        return out
    return [(prefix, repr(value))]


def _merge(
    base_value: Any,
    override_value: Any,
    *,
    sources: dict[str, str] | None,
    prefix: str,
    label: str,
) -> Any:
    """单值合并内核。

    Args:
        base_value (`Any`): 基线值。
        override_value (`Any`): 覆盖值。
        sources (`dict[str, str] | None`): 来源记录累加器；``None`` 表示不记录。
        prefix (`str`): 当前键的点号路径。
        label (`str`): 当前覆盖层的来源标签。

    Returns:
        `Any`: 合并结果；返回 :data:`_DELETE` 表示删除该键。
    """
    if override_value is None:
        return _DELETE

    if isinstance(override_value, AppendList):
        base_list = list(base_value) if isinstance(base_value, list) else []
        if sources is not None:
            sources[prefix] = label
        return base_list + list(override_value)

    if isinstance(base_value, Mapping) and isinstance(
        override_value,
        Mapping,
    ):
        merged: dict[str, Any] = dict(base_value)
        for key, value in override_value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            if key in merged:
                outcome = _merge(
                    merged[key],
                    value,
                    sources=sources,
                    prefix=child_path,
                    label=label,
                )
                if outcome is _DELETE:
                    merged.pop(key, None)
                else:
                    merged[key] = outcome
            else:
                outcome = _merge(
                    {},
                    value,
                    sources=sources,
                    prefix=child_path,
                    label=label,
                )
                if outcome is not _DELETE:
                    merged[key] = outcome
        return merged

    if sources is not None:
        sources[prefix] = label
    if isinstance(override_value, Mapping):  # pragma: no cover - 类型收窄
        return dict(override_value)
    if isinstance(override_value, list):
        return list(override_value)
    if isinstance(override_value, BaseModel):  # pragma: no cover - 类型收窄
        return override_value.model_copy(deep=True)
    return override_value


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深合并两个字典（契约 §6.2 的唯一实现）。

    - 两边都是 map → 递归深合并；
    - 叶子字段冲突 → ``override`` 胜；
    - 两边都是 list → 默认**整体替换**；
    - ``override`` 的值是 :class:`AppendList`（YAML ``!append``）→ 追加到 base 之后；
    - ``override`` 的值显式是 ``None`` → **删除**该键。

    Args:
        base (`dict[str, Any]`): 基线字典。
        override (`dict[str, Any]`): 覆盖字典。

    Returns:
        `dict[str, Any]`: 新字典，``base`` 与 ``override`` 都不会被就地修改。

    Example:
        >>> merge_dicts({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
        {'a': {'b': 9, 'c': 2}}
        >>> merge_dicts({"packs": ["builtin"]}, {"packs": AppendList(["repo"])})
        {'packs': ['builtin', 'repo']}
        >>> merge_dicts({"memory": {"enabled": True}}, {"memory": None})
        {}
    """
    merged = _merge(base, override, sources=None, prefix="", label="")
    if merged is _DELETE:  # pragma: no cover - 顶层不会走到
        return {}
    if not isinstance(merged, dict):  # pragma: no cover - 类型收窄
        raise TypeError(f"merge_dicts 的结果不是 dict: {type(merged)!r}")
    return merged


# ======================================================================
# Profile 解析
# ======================================================================
def resolve_profile(profile: Profile, *, search_dir: Path) -> ResolvedProfile:
    """把 :class:`Profile` 解析成冻结的 :class:`ResolvedProfile`。

    顺序严格照契约 §6.1：

    1. 解析 ``extends`` 链（单继承，沿链向上递归，检测成环）；
    2. 从基到子逐层合并：每层先合并它的 ``bundles``（从左到右，后者胜），再应用它自身的覆盖；
    3. pydantic 校验（缺字段用 Spec 默认值补齐）→ 冻结。

    Args:
        profile (`Profile`): 待解析的 Profile。
        search_dir (`Path`): ``extends`` / ``bundles`` 按名查找时的搜索目录。

    Returns:
        `ResolvedProfile`: 合并冻结后的结果，带 ``source_chain`` 与 ``source_map``。

    Raises:
        ConfigCycleError: ``extends`` 链成环（``harness_kit.config.loader`` 提供）。
        ConfigNotFoundError: ``extends`` 或 ``bundles`` 引用了不存在的名字。
    """
    # 延迟导入：loader 在模块顶层 import 本模块的模型，避免循环导入
    from harness_kit.config.loader import (
        ConfigCycleError,
        ConfigNotFoundError,
        load_bundle,
        load_profile,
    )

    chain: list[tuple[str, Profile]] = []
    seen: set[str] = set()
    cursor: Profile | None = profile
    while cursor is not None:
        key = cursor.name
        if key in seen:
            raise ConfigCycleError(
                f"extends 链成环：{' -> '.join([n for n, _ in chain] + [key])}",
            )
        seen.add(key)
        chain.append((key, cursor))
        if cursor.extends is None:
            break
        cursor = load_profile(cursor.extends, search_dir=search_dir)
    chain.reverse()  # 基类在前，子类在后

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    source_chain: list[str] = []

    for name, layer in chain:
        # source_chain 记录的是**合并顺序**：先 bundles（从左到右，后者胜），
        # 再本层自身的覆盖 —— 与下面 _merge 的调用顺序严格一致，
        # 这样 explain() 的「谁覆盖了谁」才不是一句空话。
        for bundle_name in layer.bundles:
            try:
                bundle = load_bundle(bundle_name, search_dir=search_dir)
            except FileNotFoundError as exc:
                raise ConfigNotFoundError(
                    f"Profile '{name}' 引用了不存在的 Bundle '{bundle_name}'"
                    f"（搜索目录 {search_dir}）",
                ) from exc
            label = f"bundle:{bundle_name}"
            source_chain.append(label)
            # 走 own_overlay() 而不是就地 model_dump：Bundle 里的 `!append`
            # 只有这样才活得下来（见 _RawOverlayMixin 的说明）。
            payload = bundle.own_overlay()
            merged = _merge(
                merged,
                payload,
                sources=sources,
                prefix="",
                label=label,
            )
        source_chain.append(f"profile:{name}")
        merged = _merge(
            merged,
            layer.own_overlay(),
            sources=sources,
            prefix="",
            label=f"profile:{name}",
        )

    if "model" not in merged:
        logger.bind(profile=profile.name).warning(
            "Profile '{}' 合并后没有 model 段，将使用默认 {}/{}",
            profile.name,
            "deepseek",
            "deepseek-chat",
        )

    resolved = ResolvedProfile.model_validate(
        {
            **merged,
            "name": profile.name,
            "description": profile.description,
            "source_chain": source_chain,
            "source_map": sources,
        },
    )
    logger.bind(profile=profile.name).debug(
        "ResolvedProfile 合并完成: {}",
        source_chain,
    )
    return resolved


def utc_now() -> datetime:
    """返回当前 UTC 时间（tz-aware）。

    仅作为便利函数复用 :mod:`harness_kit.events.types` 的同名实现。

    Returns:
        `datetime`: 带 ``timezone.utc`` 的当前时间。
    """
    from harness_kit.events.types import utc_now as _utc_now

    return _utc_now()
