# -*- coding: utf-8 -*-
"""多租户隔离：一个租户一个 ReMe 工作区（契约 §3.19）。

**为什么"隔离"在 ReMe 里等于"换一个工作区"**

ReMe 的隔离单位是**工作区目录**，不是数据库 schema、也不是表前缀：

- ``file_store`` / ``keyword_index`` / ``tag_index`` / ``file_catalog`` 的落盘路径
  全部由 ``ApplicationConfig.workspace_dir`` 派生
  （``third_party/ReMe/reme/components/base_component.py:205`` 的
  ``component_metadata_path`` 就是 ``workspace/<metadata_dir>/<component_type>/<name>.<ext>``）；
- ``search`` job 的检索范围是**整个工作区**，没有任何"按字段过滤租户"的入口。

所以只要两个租户共用 ``workspace_dir``，它们的记忆就会互相被检索到 ——
这是**结构性的**，不是配置能修的。harness 能做且该做的只有一件事：
把"租户 id → 工作区路径"这条映射收进一个地方，并且**在映射的入口做校验**，
让 ``TenantRouter.workspace_for("../../other")`` 这种调用在到达 ReMe 之前就失败。

**校验规则（以及为什么它必须这么严）**

:meth:`TenantRouter.validate_tenant_id` 只放行 ``[A-Za-z0-9._-]``，且必须
以字母或数字开头，长度 1–64。理由是 ``tenant_id`` 会被拼进路径，
而路径拼接的失败模式是**静默的**：

- ``"../a"`` → 工作区跑到 root 外面，两个租户可能撞进同一个目录；
- ``"a/b"`` → 层级变了，``root/a/b`` 看起来"也是合法的"，不会报错；
- ``""`` → ``root / ""`` 就是 root 本身，所有空租户共享一个工作区；
- ``"a\\b"`` → 在 Windows 上是两个层级，在 macOS/Linux 上是一个文件名 ——
  "同一个 id 在不同平台上落到不同目录"是最难查的一类 bug。

``".."`` 与 ``"."`` 这两个纯点号 id 额外单独拒绝：它们通过了字符集检查，
但语义上是目录导航。

本模块**不构造 ReMe 客户端**：它只产出 :class:`~harness_kit.memory.workspace.ReMeWorkspace`，
因为"租户路由"的职责到"路径"为止；把客户端也塞进来会让本类无法在
``import reme`` 失败的机器上使用（离线体检、配置解释）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .workspace import ReMeWorkspace

__all__ = [
    "MAX_TENANT_ID_LENGTH",
    "TENANT_ID_PATTERN",
    "TenantError",
    "TenantRouter",
]

#: 租户 id 的最大长度。64 不是魔法数：它是"足够表达 org/project/team"与
#: "不会把路径撑爆"之间的折中，也让 ``<root>/<tenant>/metadata/...`` 保持在
#: 常见文件系统的单段长度限制（255）之内的安全区。
MAX_TENANT_ID_LENGTH: int = 64

#: 租户 id 的字符集：字母数字开头，其后允许 ``.`` ``_`` ``-``。
TENANT_ID_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class TenantError(ValueError):
    """租户 id 非法（契约 §3.19）。

    继承 ``ValueError`` 而不是自定义的 ``Exception``：租户 id 是**调用方传进来的参数**，
    参数不合法在 Python 里的标准表达就是 ``ValueError``，
    这样 ``except ValueError`` 的既有代码也能接住它。
    """


class TenantRouter:
    """把租户 id 映射到独立的工作区（契约 §3.19）。

    Example::

        router = TenantRouter(Path("./.harness/tenants"))
        ws = router.workspace_for("acme")
        ws.ensure()
        assert ws.root == Path("./.harness/tenants/acme").resolve()
        router.workspace_for("../etc")     # raises TenantError

    目录布局：``<root>/<tenant_id>/``，每个租户目录下就是一份完整的 ReMe 工作区
    （``resource`` / ``daily`` / ``metadata`` / ...）。
    """

    def __init__(self, root: Path) -> None:
        """记录租户根目录。

        Args:
            root (`Path`): 存放全部租户工作区的根目录。构造时 ``resolve()`` ——
                理由与 :class:`~harness_kit.memory.workspace.ReMeWorkspace` 相同：
                macOS 的 ``/tmp`` 是 ``/private/tmp`` 的符号链接，不 resolve 会让
                后续的越界判定误判。

        Raises:
            `TenantError`: ``root`` 是空字符串。
        """
        raw = str(root).strip()
        if not raw:
            raise TenantError("TenantRouter.root 不能为空")
        self.root: Path = Path(raw).expanduser().resolve()

    # ------------------------------------------------------------------
    # 校验与映射
    # ------------------------------------------------------------------
    def validate_tenant_id(self, tenant_id: str) -> str:
        """校验并归一租户 id（契约 §3.19）。

        Args:
            tenant_id (`str`): 待校验的租户 id。

        Returns:
            `str`: 校验通过的租户 id。**原样返回，不做 casefold** ——
            大小写不同的 id 会映射到不同目录，这一点在大小写不敏感的文件系统
            （macOS 默认、Windows）上会静默撞车。本方法不做归一，
            因为"acme" 与 "ACME" 是不是同一个租户是**业务决策**；
            harness 只保证"同一个字符串总是映射到同一个目录"。

        Raises:
            `TenantError`: 不是 str、空、超长、纯点号、含非法字符、以点号结尾。
        """
        if not isinstance(tenant_id, str):
            raise TenantError(f"tenant_id 必须是 str，收到 {type(tenant_id).__name__}")
        candidate = tenant_id.strip()
        if not candidate:
            raise TenantError("tenant_id 不能为空")
        if len(candidate) > MAX_TENANT_ID_LENGTH:
            raise TenantError(
                f"tenant_id 最长 {MAX_TENANT_ID_LENGTH} 字符，收到 {len(candidate)}: {candidate[:80]!r}",
            )
        if candidate in (".", ".."):
            raise TenantError(f"tenant_id 不能是目录导航: {candidate!r}")
        if not TENANT_ID_PATTERN.match(candidate):
            raise TenantError(
                f"tenant_id 只允许 [A-Za-z0-9._-] 且必须以字母或数字开头: {candidate!r}。"
                "含路径分隔符、空白或前导点号的 id 会让工作区跑到租户根目录之外。",
            )
        if candidate.endswith("."):
            raise TenantError(
                f"tenant_id 不能以 '.' 结尾: {candidate!r}（Windows 上会被悄悄截断，"
                "导致两个不同 id 落到同一个目录）",
            )
        return candidate

    def tenant_root(self, tenant_id: str) -> Path:
        """返回租户目录（**不**创建，也**不**校验目录是否存在）。

        Args:
            tenant_id (`str`): 租户 id。

        Returns:
            `Path`: ``<root>/<tenant_id>``。

        Raises:
            `TenantError`: 见 :meth:`validate_tenant_id`。
        """
        return self.root / self.validate_tenant_id(tenant_id)

    def workspace_for(self, tenant_id: str) -> ReMeWorkspace:
        """返回该租户的 ReMe 工作区（契约 §3.19）。

        注意：**不会**自动 ``ensure()`` 建目录。原因是建目录是有副作用的行为，
        而这个方法在"我只想看看工作区在哪"的场景里也会被调用。
        需要建目录时显式调 ``ws.ensure()``（或 :meth:`ensure_workspace`）。

        Args:
            tenant_id (`str`): 租户 id。

        Returns:
            `ReMeWorkspace`: 根目录为 ``<root>/<tenant_id>`` 的工作区。

        Raises:
            `TenantError`: 见 :meth:`validate_tenant_id`。
        """
        return ReMeWorkspace(root=self.tenant_root(tenant_id))

    def ensure_workspace(self, tenant_id: str) -> ReMeWorkspace:
        """返回该租户的工作区**并建好目录**。

        Args:
            tenant_id (`str`): 租户 id。

        Returns:
            `ReMeWorkspace`: 已 ``ensure()`` 过的工作区。

        Raises:
            `TenantError`: 见 :meth:`validate_tenant_id`。
            `WorkspaceError`: 目录建不出来（权限等）。
        """
        workspace = self.workspace_for(tenant_id)
        workspace.ensure()
        return workspace

    # ------------------------------------------------------------------
    # 枚举
    # ------------------------------------------------------------------
    def list_tenants(self) -> list[str]:
        """列出已存在的租户 id（扫目录，不读配置）。

        只返回**通过了校验**的目录名：一个手工塞进 ``root`` 的怪名字目录
        不该出现在租户列表里，否则调用方拿到它再去 ``workspace_for`` 就会抛异常 ——
        "列表能跑通、逐个访问就炸"是最难用的 API 形态。

        Returns:
            `list[str]`: 已排序的合法租户 id；``root`` 不存在时返回空列表。
        """
        if not self.root.is_dir():
            return []
        found: list[str] = []
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            try:
                found.append(self.validate_tenant_id(entry.name))
            except TenantError:
                continue
        return sorted(found)

    def exists(self, tenant_id: str) -> bool:
        """判断该租户的工作区目录是否已存在。

        Args:
            tenant_id (`str`): 租户 id。

        Returns:
            `bool`: 目录存在且是目录。

        Raises:
            `TenantError`: 见 :meth:`validate_tenant_id`。
        """
        return self.tenant_root(tenant_id).is_dir()

    # ------------------------------------------------------------------
    # 隔离自检
    # ------------------------------------------------------------------
    def assert_isolated(self, left: str, right: str) -> None:
        """断言两个租户的工作区互不包含（自检用，见模块 docstring 的"结构性"一节）。

        Args:
            left (`str`): 租户 id。
            right (`str`): 租户 id。

        Raises:
            `TenantError`: 两个 id 相同，或其中一个的工作区在另一个之内。
        """
        left_root = self.tenant_root(left)
        right_root = self.tenant_root(right)
        if left_root == right_root:
            raise TenantError(f"租户 {left!r} 与 {right!r} 映射到同一个工作区: {left_root}")
        if left_root in right_root.parents or right_root in left_root.parents:
            raise TenantError(f"租户 {left!r} 与 {right!r} 的工作区互相嵌套: {left_root} / {right_root}")

    def describe(self) -> str:
        """渲染一张租户清单（给日志与教程用）。

        Returns:
            `str`: 多行文本。
        """
        tenants = self.list_tenants()
        lines = [f"TenantRouter(root={self.root})", f"  租户数: {len(tenants)}"]
        for tenant in tenants:
            marker = "已建" if self.exists(tenant) else "未建"
            lines.append(f"  - {tenant} ({marker})")
        return "\n".join(lines)

    def __repr__(self) -> str:
        """返回调试用表示。

        Returns:
            `str`: ``TenantRouter(root=...)``。
        """
        return f"TenantRouter(root={str(self.root)!r})"

    def as_dict(self) -> dict[str, Any]:
        """导出路由表（给评测/可观测层消费）。

        Returns:
            `dict[str, Any]`: ``{"root": str, "tenants": [...]}``。
        """
        return {"root": str(self.root), "tenants": self.list_tenants()}
