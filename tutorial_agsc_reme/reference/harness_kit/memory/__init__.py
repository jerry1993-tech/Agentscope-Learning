# -*- coding: utf-8 -*-
"""记忆层：在 AgentScope + ReMe 之上补齐工程化能力（契约 §3.17 ~ §3.19）。

**包的分层（读代码前先看这张表）**

========================== ==================================================
模块                        在做什么
========================== ==================================================
:mod:`~harness_kit.memory.workspace`   工作区路径模型（所有相对路径的唯一解释者）
:mod:`~harness_kit.memory.client`      嵌入式 ReMe 生命周期 + ``run_job`` 门面
:mod:`~harness_kit.memory.config`      由 harness 配置生成 ReMe app config
:mod:`~harness_kit.memory.jobs`        前台 job 的白名单与超时
:mod:`~harness_kit.memory.search`      search / traverse 的结构化封装
:mod:`~harness_kit.memory.hybrid`      关键词 + 向量融合结果的再排序
:mod:`~harness_kit.memory.citations`   chunk → 可核对的引用
:mod:`~harness_kit.memory.budget`      token 预算裁剪（唯一的注入渲染入口）
:mod:`~harness_kit.memory.gating`      召回门控（敏感会话 / 分数 / 预算）+ 写入门控
:mod:`~harness_kit.memory.metrics`     命中率与注入量的观测
:mod:`~harness_kit.memory.frontmatter` front matter 的解析与标签规范化
:mod:`~harness_kit.memory.catalog`     file catalog 台账
:mod:`~harness_kit.memory.ingest`      外部资料入工作区
:mod:`~harness_kit.memory.distill`     会话 → 记忆卡
:mod:`~harness_kit.memory.maintenance` auto_memory / auto_dream / auto_resource
:mod:`~harness_kit.memory.forget`      过期与归档
:mod:`~harness_kit.memory.proactive`   主动读取
:mod:`~harness_kit.memory.tenant`     多租户隔离
:mod:`~harness_kit.memory.doctor`      自检
:mod:`~harness_kit.memory.middleware`  AgentScope 中间件（含 ReMe 兼容补丁）
========================== ==================================================

**为什么导出是惰性的（PEP 562）**

``harness_kit.memory`` 整体遵循"可选依赖 + 优雅降级"：只装 AgentScope 不装 ReMe 时，
:mod:`~harness_kit.memory.workspace` / :mod:`~harness_kit.memory.budget` /
:mod:`~harness_kit.memory.citations` 这些**纯计算**模块仍然应该可用。
如果在 ``__init__`` 里一次性 ``import`` 全部子模块，那么
``import harness_kit.memory`` 这一个动作就会把 ``reme`` 和 ``agentscope`` 一起拉进来 ——
一个可选依赖把整个包变成不可导入，是最常见的"可选依赖"翻车方式。

:func:`__getattr__` 的兜底错误信息因此很关键：它必须告诉使用者
**是哪个依赖缺了、怎么装**。缺 ``reme`` 时按 :data:`~harness_kit.memory.client.REME_PYTHONPATH_HINT`
处理（``import reme`` 会静默拿到 site-packages 里的旧版 0.3.1.10，这条提示是本仓库的
头号踩坑点）。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    # --- workspace / 路径 -------------------------------------------------
    "DEFAULT_SUBDIRS",
    "ReMeWorkspace",
    "WorkspaceCleanReport",
    "WorkspaceError",
    # --- client / 生命周期 ------------------------------------------------
    "MemoryClient",
    "MemoryJobError",
    "MemoryUnavailableError",
    "REME_MIN_VERSION",
    "REME_PYTHONPATH_HINT",
    "REME_REQUIRED_VERSION",
    "build_memory_client",
    "reme_available",
    "reme_version",
    # --- config -----------------------------------------------------------
    "EMBEDDED_JOB_BACKENDS",
    "HarnessMemoryConfig",
    "MemoryConfigError",
    "RESCAN_REINDEX_JOB",
    # --- jobs -------------------------------------------------------------
    "DEFAULT_JOB_TIMEOUT_S",
    "JobTimeout",
    "MemoryJobs",
    "RESIDENT_BACKENDS",
    # --- search / hybrid / citations --------------------------------------
    "DEFAULT_LIMIT",
    "FusedEntry",
    "FusionMode",
    "HybridRetriever",
    "MemoryHit",
    "MemorySearch",
    "RRF_K",
    "SEARCH_JOB",
    "TRAVERSE_JOB",
    "Citation",
    "CitationBuilder",
    "SearchResult",
    "merge_intervals",
    "to_memory_hit",
    "to_memory_hits",
    # --- budget / gating / metrics ----------------------------------------
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MIN_SCORE",
    "GateDecision",
    "MemoryBudget",
    "MemoryBudgetResult",
    "MemoryGate",
    "MemoryMetrics",
    "MemoryWriteGate",
    "WriteDecision",
    "SessionMetrics",
    "TokenEstimator",
    "estimate_tokens_heuristic",
    "render_memory_block",
    # --- frontmatter / catalog --------------------------------------------
    "DEFAULT_MAX_TAG_LENGTH",
    "DEFAULT_MAX_TAGS_PER_FILE",
    "DEFAULT_TAG_KEY",
    "CatalogManager",
    "ChangeSet",
    "DEFAULT_CATALOG",
    "DEFAULT_SCAN_SUFFIXES",
    "FrontMatter",
    "FrontMatterError",
    "KEYS_PER_CATALOG",
    "ReconcileReport",
    "normalize_query_tags",
    "normalize_tags",
    "split_front_matter",
    # --- ingest / distill / maintenance -----------------------------------
    "AUTO_MEMORY_JOB",
    "AUTO_DREAM_JOB",
    "AUTO_RESOURCE_JOB",
    "FRONTMATTER_UPDATE_JOB",
    "DistillResult",
    "INGEST_STATE_NAME",
    "IngestResult",
    "MaintenanceResult",
    "MemoryIngestor",
    "MemoryMaintainer",
    "MemoryMaintenanceScheduler",
    "NightlyReport",
    "SESSION_END_EVENT_NAME",
    "SessionDistiller",
    # --- forget / proactive / tenant --------------------------------------
    "DEMOTE_KEY",
    "DEMOTE_VALUE",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_SCAN_GLOBS",
    "DIALOG_DIR",
    "ForgetPlan",
    "ForgetPolicy",
    "HitCounter",
    "MAX_TENANT_ID_LENGTH",
    "MemoryForgetter",
    "ProactiveReader",
    "TENANT_ID_PATTERN",
    "TenantError",
    "TenantRouter",
    # --- doctor / middleware ----------------------------------------------
    "CheckResult",
    "LongTermMemoryMiddleware",
    "MemoryDoctor",
    "ensure_reme_compat",
]

#: 名字 → 定义它的子模块。惰性导入靠这张表。
_OWNER: dict[str, str] = {
    "DEFAULT_SUBDIRS": "workspace",
    "ReMeWorkspace": "workspace",
    "WorkspaceCleanReport": "workspace",
    "WorkspaceError": "workspace",
    "MemoryClient": "client",
    "MemoryJobError": "client",
    "MemoryUnavailableError": "client",
    "REME_MIN_VERSION": "client",
    "REME_PYTHONPATH_HINT": "client",
    "REME_REQUIRED_VERSION": "client",
    "build_memory_client": "client",
    "reme_available": "client",
    "reme_version": "client",
    "EMBEDDED_JOB_BACKENDS": "config",
    "HarnessMemoryConfig": "config",
    "MemoryConfigError": "config",
    "RESCAN_REINDEX_JOB": "config",
    "DEFAULT_JOB_TIMEOUT_S": "jobs",
    "JobTimeout": "jobs",
    "MemoryJobs": "jobs",
    "RESIDENT_BACKENDS": "jobs",
    "DEFAULT_LIMIT": "search",
    "MemorySearch": "search",
    "SEARCH_JOB": "search",
    "TRAVERSE_JOB": "search",
    "FusedEntry": "hybrid",
    "FusionMode": "hybrid",
    "HybridRetriever": "hybrid",
    "RRF_K": "hybrid",
    "Citation": "citations",
    "CitationBuilder": "citations",
    "MemoryHit": "citations",
    "SearchResult": "citations",
    "merge_intervals": "citations",
    "to_memory_hit": "citations",
    "to_memory_hits": "citations",
    "DEFAULT_MAX_TOKENS": "budget",
    "MemoryBudget": "budget",
    "MemoryBudgetResult": "budget",
    "TokenEstimator": "budget",
    "estimate_tokens_heuristic": "budget",
    "render_memory_block": "budget",
    "DEFAULT_MIN_SCORE": "gating",
    "GateDecision": "gating",
    "MemoryGate": "gating",
    "MemoryWriteGate": "gating",
    "WriteDecision": "gating",
    "MemoryMetrics": "metrics",
    "SessionMetrics": "metrics",
    "DEFAULT_MAX_TAG_LENGTH": "frontmatter",
    "DEFAULT_MAX_TAGS_PER_FILE": "frontmatter",
    "DEFAULT_TAG_KEY": "frontmatter",
    "FrontMatter": "frontmatter",
    "FrontMatterError": "frontmatter",
    "normalize_query_tags": "frontmatter",
    "normalize_tags": "frontmatter",
    "split_front_matter": "frontmatter",
    "CatalogManager": "catalog",
    "ChangeSet": "catalog",
    "DEFAULT_CATALOG": "catalog",
    "DEFAULT_SCAN_SUFFIXES": "catalog",
    "KEYS_PER_CATALOG": "catalog",
    "ReconcileReport": "catalog",
    "INGEST_STATE_NAME": "ingest",
    "IngestResult": "ingest",
    "MemoryIngestor": "ingest",
    "AUTO_MEMORY_JOB": "distill",
    "DistillResult": "distill",
    "SessionDistiller": "distill",
    "AUTO_DREAM_JOB": "maintenance",
    "AUTO_RESOURCE_JOB": "maintenance",
    "FRONTMATTER_UPDATE_JOB": "maintenance",
    "MaintenanceResult": "maintenance",
    "MemoryMaintainer": "maintenance",
    "MemoryMaintenanceScheduler": "maintenance",
    "NightlyReport": "maintenance",
    "SESSION_END_EVENT_NAME": "maintenance",
    "DEMOTE_KEY": "forget",
    "DEMOTE_VALUE": "forget",
    "DEFAULT_SCAN_GLOBS": "forget",
    "ForgetPlan": "forget",
    "ForgetPolicy": "forget",
    "HitCounter": "forget",
    "MemoryForgetter": "forget",
    "DEFAULT_LOOKBACK_DAYS": "proactive",
    "DEFAULT_MIN_CONFIDENCE": "proactive",
    "DIALOG_DIR": "proactive",
    "ProactiveReader": "proactive",
    "MAX_TENANT_ID_LENGTH": "tenant",
    "TENANT_ID_PATTERN": "tenant",
    "TenantError": "tenant",
    "TenantRouter": "tenant",
    "CheckResult": "doctor",
    "MemoryDoctor": "doctor",
    "LongTermMemoryMiddleware": "middleware",
    "ensure_reme_compat": "middleware",
}

#: 导出名 → 它需要哪个包才能导入。用于把 ImportError 翻译成人话。
_REQUIRES: dict[str, str] = {
    "middleware": "agentscope",
}

_AGENTSCOPE_HINT = (
    "harness_kit.memory.middleware 需要 AgentScope（它继承官方 "
    "agentscope.middleware.ReMeMiddleware）。安装：\n"
    "  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/pip install -e "
    "third_party/agentscope"
)


def __getattr__(name: str) -> Any:
    """按需导入子模块里的名字（PEP 562）。

    Args:
        name (`str`): 属性名。

    Returns:
        `Any`: 对应的对象。

    Raises:
        `AttributeError`: 名字不在 :data:`__all__` 里。
        `ImportError`: 名字认得，但它依赖的包没装 ——
            错误信息里会给出**具体**的安装/排查命令，而不是一个裸的
            ``No module named 'reme'``。
    """
    module_name = _OWNER.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    try:
        module = importlib.import_module(f".{module_name}", __name__)
    except ImportError as exc:
        if module_name in _REQUIRES and _REQUIRES[module_name] == "agentscope":
            raise ImportError(f"{name} 不可用：{_AGENTSCOPE_HINT}（原始错误: {exc}）") from exc
        from .client import REME_PYTHONPATH_HINT

        raise ImportError(
            f"{name} 不可用：子模块 {module_name!r} 导入失败（{exc}）。\n"
            f"如果错误是 'No module named reme' 或版本不符，{REME_PYTHONPATH_HINT}",
        ) from exc

    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """把惰性导出也列进 ``dir()``（否则 IDE 补全看不到它们）。

    Returns:
        `list[str]`: 本模块的公开名字。
    """
    return sorted(set(__all__) | set(globals()))
