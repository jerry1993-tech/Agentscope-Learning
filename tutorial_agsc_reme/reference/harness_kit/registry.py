# -*- coding: utf-8 -*-
"""HarnessRegistry —— Layer 0：把"名字 → 工厂"登记起来，供 Profile 按名装配。

AgentScope 与 ReMe 都没有"按名字解析组件"这一层：``create_app``
（``third_party/agentscope/src/agentscope/app/_app.py:78``）是硬编码的，
ReMe 的 ``ComponentRegistry``（``third_party/ReMe/reme/components/component_registry.py:14``）
只服务于它自己的 Component / Step。本模块补的正是这一层。

**三条设计纪律**：

1. 注册的是**工厂**（``Callable``），不是实例 —— 一个 Profile 可能被装配多次，
   每次都要拿到全新的、可独立 ``aclose()`` 的对象图。
2. **不重写** AgentScope / ReMe 已有能力：``default()`` 里凡是有原生实现的
   （DeepSeek / OpenAI 模型、Local / Docker workspace、budget 中间件），
   登记的就是原生类本身。
3. **惰性解引用**：harness_kit 自己的组件按讲次分批交付，第 2 讲时
   ``harness_kit/middleware/logging.py`` 还不存在。所以 ``default()`` 用
   :class:`_LazyFactory` 登记"将来会有的东西"，把 import 推迟到
   :meth:`HarnessRegistry.get` 第一次被调用时；此时若模块仍不存在，
   要么退回原生回退实现，要么抛 :class:`ComponentNotAvailableError` 并指出归属讲次。

已知的装配陷阱（实测）：

- ``Toolkit.add_tool`` 是 ``async`` 且未知 group 名会 ``ValueError``
  （``third_party/agentscope/src/agentscope/tool/_toolkit.py:640``）；
- ``"basic"`` 是保留工具组名，构造 ``Toolkit(tools=..., tool_groups=[...])`` 时
  不能在 ``tool_groups`` 里再出现 ``"basic"``（同文件 ``:88``）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from loguru import logger

from harness_kit.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from agentscope.middleware import MiddlewareBase
    from agentscope.model import ChatModelBase
    from agentscope.permission import PermissionEngine
    from agentscope.tool import ToolBase
    from agentscope.workspace import WorkspaceBase

    from harness_kit.config.schema import (
        MCPSpec,
        MemorySpec,
        MiddlewareSpec,
        ModelSpec,
        PermissionSpec,
        ResolvedProfile,
        ToolsSpec,
        WorkspaceSpec,
    )

__all__ = [
    "BuildContext",
    "ComponentKind",
    "ComponentNotAvailableError",
    "HarnessRegistry",
    "RegistryFrozenError",
    "UnknownComponentError",
]

ComponentKind = Literal[
    "model",
    "tool_pack",
    "middleware",
    "workspace",
    "memory",
    "permission",
    "mcp",
]
"""组件类目。前 5 个是契约 §3.2 明列的；``permission`` / ``mcp`` 是 harness_kit
为第 7 / 第 11 讲预留的类目（契约里这两讲的组件没有对应的 register_* 方法）。"""

_MODEL = "model"
_TOOL_PACK = "tool_pack"
_MIDDLEWARE = "middleware"
_WORKSPACE = "workspace"
_MEMORY = "memory"
_PERMISSION = "permission"
_MCP = "mcp"

_ALL_KINDS: tuple[ComponentKind, ...] = (
    _MODEL,
    _TOOL_PACK,
    _MIDDLEWARE,
    _WORKSPACE,
    _MEMORY,
    _PERMISSION,
    _MCP,
)


class UnknownComponentError(KeyError):
    """按名取组件时，类目或名字未登记。"""


class ComponentNotAvailableError(UnknownComponentError):
    """名字登记了，但它归属的模块还没交付（或 import 失败），且没有回退实现。"""


class RegistryFrozenError(RuntimeError):
    """冻结之后仍然尝试注册。"""


@dataclass
class BuildContext:
    """装配上下文：工厂在需要"环境信息"时可以从这里取。

    - ``settings``：全局设置（路径锚点、LLM 凭据都从这里来）；
    - ``profile``：正在被装配的 :class:`ResolvedProfile`（回退工厂读它兜底）；
    - ``workspace``：已经装配好的 workspace（工具包需要 backend / workdir 时用）；
    - ``workdir``：工作区绝对路径。

    之所以让工厂"从上下文取"而不是让 builder 把参数塞进契约规定的单参工厂签名里：
    契约 §3.2 把工厂签名钉成了 ``Callable[[ToolsSpec], Awaitable[list[ToolBase]]]``，
    不能改。builder 因此采用「签名里出现 ``ctx`` 就传，否则只传 spec」的兼容策略
    （见 :meth:`HarnessBuilder._invoke`）。
    """

    settings: Settings
    profile: "ResolvedProfile | None" = None
    workspace: "WorkspaceBase | None" = None
    workdir: Path | None = None
    environ: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """补全 ``workdir`` 与 ``environ``。"""
        if self.workdir is None:
            self.workdir = self.settings.resolve(self.settings.workspace_dir)
        if not self.environ:
            self.environ = self.settings.environ_overlay()

    def require_llm_env(self, env_name: str, *, what: str) -> str:
        """解引用一个"存着环境变量名"的配置项。

        Args:
            env_name (`str`): 环境变量名，例如 ``ModelSpec.api_key_env``。
            what (`str`): 用于报错的描述，例如 ``"API key"``。

        Returns:
            `str`: 环境变量的值。

        Raises:
            ValueError: 变量未定义或为空。
        """
        value = self.environ.get(env_name)
        if not value:
            raise ValueError(
                f"{what} 缺失：环境变量 {env_name} 未定义或为空；"
                f"请在 .env 里补上，或用 Settings(llm_api_key=...) 显式传入",
            )
        return value


@dataclass(frozen=True)
class _LazyFactory:
    """延迟到 :meth:`HarnessRegistry.get` 才解引用的工厂。"""

    module: str
    """目标模块的点号路径。"""

    attrs: tuple[str, ...]
    """候选属性名（按序尝试，命中即用）—— 兼容不同讲次可能采用的命名。"""

    owner: str
    """归属讲次，用于报错时指路。"""

    kind: str
    """组件类目。"""

    name: str
    """组件名。"""

    adapter: Callable[[Any], Callable[..., Any]] | None = None
    """把解引用到的对象规整成契约要求的工厂签名。"""

    fallback: Callable[..., Any] | None = None
    """模块尚不存在时的原生回退实现（None 表示没有回退，直接报错）。"""


class HarnessRegistry:
    """Layer 0：名字 → 工厂 的注册表。

    Example:
        >>> registry = HarnessRegistry.default()
        >>> registry.register_model("echo", my_echo_factory)
        >>> registry.freeze()
        >>> factory = registry.get("model", "deepseek")
    """

    def __init__(self) -> None:
        """构造一个空注册表。"""
        self._registry: dict[str, dict[str, Callable[..., Any]]] = {
            kind: {} for kind in _ALL_KINDS
        }
        self._frozen: bool = False

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(
        self,
        kind: ComponentKind,
        name: str,
        factory: Callable[..., Any],
    ) -> None:
        """通用注册入口。

        Args:
            kind (`ComponentKind`): 组件类目。
            name (`str`): 组件名。
            factory (`Callable[..., Any]`): 工厂。

        Raises:
            RegistryFrozenError: 注册表已冻结。
            ValueError: 类目不存在。
        """
        if self._frozen:
            raise RegistryFrozenError(
                f"注册表已冻结，不能再注册 {kind}:{name}",
            )
        if kind not in self._registry:
            raise ValueError(
                f"未知组件类目 '{kind}'；可用类目: {sorted(self._registry)}",
            )
        existing = self._registry[kind].get(name)
        if existing is not None and not isinstance(existing, _LazyFactory):
            logger.warning(
                "组件 {}:{} 被重复注册，后注册的工厂将覆盖先前的",
                kind,
                name,
            )
        self._registry[kind][name] = factory

    def register_model(
        self,
        name: str,
        factory: Callable[["ModelSpec"], Awaitable["ChatModelBase"]],
    ) -> None:
        """登记模型提供方。

        Args:
            name (`str`): ``ModelSpec.provider`` 的取值。
            factory (`Callable[[ModelSpec], Awaitable[ChatModelBase]]`): 异步工厂。
        """
        self.register(_MODEL, name, factory)

    def register_tool_pack(
        self,
        name: str,
        factory: Callable[["ToolsSpec"], Awaitable[list["ToolBase"]]],
    ) -> None:
        """登记工具包。

        Args:
            name (`str`): ``ToolsSpec.packs`` 里的名字。
            factory (`Callable[[ToolsSpec], Awaitable[list[ToolBase]]]`): 异步工厂。
        """
        self.register(_TOOL_PACK, name, factory)

    def register_middleware(
        self,
        name: str,
        factory: Callable[["MiddlewareSpec"], "MiddlewareBase"],
    ) -> None:
        """登记中间件。

        Args:
            name (`str`): ``MiddlewareSpec.name``。
            factory (`Callable[[MiddlewareSpec], MiddlewareBase]`): 同步工厂。
        """
        self.register(_MIDDLEWARE, name, factory)

    def register_workspace(
        self,
        name: str,
        factory: Callable[["WorkspaceSpec"], Awaitable["WorkspaceBase"]],
    ) -> None:
        """登记工作区后端。

        Args:
            name (`str`): ``WorkspaceSpec.kind``。
            factory (`Callable[[WorkspaceSpec], Awaitable[WorkspaceBase]]`): 异步工厂。
        """
        self.register(_WORKSPACE, name, factory)

    def register_memory(
        self,
        name: str,
        factory: Callable[["MemorySpec"], Awaitable[Any]],
    ) -> None:
        """登记记忆后端。

        Args:
            name (`str`): 记忆实现名（``"reme"``）。
            factory (`Callable[[MemorySpec], Awaitable[Any]]`): 异步工厂。
        """
        self.register(_MEMORY, name, factory)

    def register_permission(
        self,
        name: str,
        factory: Callable[["PermissionSpec"], Awaitable["PermissionEngine"]],
    ) -> None:
        """登记权限引擎后端（契约之外的补充类目，给第 11 讲的 ``HarnessPermissionEngine`` 用）。

        Args:
            name (`str`): 后端名。
            factory (`Callable[[PermissionSpec], Awaitable[PermissionEngine]]`): 异步工厂。
        """
        self.register(_PERMISSION, name, factory)

    def register_mcp(
        self,
        name: str,
        factory: Callable[["MCPSpec"], Awaitable[list[Any]]],
    ) -> None:
        """登记 MCP 装配器（契约之外的补充类目，给第 7 讲的 ``MCPServerRegistry`` 用）。

        Args:
            name (`str`): 装配器名。
            factory (`Callable[[MCPSpec], Awaitable[list[Any]]]`): 返回 MCP 客户端列表。
        """
        self.register(_MCP, name, factory)

    def register_lazy(
        self,
        kind: ComponentKind,
        name: str,
        *,
        module: str,
        attrs: tuple[str, ...],
        owner: str,
        adapter: Callable[[Any], Callable[..., Any]] | None = None,
        fallback: Callable[..., Any] | None = None,
    ) -> None:
        """登记一个"将来才存在"的组件。

        ``adapter`` 为 ``None`` 时，命中属性必须是一个**工厂函数**，签名按类目分别是：

        - ``model``：``async build_xxx(spec: ModelSpec) -> ChatModelBase``
        - ``tool_pack``：``async build_xxx(spec: ToolsSpec) -> list[ToolBase]``
        - ``workspace``：``async build_xxx(spec: WorkspaceSpec) -> WorkspaceBase``
        - ``memory``：``async build_xxx(spec: MemorySpec) -> Any``
        - ``permission``：``async build_xxx(spec: PermissionSpec) -> PermissionEngine``
        - ``mcp``：``async build_xxx(spec: MCPSpec) -> list[MCPClient]``

        （每个工厂都可以多接一个可选的关键字参数 ``ctx: BuildContext``，
        builder 会自动识别并传入。）命中类名而不命中工厂函数会抛
        :class:`ComponentNotAvailableError` 而不是静默地当工厂调用。

        Args:
            kind (`ComponentKind`): 组件类目。
            name (`str`): 组件名。
            module (`str`): 归属模块的点号路径。
            attrs (`tuple[str, ...]`): 候选属性名，按序尝试。
            owner (`str`): 归属讲次描述，报错时指路用。
            adapter (`Callable[[Any], Callable[..., Any]] | None`): 归一化适配器。
            fallback (`Callable[..., Any] | None`): 原生回退实现。
        """
        self.register(
            kind,
            name,
            _LazyFactory(
                module=module,
                attrs=attrs,
                owner=owner,
                kind=kind,
                name=name,
                adapter=adapter,
                fallback=fallback,
            ),
        )

    # ------------------------------------------------------------------
    # 冻结与查询
    # ------------------------------------------------------------------
    def freeze(self) -> None:
        """冻结注册表：之后任何注册都抛 :class:`RegistryFrozenError`。

        冻结不解引用惰性工厂（解引用需要 IO），但会把当前登记的名字快照进日志，
        便于"配置里引用了未登记的名字"这类问题在装配前就被看见。
        """
        self._frozen = True
        logger.debug("HarnessRegistry 已冻结: {}", self.snapshot())

    @property
    def frozen(self) -> bool:
        """注册表是否已冻结。

        Returns:
            `bool`: 冻结状态。
        """
        return self._frozen

    def names(self, kind: ComponentKind) -> list[str]:
        """列出某类目下已登记的名字。

        Args:
            kind (`ComponentKind`): 组件类目。

        Returns:
            `list[str]`: 排序后的名字列表。
        """
        return sorted(self._registry.get(kind, {}))

    def snapshot(self) -> dict[str, list[str]]:
        """返回全部类目的名字快照。

        Returns:
            `dict[str, list[str]]`: ``类目 -> 名字列表``。
        """
        return {kind: self.names(kind) for kind in _ALL_KINDS}

    def get(self, kind: ComponentKind, name: str) -> Callable[..., Any]:
        """取出某类目下的工厂（必要时解引用惰性登记）。

        Args:
            kind (`ComponentKind`): 组件类目。
            name (`str`): 组件名。

        Returns:
            `Callable[..., Any]`: 可以按契约签名直接调用的工厂。

        Raises:
            UnknownComponentError: 类目或名字未登记。
            ComponentNotAvailableError: 名字登记了但模块未交付且无回退。
        """
        if kind not in self._registry:
            raise UnknownComponentError(
                f"未知组件类目 '{kind}'；可用类目: {sorted(self._registry)}",
            )
        bucket = self._registry[kind]
        if name not in bucket:
            raise UnknownComponentError(
                f"类目 '{kind}' 未登记组件 '{name}'；已登记: {sorted(bucket)}",
            )
        entry = bucket[name]
        if isinstance(entry, _LazyFactory):
            entry = self._resolve_lazy(entry)
            # 直接写回字典：解的是一次性 IO，且不能受 frozen 影响
            bucket[name] = entry
        return entry

    def try_get(
        self,
        kind: ComponentKind,
        name: str,
    ) -> Callable[..., Any] | None:
        """``get`` 的容错版本：任何 :class:`UnknownComponentError` 都返回 ``None``。

        装配器用它做"优先用策略实现，退化用原生实现"的探测。

        Args:
            kind (`ComponentKind`): 组件类目。
            name (`str`): 组件名。

        Returns:
            `Callable[..., Any] | None`: 工厂，或 ``None``。
        """
        try:
            return self.get(kind, name)
        except UnknownComponentError:
            return None

    def _resolve_lazy(self, lazy: _LazyFactory) -> Callable[..., Any]:
        """解引用一个惰性登记。

        Args:
            lazy (`_LazyFactory`): 惰性登记项。

        Returns:
            `Callable[..., Any]`: 真实工厂。

        Raises:
            ComponentNotAvailableError: 模块未交付且无回退实现。
        """
        try:
            module = import_module(lazy.module)
        except ImportError as exc:
            module = None
            import_error: str = str(exc)
        else:
            import_error = ""

        if module is not None:
            for attr in lazy.attrs:
                target = getattr(module, attr, None)
                if target is None:
                    continue
                if lazy.adapter is not None:
                    factory = lazy.adapter(target)
                else:
                    factory = _require_factory(target, lazy, attr)
                logger.debug(
                    "惰性组件 {}:{} 解引用 -> {}.{}",
                    lazy.kind,
                    lazy.name,
                    lazy.module,
                    attr,
                )
                return factory

        if lazy.fallback is not None:
            logger.warning(
                "组件 {}:{} 归属 {}，对应模块 {} 尚未交付（{}），"
                "退化为内置回退实现",
                lazy.kind,
                lazy.name,
                lazy.owner,
                lazy.module,
                import_error or "找不到目标属性",
            )
            return lazy.fallback

        raise ComponentNotAvailableError(
            f"组件 {lazy.kind}:{lazy.name} 归属 {lazy.owner}，"
            f"但模块 {lazy.module} 不可用（{import_error or '无非候选属性 ' + str(lazy.attrs)}），"
            "且没有内置回退实现",
        )

    # ------------------------------------------------------------------
    # 内置注册表
    # ------------------------------------------------------------------
    @classmethod
    def default(cls) -> "HarnessRegistry":
        """构造内置注册表：把 harness_kit 全部组件登记上。

        已有原生实现的直接登记原生类；harness_kit 自己的组件按讲次惰性登记。

        Returns:
            `HarnessRegistry`: 未冻结的注册表，调用方可继续追加自己的组件。
        """
        registry = cls()
        _register_models(registry)
        _register_tool_packs(registry)
        _register_middlewares(registry)
        _register_workspaces(registry)
        _register_memories(registry)
        _register_permissions(registry)
        _register_mcps(registry)
        return registry


# ======================================================================
# 内置工厂：模型
# ======================================================================
def _resolve_credential(
    spec: "ModelSpec",
    ctx: BuildContext | None,
) -> tuple[str, str | None]:
    """从 ``ModelSpec`` 的 ``*_env`` 字段解引用出凭据。

    Args:
        spec (`ModelSpec`): 模型声明。
        ctx (`BuildContext | None`): 装配上下文；``None`` 时退回 ``os.environ``。

    Returns:
        `tuple[str, str | None]`: ``(api_key, base_url)``。

    Raises:
        ValueError: 必需的 API key 未定义。
    """
    if ctx is not None:
        api_key = ctx.require_llm_env(spec.api_key_env, what="LLM API key")
        base_url = ctx.environ.get(spec.base_url_env) or None
        return api_key, base_url

    import os

    api_key = os.environ.get(spec.api_key_env)
    if not api_key:
        raise ValueError(f"环境变量 {spec.api_key_env} 未定义或为空")
    return api_key, os.environ.get(spec.base_url_env) or None


def _model_parameters_kwargs(
    spec: "ModelSpec",
    parameters_cls: type,
    *,
    provider: str,
) -> dict[str, Any]:
    """把 :class:`ModelSpec` 的公共字段 + ``extra`` 翻译成模型 Parameters 的 kwargs。

    ``Extra`` 里不认识的键会被丢弃并打 warning（例如把 DeepSeek 的
    ``parallel_tool_calls`` 写给了 DeepSeek 的参数类）。

    Args:
        spec (`ModelSpec`): 模型声明。
        parameters_cls (`type`): 例如 ``DeepSeekChatModel.Parameters``。
        provider (`str`): 提供方名，用于日志。

    Returns:
        `dict[str, Any]`: 可直接 ``Parameters(**kwargs)`` 的字典。
    """
    allowed = set(parameters_cls.model_fields)
    kwargs: dict[str, Any] = {}
    if "max_tokens" in allowed and spec.max_tokens is not None:
        kwargs["max_tokens"] = spec.max_tokens
    if "temperature" in allowed:
        kwargs["temperature"] = spec.temperature
    if "parallel_tool_calls" in allowed:
        kwargs["parallel_tool_calls"] = bool(
            spec.extra.get("parallel_tool_calls", True),
        )
    unknown: list[str] = []
    for key, value in spec.extra.items():
        if key not in allowed:
            unknown.append(key)
            continue
        kwargs[key] = value
    if unknown:
        logger.bind(provider=provider).warning(
            "ModelSpec.extra 里有 {} 的参数类不支持的键，已忽略: {}；"
            "该参数类支持的键: {}",
            provider,
            unknown,
            sorted(allowed),
        )
    return kwargs


async def _build_deepseek_model(
    spec: "ModelSpec",
    ctx: BuildContext | None = None,
) -> "ChatModelBase":
    """按 :class:`ModelSpec` 造一个 ``DeepSeekChatModel``。

    用到的真实 API：
    ``third_party/agentscope/src/agentscope/model/_deepseek/_model.py:26``（类）、``:79``（``__init__``）；
    ``third_party/agentscope/src/agentscope/credential/_deepseek.py:15``（``DeepSeekCredential``）。

    Args:
        spec (`ModelSpec`): 模型声明。
        ctx (`BuildContext | None`): 装配上下文。

    Returns:
        `ChatModelBase`: 形状为 ``DeepSeekChatModel`` 的模型对象。
    """
    from agentscope.credential import DeepSeekCredential
    from agentscope.model import DeepSeekChatModel

    api_key, base_url = _resolve_credential(spec, ctx)
    credential_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        credential_kwargs["base_url"] = base_url
    parameters = DeepSeekChatModel.Parameters(
        **_model_parameters_kwargs(
            spec,
            DeepSeekChatModel.Parameters,
            provider="deepseek",
        ),
    )
    return DeepSeekChatModel(
        credential=DeepSeekCredential(**credential_kwargs),
        model=spec.model_name,
        parameters=parameters,
        stream=spec.stream,
        client_kwargs={"timeout": spec.timeout_s},
    )


async def _build_openai_model(
    spec: "ModelSpec",
    ctx: BuildContext | None = None,
) -> "ChatModelBase":
    """按 :class:`ModelSpec` 造一个 ``OpenAIChatModel``。

    用到的真实 API：
    ``third_party/agentscope/src/agentscope/model/_openai_chat/_model.py:36``（类）、``:111``（``__init__``）；
    ``third_party/agentscope/src/agentscope/credential/_openai.py:16``（``OpenAICredential``）。

    Args:
        spec (`ModelSpec`): 模型声明。
        ctx (`BuildContext | None`): 装配上下文。

    Returns:
        `ChatModelBase`: 形状为 ``OpenAIChatModel`` 的模型对象。
    """
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    api_key, base_url = _resolve_credential(spec, ctx)
    credential_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        credential_kwargs["base_url"] = base_url
    parameters = OpenAIChatModel.Parameters(
        **_model_parameters_kwargs(
            spec,
            OpenAIChatModel.Parameters,
            provider="openai",
        ),
    )
    return OpenAIChatModel(
        credential=OpenAICredential(**credential_kwargs),
        model=spec.model_name,
        parameters=parameters,
        stream=spec.stream,
        client_kwargs={"timeout": spec.timeout_s},
    )


def _spec_class_adapter(target: Any) -> Callable[..., Any]:
    """把"某个类"规整成契约要求的 ``Callable[[MiddlewareSpec], MiddlewareBase]``。

    中间件是最简单的一类：它只吃自己的 ``params``，不需要装配上下文。
    如果目标本身已经是函数（工厂），调用方应直接用 :attr:`_LazyFactory.adapter` 之外的路径，
    也就是把 ``adapter`` 传 ``None``。

    Args:
        target (`Any`): 解引用到的类。

    Returns:
        `Callable[..., Any]`: ``(spec) -> target(**spec.params)``。
    """

    def _factory(spec: Any, ctx: BuildContext | None = None) -> Any:
        del ctx
        params = getattr(spec, "params", None)
        return target(**params) if isinstance(params, dict) else target()

    return _factory


def _require_factory(target: Any, lazy: "_LazyFactory", attr: str) -> Callable[..., Any]:
    """校验解引用到的对象确实是一个"吃 spec 的工厂"。

    ``attrs`` 里同时列了工厂函数名（首选）与类名（兜底）。如果只命中类名，
    说明归属讲次还没补工厂函数 —— 这时候必须**明确报错**，而不是把类当工厂
    调用后抛一个看不懂的 ``TypeError``。

    Args:
        target (`Any`): 解引用到的对象。
        lazy (`_LazyFactory`): 惰性登记项。
        attr (`str`): 命中的属性名。

    Returns:
        `Callable[..., Any]`: 可调用的工厂。

    Raises:
        ComponentNotAvailableError: 目标是个类（没有工厂函数）。
    """
    if inspect.isclass(target):
        raise ComponentNotAvailableError(
            f"组件 {lazy.kind}:{lazy.name} 在 {lazy.module}.{attr} 只找到了类，"
            "没有找到吃 spec 的工厂函数；请在 "
            f"{lazy.module} 里补一个工厂（签名形如 ``build_xxx(spec)``），"
            "或显式调用 registry.register_*() 覆盖本次登记",
        )
    return target


def _register_models(registry: HarnessRegistry) -> None:
    """登记模型提供方。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    registry.register_model("deepseek", _build_deepseek_model)
    registry.register_model("openai", _build_openai_model)
    registry.register_lazy(
        _MODEL,
        "echo",
        module="harness_kit.models.adapters.echo",
        attrs=("build_echo_model", "EchoChatModel"),
        owner="第 4 讲（harness_kit/models/adapters/echo.py）",
    )


