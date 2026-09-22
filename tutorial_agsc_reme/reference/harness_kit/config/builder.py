# -*- coding: utf-8 -*-
"""HarnessBuilder —— 把 :class:`ResolvedProfile` 变成真实的 AgentScope 运行对象。

这是整套脚手架的**心脏**：契约 §1.3 的缺口 5（"没有声明式 Profile/Bundle 装配"）
就落在这个文件。AgentScope 原生的装配是硬编码在
``third_party/agentscope/src/agentscope/app/_app.py:78`` 的 ``create_app`` 里的，
本模块把它换成"读 Profile → 查注册表 → 造对象"。

用到的真实 AgentScope API（全部实测）：

=========================================== ================================================================
能力                                          源码位置
=========================================== ================================================================
``Agent(name, system_prompt, model, ...)``   ``third_party/agentscope/src/agentscope/agent/_agent.py:117``
``AgentState(session_id=..., permission_context=...)`` ``third_party/agentscope/src/agentscope/state/_state.py:209``
``ReActConfig(max_iters=...)``               ``third_party/agentscope/src/agentscope/agent/_config.py:362``
``ContextConfig(tool_result_limit=...)``     ``third_party/agentscope/src/agentscope/agent/_config.py:51``
``Toolkit(tools, skills_or_loaders, mcps, tool_groups)`` ``third_party/agentscope/src/agentscope/tool/_toolkit.py:66``
``ToolGroup(name, description, tools, ...)`` ``third_party/agentscope/src/agentscope/tool/_tool_group.py:10``
``LocalSkillLoader(directory, scan_subdir)`` ``third_party/agentscope/src/agentscope/skill/_local_loader.py:16``
``PermissionContext(mode, allow_rules, ...)`` ``third_party/agentscope/src/agentscope/permission/_context.py:24``
``PermissionEngine(context).add_rule(rule)`` ``third_party/agentscope/src/agentscope/permission/_engine.py:17``, ``:49``
``PermissionMode`` / ``PermissionBehavior``  ``third_party/agentscope/src/agentscope/permission/_types.py:18``, ``:88``
``LocalWorkspace(workdir=...)``              ``third_party/agentscope/src/agentscope/workspace/_local_workspace.py:77``
``DockerWorkspace(host_workdir=...)``        ``third_party/agentscope/src/agentscope/workspace/_docker/_docker_workspace.py:52``
``Offloader`` Protocol（3 个方法）             ``third_party/agentscope/src/agentscope/workspace/_offload_protocol.py:8``
=========================================== ================================================================

**权限接线的关键事实**（实测）：``Agent`` 内部自己造引擎
（``agent/_agent.py:193`` 的 ``self._engine = PermissionEngine(self.state.permission_context)``），
所以想让 Profile 的权限配置真正生效，唯一正确的做法是**在构造 Agent 之前**
把配置好的 ``PermissionContext`` 塞进 ``AgentState``。本模块因此让
:meth:`HarnessBuilder.build_permission_engine` 与 :meth:`HarnessBuilder.build_agent`
共用同一个 context（引擎对象只是给调用方做预检 / 审计用的把手）。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.config.schema import (
    PermissionSpec,
    ResolvedProfile,
    WorkspaceSpec,
)
from harness_kit.registry import (
    BuildContext,
    ComponentNotAvailableError,
    HarnessRegistry,
    UnknownComponentError,
    accepts_build_context,
)
from harness_kit.settings import Settings
from harness_kit.skills import build_skill_instruction_template

# 运行期对象（Agent / Toolkit / Workspace 等）不是 pydantic 模型，
# 但它们都实现了 Offloader / MiddlewareBase 之类的协议，这里只做类型注解。
from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.permission import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolGroup, Toolkit
from agentscope.workspace import LocalWorkspace, Offloader, WorkspaceBase

__all__ = [
    "BuildContext",
    "BuiltHarness",
    "HarnessBuilder",
    "build_from_profile",
]

_CHARS_PER_TOKEN: int = 4
"""``ContextConfig.tool_result_limit`` 的单位是 **token**
（``third_party/agentscope/src/agentscope/agent/_config.py:146``），而
:attr:`~harness_kit.config.schema.ToolsSpec.max_result_chars` 的单位是**字符**。
用业界常用的 4 字符 ≈ 1 token 做换算，并把换算过程写进日志，避免读者以为两者同单位。
"""


class BuiltHarness(BaseModel):
    """一次完整装配的产物。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profile: ResolvedProfile
    """本次装配使用的已解析 Profile。"""

    model: ChatModelBase
    """``ChatModelBase`` 实例（DeepSeek / OpenAI / 自定义适配器）。"""

    toolkit: Toolkit
    """装载了工具包、技能、MCP 的 ``Toolkit``。"""

    middlewares: list[MiddlewareBase] = Field(default_factory=list)
    """按 Profile 顺序排好的中间件链。"""

    workspace: WorkspaceBase | None = None
    """工作区对象；``None`` 表示本次装配不带 workspace。"""

    permission_engine: PermissionEngine | None = None
    """权限引擎把手。注意：Agent 内部**另有一个**引擎，两者共享同一个
    ``PermissionContext``（见模块 docstring 的"权限接线的关键事实"）。"""

    memory: Any | None = None
    """记忆客户端（当前为 ReMe 嵌入式 Application 的门面）。"""

    agent: Agent
    """最终装配出的 AgentScope ``Agent``。"""

    session_id: str
    """本次装配绑定的会话 id（写进了 ``AgentState.session_id``）。"""

    @property
    def state(self) -> AgentState:
        """本次装配产物的 AgentState。

        Returns:
            `AgentState`: ``agent.state``。
        """
        return self.agent.state


