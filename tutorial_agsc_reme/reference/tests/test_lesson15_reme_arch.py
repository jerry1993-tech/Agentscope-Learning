# -*- coding: utf-8 -*-
"""第 15 讲的 pytest：ReMe 架构总览配套的四个 harness_kit 模块。

五条纪律（延续第 14 讲，但本讲的"假东西"换了一批）：

1. **0 次 LLM 调用**。本讲测的是"配置与生命周期"，不是模型行为。真实模型调用
   在 ``scripts/01_reme_doctor.py --live``（1 次 deepseek-flash）。
2. **"不用真的启动 ReMe 也能测"是第一优先级**。``run_job`` 的错误归一
   （假成功拦截、``success=False`` → :class:`MemoryJobError`、未知 job 名）
   全部靠本模块里的 :class:`_FakeApp` 驱动 —— 它是一个只有 12 行的替身，
   ``is_started`` / ``context.jobs`` / ``run_job`` 三个属性就够。
   只有两条**集成测试**真的 ``await client.start()``：它们要证明的恰恰是
   "harness 的配置能被 ReMe 真的吃下去"，替身证明不了这件事。
3. **回归测试必须钉住"已经被踩过的坑"**，而不是泛泛地测功能。本模块里
   ``test_build_drops_resident_jobs_after_whitelist`` 对应"深合并把删掉的
   background job 又留下来"这个真实 bug；``test_workspace_relative_is_idempotent``
   对应"相对路径进、绝对路径出"那个把 tag_index 打挂的 bug。
4. **环境真相要如实断言，不能假装它是绿的**。``agentscope.reme_middleware``
   这一项在本环境**确实**是 ``ok=False``（reme 0.4.1.13 没注册
   ``dream_topics_step``），第 19 讲才修。所以测试把它**单独**处理：
   要么断言 detail 里点名了 ``dream_topics_step``，要么 ``skip``，
   绝不写成 ``assert doctor.ok()``（那会变成一条永远红着的测试，
   然后被人加 ``xfail`` 掩掉）。
5. **每个测试自己建工作区，绝不共享**。``HarnessMemoryConfig`` 的 ``build()``
   结果是深拷贝、``clean()`` 会真删文件、``destroy()`` 会真删目录 ——
   共享 ``tmp_path`` 会制造"测试顺序变了就红"的幽灵失败。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson15_reme_arch.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from harness_kit.config import load_profile
from harness_kit.config.schema import MemorySpec
from harness_kit.memory import (
    DEFAULT_SUBDIRS,
    EMBEDDED_JOB_BACKENDS,
    RESCAN_REINDEX_JOB,
    CheckResult,
    HarnessMemoryConfig,
    MemoryClient,
    MemoryConfigError,
    MemoryDoctor,
    MemoryJobError,
    MemoryUnavailableError,
    ReMeWorkspace,
    WorkspaceError,
    reme_version,
)

#: ``tests/`` 的上一级 = ``tutorial_agsc_reme/reference``。
REFERENCE_ROOT: Path = Path(__file__).resolve().parents[1]
#: Profile YAML 的搜索目录。
PROFILE_DIR: Path = REFERENCE_ROOT / "harness_kit" / "profiles"

#: ``MemoryDoctor.check()`` 逐字期望的 12 个检查项名（契约 §3.15）。
EXPECTED_CHECK_NAMES: tuple[str, ...] = (
    "reme.version",
    "reme.path",
    "config.parse",
    "config.service",
    "config.jobs",
    "registry.jobs",
    "registry.steps",
    "registry.components",
    "workspace.dirs",
    "workspace.writable",
    "agentscope.version",
    "agentscope.reme_middleware",
)


def _ws(tmp_path: Path, name: str = "reme") -> ReMeWorkspace:
    """造一个已 ``ensure()`` 的独立工作区。

    Args:
        tmp_path (`Path`): pytest 给的临时目录。
        name (`str`): 子目录名，便于同一个测试里造多个互不干扰的工作区。

    Returns:
        `ReMeWorkspace`: 六目录齐全的工作区。
    """
    ws = ReMeWorkspace(root=tmp_path / name)
    ws.ensure()
    return ws


# ======================================================================
# 一、ReMeWorkspace：路径语义 + 生命周期
# ======================================================================
class TestWorkspace:
    """工作区是"所有相对路径的唯一解释者"，所以它必须先被钉死。"""

    def test_default_subdirs_match_reme_schema(self) -> None:
        """六个子目录名逐字来自 ``reme/schema/application_config.py``。"""
        assert [name for name, _ in DEFAULT_SUBDIRS] == [
            "metadata_dir",
            "session_dir",
            "mem_session_dir",
            "resource_dir",
            "daily_dir",
            "digest_dir",
        ]
        assert [default for _, default in DEFAULT_SUBDIRS] == [
            "metadata",
            "session",
            "mem_session",
            "resource",
            "daily",
            "digest",
        ]

    def test_empty_root_is_rejected_before_pydantic(self) -> None:
        """空字符串 root 必须抛 :class:`WorkspaceError`，而不是静默绑定 cwd。

        这是本仓库最贵的一个坑：``ReMeWorkspace(root="")`` 在修好之前，
        ``root`` 会变成 ``Path("")`` → ``Path(".")`` → 解析后是 cwd，
        于是 :meth:`~ReMeWorkspace.destroy` 会去删当前工作目录。
        """
        for bad in ("", "   ", "\t\n"):
            with pytest.raises(WorkspaceError, match="不能为空"):
                ReMeWorkspace(root=bad)
        with pytest.raises(WorkspaceError, match="需要 root"):
            ReMeWorkspace()

    def test_root_is_resolved_and_expanded(self, tmp_path: Path) -> None:
        """``root`` 一定被 ``resolve()``：否则 ``/tmp`` → ``/private/tmp`` 会误判越界。"""
        ws = ReMeWorkspace(root=tmp_path / "a" / ".." / "b")
        assert ws.root.is_absolute()
        assert ".." not in ws.root.parts
        assert ws.root == (tmp_path / "b").resolve()

    @pytest.mark.parametrize("field", ["metadata_dir", "session_dir", "mem_session_dir", "resource_dir", "daily_dir", "digest_dir"])
    def test_subdir_must_be_single_relative_segment(self, tmp_path: Path, field: str) -> None:
        """子目录名不许绝对路径、不许 ``..``、不许两级。

        Raises:
            `ValidationError`: 三种非法写法都必须被 pydantic 挡下。
        """
        for bad in ("../evil", "/abs", "a/b", "..", ".", ""):
            with pytest.raises(ValidationError):
                ReMeWorkspace(root=tmp_path / "ws", **{field: bad})

    def test_subdir_unknown_field_raises_workspace_error(self, tmp_path: Path) -> None:
        """``subdir()`` 只认 :data:`DEFAULT_SUBDIRS` 里的字段名。"""
        ws = ReMeWorkspace(root=tmp_path / "ws")
        with pytest.raises(WorkspaceError, match="未知的工作区子目录字段"):
            ws.subdir("cache_dir")

    def test_ensure_is_idempotent_and_validate_is_readonly(self, tmp_path: Path) -> None:
        """``ensure()`` 幂等；``validate()`` **不创建**任何目录（所以能安全进体检）。"""
        ws = ReMeWorkspace(root=tmp_path / "ws")
        assert ws.validate() == [f"工作区根不存在: {ws.root}"]

        ws.ensure()
        ws.ensure()
        assert ws.validate() == []
        assert ws.is_healthy() is True
        assert all(path.is_dir() for path in ws.all_paths())

        # 只读：删掉一个子目录后 validate 报缺，但不会替我们补回来。
        ws.digest_path().rmdir()
        assert ws.validate() == [f"缺少子目录: digest"]
        assert not ws.digest_path().exists()

    def test_ensure_rejects_file_in_place_of_subdir(self, tmp_path: Path) -> None:
        """子目录位置被同名文件占用时必须报错，而不是静默跳过。"""
        ws = ReMeWorkspace(root=tmp_path / "ws")
        ws.root.mkdir(parents=True)
        (ws.root / "daily").write_text("not a dir", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="被同名文件占用"):
            ws.ensure()

    def test_dialog_path_is_session_dialog(self, tmp_path: Path) -> None:
        """ReMe 的对话原文落在 ``session/dialog/``，这是它的固定派生位置。"""
        ws = _ws(tmp_path)
        assert ws.dialog_path() == ws.session_path() / "dialog"
        assert ws.dialog_path().parent == ws.session_path()

    def test_dir_overrides_are_reme_config_keys(self, tmp_path: Path) -> None:
        """``dir_overrides()`` 的键必须能直接喂给 ``resolve_app_config(**kw)``。"""
        ws = _ws(tmp_path)
        overrides = ws.dir_overrides()
        assert set(overrides) == {name for name, _ in DEFAULT_SUBDIRS}
        assert overrides["daily_dir"] == "daily"

    def test_custom_subdir_names_flow_into_overrides(self, tmp_path: Path) -> None:
        """改名后 ``dir_overrides()`` 跟着变 —— 这是"两边同时改"的机制保证。"""
        ws = ReMeWorkspace(root=tmp_path / "ws", daily_dir="cards", digest_dir="docs")
        ws.ensure()
        assert ws.daily_path() == ws.root / "cards"
        assert ws.dir_overrides()["daily_dir"] == "cards"
        assert ws.dir_overrides()["digest_dir"] == "docs"

    def test_relative_is_idempotent_for_workspace_relative_input(self, tmp_path: Path) -> None:
        """**回归测试**：相对路径进、相对路径出。

        修好之前 ``relative("resource/x.md")`` 会先 ``Path.absolute()``
        再 ``relative_to``，产出一个基于 CWD 的 ``/private/tmp/...`` 绝对路径；
        检索结果里的路径被这样改写后，tag_index 的 ``_validate_path``
        会直接拒绝它（``local_tag_index.py:64-71``）。
        """
        ws = _ws(tmp_path)
        assert ws.relative("resource/x.md") == "resource/x.md"
        assert ws.relative("resource/x.md") == ws.relative(ws.relative("resource/x.md"))

    def test_relative_converts_absolute_inside_and_passes_outside(self, tmp_path: Path) -> None:
        """绝对路径：区内给相对、区外原样给绝对（与 ReMe 的 ``to_workspace_relative`` 一致）。"""
        ws = _ws(tmp_path)
        inside = ws.daily_path() / "2026-09-22" / "card.md"
        assert ws.relative(inside) == "daily/2026-09-22/card.md"

        outside = tmp_path / "elsewhere.md"
        assert ws.relative(outside) == str(outside.resolve())
        assert ws.relative("../../escape.md") == str(Path("../../escape.md").resolve())

    def test_is_inside_semantics(self, tmp_path: Path) -> None:
        """相对路径按"工作区相对"理解，``..`` 除外。"""
        ws = _ws(tmp_path)
        assert ws.is_inside("resource/x.md") is True
        assert ws.is_inside("../x.md") is False
        assert ws.is_inside(ws.root) is True
        assert ws.is_inside(ws.daily_path() / "a.md") is True
        assert ws.is_inside("/etc/hosts") is False

    def test_resolve_relative_blocks_escape(self, tmp_path: Path) -> None:
        """相对路径还原时越界必须抛错。

        Raises:
            `WorkspaceError`: ``../`` 逃出工作区根。
        """
        ws = _ws(tmp_path)
        assert ws.resolve_relative("daily/2026-09-22/card.md") == ws.daily_path() / "2026-09-22" / "card.md"
        with pytest.raises(WorkspaceError, match="越界"):
            ws.resolve_relative("../outside.md")

    def test_clean_keep_index_keeps_memory_and_deletes_junk(self, tmp_path: Path) -> None:
        """``keep="index"``：只删垃圾文件，记忆正文/索引/对话原文全留。"""
        ws = _ws(tmp_path)
        (ws.daily_path() / "2026-09-22").mkdir(parents=True)
        (ws.daily_path() / "2026-09-22" / "card.md").write_text("记忆正文", encoding="utf-8")
        (ws.resource_path() / "note.md").write_text("原始资料", encoding="utf-8")
        (ws.resource_path() / "scratch.tmp").write_text("junk", encoding="utf-8")
        (ws.resource_path() / "run.log").write_text("junk", encoding="utf-8")
        (ws.metadata_path() / "bm25.pkl").write_text("idx", encoding="utf-8")
        ws.dialog_path().mkdir(parents=True)
        (ws.dialog_path() / "s1.jsonl").write_text("{}\n", encoding="utf-8")

        report = ws.clean(keep="index")

        assert sorted(report.removed_files) == ["resource/run.log", "resource/scratch.tmp"]
        assert report.bytes_freed > 0
        assert "daily/2026-09-22/card.md" in report.kept
        assert "metadata/bm25.pkl" in report.kept
        assert "session/dialog/s1.jsonl" in report.kept
        assert (ws.daily_path() / "2026-09-22" / "card.md").exists()
        assert (ws.metadata_path() / "bm25.pkl").exists()
        assert not (ws.resource_path() / "scratch.tmp").exists()

    def test_clean_keep_none_needs_explicit_keep_session(self, tmp_path: Path) -> None:
        """``keep="none"`` 连对话原文一起删；要留住必须显式 ``keep_session=True``。"""
        ws = _ws(tmp_path)
        ws.dialog_path().mkdir(parents=True)
        (ws.dialog_path() / "s1.jsonl").write_text("{}\n", encoding="utf-8")
        (ws.daily_path() / "card.md").write_text("记忆正文", encoding="utf-8")

        wiped = ws.clean(keep="none")
        assert "session/dialog/s1.jsonl" in wiped.removed_files
        assert "daily/card.md" in wiped.removed_files
        assert not (ws.dialog_path() / "s1.jsonl").exists()
        # 空掉的 dialog/ 目录也会被顺手回收（先删文件、再删空目录）。
        assert "session/dialog" in wiped.removed_dirs

        ws.ensure()
        ws.dialog_path().mkdir(parents=True, exist_ok=True)
        (ws.dialog_path() / "s2.jsonl").write_text("{}\n", encoding="utf-8")
        kept = ws.clean(keep="none", keep_session=True)
        assert "session/dialog/s2.jsonl" in kept.kept
        assert (ws.dialog_path() / "s2.jsonl").exists()

    def test_clean_keep_all_is_dry_run(self, tmp_path: Path) -> None:
        """``keep="all"`` 什么都不删（整个根都是保护目录）。"""
        ws = _ws(tmp_path)
        (ws.resource_path() / "scratch.tmp").write_text("junk", encoding="utf-8")
        report = ws.clean(keep="all")
        assert report.removed_files == []
        assert report.removed_dirs == []
        assert (ws.resource_path() / "scratch.tmp").exists()

    def test_clean_max_age_days_spares_fresh_files(self, tmp_path: Path) -> None:
        """``max_age_days`` 只清理"够旧"的文件，新文件留着。

        判据是 ``path.stat().st_mtime >= time.time() - N*86400``（``cutoff``），
        所以 ``N=0`` 的语义是"删掉所有 mtime 早于此刻的文件" —— 因为刚写的文件
        mtime 也已经早于 ``time.time()``，``N=0`` 实际上**什么都删**。
        要真的把"跳过"这条分支测出来，必须造一个够旧的文件（``os.utime`` 回拨）。
        """
        import os
        import time

        ws = _ws(tmp_path)
        old = ws.resource_path() / "old.tmp"
        fresh = ws.resource_path() / "fresh.tmp"
        old.write_text("junk", encoding="utf-8")
        fresh.write_text("junk", encoding="utf-8")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))

        report = ws.clean(keep="none", max_age_days=5)
        assert report.removed_files == ["resource/old.tmp"]
        assert not old.exists()
        assert fresh.exists()

    def test_clean_requires_existing_root(self, tmp_path: Path) -> None:
        """工作区根不存在时 ``clean()`` 抛错，而不是悄悄返回空报告。"""
        ws = ReMeWorkspace(root=tmp_path / "nope")
        with pytest.raises(WorkspaceError, match="工作区根不存在"):
            ws.clean()

    def test_destroy_needs_confirm_and_refuses_system_paths(self, tmp_path: Path) -> None:
        """``destroy()`` 是危险的，必须显式 ``confirm=True``。

        Raises:
            `WorkspaceError`: 没给 ``confirm``，或路径浅到像系统目录。
        """
        ws = _ws(tmp_path)
        (ws.daily_path() / "card.md").write_text("x", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="confirm=True"):
            ws.destroy()
        assert ws.root.is_dir()

        # 系统目录护栏。**故意用一个不存在的浅路径**：万一将来护栏被改坏，
        # ``resolved.is_dir()`` 也是 False，rmtree 不会真的跑起来。
        # （不要拿 ``"/"`` 或 ``"/tmp"`` 测这一条 —— macOS 上 ``/tmp`` 会
        # resolve 成 ``/private/tmp``，parts 长度是 3，护栏不会触发。）
        with pytest.raises(WorkspaceError, match="系统目录"):
            ReMeWorkspace(root="/harness-kit-must-never-exist").destroy(confirm=True)

        ws.destroy(confirm=True)
        assert not ws.root.exists()

    def test_stats_and_fingerprint(self, tmp_path: Path) -> None:
        """``stats()`` 计卡/会话；``fingerprint()`` 是 ``sha256(root)[:16]``。"""
        import hashlib

        ws = _ws(tmp_path)
        (ws.daily_path() / "2026-09-22").mkdir(parents=True)
        (ws.daily_path() / "2026-09-22" / "a.md").write_text("a", encoding="utf-8")
        (ws.daily_path() / "2026-09-22" / "b.md").write_text("bb", encoding="utf-8")
        ws.dialog_path().mkdir(parents=True)
        (ws.dialog_path() / "s1.jsonl").write_text("{}\n", encoding="utf-8")
        (ws.metadata_path() / "bm25.pkl").write_text("idx", encoding="utf-8")

        stats = ws.stats()
        assert stats["cards"] == 2
        assert stats["sessions"] == 1
        # metadata/ 不进 iter_files，所以 files 只数 daily 的两张卡 + 会话原文。
        assert stats["files"] == 3
        assert stats["bytes"] == 1 + 2 + 3
        assert ws.fingerprint() == hashlib.sha256(str(ws.root).encode("utf-8")).hexdigest()[:16]
        assert str(ws) == str(ws.root)

    def test_iter_files_skips_metadata_and_hidden(self, tmp_path: Path) -> None:
        """``iter_files()`` 跳过 ``metadata/`` 与隐藏文件，避免把索引当资料再入库。"""
        ws = _ws(tmp_path)
        (ws.resource_path() / "keep.md").write_text("x", encoding="utf-8")
        (ws.resource_path() / ".hidden.md").write_text("x", encoding="utf-8")
        (ws.resource_path() / "note.txt").write_text("x", encoding="utf-8")
        (ws.metadata_path() / "bm25.pkl").write_text("x", encoding="utf-8")

        md_only = [p.name for p in ws.iter_files()]
        all_suffix = [p.name for p in ws.iter_files(suffix=None)]
        assert md_only == ["keep.md"]
        assert "bm25.pkl" not in all_suffix
        assert sorted(all_suffix) == ["keep.md", "note.txt"]


# ======================================================================
# 二、HarnessMemoryConfig：把 harness 意图翻译成 ReMe app config
# ======================================================================
class TestConfigBuild:
    """``build()`` 的每一条覆盖都是一次真实的踩坑记录，逐条测。"""

    def test_build_pins_workspace_dir_even_if_overridden(self, tmp_path: Path) -> None:
        """工作区永远最后一道：``extra_overrides`` 改不走它。

        防的是"以为在 A 写、其实写到 B"这类最难查的问题。
        """
        ws = _ws(tmp_path)
        builder = HarnessMemoryConfig(workspace=ws).with_overrides(workspace_dir="/tmp/evil-elsewhere")
        cfg = builder.build()
        assert cfg["workspace_dir"] == str(ws.root)
        assert cfg["daily_dir"] == "daily"
        assert cfg["session_dir"] == "session"

    def test_build_uses_cli_service_without_port(self, tmp_path: Path) -> None:
        """嵌入式装配必须是谁都不占的 ``cli`` 后端。"""
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path)).build()
        assert cfg["service"]["backend"] == "cli"
        assert cfg["service"]["host"] is None
        assert cfg["service"]["port"] is None
        assert cfg["service"]["web_enabled"] is False
        assert cfg["service"]["mcp_enabled"] is False

    def test_build_drops_resident_jobs(self, tmp_path: Path) -> None:
        """``background`` / ``cron`` job 必须全部消失。

        丢掉它们不是为了 ``start()``（实测含常驻 job 时 start 照常返回），
        而是因为 ``run_job`` 的语义是"跑一次并等结果"，而这类 job 的
        ``watch_changes_step`` 永不返回；而且它们的 close 要等 ``close_timeout``。
        """
        ws = _ws(tmp_path)
        raw = HarnessMemoryConfig(workspace=ws)._resolve_base()
        resident = sorted(
            name for name, spec in raw["jobs"].items() if spec.get("backend") not in EMBEDDED_JOB_BACKENDS
        )
        assert resident, "default.yaml 里应当本来就有常驻 job，否则这条测试失去意义"

        cfg = HarnessMemoryConfig(workspace=ws).build()
        assert resident[0] in raw["jobs"]
        assert not (set(cfg["jobs"]) & set(resident))
        assert all(spec["backend"] in EMBEDDED_JOB_BACKENDS for spec in cfg["jobs"].values())

    def test_build_drops_resident_jobs_after_whitelist(self, tmp_path: Path) -> None:
        """**回归测试**：白名单 + 过滤不能把常驻 job 又"深合并"回来。

        初版实现用 ``_deep_merge`` 覆盖 ``jobs``，于是刚删掉的 ``dream_cron``
        被底稿带了回来；``MemoryDoctor`` 的 ``config.jobs`` 检查项抓到了它。
        现在 ``_apply_jobs`` 是**整体替换** ``result["jobs"] = jobs``。
        """
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path)).with_jobs("search", "write").build()
        assert set(cfg["jobs"]) == {"search", "write"}
        assert "dream_cron" not in cfg["jobs"]
        assert "index_update_loop" not in cfg["jobs"]

    def test_build_reindex_is_the_rescan_version(self, tmp_path: Path) -> None:
        """``reindex`` 必须被换成"重新扫描"版，带 ``watch_dirs`` / ``watch_suffixes``。

        官方 ``ReindexStep`` 只重建**已入库 chunk** 的索引（
        ``reme/steps/index/reindex.py:11`` 的注释写得很明白）；漏掉
        ``watch_dirs`` 会让 ``clear_store_step`` 清空索引后装不回来，
        而且返回 ``success=True``、全程无报错。
        """
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path)).build()
        job = cfg["jobs"]["reindex"]
        assert job["backend"] == "base"
        assert job["watch_dirs"] == ["daily_dir", "digest_dir", "resource_dir"]
        assert job["watch_suffixes"] == ["md"]
        assert [step["backend"] for step in job["steps"]] == ["clear_store_step", "init_changes_step"]
        assert job["steps"][1]["dispatch_steps"] == ["update_index_step"]
        assert job == RESCAN_REINDEX_JOB

    def test_build_returns_independent_deep_copies(self, tmp_path: Path) -> None:
        """同一个 builder 造多份互不干扰的配置（多租户/并发测试靠它）。"""
        builder = HarnessMemoryConfig(workspace=_ws(tmp_path))
        first = builder.build()
        second = builder.build()
        assert first is not second
        assert first["jobs"] is not second["jobs"]

        first["jobs"]["reindex"]["watch_dirs"].append("resource_dir")
        first["components"]["as_llm"] = {"default": {"model": "tampered"}}
        third = builder.build()
        assert third["jobs"]["reindex"]["watch_dirs"] == ["daily_dir", "digest_dir", "resource_dir"]
        assert RESCAN_REINDEX_JOB["watch_dirs"] == ["daily_dir", "digest_dir", "resource_dir"]
        assert "as_llm" not in third["components"] or third["components"]["as_llm"].get("default", {}).get(
            "model",
        ) != "tampered"

    def test_with_jobs_rejects_unknown_name(self, tmp_path: Path) -> None:
        """白名单里拼错的 job 名必须报错并列出可用项，而不是静默吞掉。

        Raises:
            `MemoryConfigError`: 名字在 ReMe 配置里不存在。
        """
        builder = HarnessMemoryConfig(workspace=_ws(tmp_path)).with_jobs("search", "serach")
        with pytest.raises(MemoryConfigError, match="不存在") as excinfo:
            builder.build()
        assert "serach" in str(excinfo.value)
        assert "search" in str(excinfo.value)

    def test_with_jobs_rejects_name_dropped_as_resident(self, tmp_path: Path) -> None:
        """被"常驻过滤"丢掉的 job 名进白名单也要报错，并提示这个原因。"""
        builder = HarnessMemoryConfig(workspace=_ws(tmp_path)).with_jobs("dream_cron")
        with pytest.raises(MemoryConfigError, match="background/cron"):
            builder.build()

    def test_keep_background_jobs_true_only_skips_filtering(self, tmp_path: Path) -> None:
        """``keep_background_jobs=True`` 时保留常驻 job，也不覆盖 reindex。

        这条是给"我就是要起 http 服务 / 真后台"的高级用法留的口子：
        关闭过滤后，``reindex`` 保持官方原文（``reindex_step``，不重扫，
        没有 ``watch_dirs``）—— 也就是说这时的重扫语义要调用方自己负责。
        """
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path), keep_background_jobs=True).build()
        assert cfg["jobs"]["dream_cron"]["backend"] == "cron"
        assert cfg["jobs"]["index_update_loop"]["backend"] == "background"
        assert cfg["jobs"]["reindex"]["steps"] == [{"backend": "reindex_step"}]
        assert "watch_dirs" not in cfg["jobs"]["reindex"]

    def test_build_whitelist_order_follows_whitelist(self, tmp_path: Path) -> None:
        """白名单同时决定**保留谁**与**顺序**（启动顺序可预测）。"""
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path)).with_jobs("write", "search").build()
        assert list(cfg["jobs"]) == ["write", "search"]

    def test_with_components_deep_merges(self, tmp_path: Path) -> None:
        """``with_components`` 是深合并，不是整体替换。"""
        builder = HarnessMemoryConfig(workspace=_ws(tmp_path)).with_components(
            as_llm={"default": {"model": "deepseek-flash"}},
            keyword_index={"default": {"k1": 1.5}},
        )
        cfg = builder.build()
        as_llm = cfg["components"]["as_llm"]["default"]
        assert as_llm["model"] == "deepseek-flash"
        # 深合并：原来 default.yaml 里的字段还在。
        assert "backend" in as_llm
        assert cfg["components"]["keyword_index"]["default"]["k1"] == 1.5

    def test_with_embedding_dimensions_validates(self, tmp_path: Path) -> None:
        """维度必须是正整数或 ``None``。

        Raises:
            `MemoryConfigError`: 传入 0 / 负数。
        """
        builder = HarnessMemoryConfig(workspace=_ws(tmp_path))
        with pytest.raises(MemoryConfigError, match="正整数"):
            builder.with_embedding_dimensions(0)
        with pytest.raises(MemoryConfigError, match="正整数"):
            builder.with_embedding_dimensions(-8)

    def test_embedding_disabled_by_default(self, tmp_path: Path) -> None:
        """``embedding_dimensions=None`` 是**合法**状态：退化关键词检索。"""
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path)).build()
        assert "embedding_store" not in cfg["components"]

    def test_embedding_enabled_wires_three_components(self, tmp_path: Path) -> None:
        """给了维度就要把 ``as_embedding`` / ``embedding_store`` / ``file_store`` 三处串起来。"""
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path), embedding_dimensions=16).build()
        assert cfg["components"]["as_embedding"]["default"]["dimensions"] == 16
        assert cfg["components"]["embedding_store"]["default"]["as_embedding"] == "default"
        assert cfg["components"]["file_store"]["default"]["embedding_store"] == "default"

    def test_describe_lists_jobs_and_components(self, tmp_path: Path) -> None:
        """``describe()`` 是给日志/教程看的，不该碰网络也不该解释凭据。"""
        text = HarnessMemoryConfig(workspace=_ws(tmp_path)).with_jobs("search", "write").describe()
        assert "service       = cli" in text
        assert "embedding     = disabled" in text
        assert "search" in text and "write" in text
        assert "components:" in text
        assert "api_key" not in text

    def test_build_carries_llm_credentials_from_env(self, tmp_path: Path, llm_env: dict[str, str]) -> None:
        """``.env`` 的 ``OPENAI_*`` 必须被显式写进 ``as_llm.credential``。

        ``default.yaml`` 只认 ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL_NAME``，
        不覆盖就会在 ``Application.start()`` 里炸 ``Missing credentials``。
        """
        from harness_kit.settings import Settings

        assert llm_env["api_key"]
        cfg = HarnessMemoryConfig(workspace=_ws(tmp_path), settings=Settings.from_env()).build()
        as_llm = cfg["components"]["as_llm"]["default"]
        assert as_llm["credential"]["api_key"] == llm_env["api_key"]
        assert as_llm["credential"]["base_url"] == llm_env["base_url"]
        assert as_llm["model"] == llm_env["model"]

    def test_from_spec_reads_memory_spec(self, tmp_path: Path, settings: Any) -> None:
        """``from_spec`` 认 ``MemorySpec`` 的形状，并把相对路径解析到仓库根。

        Raises:
            `MemoryConfigError`: 传进去的不是 MemorySpec 形状。
        """
        spec = MemorySpec(
            enabled=True,
            workspace_root="./.harness/reme/from-spec",
            jobs=["search", "auto_memory"],
            top_k=3,
        )
        builder = HarnessMemoryConfig.from_spec(spec, settings=settings)
        assert builder.job_whitelist == ("search", "auto_memory")
        assert Path(builder.workspace.root).is_absolute()
        assert str(builder.workspace.root).endswith(".harness/reme/from-spec")
        assert set(builder.build()["jobs"]) == {"search", "auto_memory"}

        with pytest.raises(MemoryConfigError, match="需要 MemorySpec"):
            HarnessMemoryConfig.from_spec({"workspace_root": "x"})


# ======================================================================
# 三、MemoryDoctor：不启动 app 的体检
# ======================================================================
class TestDoctor:
    """体检的价值在"改完配置先别跑、先 doctor 一下"，所以它必须能在没 start 时跑。"""

    def test_all_expected_checks_are_present(self, tmp_path: Path) -> None:
        """12 个检查项一个都不能少（契约 §3.15 的名字）。"""
        doctor = MemoryDoctor(HarnessMemoryConfig(workspace=_ws(tmp_path)).build())
        names = [result.name for result in doctor.check()]
        assert names == list(EXPECTED_CHECK_NAMES)
        assert all(isinstance(result, CheckResult) for result in doctor.check())

    def test_only_the_documented_gap_fails(self, tmp_path: Path) -> None:
        """除已知缺口外，本环境应当全绿。

        ``agentscope.reme_middleware`` 是**真实存在**的兼容缺口：
        AgentScope 2.0.8 的 ``_longterm_memory/_reme/_config.py`` 里
        ``_dream_steps()`` 引用了 ``dream_topics_step``，而 reme 0.4.1.13
        没注册它（详见 ``doctor.py:504-529`` 的 ``_dream_topics_missing``）。
        第 19 讲用 ``ensure_reme_compat()`` 修。这里**只**放行这一项，
        其余任何失败都说明环境真有问题。
        """
        doctor = MemoryDoctor(HarnessMemoryConfig(workspace=_ws(tmp_path)).build())
        unexpected = [
            result for result in doctor.check() if not result.ok and result.name != "agentscope.reme_middleware"
        ]
        assert unexpected == [], doctor.report()

    def test_known_gap_is_named_not_silently_ignored(self, tmp_path: Path) -> None:
        """已知缺口要么被点名报告，要么说明环境已经修好了（那就 skip）。"""
        doctor = MemoryDoctor(HarnessMemoryConfig(workspace=_ws(tmp_path)).build())
        gap = next(r for r in doctor.check() if r.name == "agentscope.reme_middleware")
        if gap.ok:
            pytest.skip("本环境 reme 已注册 dream_topics_step（第 19 讲补丁可能已生效）")
        assert "dream_topics_step" in gap.detail
        assert "ensure_reme_compat" in gap.hint

    def test_doctor_ok_failures_report_and_log(self, tmp_path: Path) -> None:
        """``ok()`` / ``failures()`` / ``report()`` / ``log()`` 四件套语义一致。"""
        ws = _ws(tmp_path)
        config = HarnessMemoryConfig(workspace=ws).build()
        # 故意注入一个 http service，让 config.service 失败。
        config["service"]["backend"] = "http"
        doctor = MemoryDoctor(config)

        failures = doctor.failures()
        names = {result.name for result in failures}
        assert "config.service" in names
        # 只放行已知缺口，别的失败都要露出来。
        assert names - {"config.service", "agentscope.reme_middleware"} == set()
        assert doctor.ok() is False
        assert "[FAIL] config.service" in doctor.report()
        assert "合计 12 项" in doctor.report()

        doctor.log()  # 只要求不炸：有失败项时走 WARNING 分支

    def test_config_property_is_a_defensive_copy(self, tmp_path: Path) -> None:
        """``doctor.config`` 是深拷贝，体检不能被调用方改配置。"""
        config = HarnessMemoryConfig(workspace=_ws(tmp_path)).build()
        doctor = MemoryDoctor(config)
        leaked = doctor.config
        leaked["service"]["backend"] = "http"
        assert doctor.config["service"]["backend"] == "cli"

    def test_flags_resident_jobs(self, tmp_path: Path) -> None:
        """混进 background/cron job 必须被点名（它们的 step 永不返回）。"""
        config = HarnessMemoryConfig(workspace=_ws(tmp_path)).build()
        config["jobs"]["bogus_loop"] = {"backend": "background", "steps": []}
        result = next(r for r in MemoryDoctor(config).check() if r.name == "config.jobs")
        assert result.ok is False
        assert "bogus_loop" in result.detail
        assert "吊死" in result.hint

    def test_flags_missing_workspace(self, tmp_path: Path) -> None:
        """没 ``ensure()`` 的工作区：``workspace.dirs`` 与 ``workspace.writable`` 双红。"""
        ws = ReMeWorkspace(root=tmp_path / "not-created")
        config = HarnessMemoryConfig(workspace=ws).build()
        by_name = {r.name: r for r in MemoryDoctor(config).check()}
        assert by_name["workspace.dirs"].ok is False
        assert by_name["workspace.writable"].ok is False
        assert "ensure()" in by_name["workspace.dirs"].hint

    def test_workspace_checks_turn_green_after_ensure(self, tmp_path: Path) -> None:
        """``ensure()`` 之后这两项必须变绿（含写探针）。"""
        ws = _ws(tmp_path)
        by_name = {r.name: r for r in MemoryDoctor(HarnessMemoryConfig(workspace=ws).build()).check()}
        assert by_name["workspace.dirs"].ok is True
        assert by_name["workspace.writable"].ok is True
        # 探针文件必须被清掉，不能留在工作区里。
        assert list(ws.root.glob(".harness_doctor_probe")) == []

    def test_registry_checks_see_harness_overrides(self, tmp_path: Path) -> None:
        """harness 自己塞进去的 step（``clear_store_step``）必须也在注册表里。"""
        config = HarnessMemoryConfig(workspace=_ws(tmp_path)).build()
        by_name = {r.name: r for r in MemoryDoctor(config).check()}
        assert by_name["registry.steps"].ok is True, by_name["registry.steps"].detail
        assert by_name["registry.jobs"].ok is True
        assert by_name["registry.components"].ok is True

    def test_check_result_forbids_extra_fields(self) -> None:
        """``CheckResult`` 是 ``extra="forbid"``，拼错的字段名会立刻暴露。

        Raises:
            `ValidationError`: 多给了一个字段。
        """
        with pytest.raises(ValidationError):
            CheckResult(name="x", ok=True, extra_field=1)  # type: ignore[call-arg]
        assert CheckResult(name="x", ok=True).detail == ""
        assert CheckResult(name="x", ok=True).hint == ""


# ======================================================================
# 四、MemoryClient：错误归一（用替身，不启动 ReMe）
# ======================================================================
class _FakeApp:
    """``MemoryClient`` 需要的最小 ReMe app 替身。

    它只实现 ``MemoryClient`` 真的会碰的三样东西：
    ``is_started`` / ``context.jobs`` / ``context.components`` / ``run_job``。
    这样"错误归一"这条逻辑可以在**不启动 ReMe** 的前提下被测到 ——
    真实的 ``Response.success=False`` 很难在离线环境里可靠复现。
    """

    def __init__(self, *, success: bool = True) -> None:
        """构造替身。

        Args:
            success (`bool`): ``run_job`` 返回的 ``success`` 值。
        """
        self.is_started = True
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._success = success
        self.context = SimpleNamespace(
            jobs={"search": {"backend": "base"}, "version": {"backend": "base"}},
            components={"file_store": {"default": object()}},
        )

    async def run_job(self, name: str, **kwargs: Any) -> Any:
        """记录调用并返回一个 ReMe ``Response`` 形状的对象。

        Args:
            name (`str`): job 名。
            **kwargs (`Any`): job 参数。

        Returns:
            `Any`: ``answer`` / ``success`` / ``metadata`` 三属性对象。
        """
        self.calls.append((name, kwargs))
        return SimpleNamespace(
            answer=f"answer-of-{name}",
            success=self._success,
            metadata={"why": "demo"},
        )

    async def close(self) -> None:
        """记录关闭。"""
        self.closed = True
        self.is_started = False


class TestClientErrors:
    """``MemoryClient`` 只做四件事，其中三件是错误归一 —— 所以大部分测试是失败路径。"""

    @staticmethod
    def _client(tmp_path: Path, *, success: bool = True) -> tuple[MemoryClient, _FakeApp]:
        """造一个"已启动"的假客户端。

        Args:
            tmp_path (`Path`): 临时目录（当 ``workspace_dir``）。
            success (`bool`): 替身 ``run_job`` 的成败。

        Returns:
            `tuple[MemoryClient, _FakeApp]`: (客户端, 替身)。
        """
        client = MemoryClient({"workspace_dir": str(tmp_path), "jobs": {"search": {}, "version": {}}})
        app = _FakeApp(success=success)
        client._app = app  # 契约的"只读逃生口"在这里被反向用来注入替身
        return client, app

    async def test_run_job_before_start_raises(self, tmp_path: Path) -> None:
        """**假成功拦截**：没 ``start()`` 就 ``run_job`` 必须抛错。

        跳过 ``start()`` 时 ``BaseJob`` 的 ``step_specs`` 是空的，于是
        ``Application.run_job`` 会返回 ``success=True`` / ``answer=''`` ——
        一个看起来成功的空结果，比报错危险得多。

        Raises:
            `MemoryUnavailableError`: 永远如此。
        """
        client = MemoryClient({"workspace_dir": str(tmp_path), "jobs": {"version": {}}})
        assert client.started is False
        with pytest.raises(MemoryUnavailableError, match="假成功"):
            await client.run_job("version")
        with pytest.raises(MemoryUnavailableError, match="尚未 start"):
            _ = client.application

    async def test_job_names_falls_back_to_config(self, tmp_path: Path) -> None:
        """没启动时 ``job_names()`` 从配置里读（launch 前也能做白名单校验）。"""
        client = MemoryClient({"workspace_dir": str(tmp_path), "jobs": {"write": {}, "search": {}}})
        assert client.job_names() == ["search", "write"]
        assert client.workspace_dir == str(tmp_path)
        assert client.config["jobs"] == {"write": {}, "search": {}}

    async def test_run_job_unknown_name_lists_available(self, tmp_path: Path) -> None:
        """未知 job 名要在**本地**就被挡住，并列出可用项。

        Raises:
            `MemoryUnavailableError`: job 名不在 ``context.jobs`` 里。
        """
        client, app = self._client(tmp_path)
        with pytest.raises(MemoryUnavailableError, match="没有 job 'serach'"):
            await client.run_job("serach")
        assert app.calls == []

    async def test_run_job_passes_kwargs_positionally_only_name(self, tmp_path: Path) -> None:
        """``name`` 是 positional-only：``run_job(name="search")`` 必须报 ``TypeError``。

        ReMe 自己的签名就是 ``async def run_job(self, name: str, /, **kwargs)``
        （``third_party/ReMe/reme/application.py:281``），harness 侧逐字对齐 ——
        否则 ``name`` 会被当成 job 参数透传下去，出现"search 里多了一个 name=..."的怪事。
        """
        client, app = self._client(tmp_path)
        response = await client.run_job("search", query="部署令牌", limit=5)
        assert response.success is True
        assert response.answer == "answer-of-search"
        assert app.calls == [("search", {"query": "部署令牌", "limit": 5})]

        with pytest.raises(TypeError):
            await client.run_job(name="search")  # type: ignore[call-arg]

    async def test_run_job_converts_success_false_to_memoryjoberror(self, tmp_path: Path) -> None:
        """``success=False`` 必须变成异常，且带上 job 名/答案/元数据。

        Raises:
            `MemoryJobError`: 携带全部诊断信息。
        """
        client, _ = self._client(tmp_path, success=False)
        with pytest.raises(MemoryJobError) as excinfo:
            await client.run_job("search")
        error = excinfo.value
        assert error.job == "search"
        assert error.answer == "answer-of-search"
        assert error.metadata == {"why": "demo"}
        # 消息模板 ``f"ReMe job {job!r} failed: {answer}"``：带上真实诊断，
        # 而不是一句 "job failed" 让人无从下手。
        assert str(error) == "ReMe job 'search' failed: answer-of-search"

    async def test_run_job_raw_swallows_failure(self, tmp_path: Path) -> None:
        """``run_job_raw`` 是"允许失败"版本：返回 ``success=False`` 的替身而非抛错。"""
        client, _ = self._client(tmp_path, success=False)
        response = await client.run_job_raw("search")
        assert response.success is False
        assert "answer-of-search" in str(response.answer)

        raw_unknown = await client.run_job_raw("nope")
        assert raw_unknown.success is False
        assert "没有 job 'nope'" in str(raw_unknown.answer)

    async def test_component_lookup_and_keyerror(self, tmp_path: Path) -> None:
        """``component()`` 取得到就给对象，取不到就给"可用清单"。

        Raises:
            `KeyError`: 组件类型或实例名不存在。
        """
        client, _ = self._client(tmp_path)
        assert client.component("file_store") is not None
        with pytest.raises(KeyError, match="file_store:digest"):
            client.component("file_store", "digest")
        with pytest.raises(KeyError, match="keyword_index"):
            client.component("keyword_index")

    async def test_aclose_is_idempotent_and_flips_started(self, tmp_path: Path) -> None:
        """``aclose()`` 幂等；关闭后 ``started`` 为假，再 ``run_job`` 又被拦下。"""
        client, app = self._client(tmp_path)
        assert client.started is True
        await client.aclose()
        assert app.closed is True
        assert client.started is False
        await client.aclose()  # 幂等，不炸

        with pytest.raises(MemoryUnavailableError):
            await client.run_job("search")

    async def test_async_context_manager_closes(self, tmp_path: Path) -> None:
        """``async with`` 退出时必须关闭（``__aenter__`` 会真的 start，故这里只测替身路径）。"""
        client, app = self._client(tmp_path)
        async with client as entered:
            assert entered is client
            assert client.started is True
        assert app.closed is True
        assert client.started is False

    async def test_run_job_timeout_is_translated(self, tmp_path: Path) -> None:
        """超时要变成带 job 名的 ``TimeoutError``，而不是裸的 asyncio 异常。"""

        class _SlowApp(_FakeApp):
            async def run_job(self, name: str, **kwargs: Any) -> Any:
                await asyncio.sleep(5)
                return await super().run_job(name, **kwargs)

        client = MemoryClient(
            {"workspace_dir": str(tmp_path), "jobs": {"search": {}}},
            job_timeout_s=0.05,
        )
        client._app = _SlowApp()
        with pytest.raises(TimeoutError, match="超过 0.05s"):
            await client.run_job("search")

    async def test_exception_classes_carry_hints(self) -> None:
        """两个异常类都必须是 ``RuntimeError`` 子类，且提示语是本仓库专属的。"""
        from harness_kit.memory import REME_PYTHONPATH_HINT

        assert issubclass(MemoryUnavailableError, RuntimeError)
        assert issubclass(MemoryJobError, RuntimeError)
        assert issubclass(MemoryConfigError, RuntimeError)
        assert "third_party/ReMe" in REME_PYTHONPATH_HINT
        assert "0.3.1.10" in REME_PYTHONPATH_HINT


# ======================================================================
# 五、集成：真的把 ReMe 启起来（唯一的"真"测试）
# ======================================================================
class TestEmbeddedIntegration:
    """两条集成测试。

    它们要证明的是"harness 的配置真的能被 ReMe 吃下去"，这恰恰是替身证明不了的。
    其余全部逻辑留在替身测试里，所以即使 ReMe 不可用，本文件也只有这两条 skip。
    """

    @staticmethod
    async def _started(tmp_path: Path) -> MemoryClient:
        """构建并启动一个真的嵌入式 client。

        Args:
            tmp_path (`Path`): 临时目录。

        Returns:
            `MemoryClient`: 已启动的客户端。

        Raises:
            `pytest.skip`: reme 不可导入时跳过（离线 CI 的正常路径）。
        """
        ws = _ws(tmp_path)
        builder = HarnessMemoryConfig(workspace=ws).with_jobs(
            "health_check",
            "read",
            "reindex",
            "search",
            "version",
            "write",
        )
        client = MemoryClient(builder.build())
        try:
            await client.start()
        except MemoryUnavailableError as exc:
            pytest.skip(f"reme 不可用，跳过集成测试: {exc}")
        return client

    async def test_start_registers_jobs_and_version_job_answers(self, tmp_path: Path) -> None:
        """启动后：job 白名单生效、``version`` 报出真实版本号。

        Raises:
            `MemoryUnavailableError`: reme 不可用（转成 skip）。
        """
        client = await self._started(tmp_path)
        try:
            assert client.started is True
            assert client.job_names() == ["health_check", "read", "reindex", "search", "version", "write"]
            assert all(
                spec["backend"] in EMBEDDED_JOB_BACKENDS for spec in client.config["jobs"].values()
            )

            response = await client.run_job("version")
            assert response.success is True
            assert str(response.answer) == reme_version()

            # 组件真的被装配起来了（file_store 是记忆的落盘处）。
            store = client.component("file_store")
            assert store is not None
        finally:
            await client.aclose()
        assert client.started is False

    async def test_write_reindex_search_roundtrip(self, tmp_path: Path) -> None:
        """**本讲的核心断言**：写文件 → 重扫式 reindex → search 能命中。

        它同时证明了 ``RESCAN_REINDEX_JOB`` 的覆盖是必须的：
        官方 ``reindex`` 只重建已入库 chunk，写完文件直接调它，
        返回的 ``metadata['counts']`` 全是 0，``search`` 命中 0 条。
        """
        client = await self._started(tmp_path)
        try:
            ws = ReMeWorkspace(root=client.workspace_dir)

            write = await client.run_job(
                "write",
                path="daily/2026-09-22/lesson15.md",
                content="# ReMe 架构总览\n\nApplication 按 Kahn 拓扑序启动组件。",
            )
            assert write.success is True
            card = ws.daily_path() / "2026-09-22" / "lesson15.md"
            assert card.is_file()
            assert "Kahn 拓扑序" in card.read_text(encoding="utf-8")

            reindex = await client.run_job("reindex")
            assert reindex.success is True
            counts = dict(reindex.metadata)["counts"]
            assert counts["added"] >= 1, reindex.metadata

            found = await client.run_job("search", query="Kahn 拓扑序", limit=3)
            assert found.success is True
            assert "lesson15.md" in str(found.answer)

            searched_missing = await client.run_job("search", query="完全不存在的关键词 zzzz", limit=3)
            assert searched_missing.success is True
        finally:
            await client.aclose()


# ======================================================================
# 六、Profile 纳管 + 惰性导出
# ======================================================================
class TestProfileWiring:
    """把 ReMe 接进 harness_kit 的 Profile 体系 —— 这是"不许另起内核"的落地点。"""

    def test_coding_profile_memory_spec_flows_to_reme_config(self, tmp_path: Path, settings: Any) -> None:
        """``coding.yaml`` 的 ``memory:`` 段一路走到 ReMe 的 ``jobs`` 里。"""
        profile = load_profile("coding", search_dir=PROFILE_DIR)
        spec = profile.memory
        assert spec.enabled is True
        assert spec.jobs == ["search", "auto_memory"]
        assert spec.top_k == 5
        assert spec.min_score == pytest.approx(0.2)
        assert spec.workspace_root == "./.harness/reme/coding"

        builder = HarnessMemoryConfig.from_spec(spec, settings=settings)
        config = builder.build()
        assert set(config["jobs"]) == {"search", "auto_memory"}
        # ``auto_memory`` 是 base 后端、不是常驻 job，所以它应当活下来。
        assert config["jobs"]["auto_memory"]["backend"] == "base"
        # **白名单就是白名单**：它把 harness 顺手加进去的 ``reindex`` 也一起挡掉了。
        # 这是刻意的（"只保留这些 job" 要说到做到），但也是一个必须知道的后果：
        # Profile 想用重扫式 reindex，就必须在 ``memory.jobs`` 里显式写上 "reindex"。
        assert "reindex" not in config["jobs"]
        assert "reindex" in HarnessMemoryConfig.from_spec(spec, settings=settings).with_jobs(
            "search",
            "auto_memory",
            "reindex",
        ).build()["jobs"]

    def test_memory_spec_defaults_are_off(self) -> None:
        """``MemorySpec`` 默认 ``enabled=False`` / 空 job 列表（记忆是可选层）。"""
        spec = MemorySpec()
        assert spec.enabled is False
        assert spec.jobs == []
        assert spec.embedding_dimensions is None

    def test_lazy_exports_resolve_and_are_listed(self) -> None:
        """``harness_kit.memory`` 是 PEP 562 惰性导出：认得的名字能取到、进 ``dir()``。

        Raises:
            `AttributeError`: 完全没听说过的名字。
        """
        import harness_kit.memory as memory

        assert "ReMeWorkspace" in dir(memory)
        assert "MemoryDoctor" in dir(memory)
        assert memory.ReMeWorkspace is ReMeWorkspace
        assert memory.MemoryClient is MemoryClient
        with pytest.raises(AttributeError, match="has no attribute"):
            _ = memory.NoSuchThingAtAll

    def test_contract_names_are_exported(self) -> None:
        """契约 §3.15 点名的四个类 + 常量必须都在 ``__all__`` 里。"""
        import harness_kit.memory as memory

        for name in (
            "ReMeWorkspace",
            "WorkspaceError",
            "HarnessMemoryConfig",
            "MemoryConfigError",
            "MemoryClient",
            "MemoryJobError",
            "MemoryUnavailableError",
            "MemoryDoctor",
            "CheckResult",
            "RESCAN_REINDEX_JOB",
            "EMBEDDED_JOB_BACKENDS",
        ):
            assert name in memory.__all__, name
            assert getattr(memory, name) is not None
