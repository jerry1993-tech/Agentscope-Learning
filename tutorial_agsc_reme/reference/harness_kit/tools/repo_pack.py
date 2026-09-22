# -*- coding: utf-8 -*-
"""``RepoToolPack`` —— 面向代码仓库的高阶工具包。

**它跟 ``builtin_pack`` 的分工**：``builtin`` 给的是「文件与命令」的**原子**
能力（读一个文件、跑一条命令）；``repo`` 给的是「仓库」层面的**复合**能力
（这个仓库有哪些文件、按语义搜代码、精确替换一段文本、跑测试并汇总结果）。
有了它，模型不必把 5 条 ``Bash`` 拼成一条才能回答「测试过了吗」。

四个工具都是 ``FunctionTool``，都**跑在 ``BackendBase`` 抽象上**
（``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:138``），
因此换成 Docker backend 就能整包搬进沙箱，不需要改一行工具代码 —— 这正是
「不重写工具、只做组合」的价值。

====================== ====================================================
工具                    为什么它不是 AgentScope 已有的某个工具
====================== ====================================================
``RepoTree``           ``Glob`` 是**按模式匹配文件**；仓库概览需要的是
                       「一次列出全部受版本控制的文件并给出结构」。
``RepoSearch``         ``Grep`` 面向**文件内容**且输出很长；仓库搜索需要
                       **分组 + 限量 + 带行号**的紧凑结果。
``RepoReplace``        ``Edit`` 用「片段匹配」，同一片段出现多次时行为取决于
                       实现细节；精确替换要求「出现次数必须等于期望值」，
                       不满足就**拒绝写入**。
``RunTests``           没有任何内置工具跑测试。这是代码 Agent 最频繁的动作。
====================== ====================================================

**为什么 ``RepoReplace`` 要检查出现次数**：这是本模块最重要的一个设计。
``Edit`` 的语义是「找到这段就换成那段」，如果 ``old_text`` 在文件里出现 3 次，
你换掉了第 1 次，工具报成功，但模型以为全换完了 —— 静默的部分成功是最难查的
一类 bug。强制 ``expected_count`` 让「多义匹配」变成显式错误，模型必须提供更
长的上下文才能成功。

**为什么 ``RunTests`` 要解析输出**：把 3000 行 pytest 输出原样灌进上下文，
既贵又会淹没关键信息。这里返回结构化 JSON（exit_code / 通过失败数 / 失败用例
名 / 截断后的原输出），模型先看摘要，需要细节再自己 ``Read`` 日志。

**真实 API 锚点**：

- :meth:`BackendBase.exec_shell` / :meth:`read_file` / :meth:`write_file`：
  ``.../tool/_builtin/_backend.py:294`` / ``:331`` / ``:344``
- ``ExecResult(exit_code, stdout, stderr)``：``.../_backend.py:62``
- ``FunctionTool``：``.../tool/_adapters.py:36``
- 只读放行决策：``harness_kit.tools.builtin_pack.READ_ONLY_ALLOW``
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any, Sequence

from loguru import logger

from agentscope.tool import BackendBase, FunctionTool, LocalBackend, ToolBase

from harness_kit.tools.builtin_pack import READ_ONLY_ALLOW
from harness_kit.tools.pack import ToolPackBase, ToolPackManifest
from harness_kit.tools.utils import summarize_tool_result

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from harness_kit.config.schema import ToolsSpec

__all__ = [
    "RepoToolPack",
    "build_repo_pack",
]

_PYTEST_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors|skipped|xfailed|xpassed)",
)
"""pytest 摘要行里 ``12 passed`` / ``1 failed`` 这种片段的抓取器。"""

_PYTEST_FAILED_CASE_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<name>\S+)",
    re.MULTILINE,
)
"""``FAILED tests/test_x.py::test_y - AssertionError`` 里的用例名。"""

DEFAULT_TIMEOUT_S: float = 300.0
"""单个仓库命令的默认超时（秒）。