# ======================================================================
# 内置工厂：工具包
# ======================================================================
async def _builtin_tool_pack(
    spec: "ToolsSpec",
    ctx: BuildContext | None = None,
) -> list["ToolBase"]:
    """``builtin`` 工具包：AgentScope 原生内置文件工具的组合。

    这里**不重写任何工具**，只是把 AgentScope 自带的
    ``Bash`` / ``Read`` / ``Write`` / ``Edit`` / ``Glob`` / ``Grep`` 组合成一个包，
    并让它们共用同一个 ``LocalBackend``（这样 ``Read`` 的缓存与 ``Bash`` 的沙箱
    行为一致）：

    - ``third_party/agentscope/src/agentscope/tool/_builtin/_bash.py:25``
    - ``.../tool/_builtin/_read.py``（``Read``）、``_write.py``、``_edit.py``、``_glob.py``、``_grep.py``
    - ``.../tool/_builtin/_backend.py`` 里的 ``LocalBackend``
      （在 ``third_party/agentscope/src/agentscope/tool/__init__.py:29`` 导出）

    Args:
        spec (`ToolsSpec`): 工具声明（``max_result_chars`` 由中间件层消费，这里不用）。
        ctx (`BuildContext | None`): 装配上下文，提供 ``workdir``。

    Returns:
        `list[ToolBase]`: 6 个内置工具实例。
    """
    from agentscope.tool import (
        Bash,
        Edit,
        Glob,
        Grep,
        LocalBackend,
        Read,
        Write,
    )

    backend = LocalBackend()
    workdir: str | None = None
    if ctx is not None:
        workdir = str(ctx.workdir) if ctx.workdir is not None else None

    tools: list[ToolBase] = [
        Bash(cwd=workdir, backend=backend),
        Edit(backend=backend),
        Glob(backend=backend),
        Grep(backend=backend),
        Read(backend=backend),
        Write(backend=backend),
    ]
    if spec.max_result_chars <= 0:
        raise ValueError(
            f"ToolsSpec.max_result_chars 必须为正数，收到 {spec.max_result_chars}",
        )
    logger.bind(tools=[tool.name for tool in tools]).debug("builtin 工具包已装配")
    return tools


