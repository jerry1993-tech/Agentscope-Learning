# -*- coding: utf-8 -*-
"""沙箱层：策略 / 本地 / Docker / offload / 路径护栏（契约 §3.10，第 10 讲）。

五个模块的分工，一句话各自说清"它补的是 AgentScope 的哪个缺口"：

=================================== =========================================================
:mod:`~harness_kit.sandbox.guard`   **路径护栏**。AgentScope 的路径判定散落在
                                    工具里（``tool/_base.py:390`` 的
                                    ``_path_in_allowed_working_path``、
                                    ``tool/_builtin/_write.py:194`` 的 fnmatch），
                                    没有一处可复用的"越界检测"。
                                    ``PathGuard`` 把它收成一个类：先 ``realpath``
                                    再比较，符号链接与 ``..`` 一起吃。
:mod:`~harness_kit.sandbox.policy`  **策略模型**。AgentScope 只有"后端"
                                    （``workspace/_base.py:223`` 的 ``WorkspaceBase``），
                                    没有"这个后端该被限制成什么样"这一层。
                                    ``SandboxPolicy`` 是那份声明，也是 Profile YAML
                                    里 ``policy:`` 块直接校验的对象
                                    （``config/schema.py:140``）。
:mod:`~harness_kit.sandbox.local`   **本地执行**。策略校验打在 ``BackendBase``
                                    边界（内置工具只认 backend），
                                    外加超时压缩与输出截断。
:mod:`~harness_kit.sandbox.docker`  **容器执行**。AgentScope 的
                                    ``DockerWorkspace`` 建容器时
                                    ``HostConfig`` 只有 ``Binds``
                                    （``_docker/_docker_workspace.py:304-311``），
                                    ``QuotaMixin`` 把 CPU / 内存 / swap / 进程数 /
                                    网络真的交给 daemon，并支持 Docker 不可用时
                                    明确报错降级。
:mod:`~harness_kit.sandbox.offload` **大内容落盘**。协议本身在工作区里已有实现
                                    （``workspace/_base.py:1004/:1060/:1119``），
                                    ``HarnessOffloader`` 只补三件工作区不该管的事：
                                    无工作区时的明确降级、单次体积护栏、可观测计数。
=================================== =========================================================

**为什么"策略"和"执行"要分开**

同一份 :class:`~harness_kit.sandbox.policy.SandboxPolicy` 要能喂给两个完全不同的
执行环境（本地进程 / 容器）。把它塞进任意一个后端，另一个就得跟着改 ——
而两个后端的限制手段毫无共同点（本地靠拦路径，容器靠 cgroup + 网络命名空间）。
"""

from harness_kit.sandbox.docker import (
    CONTAINER_WORKDIR,
    DockerUnavailableError,
    QuotaDockerWorkspace,
    QuotaMixin,
    build_quota_docker_workspace,
    docker_available,
)
from harness_kit.sandbox.guard import (
    PathEscapeError,
    PathGuard,
)
from harness_kit.sandbox.local import (
    PolicyBackend,
    PolicyLocalWorkspace,
    build_policy_local_workspace,
)
from harness_kit.sandbox.offload import (
    DEFAULT_MAX_OFFLOAD_BYTES,
    HarnessOffloader,
)
from harness_kit.sandbox.policy import (
    BACKEND_AVAILABILITY_PROBES,
    DEFAULT_DENY_PATHS,
    SANDBOX_KINDS,
    SandboxPolicy,
    choose_workspace_kind,
)

__all__ = [
    "BACKEND_AVAILABILITY_PROBES",
    "CONTAINER_WORKDIR",
    "DEFAULT_DENY_PATHS",
    "DEFAULT_MAX_OFFLOAD_BYTES",
    "DockerUnavailableError",
    "HarnessOffloader",
    "PathEscapeError",
    "PathGuard",
    "PolicyBackend",
    "PolicyLocalWorkspace",
    "QuotaDockerWorkspace",
    "QuotaMixin",
    "SANDBOX_KINDS",
    "SandboxPolicy",
    "build_policy_local_workspace",
    "build_quota_docker_workspace",
    "choose_workspace_kind",
    "docker_available",
]