设它是**必须**的：``exec_shell(timeout=None)`` 会无限等待，而一个死循环的
测试或一个交互式的命令（``vim``、等 stdin 的脚本）会让整个 Agent 挂死。
超时后 ``ExecResult.exit_code`` 是 ``-1``（``.../_backend.py:64``）。
"""


class RepoToolPack(ToolPackBase):
    """仓库工具包。

    Args:
        backend (`BackendBase | None`): 执行后端。``None`` 时新建
            ``LocalBackend``。传 Docker backend 就能整包进沙箱。
        workdir (`str | None`): 仓库根目录；所有相对路径都相对它解析。
            ``None`` 时用 backend 的当前目录（``LocalBackend`` 下即进程 cwd）。
        test_command (`Sequence[str] | None`): 跑测试的命令前缀，
            默认 ``[sys.executable, "-m", "pytest"]``。用 ``sys.executable``
            而不是 ``"pytest"`` 是刻意的：``pytest`` 不一定在 PATH 上，
            而当前解释器一定装了（否则这个进程跑不起来）。
        timeout_s (`float`): 命令超时，默认 :data:`DEFAULT_TIMEOUT_S`。
        max_tree_entries (`int`): ``RepoTree`` 默认最多列多少个文件。
        max_search_results (`int`): ``RepoSearch`` 默认最多返回多少条。

    Raises:
        ImportError: ``sys.executable`` 不可用（嵌入式解释器场景）。
    """

    manifest: ToolPackManifest = ToolPackManifest(
        name="repo",
        version="0.1.0",
        description=(
            "仓库工具包：受版本控制文件概览、代码搜索、精确文本替换、"
            "跑测试并汇总结果。依赖 builtin 包提供的 backend 约定。"
        ),
        requires=["builtin"],
        # 同 builtin 包的理由（见 harness_kit/tools/builtin_pack.py 的说明）：
        # 只读的 RepoTree / RepoSearch 留在 basic 常驻，会改文件、会执行代码的
        # RepoReplace / RunTests 入组，由模型按需激活。
        groups={
            "repo_write": ["RepoReplace", "RunTests"],
        },
        tags=["repo", "code", "tests"],
        dangerous_tools=["RepoReplace", "RunTests"],
    )

    def __init__(
        self,
        *,
        backend: BackendBase | None = None,
        workdir: str | None = None,
        test_command: Sequence[str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_tree_entries: int = 200,
        max_search_results: int = 50,
    ) -> None:
        """记录配置；工具在 :meth:`build_tools` 里才建。"""
        super().__init__()
        self._backend = backend
        self.workdir = workdir
        self.test_command: list[str] = list(
            test_command or [sys.executable, "-m", "pytest"],
        )
        self.timeout_s = float(timeout_s)
        self.max_tree_entries = int(max_tree_entries)
        self.max_search_results = int(max_search_results)
        self._built: list[ToolBase] | None = None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _backend_or_new(self) -> BackendBase:
        """取注入的 backend，没有就新建 ``LocalBackend``。

        Returns:
            `BackendBase`: 后端。
        """
        if self._backend is None:
            self._backend = LocalBackend()
        return self._backend

    def _resolve(self, path: str) -> str:
        """把相对路径解析到仓库根下（绝对路径原样返回）。

        Args:
            path (`str`): 用户给的路径。

        Returns:
            `str`: 后端环境里的路径。
        """
        backend = self._backend_or_new()
        if backend.isabs(path):
            return path
        if self.workdir:
            return backend.join_path(self.workdir, path)
        return path

    def _dump(self, payload: dict[str, Any], max_chars: int) -> str:
        """序列化成工具结果文本并按上限截断。

        Args:
            payload (`dict[str, Any]`): 结构化结果。
            max_chars (`int`): 字符上限。

        Returns:
            `str`: 结果文本。
        """
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return summarize_tool_result(text, max_chars=max_chars)

    # ------------------------------------------------------------------
    # 工具工厂：每个工具一个闭包，带真实签名 + docstring
    # ------------------------------------------------------------------
    def _make_tree(self, max_chars: int) -> Any:
        """造 ``RepoTree`` 工具函数。

        Args:
            max_chars (`int`): 结果字符上限。

        Returns:
            `Any`: 异步工具函数。
        """
        pack = self

        async def RepoTree(  # noqa: N802 - 工具名即函数名，保持与 schema 一致
            path: str = ".",
            max_entries: int = 0,
            include_untracked: bool = False,
        ) -> str:
            """List the repository's files in one call.

            Prefer this over Glob when you need an overview of the codebase
            rather than files matching a pattern. Uses `git ls-files`, so
            ignored files are excluded; falls back to `find` outside a repo.

            Args:
                path (str): Subdirectory to list, relative to the repo root.
                    Defaults to the whole repo.
                max_entries (int): Cap on returned entries; 0 means use the
                    pack default.
                include_untracked (bool): Also list untracked (but not
                    ignored) files via `git ls-files --others
                    --exclude-standard`.
            """
            backend = pack._backend_or_new()  # noqa: SLF001
            limit = max_entries or pack.max_tree_entries
            target = pack._resolve(path)  # noqa: SLF001

            command = ["git", "ls-files"]
            if include_untracked:
                command = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
            result = await backend.exec_shell(
                command,
                cwd=target,
                timeout=pack.timeout_s,
            )
            source = "git ls-files"

            if not result.ok() or not result.stdout.strip():
                result = await backend.exec_shell(
                    [
                        "find",
                        ".",
                        "-type",
                        "f",
                        "-not",
                        "-path",
                        "./.git/*",
                    ],
                    cwd=target,
                    timeout=pack.timeout_s,
                )
                source = "find"

            if not result.ok():
                return pack._dump(  # noqa: SLF001
                    {
                        "ok": False,
                        "error": "无法列出仓库文件",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode(
                            "utf-8",
                            errors="replace",
                        )[:2000],
                    },
                    max_chars,
                )

            files = sorted(
                line.strip()
                for line in result.stdout.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            )
            return pack._dump(  # noqa: SLF001
                {
                    "ok": True,
                    "source": source,
                    "root": target,
                    "total": len(files),
                    "returned": min(len(files), limit),
                    "truncated": len(files) > limit,
                    "files": files[:limit],
                },
                max_chars,
            )

        return RepoTree

    def _make_search(self, max_chars: int) -> Any:
        """造 ``RepoSearch`` 工具函数。

        Args:
            max_chars (`int`): 结果字符上限。

        Returns:
            `Any`: 异步工具函数。
        """
        pack = self

        async def RepoSearch(  # noqa: N802 - 工具名即函数名，保持与 schema 一致
            pattern: str,
            path: str = ".",
            file_glob: str = "",
            max_results: int = 0,
            case_sensitive: bool = False,
        ) -> str:
            """Search the codebase and get compact, grouped matches.

            Prefer this over Grep when you want a token-efficient, grouped
            result. Uses `git grep -n -E`; falls back to `grep -rn` outside
            a repository.

            Args:
                pattern (str): Extended regular expression to search for.
                path (str): Subdirectory or file to search, relative to the
                    repo root. Defaults to the whole repo.
                file_glob (str): Optional pathspec/glob filter, e.g. `*.py`.
                max_results (int): Cap on returned matches; 0 means use the
                    pack default.
                case_sensitive (bool): Match case when True (default False).
            """
            backend = pack._backend_or_new()  # noqa: SLF001
            limit = max_results or pack.max_search_results
            target = pack._resolve(path)  # noqa: SLF001

            command = ["git", "grep", "-n", "-E"]
            if not case_sensitive:
                command.append("-i")
            command.append(pattern)
            if file_glob:
                command.extend(["--", file_glob])
            result = await backend.exec_shell(
                command,
                cwd=target,
                timeout=pack.timeout_s,
            )
            source = "git grep"

            # git grep 退出码 1 = 没有匹配（不是错误）；128 = 不在仓库里
            if result.exit_code == 128 or (
                result.exit_code != 0 and result.exit_code != 1
            ):
                fallback = ["grep", "-rn", "-E"]
                if not case_sensitive:
                    fallback.append("-i")
                if file_glob:
                    fallback.extend(["--include", file_glob])
                fallback.extend([pattern, "."])
                result = await backend.exec_shell(
                    fallback,
                    cwd=target,
                    timeout=pack.timeout_s,
                )
                source = "grep -rn"

            if result.exit_code not in (0, 1):
                return pack._dump(  # noqa: SLF001
                    {
                        "ok": False,
                        "error": "搜索失败",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode(
                            "utf-8",
                            errors="replace",
                        )[:2000],
                    },
                    max_chars,
                )

            lines = [
                line
                for line in result.stdout.decode(
                    "utf-8",
                    errors="replace",
                ).splitlines()
                if line.strip()
            ]
            by_file: dict[str, list[str]] = {}
            for line in lines:
                file_part, _, rest = line.partition(":")
                by_file.setdefault(file_part, []).append(rest)

            return pack._dump(  # noqa: SLF001
                {
                    "ok": True,
                    "source": source,
                    "pattern": pattern,
                    "root": target,
                    "total_matches": len(lines),
                    "file_count": len(by_file),
                    "truncated": len(lines) > limit,
                    "matches": [
                        {"file": file_name, "hits": hits[:limit]}
                        for file_name, hits in list(by_file.items())[:limit]
                    ],
                },
                max_chars,
            )

        return RepoSearch

    def _make_replace(self, max_chars: int) -> Any:
        """造 ``RepoReplace`` 工具函数。

        Args:
            max_chars (`int`): 结果字符上限。

        Returns:
            `Any`: 异步工具函数。
        """
        pack = self

        async def RepoReplace(  # noqa: N802 - 工具名即函数名，保持与 schema 一致
            path: str,
            old_text: str,
            new_text: str,
            expected_count: int = 1,
        ) -> str:
            """Replace an EXACT piece of text in a file, atomically.

            Unlike Edit, this tool counts occurrences first and REFUSES to
            write unless the count equals `expected_count`. That turns an
            ambiguous match into a loud error instead of a silent partial
            replacement. Include enough surrounding context in `old_text` to
            make it unique.

            Args:
                path (str): File to modify, relative to the repo root.
                old_text (str): The exact text to replace, including
                    whitespace and indentation.
                new_text (str): The replacement text.
                expected_count (int): How many times `old_text` must occur.
                    The write only happens when the actual count matches.
            """
            backend = pack._backend_or_new()  # noqa: SLF001
            target = pack._resolve(path)  # noqa: SLF001

            if not old_text:
                return pack._dump(  # noqa: SLF001
                    {"ok": False, "error": "old_text 不能为空"},
                    max_chars,
                )
            if old_text == new_text:
                return pack._dump(  # noqa: SLF001
                    {"ok": False, "error": "old_text 与 new_text 相同，无需替换"},
                    max_chars,
                )

            try:
                raw = await backend.read_file(target)
            except (FileNotFoundError, OSError) as exc:
                return pack._dump(  # noqa: SLF001
                    {"ok": False, "error": f"读取失败：{type(exc).__name__}: {exc}"},
                    max_chars,
                )

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return pack._dump(  # noqa: SLF001
                    {
                        "ok": False,
                        "error": "文件不是 UTF-8 文本，拒绝改写（避免损坏二进制文件）",
                    },
                    max_chars,
                )

            actual = text.count(old_text)
            if actual != expected_count:
                return pack._dump(  # noqa: SLF001
                    {
                        "ok": False,
                        "error": (
                            f"old_text 实际出现 {actual} 次，期望 {expected_count} 次，"
                            "已拒绝写入。请把 old_text 扩展到唯一的上下文，"
                            "或修正 expected_count。"
                        ),
                        "actual_count": actual,
                        "expected_count": expected_count,
                    },
                    max_chars,
                )

            updated = text.replace(old_text, new_text, expected_count)
            await backend.write_file(target, updated.encode("utf-8"))
            logger.info(
                "RepoReplace 改写 {}（{} 处，{} -> {} 字符）",
                target,
                actual,
                len(text),
                len(updated),
            )
            return pack._dump(  # noqa: SLF001
                {
                    "ok": True,
                    "path": target,
                    "replaced": actual,
                    "chars_before": len(text),
                    "chars_after": len(updated),
                },
                max_chars,
            )

        return RepoReplace

    def _make_run_tests(self, max_chars: int) -> Any:
        """造 ``RunTests`` 工具函数。

        Args:
            max_chars (`int`): 结果字符上限。

        Returns:
            `Any`: 异步工具函数。
        """
        pack = self

        async def RunTests(  # noqa: N802 - 工具名即函数名，保持与 schema 一致
            test_path: str = "",
            extra_args: str = "",
            keyword: str = "",
        ) -> str:
            """Run the project's test suite and get a structured summary.

            Returns exit code, pass/fail counts, the names of failing tests,
            and the truncated raw output. Prefer this over running pytest
            through Bash — the raw output is thousands of lines.

            Args:
                test_path (str): Specific test file or directory to run.
                    Empty means the whole suite.
                extra_args (str): Extra pytest flags, space separated,
                    e.g. `-x -q`.
                keyword (str): Passed as `-k <keyword>` to select tests by
                    name substring.
            """
            backend = pack._backend_or_new()  # noqa: SLF001
            command = list(pack.test_command)
            if keyword:
                command.extend(["-k", keyword])
            if extra_args:
                command.extend(extra_args.split())
            if test_path:
                command.append(test_path)

            logger.debug("RunTests 执行 {}", command)
            result = await backend.exec_shell(
                command,
                cwd=pack.workdir,
                timeout=pack.timeout_s,
            )

            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            combined = stdout + ("\n" + stderr if stderr else "")

            counts: dict[str, int] = {}
            for match in _PYTEST_SUMMARY_RE.finditer(combined):
                kind = match.group("kind").replace("errors", "error")
                counts[kind] = counts.get(kind, 0) + int(match.group("count"))

            failed_cases = _PYTEST_FAILED_CASE_RE.findall(combined)[:20]

            if result.exit_code == -1:
                return pack._dump(  # noqa: SLF001
                    {
                        "ok": False,
                        "error": f"测试超时（>{pack.timeout_s:.0f}s）或内部失败",
                        "exit_code": result.exit_code,
                        "command": command,
                    },
                    max_chars,
                )

            return pack._dump(  # noqa: SLF001
                {
                    "ok": result.exit_code == 0,
                    "exit_code": result.exit_code,
                    "command": command,
                    "passed": counts.get("passed", 0),
                    "failed": counts.get("failed", 0),
                    "errors": counts.get("error", 0),
                    "skipped": counts.get("skipped", 0),
                    "failed_cases": failed_cases,
                    "output": summarize_tool_result(
                        combined.strip(),
                        max_chars=max(500, max_chars // 2),
                    ),
                },
                max_chars,
            )

        return RunTests

    # ------------------------------------------------------------------
    # 契约方法
    # ------------------------------------------------------------------
    async def build_tools(self, spec: "ToolsSpec") -> list[ToolBase]:
        """造出 4 个仓库工具。

        Args:
            spec (`ToolsSpec`): 工具声明；``max_result_chars`` 决定每个工具
                结果的截断上限。

        Returns:
            `list[ToolBase]`: 工具实例（幂等：重复调用返回同一批对象）。

        Raises:
            ValueError: ``max_result_chars`` 非正数。
        """
        if spec.max_result_chars <= 0:
            raise ValueError(
                f"ToolsSpec.max_result_chars 必须为正数，收到 {spec.max_result_chars}",
            )
        if self._built is not None:
            return list(self._built)

        max_chars = spec.max_result_chars
        self._built = [
            FunctionTool(
                self._make_tree(max_chars),
                name="RepoTree",
                is_read_only=True,
                is_concurrency_safe=True,
                permission=READ_ONLY_ALLOW,
            ),
            FunctionTool(
                self._make_search(max_chars),
                name="RepoSearch",
                is_read_only=True,
                is_concurrency_safe=True,
                permission=READ_ONLY_ALLOW,
            ),
            FunctionTool(
                self._make_replace(max_chars),
                name="RepoReplace",
                is_read_only=False,
                is_concurrency_safe=False,
            ),
            FunctionTool(
                self._make_run_tests(max_chars),
                name="RunTests",
                is_read_only=False,
                is_concurrency_safe=False,
            ),
        ]
        logger.bind(tools=[tool.name for tool in self._built]).debug(
            "repo 工具包已装配（workdir={}，test_command={}）",
            self.workdir,
            self.test_command,
        )
        return list(self._built)

    def build_context(self) -> dict[str, Any]:
        """对外暴露 backend / workdir，供其它包复用。

        Returns:
            `dict[str, Any]`: ``{"backend": ..., "workdir": ...}``。
        """
        return {
            "backend": self._backend_or_new(),
            "workdir": self.workdir,
        }

    def _group_description(self, name: str, members: Sequence[ToolBase]) -> str:
        """给 ``repo_write`` 组写描述。

        Args:
            name (`str`): 组名。
            members (`Sequence[ToolBase]`): 组内工具。

        Returns:
            `str`: 组描述。
        """
        return (
            f"{name}：会**改动仓库**的操作 —— 精确替换文件内容、运行测试"
            f"（会执行仓库里的代码）。确认要落盘改动或跑测试时激活本组。"
            f"（包含 {', '.join(tool.name for tool in members)}）"
        )


async def build_repo_pack(
    spec: "ToolsSpec",
    ctx: Any = None,
) -> list[ToolBase]:
    """Layer 0 直连工厂：``async (spec, ctx) -> list[ToolBase]``。

    这是 ``harness_kit.registry`` 里 ``"repo"`` 懒加载条目指向的函数
    （``attrs=("build_repo_pack", "RepoToolPack")``），签名必须与
    ``registry._builtin_tool_pack`` 一致。

    **它从 ``ctx`` 里取 ``workdir``**：仓库工具全部是「相对仓库根」的，
    没有正确的工作目录就等于没有用。

    Args:
        spec (`ToolsSpec`): 工具声明。
        ctx (`BuildContext | None`, optional): 装配上下文；读 ``workdir``。

    Returns:
        `list[ToolBase]`: 4 个仓库工具。
    """
    workdir: str | None = None
    if ctx is not None and getattr(ctx, "workdir", None) is not None:
        workdir = str(ctx.workdir)
    pack = RepoToolPack(workdir=workdir)
    return await pack.build_tools(spec)
