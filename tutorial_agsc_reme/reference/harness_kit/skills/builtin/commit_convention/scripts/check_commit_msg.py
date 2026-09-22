#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""commit_convention 技能的配套脚本：Conventional Commits 校验器。

**只用标准库**，任何 Python 3.11+ 解释器都能直接跑。

校验规则（每条都有独立的 ``rule`` 名，方便 CI 里按规则放行）：

=================== ==========================================================
rule                含义
=================== ==========================================================
``empty-message``   空消息
``bad-header``      header 不匹配 ``type(scope)!: subject``
``unknown-type``    type 不在白名单里
``bad-scope``       scope 含大写字母/空格/空括号
``subject-case``    subject 首字母大写
``subject-period``  subject 以句号结尾
``subject-empty``   subject 为空
``header-too-long`` header 超过长度上限
``body-no-blank``   body 与 header 之间没有空行
``no-breaking-note``header 带 ``!`` 但 footer 没有 ``BREAKING CHANGE:``
=================== ==========================================================

用法：

.. code-block:: bash

    python check_commit_msg.py --string "feat(mcp): add stdio transport"
    python check_commit_msg.py /tmp/msg.txt
    git log -1 --format=%B | python check_commit_msg.py -

退出码：0 = 合规；1 = 不合规；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Sequence

__all__ = ["Issue", "check_message", "main"]

ALLOWED_TYPES: frozenset[str] = frozenset(
    {
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
    },
)
"""受支持的 commit type。``style`` 是 Conventional Commits 的原生取值，保留。"""

_HEADER_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)"
    r"(?:\((?P<scope>[^()]*)\))?"
    r"(?P<breaking>!)?"
    r": (?P<subject>.+)$",
)

_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
_REVERT_RE = re.compile(r"^This reverts commit [0-9a-f]{7,40}\.", re.MULTILINE)


@dataclass(frozen=True)
class Issue:
    """一条校验问题。"""

    rule: str
    """规则名。"""

    message: str
    """人能读懂的说明。"""

    hint: str = ""
    """怎么改。"""

    def to_dict(self) -> dict[str, str]:
        """转成可 JSON 序列化的 dict。

        Returns:
            `dict[str, str]`: 字段字典。
        """
        return asdict(self)