def _register_tool_packs(registry: HarnessRegistry) -> None:
    """登记工具包。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    registry.register_tool_pack("builtin", _builtin_tool_pack)
    registry.register_lazy(
        _TOOL_PACK,
        "repo",
        module="harness_kit.tools.repo_pack",
        attrs=("build_repo_pack", "RepoToolPack"),
        owner="第 5 讲（harness_kit/tools/repo_pack.py）",
    )


# ======================================================================
# 内置工厂：中间件
# ======================================================================
def _native_budget_middleware(spec: "MiddlewareSpec") -> "MiddlewareBase":
    """``budget`` 的原生回退实现：直接用 AgentScope 的 ``ReplyBudgetControlMiddleware``。

    真实 API：``third_party/agentscope/src/agentscope/middleware/_budget.py:21``，
    ``__init__(token_budget, input_token_weight=1, output_token_weight=1, hint_message=...)``。

    第 8 讲的 ``harness_kit/middleware/budget.py`` 交付后会自动接管这个名字
    （它的 ``params`` 用的是 ``max_prompt_tokens`` / ``max_completion_tokens`` /
    ``max_tool_calls`` / ``on_exceed``：契约 §6.3 的 ``coding.yaml``）。

    Args:
        spec (`MiddlewareSpec`): 中间件声明。

    Returns:
        `MiddlewareBase`: ``ReplyBudgetControlMiddleware`` 实例。
    """
    from agentscope.middleware import ReplyBudgetControlMiddleware

    params = dict(spec.params)
    if "token_budget" in params:
        return ReplyBudgetControlMiddleware(
            token_budget=float(params["token_budget"]),
            input_token_weight=float(params.get("input_token_weight", 1)),
            output_token_weight=float(params.get("output_token_weight", 1)),
        )
    max_prompt = params.get("max_prompt_tokens")
    max_completion = params.get("max_completion_tokens")
    if max_prompt is None and max_completion is None:
        raise ValueError(
            "中间件 'budget' 的原生回退实现需要 token_budget，"
            "或 max_prompt_tokens/max_completion_tokens 之一；"
            f"收到 params={params}",
        )
    total = float(max_prompt or 0) + float(max_completion or 0)
    return ReplyBudgetControlMiddleware(
        token_budget=total,
        input_token_weight=float(params.get("input_token_weight", 1)),
        output_token_weight=float(params.get("output_token_weight", 1)),
    )


def _register_middlewares(registry: HarnessRegistry) -> None:
    """登记中间件。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    for name, class_name in (
        ("logging", "LoggingMiddleware"),
        ("redact", "RedactMiddleware"),
        ("guards", "GuardsMiddleware"),
        # 第 8 讲补遗：短期上下文压缩的观测 + 护栏。挂上它就是往
        # ``Agent(middlewares=[...])`` 里多传一个实现了 ``on_compress_context``
        # 的对象 —— 压缩算法本身仍然是 AgentScope 的 ``_compress_context_impl``。
        ("compact", "ContextCompactionMiddleware"),
    ):
        registry.register_lazy(
            _MIDDLEWARE,
            name,
            module=f"harness_kit.middleware.{name}",
            attrs=(class_name,),
            owner=f"第 8 讲（harness_kit/middleware/{name}.py）",
            adapter=_spec_class_adapter,
        )

    registry.register_lazy(
        _MIDDLEWARE,
        "budget",
        module="harness_kit.middleware.budget",
        attrs=("BudgetMiddleware",),
        owner="第 8 讲（harness_kit/middleware/budget.py）",
        adapter=_spec_class_adapter,
        fallback=_native_budget_middleware,
    )
    registry.register_lazy(
        _MIDDLEWARE,
        "tracing",
        module="harness_kit.middleware.tracing",
        attrs=("TracingMiddleware",),
        owner="第 8 讲（harness_kit/middleware/tracing.py）",
        adapter=_spec_class_adapter,
    )

    # ``reme_memory`` 不套 ``_spec_class_adapter``：``LongTermMemoryMiddleware``
    # 的参数来自 Profile 的 ``memory:`` 块而**不是** ``params`` 里的一个 ``name``
    # 字段，工厂要同时读 ``spec.params`` 与 ``ctx.profile.memory`` 才能把两边
    # 拼起来（见 harness_kit/memory/middleware.py 的 ``build_memory_middleware``）。
    registry.register_lazy(
        _MIDDLEWARE,
        "reme_memory",
        module="harness_kit.memory.middleware",
        attrs=("build_memory_middleware",),
        owner="第 19 讲（harness_kit/memory/middleware.py）",
    )


