# -*- coding: utf-8 -*-
"""沙箱策略模型（契约 §3.10，第 10 讲）。

**这个类为什么必须逐字对齐契约字段**

``harness_kit/config/schema.py:140`` 在**导入时**就把
``harness_kit.sandbox.policy.SandboxPolicy`` 解引用成
:data:`~harness_kit.config.schema.SANDBOX_POLICY_CLASS`，并让
:class:`~harness_kit.config.schema.WorkspaceSpec` 的 ``policy`` 字段直接用它做注解：

.. code-block:: text

    third_party/... 不是重点，重点是：
    harness_kit/config/schema.py:140-144  SANDBOX_POLICY_CLASS = _resolve_external_type(...)
    harness_kit/config/schema.py:281      policy: SandboxPolicy | None = None

也就是说：**Profile YAML 里的 ``policy:`` 块会被我这一份定义直接校验**。
字段名少一个 → ``extra="forbid"`` 直接报错，整个 Profile 加载不出来。
所以这里的字段是契约 §3.10 的逐字翻译，一个不多一个不少，
校验（validator）只加在"值域"上，不改名、不增删字段。

**这个类为什么是"策略"而不是"沙箱"**

它只描述**意图**（哪些路径可读可写、网络怎么走、给多少 CPU/内存/进程数），
不碰任何执行。真正的执行分给两个后端：

- :mod:`harness_kit.sandbox.local` —— 本地目录，靠 :class:`~harness_kit.sandbox.guard.PathGuard`
  在 ``BackendBase`` 边界拦路径；
- :mod:`harness_kit.sandbox.docker` —— 容器，靠 ``docker run`` 的
  ``--cpus`` / ``--memory`` / ``--pids-limit`` / ``--network`` 真的设限。

同一份策略能喂给两个完全不同的执行环境，这正是把它单独建模的理由
（AgentScope 原生只有"后端"，没有"策略"这一层：
``third_party/agentscope/src/agentscope/workspace/_base.py:223`` 的 ``WorkspaceBase``
只管生命周期与布局）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harness_kit.sandbox.guard import PathEscapeError, PathGuard

__all__ = [
    "BACKEND_AVAILABILITY_PROBES",
    "DEFAULT_DENY_PATHS",
    "SANDBOX_KINDS",
    "SandboxPolicy",
    "choose_workspace_kind",
]

DEFAULT_DENY_PATHS: list[str] = [".git/", ".env", "**/*.pem"]
"""契约 §3.10 的 ``deny_paths`` 默认值（原样抄下来，供文档与测试引用）。"""

SANDBOX_KINDS: tuple[str, ...] = ("local", "docker", "e2b")
"""本层认识的后端种类。

``e2b`` 的对应实现是 AgentScope 自带的
``E2BWorkspace``（``third_party/agentscope/src/agentscope/workspace/_e2b/``），
它需要 ``e2b`` 这个第三方包；**实测本环境没装**（``import e2b`` →
``ModuleNotFoundError``），因此 :func:`choose_workspace_kind` 会把它降级掉。
"""

BACKEND_AVAILABILITY_PROBES: dict[str, bool | None] = {
    "local": True,
    "docker": None,
    "e2b": None,
}
"""各后端的"静态可用性"。``None`` 表示"要探测才知道"，``True``/``False`` 是硬结论。

