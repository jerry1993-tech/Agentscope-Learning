# -*- coding: utf-8 -*-
"""Docker 工作区 + 配额（契约 §3.10，第 10 讲）。

**AgentScope 的 Docker 后端缺什么**

``DockerWorkspace`` 会在容器里跑一个 MCP gateway、按内容哈希构建镜像、
把宿主目录 bind-mount 到 ``/workspace``，但它**完全不管资源配额**。
``third_party/agentscope/src/agentscope/workspace/_docker/_docker_workspace.py:304-311``
构造 ``HostConfig`` 的原文（实测抄录）是：

.. code-block:: python

    host_config: dict[str, Any] = {}
    if self.host_workdir is not None:
        os.makedirs(self.host_workdir, exist_ok=True)
        host_config["Binds"] = [
            f"{os.path.abspath(self.host_workdir)}:{CONTAINER_WORKDIR}:rw",
        ]
    config["HostConfig"] = host_config

只有一项 ``Binds``。也就是说一个 ``while True: os.fork()`` 能打满宿主机，
一次内存爆炸能把整台机器拖进 swap。契约 §3.10 要的两件事就是补这个：

1. 把 :class:`~harness_kit.sandbox.policy.SandboxPolicy` 翻译成
   ``--cpus`` / ``--memory`` / ``--pids-limit`` / ``--network``；
2. 超时与输出截断。

**为什么用"临时替换一个方法"而不是重写建容器流程**

AgentScope 2.0.8 **没有**任何 HostConfig 扩展点（没有 hook 参数、
没有可覆写的 ``_build_host_config``）。要注入配额只有三条路：

1. 把 ``_create_and_start_container`` 整段抄一遍再加字段 —— 抄 35 行第三方私有
   实现（还要连带 ``_image_tag`` / ``CONTAINER_WORKDIR`` / ``DockerBackend``），
   上游一改就静默错位；
2. 构造后再改容器 —— 配额是**创建时**属性，``docker update`` 覆盖不了
   ``NetworkMode`` / ``PidsLimit``，不完整；
3. **只在一个方法的调用窗口里替换 ``containers.create_or_replace``**，
   把配额并进 ``config["HostConfig"]``，然后原样交回给基类实现。

本模块选 3：改动面最小（一个方法、一次调用、``finally`` 立刻还原），
基类的镜像构建 / 缓存 / gateway 流程一行不动。补丁本体是纯函数
（:meth:`QuotaMixin.apply_quotas_to_config`），不连 daemon 也能真跑真断言。

**容器内的路径策略为什么与本地不同**

容器里 ``/workspace`` 只是 bind-mount 的一个点，AgentScope 的容器初始化会
``exec_shell(["mkdir", "-p", ...], cwd="/")``
（``third_party/agentscope/src/agentscope/workspace/_sandboxed_base.py:417-428``），
gateway 又装在 ``/root/.agentscope``（``.../_docker/_make_dockerfile.py:40``）。
所以 :class:`~harness_kit.sandbox.local.PolicyBackend` 在这里用
``mode="container"``：容器内任意路径放行，**只有** ``/workspace`` 之下才查
``deny_paths``（保住 bind-mount 回宿主的那部分）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentscope.workspace import DockerWorkspace
from loguru import logger

from harness_kit.sandbox.local import PolicyBackend
from harness_kit.sandbox.policy import SandboxPolicy

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from harness_kit.registry import BuildContext

__all__ = [
    "CONTAINER_WORKDIR",
    "DockerUnavailableError",
    "QuotaDockerWorkspace",
    "QuotaMixin",
    "build_quota_docker_workspace",
    "docker_available",
]

CONTAINER_WORKDIR = "/workspace"
"""容器内的工作目录（实测常量 ``CONTAINER_WORKDIR``，来自
``third_party/agentscope/src/agentscope/workspace/_docker/_make_dockerfile.py:38``，
被 ``_docker_workspace.py:129`` 赋给 ``self.workdir``）。"""


class DockerUnavailableError(RuntimeError):
    """Docker 不可用（aiodocker 没装 / daemon 连不上）。

    继承 :class:`RuntimeError`：这是**环境**问题，不是参数问题。
    调用方应当据此降级到 :class:`~harness_kit.sandbox.local.PolicyLocalWorkspace`
    （``harness_kit.sandbox.policy.choose_workspace_kind()`` 就是干这个的），
    而不是改参数重试。
    """


async def docker_available() -> bool:
    """探测 Docker 是否真的可用（能连上 daemon）。

    只看"``import aiodocker`` 成功"是不够的：Docker Desktop 没启动时
    包照样 import 得进来，一调 daemon 才炸。所以这里真的 ping 一次
    ``client.version()``，并且**一定**关掉 client —— aiodocker 内部持有
    aiohttp 会话，不关会拖到进程退出时打一堆 "Unclosed client session"。

    Returns:
        `bool`: 可用则 ``True``。
    """
    try:
        import aiodocker
    except ImportError:
        logger.debug("Docker 探测：aiodocker 未安装")
        return False
    client = None
    try:
        client = aiodocker.Docker()
        await client.version()
        return True
    except Exception as exc:  # noqa: BLE001 - 探测就该吞掉一切
        logger.debug("Docker 探测失败：{}: {}", type(exc).__name__, exc)
        return False
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


class QuotaMixin:
    """把 :class:`SandboxPolicy` 的配额真的交给 Docker daemon。

    独立成 mixin：它的逻辑（纯函数翻译 + 一次窗口期方法替换 + 一个后端包装）
    与"真连 daemon"的生命周期无关，单独测得了，也免得
    :class:`QuotaDockerWorkspace` 变成一坨。

    依赖宿主类提供的东西（由 :class:`QuotaDockerWorkspace` 保证）：
    ``self.policy``、``self._client``、``self._backend``、
    ``_provision_backend()``、``_create_and_start_container()``。
    """

    policy: SandboxPolicy
    """生效中的策略（由 :meth:`QuotaDockerWorkspace.__init__` 设置）。"""

    def quotas_as_host_config(self) -> dict[str, Any]:
        """把策略翻译成 Docker ``HostConfig`` 的键值（纯函数，可直接测）。

        字段映射（Docker Engine API 的命名，数值一律整数）：

        =================== ================================================
        ``NanoCpus``        ``int(cpu * 1e9)`` —— Docker 的 CPU 单位是
                            "十亿分之一核"，``--cpus 1.5`` → ``1500000000``
        ``Memory``          ``memory_mb * 1024 * 1024``（字节）
        ``MemorySwap``      与 ``Memory`` 相同 → **禁用 swap**。
                            不设这一项时容器内存超限可以吃宿主 swap，
                            配额就形同虚设
        ``PidsLimit``       ``pids``（防 fork 炸弹）
        ``NetworkMode``     ``none`` / ``allowlist`` → ``"none"``；
                            ``full`` → 不写这个键（用 Docker 默认 bridge）
        =================== ================================================

        ``network="allowlist"`` 时**降级为断网**（fail closed）并打 warning：
        HostConfig 表达不了"只允许连某些域名"，那需要自定义网络配合
        代理/iptables 才能真正生效。断网更严，方向是安全的，而且日志说清了。

        Returns:
            `dict[str, Any]`: 可直接 ``update`` 进 ``HostConfig`` 的字典。
        """
        memory_bytes = int(self.policy.memory_mb) * 1024 * 1024
        host_config: dict[str, Any] = {
            "NanoCpus": int(self.policy.cpu * 1_000_000_000),
            "Memory": memory_bytes,
            "MemorySwap": memory_bytes,
            "PidsLimit": int(self.policy.pids),
        }
        if self.policy.network == "full":
            logger.debug("Docker 配额：network=full，不限制网络")
        else:
            host_config["NetworkMode"] = "none"
            if self.policy.network == "allowlist":
                logger.warning(
                    "Docker 配额：network=allowlist 无法由 HostConfig 表达，"
                    "已降级为 network=none（断网，fail closed）；"
                    "白名单 {} 本次不生效。需要按域名放行请改用自定义 "
                    "bridge 网络 + 代理/iptables",
                    self.policy.network_allowlist,
                )
        return host_config

    def apply_quotas_to_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """把配额并进一份容器创建 ``config``（原地改，也返回它）。

        单独成方法是为了让"翻译结果对不对"能在**不连 daemon** 的前提下
        被真实执行验证（验证脚本里直接喂一份假 config 进来断言）。

        Args:
            config (`dict[str, Any]`): ``create_or_replace`` 用的 config。

        Returns:
            `dict[str, Any]`: 同一个 dict（已并入 ``HostConfig`` 配额）。

        Raises:
            TypeError: ``config`` 不是 dict，或 ``HostConfig`` 存在但不是 dict。
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"容器创建 config 期望 dict，得到 {type(config).__name__}",
            )
        host_config = config.setdefault("HostConfig", {})
        if not isinstance(host_config, dict):
            raise TypeError(
                f"HostConfig 期望 dict，得到 {type(host_config).__name__}",
            )
        host_config.update(self.quotas_as_host_config())
        return config

    async def _create_and_start_container(self) -> None:
        """在基类建容器的**调用窗口内**注入配额。

        实现：临时把 ``self._client.containers.create_or_replace`` 换成一层包装，
        把配额并进 ``config["HostConfig"]``，其余参数原样透传
        （``*args, **kwargs``，所以位置参数/关键字参数两种调用方式都不会错位）。
        ``finally`` 里立刻还原，不影响其它 workspace 实例。

        Raises:
            DockerUnavailableError: ``self._client`` 还没建立。
        """
        client = self._client
        if client is None:  # pragma: no cover - 基类保证先建 client
            raise DockerUnavailableError(
                "QuotaMixin._create_and_start_container 在 _provision_backend "
                "之前被调用：self._client 为空",
            )
        containers = client.containers
        original = containers.create_or_replace

        async def _with_quotas(*args: Any, **kwargs: Any) -> Any:
            """带配额的 ``create_or_replace``。"""
            config: Any = kwargs.get("config")
            if config is None and len(args) >= 2:
                config = args[1]
            if isinstance(config, dict):
                self.apply_quotas_to_config(config)
                logger.info("Docker 配额已注入：{}", self.quotas_as_host_config())
            return await original(*args, **kwargs)

        containers.create_or_replace = _with_quotas  # type: ignore[method-assign]
        try:
            await super()._create_and_start_container()  # type: ignore[misc]
        finally:
            containers.create_or_replace = original  # type: ignore[method-assign]

    async def _provision_backend(self) -> None:
        """先按基类拉起容器，再把 backend 换成策略化包装。

        ``mode="container"`` 的那一层负责契约 §3.10 的"② 超时与输出截断"，
        以及保护 ``/workspace`` 这个 bind-mount 点的 ``deny_paths``。

        Raises:
            DockerUnavailableError: 基类没有把 ``_backend`` 准备好。
        """
        await super()._provision_backend()  # type: ignore[misc]
        inner = self._backend
        if inner is None:  # pragma: no cover - 基类有 assert 兜底
            raise DockerUnavailableError(
                "_provision_backend 结束后 self._backend 仍为 None",
            )
        self._backend = PolicyBackend(
            inner=inner,
            policy=self.policy,
            workspace_root=CONTAINER_WORKDIR,
            mode="container",
        )
        logger.debug(
            "容器 backend 已包装：{} -> {}",
            type(inner).__name__,
            type(self._backend).__name__,
        )