# ======================================================================
# 内置工厂：工作区
# ======================================================================
async def _build_local_workspace(
    spec: "WorkspaceSpec",
    ctx: BuildContext | None = None,
) -> "WorkspaceBase":
    """``local`` 工作区：直接用 AgentScope 的 ``LocalWorkspace``。

    真实 API：``third_party/agentscope/src/agentscope/workspace/_local_workspace.py:65``（类）、
    ``:77``（``__init__(*, workdir, ...)``）。

    Args:
        spec (`WorkspaceSpec`): 工作区声明。
        ctx (`BuildContext | None`): 装配上下文，用于把 ``root`` 锚定到 repo_root。

    Returns:
        `WorkspaceBase`: 未 ``initialize()`` 的 ``LocalWorkspace``。
    """
    from agentscope.workspace import LocalWorkspace

    root = spec.root
    if ctx is not None:
        resolved = str(ctx.settings.resolve(root))
    else:
        resolved = str(Path(root).expanduser().resolve())
    return LocalWorkspace(workdir=resolved)


async def _build_docker_workspace(
    spec: "WorkspaceSpec",
    ctx: BuildContext | None = None,
) -> "WorkspaceBase":
    """``docker`` 工作区：直接用 AgentScope 的 ``DockerWorkspace``。

    真实 API：``third_party/agentscope/src/agentscope/workspace/_docker/_docker_workspace.py:43``（类）、
    ``:52``（``__init__(*, host_workdir=..., ...)``）。注意参数名是 ``host_workdir``
    而不是 ``workdir``（旧的 ``workdir`` 走 deprecated 分支）。

    这里**不** ``initialize()``：拉起容器是有副作用的慢操作，交给调用方决定时机
    （``async with workspace`` 或显式 ``await workspace.initialize()``）。

    Args:
        spec (`WorkspaceSpec`): 工作区声明。
        ctx (`BuildContext | None`): 装配上下文。

    Returns:
        `WorkspaceBase`: 未 ``initialize()`` 的 ``DockerWorkspace``。
    """
    from agentscope.workspace import DockerWorkspace

    root = spec.root
    host_workdir = (
        str(ctx.settings.resolve(root)) if ctx is not None else str(Path(root).resolve())
    )
    return DockerWorkspace(host_workdir=host_workdir)