class HarnessBuilder:
    """把 :class:`ResolvedProfile` 变成真实运行对象。唯一装配出口。

    Example:
        >>> from harness_kit.registry import HarnessRegistry
        >>> from harness_kit.settings import Settings
        >>> settings = Settings.from_env()
        >>> builder = HarnessBuilder(profile, settings=settings)   # doctest: +SKIP
        >>> async with builder:                                    # doctest: +SKIP
        ...     built = await builder.build_all()                  # doctest: +SKIP
        ...     reply = await built.agent.reply(UserMsg("user", "hi"))
    """

    def __init__(
        self,
        profile: ResolvedProfile,
        *,
        settings: Settings,
        registry: HarnessRegistry | None = None,
        session_id: str | None = None,
        offloader: Offloader | None = None,
        context_budget: Any | None = None,
    ) -> None:
        """初始化装配器。

        Args:
            profile (`ResolvedProfile`): 已解析冻结的 Profile。
            settings (`Settings`): 全局设置（路径锚点 + LLM 凭据来源）。
            registry (`HarnessRegistry | None`): 组件注册表；``None`` 时用
                :meth:`HarnessRegistry.default`，并立即 :meth:`HarnessRegistry.freeze`。
            session_id (`str | None`): 会话 id；``None`` 时生成 ``uuid4().hex``。
            offloader (`Offloader | None`): 自定义 offloader；``None`` 时优先用
                第 10 讲的 ``HarnessOffloader``，再退回把 workspace 本身当 offloader。
            context_budget (`ContextBudgetSpec | None`): 短期上下文压缩的声明式
                配置（第 8 讲补遗，:class:`~harness_kit.middleware.compact.ContextBudgetSpec`）。
                ``None`` 时只把 ``ToolsSpec.max_result_chars`` 翻译成
                ``tool_result_limit``，其余 ``ContextConfig`` 字段用官方默认。
                **刻意做成显式入参而不是 Profile 字段**：``Profile`` 的 schema
                属于第 2 讲，压缩配置属于第 8 讲，让第 2 讲的文件去 import 第 8 讲
                的模块会造出一条反向依赖（"第 N 讲用了第 N+6 讲的类"）。
        """
        self.profile = profile
        self.settings = settings
        if registry is None:
            registry = HarnessRegistry.default()
            registry.freeze()
        self.registry = registry

        self.session_id: str = session_id or uuid4().hex
        self._offloader_override = offloader
        self._context_budget_override = context_budget

        self._context_obj = BuildContext(
            settings=settings,
            profile=profile,
            workspace=None,
        )

        # 每个 build_* 都做一次缓存，保证 build_all() 与单独调用结果一致
        self._model: ChatModelBase | None = None
        self._toolkit: Toolkit | None = None
        self._middlewares: list[MiddlewareBase] | None = None
        self._workspace: WorkspaceBase | None = None
        self._permission_context: PermissionContext | None = None
        self._permission_engine: PermissionEngine | None = None
        self._memory: Any | None = None
        self._agent: Agent | None = None
        self._state: AgentState | None = None
        self._closed: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "HarnessBuilder":
        """``async with`` 入口。

        Returns:
            `HarnessBuilder`: 自身。
        """
        return self

    async def __aexit__(self, *exc: object) -> None:
        """``async with`` 出口，等价于 :meth:`aclose`。

        Args:
            *exc (`object`): 异常三元组，未使用。
        """
        del exc
        await self.aclose()

    async def aclose(self) -> None:
        """**逆序**释放已装配的资源。

        顺序：agent（无资源） → middlewares（``close``/``aclose``） →
        memory → workspace → model。任何一步抛异常都只记 warning，不阻断后续释放。
        """
        if self._closed:
            logger.debug("HarnessBuilder 已释放过，跳过重复 aclose")
            return
        self._closed = True

        for middleware in reversed(self._middlewares or []):
            await self._safe_close(middleware, kind="middleware")
        await self._safe_close(self._memory, kind="memory")
        await self._safe_close(self._workspace, kind="workspace")
        await self._safe_close(self._model, kind="model")

        logger.bind(session_id=self.session_id).debug("HarnessBuilder 已释放")

    @staticmethod
    async def _safe_close(target: Any, *, kind: str) -> None:
        """尽力释放一个对象：有 ``aclose`` 用 ``aclose``，否则用 ``close``。

        Args:
            target (`Any`): 待释放对象。
            kind (`str`): 类目名，仅用于日志。
        """
        if target is None:
            return
        closer = getattr(target, "aclose", None) or getattr(target, "close", None)
        if not callable(closer):
            return
        try:
            outcome = closer()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 - 释放阶段的异常不能阻断其他释放
            logger.bind(kind=kind).warning(
                "释放 {} 时出错（已忽略）: {}: {}",
                kind,
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------
    # 工厂调用
    # ------------------------------------------------------------------
    @property
    def context(self) -> BuildContext:
        """当前装配上下文。

        Returns:
            `BuildContext`: 与 :attr:`profile` / :attr:`settings` 绑定，
            :attr:`BuildContext.workspace` 会在 :meth:`build_workspace` 之后被填上。
        """
        return self._context_obj

    @staticmethod
    async def _invoke(
        factory: Callable[..., Any],
        spec: Any,
        ctx: BuildContext,
    ) -> Any:
        """调用一个工厂，按需要补上 ``ctx`` 关键字，并容忍同步 / 异步两种实现。

        **为什么必须容忍同步**：注册表里的工厂有两个来源 —— AgentScope 原生的
        工厂是异步的，而 harness_kit 自己的工厂有的是**同步函数**
        （第 4 讲的 ``build_echo_model``，``harness_kit/models/adapters/echo.py:396``）。
        契约 §3.2 把工厂签名写成 ``Callable[[XSpec], Awaitable[...]]``，实现里却
        确实存在同步工厂；若这里直接 ``await``，同步工厂的返回值会抛
        ``TypeError: object EchoChatModel can't be used in 'await' expression``
        （本讲实测，见第六节排查表）。因此统一成「可等待就 await，否则原样返回」。

        Args:
            factory (`Callable[..., Any]`): 工厂。
            spec (`Any`): 该层级的 Spec。
            ctx (`BuildContext`): 装配上下文。

        Returns:
            `Any`: 工厂返回值（可等待则已 await）。
        """
        outcome = (
            factory(spec, ctx=ctx) if accepts_build_context(factory) else factory(spec)
        )
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    @staticmethod
    async def _invoke_any(
        factory: Callable[..., Any],
        spec: Any,
        ctx: BuildContext,
    ) -> Any:
        """``_invoke`` 的别名，语义完全相同（中间件工厂天然是同步的）。

        Args:
            factory (`Callable[..., Any]`): 工厂。
            spec (`Any`): 该层级的 Spec。
            ctx (`BuildContext`): 装配上下文。

        Returns:
            `Any`: 工厂返回值（若可等待则已 await）。
        """
        return await HarnessBuilder._invoke(factory, spec, ctx)

    # ------------------------------------------------------------------
    # 各层装配
    # ------------------------------------------------------------------
    async def build_model(self) -> ChatModelBase:
        """按 ``profile.model.provider`` 造模型。

        Returns:
            `ChatModelBase`: 模型对象（已缓存）。

        Raises:
            UnknownComponentError: provider 未在注册表登记。
        """
        if self._model is not None:
            return self._model
        spec = self.profile.model
        factory = self.registry.get("model", spec.provider)
        self._model = await self._invoke(factory, spec, self.context)
        logger.bind(
            session_id=self.session_id,
            provider=spec.provider,
            model=spec.model_name,
            stream=spec.stream,
        ).debug("模型已装配")
        return self._model

    async def build_workspace(self) -> WorkspaceBase | None:
        """按 ``profile.workspace`` 选 workspace 后端。

        选型策略：配了 ``policy`` 时优先用第 10 讲的策略化实现
        （``policy_local`` / ``quota_docker``）；不可用时退回 AgentScope 原生实现，
        并**打一条 warning** —— 因为此时 ``policy`` 里的路径/网络/资源限制并不会被执行。

        Returns:
            `WorkspaceBase | None`: **未 ``initialize()``** 的 workspace
            （拉起容器/建目录有副作用，交给调用方决定时机）。
        """
        if self._workspace is not None:
            return self._workspace

        spec: WorkspaceSpec = self.profile.workspace
        policy_names = {
            "local": "policy_local",
            "docker": "quota_docker",
        }
        chosen: str = spec.kind
        if spec.policy is not None:
            preferred = policy_names.get(spec.kind)
            if preferred is not None:
                if self.registry.try_get("workspace", preferred) is not None:
                    chosen = preferred
                else:
                    logger.warning(
                        "Profile 配了 workspace.policy，但第 10 讲的 {} 尚未交付，"
                        "退回 AgentScope 原生 {}；**policy 里的路径/网络/资源限制"
                        "本次不会被执行**",
                        preferred,
                        spec.kind,
                    )

        factory = self.registry.get("workspace", chosen)
        workspace = await self._invoke(factory, spec, self.context)
        self._workspace = workspace
        self.context.workspace = workspace
        logger.bind(
            session_id=self.session_id,
            kind=spec.kind,
            backend=chosen,
            root=spec.root,
        ).debug("workspace 已装配（未 initialize）")
        return workspace

    async def build_mcp_clients(self) -> list[Any]:
        """按 ``profile.mcp`` 装配 MCP 客户端列表。

        Returns:
            `list[Any]`: ``MCPClient`` 列表；``servers`` 为空时返回空列表。

        Raises:
            ComponentNotAvailableError: 声明了 server 但第 7 讲的装配器不可用。
        """
        spec = self.profile.mcp
        if not spec.servers:
            return []
        factory = self.registry.try_get("mcp", "spec_registry")
        if factory is None:
            raise ComponentNotAvailableError(
                f"Profile 声明了 {len(spec.servers)} 个 MCP server，"
                "但第 7 讲的 harness_kit/mcp/registry.py 尚未交付，"
                "无法把 MCPServerSpec 变成 MCPClient",
            )
        clients = await self._invoke(factory, spec, self.context)
        return list(clients)

    async def build_toolkit(self) -> Toolkit:
        """按 ``profile.tools`` / ``skills`` / ``mcp`` 造 ``Toolkit``。

        装配顺序与真实 API 的约束（实测）：

        1. 逐个工具包调注册表工厂（第 5 讲交付后 ``repo`` 包自动可用）；
        2. ``ToolsSpec.groups`` 里的名字建 :class:`ToolGroup` —— **不能叫 ``basic``**，
           那是保留组名（``third_party/agentscope/src/agentscope/tool/_toolkit.py:88``
           会在构造时直接 ``ValueError``）；
        3. 技能目录交给第 6 讲的
           :func:`harness_kit.skills.loader.loaders_from_spec`，由它建
           :class:`~harness_kit.skills.loader.HarnessSkillLoader`
           （继承 ``LocalSkillLoader``，把 ``scan_subdir`` / ``enabled`` /
           租户白名单都变成真的过滤），并按 ``SkillsSpec.disclosure``
           选系统提示词模板；
        4. MCP 客户端塞进 ``basic`` 组或 ``MCPSpec.group`` 指定的组；
        5. 最后按 ``ToolsSpec.disabled`` 摘掉工具。

        Returns:
            `Toolkit`: 可直接交给 ``Agent`` 的 Toolkit。

        Raises:
            UnknownComponentError: 工具包名未登记。
            ValueError: ``groups`` 里出现保留组名 ``basic``，或引用了不存在的工具名。
            ComponentNotAvailableError: 声明了 MCP 但装配器不可用。
        """
        if self._toolkit is not None:
            return self._toolkit

        tools_spec = self.profile.tools
        skills_spec = self.profile.skills
        mcp_spec = self.profile.mcp
        ctx = self.context

        if ctx.workspace is None:
            # 工具包工厂（builtin）需要 workdir 才能给 Bash 定 cwd
            await self.build_workspace()

        produced: list[ToolBase] = []
        for pack_name in tools_spec.packs:
            factory = self.registry.get("tool_pack", pack_name)
            pack_tools = await self._invoke(factory, tools_spec, ctx)
            produced.extend(pack_tools)

        by_name = {tool.name: tool for tool in produced}
        if len(by_name) != len(produced):
            duplicates = [
                name
                for name in {tool.name for tool in produced}
                if sum(1 for tool in produced if tool.name == name) > 1
            ]
            raise ValueError(
                f"工具包里出现重名工具: {duplicates}；"
                "``Toolkit.add_tool`` 对重名只会 warning 并覆盖，这里提前失败",
            )

        tool_groups: list[ToolGroup] = []
        grouped_names: set[str] = set()
        for group_name, tool_names in tools_spec.groups.items():
            if group_name == "basic":
                raise ValueError(
                    "``ToolsSpec.groups`` 里不能出现保留组名 'basic'；"
                    "基本的工具请直接放进 tools.packs",
                )
            missing = [name for name in tool_names if name not in by_name]
            if missing:
                raise ValueError(
                    f"工具组 '{group_name}' 引用了不存在的工具 {missing}；"
                    f"当前已装配: {sorted(by_name)}",
                )
            grouped_names.update(tool_names)
            tool_groups.append(
                ToolGroup(
                    name=group_name,
                    description=(
                        f"Tool group '{group_name}'，包含工具: "
                        f"{', '.join(tool_names)}。"
                        "该组默认不激活，需要 Agent 先调用 meta tool 激活。"
                    ),
                    tools=[by_name[name] for name in tool_names],
                ),
            )

        # 进了工具组的工具必须**离开** basic 组：AgentScope 的 basic 组是
        # 常驻激活的，而 ``tool_groups`` 里的组要靠 meta tool 激活
        # （third_party/agentscope/src/agentscope/tool/_toolkit.py:127/:503）。
        # 同一个工具同时出现在两边会让「按需激活」这个语义失效。
        basic_tools = [tool for tool in produced if tool.name not in grouped_names]

        # 技能装配（第 6 讲接管）：``loaders_from_spec`` 内部用
        # ``HarnessSkillLoader``，它继承 ``LocalSkillLoader`` 并把
        # ``enabled`` / 租户白名单做成真的会过滤 ``list_skills()`` 的开关。
        # 租户从 ``HARNESS_TENANT`` 环境变量读 —— Profile 里不适合放运行时身份。
        skill_loaders: list[Any] = []
        skill_tenant: str | None = self.context.environ.get("HARNESS_TENANT") or None
        if skills_spec.directories:
            from harness_kit.skills import loaders_from_spec

            skill_loaders = list(
                loaders_from_spec(
                    skills_spec,
                    settings=self.settings,
                    tenant=skill_tenant,
                ),
            )

        mcp_clients = await self.build_mcp_clients()
        basic_mcps: list[Any] = []
        if mcp_clients:
            if mcp_spec.group == "basic":
                basic_mcps = mcp_clients
            else:
                tool_groups.append(
                    ToolGroup(
                        name=mcp_spec.group,
                        description=(
                            f"MCP server 提供的工具，共 {len(mcp_clients)} 个 server"
                        ),
                        mcps=mcp_clients,
                    ),
                )

        # ``disclosure`` 直接换成 AgentScope 的 Jinja2 模板：
        # ``index`` ⇒ 只给 name/description/dir（Token 省），
        # ``full`` ⇒ 连 SKILL.md 正文一起铺进系统提示词（省一次工具调用）。
        toolkit = Toolkit(
            tools=basic_tools,
            skills_or_loaders=skill_loaders,
            mcps=basic_mcps,
            tool_groups=tool_groups,
            skill_instruction_template=build_skill_instruction_template(
                skills_spec.disclosure,
            ),
        )

        if tools_spec.disabled:
            present = [name for name in tools_spec.disabled if name in by_name]
            if present:
                await toolkit.remove_tool(present)
            unknown = [name for name in tools_spec.disabled if name not in by_name]
            if unknown:
                logger.bind(disabled=unknown).warning(
                    "ToolsSpec.disabled 里有未装配的工具名，已忽略: {}",
                    unknown,
                )

        self._toolkit = toolkit
        logger.bind(
            session_id=self.session_id,
            packs=tools_spec.packs,
            basic_tools=sorted(tool.name for tool in basic_tools),
            groups={group.name: sorted(t.name for t in group.tools) for group in tool_groups},
        ).debug("Toolkit 已装配")
        return toolkit

    async def build_middlewares(self) -> list[MiddlewareBase]:
        """按 ``profile.middleware`` 顺序装配中间件链。

        Profile 里写的顺序**就是** hook 的执行顺序（Agent 构造时会按
        ``is_implemented`` 把中间件分派到 8 个 hook 点上，见
        ``third_party/agentscope/src/agentscope/agent/_agent.py:207`` 起）。

        Returns:
            `list[MiddlewareBase]`: 中间件列表。

        Raises:
            UnknownComponentError: 中间件名未登记。
        """
        if self._middlewares is not None:
            return self._middlewares

        built: list[MiddlewareBase] = []
        ctx = self.context
        for spec in self.profile.middleware:
            factory = self.registry.get("middleware", spec.name)
            middleware = await self._invoke_any(factory, spec, ctx)
            if not isinstance(middleware, MiddlewareBase):
                raise TypeError(
                    f"中间件 '{spec.name}' 的工厂返回了 "
                    f"{type(middleware).__name__}，不是 MiddlewareBase 子类",
                )
            built.append(middleware)

        self._middlewares = built
        logger.bind(
            session_id=self.session_id,
            middlewares=[type(m).__name__ for m in built],
        ).debug("中间件链已装配")
        return built

    def _permission_mode(self) -> PermissionMode:
        """把 ``PermissionSpec.mode`` 翻译成 :class:`PermissionMode`。

        ``enable_hitl=False`` 会把 ``default`` / ``accept_edits`` 改写成
        ``DONT_ASK``（把所有 ASK 转 DENY），这样无人值守时不会挂起等确认。
        见 :attr:`~harness_kit.config.schema.AgentSpec.enable_hitl` 的契约偏离说明。

        Returns:
            `PermissionMode`: 实际生效的模式。

        Raises:
            ValueError: 模式名不在 AgentScope 的 5 个取值里。
        """
        raw = self.profile.permission.mode.strip().lower()
        try:
            mode = PermissionMode(raw)
        except ValueError as exc:
            valid = [member.value for member in PermissionMode]
            raise ValueError(
                f"PermissionSpec.mode='{raw}' 非法；PermissionMode 只有 {valid} "
                "（third_party/agentscope/src/agentscope/permission/_types.py:18）",
            ) from exc

        if not self.profile.agent.enable_hitl:
            if mode in (PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS):
                logger.bind(session_id=self.session_id).info(
                    "enable_hitl=False，权限模式 {} -> dont_ask（ASK 一律转 DENY）",
                    mode.value,
                )
                return PermissionMode.DONT_ASK
        return mode

    def _load_rules(self, spec: PermissionSpec) -> list[PermissionRule]:
        """从 ``PermissionSpec.rule_files`` 装载规则。

        优先用第 11 讲的 ``RuleSet.from_yaml``（契约 §3.11）；不可用时抛
        :class:`ComponentNotAvailableError`，避免静默地"配了规则但不生效"。

        Args:
            spec (`PermissionSpec`): 权限声明。

        Returns:
            `list[PermissionRule]`: 汇总后的规则列表（按 ``order`` 升序）。

        Raises:
            ComponentNotAvailableError: 声明了规则文件但装载器不可用。
        """
        if not spec.rule_files:
            return []

        try:
            from harness_kit.permission.rules import RuleSet
        except ImportError as exc:
            raise ComponentNotAvailableError(
                f"PermissionSpec 声明了 {len(spec.rule_files)} 个规则文件，"
                "但第 11 讲的 harness_kit/permission/rules.py 尚未交付，"
                "无法把 YAML 变成 PermissionRule",
            ) from exc

        rulesets = [
            RuleSet.from_yaml(Path(self.settings.resolve(path)))
            for path in spec.rule_files
        ]
        rulesets.sort(key=lambda item: item.order)
        rules: list[PermissionRule] = []
        for ruleset in rulesets:
            rules.extend(ruleset.rules)
        logger.bind(
            session_id=self.session_id,
            rule_files=spec.rule_files,
            rule_count=len(rules),
        ).debug("权限规则已装载")
        return rules

    async def build_permission_engine(self) -> PermissionEngine | None:
        """装配权限引擎，并缓存它使用的 :class:`PermissionContext`。

        优先用第 11 讲的 ``HarnessPermissionEngine.from_profile``（它会在原生引擎
        外面补"规则文件 + 审计日志"）；不可用时用原生
        :class:`PermissionEngine` + 本模块装载的规则。

        Returns:
            `PermissionEngine | None`: 引擎把手；``AgentState`` 里会复用它的
            ``context``，因此 Profile 的权限配置对 Agent 真实生效。

        Raises:
            ValueError: 权限模式非法。
            ComponentNotAvailableError: 声明了规则文件但装载器不可用。
        """
        if self._permission_engine is not None:
            return self._permission_engine

        spec = self.profile.permission
        mode = self._permission_mode()

        engine: PermissionEngine | None = None
        factory = self.registry.try_get("permission", "yaml_ruleset")
        if factory is not None:
            try:
                engine = await self._invoke(factory, spec, self.context)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "第 11 讲的权限引擎装配失败（{}），退回原生 PermissionEngine",
                    exc,
                )
                engine = None

        if engine is None:
            if spec.rule_files:
                # 先验证规则可装载，避免"静默无规则"
                rules = self._load_rules(spec)
            else:
                rules = []
            context = PermissionContext(mode=mode)
            engine = PermissionEngine(context)
            for rule in rules:
                engine.add_rule(rule)
            if spec.rule_files:
                logger.bind(session_id=self.session_id).info(
                    "使用原生 PermissionEngine + {} 条规则；"
                    "RuleSet.default_behavior 需第 11 讲才能生效，当前回退行为由 mode={} 决定",
                    len(rules),
                    mode.value,
                )

        if engine.context.mode is not mode:
            # 第 11 讲的实现可能自己决定了 mode（例如从 rule_files 推导），以其为准
            logger.bind(session_id=self.session_id).debug(
                "权限模式以第 11 讲引擎为准: {}（Profile 写的是 {}）",
                engine.context.mode.value,
                mode.value,
            )

        self._permission_engine = engine
        self._permission_context = engine.context
        logger.bind(
            session_id=self.session_id,
            mode=engine.context.mode.value,
            allow_rules=sorted(engine.context.allow_rules),
            deny_rules=sorted(engine.context.deny_rules),
            ask_rules=sorted(engine.context.ask_rules),
        ).debug("权限引擎已装配")
        return engine

    async def build_memory(self) -> Any | None:
        """按 ``profile.memory`` 装配记忆后端。

        ``MemorySpec.enabled=False`` 时返回 ``None``（不装配、不 import ReMe）。
        记忆实现走注册表的 ``memory`` 类目（``"reme"`` → 第 15 讲）。

        Returns:
            `Any | None`: 记忆客户端门面，或 ``None``。

        Raises:
            UnknownComponentError: 记忆实现名未登记。
            ComponentNotAvailableError: ``enabled=True`` 但实现尚未交付。
        """
        if self._memory is not None:
            return self._memory
        spec = self.profile.memory
        if not spec.enabled:
            logger.bind(session_id=self.session_id).debug(
                "MemorySpec.enabled=False，跳过记忆装配",
            )
            return None
        factory = self.registry.get("memory", "reme")
        self._memory = await self._invoke(factory, spec, self.context)
        return self._memory

    def _resolve_offloader(self) -> Offloader | None:
        """决定 ``Agent(offloader=...)`` 用什么。

        优先级：显式入参 > 第 10 讲的 ``HarnessOffloader`` > workspace 自身
        （``WorkspaceBase`` 结构上就满足 ``Offloader`` Protocol，
        三个方法见 ``third_party/agentscope/src/agentscope/workspace/_base.py:1004/:1060/:1119``）。

        Returns:
            `Offloader | None`: offloader，或 ``None``。
        """
        if self._offloader_override is not None:
            return self._offloader_override
        try:
            from harness_kit.sandbox.offload import HarnessOffloader

            return HarnessOffloader(workspace=self._workspace)  # type: ignore[call-arg]
        except (ImportError, TypeError):
            pass
        if self._workspace is not None:
            return self._workspace
        return None

    def _build_context_config(self) -> ContextConfig:
        """把 ``ToolsSpec.max_result_chars`` 与 ``ContextBudgetSpec`` 翻译成 ``ContextConfig``。

        两条路进来：

        1. ``ToolsSpec.max_result_chars`` → ``tool_result_limit``（第 2 讲就有的行为，
           保持默认值不变）；
        2. ``self._context_budget_override`` → 触发比 / 保留比 / 缓冲带 /
           压缩工具开关 / 失败降级开关 / 图片上限 6 个标量字段（第 8 讲补遗）。

        第 2 条**刻意用惰性 import**：``Profile`` 的 schema 属于第 2 讲，
        压缩配置属于第 8 讲，在模块顶层 import 会让第 2 讲的文件反向依赖
        第 8 讲。这里与 ``_build_offloader`` 里
        ``from harness_kit.sandbox.offload import HarnessOffloader`` 是同一手法 ——
        只有真传了 override 才会触到第 8 讲的模块。

        Returns:
            `ContextConfig`: 工具结果超过 ``max_result_chars`` 时会被截断
            （原生字段单位是 token，这里按 4 字符/token 换算）；
            override 里的字段覆盖官方默认。
        """
        tokens = max(1, self.profile.tools.max_result_chars // _CHARS_PER_TOKEN)
        logger.bind(
            session_id=self.session_id,
            max_result_chars=self.profile.tools.max_result_chars,
            tool_result_limit_tokens=tokens,
        ).debug("tool_result_limit 换算完成")

        if self._context_budget_override is None:
            return ContextConfig(tool_result_limit=tokens)

        from harness_kit.middleware.compact import build_context_config

        config = build_context_config(
            self._context_budget_override,
            tool_result_limit_tokens=tokens,
        )
        logger.bind(
            session_id=self.session_id,
            trigger_ratio=config.trigger_ratio,
            reserve_ratio=config.reserve_ratio,
            compression_tool_enabled=config.compression_tool_enabled,
        ).debug("ContextBudgetSpec 生效")
        return config

    async def build_agent(self) -> Agent:
        """装配最终的 AgentScope ``Agent``。

        依赖顺序：workspace → permission context → model → toolkit → middlewares。
        每一步都会复用缓存，因此本方法可以单独调用。

        Returns:
            `Agent`: AgentScope 原生 Agent（我们**不重写**它的 Agent Loop）。

        Raises:
            ValueError: ``AgentSpec.max_iters`` 非正。
            UnknownComponentError: 依赖的组件未登记。
        """
        if self._agent is not None:
            return self._agent

        spec = self.profile.agent
        if spec.max_iters <= 0:
            raise ValueError(f"AgentSpec.max_iters 必须为正数，收到 {spec.max_iters}")

        await self.build_workspace()
        engine = await self.build_permission_engine()
        model = await self.build_model()
        toolkit = await self.build_toolkit()
        middlewares = await self.build_middlewares()
        memory = await self.build_memory()

        # 关键：把配置好的 PermissionContext 塞进 AgentState，
        # Agent 内部的 PermissionEngine 才会用上它（agent/_agent.py:193）
        permission_context = (
            engine.context if engine is not None else PermissionContext()
        )
        state = AgentState(
            session_id=self.session_id,
            permission_context=permission_context,
        )

        if spec.parallel_tool_calls:
            parameters_cls = getattr(model, "Parameters", None)
            if parameters_cls is not None:
                supported = set(parameters_cls.model_fields)
                if "parallel_tool_calls" in supported:
                    model.parameters.parallel_tool_calls = True
                else:
                    logger.bind(
                        session_id=self.session_id,
                        model=type(model).__name__,
                    ).debug(
                        "AgentSpec.parallel_tool_calls 无法生效：{} 的参数类没有该字段"
                        "（只有 OpenAIChatModel / DashScopeChatModel 有）",
                        type(model).__name__,
                    )

        agent = Agent(
            name=spec.name,
            system_prompt=spec.sys_prompt,
            model=model,
            toolkit=toolkit,
            middlewares=middlewares,
            state=state,
            offloader=self._resolve_offloader(),
            context_config=self._build_context_config(),
            react_config=ReActConfig(max_iters=spec.max_iters),
        )

        self._state = state
        self._agent = agent
        logger.bind(
            session_id=self.session_id,
            agent=spec.name,
            max_iters=spec.max_iters,
            mode=permission_context.mode.value,
            has_memory=memory is not None,
        ).info("Agent 装配完成")
        return agent

    async def build_all(self) -> BuiltHarness:
        """一次装好全部对象。

        Returns:
            `BuiltHarness`: 装配产物；可直接 ``await built.agent.reply(...)``。
        """
        workspace = await self.build_workspace()
        model = await self.build_model()
        toolkit = await self.build_toolkit()
        middlewares = await self.build_middlewares()
        engine = await self.build_permission_engine()
        memory = await self.build_memory()
        agent = await self.build_agent()

        return BuiltHarness(
            profile=self.profile,
            model=model,
            toolkit=toolkit,
            middlewares=middlewares,
            workspace=workspace,
            permission_engine=engine,
            memory=memory,
            agent=agent,
            session_id=self.session_id,
        )


async def build_from_profile(
    profile: ResolvedProfile,
    *,
    settings: Settings,
    registry: HarnessRegistry | None = None,
    session_id: str | None = None,
) -> BuiltHarness:
    """便捷函数：一行装好一个 Profile。

    Args:
        profile (`ResolvedProfile`): 已解析冻结的 Profile。
        settings (`Settings`): 全局设置。
        registry (`HarnessRegistry | None`): 组件注册表。
        session_id (`str | None`): 会话 id。

    Returns:
        `BuiltHarness`: 装配产物。
    """
    builder = HarnessBuilder(
        profile,
        settings=settings,
        registry=registry,
        session_id=session_id,
    )
    return await builder.build_all()


def summarize_agent(agent: Agent) -> dict[str, Any]:
    """把一个装好的 Agent 总结成可打印的字典（``harness-kit doctor`` 用）。

    Args:
        agent (`Agent`): AgentScope Agent。

    Returns:
        `dict[str, Any]`: 名称、模型、工具名、中间件、权限模式、上下文长度。
    """
    toolkit: Toolkit = agent.toolkit
    tools: Sequence[ToolBase] = [
        tool for group in toolkit.tool_groups for tool in group.tools
    ]
    return {
        "name": agent.name,
        "model": type(agent.model).__name__,
        "model_name": getattr(agent.model, "model", None),
        "stream": getattr(agent.model, "stream", None),
        "tools": sorted(tool.name for tool in tools),
        "basic_tools": sorted(tool.name for tool in toolkit.tool_groups[0].tools),
        "tool_groups": [group.name for group in toolkit.tool_groups],
        "context_len": len(agent.state.context),
        "session_id": agent.state.session_id,
        "permission_mode": agent.state.permission_context.mode.value,
        "max_iters": agent.react_config.max_iters,
        "has_offloader": agent.offloader is not None,
    }