``local`` 恒为 ``True``（``LocalBackend`` 只用标准库；
``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:761`` 起）。
``docker`` 需要 aiodocker + 活着的 daemon（探测函数
:func:`harness_kit.sandbox.docker.docker_available`）。
``e2b`` 需要 ``e2b`` 包与 API key。
"""


class SandboxPolicy(BaseModel):
    """一次执行所允许的路径 / 网络 / 资源边界（契约 §3.10）。

    所有路径判定都是**"先归一化，再比较"**：内部一律走
    :class:`~harness_kit.sandbox.guard.PathGuard`，因此 ``/tmp/ws/../../../etc``
    这类相对穿越和 macOS 的 ``/tmp`` → ``/private/tmp`` 符号链接都不会误判。

    Example:
        >>> policy = SandboxPolicy(workspace_root="/private/tmp/ws")
        >>> policy.is_writable("/private/tmp/ws/a.py")
        True
        >>> policy.is_writable("/private/tmp/ws/.env")
        False
        >>> policy.is_readable("/etc/passwd")
        False
    """

    model_config = ConfigDict(extra="forbid")

    workspace_root: Path
    """工作区根目录。**工作区内的路径默认可读可写**（再被 ``deny_paths`` 扣除）。"""

    read_paths: list[str] = Field(default_factory=list)
    """允许读的**额外**绝对/相对路径。相对路径按 :attr:`workspace_root` 解析。"""

    write_paths: list[str] = Field(default_factory=list)
    """允许写的**额外**绝对/相对路径。相对路径按 :attr:`workspace_root` 解析。"""

    deny_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_PATHS))
    """**黑名单**，优先级高于上面两条白名单。模式语义见
    :meth:`harness_kit.sandbox.guard.PathGuard.matches_patterns` ——
    匹配规则刻意偏保守（多拦一点是安全的，漏拦才是事故）。"""

    network: Literal["none", "allowlist", "full"] = "none"
    """网络档位：``none`` 断网 / ``allowlist`` 只放 :attr:`network_allowlist` /
    ``full`` 全放。**本地后端无法执行这一项**（本地进程没有网络命名空间），
    因此 :mod:`harness_kit.sandbox.local` 会在策略要求 ``none`` 却只能本地执行时
    打一条 warning —— 见 :meth:`is_network_enforceable`。"""

    network_allowlist: list[str] = Field(default_factory=list)
    """``network="allowlist"`` 时的白名单（主机名 / CIDR / 域名后缀）。"""

    cpu: float = 1.0
    """CPU 核数上限（``docker run --cpus``）。"""

    memory_mb: int = 1024
    """内存上限（MiB，``docker run --memory``）。"""

    pids: int = 128
    """进程数上限（``docker run --pids-limit``），防 fork 炸弹。"""

    timeout_s: int = 60
    """单次命令超时（秒）。"""

    max_output_bytes: int = 1_000_000
    """单次命令 stdout/stderr 的字节上限，超出即截断。"""

    # ------------------------------------------------------------------
    # 校验：只补值域，不改字段
    # ------------------------------------------------------------------
    @field_validator("read_paths", "write_paths", "deny_paths", "network_allowlist")
    @classmethod
    def _strip_empty(cls, value: list[str]) -> list[str]:
        """去掉空字符串项，避免空模式被当成"匹配一切"。

        ``PathGuard.matches_patterns`` 本来就会跳过空模式
        （``harness_kit/sandbox/guard.py:185`` 的 ``if not pattern: continue``），
        这里再拦一次是为了让"用户写了 ``- ""``"这件事在加载期就可见，
        而不是悄悄地什么都没发生。

        Args:
            value (`list[str]`): 原始列表。

        Returns:
            `list[str]`: 去掉空串后的列表。
        """
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("cpu")
    @classmethod
    def _require_positive_cpu(cls, value: float) -> float:
        """``cpu`` 必须为正。

        Args:
            value (`float`): CPU 核数。

        Returns:
            `float`: 原值。

        Raises:
            ValueError: 不为正。
        """
        if value <= 0:
            raise ValueError(f"cpu 必须为正数，得到 {value!r}（0 核容器起不来）")
        return value

    @field_validator("memory_mb", "pids", "timeout_s", "max_output_bytes")
    @classmethod
    def _require_positive_int(cls, value: int) -> int:
        """这几个资源上限必须为正整数。

        Args:
            value (`int`): 上限值。

        Returns:
            `int`: 原值。

        Raises:
            ValueError: 不为正。
        """
        if value <= 0:
            raise ValueError(f"资源上限必须为正整数，得到 {value!r}")
        return value

    @model_validator(mode="after")
    def _check_allowlist(self) -> "SandboxPolicy":
        """``network="allowlist"`` 必须真的给出白名单。

        失败关闭（fail closed）：白名单为空却声明 ``allowlist``，
        语义上等于"谁都别想连"，但很容易被误读成"只放白名单里的人"。
        与其在运行时让 agent 对着一个连不通的网络发懵，不如加载期直接报错。

        Returns:
            `SandboxPolicy`: 自身。

        Raises:
            ValueError: 声明了 ``allowlist`` 却没给 :attr:`network_allowlist`。
        """
        if self.network == "allowlist" and not self.network_allowlist:
            raise ValueError(
                'network="allowlist" 但 network_allowlist 为空：'
                "请给出白名单，或改用 network=\"none\"/\"full\"",
            )
        return self

    # ------------------------------------------------------------------
    # 派生量
    # ------------------------------------------------------------------
    @property
    def guard(self) -> PathGuard:
        """指向 :attr:`workspace_root` 的路径守卫。

        每次访问都新建：:class:`PathGuard` 很轻（只有一次
        ``os.path.realpath``），而缓存它会让"strategy 被原地改动"
        这种边缘情况变得难查。

        Returns:
            `PathGuard`: 新的守卫实例。
        """
        return PathGuard(self.workspace_root)

    def extra_roots(self, kind: Literal["read", "write"]) -> list[Path]:
        """把 :attr:`read_paths` / :attr:`write_paths` 解析成绝对路径。

        相对路径按 :attr:`workspace_root` 解析（**不是**进程 cwd）：
        配置文件里写的 ``"./shared"`` 显然指"工作区里的 shared"。

        Args:
            kind (`Literal["read", "write"]`): 取哪一组。

        Returns:
            `list[Path]`: 解析后的绝对路径列表（未解符号链接，比较时由
            :class:`PathGuard` 再解）。
        """
        raw = self.read_paths if kind == "read" else self.write_paths
        root = Path(self.workspace_root).expanduser()
        out: list[Path] = []
        for item in raw:
            candidate = Path(item).expanduser()
            out.append(candidate if candidate.is_absolute() else root / candidate)
        return out

    # ------------------------------------------------------------------
    # 判定（契约要求的两个公开方法）
    # ------------------------------------------------------------------
    def is_readable(self, path: Path | str) -> bool:
        """路径是否允许读。

        判定顺序（**先白后黑**）：

        1. 在 :attr:`workspace_root` 之内，或在 :attr:`read_paths` 之一之内 → 候选可读；
        2. 命中 :attr:`deny_paths` → **否决**（黑名单压倒一切）。

        注意"路径不在工作区内"只返回 ``False``，**不抛异常** ——
        契约把它定义成谓词。需要异常的是执行层
        （:mod:`harness_kit.sandbox.local` 抛 :class:`PathEscapeError`）。

        Args:
            path (`Path | str`): 待判断路径。

        Returns:
            `bool`: 是否可读。

        Example:
            >>> SandboxPolicy(workspace_root="/private/tmp/ws").is_readable("/private/tmp/ws/a.txt")
            True
            >>> SandboxPolicy(workspace_root="/private/tmp/ws").is_readable("/etc/passwd")
            False
        """
        return self._allows(path, "read")

    def is_writable(self, path: Path | str) -> bool:
        """路径是否允许写。

        判定顺序与 :meth:`is_readable` 相同，白名单换成
        :attr:`workspace_root` + :attr:`write_paths`。

        额外一条：路径**自身的父链上**只要有一环命中黑名单也拒绝。
        这是为了避免"写 ``.git/config``"这类操作绕过只匹配叶子的模式
        （``PathGuard.matches_patterns`` 的第 3、4 条规则已经覆盖了
        ``".git/"`` 这种目录段模式，这里再显式说明一遍语义，
        真正的实现复用同一个守卫，不重复造轮子）。

        Args:
            path (`Path | str`): 待判断路径。

        Returns:
            `bool`: 是否可写。

        Example:
            >>> SandboxPolicy(workspace_root="/private/tmp/ws").is_writable("/private/tmp/ws/.env")
            False
        """
        return self._allows(path, "write")

    def containing_root(self, path: Path | str, kind: Literal["read", "write"]) -> Path | None:
        """路径落在哪一个白名单根之内。

        ``deny_paths`` 的模式是**相对某个根**写的（``".env"`` / ``".git/"``），
        所以判定黑名单前必须先知道"用哪个根去算相对路径"。
        实测踩过的坑：额外 ``read_paths`` 里的路径不在
        :attr:`workspace_root` 之下，如果硬拿 workspace_root 的
        :class:`PathGuard` 去算相对路径，``PathGuard.matches_patterns``
        会直接抛 :class:`PathEscapeError`
        （``harness_kit/sandbox/guard.py:153`` 的 ``resolve_within``）——
        结果是"加进白名单的路径反而读不了"。所以这里返回**真正包含它**的那个根。

        Args:
            path (`Path | str`): 待判断路径。
            kind (`Literal["read", "write"]`): 读还是写（决定用哪组额外白名单）。

        Returns:
            `Path | None`: 包含它的根目录，或 ``None``（越界）。
        """
        guard = self.guard
        if guard.is_within(path):
            return guard.root
        for root in self.extra_roots(kind):
            rooted = PathGuard(root)
            if rooted.is_within(path):
                return rooted.root
        return None

    def deny_hit(self, path: Path | str, kind: Literal["read", "write"] = "read") -> str | None:
        """路径是否命中黑名单，命中则返回那个模式。

        模式以 :meth:`containing_root` 返回的根为基准做相对匹配；
        路径越界时返回 ``None``（越界本身已由 :meth:`containing_root` 表达，
        不需要再叠一层黑名单语义）。

        Args:
            path (`Path | str`): 待判断路径。
            kind (`Literal["read", "write"]`, defaults to ``"read"``): 用哪组白名单定根。

        Returns:
            `str | None`: 命中的模式，或 ``None``。
        """
        try:
            root = self.containing_root(path, kind)
        except PathEscapeError:
            return None
        if root is None:
            return None
        try:
            return PathGuard(root).matches_patterns(path, self.deny_paths)
        except PathEscapeError:
            return None

    def _allows(self, path: Path | str, kind: Literal["read", "write"]) -> bool:
        """白名单 + 黑名单的公共判定。

        Args:
            path (`Path | str`): 待判断路径。
            kind (`Literal["read", "write"]`): 读还是写。

        Returns:
            `bool`: 是否允许。
        """
        try:
            if self.containing_root(path, kind) is None:
                return False
            hit = self.deny_hit(path, kind)
        except PathEscapeError:
            # 连相对路径都算不出来（例如 path 是空串）→ 一律拒绝
            return False
        if hit is not None:
            logger.bind(path=str(path), pattern=hit).debug("沙箱策略拒绝：命中 deny_paths")
            return False
        return True

    def is_network_enforceable(self) -> bool:
        """当前策略的网络档位是否能被**本地**后端真正执行。

        本地后端是在宿主进程里跑的 ``LocalBackend``
        （``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:764``），
        没有任何网络命名空间/防火墙能力，所以只有"不管网络"这一种可能。
        容器后端（``third_party/agentscope/src/agentscope/workspace/_docker/``）
        才有 ``--network`` 可用。

        Returns:
            `bool`: ``network == "full"``（即"不限制"，本地恰好等价）时为 ``True``。
        """
        return self.network == "full"

    def warn_if_unenforceable(self, backend: str) -> None:
        """在本地后端上发现网络/配额无法执行时，**明确**打一条 warning。

        契约硬性要求"优雅降级并明确报错"。沉默降级是安全事故的温床：
        运维以为断网了，其实 agent 正连着外网。所以选完后端就喊一声。

        Args:
            backend (`str`): 后端名（``"local"`` / ``"docker"`` / ...）。
        """
        if backend == "local" and not self.is_network_enforceable():
            logger.warning(
                "沙箱降级：后端 {} 无法执行 network={}（本地进程没有网络命名空间）；"
                "路径策略仍然生效，网络限制本次**不会**生效。"
                "需要真的断网请用 quota_docker 后端（第 10 讲 sandbox/docker.py）",
                backend,
                self.network,
            )

    # ------------------------------------------------------------------
    # 描述
    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """返回自述（日志 / CLI / ``sandbox doctor`` 用）。

        Returns:
            `dict[str, Any]`: 全部生效中的限制。
        """
        return {
            "workspace_root": str(self.workspace_root),
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "deny_paths": list(self.deny_paths),
            "network": self.network,
            "network_allowlist": list(self.network_allowlist),
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "pids": self.pids,
            "timeout_s": self.timeout_s,
            "max_output_bytes": self.max_output_bytes,
        }

    def __repr__(self) -> str:
        """调试表示。

        Returns:
            `str`: 形如 ``SandboxPolicy(root=..., network=none, cpu=1.0)``。
        """
        return (
            f"SandboxPolicy(root={self.workspace_root}, network={self.network}, "
            f"cpu={self.cpu}, memory_mb={self.memory_mb}, pids={self.pids})"
        )


def choose_workspace_kind(
    requested: str,
    *,
    docker_ok: bool | None = None,
    e2b_ok: bool | None = None,
) -> str:
    """按 Profile 声明 + 实际可用性选后端，并把降级说清楚（第 10 讲）。

    契约只给了"后端选择与降级策略（本地 / Docker / E2B，按 Profile 与可用性自动选）"
    这一句话，这里把它落成一个**纯函数**（可测、无副作用、不 import agentscope），
    调用方（``harness_kit/config/builder.py`` 的 ``build_workspace``）自己决定
    要不要用它覆盖 ``WorkspaceSpec.kind``。

    规则：

    1. ``requested="local"`` → 恒 ``"local"``；
    2. ``requested="docker"``：``docker_ok`` 为真 → ``"docker"``，
       否则 → ``"local"`` + warning；
    3. ``requested="e2b"``：``e2b_ok`` 为真 → ``"e2b"``，
       否则 → ``"docker"``（若 Docker 可用，容器仍是真隔离）→ 再不行 → ``"local"`` + warning；
    4. 其它取值 → :class:`ValueError`（拼错的后端名绝不能被静默降级：
       那等于"以为在沙箱里跑，其实在宿主机上跑"）。

    降级**一定**打 warning，且 warning 里必须写清"哪些限制不会生效" ——
    静默降级是安全事故的温床。

    Args:
        requested (`str`): Profile 里声明的后端种类。
        docker_ok (`bool | None`, optional): Docker 可用性；``None`` 表示调用方
            没探测，此时按"不可用"处理（失败关闭）。
        e2b_ok (`bool | None`, optional): E2B 可用性；``None`` 也按"不可用"处理。

    Returns:
        `str`: ``"local"`` / ``"docker"`` / ``"e2b"``。

    Raises:
        ValueError: ``requested`` 不是 :data:`SANDBOX_KINDS` 之一。

    Example:
        >>> choose_workspace_kind("docker", docker_ok=False)
        'local'
        >>> choose_workspace_kind("docker", docker_ok=True)
        'docker'
        >>> choose_workspace_kind("e2b", docker_ok=False, e2b_ok=False)
        'local'
    """
    if requested not in SANDBOX_KINDS:
        raise ValueError(
            f"未知的沙箱后端 {requested!r}；只支持 {list(SANDBOX_KINDS)}",
        )
    if requested == "local":
        return "local"
    if requested == "docker":
        if docker_ok:
            return "docker"
        logger.warning(
            "沙箱降级：Profile 要求 docker，但 Docker 不可用（aiodocker 缺失或 "
            "daemon 连不上），改用 local 后端。**容器的进程/网络/文件系统隔离"
            "本次都不会生效**，只有路径策略还拦得住越界访问；"
            "请启动 Docker Desktop，或把 workspace.kind 显式改成 local 以去掉这条告警",
        )
        return "local"
    # requested == "e2b"
    if e2b_ok:
        return "e2b"
    if docker_ok:
        logger.warning(
            "沙箱降级：Profile 要求 e2b，但 e2b 不可用（未安装 e2b 包或缺少 API key），"
            "改用 docker 后端 —— 容器隔离仍然成立，但 **E2B 的远程执行语义"
            "（超时自动销毁、远端镜像）不生效**",
        )
        return "docker"
    logger.warning(
        "沙箱降级：Profile 要求 e2b，但 e2b 与 docker 都不可用，改用 local 后端。"
        "**进程/网络隔离本次都不会生效**，只剩路径策略；"
        "请在 .env 里配好 E2B_API_KEY，或启动 Docker",
    )
    return "local"
