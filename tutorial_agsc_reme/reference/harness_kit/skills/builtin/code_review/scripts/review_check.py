#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_review 技能的配套脚本：机械性代码问题的静态扫描器。

**只用标准库**（``ast`` / ``argparse`` / ``json`` / ``pathlib``），因此任何
Python 3.11+ 解释器都能直接跑，不依赖 harness_kit 的虚拟环境。

它负责"能用规则说清楚"的那部分检查；语义问题交给模型的第 3 步精读。
两者是互补关系，脚本的每一个 finding 都带 ``file:line``，模型不需要猜位置。

用法：

.. code-block:: bash

    python review_check.py path/to/file.py
    python review_check.py ./src --json
    python review_check.py ./src --fail-on medium

退出码：0 = 未达 ``--fail-on`` 阈值；1 = 达到阈值；2 = 用法错误（路径不存在等）。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Sequence

__all__ = ["Finding", "check_source", "iter_python_files", "main"]

SEVERITY_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1}
"""严重度排序权重。``--fail-on`` 就是拿它做比较。"""

SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    },
)

_MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
_DANGEROUS_NAMES: frozenset[str] = frozenset({"eval", "exec", "compile"})


@dataclass(frozen=True)
class Finding:
    """一条扫描结果。"""

    rule: str
    """规则标识，例如 ``bare-except``。"""

    severity: str
    """``high`` / ``medium`` / ``low``。"""

    file: str
    """文件路径（相对于用户传入的路径）。"""

    line: int
    """1 起的行号。"""

    message: str
    """人能读懂的说明。"""

    snippet: str = ""
    """触发问题的原始代码行（去掉首尾空白）。"""

    def to_dict(self) -> dict[str, object]:
        """转成可 JSON 序列化的 dict。

        Returns:
            `dict[str, object]`: 字段字典。
        """
        return asdict(self)


def iter_python_files(target: Path) -> Iterator[Path]:
    """展开用户传入的路径。

    Args:
        target (`Path`): 文件或目录。

    Yields:
        `Path`: 待扫描的 ``.py`` 文件。

    Raises:
        FileNotFoundError: 路径不存在。
    """
    if not target.exists():
        raise FileNotFoundError(f"路径不存在: {target}")
    if target.is_file():
        yield target
        return
    for path in sorted(target.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _snippet(lines: Sequence[str], lineno: int) -> str:
    """取某一行原文。

    Args:
        lines (`Sequence[str]`): 文件按行切分的结果。
        lineno (`int`): 1 起的行号。

    Returns:
        `str`: 该行去掉首尾空白后的文本；越界时为空串。
    """
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def check_source(source: str, *, filename: str, max_function_lines: int = 60) -> list[Finding]:
    """对一段 Python 源码做扫描。

    Args:
        source (`str`): 源码文本。
        filename (`str`): 用于 finding 的文件名。
        max_function_lines (`int`): 函数超过多少行报 ``long-function``。

    Returns:
        `list[Finding]`: 命中的问题（未排序）。

    Raises:
        SyntaxError: 源码无法解析。调用方 :func:`scan_paths` 会把它转成一条
            ``syntax-error`` finding，而不是让整次扫描中断。
    """
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    findings: list[Finding] = []

    def add(rule: str, severity: str, node: ast.AST, message: str) -> None:
        """记录一条 finding。

        Args:
            rule (`str`): 规则名。
            severity (`str`): 严重度。
            node (`ast.AST`): 位置来源。
            message (`str`): 说明文本。
        """
        lineno = getattr(node, "lineno", 1)
        findings.append(
            Finding(
                rule=rule,
                severity=severity,
                file=filename,
                line=lineno,
                message=message,
                snippet=_snippet(lines, lineno),
            ),
        )

    # ---- 逐节点规则 ----
    for node in ast.walk(tree):
        # 1) 裸 except / except: pass
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add(
                    "bare-except",
                    "high",
                    node,
                    "裸 except 会连 KeyboardInterrupt / SystemExit 一起吞掉，"
                    "请指定具体异常类型",
                )
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                add(
                    "swallowed-exception",
                    "high",
                    node,
                    "except 块里只有 pass，异常被静默吞掉；至少 log 或重新抛出",
                )

        # 2) 可变默认参数
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    add(
                        "mutable-default-arg",
                        "high",
                        node,
                        f"函数 {node.name}() 的默认参数是可变容器，"
                        "会在多次调用间共享状态；改用 None 再在函数体内建",
                    )

        # 3) eval / exec
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DANGEROUS_NAMES:
                add(
                    "dangerous-builtin",
                    "high",
                    node,
                    f"使用了 {node.func.id}()，对不可信输入执行代码会直接导致 RCE",
                )

        # 4) SQL 字符串拼接
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = ast.unparse(node.left)
            if re.search(r"(SELECT|INSERT|UPDATE|DELETE|WHERE)\b", left, re.I):
                add(
                    "sql-string-concat",
                    "high",
                    node,
                    "疑似用字符串拼接构造 SQL；请改用参数化查询",
                )

        # 5) print 调试残留
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "print":
                add("print-call", "low", node, "生产代码里出现 print()，请换成 logger")

    # ---- 函数级规则 ----
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            length = end - node.lineno + 1
            if length > max_function_lines:
                add(
                    "long-function",
                    "medium",
                    node,
                    f"函数 {node.name}() 有 {length} 行（阈值 {max_function_lines}），"
                    "建议拆分为若干有名字的小函数",
                )
            if node.name.startswith("_") is False and node.returns is None:
                add(
                    "missing-return-annotation",
                    "medium",
                    node,
                    f"公共函数 {node.name}() 缺少返回类型注解",
                )
            returns_none = any(
                isinstance(sub, ast.Return) and sub.value is None
                for sub in ast.walk(node)
            )
            raises = any(isinstance(sub, ast.Raise) for sub in ast.walk(node))
            if returns_none and not raises:
                add(
                    "implicit-none-return",
                    "medium",
                    node,
                    f"函数 {node.name}() 有裸 return（返回 None）却没有任何 raise，"
                    "调用方无法区分'失败'与'没结果'",
                )

    # ---- 文本级规则（TODO / 超长行）----
    for index, text in enumerate(lines, start=1):
        marker = _MARKER_RE.search(text)
        if marker:
            findings.append(
                Finding(
                    rule="stale-marker",
                    severity="low",
                    file=filename,
                    line=index,
                    message=f"遗留 {marker.group(1)} 标记，确认是否已解决",
                    snippet=text.strip(),
                ),
            )
        if len(text) > 120 and "noqa" not in text and "#" not in text[:1]:
            findings.append(
                Finding(
                    rule="long-line",
                    severity="low",
                    file=filename,
                    line=index,
                    message=f"行长度 {len(text)} 超过 120 字符",
                    snippet=text.strip()[:80] + "...",
                ),
            )

    return findings


def _dedupe(findings: Sequence[Finding]) -> list[Finding]:
    """按 ``(rule, file, line)`` 去重并稳定排序。

    Args:
        findings (`Sequence[Finding]`): 原始结果。

    Returns:
        `list[Finding]`: 去重后按严重度降序、再按文件行号升序的结果。
    """
    unique: dict[tuple[str, str, int], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.rule, finding.file, finding.line), finding)
    return sorted(
        unique.values(),
        key=lambda f: (-SEVERITY_ORDER[f.severity], f.file, f.line),
    )


