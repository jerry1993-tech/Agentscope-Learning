# -*- coding: utf-8 -*-
"""把一小段代码库灌进 ReMe 索引（Demo 的"写入侧"）。

**为什么要在写入前做一次"渲染"**

ReMe 的分块器按后缀挑（``reme/components/file_chunker``）：``.md`` 走 markdown
分块器（按标题切），其余走通用文本切分。而代码库里的 ``.py`` 是**没有标题**的，
直接丢进去只能被通用切分器按长度硬切，切出来的块没有任何"这是哪个文件哪一段"
的结构化标记 —— 后面检索命中时，模型看到的是一段裸代码，说不出出处。

所以这里的做法是：**把源文件渲染成一篇 markdown**
（``# <来源路径>`` + 每 N 行一个 ``## 行 a-b`` 小节 + 围栏代码块），
再按 ``.md`` 入库。这样引用行里的 ``### resource/.../x.py.md:12-40``
与源码行号的对应关系是**渲染时就固定下来的**，不靠模型猜。

**为什么落到工作区** ``resource/`` **里面再入库**

``MemoryIngestor.add_file`` 对工作区**外**的文件有一套复制去重逻辑：复制到
``resource/<basename>``，同名冲突时退化成 ``x-1.md`` / ``x-2.md``
（``harness_kit/memory/ingest.py:604`` 的 ``_stage_source``）。于是
``.../agent/_agent.py`` 与 ``.../plan/_agent.py`` 会撞名，引用里只剩一个
``_agent-1.md``，看不出是哪棵树里的。

把渲染产物**先写进工作区**（``<workspace>/resource/<alias>/<相对路径>.md``）
就绕开了复制逻辑：``_stage_source`` 对区内文件原样返回
（``ingest.py:635``），相对路径完整保留，引用可读、可回溯。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_SUFFIXES",
    "SECTION_LINES",
    "IngestReport",
    "SelectResult",
    "render_source_document",
    "select_files",
    "stage_documents",
    "ingest_repo",
    "main",
]

#: 默认索引哪些后缀。``.py`` 是主料，``.md`` 是现成的文档（原样入库）。
DEFAULT_SUFFIXES: tuple[str, ...] = (".py", ".md")

#: 每个 markdown 小节覆盖多少行源码。120 行 ≈ 一个屏幕，
#: 也是"检索命中一段代码"比较舒服的粒度。
SECTION_LINES: int = 120

#: 后缀 → 围栏代码块的语言标记。
_LANG_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".sh": "bash",
    ".txt": "text",
    ".cfg": "ini",
    ".ini": "ini",
}

#: 单文件读取上限（字符）。超大文件（例如压缩过的 JSON）不该拖垮索引。
MAX_FILE_CHARS: int = 200_000


class SelectResult(BaseModel):
    """一次文件筛选的结果。"""

    model_config = ConfigDict(extra="forbid")

    root: str
    """被扫描的根目录（绝对路径）。"""

    files: list[str] = Field(default_factory=list)
    """选中的文件（相对 ``root``）。"""

    skipped: list[str] = Field(default_factory=list)
    """被跳过的文件，格式 ``相对路径: 原因``。"""

    @property
    def truncated(self) -> bool:
        """是否因为 ``limit`` 而截断。

        Returns:
            `bool`: 是否还有没选进来的文件。
        """
        return any(item.endswith(":limit") for item in self.skipped)


class IngestReport(BaseModel):
    """一次入库的汇总。"""

    model_config = ConfigDict(extra="forbid")

    workspace: str
    """记忆工作区（绝对路径）。"""

    staged: list[str] = Field(default_factory=list)
    """渲染并写进工作区的文档（工作区相对路径）。"""

    added: int = 0
    """真正新增/更新的文件数。"""

    unchanged: int = 0
    """内容没变、被幂等跳过的文件数（重跑时应该全是这个）。"""

    failed: list[str] = Field(default_factory=list)
    """入库失败的文件（``路径: 原因``）。"""

    chunks: int = 0
    """新增的 chunk 总数。"""

    @property
    def ok(self) -> bool:
        """是否至少有一个文件入库成功、且没有失败项。

        Returns:
            `bool`: 是否健康。
        """
        return not self.failed and (self.added > 0 or self.unchanged > 0)

    def summary(self) -> str:
        """一行结论。

        Returns:
            `str`: 人类可读的汇总。
        """
        return (
            f"新增/更新 {self.added} 个、未变 {self.unchanged} 个、"
            f"失败 {len(self.failed)} 个，共 {self.chunks} 个 chunk"
        )


# ======================================================================
# 渲染
# ======================================================================
def render_source_document(
    text: str,
    *,
    origin: str,
    lang: str = "text",
    section_lines: int = SECTION_LINES,
) -> str:
    """把一段源码渲染成 markdown（见模块 docstring 的说明）。

    Args:
        text (`str`): 源文件正文。
        origin (`str`): 来源标识（写进标题，通常是"相对仓库根的路径"）。
        lang (`str`): 围栏代码块的语言标记。
        section_lines (`int`): 每小节覆盖多少行。

    Returns:
        `str`: markdown 正文。

    Raises:
        `ValueError`: ``section_lines`` 非正。
    """
    if section_lines <= 0:
        raise ValueError(f"section_lines 必须为正，收到 {section_lines}")

    lines = text.splitlines()
    total = len(lines)
    parts: list[str] = [
        f"# {origin}",
        "",
        f"> 来源：`{origin}`，共 {total} 行。每个小节标题里的行号是该小节在源文件中的真实行区间。",
        "",
    ]
    for start in range(0, max(total, 1), section_lines):
        end = min(start + section_lines, total)
        chunk = lines[start:end]
        parts.append(f"## {origin} 行 {start + 1}-{end}")
        parts.append("")
        parts.append(f"```{lang}")
        parts.extend(chunk)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def _safe_alias(text: str) -> str:
    """把别名净化成安全的单层目录名。

    Args:
        text (`str`): 原始别名。

    Returns:
        `str`: 只含 ``[A-Za-z0-9._-]`` 的名字。
    """
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in text)
    return cleaned.strip("-") or "repo"


def select_files(
    root: str | Path,
    *,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    patterns: Sequence[str] | None = None,
    limit: int = 20,
    max_bytes: int = 512 * 1024,
) -> SelectResult:
    """挑出要索引的文件（稳定排序，便于复现）。

    Args:
        root (`str | Path`): 扫描根目录。
        suffixes (`Sequence[str]`): 允许的后缀（小写比较）。
        patterns (`Sequence[str] | None`): 额外的 glob 片段（匹配相对路径）。
        limit (`int`): 最多选几个文件（``<= 0`` 表示不限）。
        max_bytes (`int`): 单文件大小上限（字节）。

    Returns:
        `SelectResult`: 选中与被跳过的文件。

    Raises:
        `NotADirectoryError`: ``root`` 不是目录。
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"不是目录: {base}")

    wanted = {suffix.lower() for suffix in suffixes}
    globs = list(patterns or [])
    skipped: list[str] = []
    selected: list[str] = []

    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if path.suffix.lower() not in wanted:
            continue
        if globs and not any(fragment in relative for fragment in globs):
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:  # pragma: no cover - 权限/竞态
            skipped.append(f"{relative}:stat:{exc}")
            continue
        if size > max_bytes:
            skipped.append(f"{relative}:too_large({size}B)")
            continue
        selected.append(relative)

    if limit > 0 and len(selected) > limit:
        for extra in selected[limit:]:
            skipped.append(f"{extra}:limit")
        selected = selected[:limit]

    return SelectResult(root=str(base), files=selected, skipped=skipped)


