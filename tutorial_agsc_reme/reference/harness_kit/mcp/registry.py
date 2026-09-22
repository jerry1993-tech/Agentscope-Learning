# -*- coding: utf-8 -*-
"""MCP 服务器配置与连接管理（契约 §3.7，第 7 讲）。

AgentScope 已经给了完整的 MCP 客户端实现：

- ``agentscope.mcp.MCPClient``（``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:33``）
  —— 一个 pydantic ``BaseModel``，字段 ``name`` / ``is_stateful`` / ``mcp_config``，
  方法 ``connect()`` / ``close()`` / ``list_raw_tools()`` / ``list_tools()`` / ``get_tool()``；
- ``StdioMCPConfig``（``.../mcp/_config.py:9``）与 ``HttpMCPConfig``（``.../mcp/_config.py:44``）
  —— 两种 transport 的配置模型，``HttpMCPConfig`` 同时覆盖 SSE 与 streamable-http。

harness_kit 要补的是**声明式装配**这一层：Profile 里写的是 YAML，
需要有人把"transport: stdio + command: python + args: [...]"翻译成
``StdioMCPConfig``，再把若干 server 汇总成 ``list[MCPClient]``。
这一层的真实价值在**前置校验**上 —— AgentScope 的校验全部发生在构造
``MCPClient`` 的那一刻，而配置错误（写错 transport、SSE 的 url 不以 ``/sse`` 结尾、
stdio 没给 command）在生产里表现为"Agent 莫名其妙少了一组工具"。
本模块把这些错误提前到装配阶段，并给出可操作的报错。

四个已经实测过的坑，全部在这里被挡住：

1. ``MCPClient.name`` 必须匹配 ``^[a-zA-Z0-9_-]+$``
   （``.../mcp/_mcp_client.py:148``），否则构造即 ``ValueError``；
2. **STDIO 必须 stateful**（同文件 ``:156``），``is_stateful=False`` 直接 ``ValueError``；
3. SSE 与 streamable-http 共用 ``HttpMCPConfig``，靠 **url 路径** 区分：
   ``path.endswith("/sse") or path.endswith("/messages/")`` 才走 SSE
   （同文件 ``:229``），所以写错路径会静默走错 transport；
4. transport 的 context manager **一次性**（同文件 ``:340`` 的注释），
   ``connect()`` 会重建，``close()`` 后不能再 ``connect()`` 同一个对象 ——
   要重连请重新 ``to_clients()``。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from harness_kit.config.schema import MCPSpec

__all__ = [
    "MCPServerRegistry",
    "MCPServerSpec",
    "TRANSPORTS",
    "build_mcp_clients",
    "close_mcp_clients",
    "connect_mcp_clients",
    "namespace_of",
    "namespaced_tool_name",
    "sanitize_tool_name",
]

TRANSPORTS: tuple[str, ...] = ("stdio", "sse", "streamable_http")
"""契约 §3.7 规定的三种 transport。注意 YAML 里写 ``streamable_http``（下划线），
而 MCP SDK 的 ``run(transport=...)`` 参数写 ``streamable-http``（连字符）。"""

_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]+$")
"""``MCPClient.name`` 的约束（``.../mcp/_mcp_client.py:148``）。"""

_TOOL_NAME_SANITIZER: re.Pattern[str] = re.compile(r"[^a-zA-Z0-9_-]")
"""MCP server 返回的工具名里可能有点号、冒号等，会被 ``MCPTool`` 换成 ``x``
（``.../tool/_adapters.py:246``）。这里用同一条规则，保证
:func:`namespaced_tool_name` 与模型实际看到的名字一致。"""

NAMESPACE_PREFIX: str = "mcp"
"""模型侧工具名的固定前缀。``MCPTool`` 生成的名字是
``mcp__<server>__<tool>``（``.../tool/_adapters.py:246``）。"""


def sanitize_tool_name(tool_name: str) -> str:
    """把远端工具名净化成 LLM 提供方接受的形式。

    与 ``MCPTool`` 内部用的规则一致：非法字符替换成 ``x`` 而不是 ``_``，
    以免与 ``mcp__server__tool`` 里的 ``__`` 分隔符混淆
    （``third_party/agentscope/src/agentscope/tool/_adapters.py:240-246``）。

    Args:
        tool_name (`str`): 远端原始工具名。

    Returns:
        `str`: 只含 ``[a-zA-Z0-9_-]`` 的工具名。
    """
    return _TOOL_NAME_SANITIZER.sub("x", tool_name)


def namespace_of(name: str) -> str:
    """把任意字符串净化成合法的 MCP 命名空间。

    Args:
        name (`str`): 原始名字（通常是 server 名）。

    Returns:
        `str`: 只含 ``[a-zA-Z0-9_-]`` 的命名空间；原串被清空时返回 ``"mcp"``。
    """
    cleaned = _TOOL_NAME_SANITIZER.sub("-", name)
    return cleaned or NAMESPACE_PREFIX


def namespaced_tool_name(namespace: str, tool_name: str) -> str:
    """算出模型看到的完整工具名。

    Args:
        namespace (`str`): 命名空间（server 的 ``namespace`` 或 ``name``）。
        tool_name (`str`): MCP server 上的原始工具名。

    Returns:
        `str`: 形如 ``mcp__filesystem__read_file``。
    """
    return f"{NAMESPACE_PREFIX}__{namespace_of(namespace)}__{sanitize_tool_name(tool_name)}"


class MCPServerSpec(BaseModel):
    """一个 MCP server 的声明（契约 §3.7）。

    契约里只有 8 个字段；后面 4 个是 harness_kit 为生产场景追加的，
    全部有默认值，因此既有配置不受影响。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """server 名。会成为命名空间的一部分，因此必须是 ``[a-zA-Z0-9_-]``。"""

    transport: Literal["stdio", "sse", "streamable_http"]
    """传输方式。"""

    command: str | None = None
    """``stdio`` 必填：启动 server 的可执行文件（建议写 ``python`` 的绝对路径）。"""

    args: list[str] = Field(default_factory=list)
    """``stdio`` 的启动参数。"""

    url: str | None = None
    """``sse`` / ``streamable_http`` 必填。"""

    env: dict[str, str] = Field(default_factory=dict)
    """传给子进程的环境变量（``stdio``）。**注意**：这是全量替换而不是追加，
    AgentScope 会把它原样交给 ``StdioServerParameters(env=...)``
    （``.../mcp/_mcp_client.py:198``），因此需要 ``PATH`` 才能找到命令。"""

    enabled: bool = True
    """``False`` 时 ``MCPServerRegistry.to_clients()`` 会跳过它（契约 §3.7）。"""

    namespace: str | None = None
    """工具名前缀，默认取 ``name``。多个 server 工具重名时用它区分。"""

    # ---- harness_kit 追加 ----

    cwd: str | None = None
    """``stdio`` 子进程的工作目录。"""

    is_stateful: bool | None = None
    """是否长连接。``None``（默认）时：stdio 强制 ``True``、HTTP 用 ``False``。
    显式指定会覆盖默认值，但仍要满足"stdio 必须 stateful"。"""

    enable_tools: list[str] | None = None
    """白名单：只暴露这些工具（``MCPClient.enable_tools``）。"""

    disable_tools: list[str] | None = None
    """黑名单：屏蔽这些工具（``MCPClient.disable_tools``）。"""

    execution_timeout: float | None = None
    """单次工具调用超时（秒）。"""

    headers: dict[str, str] = Field(default_factory=dict)
    """``sse`` / ``streamable_http`` 的额外请求头（例如 ``Authorization``）。"""

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_transport(self) -> "MCPServerSpec":
        """校验 transport 与其它字段是否自洽。

        Returns:
            `MCPServerSpec`: 自身。

        Raises:
            ValueError: 字段组合非法。
        """
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"MCP server 名 {self.name!r} 含非法字符；"
                f"必须匹配 {_NAME_RE.pattern}（LLM 提供方对工具名的硬约束）",
            )

        if self.enable_tools and self.disable_tools:
            overlap = set(self.enable_tools) & set(self.disable_tools)
            if overlap:
                raise ValueError(
                    f"{self.name}: enable_tools 与 disable_tools 不能重叠，"
                    f"重叠项 {sorted(overlap)}",
                )

        if self.transport == "stdio":
            if not self.command:
                raise ValueError(
                    f"{self.name}: transport=stdio 必须提供 command",
                )
            if self.url:
                raise ValueError(
                    f"{self.name}: transport=stdio 不应提供 url",
                )
        else:
            if not self.url:
                raise ValueError(
                    f"{self.name}: transport={self.transport} 必须提供 url",
                )
            if self.command:
                raise ValueError(
                    f"{self.name}: transport={self.transport} 不应提供 command",
                )
            path = urlsplit(self.url).path
            is_sse_url = path.endswith("/sse") or path.endswith("/messages/")
            if self.transport == "sse" and not is_sse_url:
                raise ValueError(
                    f"{self.name}: transport=sse 要求 url 路径以 /sse 结尾，"
                    f"收到 {path!r}。AgentScope 是按 url 路径判断 SSE 的"
                    "（third_party/agentscope/src/agentscope/mcp/_mcp_client.py:229），"
                    "路径写错会静默走成 streamable-http",
                )
            if self.transport == "streamable_http" and is_sse_url:
                raise ValueError(
                    f"{self.name}: transport=streamable_http 的 url 路径不能以 "
                    f"/sse 或 /messages/ 结尾（收到 {path!r}），"
                    "否则会被 AgentScope 路由到 SSE 传输",
                )

        if self.transport == "stdio" and self.is_stateful is False:
            raise ValueError(
                f"{self.name}: STDIO MCP 必须 stateful"
                "（third_party/agentscope/src/agentscope/mcp/_mcp_client.py:156）",
            )
        return self

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def resolved_namespace(self) -> str:
        """实际生效的命名空间（``namespace`` 为空时取 ``name``）。

        Returns:
            `str`: 净化后的命名空间。
        """
        return namespace_of(self.namespace or self.name)

    @property
    def stateful(self) -> bool:
        """解析后的 ``is_stateful``。

        stdio 恒为 ``True``（AgentScope 的硬约束）；HTTP 默认 ``False``
        —— 无状态的 streamable-http 不需要 ``connect()``，每次调用临时建会话
        （``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:415``）。

        Returns:
            `bool`: 是否长连接。
        """
        if self.transport == "stdio":
            return True
        return bool(self.is_stateful)

    def namespaced_name(self, tool_name: str) -> str:
        """算出某个远端工具在这个 server 下的模型侧名字。

        Args:
            tool_name (`str`): MCP server 上的原始工具名。

        Returns:
            `str`: 形如 ``mcp__filesystem__read_file``。
        """
        return namespaced_tool_name(self.resolved_namespace, tool_name)

    def check_launch_targets(self) -> None:
        """stdio 声明里的"看起来是文件路径"的启动目标必须存在。

        这是**在把配置交给 SDK 之前**的最后一道前置校验，理由是实测出来的
        一个真坑：``command`` 或脚本路径打错时，``MCPClient.connect()`` 会走到
        自己的失败清理路径，而那条路径用 ``asyncio.shield(stack.aclose())``
        （``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:359-372``）。
        ``shield`` 把清理放进**另一个 task**，anyio 的 cancel scope 于是只能报
        ``Attempted to exit cancel scope in a different task than it was entered
        in``，**原始异常被顶掉**，调用方拿到的是一个莫名其妙的
        ``CancelledError``（``CancelledError`` 继承 ``BaseException``，
        ``except Exception`` 抓不到它，见 :func:`connect_mcp_clients`）。
        提前判断路径存在性，可以让这个最常见的原因以一句可操作的中文报错出现。

        判据刻意保守，避免误报：

        - ``command`` 含路径分隔符（绝对路径/相对路径）时，要求它存在；
          裸命令名（``"python"``）交给 ``PATH`` 查找，不检查；
        - ``args`` 里**既含路径分隔符又以 ``.py`` 结尾**的项，要求它存在
          —— 这样 ``["--output", "report.py"]`` 这类"看起来像路径的值"不会误伤。

        Raises:
            FileNotFoundError: 有启动目标不存在。
        """
        if self.transport != "stdio":
            return
        candidates: list[str] = []
        command = self.command or ""
        if os.sep in command:
            candidates.append(command)
        for arg in self.args:
            if arg.endswith(".py") and os.sep in arg:
                candidates.append(arg)

        missing = [path for path in candidates if not Path(path).exists()]
        if missing:
            raise FileNotFoundError(
                f"{self.name}: stdio 启动目标不存在 {missing}；"
                f"command={self.command!r} args={self.args!r}。"
                "这类配置错误如果留给 AgentScope 处理，会被它的 shield 清理顶成"
                "CancelledError，排查成本极高（见本方法 docstring）",
            )

    def to_client(self) -> MCPClient:
        """构造 ``MCPClient``（**未连接**）。

        Returns:
            `MCPClient`: AgentScope 的原生客户端对象。

        Raises:
            FileNotFoundError: stdio 的启动目标（命令或脚本）不存在。
            ValueError: 字段组合非法（此时 pydantic 已在校验阶段拦下，属兜底）。
        """
        self.check_launch_targets()
        if self.transport == "stdio":
            config: StdioMCPConfig | HttpMCPConfig = StdioMCPConfig(
                command=self.command or "",
                args=self.args or None,
                env=self.env or None,
                cwd=self.cwd,
            )
        else:
            config = HttpMCPConfig(
                url=self.url or "",
                headers=self.headers or None,
            )

        return MCPClient(
            name=self.resolved_namespace,
            is_stateful=self.stateful,
            mcp_config=config,
            enable_tools=self.enable_tools,
            disable_tools=self.disable_tools,
            execution_timeout=self.execution_timeout,
        )

    def describe(self) -> str:
        """返回一行可读摘要，用于日志与 CLI 展示。

        Returns:
            `str`: 形如 ``filesystem[stdio] mcp__filesystem__* (stateful)``。
        """
        target = self.command if self.transport == "stdio" else self.url
        return (
            f"{self.name}[{self.transport}] -> {target} "
            f"namespace={self.resolved_namespace} "
            f"{'stateful' if self.stateful else 'stateless'}"
        )