@dataclass
class CheckResult:
    """一次校验的完整结果。"""

    ok: bool
    """是否通过。"""

    header: str = ""
    """解析出的 header 行。"""

    commit_type: str | None = None
    """解析出的 type（未解析出来时为 ``None``）。"""

    scope: str | None = None
    """解析出的 scope。"""

    breaking: bool = False
    """是否标注了破坏性变更。"""

    subject: str = ""
    """解析出的 subject。"""

    issues: list[Issue] = field(default_factory=list)
    """全部问题。"""

    def to_dict(self) -> dict[str, object]:
        """转成可 JSON 序列化的 dict。

        Returns:
            `dict[str, object]`: 字段字典。
        """
        return {
            "ok": self.ok,
            "header": self.header,
            "type": self.commit_type,
            "scope": self.scope,
            "breaking": self.breaking,
            "subject": self.subject,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def check_message(message: str, *, max_header_length: int = 72) -> CheckResult:
    """校验一段完整的提交信息。

    Args:
        message (`str`): 完整提交信息（允许含 ``#`` 注释行，会被忽略）。
        max_header_length (`int`): header 行长度上限，默认 72。

    Returns:
        `CheckResult`: 校验结果；``ok`` 为 ``True`` 表示全部规则通过。
    """
    # git 会把 template 注释行以 '#' 开头写进 COMMIT_EDITMSG，跳过它们
    lines = [ln for ln in message.splitlines() if not ln.startswith("#")]
    while lines and not lines[-1].strip():
        lines.pop()

    result = CheckResult(ok=True)
    if not lines:
        result.issues.append(
            Issue("empty-message", "提交信息为空", "写一句话说明这个提交做了什么"),
        )
        result.ok = False
        return result

    result.header = lines[0].rstrip()
    match = _HEADER_RE.match(result.header)
    if not match:
        result.issues.append(
            Issue(
                "bad-header",
                f"header 不匹配 '<type>(<scope>)!: <subject>'：{result.header!r}",
                "示例：fix(mcp): handle empty tool list",
            ),
        )
        result.ok = False
        return result

    commit_type = match.group("type")
    scope = match.group("scope")
    subject = match.group("subject")
    result.commit_type = commit_type
    result.scope = scope
    result.subject = subject
    result.breaking = bool(match.group("breaking"))

    if commit_type.lower() not in ALLOWED_TYPES:
        result.issues.append(
            Issue(
                "unknown-type",
                f"type {commit_type!r} 不在白名单里",
                f"可用：{', '.join(sorted(ALLOWED_TYPES))}",
            ),
        )

    if scope is not None:
        if not scope.strip():
            result.issues.append(
                Issue("bad-scope", "scope 是空括号", "要么去掉括号，要么填模块名"),
            )
        elif scope != scope.lower() or " " in scope:
            result.issues.append(
                Issue(
                    "bad-scope",
                    f"scope {scope!r} 必须是小写且不含空格",
                    "例如 (mcp)、(memory)、(cli)",
                ),
            )

    if not subject.strip():
        result.issues.append(
            Issue("subject-empty", "subject 为空", "写一句祈使句描述这个提交"),
        )
    else:
        first = subject.lstrip()[0]
        if first.isupper() and not re.match(r"^[A-Z]{2,}\b", subject.lstrip()):
            result.issues.append(
                Issue(
                    "subject-case",
                    f"subject 首字母大写：{subject!r}",
                    "改成小写开头，例如 'add stdio transport'",
                ),
            )
        if subject.rstrip().endswith("."):
            result.issues.append(
                Issue("subject-period", "subject 以句号结尾", "去掉结尾的句号"),
            )

    if len(result.header) > max_header_length:
        result.issues.append(
            Issue(
                "header-too-long",
                f"header 长度 {len(result.header)} 超过 {max_header_length}",
                "把细节挪到 body，header 只留一句概括",
            ),
        )

    body_start = 1
    if len(lines) > 1:
        if lines[1].strip():
            result.issues.append(
                Issue(
                    "body-no-blank",
                    "body 与 header 之间缺少空行",
                    "在 header 后插入一个空行，git 才会把它拆成 title/body",
                ),
            )
    else:
        body_start = len(lines)

    body_and_footer = "\n".join(lines[body_start:])
    breaking_in_footer = bool(_BREAKING_FOOTER_RE.search(body_and_footer))
    if result.breaking and not breaking_in_footer:
        result.issues.append(
            Issue(
                "no-breaking-note",
                "header 带了 '!' 但 footer 里没有 'BREAKING CHANGE:'",
                "在 footer 补一行 'BREAKING CHANGE: <影响与迁移方式>'",
            ),
        )
    if (
        commit_type.lower() == "revert"
        and body_and_footer.strip()
        and not _REVERT_RE.search(body_and_footer)
    ):
        result.issues.append(
            Issue(
                "revert-no-sha",
                "revert 类型的 body 里没有 'This reverts commit <sha>.'",
                "补一行 'This reverts commit <40 位 sha>.'，git revert 会自动生成",
            ),
        )

    result.ok = not result.issues
    return result


def _read_message(args: argparse.Namespace) -> str:
    """按命令行参数读出待校验的提交信息。

    Args:
        args (`argparse.Namespace`): 解析后的参数。

    Returns:
        `str`: 提交信息文本。

    Raises:
        OSError: 文件读取失败。
    """
    if args.string is not None:
        return args.string
    if args.path == "-":
        return sys.stdin.read()
    with open(args.path, "r", encoding="utf-8") as handle:
        return handle.read()


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv (`Sequence[str] | None`): 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        `int`: 进程退出码。
    """
    parser = argparse.ArgumentParser(
        prog="check_commit_msg.py",
        description="Conventional Commits 校验器（纯标准库）",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="提交信息文件路径；'-' 表示从 stdin 读；与 --string 二选一",
    )
    parser.add_argument("--string", help="直接校验一段提交信息")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--max-header-length",
        type=int,
        default=72,
        help="header 长度上限（默认 72）",
    )
    args = parser.parse_args(argv)

    if args.string is None and args.path is None:
        parser.error("必须提供 path 或 --string")

    try:
        message = _read_message(args)
    except OSError as exc:
        print(f"错误: 无法读取提交信息: {exc}", file=sys.stderr)
        return 2

    result = check_message(message, max_header_length=args.max_header_length)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif result.ok:
        print(
            f"OK  type={result.commit_type} scope={result.scope} "
            f"breaking={result.breaking}",
        )
    else:
        print(f"FAIL  header={result.header!r}")
        for issue in result.issues:
            print(f"  - [{issue.rule}] {issue.message}")
            if issue.hint:
                print(f"    修复: {issue.hint}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