class QuotaDockerWorkspace(QuotaMixin, DockerWorkspace):
    """Docker 工作区 + 配额（契约 §3.10）。

    MRO：``QuotaDockerWorkspace`` → :class:`QuotaMixin` → ``DockerWorkspace``
    → ``SandboxedWorkspaceBase`` → ``WorkspaceBase``。
    mixin 里的 ``super()`` 因此正好落到 ``DockerWorkspace`` 的实现上，
    生命周期（``initialize`` / ``close`` / gateway / 镜像缓存）全部复用。

    Example:
        >>> policy = SandboxPolicy(workspace_root="/private/tmp/ws", cpu=0.5)
        >>> ws = QuotaDockerWorkspace(policy=policy)   # doctest: +SKIP
        >>> await ws.start()                           # doctest: +SKIP
        >>> await ws.run_command("nproc")              # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        policy: SandboxPolicy,
        image: str = "python:3.11-slim",
        base_workspace: "Any | None" = None,
    ) -> None:
        """构造配额化 Docker 工作区（**不**拉容器）。

        Args:
            policy (`SandboxPolicy`): 生效中的策略。``workspace_root`` 在这里
                表示**宿主**侧的持久化目录；容器内固定是 ``/workspace``。
            image (`str`, defaults to ``"python:3.11-slim"``): 基础镜像，
                对应 ``DockerWorkspace(base_image=...)``。
            base_workspace (`Any | None`, optional): 复用其身份（``workspace_id``）
                与宿主工作目录；``None`` 表示新建。
        """
        kwargs: dict[str, Any] = {}
        if base_workspace is not None:
            workspace_id = getattr(base_workspace, "workspace_id", None)
            if workspace_id:
                kwargs["workspace_id"] = workspace_id
        super().__init__(
            base_image=image,
            host_workdir=str(policy.workspace_root),
            **kwargs,
        )
        self.policy = policy
        self.image = image
        self.base_workspace = base_workspace
        self.quotas_injected = False
        policy.warn_if_unenforceable("docker")
        logger.bind(image=image, policy=policy.describe()).debug(
            "配额化 Docker 工作区已构造（未 initialize）",
        )

    # ------------------------------------------------------------------
    # 契约要求的生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """探测 Docker 后启动工作区（契约 §3.10）。

        **先探测再动手**：daemon 不可用时给出的是
        :class:`DockerUnavailableError` 加一句"请降级到 policy_local"，
        而不是 aiodocker 里冒出来的一串 ``ClientConnectorError``。

        Raises:
            DockerUnavailableError: aiodocker 未安装或 daemon 连不上。
        """
        if not await docker_available():
            raise DockerUnavailableError(
                "Docker 不可用：aiodocker 未安装或 daemon 连不上。"
                "请改用本地后端（harness_kit.sandbox.local.PolicyLocalWorkspace / "
                'registry 里的 "policy_local"），或用 '
                "harness_kit.sandbox.policy.choose_workspace_kind() 让它自动降级",
            )
        await self.initialize()
        logger.info(
            "配额化 Docker 工作区已启动：image={} workdir={} quotas={}",
            self.image,
            self.workdir,
            self.quotas_as_host_config(),
        )

    async def aclose(self) -> None:
        """关闭容器与 client（``close()`` 的别名，契约 §3.10）。"""
        await self.close()

    # ------------------------------------------------------------------
    # 便利方法（与 PolicyLocalWorkspace 对齐的调用面）
    # ------------------------------------------------------------------
    async def run_command(self, command: str, **kwargs: Any) -> str:
        """在容器里跑一条 shell 命令行。

        与 :meth:`harness_kit.sandbox.local.PolicyLocalWorkspace.run_command`
        同一套语义，只是执行地点在容器内、路径判定走 ``container`` 模式。

        Args:
            command (`str`): shell 命令行。
            **kwargs (`Any`): ``cwd``（容器内路径）、``timeout``、
                ``check``（默认 ``False``）。

        Returns:
            `str`: stdout + （非空时）``[stderr]`` 段。

        Raises:
            RuntimeError: ``check=True`` 且退出码非零。
        """
        backend = self.get_backend()
        argv = ["cmd.exe", "/c", command] if backend.os_name == "nt" else [
            "/bin/sh",
            "-c",
            command,
        ]
        result = await backend.exec_shell(
            argv,
            cwd=kwargs.get("cwd"),
            timeout=kwargs.get("timeout"),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        text = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
        if kwargs.get("check") and not result.ok():
            raise RuntimeError(f"容器内命令退出码 {result.exit_code}：{command}\n{text}")
        return text

    async def read_file(self, path: str, **kwargs: Any) -> str:
        """读容器内文本文件。

        Args:
            path (`str`): 容器内路径（相对路径按 ``/workspace`` 解析）。
            **kwargs (`Any`): ``encoding`` / ``errors``。

        Returns:
            `str`: 文本内容。
        """
        backend = self.get_backend()
        target = backend.abspath(path, cwd=CONTAINER_WORKDIR)  # type: ignore[attr-defined]
        raw = await backend.read_file(target)
        return raw.decode(
            str(kwargs.get("encoding") or "utf-8"),
            errors=str(kwargs.get("errors") or "replace"),
        )

    async def write_file(self, path: str, content: str, **kwargs: Any) -> None:
        """写容器内文本文件。

        Args:
            path (`str`): 容器内路径。
            content (`str`): 文本内容。
            **kwargs (`Any`): ``encoding``。
        """
        backend = self.get_backend()
        target = backend.abspath(path, cwd=CONTAINER_WORKDIR)  # type: ignore[attr-defined]
        await backend.write_file(target, content.encode(str(kwargs.get("encoding") or "utf-8")))

    def describe(self) -> dict[str, Any]:
        """自述（日志 / doctor 用）。

        Returns:
            `dict[str, Any]`: 镜像、宿主机目录、容器内目录、配额。
        """
        return {
            "workspace": type(self).__name__,
            "workspace_id": self.workspace_id,
            "image": self.image,
            "host_workdir": self.host_workdir,
            "container_workdir": self.workdir,
            "is_alive": self.is_alive,
            "quotas": self.quotas_as_host_config(),
            "policy": self.policy.describe(),
        }


async def build_quota_docker_workspace(
    spec: Any,
    ctx: "BuildContext | None" = None,
) -> QuotaDockerWorkspace:
    """注册表工厂：``WorkspaceSpec`` → :class:`QuotaDockerWorkspace`。

    与其它工作区工厂同签名（``async def build_xxx(spec, ctx=None)``），
    因为 ``harness_kit/registry.py`` 的 ``_LazyFactory`` 会把 ``(spec, ctx)``
    一起喂进来（``_register_workspaces`` 里 ``quota_docker`` 的 ``owner`` 是本讲）。

    **不** ``initialize()``：拉容器是有副作用的慢操作，时机交给调用方
    （``await ws.start()`` 或 ``async with ws``）。

    Args:
        spec (`Any`): :class:`~harness_kit.config.schema.WorkspaceSpec`。
        ctx (`BuildContext | None`, optional): 装配上下文，用于把 ``root``
            锚到 ``repo_root``。

    Returns:
        `QuotaDockerWorkspace`: 未 ``initialize()`` 的工作区。

    Raises:
        ValueError: ``spec.policy`` 为 ``None``。
    """
    from pathlib import Path

    raw = getattr(spec, "policy", None)
    if raw is None:
        raise ValueError(
            "quota_docker 需要 workspace.policy；未配置策略请使用原生 docker 后端",
        )
    policy = raw if isinstance(raw, SandboxPolicy) else SandboxPolicy.model_validate(raw)

    root = getattr(spec, "root", None) or policy.workspace_root
    if ctx is not None:
        resolved_root = str(ctx.settings.resolve(str(root)))
    else:
        resolved_root = str(Path(str(root)).expanduser().resolve())
    if str(policy.workspace_root) != resolved_root:
        policy = policy.model_copy(update={"workspace_root": Path(resolved_root)})

    image = getattr(spec, "image", None) or "python:3.11-slim"
    workspace = QuotaDockerWorkspace(policy=policy, image=str(image))
    logger.bind(root=resolved_root, image=image, kind="quota_docker").debug(
        "已构造配额化 Docker 工作区（未 initialize）",
    )
    return workspace
