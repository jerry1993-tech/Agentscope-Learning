#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第 16 讲《记忆即文件：写入、frontmatter 与 wikilink 图》验证脚本。

跑法（在仓库根，或任何地方用绝对路径）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/16_memory_write.py

加 ``--live`` 会多跑 E 段（真实 deepseek-flash 蒸馏一个会话，实测 2 次计费补全调用）。

五段的结构与「消耗几次模型调用」：

===  ==============================================================  ============
段   内容                                                            模型调用
===  ==============================================================  ============
A    frontmatter：拆分、标签治理、渲染、与 ReMe 类型互转              0（纯函数）
B    ingest：幂等、去重、行锚点、wikilink 图、写入审计                 0（本地嵌入 ReMe，不调模型）
C    catalog：增量扫描 ``scan_changes`` + 四方对账 ``reconcile``       0
D    distill：会话整形（``shape_messages`` / ``messages_from_agent``） 0
E    （``--live``）真实 deepseek-flash 蒸馏                            2（实测）
===  ==============================================================  ============

**A~D 段全部离线**：ReMe 是本地嵌入式装配（``reme.ReMe(**config)`` + ``run_job``，
不起 HTTP 服务、不占端口），LLM 组件装配上了但一次都不调用 —— 所以这几段
既可以在没有 key 的机器上跑，也可以在 CI 里跑，而且结果完全确定。

**E 段是唯一花钱的一段**：一次 ``SessionDistiller.distill()``。它内部是
``auto_memory`` job → ``AutoMemoryStep`` → 一个 ReMe 自己的 ReAct agent，
实测一次 distill = **2 次补全调用**（脚本里的 ``_counting_model`` 会把真实
次数打出来，所以这里写死一个数也不会和输出对不上）。留 4 次余量，
正好卡在本讲的 6 次上限内。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

# ----------------------------------------------------------------------
# 路径与 .env
# ----------------------------------------------------------------------
#: ``<repo>/tutorial_agsc_reme/reference``
REF: Path = Path(__file__).resolve().parents[1]
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根
REPO: Path = REF.parents[1]
#: 本地 ReMe 克隆必须排在 ``sys.path`` 最前（压住 site-packages 里的 0.3.1.10）。
REME_SRC: Path = REPO / "third_party" / "ReMe"