def scan_paths(
    paths: Sequence[Path],
    *,
    max_function_lines: int = 60,
) -> list[Finding]:
    """扫描多个路径。

    Args:
        paths (`Sequence[Path]`): 文件或目录。
        max_function_lines (`int`): 见 :func:`check_source`。

    Returns:
        `list[Finding]`: 去重排序后的结果。
    """
    findings: list[Finding] = []
    for target in paths:
        for path in iter_python_files(target):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                findings.append(
                    Finding(
                        rule="unreadable",
                        severity="medium",
                        file=str(path),
                        line=1,
                        message=f"无法以 UTF-8 读取: {exc}",
                    ),
                )
                continue
            try:
                findings.extend(
                    check_source(
                        source,
                        filename=str(path),
                        max_function_lines=max_function_lines,
                    ),
                )
            except SyntaxError as exc:
                findings.append(
                    Finding(
                        rule="syntax-error",
                        severity="high",
                        file=str(path),
                        line=exc.lineno or 1,
                        message=f"语法错误，文件无法解析: {exc.msg}",
                        snippet=(exc.text or "").strip(),
                    ),
                )
    return _dedupe(findings)


def _render_text(findings: Sequence[Finding]) -> str:
    """渲染成人读的文本报告。

    Args:
        findings (`Sequence[Finding]`): 扫描结果。

    Returns:
        `str`: 多行报告。
    """
    if not findings:
        return "未发现问题。"

    counters: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counters[finding.severity] += 1

    out: list[str] = [
        f"共 {len(findings)} 条："
        f"high={counters['high']} medium={counters['medium']} low={counters['low']}",
        "",
    ]
    for finding in findings:
        out.append(
            f"[{finding.severity.upper():6s}] {finding.file}:{finding.line} "
            f"({finding.rule}) {finding.message}",
        )
        if finding.snippet:
            out.append(f"           | {finding.snippet}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv (`Sequence[str] | None`): 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        `int`: 进程退出码。
    """
    parser = argparse.ArgumentParser(
        prog="review_check.py",
        description="code_review 技能的机械性问题扫描器（纯标准库）",
    )
    parser.add_argument("paths", nargs="+", help="要扫描的文件或目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--max-function-lines",
        type=int,
        default=60,
        help="函数超过多少行报 long-function（默认 60）",
    )
    parser.add_argument(
        "--fail-on",
        choices=["high", "medium", "low", "none"],
        default="high",
        help="达到该严重度即返回退出码 1（默认 high）",
    )
    args = parser.parse_args(argv)

    try:
        findings = scan_paths(
            [Path(p) for p in args.paths],
            max_function_lines=args.max_function_lines,
        )
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "findings": [f.to_dict() for f in findings],
                    "count": len(findings),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    else:
        print(_render_text(findings))

    if args.fail_on == "none":
        return 0
    threshold = SEVERITY_ORDER[args.fail_on]
    return 1 if any(SEVERITY_ORDER[f.severity] >= threshold for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
