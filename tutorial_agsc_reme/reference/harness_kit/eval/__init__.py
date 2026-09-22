# -*- coding: utf-8 -*-
"""评测层（第 20 讲）：数据集 → 运行器 → 指标 → 报告 → 数据合成。

六个子模块各管一段：

- :mod:`harness_kit.eval.dataset` —— ``EvalCase`` / ``EvalDataset`` + JSONL 读写；
- :mod:`harness_kit.eval.runner` —— ``EvalRunner`` / ``EvalResult``（并发跑真 Agent）；
- :mod:`harness_kit.eval.metrics` —— 指标函数与 ``MetricFn`` / ``ObservedRun``；
- :mod:`harness_kit.eval.report` —— ``EvalReport`` 对比报告（Markdown + JSON）；
- :mod:`harness_kit.eval.synthesize` —— 从会话事件流反推可复用的评测样本；
- :mod:`harness_kit.eval.feedback` —— 生产反馈闭环（§7 补遗）：失败会话挖掘 →
  回归评测集 → 回归闸门 → 台账。

**前后两半的分工**：前五个模块回答"怎么评测"（离线可跑、不依赖线上数据），
``feedback`` 回答"**评测什么**" —— 它把线上出过事的会话变成用例，把"这次改动
有没有让老问题复发"变成一道可以自动拦下来的闸门。它**只读**会话事件日志，
一行 Agent / 中间件代码都不改，因此不需要动 docs 之外的任何上游实现。

**为什么需要这一层**：AgentScope 2.0.8 没有 benchmark / eval 引擎，ReMe 只有
单步 ``evaluate`` 组件，谁都没有"批量跑用例 + 跨 Profile 对比 + 出报告"。
契约 §1.3 缺口 3 指的就是这里。

**导入顺序有讲究**：``runner`` 与 ``report`` 互为依赖（``runner`` 要
``EvalReport``、``report`` 要 ``EvalResult``）。解法是 ``report`` 在模块顶层
import ``runner.EvalResult``，``runner`` 只在 ``run()`` 内部局部 import
``EvalReport`` —— 因此**先 import ``runner`` 再 import ``report``** 是安全的，
反过来也只多走一次局部导入，不会死锁。下面的惰性表不关心顺序，因为
``__getattr__`` 是运行时才解析。

与 ``harness_kit/__init__.py``、``harness_kit/observe/__init__.py`` 一致，
这里不做立即重导出：``eval`` 一被 import 就拉进整套评测依赖没有必要。
"""

from typing import TYPE_CHECKING, Any

__all__ = [
    "DEFAULT_MAX_METRIC_DROP",
    "DEFAULT_OVER_ITERATIONS",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_TRIGGERS",
    "EvalCase",
    "EvalDataset",
    "EvalReport",
    "EvalResult",
    "EvalRunner",
    "FAILURE_TRIGGERS",
    "FeedbackLedgerEntry",
    "GateDecision",
    "MetricFn",
    "NOT_APPLICABLE",
    "ObservedRun",
    "RegressionGate",
    "RewriterFn",
    "SessionPick",
    "append_ledger",
    "build_regression_dataset",
    "collect_observed_run",
    "current_run",
    "dataset_from_cases",
    "entries_from_decision",
    "observed_run",
    "percentile",
    "read_ledger",
    "scan_sessions",
    "score_summary",
    "synthesize_cases",
    "synthesize_from_session",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "EvalCase": ("harness_kit.eval.dataset", "EvalCase"),
    "EvalDataset": ("harness_kit.eval.dataset", "EvalDataset"),
    "RewriterFn": ("harness_kit.eval.dataset", "RewriterFn"),
    "dataset_from_cases": ("harness_kit.eval.dataset", "dataset_from_cases"),
    "synthesize_cases": ("harness_kit.eval.dataset", "synthesize_cases"),
    "EvalResult": ("harness_kit.eval.runner", "EvalResult"),
    "EvalRunner": ("harness_kit.eval.runner", "EvalRunner"),
    "collect_observed_run": ("harness_kit.eval.runner", "collect_observed_run"),
    "MetricFn": ("harness_kit.eval.metrics", "MetricFn"),
    "ObservedRun": ("harness_kit.eval.metrics", "ObservedRun"),
    "NOT_APPLICABLE": ("harness_kit.eval.metrics", "NOT_APPLICABLE"),
    "DEFAULT_THRESHOLDS": ("harness_kit.eval.metrics", "DEFAULT_THRESHOLDS"),
    "current_run": ("harness_kit.eval.metrics", "current_run"),
    "observed_run": ("harness_kit.eval.metrics", "observed_run"),
    "score_summary": ("harness_kit.eval.metrics", "score_summary"),
    "EvalReport": ("harness_kit.eval.report", "EvalReport"),
    "percentile": ("harness_kit.eval.report", "percentile"),
    "synthesize_from_session": ("harness_kit.eval.synthesize", "synthesize_from_session"),
    "FeedbackLedgerEntry": ("harness_kit.eval.feedback", "FeedbackLedgerEntry"),
    "GateDecision": ("harness_kit.eval.feedback", "GateDecision"),
    "RegressionGate": ("harness_kit.eval.feedback", "RegressionGate"),
    "SessionPick": ("harness_kit.eval.feedback", "SessionPick"),
    "append_ledger": ("harness_kit.eval.feedback", "append_ledger"),
    "build_regression_dataset": ("harness_kit.eval.feedback", "build_regression_dataset"),
    "entries_from_decision": ("harness_kit.eval.feedback", "entries_from_decision"),
    "read_ledger": ("harness_kit.eval.feedback", "read_ledger"),
    "scan_sessions": ("harness_kit.eval.feedback", "scan_sessions"),
    "FAILURE_TRIGGERS": ("harness_kit.eval.feedback", "FAILURE_TRIGGERS"),
    "DEFAULT_TRIGGERS": ("harness_kit.eval.feedback", "DEFAULT_TRIGGERS"),
    "DEFAULT_MAX_METRIC_DROP": ("harness_kit.eval.feedback", "DEFAULT_MAX_METRIC_DROP"),
    "DEFAULT_OVER_ITERATIONS": ("harness_kit.eval.feedback", "DEFAULT_OVER_ITERATIONS"),
}


def __getattr__(name: str) -> Any:
    """惰性导入（:pep:`562`）。

    Args:
        name (`str`): 属性名。

    Returns:
        `Any`: 目标对象。

    Raises:
        `AttributeError`: 名字不在导出表内。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}; available: {sorted(_LAZY_EXPORTS)}",
        )
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让 ``dir()`` 同时列出惰性导出项。

    Returns:
        `list[str]`: 排序后的公开名字。
    """
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from harness_kit.eval.dataset import EvalCase, EvalDataset, RewriterFn
    from harness_kit.eval.feedback import (
        DEFAULT_MAX_METRIC_DROP,
        DEFAULT_OVER_ITERATIONS,
        DEFAULT_TRIGGERS,
        FAILURE_TRIGGERS,
        FeedbackLedgerEntry,
        GateDecision,
        RegressionGate,
        SessionPick,
        append_ledger,
        build_regression_dataset,
        entries_from_decision,
        read_ledger,
        scan_sessions,
    )
    from harness_kit.eval.metrics import DEFAULT_THRESHOLDS, NOT_APPLICABLE, MetricFn, ObservedRun
    from harness_kit.eval.report import EvalReport, percentile
    from harness_kit.eval.runner import EvalResult, EvalRunner, collect_observed_run
    from harness_kit.eval.synthesize import synthesize_from_session