class MCPServerRegistry:
    """MCP server 声明的登记表（契约 §3.7）。

    只负责"配置 → 客户端"的翻译与生命周期管理，**不缓存连接**：
    :meth:`to_clients` 每次都返回全新对象，因为 transport 的 context manager
    是一次性的（``.../mcp/_mcp_client.py:340``），复用同一个 ``MCPClient``
    在 ``close()`` 之后无法重连。

    Example:
        >>> registry = MCPServerRegistry()                    # doctest: +SKIP
        >>> registry.add(MCPServerSpec(name="fs", transport="stdio",
        ...                            command="python", args=["server.py"]))
        >>> clients = registry.to_clients()
        >>> await connect_mcp_clients(clients)
    """

    def __init__(self, specs: list[MCPServerSpec] | None = None) -> None:
        """构造登记表。

        Args:
            specs (`list[MCPServerSpec] | None`): 初始声明列表。

        Raises:
            ValueError: 初始列表里有重名。
        """
        self._specs: dict[str, MCPServerSpec] = {}
        for spec in specs or []:
            self.add(spec)

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def add(self, spec: MCPServerSpec) -> None:
        """登记一个 server 声明；同名覆盖。

        Args:
            spec (`MCPServerSpec`): server 声明。
        """
        if spec.name in self._specs:
            logger.warning(
                "MCP server {} 被重复登记，后者覆盖前者（{} -> {}）",
                spec.name,
                self._specs[spec.name].describe(),
                spec.describe(),
            )
        self._specs[spec.name] = spec

    def extend(self, specs: list[MCPServerSpec]) -> None:
        """批量登记。

        Args:
            specs (`list[MCPServerSpec]`): server 声明列表。
        """
        for spec in specs:
            self.add(spec)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def specs(self) -> list[MCPServerSpec]:
        """全部声明（含 ``enabled=False`` 的）。

        Returns:
            `list[MCPServerSpec]`: 按登记顺序排列。
        """
        return list(self._specs.values())

    @property
    def enabled_specs(self) -> list[MCPServerSpec]:
        """只含 ``enabled=True`` 的声明。

        Returns:
            `list[MCPServerSpec]`: 按登记顺序排列。
        """
        return [spec for spec in self._specs.values() if spec.enabled]

    def get(self, name: str) -> MCPServerSpec:
        """按名取声明。

        Args:
            name (`str`): server 名。

        Returns:
            `MCPServerSpec`: 声明。

        Raises:
            KeyError: 未登记。
        """
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(
                f"MCP server {name!r} 未登记；已登记: {sorted(self._specs)}",
            ) from exc

    def namespaced_name(self, spec: MCPServerSpec | str, tool_name: str) -> str:
        """算出模型侧工具名（契约 §3.7）。

        Args:
            spec (`MCPServerSpec | str`): 声明对象或 server 名。
            tool_name (`str`): 远端工具名。

        Returns:
            `str`: 形如 ``mcp__filesystem__read_file``。
        """
        resolved = self.get(spec) if isinstance(spec, str) else spec
        return resolved.namespaced_name(tool_name)

    def __len__(self) -> int:
        """登记数量（含被禁用的）。

        Returns:
            `int`: 数量。
        """
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        """是否登记过某个名字。

        同时接受 ``str``（server 名）与 :class:`MCPServerSpec`（按 spec.name 查）——
        后者是写验证脚本时最自然的写法（``spec in registry``），
        而 ``MCPServerSpec`` 是 pydantic 模型、**不可哈希**，直接丢给 dict
        只会得到 ``TypeError: unhashable type`` 这种看不懂的报错。

        Args:
            name (`object`): server 名或 :class:`MCPServerSpec`。

        Returns:
            `bool`: 是否登记。
        """
        key = name.name if isinstance(name, MCPServerSpec) else name
        return key in self._specs

    # ------------------------------------------------------------------
    # 转换
    # ------------------------------------------------------------------
    def to_clients(self) -> list[MCPClient]:
        """把 **enabled** 的声明转成 ``MCPClient`` 列表（契约 §3.7）。

        Returns:
            `list[MCPClient]`: 未连接的客户端。

        Raises:
            ValueError: 某个声明的字段组合非法。
        """
        clients: list[MCPClient] = []
        for spec in self.enabled_specs:
            client = spec.to_client()
            clients.append(client)
            logger.bind(server=spec.name).debug("MCP 客户端已构造: {}", spec.describe())
        disabled = [s.name for s in self.specs if not s.enabled]
        if disabled:
            logger.info("MCP server 已禁用，跳过: {}", disabled)
        return clients

    def describe_all(self) -> list[str]:
        """返回每个声明的单行摘要。

        Returns:
            `list[str]`: 摘要列表。
        """
        return [
            f"{'[on] ' if spec.enabled else '[off]'} {spec.describe()}"
            for spec in self.specs
        ]


