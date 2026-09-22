# -*- coding: utf-8 -*-
"""ReMe 工作区（workspace / vault）的路径模型与目录治理。

**这一层为什么存在**

ReMe 自己会在 ``Application.__init__`` 里创建六个子目录
（``third_party/ReMe/reme/application.py:62-79`` 的 ``_setup_workspace_directories``），
但它只负责 "mkdir"，不负责：

1. **路径语义**：契约 §3.15 规定 ``metadata/`` 放组件元数据、``session/`` 放会话、
   ``mem_session/`` 放记忆会话、``resource/`` 放原始资源、``daily/`` 放日志、
   ``digest/`` 放摘要 —— 这些名字的真值来自
   ``third_party/ReMe/reme/schema/application_config.py:37-44`` 的
   ``ApplicationConfig`` 字段默认值。harness_kit 把这份真值固化成一个 pydantic 模型，
   这样 "目录名" 只有一个来源，配置文件改了就全都跟着改。
2. **校验与清理**：工作区被别人塞进非目录的同名文件、残留了上次崩溃的半成品、
   或者租户 id 里带了 ``../`` —— 这些都要在动手前拦住。
3. **macOS 的 ``/tmp`` 陷阱**：``/tmp`` 是 ``/private/tmp`` 的符号链接，
   而 ``ComponentMixin.to_workspace_relative`` 用的是 ``Path.absolute()``（不解析符号链接），
   拼出来的相对路径会退化成绝对路径（已实测，见 ``_recon/15_integration_agentscope_reme.md`` 的坑表）。
   所以本模块一律 ``Path.resolve()`` 之后再比前缀。

**对齐的真实 API**

- ``third_party/ReMe/reme/schema/application_config.py:33-44``
  ``ApplicationConfig.workspace_dir`` / ``metadata_dir`` / ``session_dir`` /
  ``mem_session_dir`` / ``resource_dir`` / ``daily_dir`` / ``digest_dir``
- ``third_party/ReMe/reme/application.py:62`` ``_setup_workspace_directories``
- ``third_party/ReMe/reme/components/base_component.py:38`` ``workspace_path``
- ``third_party/ReMe/reme/components/base_component.py:49`` ``to_workspace_relative``
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DEFAULT_SUBDIRS",
    "WorkspaceError",
    "ReMeWorkspace",
    "WorkspaceCleanReport",
]

#: 六个子目录的 ``(字段名, 默认目录名)``。默认目录名逐字取自
#: ``third_party/ReMe/reme/schema/application_config.py:37-44``。
DEFAULT_SUBDIRS: tuple[tuple[str, str], ...] = (
    ("metadata_dir", "metadata"),
    ("session_dir", "session"),
    ("mem_session_dir", "mem_session"),
    ("resource_dir", "resource"),
    ("daily_dir", "daily"),
    ("digest_dir", "digest"),
)


class WorkspaceError(RuntimeError):
    """工作区不可用（不可创建 / 不可写 / 越界 / 结构损坏）。"""


class WorkspaceCleanReport(BaseModel):
    """一次 :meth:`ReMeWorkspace.clean` 的结果。"""

    model_config = ConfigDict(extra="forbid")

    removed_files: list[str] = Field(default_factory=list)
    """被删除的文件（相对工作区的 POSIX 路径）。"""

    removed_dirs: list[str] = Field(default_factory=list)
    """被删除的空目录（相对工作区的 POSIX 路径）。"""

    bytes_freed: int = 0
    """回收的字节数。"""

    kept: list[str] = Field(default_factory=list)
    """因为落在保护名单里而**没有**被删除的路径。"""


class ReMeWorkspace(BaseModel):
    """ReMe 工作区的路径与元数据（契约 §3.15）。

    这个类**不持有任何 ReMe 对象**，只持有路径与目录名，因此可以在
    ``import reme`` 失败时照样构造（离线体检、配置解释、单元测试都靠这一点）。

    目录名与 :class:`reme.schema.ApplicationConfig` 的字段一一对应，
    改配置时两边必须同时改；:meth:`dir_overrides` 把六个字段导成字典，
    :meth:`~harness_kit.memory.config.HarnessMemoryConfig.build` 再把它
    合并进 ReMe 配置（``config.py`` 里那句 ``cfg.update(self.workspace.dir_overrides())``）。

    Example::

        ws = ReMeWorkspace(root=Path("./.harness/reme"))
        ws.ensure()
        cfg = {"workspace_dir": str(ws.root)} | ws.dir_overrides()
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    root: Path
    """工作区根目录。构造时会被 ``resolve()`` 成绝对路径。"""

    metadata_dir: str = "metadata"
    """组件元数据目录（BM25 的 ``.pkl``、file_store 的 ``.jsonl.zst`` 都在这里）。"""

    session_dir: str = "session"
    """Agent 会话目录；对话原文固定派生成 ``{session_dir}/dialog``。"""

    mem_session_dir: str = "mem_session"
    """记忆会话目录（auto_memory 的输入整形结果）。"""

    resource_dir: str = "resource"
    """原始资源目录（待入库的文档、图片等）。"""

    daily_dir: str = "daily"
    """日记忆目录：``daily/<YYYY-MM-DD>/<card>.md``。"""

    digest_dir: str = "digest"
    """摘要目录（dream 流水线的产物）。"""

    def __init__(self, **data: object) -> None:
        """构造并把 ``root`` 解析成绝对路径。

        空 ``root`` 的检查必须发生在 ``super().__init__`` **之前**：
        pydantic 会把 ``root=""`` 先强制转成 ``Path("")``，也就是 ``Path(".")``，
        于是后面那个 ``str(self.root).strip()`` 拿到的是 ``"."``（非空），
        检查形同虚设 —— 工作区会**静默绑定到当前工作目录**
        （实测：``ReMeWorkspace(root="")`` 的 ``root`` 是 ``/private/tmp``）。
        这个坑的后果不是"路径不对"，而是 :meth:`destroy` 会去删 cwd。
        所以这里按原始入参先挡一道，并且抛 :class:`WorkspaceError`
        （在 validator 里抛会被 pydantic 包成 ``ValidationError``，调用方就抓不到了）。

        Args:
            **data (`object`): 字段值，至少要有 ``root``。

        Raises:
            `WorkspaceError`: ``root`` 缺省、或其字符串形式为空白。
        """
        raw_root = data.get("root")
        if raw_root is None:
            raise WorkspaceError("ReMeWorkspace 需要 root（工作区根目录）")
        if isinstance(raw_root, str) and not raw_root.strip():
            raise WorkspaceError(
                "ReMeWorkspace.root 不能为空/纯空白：空字符串会被 ``Path('')`` 悄悄解释成当前目录，"
                "使工作区绑定到 cwd，destroy() 时可能删掉不该删的东西。",
            )
        super().__init__(**data)  # type: ignore[arg-type]
        raw = str(self.root).strip()
        if not raw:  # pragma: no cover - 上面的前置检查已经覆盖
            raise WorkspaceError("ReMeWorkspace.root 不能为空")
        # 已知坑：/tmp → /private/tmp。必须 resolve()，否则后续 relative_to 会误判越界。
        object.__setattr__(self, "root", Path(raw).expanduser().resolve())

    @field_validator("metadata_dir", "session_dir", "mem_session_dir", "resource_dir", "daily_dir", "digest_dir")
    @classmethod
    def _validate_subdir(cls, value: str) -> str:
        """子目录必须是**单个**相对路径片段，不许 ``..``、不许绝对路径。

        Args:
            value (`str`): 待校验的目录名。

        Returns:
            `str`: 校验通过的目录名。

        Raises:
            `ValueError`: 空、绝对路径、含 ``..``、或含路径分隔符之外的越界成分。
        """
        name = value.strip()
        if not name:
            raise ValueError("子目录名不能为空")
        pure = Path(name)
        if pure.is_absolute() or name != pure.as_posix():
            raise ValueError(f"子目录必须是工作区内的相对路径片段: {value!r}")
        if any(part in ("", ".", "..") for part in pure.parts) or len(pure.parts) != 1:
            raise ValueError(f"子目录不能包含 '.' / '..' / 多级路径: {value!r}")
        return name

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    def subdir(self, field_name: str) -> Path:
        """按 :data:`DEFAULT_SUBDIRS` 的字段名取子目录。

        Args:
            field_name (`str`): ``metadata_dir`` / ``session_dir`` / ... 之一。

        Returns:
            `Path`: 绝对路径（未创建）。

        Raises:
            `WorkspaceError`: 字段名不在 :data:`DEFAULT_SUBDIRS` 里。
        """
        known = {name for name, _ in DEFAULT_SUBDIRS}
        if field_name not in known:
            raise WorkspaceError(f"未知的工作区子目录字段 {field_name!r}；可选: {sorted(known)}")
        return self.root / getattr(self, field_name)

    def metadata_path(self) -> Path:
        """``metadata/``：组件元数据（BM25 索引、file_store 快照）。

        Returns:
            `Path`: 绝对路径。
        """
        return self.subdir("metadata_dir")

    def session_path(self) -> Path:
        """``session/``：Agent 会话。

        注意 ReMe 的对话原文落在 ``session/dialog/<session_id>.jsonl``
        （``third_party/ReMe/reme/steps/evolve/auto_memory.py:73``），
        所以这里额外提供 :meth:`dialog_path`。

        Returns:
            `Path`: 绝对路径。
        """
        return self.subdir("session_dir")

    def mem_session_path(self) -> Path:
        """``mem_session/``：记忆会话。

        Returns:
            `Path`: 绝对路径。
        """
        return self.subdir("mem_session_dir")

    def resource_path(self) -> Path:
        """``resource/``：原始资源（摄入的文档与图片）。

        Returns:
            `Path`: 绝对路径。
        """
        return self.subdir("resource_dir")

    def daily_path(self) -> Path:
        """``daily/``：日记忆根目录。

        Returns:
            `Path`: 绝对路径。
        """
        return self.subdir("daily_dir")

    def digest_path(self) -> Path:
        """``digest/``：摘要目录。

        Returns:
            `Path`: 绝对路径。
        """
        return self.subdir("digest_dir")

    def dialog_path(self) -> Path:
        """``session/dialog/``：ReMe 存对话原始 jsonl 的位置。

        Returns:
            `Path`: 绝对路径。
        """
        return self.session_path() / "dialog"

    def all_paths(self) -> list[Path]:
        """六个子目录的绝对路径，顺序与 :data:`DEFAULT_SUBDIRS` 一致。

        Returns:
            `list[Path]`: 六个绝对路径。
        """
        return [self.subdir(name) for name, _ in DEFAULT_SUBDIRS]

    def dir_overrides(self) -> dict[str, str]:
        """返回可直接喂给 ``resolve_app_config(**overrides)`` 的目录字段。

        Returns:
            `dict[str, str]`: ``{"metadata_dir": ..., "session_dir": ..., ...}``。
        """
        return {name: getattr(self, name) for name, _ in DEFAULT_SUBDIRS}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def ensure(self) -> None:
        """幂等创建根目录与六个子目录。

        Raises:
            `WorkspaceError`: 某个目标路径存在但不是目录，或创建后仍不可写。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise WorkspaceError(f"工作区根不是目录: {self.root}")
        for path in self.all_paths():
            if path.exists() and not path.is_dir():
                raise WorkspaceError(f"工作区子目录被同名文件占用: {path}")
            path.mkdir(parents=True, exist_ok=True)
        self.require_writable()

    def validate(self) -> list[str]:
        """校验工作区结构，返回问题清单（空列表 = 健康）。

        只做只读检查，**不创建**任何目录，因此可以安全地跑在体检里。

        Returns:
            `list[str]`: 人类可读的问题描述；全部正常时为空。
        """
        problems: list[str] = []
        if not self.root.exists():
            problems.append(f"工作区根不存在: {self.root}")
            return problems
        if not self.root.is_dir():
            problems.append(f"工作区根不是目录: {self.root}")
            return problems
        if not os.access(self.root, os.W_OK):
            problems.append(f"工作区根不可写: {self.root}")
        for path in self.all_paths():
            if not path.exists():
                problems.append(f"缺少子目录: {path.relative_to(self.root).as_posix()}")
            elif not path.is_dir():
                problems.append(f"子目录被同名文件占用: {path.relative_to(self.root).as_posix()}")
        return problems

    def is_healthy(self) -> bool:
        """结构是否完好（:meth:`validate` 返回空）。

        Returns:
            `bool`: 完好为 ``True``。
        """
        return not self.validate()

    def require_writable(self) -> None:
        """写一个探针文件验证可写，然后删掉。

        Raises:
            `WorkspaceError`: 建不出探针文件。
        """
        probe = self.root / f".harness_probe_{os.getpid()}"
        try:
            probe.write_text("ok", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 权限问题
            raise WorkspaceError(f"工作区不可写: {self.root} ({exc})") from exc
        finally:
            probe.unlink(missing_ok=True)

    def clean(
        self,
        *,
        keep: Literal["all", "index", "none"] = "index",
        keep_session: bool = False,
        max_age_days: int | None = None,
    ) -> WorkspaceCleanReport:
        """清理工作区里的派生文件（幂等、可解释）。

        保护名单按 ``keep`` 决定：

        ========== ====================================================
        keep       保留什么
        ========== ====================================================
        ``all``    什么都不删（只统计，纯 dry-run）
        ``index``  ``metadata/``（索引与组件快照）+ ``daily/``/``digest/``
                   的**记忆正文**；只删临时文件、``__pycache__``、
                   ``*.tmp``、``*.lock``、``*.log``
        ``none``   只保留六个目录骨架本身，里面全部清空
        ========== ====================================================

        ``session/dialog/*.jsonl``（对话原文）的保护规则单独说，因为它是**唯一
        不可再生**的数据（daily/digest 的记忆卡都是从它加工出来的）：

        * ``keep="index"``（默认）：对话原文一并保留，只删垃圾文件。
        * ``keep="none"``：默认会连对话原文一起删；要留住它必须**显式**传
          ``keep_session=True``。所以 "清空工作区但保住原文" 的正确写法是
          ``clean(keep="none", keep_session=True)``。

        实测（本讲验证脚本 B 段）：``keep="index"`` 下
        ``session/dialog/s1.jsonl`` 出现在 ``report.kept`` 里；``keep="none"``
        下它会出现在 ``removed_files`` 里，并且随后那个空掉的 ``dialog/``
        目录也会被顺手删掉（``removed_dirs``）。

        Args:
            keep (`Literal["all", "index", "none"]`): 保留级别，默认 ``"index"``。
            keep_session (`bool`): ``False`` 时在 ``keep="none"`` 下连会话原文一起删。
            max_age_days (`int | None`): 只清理修改时间早于 N 天的文件；``None`` 表示不限。

        Returns:
            `WorkspaceCleanReport`: 删了什么、回收了多少字节。

        Raises:
            `WorkspaceError`: 工作区根不存在。
        """
        if not self.root.is_dir():
            raise WorkspaceError(f"工作区根不存在: {self.root}")

        protected_roots: set[Path] = set()
        if keep == "all":
            protected_roots = {self.root}
        elif keep == "index":
            protected_roots = {self.metadata_path(), self.daily_path(), self.digest_path()}
            if keep_session:
                protected_roots.add(self.session_path())

        report = WorkspaceCleanReport()
        cutoff = None
        if max_age_days is not None:
            import time

            cutoff = time.time() - max_age_days * 86400

        for path in sorted(self.root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            rel = path.relative_to(self.root).as_posix()
            if path.is_dir():
                if path in self.all_paths():
                    continue
                try:
                    if not any(path.iterdir()):
                        path.rmdir()
                        report.removed_dirs.append(rel)
                except OSError:  # pragma: no cover - 并发删除
                    pass
                continue

            if cutoff is not None and path.stat().st_mtime >= cutoff:
                continue
            if self._is_protected(path, protected_roots, keep_session, keep):
                report.kept.append(rel)
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:  # pragma: no cover - 权限问题
                logger.warning("清理 {} 失败: {}", rel, exc)
                continue
            report.removed_files.append(rel)
            report.bytes_freed += size

        logger.info(
            "工作区清理完成 root={} keep={} files={} dirs={} freed={}B kept={}",
            self.root,
            keep,
            len(report.removed_files),
            len(report.removed_dirs),
            report.bytes_freed,
            len(report.kept),
        )
        return report

    @staticmethod
    def _is_protected(path: Path, protected_roots: set[Path], keep_session: bool, keep: str) -> bool:
        """判断一个文件是否落在保护名单里。

        Args:
            path (`Path`): 待判断的绝对路径。
            protected_roots (`set[Path]`): ``keep`` 推导出的保护目录。
            keep_session (`bool`): 是否保留会话原文。
            keep (`str`): :meth:`clean` 的保留级别。

        Returns:
            `bool`: 受保护为 ``True``。
        """
        if keep == "index":
            # 只删垃圾，记忆正文与索引都留
            return not (
                path.name.endswith((".tmp", ".lock", ".log", ".pyc"))
                or "__pycache__" in path.parts
                or path.name.startswith(".harness_probe_")
            )
        for root in protected_roots:
            if root == path or root in path.parents:
                return True
        if keep == "none" and not keep_session and "dialog" in path.parts:
            return False
        if keep == "none":
            return "dialog" in path.parts or "session" in path.parts
        return False

    def destroy(self, *, confirm: bool = False) -> None:
        """整目录删除工作区（危险操作，必须显式确认）。

        Args:
            confirm (`bool`): 必须为 ``True``，否则抛错。这是刻意的防手滑设计。

        Raises:
            `WorkspaceError`: ``confirm`` 不为 ``True``，或根路径看起来像系统目录。
        """
        if not confirm:
            raise WorkspaceError("destroy() 需要 confirm=True；这是不可逆操作")
        resolved = self.root
        if resolved == Path(resolved.anchor) or len(resolved.parts) <= 2:
            raise WorkspaceError(f"拒绝删除看起来像系统目录的路径: {resolved}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
            logger.warning("已删除 ReMe 工作区: {}", resolved)

    # ------------------------------------------------------------------
    # 路径换算
    # ------------------------------------------------------------------
    def is_inside(self, path: str | Path) -> bool:
        """``path`` 是否落在工作区内（符号链接已解析）。

        **相对路径的语义**：非绝对路径一律按"工作区相对路径"理解 ——
        只要不含 ``..`` 就算在区内。原因见 :meth:`relative`。

        Args:
            path (`str | Path`): 任意路径。

        Returns:
            `bool`: 在工作区内为 ``True``。
        """
        candidate_path = Path(path).expanduser()
        if not candidate_path.is_absolute():
            return ".." not in PurePosixPath(str(path).replace("\\", "/")).parts
        try:
            candidate = candidate_path.resolve()
        except OSError:  # pragma: no cover - 断裂的符号链接
            return False
        return candidate == self.root or self.root in candidate.parents

    def relative(self, path: str | Path) -> str:
        """换算成工作区相对 POSIX 路径；不在工作区内则返回绝对路径。

        返回值与 ``ComponentMixin.to_workspace_relative``
        （``third_party/ReMe/reme/components/base_component.py:44-50``）
        **对绝对路径**完全一致：区内给相对、区外给绝对。

        **对相对路径则是刻意不同的一处**：ReMe 的实现用 ``Path.absolute()``
        （不解析符号链接、且以 CWD 为基准）再 ``relative_to``，
        于是 ``to_workspace_relative("resource/x.md")`` 会得到一个基于 CWD 的
        绝对路径。harness 侧不能照抄，因为 :meth:`relative` 的**输出会被喂回自己**：
        检索结果里的路径要拿去查 :meth:`~harness_kit.memory.search.MemorySearch.tags_for_path`、
        拿去 :meth:`resolve_relative`、拿去写给模型看。只要"相对进、相对出"这条
        幂等性破了，整条链路就会把 ``resource/x.md`` 变成一个满是 CWD 前缀的
        `/private/tmp/...` 绝对路径，而 tag_index 会直接拒绝它
        （``local_tag_index.py:64-71`` 的 ``_validate_path``）。
        这个 bug 真实存在过，就是这么被抓到的。

        Args:
            path (`str | Path`): 任意路径。

        Returns:
            `str`: 相对路径或绝对 POSIX 路径。
        """
        raw = str(path).replace("\\", "/")
        candidate_path = Path(path).expanduser()
        if not candidate_path.is_absolute():
            posix = PurePosixPath(raw)
            if ".." not in posix.parts:
                # 已经是工作区相对路径：归一化后原样返回（幂等）。
                return posix.as_posix()
        candidate = candidate_path.resolve()
        if self.is_inside(candidate):
            return candidate.relative_to(self.root).as_posix()
        return candidate.as_posix()

    def resolve_relative(self, rel: str | Path) -> Path:
        """把工作区相对路径还原成绝对路径，并拦住越界。

        Args:
            rel (`str | Path`): 工作区相对路径。

        Returns:
            `Path`: 绝对路径（未创建）。

        Raises:
            `WorkspaceError`: ``rel`` 逃出了工作区根。
        """
        candidate = (self.root / str(rel)).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        if not self.is_inside(candidate):
            raise WorkspaceError(f"路径越界，拒绝访问: {rel!r} -> {candidate}")
        return candidate

    def iter_files(self, *, suffix: str | None = ".md") -> Iterator[Path]:
        """遍历工作区内的文件（跳过 ``metadata/`` 与隐藏文件）。

        Args:
            suffix (`str | None`): 只要这个后缀；``None`` 表示不限。

        Yields:
            `Path`: 绝对路径，按字典序。
        """
        metadata = self.metadata_path()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if metadata == path or metadata in path.parents:
                continue
            if path.name.startswith("."):
                continue
            if suffix is not None and path.suffix != suffix:
                continue
            yield path

    def stats(self) -> dict[str, object]:
        """统计工作区规模，供体检与指标上报。

        Returns:
            `dict[str, object]`: ``files`` / ``bytes`` / ``cards`` / ``sessions``
            四个计数。
        """
        files = 0
        total = 0
        for path in self.iter_files(suffix=None):
            files += 1
            try:
                total += path.stat().st_size
            except OSError:  # pragma: no cover - 并发删除
                continue
        daily = self.daily_path()
        cards = sum(1 for _ in daily.rglob("*.md")) if daily.is_dir() else 0
        dialog = self.dialog_path()
        sessions = sum(1 for _ in dialog.glob("*.jsonl")) if dialog.is_dir() else 0
        return {"files": files, "bytes": total, "cards": cards, "sessions": sessions}

    def fingerprint(self) -> str:
        """工作区根路径的稳定指纹（多租户指标打标签用）。

        Returns:
            `str`: ``sha256(str(root))[:16]``。
        """
        return hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]

    def __str__(self) -> str:
        """返回人类可读的工作区标识。

        Returns:
            `str`: ``"<root>"`` 的绝对路径字符串。
        """
        return str(self.root)