def stage_documents(
    select: SelectResult,
    *,
    workspace: str | Path,
    alias: str,
    section_lines: int = SECTION_LINES,
    max_chars: int = MAX_FILE_CHARS,
) -> list[Path]:
    """把选中的文件渲染成 markdown 写进 ``<workspace>/resource/<alias>/``。

    Args:
        select (`SelectResult`): :func:`select_files` 的结果。
        workspace (`str | Path`): ReMe 工作区根目录。
        alias (`str`): 一层目录名（用来区分不同代码库，避免同名文件互撞）。
        section_lines (`int`): 每小节行数。
        max_chars (`int`): 单文件读取上限。

    Returns:
        `list[Path]`: 写好的文档路径（已 resolve）。

    Raises:
        `NotADirectoryError`: ``workspace`` 不存在且创建失败。
    """
    root = Path(select.root)
    target_dir = Path(workspace) / "resource" / _safe_alias(alias)
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for relative in select.files:
        source = root / relative
        try:
            raw = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - 权限/竞态
            logger.warning("读取 {} 失败: {}", source, exc)
            continue
        if len(raw) > max_chars:
            raw = raw[:max_chars] + f"\n\n（已截断：原文超过 {max_chars} 字符）"

        if source.suffix.lower() == ".md":
            # 现成的 markdown 原样入库：它自己的标题结构就是分块边界。
            document = f"# {relative}\n\n> 来源：`{relative}`\n\n{raw}"
        else:
            document = render_source_document(
                raw,
                origin=relative,
                lang=_LANG_BY_SUFFIX.get(source.suffix.lower(), "text"),
                section_lines=section_lines,
            )

        destination = target_dir / f"{relative}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(document, encoding="utf-8")
        written.append(destination.resolve())
        logger.debug("已渲染 {} → {}", relative, destination)
    return written