async def connect_mcp_clients(
    clients: list[MCPClient],
    *,
    timeout_s: float | None = None,
    sequential: bool = True,
) -> None:
    """连接一批客户端；任何一路失败都会先把**已经连上的**关掉再抛。

    两个实测出来的注意点：

1. **不要用 ``asyncio.wait_for`` 包 ``client.connect()``**。stdio 传输底层是
   anyio 的 ``stdio_client()``，而 anyio 的 cancel scope 要求"进入与退出在
   同一个 task"；``asyncio.wait_for`` 会把协程包进一个新的 Task，
   于是超时/异常路径上必然报
   ``RuntimeError: Attempted to exit cancel scope in a different task than
   it was entered in``（本模块的联调中真实踩到过）。所以 ``timeout_s``
   默认 ``None``，真需要超时请用 ``asyncio.timeout()`` 包**整个**
   :func:`connect_mcp_clients` 调用，而不是包单个客户端。

2. **默认串行连接**（``sequential=True``）。``asyncio.gather`` 会让多个
   ``stdio_client`` 在同一批 task 里并发建 cancel scope，一旦有一个失败，
   回滚时的 scope 嵌套很容易错乱。MCP server 的启动开销在毫秒级，
   串行完全够用；确实要并发时显式传 ``sequential=False``。

    Args:
        clients (`list[MCPClient]`): ``MCPServerRegistry.to_clients()`` 的产物。
        timeout_s (`float | None`): 已废弃的兼容参数；非 ``None`` 时会在每个
            client 上打一条 warning（行为不变，见上面第 1 点）。
        sequential (`bool`): 是否串行连接，默认 ``True``。

    3. **回滚必须抓 ``BaseException``**。已连接的客户端是活着的子进程，
       任何提前退出路径都必须把它们关掉。这里抓 ``BaseException`` 而不是
       ``Exception``，是因为 AgentScope 在 stdio server 启动失败时会抛出
       ``CancelledError``（继承 ``BaseException``，见
       ``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:359-372`` 的
       ``asyncio.shield`` 清理），``except Exception`` 漏掉它 → 已连上的
       server 全部泄漏成孤儿进程。

    Args:
        clients (`list[MCPClient]`): ``MCPServerRegistry.to_clients()`` 的产物。
        timeout_s (`float | None`): 已废弃的兼容参数；非 ``None`` 时会在每个
            client 上打一条 warning（行为不变，见上面第 1 点）。
        sequential (`bool`): 是否串行连接，默认 ``True``。

    Raises:
        Exception: 任一 server 连接失败时原样抛出（``CancelledError`` 除外）。
        asyncio.CancelledError: server 进程起得来但握手失败时抛的就是它
            （AgentScope 的 ``shield`` 清理产物）。**原样透传**，不做包装 ——
            把它换成 ``RuntimeError`` 会破坏 ``asyncio.timeout()`` /
            ``Task.cancel()`` 的语义。要区分"真的被取消"与"server 起不来"，
            看 :meth:`MCPServerSpec.check_launch_targets` 是否已经通过了。
    """
    if not clients:
        return
    if timeout_s is not None:
        logger.warning(
            "connect_mcp_clients(timeout_s=...) 已废弃：asyncio.wait_for 会破坏 "
            "anyio 的 cancel scope。请在外面用 asyncio.timeout() 包住整个调用",
        )

    connected: list[MCPClient] = []

    async def _connect(client: MCPClient) -> None:
        """连接单个客户端。

        Args:
            client (`MCPClient`): 目标客户端。

        Raises:
            Exception: 连接失败（``CancelledError`` 原样透传）。
        """
        try:
            await client.connect()
        except asyncio.CancelledError:
            logger.error(
                "MCP server {!r} 握手被 CancelledError 打断；stdio server "
                "启动失败时 AgentScope 的 shield 清理会产出这个伪取消"
                "（third_party/agentscope/src/agentscope/mcp/_mcp_client.py:359）",
                client.name,
            )
            raise
        except Exception as exc:
            raise RuntimeError(
                f"MCP server {client.name!r} 连接失败: {type(exc).__name__}: {exc}",
            ) from exc
        connected.append(client)

    try:
        if sequential:
            for client in clients:
                await _connect(client)
        else:
            await asyncio.gather(*(_connect(client) for client in clients))
    except BaseException as exc:
        logger.error("MCP 连接失败（{}），回滚已连接的 {} 个", exc, len(connected))
        await close_mcp_clients(connected)
        raise

    logger.info("MCP 已连接 {} 个 server: {}", len(clients), [c.name for c in clients])


