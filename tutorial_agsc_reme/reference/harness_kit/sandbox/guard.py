# -*- coding: utf-8 -*-
"""路径逃逸检测（契约 §3.10，第 10 讲）。

**为什么这件事不能靠字符串前缀比较**

两个真实存在的坑，只靠 ``str.startswith`` 一个都躲不过：

1. **符号链接**。macOS 上 ``/tmp`` 是 ``/private/tmp`` 的符号链接。
   ``/tmp/ws/../../../etc/passwd`` 这类路径在字符串层面看不出越界，
   而 :func:`os.path.realpath` 一解就原形毕露。
   AgentScope 自己也踩过这个坑并留下了注释：
   ``third_party/agentscope/src/agentscope/tool/_base.py:398``
   （"Paths are compared via :func:`os.path.realpath` so that aliases like
   macOS's ``/tmp`` → ``/private/tmp`` ... compare equal on both sides"）。
2. **前缀伪装**。``/tmp/workspace-evil`` 的字符串以 ``/tmp/workspace`` 开头，
   但它显然不在 ``/tmp/workspace`` 里。所以比较必须带上分隔符
   （``Path.is_relative_to`` / ``os.path.relpath`` 都处理了这点）。

本模块把这两件事一次性封装成 :class:`PathGuard`，并被
:mod:`harness_kit.sandbox.policy` 与 :mod:`harness_kit.sandbox.local` 共用。

**另外一件事：模式匹配（``deny_paths``）**

``SandboxPolicy.deny_paths`` 里写的是 ``".git/"`` / ``".env"`` / ``"**/*.pem"``
这种**模式**，不是精确路径。:meth:`PathGuard.matches_patterns` 规定了它们的匹配语义
（见该方法 docstring）—— 关键在于"模式列表是黑名单，多拦一点是安全的，
漏拦才是事故"，所以匹配规则刻意偏保守。
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

__all__ = [
    "PathEscapeError",
    "PathGuard",
]


class PathEscapeError(PermissionError):
    """路径逃逸出策略允许的范围（契约 §3.10）。

    继承 :class:`PermissionError` 而不是自定义的 ``Exception``：
    调用方常常已经有"捕获 ``PermissionError`` 就当成拒绝"的兜底逻辑，
    逃逸检测必须落在同一条路径上，否则会绕过它。
    """


class PathGuard:
    """把一个根目录变成"只能进不能出"的路径守卫（契约 §3.10）。

    Example:
        >>> guard = PathGuard(Path("/private/tmp/ws"))
        >>> guard.is_within("/private/tmp/ws/a.py")
        True
        >>> guard.is_within("/private/tmp/ws-evil/a.py")
        False
        >>> guard.is_within("/private/tmp/ws/../../etc/passwd")
        False
    """

    def __init__(self, root: Path) -> None:
        """构造守卫。

        Args:
            root (`Path`): 允许范围的根目录。构造时立即 ``resolve()``，
                因此符号链接在此刻就被解开，后续每次比较都不需要重复解。
        """
        self.root: Path = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def is_within(self, path: str | Path) -> bool:
        """判断路径是否落在根目录内（含根目录自身）。

        Args:
            path (`str | Path`): 待判断的路径；可以是绝对路径，也可以是
                相对路径（**相对当前进程的 cwd**，与 :func:`os.path.realpath`
                的语义一致；相对路径不会被当成"相对根目录"）。

        Returns:
            `bool`: 是否在范围内。

        Example:
            >>> PathGuard(Path("/private/tmp/ws")).is_within("/private/tmp/ws")
            True
            >>> PathGuard(Path("/private/tmp/ws")).is_within("/private/tmp")
            False
        """
        candidate = self._resolve(path)
        return candidate == self.root or self.root in candidate.parents

    def resolve_within(self, path: str | Path) -> Path:
        """把路径解析成绝对路径，越界则抛异常（契约 §3.10）。

        Args:
            path (`str | Path`): 待解析的路径。

        Returns:
            `Path`: ``realpath`` 之后的绝对路径。

        Raises:
            PathEscapeError: 路径逃逸出根目录。

        Example:
            >>> PathGuard(Path("/private/tmp/ws")).resolve_within("/private/tmp/ws/a.py")
            PosixPath('/private/tmp/ws/a.py')
        """
        candidate = self._resolve(path)
        if not (candidate == self.root or self.root in candidate.parents):
            raise PathEscapeError(
                f"路径逃逸：{path!r} 解析为 {candidate}，"
                f"不在工作区 {self.root} 之内",
            )
        return candidate

    @staticmethod
    def _resolve(path: str | Path) -> Path:
        """``expanduser`` + ``realpath`` + 绝对化。

        用 :func:`os.path.realpath` 而不是 :meth:`Path.resolve`：
        前者对**不存在的路径**也照样做符号链接解析（``strict=False`` 的
        ``Path.resolve`` 在 Python 3.6+ 行为接近，但 ``realpath`` 在
        "父目录存在、叶子不存在"这种最常见的情形下行为最可预期 ——
        写文件时目标文件本来就不存在）。

        Args:
            path (`str | Path`): 待解析路径。

        Returns:
            `Path`: 绝对化且解开符号链接的路径。
        """
        return Path(os.path.realpath(os.path.expanduser(str(path))))

    # ------------------------------------------------------------------
    # 相对路径与模式匹配
    # ------------------------------------------------------------------
    def relative_to_root(self, path: str | Path) -> str:
        """把路径转成相对根目录的 POSIX 字符串（用于打日志 / 模式匹配）。

        Args:
            path (`str | Path`): 待转换路径。

        Returns:
            `str`: 相对路径（POSIX 分隔符）；路径就在根目录时返回 ``"."``。

        Raises:
            PathEscapeError: 路径逃逸出根目录。
        """
        candidate = self.resolve_within(path)
        return os.path.relpath(candidate, self.root).replace(os.sep, "/")

    def matches_patterns(self, path: str | Path, patterns: list[str]) -> str | None:
        """判断路径是否命中任一模式，命中则返回命中的那个模式。

        **匹配语义**（四种任一命中即算命中）：

        1. 模式与**相对根目录的路径**直接 fnmatch（``"src/*.py"``）；
        2. 模式与**文件名** fnmatch（``"*.pem"`` 命中 ``a/b/k.pem``）；
        3. 模式与路径的**任一段** fnmatch（``".git/"`` 命中 ``src/.git/config``）；
        4. 把模式开头的 ``**/`` 去掉再按 1 试一次（``"**/*.pem"`` 命中根下的 ``k.pem``）。

        第 3、4 条是对 glob 的**放宽**，刻意的：``deny_paths`` 是黑名单，
        放宽只会多拦、不会漏拦。真正需要精确语义的场景应当用第 1 条那种
        写全的相对 glob。

        Args:
            path (`str | Path`): 待判断路径。
            patterns (`list[str]`): 模式列表。

        Returns:
            `str | None`: 命中的模式，或 ``None``。

        Raises:
            PathEscapeError: 路径逃逸出根目录（连相对路径都算不出来）。
        """
        rel = self.relative_to_root(path)
        segments = rel.split("/")
        basename = segments[-1] if segments else rel

        for pattern in patterns:
            if not pattern:
                continue
            normalized = pattern.replace(os.sep, "/")
            candidates = {
                normalized,
                normalized.rstrip("/"),
                normalized[3:] if normalized.startswith("**/") else normalized,
            }
            for candidate in candidates:
                if not candidate:
                    continue
                if fnmatch.fnmatch(rel, candidate):
                    return pattern
                if fnmatch.fnmatch(basename, candidate):
                    return pattern
            stripped = normalized.rstrip("/")
            if any(fnmatch.fnmatch(segment, stripped) for segment in segments):
                return pattern
        return None

    # ------------------------------------------------------------------
    # 描述
    # ------------------------------------------------------------------
    def describe(self) -> dict[str, str]:
        """返回自述（日志 / CLI 用）。

        Returns:
            `dict[str, str]`: 根目录信息。
        """
        return {
            "root": str(self.root),
            "is_symlink_resolved": str(self.root == Path(os.path.realpath(str(self.root)))),
        }

    def __repr__(self) -> str:
        """调试表示。

        Returns:
            `str`: 形如 ``PathGuard(root=/private/tmp/ws)``。
        """
        return f"PathGuard(root={self.root})"
