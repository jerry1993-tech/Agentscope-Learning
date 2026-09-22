# -*- coding: utf-8 -*-
"""本地工作区 + 策略校验（契约 §3.10，第 10 讲）。

**策略校验必须打在 ``BackendBase`` 边界，而不是 ``WorkspaceBase`` 边界**

这是本模块最重要的一条设计结论，靠读源码得出：

- AgentScope 的 ``WorkspaceBase`` **没有** ``read_file`` / ``write_file`` / ``run_command``
  —— 它的公开面只有生命周期（``initialize`` / ``close`` / ``reset``）与派生能力
  （``list_tools`` / ``list_skills`` / ``offload_*`` / MCP 持久化）。
  见 ``third_party/agentscope/src/agentscope/workspace/_base.py:223-1170`` 的方法清单。
- 真正读写文件的是工具，而工具持有的是 **backend**：
  ``third_party/agentscope/src/agentscope/tool/_builtin/_write.py:240``
  校验 ``self._backend.isabs(file_path)``、
  ``:302`` 调 ``self._backend.write_file(file_path, ...)``；
  ``third_party/agentscope/src/agentscope/tool/_builtin/_bash.py`` 走
  ``self._backend.exec_shell(...)``。
- 而工具的 backend 来自工作区：``third_party/agentscope/src/agentscope/workspace/_local_workspace.py:141``
  ``list_tools()`` 里就是 ``backend = self.get_backend()``。

结论：**把 ``get_backend()`` 换成策略化的 backend**，内置工具、技能落盘、
MCP 持久化、offload 全都会自动过策略 —— 一处改动覆盖全部入口。
反过来，如果只在 ``WorkspaceBase`` 上包一层，内置工具完全绕得过去。

**为什么直接继承 ``LocalWorkspace`` 而不是再包一层**

契约写的是 ``class PolicyLocalWorkspace(WorkspaceBase)``，而 ``LocalWorkspace``
本身就是 ``WorkspaceBase`` 的子类，因此继承 ``LocalWorkspace`` 满足契约的类型约束，
同时避免"包一层"特有的坑：``LocalWorkspace`` 覆盖了 13 个基类方法
（实测：``initialize`` / ``close`` / ``reset`` / ``get_instructions`` / ``list_tools`` /
``list_skills`` / ``add_skill`` / ``add_skill_archive`` / ``remove_skill`` /
``add_mcp`` / ``remove_mcp`` / ``_python_command`` / ``__init__``），
一个只做 ``__getattr__`` 转发的包装类会在这些同名方法上**静默**退回基类实现
（``__getattr__`` 只在正常查找失败时才触发）。继承没有这个问题。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal

from agentscope.tool import ExecResult
from agentscope.tool._builtin._backend import BackendBase, LocalBackend
from agentscope.workspace import LocalWorkspace
from loguru import logger

from harness_kit.sandbox.guard import PathEscapeError, PathGuard
from harness_kit.sandbox.policy import SandboxPolicy

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.workspace import WorkspaceBase

    from harness_kit.registry import BuildContext

_AccessKind = Literal["read", "write"]
"""本模块内部用的访问类型别名（读 / 写）。"""

_PathMode = Literal["host", "container"]
"""路径判定模式：宿主工作区 / 容器内。"""

__all__ = [
    "PolicyBackend",
    "PolicyLocalWorkspace",
    "build_policy_local_workspace",
]


class PolicyBackend(BackendBase):
    """把每一次路径访问先过 :class:`~harness_kit.sandbox.policy.SandboxPolicy`。

    两个模式，由 ``mode`` 参数选（本地用 ``"host"``，
    :mod:`harness_kit.sandbox.docker` 用 ``"container"``）：

    ============ ==================================================================
    ``host``     白名单 = ``workspace_root`` + ``read_paths`` / ``write_paths``；
                 越界即拒。``cwd`` 也必须落在白名单内。
    ``container`` 容器本身就是边界：容器内**任意**路径放行，
                 只有落在容器工作目录下的路径才查 ``deny_paths``
                 （用来保护 bind-mount 回宿主的那部分目录）。
                 这不是偷懒，是实测逼出来的：AgentScope 的容器初始化会调
                 ``backend.exec_shell(["mkdir", "-p", ...], cwd="/")``
                 （``third_party/agentscope/src/agentscope/workspace/_sandboxed_base.py:417-428``），
                 gateway 又装在 ``/root/.agentscope``
                 （``.../_docker/_make_dockerfile.py:40``）—— 都在工作目录之外。
                 若按 host 模式判，容器根本起不来。
    ============ ==================================================================

    超时策略也随模式不同：

    - ``host``：硬压到 ``policy.timeout_s``（``timeout=None`` 是"无限等待"，
      正是 agent 卡死的成因）；
    - ``container``：``timeout=None`` 时取 ``policy.timeout_s``，
      **显式**给的 timeout 原样放行 —— 容器 bootstrap 的
      ``_bootstrap_cmd_timeout`` 是 1800 秒
      （``.../_sandboxed_base.py:67``），硬压会让 pip 安装中途被杀。
    ============ ==================================================================

    装饰器式包装：所有真正的 IO 交给 ``inner``（真实的 ``LocalBackend``），
    本类只负责"先问策略，再放行"以及三个资源约束（超时、输出截断、cwd 越界）。

    **只覆盖需要判定的方法，其余全部转发**：``join_path`` / ``dirname`` /
    ``basename`` / ``isabs`` / ``normpath`` / ``abspath`` 是纯字符串运算
    （``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:189-291``），
    没有副作用，不需要过策略 —— 而且 ``abspath`` 正是工具构造路径时要用的，
    拦它只会在"还没拿到路径"的阶段就报错，属于帮倒忙。

    ``_path_module`` / ``os_name`` 从 ``inner`` 抄，不硬编码：本地后端用
    ``os.path`` + 宿主 ``os.name``（``_backend.py:762-768``），
    抄过来才能让路径语义与实际执行环境一致。
    """

    def __init__(
        self,
        *,
        inner: BackendBase,
        policy: SandboxPolicy,
        workspace_root: Path | str | None = None,
        mode: _PathMode = "host",
    ) -> None:
        """构造策略化后端。

        Args:
            inner (`BackendBase`): 被包装的真实后端（``LocalBackend`` /
                ``DockerBackend`` / ...）。
            policy (`SandboxPolicy`): 生效中的策略。
            workspace_root (`Path | str | None`, optional): 覆盖策略里的根目录。
                给 Docker 用时传容器内的 ``/workspace`` —— 策略里写的是**宿主**路径，
                容器里根本不长这样，不换根就会把容器里的每个路径都判成越界。
                ``None`` 表示直接用 ``policy.workspace_root``。
            mode (`_PathMode`, defaults to ``"host"``): 见类 docstring。

        Raises:
            ValueError: ``mode`` 不是 ``"host"`` / ``"container"``。
        """
        if mode not in ("host", "container"):
            raise ValueError(f"未知的 mode：{mode!r}（只支持 host / container）")
        self._inner: BackendBase = inner
        self.policy: SandboxPolicy = policy
        self.mode: _PathMode = mode
        root = Path(workspace_root) if workspace_root is not None else Path(policy.workspace_root)
        if root != Path(policy.workspace_root):
            # 换根的同时丢掉宿主侧的额外白名单：那些是宿主路径，
            # 在容器里没有意义（容器内的等价物由 bind-mount 决定）。
            self._view: SandboxPolicy = policy.model_copy(
                update={
                    "workspace_root": root,
                    "read_paths": [],
                    "write_paths": [],
                },
            )
        else:
            self._view = policy
        self._guard: PathGuard = PathGuard(root)
        # 实例属性遮蔽类属性：让路径语义跟着 inner 走
        self._path_module = inner._path_module
        self.os_name = inner.os_name

    # ------------------------------------------------------------------
    # 策略判定
    # ------------------------------------------------------------------
    @property
    def workspace_root(self) -> Path:
        """本后端实际使用的根目录（可能是被覆盖过的容器路径）。

        Returns:
            `Path`: 根目录。
        """
        return Path(self._view.workspace_root)

    def absolute(self, path: str) -> str:
        """把可能相对的路径锚到工作区根目录（**不是**进程 cwd）。

        为什么不用 ``os.path.abspath``：那个会按**宿主进程**的 cwd 解析，
        而宿主 cwd 与工作区毫无关系（AgentScope 自己在
        ``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:406``
        的 ``getcwd`` docstring 里也强调过这一点）。

        实测：内置工具的 ``file_path`` 参数**要求绝对路径**
        （``third_party/agentscope/src/agentscope/tool/_builtin/_write.py:240``
        ``if not self._backend.isabs(file_path): ... "file_path must be an absolute path"``），
        所以这条分支主要用于人肉调用与 ``run_command`` 的 ``cwd``。

        Args:
            path (`str`): 原始路径。

        Returns:
            `str`: 绝对路径。
        """
        expanded = os.path.expanduser(path) if self.os_name == "posix" else path
        if self._path_module.isabs(expanded):
            return self._path_module.normpath(expanded)
        return self._path_module.normpath(
            self._path_module.join(str(self.workspace_root), expanded),
        )

    def _explain(self, target: str, kind: _AccessKind) -> str | None:
        """路径被拒时的具体原因（``None`` 表示没被拒）。

        只为**报错信息**服务：判定本身复用
        :meth:`SandboxPolicy.is_readable` / :meth:`is_writable`，
        不重复实现一遍语义（两处实现迟早会分叉）。

        Args:
            target (`str`): 已绝对化的路径。
            kind (`_AccessKind`): ``"read"`` 或 ``"write"``。

        Returns:
            `str | None`: 人类可读的拒绝原因。
        """
        if self.mode == "container":
            return self._explain_container(target, kind)
        allowed = (
            self._view.is_readable(target)
            if kind == "read"
            else self._view.is_writable(target)
        )
        if allowed:
            return None
        hit = self._view.deny_hit(target, kind)
        if hit is not None:
            return f"命中 deny_paths 模式 {hit!r}"
        roots = [str(self.workspace_root)] + [
            str(item) for item in self._view.extra_roots(kind)
        ]
        return f"不在 {kind} 白名单 {roots} 之内（工作区根为 {self.workspace_root}）"

    def _explain_container(self, target: str, kind: _AccessKind) -> str | None:
        """容器模式的拒绝原因（``None`` 表示放行）。

        容器内的工作目录之外一律放行（容器自身就是那层隔离），
        工作目录之内则照常查 ``deny_paths``。

        Args:
            target (`str`): 已绝对化的容器内路径。
            kind (`_AccessKind`): ``"read"`` 或 ``"write"``。

        Returns:
            `str | None`: 拒绝原因，或 ``None``。
        """
        if not self._guard.is_within(target):
            return None
        hit = self._view.deny_hit(target, kind)
        if hit is not None:
            return (
                f"命中 deny_paths 模式 {hit!r}"
                f"（容器工作目录 {self.workspace_root}）"
            )
        return None

    def _enforce(self, path: str, kind: _AccessKind) -> str:
        """判定并放行；被拒则抛 :class:`PathEscapeError`。

        Args:
            path (`str`): 原始路径。
            kind (`_AccessKind`): ``"read"`` 或 ``"write"``。

        Returns:
            `str`: 已绝对化的路径（调用方应拿它去调 ``inner``，
            这样 inner 就不会再按宿主 cwd 解析一次）。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        target = self.absolute(path)
        reason = self._explain(target, kind)
        if reason is not None:
            logger.bind(path=path, target=target, kind=kind).warning(
                "沙箱策略拒绝 {}：{}",
                kind,
                reason,
            )
            raise PathEscapeError(
                f"沙箱策略拒绝 {kind} {path!r}（解析为 {target}）：{reason}",
            )
        return target

    # ------------------------------------------------------------------
    # 抽象方法（三个）
    # ------------------------------------------------------------------
    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """执行命令，并施加三重资源约束。

        实测的约束行为（全部来自 ``SandboxPolicy`` 的字段）：

        - **cwd**：``host`` 模式下给了就必须落在读白名单内 —— 否则等于把进程的
          工作目录挪到沙箱外，后续任何相对路径写入都会绕开路径策略；
          ``container`` 模式不查（容器内换目录是正常操作）；
        - **timeout**：``host`` 模式一律压到 ``policy.timeout_s``（原生
          ``LocalBackend`` 的 ``timeout=None`` 是"无限等待"，``_backend.py:294``，
          那正是"agent 卡死"的成因）；``container`` 模式只在
          ``timeout is None`` 时取 ``policy.timeout_s``，
          显式超时（例如 bootstrap 的 1800 秒）原样放行；
        - **max_output_bytes**：stdout/stderr 各自截断，避免一条 ``cat``
          把几 GB 灌进上下文。截断只发生在**直接调本方法**时；
          ``list_dir`` / ``scandir`` 等内部走的是 ``inner`` 自己的
          ``exec_shell``，不会因为截断而解析出半条记录。

        Args:
            command (`list[str]`): 可执行文件 + 参数（不经 shell）。
            cwd (`str | None`, optional): 工作目录。
            timeout (`float | None`, optional): 超时秒数（会被压到策略上限）。

        Returns:
            `ExecResult`: 执行结果（可能已截断）。

        Raises:
            PathEscapeError: ``cwd`` 越界。
        """
        if self.mode == "container":
            resolved_cwd = cwd
            effective_timeout = (
                float(self.policy.timeout_s) if timeout is None else float(timeout)
            )
        else:
            resolved_cwd = self._enforce(cwd, "read") if cwd else None
            effective_timeout = (
                float(self.policy.timeout_s)
                if timeout is None
                else min(float(timeout), float(self.policy.timeout_s))
            )
        result = await self._inner.exec_shell(
            command,
            cwd=resolved_cwd,
            timeout=effective_timeout,
        )
        return self._truncate(result)

    def _truncate(self, result: ExecResult) -> ExecResult:
        """把 stdout/stderr 压到 ``policy.max_output_bytes`` 以内。

        Args:
            result (`ExecResult`): 原始结果。

        Returns:
            `ExecResult`: 截断后的结果（未超限时原样返回同一个对象）。
        """
        limit = int(self.policy.max_output_bytes)
        stdout, cut_out = _cut(result.stdout, limit)
        stderr, cut_err = _cut(result.stderr, limit)
        if not (cut_out or cut_err):
            return result
        logger.bind(
            max_output_bytes=limit,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
        ).warning(
            "沙箱截断：命令输出超过 max_output_bytes={}，已裁剪（stdout={} stderr={}）",
            limit,
            cut_out,
            cut_err,
        )
        return ExecResult(
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    async def read_file(self, path: str) -> bytes:
        """读文件（先过读策略）。

        Args:
            path (`str`): 路径。

        Returns:
            `bytes`: 文件内容。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.read_file(self._enforce(path, "read"))

    async def write_file(self, path: str, data: bytes) -> None:
        """写文件（先过写策略）。

        Args:
            path (`str`): 路径。
            data (`bytes`): 内容。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        await self._inner.write_file(self._enforce(path, "write"), data)

    # ------------------------------------------------------------------
    # 派生 IO（逐个覆盖，都要求先过策略）
    # ------------------------------------------------------------------
    async def read_stream(
        self,
        path: str,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        """流式读（先过读策略）。

        Args:
            path (`str`): 路径。
            chunk_size (`int`, defaults to 1 MiB): 分块大小。

        Yields:
            `bytes`: 数据块。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        target = self._enforce(path, "read")
        async for chunk in self._inner.read_stream(target, chunk_size):
            yield chunk

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
    ) -> None:
        """流式写（先过写策略）。

        Args:
            path (`str`): 路径。
            stream (`AsyncIterator[bytes]`): 数据流。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        await self._inner.write_stream(self._enforce(path, "write"), stream)

    async def file_exists(self, path: str) -> bool:
        """是否存在（先过读策略）。

        Args:
            path (`str`): 路径。

        Returns:
            `bool`: 是否存在。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.file_exists(self._enforce(path, "read"))

    async def is_dir(self, path: str) -> bool:
        """是否目录（先过读策略）。

        Args:
            path (`str`): 路径。

        Returns:
            `bool`: 是否目录。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.is_dir(self._enforce(path, "read"))

    async def list_dir(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> list[str]:
        """列目录（先过读策略）。

        Args:
            path (`str`): 目录路径。
            recursive (`bool`, defaults to False): 是否递归。

        Returns:
            `list[str]`: 目录项。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.list_dir(
            self._enforce(path, "read"),
            recursive=recursive,
        )

    async def scandir(self, path: str) -> list[Any]:
        """带元数据的单层列举（先过读策略）。

        Args:
            path (`str`): 目录路径。

        Returns:
            `list[Any]`: ``DirEntry`` 列表（原样透传，不重造类型）。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.scandir(self._enforce(path, "read"))

    async def stat(self, path: str) -> Any:
        """单路径元数据（先过读策略）。

        Args:
            path (`str`): 路径。

        Returns:
            `Any`: ``DirEntry`` 或 ``None``。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.stat(self._enforce(path, "read"))

    async def stat_mtime(self, path: str) -> float | None:
        """修改时间（先过读策略）。

        Args:
            path (`str`): 路径。

        Returns:
            `float | None`: 时间戳。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.stat_mtime(self._enforce(path, "read"))

    async def delete_path(self, path: str) -> None:
        """删除（先过**写**策略：删除是写操作）。

        Args:
            path (`str`): 路径。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        await self._inner.delete_path(self._enforce(path, "write"))

    # ------------------------------------------------------------------
    # 纯字符串运算 + 转发
    # ------------------------------------------------------------------
    async def getcwd(self) -> str:
        """返回 backend 环境的工作目录。

        Returns:
            `str`: 工作区根目录（策略视角下的"当前目录"）。
        """
        return str(self.workspace_root)

    async def expanduser(self, path: str) -> str:
        """展开 ``~``（先过读策略，再转发给 inner）。

        Args:
            path (`str`): 路径。

        Returns:
            `str`: 展开后的路径。

        Raises:
            PathEscapeError: 越界或命中黑名单。
        """
        return await self._inner.expanduser(self._enforce(path, "read"))

    def describe(self) -> dict[str, Any]:
        """自述（日志 / doctor 用）。

        Returns:
            `dict[str, Any]`: 内部后端类名 + 策略摘要。
        """
        return {
            "backend": type(self).__name__,
            "inner": type(self._inner).__name__,
            "mode": self.mode,
            "workspace_root": str(self.workspace_root),
            "policy": self._view.describe(),
        }

    def __repr__(self) -> str:
        """调试表示。

        Returns:
            `str`: 形如 ``PolicyBackend(inner=LocalBackend, mode=host, root=...)``。
        """
        return (
            f"PolicyBackend(inner={type(self._inner).__name__}, mode={self.mode}, "
            f"root={self.workspace_root})"
        )


def _cut(data: bytes, limit: int) -> tuple[bytes, bool]:
    """把字节串裁到 ``limit`` 以内，并追加一句人类可读的提示。

    Args:
        data (`bytes`): 原始字节。
        limit (`int`): 上限。

    Returns:
        `tuple[bytes, bool]`: ``(裁剪后的字节, 是否发生了裁剪)``。
    """
    if len(data) <= limit:
        return data, False
    marker = f"\n... [harness_kit 沙箱已截断 {len(data) - limit} 字节]".encode()
    return data[:limit] + marker, True


class PolicyLocalWorkspace(LocalWorkspace):
    """本地工作区 + 策略校验（契约 §3.10）。

    在做完原生 ``LocalWorkspace`` 的全部事情之后，只多做两件事：

    1. 把 ``self._backend`` 换成 :class:`PolicyBackend`（``mode="host"``），
       于是 ``get_backend()`` 的每个调用方都被策略覆盖（含内置工具）；
    2. 构造期校验 ``workdir`` 落在 ``policy.workspace_root`` 之内 ——
       "策略根目录在 A、工作区实际在 B"是完全静默的失效模式，
       必须在这里就炸掉。

    Example:
        >>> policy = SandboxPolicy(workspace_root="/private/tmp/ws")
        >>> ws = PolicyLocalWorkspace(policy=policy)   # doctest: +SKIP
        >>> await ws.initialize()                      # doctest: +SKIP
        >>> await ws.read_file("a.txt")                # doctest: +SKIP
        >>> await ws.read_file("/etc/passwd")          # doctest: +SKIP
        Traceback (most recent call last):
        PathEscapeError: ...
    """

    def __init__(
        self,
        *,
        policy: SandboxPolicy,
        base_workspace: "WorkspaceBase | None" = None,
    ) -> None:
        """构造策略化本地工作区。

        ``base_workspace`` 的语义（契约只给了参数名，这里明确化）：

        - ``None``：自己造一个 ``LocalWorkspace``，工作目录取
          ``policy.workspace_root``；
        - 传了一个工作区：**沿用它的身份与配置**（``workspace_id`` /
          ``workdir`` / ``default_mcps`` / ``skill_paths`` /
          ``max_live_stateful_mcps``），保证"同一个工作区换个策略化壳"
          不会换掉 session 目录与技能分区；
          - 它的 ``workdir`` 就是新工作区的 ``workdir``；
          - 它已经活着的 backend 会被复用（如果它本身也是本地类后端），
            否则退回一个新的 ``LocalBackend``。

        Args:
            policy (`SandboxPolicy`): 生效中的策略。
            base_workspace (`WorkspaceBase | None`, optional): 被复用的工作区。

        Raises:
            PathEscapeError: ``workdir`` 落在 ``policy.workspace_root`` 之外。
        """
        source = base_workspace
        if source is None:
            source = LocalWorkspace(workdir=str(policy.workspace_root))

        workdir = str(getattr(source, "workdir", policy.workspace_root))
        super().__init__(
            workdir=workdir,
            workspace_id=getattr(source, "workspace_id", None),
            default_mcps=list(getattr(source, "default_mcps", []) or []),
            skill_paths=list(getattr(source, "skill_paths", []) or []),
            max_live_stateful_mcps=getattr(source, "max_live_stateful_mcps", None),
        )

        self.policy: SandboxPolicy = policy
        self.base_workspace: "WorkspaceBase | None" = base_workspace
        self._guard = PathGuard(policy.workspace_root)

        # 沿用被复用工作区的 instructions：它已经 format 过一次，
        # 再 format 一遍会把内容里的字面花括号当成占位符炸掉。
        source_instructions = getattr(source, "instructions", None)
        if isinstance(source_instructions, str) and str(
            getattr(source, "workdir", ""),
        ) == self.workdir:
            self.instructions = source_instructions

        if not self._guard.is_within(self.workdir):
            raise PathEscapeError(
                f"工作区目录 {self.workdir} 不在策略根目录 {policy.workspace_root} 之内："
                "workdir 必须在 workspace_root 之下，否则工具会绕开路径策略",
            )

        inner = self._resolve_inner_backend(source)
        self._inner_backend: BackendBase = inner
        self._backend = PolicyBackend(inner=inner, policy=policy)

        # 本地后端无法执行网络/配额，明确喊一声（不静默降级）
        policy.warn_if_unenforceable("local")
        logger.bind(
            workdir=self.workdir,
            workspace_id=self.workspace_id,
            policy=policy.describe(),
        ).debug("策略化本地工作区已构造")

    @staticmethod
    def _resolve_inner_backend(source: "WorkspaceBase") -> BackendBase:
        """取出被复用工作区的 backend；取不到就造一个 ``LocalBackend``。

        ``WorkspaceBase.get_backend()`` 在 ``_backend is None`` 时会抛
        ``RuntimeError``（``third_party/agentscope/src/agentscope/workspace/_base.py:524``），
        Docker/E2B 工作区在 ``initialize()`` 之前正是这个状态 ——
        所以这里必须容错，而不是让构造直接失败。

        Args:
            source (`WorkspaceBase`): 被复用的工作区。

        Returns:
            `BackendBase`: 可用的本地后端。
        """
        try:
            backend = source.get_backend()
        except RuntimeError:
            return LocalBackend()
        if isinstance(backend, LocalBackend) or type(backend)._path_module is os.path:
            return backend
        logger.warning(
            "被复用工作区的 backend 是 {}（非本地语义），"
            "策略化本地工作区改为新建 LocalBackend",
            type(backend).__name__,
        )
        return LocalBackend()

    # ------------------------------------------------------------------
    # 契约要求的三个便利方法
    # ------------------------------------------------------------------
    async def read_file(self, path: str, **kwargs: Any) -> str:
        """读文本文件，越界抛 :class:`PathEscapeError`（契约 §3.10）。

        Args:
            path (`str`): 文件路径（相对路径按工作区根解析）。
            **kwargs (`Any`): ``encoding``（默认 ``utf-8``）、
                ``errors``（默认 ``replace``）透传给 ``bytes.decode``。

        Returns:
            `str`: 文件文本内容。

        Raises:
            PathEscapeError: 越界或命中 ``deny_paths``。
        """
        backend = self.get_backend()
        target = backend.abspath(path, cwd=self.workdir)  # type: ignore[attr-defined]
        raw = await backend.read_file(target)
        encoding = str(kwargs.get("encoding") or "utf-8")
        errors = str(kwargs.get("errors") or "replace")
        return raw.decode(encoding, errors=errors)

    async def write_file(self, path: str, content: str, **kwargs: Any) -> None:
        """写文本文件，越界抛 :class:`PathEscapeError`（契约 §3.10）。

        Args:
            path (`str`): 文件路径（相对路径按工作区根解析）。
            content (`str`): 文本内容。
            **kwargs (`Any`): ``encoding``（默认 ``utf-8``）。

        Raises:
            PathEscapeError: 越界或命中 ``deny_paths``。
        """
        backend = self.get_backend()
        target = backend.abspath(path, cwd=self.workdir)  # type: ignore[attr-defined]
        encoding = str(kwargs.get("encoding") or "utf-8")
        await backend.write_file(target, content.encode(encoding))

    async def run_command(self, command: str, **kwargs: Any) -> str:
        """跑一条 shell 命令行，越界抛 :class:`PathEscapeError`（契约 §3.10）。

        ``BackendBase.exec_shell`` 收的是**参数向量**而不是命令行
        （``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:294``），
        所以这里显式包一层 shell：

        - POSIX：``["/bin/sh", "-c", command]``；
        - Windows：``["cmd.exe", "/c", command]``。

        超时由 :class:`PolicyBackend` 压到 ``policy.timeout_s``；
        输出超过 ``policy.max_output_bytes`` 会被截断。

        **``cwd`` 缺省是工作区根，不是进程 cwd**：``exec_shell(cwd=None)``
        会让命令在**宿主进程**的当前目录里跑（``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:764``
        的实现只在 ``cwd is not None`` 时才传给 ``create_subprocess_exec``），
        那意味着命令里任何相对路径都落在工作区**外面** ——
        内置的 ``Bash`` 工具正是显式传 ``cwd=self._cwd``
        （``third_party/agentscope/src/agentscope/tool/_builtin/_bash.py:715``）来避免这一点，
        本方法缺省值取 ``self.workdir`` 与它对齐。

        Args:
            command (`str`): shell 命令行。
            **kwargs (`Any`): ``cwd``（相对路径按工作区根解析；缺省为
                ``self.workdir``）、``timeout``、
                ``check``（默认 ``False``；``True`` 时非零退出码抛
                :class:`RuntimeError`）。

        Returns:
            `str`: stdout 与 stderr 的拼接（stderr 带 ``[stderr]`` 前缀）。

        Raises:
            PathEscapeError: ``cwd`` 越界。
            RuntimeError: ``check=True`` 且退出码非零。
        """
        backend = self.get_backend()
        if backend.os_name == "nt":
            argv = ["cmd.exe", "/c", command]
        else:
            argv = ["/bin/sh", "-c", command]
        cwd = kwargs.get("cwd")
        result = await backend.exec_shell(
            argv,
            cwd=str(backend.abspath(str(cwd), cwd=self.workdir)) if cwd else self.workdir,  # type: ignore[attr-defined]
            timeout=kwargs.get("timeout"),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        text = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
        if kwargs.get("check") and not result.ok():
            raise RuntimeError(
                f"命令退出码 {result.exit_code}：{command}\n{text}",
            )
        return text

    # ------------------------------------------------------------------
    # 额外便利：把策略判定暴露给上层
    # ------------------------------------------------------------------
    def check_readable(self, path: str) -> Path:
        """不产生 IO 地预检一条读路径。

        Args:
            path (`str`): 路径。

        Returns:
            `Path`: 已归一化的绝对路径。

        Raises:
            PathEscapeError: 越界或命中 ``deny_paths``。
        """
        backend = self.get_backend()
        target = backend.abspath(path, cwd=self.workdir)  # type: ignore[attr-defined]
        reason = _reason_for(self.policy, target, "read")
        if reason is not None:
            raise PathEscapeError(f"沙箱策略拒绝读 {path!r}：{reason}")
        return Path(target)

    def check_writable(self, path: str) -> Path:
        """不产生 IO 地预检一条写路径。

        Args:
            path (`str`): 路径。

        Returns:
            `Path`: 已归一化的绝对路径。

        Raises:
            PathEscapeError: 越界或命中 ``deny_paths``。
        """
        backend = self.get_backend()
        target = backend.abspath(path, cwd=self.workdir)  # type: ignore[attr-defined]
        reason = _reason_for(self.policy, target, "write")
        if reason is not None:
            raise PathEscapeError(f"沙箱策略拒绝写 {path!r}：{reason}")
        return Path(target)

    async def aclose(self) -> None:
        """``close()`` 的别名（与 :class:`QuotaDockerWorkspace` 对齐）。

        AgentScope 原生叫 ``close()``，Docker 那侧契约叫 ``aclose()``；
        两个都留着，免得调用方记错名字。
        """
        await self.close()

    def describe(self) -> dict[str, Any]:
        """自述（日志 / doctor 用）。

        Returns:
            `dict[str, Any]`: 工作区与策略摘要。
        """
        return {
            "workspace": type(self).__name__,
            "workspace_id": self.workspace_id,
            "workdir": self.workdir,
            "is_alive": self.is_alive,
            "base_workspace": type(self.base_workspace).__name__
            if self.base_workspace is not None
            else None,
            "policy": self.policy.describe(),
            "backend": self._backend.describe()
            if isinstance(self._backend, PolicyBackend)
            else {"backend": type(self._backend).__name__},
        }


def _reason_for(
    policy: SandboxPolicy,
    target: str,
    kind: _AccessKind,
) -> str | None:
    """给 :meth:`PolicyLocalWorkspace.check_readable` 复用的拒绝原因。

    Args:
        policy (`SandboxPolicy`): 策略。
        target (`str`): 绝对路径。
        kind (`_AccessKind`): ``"read"`` 或 ``"write"``。

    Returns:
        `str | None`: 拒绝原因，或 ``None``。
    """
    allowed = (
        policy.is_readable(target) if kind == "read" else policy.is_writable(target)
    )
    if allowed:
        return None
    hit = policy.deny_hit(target, kind)
    if hit is not None:
        return f"命中 deny_paths 模式 {hit!r}"
    return f"不在 {kind} 白名单内（工作区根 {policy.workspace_root}）"


async def build_policy_local_workspace(
    spec: Any,
    ctx: "BuildContext | None" = None,
) -> PolicyLocalWorkspace:
    """注册表工厂：``WorkspaceSpec`` → :class:`PolicyLocalWorkspace`。

    签名与其它工作区工厂一致（``async def build_xxx(spec, ctx=None)``），
    因为 ``harness_kit/registry.py`` 的 ``_LazyFactory`` 会把
    ``(spec, ctx)`` 一起喂进来（``_register_workspaces`` 里 ``policy_local``
    的 ``owner`` 就是本讲）。

    与 ``_build_local_workspace``（``registry.py:936``）一样**不**
    ``initialize()``：建目录/拉容器是有副作用的慢操作，时机交给调用方。

    Args:
        spec (`Any`): :class:`~harness_kit.config.schema.WorkspaceSpec`。
        ctx (`BuildContext | None`, optional): 装配上下文，用于把 ``root``
            锚到 ``repo_root``。

    Returns:
        `PolicyLocalWorkspace`: 未 ``initialize()`` 的工作区。

    Raises:
        ValueError: ``spec.policy`` 为 ``None``（此时应该用原生 ``local`` 后端）。
    """
    raw = getattr(spec, "policy", None)
    if raw is None:
        raise ValueError(
            "policy_local 需要 workspace.policy；未配置策略请使用原生 local 后端",
        )
    policy = raw if isinstance(raw, SandboxPolicy) else SandboxPolicy.model_validate(raw)

    root = getattr(spec, "root", None) or policy.workspace_root
    if ctx is not None:
        resolved_root = str(ctx.settings.resolve(str(root)))
    else:
        resolved_root = str(Path(str(root)).expanduser().resolve())

    # 策略里的 workspace_root 可能还是相对路径；统一锚到同一个根，
    # 否则后面 workdir ∈ workspace_root 的校验会误报。
    if str(policy.workspace_root) != resolved_root:
        policy = policy.model_copy(update={"workspace_root": Path(resolved_root)})

    workspace = PolicyLocalWorkspace(policy=policy)
    logger.bind(root=resolved_root, kind="policy_local").debug(
        "已构造策略化本地工作区（未 initialize）",
    )
    return workspace