def _register_workspaces(registry: HarnessRegistry) -> None:
    """登记工作区后端。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    registry.register_workspace("local", _build_local_workspace)
    registry.register_workspace("docker", _build_docker_workspace)
    registry.register_lazy(
        _WORKSPACE,
        "policy_local",
        module="harness_kit.sandbox.local",
        attrs=("build_policy_local_workspace", "PolicyLocalWorkspace"),
        owner="第 10 讲（harness_kit/sandbox/local.py）",
    )
    registry.register_lazy(
        _WORKSPACE,
        "quota_docker",
        module="harness_kit.sandbox.docker",
        attrs=("build_quota_docker_workspace", "QuotaDockerWorkspace"),
        owner="第 10 讲（harness_kit/sandbox/docker.py）",
    )


# ======================================================================
# 内置工厂：记忆 / 权限 / MCP
# ======================================================================
def _register_memories(registry: HarnessRegistry) -> None:
    """登记记忆后端。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    registry.register_lazy(
        _MEMORY,
        "reme",
        module="harness_kit.memory.client",
        attrs=("build_memory_client", "MemoryClient"),
        owner="第 15 讲（harness_kit/memory/client.py）",
    )


def _register_permissions(registry: HarnessRegistry) -> None:
    """登记权限后端。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    registry.register_lazy(
        _PERMISSION,
        "yaml_ruleset",
        module="harness_kit.permission.policy",
        attrs=("build_permission_engine", "HarnessPermissionEngine"),
        owner="第 11 讲（harness_kit/permission/policy.py）",
    )


def _register_mcps(registry: HarnessRegistry) -> None:
    """登记 MCP 装配器。

    Args:
        registry (`HarnessRegistry`): 目标注册表。
    """
    registry.register_lazy(
        _MCP,
        "spec_registry",
        module="harness_kit.mcp.registry",
        attrs=("build_mcp_clients", "MCPServerRegistry"),
        owner="第 7 讲（harness_kit/mcp/registry.py）",
    )


def accepts_build_context(factory: Callable[..., Any]) -> bool:
    """判断一个工厂是否接受 ``ctx`` 关键字参数。

    builder 用它实现「签名里有 ``ctx`` 就传，否则只传 spec」的兼容策略，
    这样契约 §3.2 规定的单参工厂签名可以原样使用。

    Args:
        factory (`Callable[..., Any]`): 待探测的工厂。

    Returns:
        `bool`: 是否接受 ``ctx``。
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):  # pragma: no cover - 内建函数等
        return False
    return "ctx" in signature.parameters