async def close_mcp_clients(clients: list[MCPClient]) -> None:
    """尽力关闭一批客户端，绝不抛异常。

    Args:
        clients (`list[MCPClient]`): 待关闭的客户端。
    """
    for client in clients:
        if not getattr(client, "is_connected", False):
            continue
        try:
            await client.close(ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - 关闭阶段不允许阻断
            logger.warning("关闭 MCP {} 失败: {}", client.name, exc)


async def build_mcp_clients(
    spec: "MCPSpec",
    ctx: Any = None,
) -> list[MCPClient]:
    """按 ``MCPSpec`` 装配并连接 MCP 客户端（``HarnessRegistry`` 的工厂入口）。

    这个名字被 ``harness_kit/registry.py`` 的 ``register_lazy("mcp", "spec_registry",
    attrs=("build_mcp_clients", "MCPServerRegistry"))`` 引用
    （见 ``tutorial_agsc_reme/reference/harness_kit/registry.py:1057``），
    所以签名必须是"吃 spec 的工厂"。``ctx`` 参数是可选的装配上下文，
    ``HarnessBuilder._invoke`` 探测到形参里出现 ``ctx`` 就会传进来。

    **连接在工厂内完成**：``Toolkit(mcps=[...])`` 拿到的必须是已连接的客户端，
    Agent 的第一次 tool call 才不会因为 session 未初始化而失败。

    Args:
        spec (`MCPSpec`): Profile 里的 MCP 声明。
        ctx (`Any`): 装配上下文（本工厂不需要，保留以兼容 builder 的探测）。

    Returns:
        `list[MCPClient]`: 已连接的客户端；``servers`` 为空时返回空列表。

    Raises:
        ValueError: 某个 server 声明非法。
        Exception: 连接失败（已连接的会被回滚关闭）。
    """
    del ctx
    registry = MCPServerRegistry(list(spec.servers))
    if not len(registry):
        return []

    disabled = [s.name for s in registry.specs if not s.enabled]
    logger.bind(
        servers=[s.name for s in registry.enabled_specs],
        disabled=disabled,
        group=getattr(spec, "group", "mcp"),
    ).info("装配 MCP：启用 {} 个，禁用 {} 个", len(registry.enabled_specs), len(disabled))

    clients = registry.to_clients()
    await connect_mcp_clients(clients)
    return clients