# ======================================================================
# 入库
# ======================================================================
async def ingest_repo(
    *,
    target: str | Path,
    workspace: str | Path,
    alias: str | None = None,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    patterns: Sequence[str] | None = None,
    limit: int = 20,
    section_lines: int = SECTION_LINES,
    tags: Iterable[str] | None = None,
    catalog: str = "code",
    settings: Any | None = None,
) -> IngestReport:
    """渲染 + 入库一条龙（Demo 与 CLI 共用）。

    Args:
        target (`str | Path`): 要索引的代码目录。
        workspace (`str | Path`): ReMe 工作区根目录。
        alias (`str | None`): 工作区里的一层目录名；``None`` 时用 ``target`` 的名字。
        suffixes (`Sequence[str]`): 允许的后缀。
        patterns (`Sequence[str] | None`): 额外的 glob 片段。
        limit (`int`): 文件数上限。
        section_lines (`int`): 每小节行数。
        tags (`Iterable[str] | None`): 写入 front matter 的标签。
        catalog (`str`): file_catalog 名。
        settings (`Any | None`): :class:`~harness_kit.settings.Settings`；
            ``None`` 时从 ``workspace`` 所在位置推断（见 :mod:`agent`）。

    Returns:
        `IngestReport`: 入库汇总。

    Raises:
        `NotADirectoryError`: ``target`` 不是目录。
        `ValueError`: 没有选中任何文件（此时多半是后缀/glob 写错了）。
    """
    from harness_kit.memory.client import MemoryClient
    from harness_kit.memory.config import HarnessMemoryConfig
    from harness_kit.memory.ingest import MemoryIngestor
    from harness_kit.memory.workspace import ReMeWorkspace

    selection = select_files(
        target,
        suffixes=suffixes,
        patterns=patterns,
        limit=limit,
    )
    if not selection.files:
        raise ValueError(
            f"在 {selection.root} 下没选中任何文件（后缀={list(suffixes)}，"
            f"patterns={list(patterns or [])}）；换后缀或调大 --limit 再试。",
        )

    root = Path(workspace).expanduser().resolve()
    documents = stage_documents(
        selection,
        workspace=root,
        alias=alias or Path(selection.root).name or "repo",
        section_lines=section_lines,
    )

    report = IngestReport(workspace=str(root), staged=[str(path) for path in documents])
    if not documents:
        report.failed.append("全部文件渲染失败，见上面的 warning")
        return report

    re_workspace = ReMeWorkspace(root=root)
    config = HarnessMemoryConfig(workspace=re_workspace, settings=settings).with_jobs(
        "search",
        "node_search",
        "auto_memory",
    )
    client = MemoryClient(config.build())
    await client.start()
    try:
        ingestor = MemoryIngestor(client, workspace=re_workspace)
        tag_list = list(tags or []) or None
        for document in documents:
            result = await ingestor.add_file(document, tags=tag_list, catalog=catalog)
            if result.added:
                report.added += 1
                report.chunks += result.chunk_count
            elif result.skipped_reason == "unchanged":
                report.unchanged += 1
            else:
                report.failed.append(f"{result.path}: {result.skipped_reason}")
    finally:
        await client.aclose()

    logger.bind(
        workspace=str(root),
        added=report.added,
        unchanged=report.unchanged,
        chunks=report.chunks,
    ).info("代码库入库完成: {}", report.summary())
    return report