for _candidate in (str(REME_SRC), str(REF)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:  # python-dotenv 是 pyproject 里声明的依赖
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:  # pragma: no cover - 本环境已装
    pass

from agentscope.agent import Agent  # noqa: E402
from agentscope.message import AssistantMsg, UserMsg  # noqa: E402

from harness_kit.memory import (  # noqa: E402
    DEFAULT_TAG_KEY,
    CatalogManager,
    FrontMatter,
    FrontMatterError,
    HarnessMemoryConfig,
    MemoryClient,
    MemoryIngestor,
    ReMeWorkspace,
    SessionDistiller,
    normalize_query_tags,
    normalize_tags,
    split_front_matter,
)
from harness_kit.memory.ingest import iter_chunk_texts  # noqa: E402
from harness_kit.models.adapters.echo import EchoChatModel  # noqa: E402

#: 是否跑真实模型那一段。
LIVE: bool = "--live" in sys.argv

#: 全程使用的模型名（``.env`` 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。
MODEL_NAME: str = os.getenv("LLM_MODEL") or "deepseek-chat"

#: 默认 INFO 日志会把每一轮模型调用都打出来；A~D 段是离线断言，压到 WARNING。
logger.remove()
logger.add(sys.stderr, level="WARNING")


def banner(title: str) -> None:
    """打一条段落标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ======================================================================
# 夹具：一个隔离的嵌入式 ReMe
# ======================================================================
#: 全部 ``ReMeWorkspace`` 的落地根（跑完不删，方便读者去看真实文件）。
SANDBOX: Path = Path(tempfile.mkdtemp(prefix="lesson16_")).resolve()

#: 本脚本建过的所有客户端，``main`` 统一收尾（ReMe 的组件有后台任务）。
_CLIENTS: list[MemoryClient] = []


async def make_client(name: str, **components: Any) -> tuple[MemoryClient, ReMeWorkspace]:
    """起一个隔离的嵌入式 ReMe（**不占端口、不起服务**）。

    Args:
        name (`str`): 工作区子目录名，每个段落一个，互不干扰。
        **components (`Any`): 额外的组件覆盖，透传 ``with_components``。

    Returns:
        `tuple[MemoryClient, ReMeWorkspace]`: 已 start 的客户端与工作区。
    """
    workspace = ReMeWorkspace(root=SANDBOX / name)
    workspace.ensure()
    builder = HarnessMemoryConfig(
        workspace=workspace,
        # 不装配 embedding：本讲不测语义检索（第 17 讲才测），
        # 而且少一个组件就少一处可能的装配失败。
        embedding_dimensions=None,
    ).with_jobs(
        # 这份名单**照抄官方的记忆 job 白名单**（AgentScope 的
        # middleware/_longterm_memory/_reme/_config.py:77 的 _memory_jobs()，
        # 去掉两个常驻后端：index_update_loop 是 background、dream_cron 是 cron）。
        # 为什么必须抄全：auto_memory 内部还会自己调 daily_list / daily_write /
        # read / write —— 少了任何一个，distill 都会在 run_job 里报
        # "Job daily_list not found" / "KeyError: Job 'daily_write' not found"。
        # 教训：白名单是按「job 会调哪些 job」的闭包算的，不是按「我直接调哪些」。
        "auto_memory",
        "auto_dream",
        "daily_list",
        "daily_write",
        "edit",
        "frontmatter_read",
        "frontmatter_update",
        "move",
        "node_search",
        "read",
        "reindex",
        "search",
        "write",
    )
    if components:
        builder = builder.with_components(**components)
    client = MemoryClient(builder.build())
    await client.start()
    _CLIENTS.append(client)
    return client, workspace


async def close_all() -> None:
    """把所有客户端关掉（ReMe 的 ``aclose`` 会等后台任务退出）。"""
    for client in _CLIENTS:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 收尾失败不该盖住真正的断言
            logger.warning("aclose 失败: {}", exc)
    _CLIENTS.clear()


# ======================================================================
# A · frontmatter：契约里最小的那个模块，也是写入路径的第一道闸
# ======================================================================
def section_a() -> None:
    """A 段：拆分、标签治理、渲染、与 ReMe 类型互转。"""
    banner("A · frontmatter：拆分规则 / 标签治理 / 渲染 / 与 ReMe 互转")

    print("\n--- A1 拆分规则与 ReMe 逐字一致（三种「没有 front matter」）---")
    body = "body\n"
    cases = {
        "不以 --- 开头": f"# 标题\n\n{body}",
        "只有一个 ---": f"---\nname: x\n{body}",
        "正常": f"---\nname: x\n---\n\n{body}",
    }
    for label, text in cases.items():
        data, rest = split_front_matter(text)
        print(f"  {label:14s} -> data={data} 正文={rest!r}")
    assert split_front_matter(f"---\nname: x\n---\n\n{body}") == ({"name": "x"}, body)
    assert split_front_matter(f"# 标题\n\n{body}") == ({}, f"# 标题\n\n{body}")
    assert split_front_matter(f"---\nname: x\n{body}") == ({}, f"---\nname: x\n{body}")
    print("  >>> 「只有一个 ---」不报错、也不当成 front matter：与 ReMe 的探测逻辑一致，")
    print("      否则一个以 --- 开头的普通 Markdown 会在两边得到不同的理解。")

    print("\n--- A2 两种错误：YAML 非法 / 结构不是 mapping（ReMe 在这里是静默的）---")
    for label, text in (
        ("YAML 非法", "---\nname: [unclosed\n---\n\nbody\n"),
        ("不是 mapping", "---\n- a\n- b\n---\n\nbody\n"),
    ):
        try:
            split_front_matter(text)
        except FrontMatterError as exc:
            print(f"  {label:10s} -> FrontMatterError: "
                  f"{str(exc).replace(chr(10), ' ')[:64]}")
        else:  # pragma: no cover - 不该发生
            raise AssertionError(f"{label} 应该抛 FrontMatterError")
    loose_text = "---\nname: [unclosed\n---\n\nbody\n"
    fm_loose, rest = FrontMatter.parse(loose_text, strict=False)
    print(f"  strict=False  -> name={fm_loose.name!r} extra={fm_loose.extra} "
          f"正文=全文（{len(rest)} 字符，未去空白）")
    assert fm_loose.extra == {}
    assert rest == loose_text
    print("  >>> ReMe 的 _parse_front_matter 把 YAML 错误吞掉了")
    print("      （default_file_chunker.py:52-58），harness 侧需要一个「能炸」的版本，")
    print("      否则写坏的标签永远没人知道。")

    print("\n--- A3 标签治理：写入限 3 个，查询不限（同一个输入，两个答案）---")
    requested: list[Any] = ["Ops", "deploy plan", "!!!", "x", "y", "z", 3]
    written = normalize_tags(requested)
    queried = normalize_query_tags(requested)
    print(f"  请求  = {requested}")
    print(f"  写入侧 = {written}（最多 3 个）")
    print(f"  查询侧 = {queried}（不限条数）")
    assert written == ["ops", "deploy_plan", "x"]
    assert queried == ["ops", "deploy_plan", "x", "y", "z", "3"]
    assert normalize_tags(["a" * 65, "ok"]) == ["ok"]
    assert normalize_tags(["   ", "!!"]) == []
    print("  >>> '!!!' 被丢掉（不含字母数字）、'deploy plan' 的空格变下划线、")
    print("      超长（>64）被丢掉、大小写 casefold。写入限制 ≠ 查询限制：")
    print("      如果查询侧也只取前 3 个，AND 查询会被悄悄削弱。")

    print("\n--- A4 渲染：一等字段在前、extra 键序稳定、正文一个字节都不动 ---")
    fm = FrontMatter(name="部署手册", description="上线前的检查清单", extra={})
    fm.set_tags(["Ops", "deploy", "x", "y"])
    text = fm.render("# 部署手册\n\n正文。\n")
    print("  渲染结果：")
    for line in text.splitlines():
        print(f"    | {line}")
    assert text.startswith("---\nname: 部署手册\n")  # 中文不被转义
    assert text.endswith("正文。\n")
    assert "memory_tags:\n- ops\n- deploy\n- x\n" in text
    again, again_body = FrontMatter.parse(text)
    print(f"  回环：name={again.name!r} tags={again.tags()} 正文={again_body!r}")
    assert again.fingerprint() == fm.fingerprint()
    assert again_body == "# 部署手册\n\n正文。\n"
    empty = FrontMatter().render("只有正文\n", ensure_trailing_newline=False)
    print(f"  front matter 为空时 render() = {empty!r}（**不**补 --- 块）")
    assert empty == "只有正文\n"
    print("  >>> 键序稳定不是审美问题：front matter 在文件开头，")
    print("      yaml.safe_dump 的默认 sort_keys=True 会把 name/description/memory_tags")
    print("      重排，每次重写都会让 diff 与 KV Cache 前缀全变。")

    print("\n--- A5 指纹与 dict 顺序无关（幂等写入的判据）---")
    one = FrontMatter(extra={"a": 1, "b": 2})
    two = FrontMatter(extra={"b": 2, "a": 1})
    print(f"  {'{a,b}'} 指纹 = {one.fingerprint()[:16]}")
    print(f"  {'{b,a}'} 指纹 = {two.fingerprint()[:16]}，相同 = {one.fingerprint() == two.fingerprint()}")
    assert one.fingerprint() == two.fingerprint()
    print("  >>> 否则「同一条 front matter、两次读取顺序不同」会被判成内容变了，")
    print("      幂等写入退化成每次都重写。")

    print("\n--- A6 与 ReMe 的 FileFrontMatter 互转（extra 落在 __pydantic_extra__）---")
    native = again.to_file_front_matter()
    print(f"  type            = {type(native).__name__}")
    print(f"  model_fields    = {sorted(type(native).model_fields)}（只有两个一等字段）")
    print(f"  native.name     = {native.name!r}")
    print(f"  native.model_extra = {native.model_extra}")
    assert sorted(type(native).model_fields) == ["description", "name"]
    assert native.model_extra == {DEFAULT_TAG_KEY: ["ops", "deploy", "x"]}
    back = FrontMatter.from_file_front_matter(native)
    assert back.fingerprint() == fm.fingerprint()
    print(f"  转回来指纹一致 = {back.fingerprint() == fm.fingerprint()}")
    print("  >>> ReMe 只有 name/description 是一等字段（schema/file_front_matter.py:8），")
    print("      其余全进 __pydantic_extra__；harness 把 extra 做成**显式字段**，")
    print("      教学上更容易看清「哪些是官方的、哪些是我们加的」。")


# ======================================================================
# B · ingest：写入路径 + 幂等 + 图 + 审计
# ======================================================================
#: B 段用的多节文档（front matter 已自带标签，正文里有两条 wikilink）。
DEPLOY_DOC = """---
name: 部署手册
description: 上线前的检查清单
memory_tags: [ops, deploy]
---

# 部署手册

总览段：上线分三步走 —— 预检、切换、观察。

## 预检

预检要跑 `uv sync` 与 `pytest -q`，确认依赖锁文件与测试全绿。

## 切换

切换用蓝绿发布，切换前必须确认 [[resource/runbook.md#回滚]] 可执行，
以及 [[resource/rollback-plan.md]] 里的联系人清单是最新的。
"""


async def section_b() -> None:
    """B 段：写入、幂等、行锚点、wikilink 图、审计。"""
    banner("B · ingest：写入路径 / 幂等 / 行锚点 / wikilink 图 / 写入审计")

    # chunk_byte_size 调到 200 是为了让 3 行小节的文档也能切出多个 chunk。
    # 默认值是 10000（default_file_chunker.py:25），那个大小下一份小文档只会有一个 chunk，
    # 行锚点就看不出「每个 chunk 对应哪几行」。
    client, ws = await make_client(
        "b_ingest",
        file_chunker={"markdown": {"chunk_byte_size": 200}},
    )
    ingestor = MemoryIngestor(client, workspace=ws, chunker="markdown")

    print("\n--- B1 add_text：一段文本 → resource/<name>.md → FileNode + chunks ---")
    first = await ingestor.add_text(DEPLOY_DOC, name="deploy")
    print(f"  {first}")
    assert first.added is True
    assert first.path == "resource/deploy.md"
    assert first.chunk_count == 2
    stored = ws.resource_path() / "deploy.md"
    print(f"  落盘文件存在 = {stored.is_file()}，字节数 = {stored.stat().st_size}")
    print("  >>> 返回的 path 是**工作区相对路径**：契约里所有路径都是这个形状，")
    print("      混用绝对路径正是 tag_index 报 Invalid workspace-relative path 的原因。")

    print("\n--- B2 同一份内容再入库：added=False, skipped_reason='unchanged' ---")
    second = await ingestor.add_file(stored)
    print(f"  {second}")
    assert second.added is False and second.skipped_reason == "unchanged"
    assert second.chunk_count == first.chunk_count
    print("  >>> 判据是内容 sha256，不是 mtime：改过又改回来的文件也会被正确跳过。")

    print("\n--- B3 换个标签再入库：added=True（因为文件字节真的变了）---")
    effective = await ingestor.apply_tags(stored, ["Ops", "release", "blue-green", "4th", "5th"])
    print(f"  apply_tags 实际生效 = {effective}（请求 5 个）")
    assert effective == ["ops", "release", "blue-green"]
    third = await ingestor.add_file(stored)
    print(f"  {third}")
    assert third.added is True
    print("  >>> 这不是幂等失效，而是「内容真的变了」：标签写在文件头的 front matter 里，")
    print("      改标签 = 改文件字节 → digest 变 → 必须重新分块并 upsert。")
    print("      记住这条因果关系：**标签是文件内容的一部分，不是索引里的一个旁挂字段**。")

    print("\n--- B4 区外文件：复制进 resource/ 并按来源去重 ---")
    outside = SANDBOX / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    src = outside / "runbook.md"
    src.write_text(
        "# Runbook\n\n回滚三步：停流量、回滚镜像、验单。\n",
        encoding="utf-8",
    )
    staged1 = await ingestor.add_file(src)
    staged2 = await ingestor.add_file(src)
    print(f"  第 1 次 {staged1}")
    print(f"  第 2 次 {staged2}")
    assert staged1.path == "resource/runbook.md" and staged1.added is True
    assert staged2.path == staged1.path and staged2.added is False
    print("  >>> 区外文件的原始路径记在 ingest_state.json 的 sources 里，")
    print("      同一个来源第二次调用会复用已有副本，不会堆出一堆 runbook-1.md。")

    print("\n--- B5 add_directory：单文件失败不中断整批（三种结局同现）---")
    docs = ws.resource_path() / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "ok.md").write_text("# 好的\n\n这一段正常入库。\n", encoding="utf-8")
    (docs / "empty.md").write_text("", encoding="utf-8")
    locked = docs / "locked.md"
    locked.write_text("# 锁住\n\n权限 000，读不到。\n", encoding="utf-8")
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if not is_root:
        os.chmod(locked, 0o000)
    results = await ingestor.add_directory(docs)
    for item in results:
        print(f"  {item.path:28s} added={item.added!s:5s} chunks={item.chunk_count} "
              f"skipped={item.skipped_reason}")
    by_path = {item.path: item for item in results}
    assert set(by_path) == {
        "resource/docs/empty.md",
        "resource/docs/locked.md",
        "resource/docs/ok.md",
    }
    assert by_path["resource/docs/ok.md"].added is True
    assert by_path["resource/docs/empty.md"].skipped_reason == "empty"
    if not is_root:
        assert str(by_path["resource/docs/locked.md"].skipped_reason).startswith("error: PermissionError")
        os.chmod(locked, 0o644)
    else:  # pragma: no cover - 本环境不是 root
        print("  （当前是 root，权限位不生效，locked.md 会被正常入库）")
    print("  >>> 批量导入最怕「第 37 个文件编码坏了，前 36 个白干」；")
    print("      所以每条失败都变成一条 result，而不是一个异常。")

    print("\n--- B6 行锚点：每个 chunk 知道自己对应原文件哪几行 ---")
    relative = "resource/deploy.md"
    chunker = ingestor._chunker(stored)
    node, chunks = await chunker.chunk(stored)
    print(f"  node.path={node.path} front_matter.name={node.front_matter.name!r} "
          f"links={[(lnk.target_path, lnk.target_anchor) for lnk in node.links]}")
    lines = (stored.read_text(encoding="utf-8")).split("\n")
    for index, chunk in enumerate(chunks):
        print(f"  chunk[{index}] id={chunk.id[:16]} 行 {chunk.start_line}-{chunk.end_line} "
              f"（共 {len(lines)} 行）")
        print(f"            首行 = {chunk.text.splitlines()[0]!r}")
    assert [c.start_line for c in chunks] == sorted(c.start_line for c in chunks)
    assert all(1 <= c.start_line <= c.end_line <= len(lines) for c in chunks)
    assert chunks[1].text.startswith("# 部署手册"), chunks[1].text[:40]
    print(f"  「# 部署手册」这一行在文件的第 "
          f"{next(i for i, line in enumerate(lines, 1) if line == '# 部署手册')} 行，")
    print(f"  而 chunk[1] 的行范围是 {chunks[1].start_line}-{chunks[1].end_line} —— 不重叠，")
    print("      但 chunk.text 里照样有它：这是 MarkdownFileChunker 给每个 chunk 加的")
    print("      祖先标题面包屑（markdown_file_chunker.py:537 的 envelope/breadcrumb 预算）。")
    print("      行锚点是 1-based 且按**整个文件**算（front matter 占了前 5 行）。")

    print("\n--- B7 chunk id 是确定性哈希：同样的内容 → 同样的 id ---")
    _, chunks_again = await chunker.chunk(stored)
    print(f"  两次分块 id 相同 = {[c.id for c in chunks] == [c.id for c in chunks_again]}")
    assert [c.id for c in chunks] == [c.id for c in chunks_again]
    assert [c.id for c in chunks] == list(node.chunk_ids)
    print("  >>> id = hash(path, start_line, end_line, text)（file_chunk.py:20 set_hash_id），")
    print("      所以「重跑一次索引」不会把同一块内容变成另一条向量 —— 这是增量索引能不重复付费的前提。")

    print("\n--- B8 wikilink 图：REAL / VIRTUAL / ALL 三种 scope ---")
    from reme.enumeration import LinkScopeEnum

    store = client.component("file_store", "default")
    real = await store.get_outlinks(relative, LinkScopeEnum.REAL)
    virtual = await store.get_outlinks(relative, LinkScopeEnum.VIRTUAL)
    everything = await store.get_outlinks(relative, LinkScopeEnum.ALL)
    print(f"  REAL    = {[(lnk.target_path, lnk.target_anchor) for lnk in real]}")
    print(f"  VIRTUAL = {[(lnk.target_path, lnk.target_anchor) for lnk in virtual]}")
    print(f"  ALL     = {[(lnk.target_path, lnk.target_anchor) for lnk in everything]}")
    assert [(lnk.target_path, lnk.target_anchor) for lnk in real] == [
        ("resource/runbook.md", "回滚"),
    ]
    assert [(lnk.target_path, lnk.target_anchor) for lnk in virtual] == [
        ("resource/rollback-plan.md", None),
    ]
    incoming = await store.get_inlinks("resource/runbook.md", LinkScopeEnum.REAL)
    print(f"  runbook.md 的入链 = {[(lnk.source_path, lnk.target_anchor) for lnk in incoming]}")
    assert [lnk.source_path for lnk in incoming] == [relative]
    dumped = real[0].model_dump()
    print(f"  FileLink.model_dump() = {dumped}")
    assert "predicate" not in dumped
    print("  >>> REAL 要求**两端都是已索引节点**；rollback-plan.md 没入库，所以那条边")
    print("      只在 VIRTUAL 桶里。predicate 是废弃字段且 exclude=True（file_link.py:19），")
    print("      序列化时看不到它 —— 别在代码里读它。")

    print("\n--- B9 反面教材：[[runbook]] 这种短名链接**永远不会**变成 REAL ---")
    short = await ingestor.add_text(
        "# 短名链接\n\n见 [[runbook]] 与 [[deploy]]。\n",
        name="shortnames",
    )
    short_real = await store.get_outlinks(short.path, LinkScopeEnum.REAL)
    short_all = await store.get_outlinks(short.path, LinkScopeEnum.ALL)
    print(f"  REAL = {[(lnk.target_path, lnk.target_anchor) for lnk in short_real]}（空！）")
    print(f"  ALL  = {[(lnk.target_path, lnk.target_anchor) for lnk in short_all]}")
    assert short_real == []
    assert sorted(lnk.target_path for lnk in short_all) == ["deploy", "runbook"]
    print("  >>> ReMe 的 wikilink 目标是**字面量**：不补 .md、不做短名 basename 搜索")
    print("      （wikilink_handler.py:19-24 的模块 docstring 明写了这一点，推荐形式是")
    print("      「工作区相对全路径 + 扩展名」）。写短名的后果是图里有一条永远悬空的边，")
    print("      检索侧的邻居扩展（link_expansion）也因此拿不到它。")

    print("\n--- B10 写入审计：ingest_state.json 里到底记了什么 ---")
    state_path = ingestor.state_path()
    print(f"  状态文件 = {state_path}")
    print(f"  是工作区内的文件 = {ws.is_inside(state_path)}")
    records = await ingestor.ingested()
    for path in sorted(records):
        record = records[path]
        print(f"  {path:28s} chunks={record['chunk_count']} empty={record['empty']} "
              f"tags={record['tags']} digest={record['digest'][:12]}")
    stats = await ingestor.stats()
    print(f"  stats = {stats}")
    assert stats["files"] == len(records)
    assert stats["chunks"] == sum(int(item["chunk_count"]) for item in records.values())
    assert records["resource/deploy.md"]["tags"] == ["ops", "release", "blue-green"]
    print("  >>> 这张表就是本讲的「写入审计」：每一次写入留下了路径、摘要、块数、标签、时间。")
    print("      它**只是幂等优化**：状态文件坏掉/被删的正确反应是当成空状态重来")
    print("      （ingest.py:262-266），真值始终是磁盘上的那几个 .md。")

    print("\n--- B11 remove_file：从索引里删掉，磁盘文件不动 ---")
    removed = await ingestor.remove_file(relative)
    stats_after = await ingestor.stats()
    print(f"  remove_file -> {removed}；磁盘文件还在 = {stored.is_file()}；"
          f"files {stats['files']} -> {stats_after['files']}")
    assert removed is True and stored.is_file() is True
    assert stats_after["files"] == stats["files"] - 1
    assert await ingestor.remove_file(relative) is False
    print("  >>> 第二次返回 False：已经不在状态里了。这个返回值很重要 ——")
    print("      它把「删成功了」和「本来就没有」区分开，避免误报。")


# ======================================================================
# C · catalog：增量扫描 + 四方对账
# ======================================================================
async def section_c() -> None:
    """C 段：``scan_changes`` 与 ``reconcile``。"""
    banner("C · catalog：增量扫描 scan_changes + 四方对账 reconcile")

    client, ws = await make_client("c_catalog")
    catalog = CatalogManager(client)
    ingestor = MemoryIngestor(client, workspace=ws)

    print("\n--- C1 catalog 台账：配置里有哪些、运行时怎么新增 ---")
    print(f"  list_catalogs() = {await catalog.list_catalogs()}")
    await catalog.ensure_catalog("lesson16")
    print(f"  ensure_catalog('lesson16') 之后 = {await catalog.list_catalogs()}")
    assert "lesson16" in await catalog.list_catalogs()
    print("  >>> catalog 名来自 config/default.yaml 的 components.file_catalog；")
    print("      ensure_catalog 是按同一个 ComponentEnum 在运行时再造一个实例。")

    print("\n--- C2 写入后：scan_changes 干净，reconcile 四方一致 ---")
    added = await ingestor.add_text("# 值班手册\n\n报警先看 [[resource/runbook.md]]。\n", name="oncall")
    loaded = await ingestor.add_file(SANDBOX / "outside" / "runbook.md")
    print(f"  ingest: {added} / {loaded}")
    ingested = await ingestor.ingested()
    clean_scan = await catalog.scan_changes()
    report = await catalog.reconcile(ingested=ingested)
    print(f"  scan_changes = {clean_scan.counts}")
    print(f"  reconcile    = {report.summary()}，clean = {report.clean}")
    assert clean_scan.is_empty()
    assert report.clean is True, report.model_dump()
    print("  >>> 四方（磁盘 / catalog / graph / ingest_state）完全对齐。")
    print("      注意 scan_changes 用的是 ReMe 自己的 walker（collect_existing）")
    print("      与 diff 算法（InitChangesStep.diff），harness 只换快照、翻路径。")

    print("\n--- C3 绕过 ingestor 直接写盘：scan_changes 报 added ---")
    rogue = ws.resource_path() / "rogue.md"
    rogue.write_text("# 手工放进来的文件\n\n它没有进过索引。\n", encoding="utf-8")
    scan = await catalog.scan_changes()
    print(f"  added={scan.added} modified={scan.modified} deleted={scan.deleted}")
    assert scan.added == ["resource/rogue.md"]
    assert scan.paths() == ["resource/rogue.md"]  # paths() = added + modified（要重新索引的）
    print("  >>> paths() 只给 added + modified，不给 deleted —— 它是「该重新 upsert 哪些」")
    print("      的答案，deleted 走的是另一条路（file_store.delete）。")

    print("\n--- C4 补写之后又干净了：「对账 → 补写 → 再对账」是收敛循环 ---")
    healed_rogue = await ingestor.add_file(rogue)
    report = await catalog.reconcile(ingested=await ingestor.ingested())
    print(f"  ingest: {healed_rogue}")
    print(f"  scan_changes = {(await catalog.scan_changes()).counts}；"
          f"{report.summary()}，clean = {report.clean}")
    assert healed_rogue.added is True
    assert report.clean is True, report.model_dump()
    print("  >>> 本讲把 reconcile 设计成**返回报告**而不是抛异常，就是为了让这个循环可写：")
    print("      报告给你分类好的待办清单，补写由调用方决定（也可以交给计划任务）。")

    print("\n--- C5 改一个已登记文件的内容：modified + 两处 mtime 漂移 ---")
    oncall = ws.resource_path() / "oncall.md"
    time.sleep(0.01)  # 保证 mtime 与上次不同（文件系统时间戳精度可能只有毫秒）
    oncall.write_text("# 值班手册\n\n报警先看 [[resource/runbook.md]]，再按升级路径找人。\n", encoding="utf-8")
    scan = await catalog.scan_changes()
    report = await catalog.reconcile(ingested=await ingestor.ingested())
    print(f"  scan_changes.modified = {scan.modified}")
    print(f"  catalog_mtime_drift   = {report.catalog_mtime_drift}")
    print(f"  graph_mtime_drift     = {report.graph_mtime_drift}")
    assert scan.modified == ["resource/oncall.md"]
    assert report.catalog_mtime_drift == ["resource/oncall.md"]
    assert report.graph_mtime_drift == ["resource/oncall.md"]
    print("  >>> 判据是 st_mtime 的**严格不等**（init_changes.py:47-62 的 diff），")
    print("      所以「内容改了但 mtime 没变」不会被发现 —— 这也是为什么")
    print("      ingest 侧用的是 content digest 而不是 mtime。两把尺子量两件事。")
    healed_oncall = await ingestor.add_file(oncall)
    report = await catalog.reconcile(ingested=await ingestor.ingested())
    print(f"  重新 ingest: added={healed_oncall.added}；{report.summary()}，clean = {report.clean}")
    assert healed_oncall.added is True
    assert report.clean is True, report.model_dump()

    print("\n--- C6 撕裂状态：catalog 里登记了、graph 里没有 ---")
    torn = ws.resource_path() / "torn.md"
    torn.write_text("# 撕裂\n\n只登记了 catalog，没进 graph。\n", encoding="utf-8")
    registered = await catalog.register("resource/torn.md")
    report = await catalog.reconcile(ingested=await ingestor.ingested())
    print(f"  register -> {registered}")
    print(f"  missing_in_graph   = {report.missing_in_graph}")
    print(f"  missing_in_catalog = {report.missing_in_catalog}")
    print(f"  untracked_by_ingest = {report.untracked_by_ingest}")
    print(f"  {report.summary()}，clean = {report.clean}")
    assert report.missing_in_graph == ["resource/torn.md"]
    assert report.missing_in_catalog == []
    assert "resource/torn.md" in report.untracked_by_ingest
    print("  >>> 这就是「先 register、后 upsert，中间进程被杀」的真实形态：")
    print("      catalog 说「我登记过它」、graph 说「我没见过它」。")
    print("      危险在于 InitChangesStep 若拿 catalog 当快照，会认为它已是最新，")
    print("      这个文件**永久检索不到**。所以 register 与 upsert 必须成对出现。")
    healed_torn = await ingestor.add_file(torn)
    report = await catalog.reconcile(ingested=await ingestor.ingested())
    print(f"  补写后：added={healed_torn.added}；{report.summary()}，clean = {report.clean}")
    assert healed_torn.added is True
    assert report.clean is True, report.model_dump()

    print("\n--- C7 文件被手工删掉：stale_in_graph + orphaned_in_ingest ---")
    rogue.unlink()
    report = await catalog.reconcile(ingested=await ingestor.ingested())
    scan = await catalog.scan_changes()
    print(f"  stale_in_catalog    = {report.stale_in_catalog}")
    print(f"  stale_in_graph      = {report.stale_in_graph}")
    print(f"  orphaned_in_ingest  = {report.orphaned_in_ingest}")
    print(f"  scan_changes.deleted = {scan.deleted}")
    print(f"  {report.summary()}，clean = {report.clean}")
    assert report.stale_in_graph == ["resource/rogue.md"]
    assert report.orphaned_in_ingest == ["resource/rogue.md"]
    assert scan.deleted == ["resource/rogue.md"]
    assert report.clean is False
    print("  >>> stale_in_graph 是最危险的一类：检索会召回一个磁盘上已经不存在的文件，")
    print("      引用（citation）于是指向一个死路径。orphaned_in_ingest 说明幂等判据")
    print("      还留着它的 digest —— 同名新文件会被「跳过」判据永久挡住。")

    print("\n--- C8 标签：写 3 个、查得到（写入限制 vs 查询限制）---")
    effective = await catalog.set_tags("resource/oncall.md", ["Ops", "on call", "值班", "第4个"])
    print(f"  set_tags 实际生效 = {effective}")
    assert effective == ["ops", "on_call", "值班"]
    tagged = await catalog.paths_for_tags(["ops"])
    page = await catalog.tags_page(page=1, page_size=5, order_by="tag")
    print(f"  paths_for_tags(['ops']) = {tagged}")
    print(f"  tags_page 第 1 页 items = {page['items']}（total_tags={page['total_tags']}）")
    assert "resource/oncall.md" in tagged
    print(f"  查询侧不限条数 = {catalog.normalize_query(['ops', 'on_call', '值班', '第4个'])}")
    print(f"  写入侧只留 3 个 = {catalog.normalize_write(['ops', 'on_call', '值班', '第4个'])}")
    print("  >>> tag_index 的键是 memory_tags（config/default.yaml 的 tag_index.tag_key），")
    print("      与 frontmatter.DEFAULT_TAG_KEY 是同一个常量，页面上看到的计数")
    print("      正是 normalize_tags 之后的结果。")


# ======================================================================
# D · distill（离线部分）：把 AgentScope 会话整形给 ReMe
# ======================================================================
def section_d() -> None:
    """D 段：``shape_messages`` 与 ``messages_from_agent``（0 次模型调用）。"""
    banner("D · distill 的整形层：shape_messages / messages_from_agent（离线）")

    print("\n--- D1 去掉记忆提示消息、丢掉空消息、str 内容包成块 ---")
    agent = Agent(name="assistant", system_prompt="你是运维助手。", model=EchoChatModel())
    agent.state.context.append(UserMsg("user", "我们把部署工具从 pip 换成 uv 了。"))
    agent.state.context.append(AssistantMsg("assistant", "记住了。"))
    agent.state.context.append(UserMsg("memory", ""))  # 记忆提示消息：空内容 + 保留名
    agent.state.context.append(UserMsg("user", "   "))  # 只有空白
    print(f"  输入 {len(agent.state.context)} 条消息：")
    for index, msg in enumerate(agent.state.context):
        print(f"    [{index}] name={msg.name!r} role={msg.role!r} "
              f"content={[block.type for block in msg.content]}")
    shaped = SessionDistiller.shape_messages(agent.state.context)
    print(f"  整形后 {len(shaped)} 条；每条是 dict，content 是 list[dict]：")
    for item in shaped:
        print(f"    name={item['name']!r} role={item['role']!r} "
              f"content={[block['type'] for block in item['content']]}")
    assert len(shaped) == 2
    assert all(isinstance(item["content"], list) for item in shaped)
    assert all(isinstance(block, dict) for item in shaped for block in item["content"])
    print("  >>> 三个整形动作各自的理由：")
    print("      ① name='memory' 的是记忆提示（MEMORY_HINT_NAME），喂回去会自我强化；")
    print("      ② 空消息会让 auto_memory 的 agent 白跑一轮；")
    print("      ③ Msg(content='纯文本') 在 AgentScope 2.0.8 里直接 ValidationError")
    print("         （message/_base.py:79 的 content: list[ContentBlock]），")
    print("         而 ReMe 的 auto_memory 收的是 model_dump 出来的 dict，")
    print("         所以整形层必须把 str 包成 [{'type': 'text', 'text': ...}]。")

    print("\n--- D2 messages_from_agent：从 AgentState.context 直接取 ---")
    msgs = SessionDistiller.messages_from_agent(agent)
    print(f"  messages_from_agent -> {len(msgs)} 条：{[msg.name for msg in msgs]}（**不**过滤）")
    assert [msg.name for msg in msgs] == ["user", "assistant", "memory", "user"]
    assert SessionDistiller.shape_messages(msgs) == SessionDistiller.shape_messages(
        agent.state.context,
    )
    since = SessionDistiller.messages_from_agent(agent, since_id=agent.state.context[1].id)
    print(f"  since_id=第 2 条的 id -> {len(since)} 条：{[msg.name for msg in since]}")
    assert len(since) == 2
    print("  >>> 两个方法**职责不重叠**：messages_from_agent 只做「从状态里取会话」，")
    print("      过滤（记忆提示 / 空消息）全部在 shape_messages 里。把它俩混在一起，")
    print("      就会得到一个「有时过滤、有时不过滤」的函数。")
    print("      会话是唯一真值：AgentState.context 是 AgentScope 的持久化边界")
    print("      （state/_state.py:221），蒸馏只是把它换个形状交给 ReMe。")

    print("\n--- D3 空输入 / 全空消息：整形结果是空列表，不会抛 ---")
    print(f"  shape_messages([])      = {SessionDistiller.shape_messages([])}")
    print(f"  shape_messages(None)    = {SessionDistiller.shape_messages(None)}")
    print(f"  shape_messages([Msg('memory','')]) = "
          f"{SessionDistiller.shape_messages([UserMsg('memory', '')])}")
    assert SessionDistiller.shape_messages([]) == []
    assert SessionDistiller.shape_messages(None) == []
    assert SessionDistiller.shape_messages([UserMsg("memory", "")]) == []
    print("  >>> 整形层不抛异常、只做减法：真正的失败判定留给 auto_memory")
    print("      （它会把 success=False 报回来，harness 再翻成 MemoryJobError）。")


# ======================================================================
# E · （--live）真实 deepseek-flash 蒸馏一个会话
# ======================================================================
def counting_model(client: MemoryClient) -> tuple[Any, dict[str, int]]:
    """给 ``as_llm`` 用的模型包一层调用计数器。

    ReMe 的 ``as_llm`` 组件把模型放在 ``self.model`` 上（``as_llm/__init__.py``），
    这里替换的是**类**上的 ``__call__``，所以对实例无侵入。

    Args:
        client (`MemoryClient`): 已 start 的客户端。

    Returns:
        `tuple[Any, dict[str, int]]`: (模型实例, 计数器)。计数器里的 ``calls`` 是补全次数。
    """
    llm = client.component("as_llm", "default") if "as_llm" in (
        client.application.context.components.get("as_llm") or {}
    ) else next(iter((client.application.context.components.get("as_llm") or {}).values()))
    model = llm.model
    counter: dict[str, int] = {"calls": 0}
    original = type(model).__call__

    async def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        counter["calls"] += 1
        return await original(self, *args, **kwargs)

    type(model).__call__ = counted  # type: ignore[method-assign]
    return model, counter


async def section_e() -> dict[str, int]:
    """E 段：真实模型的蒸馏（1 次 distill）。

    Returns:
        `dict[str, int]`: 模型调用计数器（``{"calls": n}``）。
    """
    banner("E ·（--live）真实 deepseek-flash：会话 → 记忆卡")

    client, ws = await make_client("e_live")
    _, counter = counting_model(client)
    distiller = SessionDistiller(client, workspace=ws)
    print(f"  模型 = {MODEL_NAME}（来自 .env 的 LLM_MODEL）")

    messages = [
        UserMsg("user", "我们决定部署工具从 pip 换成 uv，锁文件用 uv.lock。"),
        AssistantMsg("assistant", "明白了：部署统一用 uv，锁文件是 uv.lock。"),
        UserMsg("user", "回滚的话先停流量，再切回上一个镜像 tag，最后跑一遍冒烟。"),
        AssistantMsg("assistant", "记下了：停流量 → 回滚镜像 tag → 冒烟验证。"),
    ]
    result = await distiller.distill(messages, session_id="lesson16-live")
    print(f"  path    = {result.path}")
    print(f"  created = {result.created}")
    print("  正文前 240 字符：")
    for line in result.content[:240].splitlines():
        print(f"    | {line}")
    print(f"  计费补全次数 = {counter['calls']}（1 次 distill）")
    assert result.path is not None, "真实模型这一轮没写出笔记（path=None）；重跑一次即可"
    card = ws.resolve_relative(result.path)
    print(f"  磁盘文件存在 = {Path(card).is_file()}，字节数 = {Path(card).stat().st_size}")
    head, _ = FrontMatter.parse(Path(card).read_text(encoding="utf-8"))
    print(f"  front matter = name={head.name!r} extra={head.extra}")
    assert head.extra.get("session_id") == "lesson16-live"
    assert str(head.extra.get("source_conversation", "")).startswith("[[session/dialog/")

    store = client.component("file_store", "default")
    before = await store.get_nodes([result.path])
    print(f"  distill 之后 graph 里有这个节点吗 = {bool(before)}")
    assert before == []
    print("  >>> 这一步很重要：auto_memory 只负责**写文件**（它是一个 write step），")
    print("      把它变成可检索的 chunk 是索引侧的事。生产装配里那件事由常驻的")
    print("      index_update_loop（background job）做 —— 而嵌入式装配按定义丢掉了")
    print("      所有 background job（见第 15 讲的 EMBEDDED_JOB_BACKENDS），")
    print("      所以这里必须自己补一步 reindex，否则「写了但搜不到」。")

    reindex = await client.run_job("reindex")
    after = await store.get_nodes([result.path])
    counts = dict(reindex.metadata)["counts"]
    print(f"  reindex counts = {counts}")
    print(f"  reindex 之后 graph 里有这个节点吗 = {bool(after)}；"
          f"chunks = {len(after[0].chunk_ids) if after else 0}")
    assert after, "reindex 之后应该能查到刚写下的记忆卡"
    print("  >>> 蒸馏产物是一个**普通的 Markdown 文件**：path 是工作区相对路径、")
    print("      front matter 里有 source_conversation 指回会话。这就是「记忆即文件」——")
    print("      模型写出来的东西立刻变成一个可检索、可 wikilink、可 diff、可 git 管理的文件。")
    print(f"  >>> auto_memory 是一次真正的 Agent 调用（ReMe 自己的 ReAct agent）：")
    print(f"      实测一次 distill = {counter['calls']} 次补全，全脚本只此一次，")
    print("      远在本讲的 6 次上限内。")
    return counter


async def main() -> int:
    """跑全部段落，返回退出码。

    Returns:
        `int`: 0 = 全部通过。
    """
    print(f"沙箱目录 = {SANDBOX}")
    calls = 0
    try:
        section_a()
        await section_b()
        await section_c()
        section_d()
        if LIVE:
            calls = (await section_e())["calls"]
        else:
            print()
            print("=" * 78)
            print("跳过 E 段（真实 LLM）。加 --live 打开：1 次 distill 计费调用。")
            print("=" * 78)
    finally:
        await close_all()
    print()
    print("=" * 78)
    if LIVE:
        print(f"PASS · 第 16 讲全部断言通过（A~D 段 0 次 LLM 调用；E 段 1 次 distill = {calls} 次补全）")
    else:
        print("PASS · 第 16 讲全部断言通过（A~D 段 0 次 LLM 调用）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
