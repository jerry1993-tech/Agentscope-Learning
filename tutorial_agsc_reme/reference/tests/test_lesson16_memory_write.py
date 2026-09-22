# -*- coding: utf-8 -*-
"""第 16 讲的 pytest：frontmatter / ingest / catalog / distill 的写入路径。

五条纪律（延续第 15 讲，本讲的重点是"文件即真值"）：

1. **0 次 LLM 调用**。写入路径上**一次模型调用都不需要**：分块、摘要、
   图边、台账、对账全是纯计算 + 本地文件 IO。真实模型的蒸馏在
   ``scripts/16_memory_write.py --live``（1 次 distill）。
2. **纯函数部分不启动 ReMe**。``frontmatter.py`` 与 ``catalog.py`` 里
   那几个静态方法（``_resolved_existing`` / ``normalize_write`` /
   ``normalize_query``）完全不依赖 ReMe，所以它们跑在**毫秒级**的测试里。
   只有"写入路径"那 10 条才需要真的 ``await client.start()`` —— 因为
   它们要证明的恰恰是"ReMe 真的把我们写的东西收下了"。
3. **每个测试自己建工作区，绝不共享**。ReMe 的 ``Application._start()``
   会建 ``asyncio.Lock``，而 ``pyproject.toml`` 里
   ``asyncio_default_fixture_loop_scope = "function"`` 意味着**每个测试
   一个事件循环**：跨测试复用一个 client 就是跨事件循环复用锁，
   会得到"测试单独跑是绿的、一起跑就红"的幽灵失败。
4. **回归测试钉住已经踩过的坑**，而不是泛泛测功能：
   ``test_short_wikilink_targets_stay_virtual`` 对应"写 ``[[runbook]]``
   却指望它变成真实图边"；``test_reconcile_detects_torn_write`` 对应
   "先 register 后 upsert，中间进程被杀"；``test_resolved_existing_follows_symlinks``
   对应 macOS 上 ``/tmp`` → ``/private/tmp`` 那个把所有文件同时算成
   added 与 deleted 的坑。
5. **失败路径一条都不能少**：坏 YAML、非 mapping、超长标签、空文件、
   读不到的文件、悬空 wikilink、撕裂状态、磁盘上被删掉的文件 ——
   这些才是写入层的日常。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson16_memory_write.py -v
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from agentscope.agent import Agent
from agentscope.message import AssistantMsg, UserMsg

from harness_kit.memory import (
    DEFAULT_TAG_KEY,
    CatalogManager,
    ChangeSet,
    FrontMatter,
    FrontMatterError,
    HarnessMemoryConfig,
    MemoryClient,
    MemoryIngestor,
    MemoryUnavailableError,
    ReMeWorkspace,
    ReconcileReport,
    SessionDistiller,
    normalize_query_tags,
    normalize_tags,
    split_front_matter,
)
from harness_kit.models.adapters.echo import EchoChatModel

#: 与 ``config/default.yaml`` 的 ``jobs`` 名单对齐的嵌入式白名单。
#: **必须抄全**：``auto_memory`` 内部还会调 ``daily_list`` / ``daily_write``，
#: 少一个就会在 ``run_job`` 里报 "Job daily_list not found"。
MEMORY_JOBS: tuple[str, ...] = (
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

#: B/C 段用的多节文档（front matter 自带标签，正文里有两条 wikilink）。
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


# ======================================================================
# 夹具
# ======================================================================
async def _started(tmp_path: Path, name: str = "ws", **components: Any) -> tuple[MemoryClient, ReMeWorkspace]:
    """起一个隔离的嵌入式 ReMe（不占端口、不起服务）。

    Args:
        tmp_path (`Path`): pytest 的临时目录。
        name (`str`): 工作区子目录名。
        **components (`Any`): 组件覆盖，透传 ``HarnessMemoryConfig.with_components``。

    Returns:
        `tuple[MemoryClient, ReMeWorkspace]`: 已 start 的客户端与工作区。

    Raises:
        `pytest.skip`: ``reme`` 不可用时跳过（离线 CI 的正常路径）。
    """
    workspace = ReMeWorkspace(root=tmp_path / name)
    workspace.ensure()
    builder = HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None).with_jobs(*MEMORY_JOBS)
    if components:
        builder = builder.with_components(**components)
    client = MemoryClient(builder.build())
    try:
        await client.start()
    except MemoryUnavailableError as exc:  # pragma: no cover - 本环境已装
        pytest.skip(f"reme 不可用，跳过集成测试: {exc}")
    return client, workspace


@pytest.fixture()
async def memory(tmp_path: Path) -> Any:
    """一个已启动的嵌入式 ReMe（每个测试一个，见纪律 3）。

    Yields:
        `tuple[MemoryClient, ReMeWorkspace]`: 客户端与工作区。
    """
    client, workspace = await _started(tmp_path)
    try:
        yield client, workspace
    finally:
        await client.aclose()


def _tick(path: Path, seconds: float = 5.0) -> float:
    """把文件的 mtime 往后拨，保证与上一次写入**严格不等**。

    ``st_mtime`` 的精度在不同文件系统上不一样（有的只有 1 秒），
    "写完立刻再写"经常拿到同一个时间戳，于是断言会随机红。
    显式 ``utime`` 是最省事的确定性做法。

    Args:
        path (`Path`): 目标文件。
        seconds (`float`): 往后拨多少秒。

    Returns:
        `float`: 设置后的 mtime。
    """
    target = time.time() + seconds
    os.utime(path, (target, target))
    return target


# ======================================================================
# 一、frontmatter 的拆分规则（纯函数，0 次 ReMe 启动）
# ======================================================================
class TestSplitFrontMatter:
    """分隔符规则必须与 ReMe 逐字一致，否则同一份文件两边理解不同。"""

    def test_normal_front_matter_returns_data_and_body(self) -> None:
        """正常情况：取两个 ``---`` 之间的 YAML，正文 lstrip 换行。"""
        data, body = split_front_matter("---\nname: 手册\nmemory_tags: [ops]\n---\n\n# 标题\n")
        assert data == {"name": "手册", "memory_tags": ["ops"]}
        assert body == "# 标题\n"

    def test_without_leading_delimiter_returns_full_text(self) -> None:
        """不以 ``---`` 开头：front matter 为空，正文是全文（不 strip）。"""
        text = "# 标题\n\n正文\n"
        assert split_front_matter(text) == ({}, text)

    def test_single_delimiter_is_not_front_matter(self) -> None:
        """只有开头一个 ``---``：视为没有 front matter，**不报错**。

        这一条对应 ``default_file_chunker.py:44-49`` 的探测逻辑：
        找不到 ``"\\n---"`` 就当没有。如果 harness 在这里抛异常，
        一个以 ``---`` 开头的普通 Markdown 就会被我们拒收。
        """
        text = "---\nname: x\n正文\n"
        assert split_front_matter(text) == ({}, text)

    def test_invalid_yaml_raises_front_matter_error(self) -> None:
        """YAML 非法 → 抛（ReMe 在那里是静默的，harness 必须能炸）。"""
        with pytest.raises(FrontMatterError, match="YAML"):
            split_front_matter("---\nname: [unclosed\n---\n\nbody\n")

    def test_non_mapping_raises_front_matter_error(self) -> None:
        """YAML 合法但不是 mapping（比如序列）→ 抛。"""
        with pytest.raises(FrontMatterError, match="mapping"):
            split_front_matter("---\n- a\n- b\n---\n\nbody\n")

    def test_strict_false_degrades_to_no_front_matter(self) -> None:
        """``strict=False`` 时不抛，退化成「没有 front matter」。"""
        text = "---\nname: [unclosed\n---\n\nbody\n"
        fm, body = FrontMatter.parse(text, strict=False)
        assert fm.name is None and fm.extra == {}
        assert body == text

    def test_empty_front_matter_block_is_empty_dict(self) -> None:
        """``---\\n---`` 空块 → ``{}``，不是 ``None``。"""
        data, body = split_front_matter("---\n---\n\nbody\n")
        assert data == {} and body == "body\n"


# ======================================================================
# 二、标签治理：写入侧与查询侧是两把尺子
# ======================================================================
class TestTagGovernance:
    """``normalize_tags`` 与 ``normalize_query_tags`` 的差别是**故意的**。"""

    def test_write_side_caps_at_three(self) -> None:
        """写入侧最多 3 个（``max_tags_per_file``）。"""
        assert normalize_tags(["ops", "deploy", "x", "y", "z"]) == ["ops", "deploy", "x"]

    def test_query_side_has_no_cap(self) -> None:
        """查询侧不截断条数，否则 AND 查询会被悄悄削弱。"""
        requested = ["ops", "deploy", "x", "y", "z"]
        assert normalize_query_tags(requested) == ["ops", "deploy", "x", "y", "z"]
        assert normalize_query_tags(requested) == normalize_tags(requested, max_tags=1 << 30)

    def test_whitespace_and_case_are_normalized(self) -> None:
        """空白 → ``_``，大小写 casefold。"""
        assert normalize_tags(["Deploy Plan", "OPS"]) == ["deploy_plan", "ops"]

    def test_tags_without_alnum_or_too_long_are_dropped(self) -> None:
        """不含字母数字的、超过 64 字符的，一律丢掉。"""
        assert normalize_tags(["!!!", "   ", "a" * 65, "ok"]) == ["ok"]

    def test_non_string_items_are_skipped(self) -> None:
        """``None`` / bool / dict 不是标签；int 是（会变成字符串）。"""
        assert normalize_tags([None, True, {"a": 1}, 3, "x"]) == ["3", "x"]

    def test_non_list_input_returns_empty(self) -> None:
        """标量、字符串、``None`` 都当"没有标签"，而不是抛。"""
        for bad in (None, "ops", 3, {"ops": 1}):
            assert normalize_tags(bad) == []

    def test_dedup_is_case_insensitive_and_keeps_order(self) -> None:
        """去重按 casefold，保留首次出现的顺序。"""
        assert normalize_tags(["Ops", "ops", "OPS", "deploy"]) == ["ops", "deploy"]

    def test_write_side_returns_what_really_took_effect(self) -> None:
        """``FrontMatter.set_tags`` 返回**实际生效**的标签，而不是请求的。"""
        fm = FrontMatter()
        assert fm.set_tags(["Ops", "deploy plan", "!!!", "x", "y"]) == ["ops", "deploy_plan", "x"]
        assert fm.extra[DEFAULT_TAG_KEY] == ["ops", "deploy_plan", "x"]
        assert fm.tags() == ["ops", "deploy_plan", "x"]

    def test_set_tags_with_all_invalid_removes_the_key(self) -> None:
        """全部被裁掉时**删掉这个键**，而不是留一个空列表。"""
        fm = FrontMatter(extra={DEFAULT_TAG_KEY: ["old"]})
        assert fm.set_tags(["!!!"]) == []
        assert DEFAULT_TAG_KEY not in fm.extra


# ======================================================================
# 三、渲染与指纹
# ======================================================================
class TestFrontMatterRender:
    """渲染的三个性质：键序稳定、正文不动、指纹与顺序无关。"""

    def test_render_keeps_first_class_order_and_unicode(self) -> None:
        """一等字段在前、中文不转义、extra 保持插入顺序。"""
        fm = FrontMatter(name="部署手册", description="上线前检查", extra={"memory_tags": ["ops"]})
        text = fm.render("# 标题\n")
        assert text.startswith("---\nname: 部署手册\ndescription: 上线前检查\nmemory_tags:\n- ops\n---\n\n")
        assert text.endswith("# 标题\n")

    def test_render_skips_none_fields(self) -> None:
        """``None`` 表示"不写这个键"，而不是写 ``null``。"""
        text = FrontMatter(extra={"a": 1}).render("body\n", ensure_trailing_newline=False)
        assert "name" not in text and "description" not in text
        assert text == "---\na: 1\n---\n\nbody\n"

    def test_render_without_front_matter_returns_body(self) -> None:
        """front matter 为空 → 直接返回正文，一个 ``---`` 都不加。"""
        assert FrontMatter().render("只有正文\n", ensure_trailing_newline=False) == "只有正文\n"

    def test_ensure_trailing_newline(self) -> None:
        """默认补尾部换行（与 ReMe 的 write step 一致）。"""
        assert FrontMatter().render("body") == "body\n"

    def test_round_trip_is_stable(self) -> None:
        """``parse(render(body))`` 的指纹与原文一致，且正文不被改动。"""
        fm = FrontMatter(name="n", extra={"memory_tags": ["ops", "值班"]})
        text = fm.render("# 标题\n\n正文\n")
        again, body = FrontMatter.parse(text)
        assert body == "# 标题\n\n正文\n"
        assert again.fingerprint() == fm.fingerprint()

    def test_fingerprint_ignores_key_order(self) -> None:
        """指纹与 dict 顺序无关，否则幂等判据会误判成"内容变了"。"""
        assert FrontMatter(extra={"a": 1, "b": 2}).fingerprint() == FrontMatter(
            extra={"b": 2, "a": 1},
        ).fingerprint()

    def test_fingerprint_changes_when_tag_changes(self) -> None:
        """改了标签指纹必须变 —— 这正是"改标签要重新索引"的判据。"""
        one = FrontMatter(extra={"memory_tags": ["ops"]})
        two = FrontMatter(extra={"memory_tags": ["ops", "deploy"]})
        assert one.fingerprint() != two.fingerprint()

    def test_from_file_reads_utf8(self, tmp_path: Path) -> None:
        """``from_file`` 用 UTF-8 读（ReMe 的 chunker 也是 UTF-8）。"""
        path = tmp_path / "a.md"
        path.write_text("---\nname: 中文\n---\n\n正文\n", encoding="utf-8")
        fm, body = FrontMatter.from_file(path)
        assert fm.name == "中文" and body == "正文\n"


# ======================================================================
# 四、与 ReMe 原生类型的桥
# ======================================================================
class TestReMeTypeBridge:
    """``FileFrontMatter`` 只有两个一等字段，其余全在 ``__pydantic_extra__``。"""

    def test_only_name_and_description_are_first_class(self) -> None:
        """一等字段逐字来自 ``schema/file_front_matter.py:8``。"""
        from reme.schema import FileFrontMatter

        assert sorted(FileFrontMatter.model_fields) == ["description", "name"]

    def test_to_file_front_matter_puts_extra_into_model_extra(self) -> None:
        """harness 的 ``extra`` → ReMe 的 ``__pydantic_extra__``。

        注意 ``extra="allow"`` 的**两个后果**同时成立：``memory_tags``
        既能用属性访问（``native.memory_tags`` 拿得到），又**不在**
        ``model_fields`` 里。所以判断"这是不是一等字段"只能看
        ``model_fields``，不能看 ``hasattr`` —— 后者永远为真。
        """
        from reme.schema import FileFrontMatter

        native = FrontMatter(name="n", extra={DEFAULT_TAG_KEY: ["ops"]}).to_file_front_matter()
        assert native.name == "n"
        assert native.model_extra == {DEFAULT_TAG_KEY: ["ops"]}
        assert native.memory_tags == ["ops"]  # 属性访问拿得到……
        assert DEFAULT_TAG_KEY not in FileFrontMatter.model_fields  # ……但它不是一等字段

    def test_from_file_front_matter_round_trip(self) -> None:
        """转过去再转回来，指纹不变（空串与 ``None`` 视为同一件事）。"""
        original = FrontMatter(name="n", description="d", extra={"memory_tags": ["ops"]})
        back = FrontMatter.from_file_front_matter(original.to_file_front_matter())
        assert back.fingerprint() == original.fingerprint()

    def test_none_name_becomes_empty_string_in_reme(self) -> None:
        """ReMe 侧 ``name`` 的默认值是 ``""``，所以 ``None`` 不该被写成 ``"None"``。"""
        native = FrontMatter().to_file_front_matter()
        assert native.name == ""
        assert FrontMatter.from_file_front_matter(native).name is None


# ======================================================================
# 五、catalog 的纯计算部分（不启动 ReMe）
# ======================================================================
class TestCatalogPure:
    """``ChangeSet`` / ``ReconcileReport`` / 路径解析 —— 全是纯函数。"""

    def test_change_set_paths_excludes_deleted(self) -> None:
        """``paths()`` 只给"要重新 upsert"的，不给 deleted。"""
        changes = ChangeSet(
            added=["resource/b.md"],
            modified=["resource/a.md"],
            deleted=["resource/gone.md"],
            counts={"added": 1, "modified": 1, "deleted": 1},
        )
        assert changes.paths() == ["resource/a.md", "resource/b.md"]
        assert changes.total() == 3
        assert changes.is_empty() is False
        assert ChangeSet().is_empty() is True

    def test_reconcile_report_clean_checks_all_eight_buckets(self) -> None:
        """``clean`` 必须覆盖 8 个桶，漏一个就等于"悄悄放过一类不一致"。"""
        assert ReconcileReport().clean is True
        buckets = (
            "missing_in_catalog",
            "missing_in_graph",
            "catalog_mtime_drift",
            "graph_mtime_drift",
            "stale_in_catalog",
            "stale_in_graph",
            "untracked_by_ingest",
            "orphaned_in_ingest",
        )
        for bucket in buckets:
            report = ReconcileReport(**{bucket: ["resource/x.md"]})
            assert report.clean is False, bucket

    def test_reconcile_report_summary_has_four_counts(self) -> None:
        """``summary()`` 把四方计数一次说清 —— 这是给人看的那一行。

        ``clean`` 只看**八类具名不一致**，**不看四个计数是否相等**。
        这是刻意的：``ingested=None`` 时 ``ingest_files`` 恒为 0，若让计数
        参与 ``clean``，一个"只查磁盘 vs 索引、不查写入状态"的调用永远不干净。
        计数是给人看的线索，具名清单才是判据。
        """
        report = ReconcileReport(scanned_files=3, catalog_files=3, graph_files=3, ingest_files=3)
        assert report.summary() == "对账 OK: 磁盘 3 / catalog 3 / graph 3 / ingest 3"
        report = ReconcileReport(scanned_files=3, catalog_files=3, graph_files=3, ingest_files=3)
        report.missing_in_graph.append("resource/torn.md")
        assert report.summary() == "发现不一致: 磁盘 3 / catalog 3 / graph 3 / ingest 3"
        # 计数不等但没有任何具名不一致 → 仍然算"干净"（见 docstring）。
        odd = ReconcileReport(scanned_files=2, catalog_files=2, graph_files=2, ingest_files=0)
        assert odd.clean is True
        assert odd.summary() == "对账 OK: 磁盘 2 / catalog 2 / graph 2 / ingest 0"

    def test_resolved_existing_follows_symlinks(self, tmp_path: Path) -> None:
        """``_resolved_existing`` 必须 ``resolve()``，否则 ``/tmp`` 会误判越界。

        ``collect_existing`` 用的是 ``Path.absolute()``（不解析软链，
        ``_watch_rules.py:69``），而 ``ReMeWorkspace.root`` 是 ``resolve()`` 过的。
        在 macOS 上 ``/tmp`` 是 ``/private/tmp`` 的软链 —— 两边不统一，
        ``InitChangesStep.diff`` 会把每个文件同时算成 added 与 deleted。
        """
        link = tmp_path / "link"
        real = tmp_path / "real"
        real.mkdir()
        link.symlink_to(real, target_is_directory=True)
        resolved = CatalogManager._resolved_existing({str(link / "a.md"): 1.0})
        assert list(resolved) == [str((real / "a.md").resolve())]

    def test_normalize_write_and_query_are_different_tools(self) -> None:
        """两个静态方法的差别就是"截断"：3 个 vs 不限。"""
        tags = ["ops", "deploy", "值班", "第4个"]
        assert CatalogManager.normalize_write(tags) == ["ops", "deploy", "值班"]
        assert CatalogManager.normalize_query(tags) == ["ops", "deploy", "值班", "第4个"]

    def test_default_tag_key_matches_reme_config(self) -> None:
        """``DEFAULT_TAG_KEY`` 必须等于 ``default.yaml`` 的 ``tag_index.tag_key``。

        这是**唯一一条跨仓库的常量契约**：harness 写 ``memory_tags``、
        ReMe 的 ``LocalTagIndex`` 读 ``memory_tags``（``tag_key`` 可配）。
        写错了不会报错，只是标签永远查不到 —— 所以用一条测试把它钉住。

        定位 ``default.yaml`` 用的是**当前 import 到的那个 reme 包**
        （而不是 ``__file__`` 往上数几层）：这样无论测试跑在参考实现的目录里，
        还是跑在 ``/tmp/lesson16_verify`` 这种"从 md 抽出来"的目录里，
        校验的都是真正在生效的那份配置 —— 顺便也把
        "site-packages 里的旧 reme 抢了 import" 这类问题暴露出来。
        """
        import reme

        assert DEFAULT_TAG_KEY == "memory_tags"
        config = Path(reme.__file__).parent / "config" / "default.yaml"
        assert config.is_file(), f"找不到 {config}（reme 装在了哪里？）"
        assert "tag_key: memory_tags" in config.read_text(encoding="utf-8")


# ======================================================================
# 六、写入路径（真的启动嵌入式 ReMe）
# ======================================================================
class TestIngest:
    """``MemoryIngestor``：写入、幂等、行锚点、图、审计。"""

    async def test_add_text_then_same_content_is_unchanged(self, memory: Any) -> None:
        """第一次真写、第二次内容相同 → ``added=False, skipped_reason='unchanged'``。"""
        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace, chunker="markdown")
        first = await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")
        assert first.added is True
        assert first.path == "resource/oncall.md"
        assert first.chunk_count >= 1
        assert first.skipped_reason is None
        assert (workspace.resource_path() / "oncall.md").is_file()

        second = await ingestor.add_file(workspace.resource_path() / "oncall.md")
        assert second.added is False
        assert second.skipped_reason == "unchanged"
        assert second.chunk_count == first.chunk_count

    async def test_changing_tags_reingests_because_bytes_changed(self, memory: Any) -> None:
        """改标签 → 文件字节变了 → digest 变了 → 必须重新入库（不是幂等失控）。

        顺带钉住规范化的边界：**只有空白**被折叠成 ``_``
        （``frontmatter.py:161`` 的 ``"_".join(str(item).split())``），
        连字符 ``-`` 原样保留 —— 别指望 ``on-call`` 会变成 ``on_call``。
        """
        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")
        stored = workspace.resource_path() / "oncall.md"
        before = stored.read_bytes()

        assert normalize_tags(["on-call", "on call"]) == ["on-call", "on_call"]
        effective = await ingestor.apply_tags(stored, ["Ops", "on call", "值班", "第4个"])
        assert effective == ["ops", "on_call", "值班"]
        assert stored.read_bytes() != before
        third = await ingestor.add_file(stored)
        assert third.added is True
        records = await ingestor.ingested()
        assert records["resource/oncall.md"]["tags"] == ["ops", "on_call", "值班"]

    async def test_outside_file_is_staged_and_deduped_by_source(self, memory: Any, tmp_path: Path) -> None:
        """区外文件复制进 ``resource/``，同一来源第二次复用已有副本。"""
        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        source = tmp_path / "elsewhere" / "runbook.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# Runbook\n\n回滚三步。\n", encoding="utf-8")

        first = await ingestor.add_file(source)
        second = await ingestor.add_file(source)
        assert first.path == "resource/runbook.md" and first.added is True
        assert second.path == first.path and second.added is False
        assert source.is_file()  # 原文件不动
        assert [p.name for p in workspace.resource_path().glob("*.md")] == ["runbook.md"]

    async def test_add_directory_survives_one_bad_file(self, memory: Any) -> None:
        """批量导入：正常 / 空 / 读不到 三种文件同现，一个异常都不抛。

        权限位这一条在 root 下不成立（root 永远读得到），所以 root 环境里
        只断言前两种，并把第三种显式跳过 —— 而不是把它写成一条永远红的测试。
        """
        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        docs = workspace.resource_path() / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "ok.md").write_text("# 好的\n\n正常入库。\n", encoding="utf-8")
        (docs / "empty.md").write_text("", encoding="utf-8")
        locked = docs / "locked.md"
        locked.write_text("# 锁住\n\n读不到。\n", encoding="utf-8")
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if not is_root:
            os.chmod(locked, 0o000)

        try:
            results = await ingestor.add_directory(docs)
        finally:
            if not is_root:
                os.chmod(locked, 0o644)

        by_path = {item.path: item for item in results}
        assert set(by_path) == {
            "resource/docs/ok.md",
            "resource/docs/empty.md",
            "resource/docs/locked.md",
        }
        assert by_path["resource/docs/ok.md"].added is True
        assert by_path["resource/docs/empty.md"].skipped_reason == "empty"
        assert by_path["resource/docs/empty.md"].chunk_count == 0
        if not is_root:
            assert str(by_path["resource/docs/locked.md"].skipped_reason).startswith(
                "error: PermissionError",
            )

    async def test_chunk_ids_and_line_anchors(self, tmp_path: Path) -> None:
        """行锚点是 1-based 全文件行号；chunk id 是确定性哈希。"""
        client, workspace = await _started(
            tmp_path,
            "chunks",
            file_chunker={"markdown": {"chunk_byte_size": 200}},
        )
        try:
            ingestor = MemoryIngestor(client, workspace=workspace, chunker="markdown")
            result = await ingestor.add_text(DEPLOY_DOC, name="deploy")
            assert result.chunk_count == 2, result
            stored = workspace.resource_path() / "deploy.md"
            chunker = ingestor._chunker(stored)
            node, chunks = await chunker.chunk(stored)

            total = len(stored.read_text(encoding="utf-8").split("\n"))
            assert node.path == "resource/deploy.md"
            assert node.front_matter.name == "部署手册"
            assert [c.start_line for c in chunks] == sorted(c.start_line for c in chunks)
            assert all(1 <= c.start_line <= c.end_line <= total for c in chunks)
            assert [c.id for c in chunks] == list(node.chunk_ids)

            _, again = await chunker.chunk(stored)
            assert [c.id for c in again] == [c.id for c in chunks]

            # 祖先标题面包屑：第二个 chunk 的行范围里没有 "# 部署手册" 这一行，
            # 但它的 text 里有 —— 这是 MarkdownFileChunker 的行为。
            heading_line = next(i for i, line in enumerate(stored.read_text("utf-8").split("\n"), 1) if line == "# 部署手册")
            assert not (chunks[1].start_line <= heading_line <= chunks[1].end_line)
            assert chunks[1].text.startswith("# 部署手册")
        finally:
            await client.aclose()

    async def test_wikilink_scopes(self, memory: Any) -> None:
        """REAL 要求两端都是已索引节点；悬空目标只在 VIRTUAL / ALL 里。"""
        from reme.enumeration import LinkScopeEnum

        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text(DEPLOY_DOC, name="deploy")
        await ingestor.add_text("# Runbook\n\n回滚三步。\n", name="runbook")
        store = client.component("file_store", "default")

        real = await store.get_outlinks("resource/deploy.md", LinkScopeEnum.REAL)
        virtual = await store.get_outlinks("resource/deploy.md", LinkScopeEnum.VIRTUAL)
        everything = await store.get_outlinks("resource/deploy.md", LinkScopeEnum.ALL)
        assert [(lnk.target_path, lnk.target_anchor) for lnk in real] == [("resource/runbook.md", "回滚")]
        assert [(lnk.target_path, lnk.target_anchor) for lnk in virtual] == [("resource/rollback-plan.md", None)]
        assert len(everything) == 2

        inlinks = await store.get_inlinks("resource/runbook.md", LinkScopeEnum.REAL)
        assert [lnk.source_path for lnk in inlinks] == ["resource/deploy.md"]
        assert "predicate" not in real[0].model_dump()

    async def test_short_wikilink_targets_stay_virtual(self, memory: Any) -> None:
        """回归：``[[runbook]]`` 这种短名**永远**不会变成真实图边。

        ReMe 的 wikilink 目标是字面量（``wikilink_handler.py:19-24``：
        不补 ``.md``、不做短名 basename 搜索），所以写短名只会得到一条
        悬空边。这条测试把"文档里推荐写全路径"这个约定钉死在代码里。
        """
        from reme.enumeration import LinkScopeEnum

        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 短名\n\n见 [[runbook]] 与 [[deploy]]。\n", name="shortnames")
        await ingestor.add_text("# Runbook\n\n回滚三步。\n", name="runbook")
        store = client.component("file_store", "default")
        assert await store.get_outlinks("resource/shortnames.md", LinkScopeEnum.REAL) == []
        everything = await store.get_outlinks("resource/shortnames.md", LinkScopeEnum.ALL)
        assert sorted(lnk.target_path for lnk in everything) == ["deploy", "runbook"]

    async def test_ingest_state_is_a_write_audit(self, memory: Any) -> None:
        """``ingest_state.json`` 逐条记录摘要素，且它是**可丢弃**的。"""
        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall", tags=["ops"])
        await ingestor.add_file(workspace.resource_path() / "oncall.md")

        state_path = ingestor.state_path()
        assert workspace.is_inside(state_path)
        assert state_path.name == "ingest_state.json"
        records = await ingestor.ingested()
        assert set(records) == {"resource/oncall.md"}
        record = records["resource/oncall.md"]
        assert record["tags"] == ["ops"] and record["chunk_count"] >= 1
        assert len(record["digest"]) == 64
        stats = await ingestor.stats()
        assert stats["files"] == 1 and stats["chunks"] == record["chunk_count"]

        # 状态文件坏掉的正确反应是"当成空状态重来"：真值在磁盘上的 .md 里。
        state_path.write_text("{ 这不是 JSON", encoding="utf-8")
        assert await ingestor.ingested() == {}

    async def test_remove_file_touches_index_only(self, memory: Any) -> None:
        """``remove_file`` 只动索引与状态，不动磁盘文件；重复调用返回 False。"""
        client, workspace = memory
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")
        stored = workspace.resource_path() / "oncall.md"
        assert await ingestor.remove_file("resource/oncall.md") is True
        assert stored.is_file() is True
        assert await ingestor.ingested() == {}
        assert await ingestor.remove_file("resource/oncall.md") is False


# ======================================================================
# 七、台账：扫描与对账
# ======================================================================
class TestCatalogScanAndReconcile:
    """``scan_changes`` 与 ``reconcile`` —— 本讲新增的那两个能力。"""

    async def test_scan_detects_added_modified_deleted(self, memory: Any) -> None:
        """三类变更各来一次：added / modified / deleted。"""
        client, workspace = memory
        catalog = CatalogManager(client)
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")
        assert (await catalog.scan_changes()).is_empty()

        rogue = workspace.resource_path() / "rogue.md"
        rogue.write_text("# 手工放进来\n", encoding="utf-8")
        scan = await catalog.scan_changes()
        assert scan.added == ["resource/rogue.md"]
        assert scan.paths() == ["resource/rogue.md"]

        oncall = workspace.resource_path() / "oncall.md"
        oncall.write_text("# 值班手册\n\n改过了。\n", encoding="utf-8")
        _tick(oncall)
        scan = await catalog.scan_changes()
        assert scan.modified == ["resource/oncall.md"]

        rogue.unlink()
        rogue.write_text("# 手工放进来\n", encoding="utf-8")  # 先让它回到 catalog 里
        await catalog.register("resource/rogue.md")
        _tick(rogue)
        rogue.unlink()
        scan = await catalog.scan_changes()
        assert "resource/rogue.md" in scan.deleted

    async def test_reconcile_is_clean_after_ingest(self, memory: Any) -> None:
        """四方对齐：磁盘 / catalog / graph / ingest_state。"""
        client, workspace = memory
        catalog = CatalogManager(client)
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")
        report = await catalog.reconcile(ingested=await ingestor.ingested())
        assert report.clean is True, report.model_dump()
        assert report.summary() == "对账 OK: 磁盘 1 / catalog 1 / graph 1 / ingest 1"

    async def test_reconcile_detects_torn_write(self, memory: Any) -> None:
        """回归：先 ``register`` 后 ``upsert``，中间进程被杀。

        这时 catalog 有、graph 没有。若增量扫描拿 catalog 当快照，
        这个文件会**永久检索不到** —— 所以对账必须把两者分开问。
        """
        client, workspace = memory
        catalog = CatalogManager(client)
        ingestor = MemoryIngestor(client, workspace=workspace)
        torn = workspace.resource_path() / "torn.md"
        torn.write_text("# 撕裂\n", encoding="utf-8")
        assert await catalog.register("resource/torn.md") is True

        report = await catalog.reconcile(ingested=await ingestor.ingested())
        assert report.missing_in_graph == ["resource/torn.md"]
        assert report.missing_in_catalog == []
        assert report.untracked_by_ingest == ["resource/torn.md"]
        assert report.clean is False

        await ingestor.add_file(torn)
        report = await catalog.reconcile(ingested=await ingestor.ingested())
        assert report.clean is True, report.model_dump()

    async def test_reconcile_detects_stale_graph_entries(self, memory: Any) -> None:
        """磁盘上被手工删掉的文件：stale_in_graph + orphaned_in_ingest。"""
        client, workspace = memory
        catalog = CatalogManager(client)
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")
        (workspace.resource_path() / "oncall.md").unlink()

        report = await catalog.reconcile(ingested=await ingestor.ingested())
        assert report.stale_in_graph == ["resource/oncall.md"]
        assert report.stale_in_catalog == ["resource/oncall.md"]
        assert report.orphaned_in_ingest == ["resource/oncall.md"]
        assert report.clean is False
        assert (await catalog.scan_changes()).deleted == ["resource/oncall.md"]

    async def test_ensure_catalog_and_list(self, memory: Any) -> None:
        """``ensure_catalog`` 幂等，且会出现在 ``list_catalogs`` 里。"""
        client, _ = memory
        catalog = CatalogManager(client)
        assert "default" in await catalog.list_catalogs()
        await catalog.ensure_catalog("lesson16")
        await catalog.ensure_catalog("lesson16")
        assert "lesson16" in await catalog.list_catalogs()
        with pytest.raises(ValueError, match="不能为空"):
            await catalog.ensure_catalog("  ")

    async def test_set_tags_flows_into_tag_index(self, memory: Any) -> None:
        """``set_tags`` 改磁盘 front matter + 重新入库，标签立即可查。"""
        client, workspace = memory
        catalog = CatalogManager(client)
        ingestor = MemoryIngestor(client, workspace=workspace)
        await ingestor.add_text("# 值班手册\n\n报警先看日志。\n", name="oncall")

        effective = await catalog.set_tags("resource/oncall.md", ["Ops", "on call", "值班", "第4个"])
        assert effective == ["ops", "on_call", "值班"]
        head, _ = FrontMatter.from_file(workspace.resource_path() / "oncall.md")
        assert head.tags() == ["ops", "on_call", "值班"]
        assert "resource/oncall.md" in await catalog.paths_for_tags(["ops"])
        page = await catalog.tags_page(page=1, page_size=10)
        assert ("ops", 1) in page["items"]

    async def test_set_tags_rejects_paths_outside_workspace(self, memory: Any, tmp_path: Path) -> None:
        """工作区外的文件必须先 ingest 复制进来 —— 否则拒绝，并说清原因。"""
        client, workspace = memory
        catalog = CatalogManager(client)
        outside = tmp_path / "outside.md"
        outside.write_text("# 外面\n", encoding="utf-8")
        with pytest.raises(ValueError, match="只能改工作区内的文件"):
            await catalog.set_tags(str(outside), ["ops"])


# ======================================================================
# 八、distill 的整形层（0 次模型调用，也不需要 ReMe）
# ======================================================================
class TestSessionDistillerShaping:
    """``shape_messages`` / ``messages_from_agent`` —— 只做减法，不调模型。"""

    def test_shape_drops_memory_hint_and_blank_messages(self) -> None:
        """丢弃记忆提示消息与空消息，其余原样保留顺序。"""
        context = [
            UserMsg("user", "我们把部署工具从 pip 换成 uv 了。"),
            AssistantMsg("assistant", "记住了。"),
            UserMsg("memory", "从记忆库召回的内容"),
            UserMsg("user", "   "),
        ]
        shaped = SessionDistiller.shape_messages(context)
        assert [item["name"] for item in shaped] == ["user", "assistant"]
        assert all(isinstance(item["content"], list) for item in shaped)
        assert all(isinstance(block, dict) for item in shaped for block in item["content"])

    def test_shape_wraps_string_content_of_dicts(self) -> None:
        """从 JSON 反序列化来的 ``{"content": "x"}`` 会被包成文本块。

        ``Msg(content="x")`` 在 AgentScope 2.0.8 里直接 ``ValidationError``
        （``message/_base.py:79`` 的 ``content: list[ContentBlock]``）。
        """
        shaped = SessionDistiller.shape_messages([{"name": "user", "role": "user", "content": "你好"}])
        assert shaped[0]["content"] == [
            block for block in shaped[0]["content"] if block["type"] == "text"
        ]
        assert shaped[0]["content"][0]["text"] == "你好"

    def test_shape_rejects_unknown_types(self) -> None:
        """既不是 ``Msg`` 也不是 dict → ``ValueError``，并说清收到了什么。"""
        with pytest.raises(ValueError, match="只接受 Msg 或 dict"):
            SessionDistiller.shape_messages([42])

    def test_shape_accepts_empty_input(self) -> None:
        """空输入返回空列表：真正的"没有值得记的东西"由 ``auto_memory`` 判定。"""
        assert SessionDistiller.shape_messages([]) == []
        assert SessionDistiller.shape_messages(None) == []

    def test_messages_from_agent_does_not_filter(self) -> None:
        """``messages_from_agent`` 只做"取"，过滤是 ``shape_messages`` 的职责。"""
        agent = Agent(name="a", system_prompt="hi", model=EchoChatModel())
        agent.state.context.append(UserMsg("user", "一"))
        agent.state.context.append(AssistantMsg("a", "二"))
        agent.state.context.append(UserMsg("memory", "召回内容"))
        messages = SessionDistiller.messages_from_agent(agent)
        assert [msg.name for msg in messages] == ["user", "a", "memory"]
        assert len(SessionDistiller.shape_messages(messages)) == 2

    def test_messages_from_agent_since_id(self) -> None:
        """``since_id`` 给的是**它之后**的消息（不含它自己）。"""
        agent = Agent(name="a", system_prompt="hi", model=EchoChatModel())
        agent.state.context.append(UserMsg("user", "一"))
        agent.state.context.append(AssistantMsg("a", "二"))
        agent.state.context.append(UserMsg("user", "三"))
        first_id = agent.state.context[0].id
        assert len(SessionDistiller.messages_from_agent(agent, since_id=first_id)) == 2
        assert SessionDistiller.messages_from_agent(agent, since_id="不存在") == []

    def test_messages_from_agent_without_state(self) -> None:
        """没有 ``state.context`` 的对象 → ``ValueError``（而不是静默返回空）。"""
        with pytest.raises(ValueError, match="state.context"):
            SessionDistiller.messages_from_agent(object())

    async def test_distill_requires_session_id(self, memory: Any) -> None:
        """空 ``session_id`` 在本地就被挡住，理由是 ReMe 会直接判失败。"""
        client, workspace = memory
        distiller = SessionDistiller(client, workspace=workspace)
        with pytest.raises(ValueError, match="session_id"):
            await distiller.distill([UserMsg("user", "hi")], session_id="  ")

    async def test_distill_with_no_messages_does_not_call_model(self, memory: Any) -> None:
        """空会话：整形后是空列表，ReMe 直接返回"没有笔记"，**0 次模型调用**。

        这条测试是"离线可跑"的保证：它证明了 distill 的失败/空路径不需要
        花钱。真实模型的蒸馏在 ``scripts/16_memory_write.py --live``。

        细节：``path=None`` 时 ``_read_note`` 读不到文件，``content`` 就
        退化成 ``auto_memory`` 的 ``answer`` —— 空输入时 ReMe 给的正是
        ``"Skipped: no messages"``（实测值）。所以**不要**断言
        ``content == ""``：那是在假设 ReMe 不解释自己为什么没写。
        """
        client, workspace = memory
        distiller = SessionDistiller(client, workspace=workspace)
        result = await distiller.distill([], session_id="empty-session")
        assert result.path is None
        assert result.created is False
        assert result.content == "Skipped: no messages"