# ======================================================================
# CLI
# ======================================================================
def _build_parser() -> argparse.ArgumentParser:
    """构造本模块的 argparse 解析器。

    Returns:
        `argparse.ArgumentParser`: 解析器。
    """
    from harness_kit.demo.code_assistant.agent import (
        DEFAULT_ALIAS,
        DEFAULT_INDEX_DIR,
        DEFAULT_TAGS,
        DEFAULT_TARGET,
    )

    parser = argparse.ArgumentParser(
        prog="ingest_repo",
        description="把一小段代码库渲染成 markdown 并灌进 ReMe 索引。",
    )
    parser.add_argument(
        "--target",
        default=None,
        help=f"要索引的目录（默认 <仓库根>/{DEFAULT_TARGET}）",
    )
    parser.add_argument("--index", default=DEFAULT_INDEX_DIR, help="ReMe 工作区（默认 Demo 专用目录）")
    # 与 main.py 共用 agent.DEFAULT_ALIAS：alias 决定渲染产物落在工作区的哪个
    # 命名空间，而检索命中的是整个工作区。两边不一致就会在同一个工作区里堆出
    # 两套 resource/ 命名空间（实测 resource/agent/ 与 resource/agentscope/ 并存），
    # 检索时两批一起返回、同名文件互相冒充 —— 见 agent.DEFAULT_ALIAS 的注释。
    parser.add_argument("--alias", default=DEFAULT_ALIAS, help=f"工作区里的一层目录名（默认 {DEFAULT_ALIAS}）")
    parser.add_argument("--limit", type=int, default=12, help="最多索引几个文件（默认 12）")
    parser.add_argument("--section-lines", type=int, default=SECTION_LINES, help="每小节覆盖的源码行数")
    parser.add_argument("--suffixes", nargs="*", default=list(DEFAULT_SUFFIXES), help="允许的后缀")
    parser.add_argument("--patterns", nargs="*", default=None, help="额外的 glob 片段（匹配相对路径）")
    # 与 main.py 共用 agent.DEFAULT_TAGS：标签会被渲染进正文，改标签就是改内容
    # sha256，入库判定会从「未变」翻成「新增」。两边默认值不同的话，交替跑会让
    # 幂等性看起来失效（实测过）。`--tags` 不给值 = 不写标签。
    parser.add_argument(
        "--tags",
        nargs="*",
        default=list(DEFAULT_TAGS),
        help=f"写入 front matter 的标签（默认 {' '.join(DEFAULT_TAGS)}；不给值 = 不写）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出汇总")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m harness_kit.demo.code_assistant.ingest_repo`` 的入口。

    Args:
        argv (`Sequence[str] | None`): 参数列表；``None`` 用 ``sys.argv[1:]``。

    Returns:
        `int`: 退出码。
    """
    from harness_kit.demo.code_assistant.agent import (
        DEFAULT_TARGET,
        default_settings,
        repo_root,
    )

    parser = _build_parser()
    options = parser.parse_args(list(argv) if argv is not None else None)

    settings = default_settings()
    target = (
        Path(options.target)
        if options.target
        else repo_root() / Path(DEFAULT_TARGET)
    )
    index = Path(options.index)
    if not index.is_absolute():
        index = settings.resolve(index)

    try:
        report = asyncio.run(
            ingest_repo(
                target=target,
                workspace=index,
                alias=options.alias,
                suffixes=tuple(options.suffixes),
                patterns=options.patterns,
                limit=options.limit,
                section_lines=options.section_lines,
                tags=options.tags,
                settings=settings,
            ),
        )
    except (NotADirectoryError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if options.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"目标代码库: {target}")
        print(f"记忆工作区: {report.workspace}")
        print(f"渲染文档  : {len(report.staged)} 篇")
        for item in report.failed:
            print(f"  [!!] {item}")
        print(f"\n{report.summary()}")
        print("（重跑同一条命令应该全部 '未变'：入库是按内容 sha256 幂等的）")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
