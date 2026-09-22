# -*- coding: utf-8 -*-
"""评测报告（``EvalReport``，契约 §3.20 / §5.6，第 20 讲）。

一份报告要回答三个问题，缺一个都不算有用：

1. **整体怎么样** —— 各指标均值、通过率、延迟 P50/P95、token、成本
   （:meth:`EvalReport.summary`）；
2. **哪几条挂了** —— 失败清单（:meth:`EvalReport.failures`），
   而不是让人在几十行 JSON 里翻；
3. **这次和上次比如何** —— :meth:`EvalReport.compare` 产出 Markdown 对比表
   与结论（"哪一项变好了 / 变差了 / 没动"）。

**P50/P95 的算法口径**：只要样本数 < 2 就退回成"最小值 / 最大值"。
单样本算分位数没有意义（线性插值会给出样本本身），这里不装样子。

对齐契约：§3.20 的 ``EvalReport`` 只有 ``dataset`` / ``started_at`` /
``finished_at`` / ``results``，§5.6 多一个 ``profile``。本实现按 §5.6
（多出来的字段带默认值，因此 §3.20 的构造方式仍然合法）。

Example:
    >>> from datetime import datetime, timezone
    >>> report = EvalReport(
    ...     dataset="demo",
    ...     profile="default",
    ...     started_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
    ...     finished_at=datetime(2026, 9, 22, 0, 0, 30, tzinfo=timezone.utc),
    ...     results=[EvalResult(case_id="c1", output="4", ok=True,
    ...                         scores={"contains": 1.0}, latency_ms=1200.0)],
    ... )
    >>> report.summary()["pass_rate"]
    1.0
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.eval.metrics import DEFAULT_THRESHOLDS, score_summary
from harness_kit.eval.runner import EvalResult

__all__ = ["EvalReport", "MetricDelta", "format_table", "percentile"]


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（与 ``numpy.percentile(method="linear")`` 同语义）。

    Args:
        values (`Sequence[float]`): 样本（顺序无所谓）。
        q (`float`): 分位点，``0 <= q <= 1``。

    Returns:
        `float`: 分位数；空样本返回 ``0.0``，单样本返回该样本本身。

    Raises:
        `ValueError`: ``q`` 不在 ``[0, 1]``。
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"分位点必须在 [0,1]，收到 {q}")
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


class MetricDelta(BaseModel):
    """两个报告之间某个指标的差值。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """指标名。"""

    before: float
    """基线值。"""

    after: float
    """当前值。"""

    @property
    def delta(self) -> float:
        """差值（``after - before``）。

        Returns:
            `float`: 差值。
        """
        return round(self.after - self.before, 6)

    @property
    def verdict(self) -> str:
        """结论词（报告里的"结论"列）。

        Returns:
            `str`: ``"提升"`` / ``"下降"`` / ``"持平"``。
        """
        if abs(self.delta) < 1e-9:
            return "持平"
        return "提升" if self.delta > 0 else "下降"


class EvalReport(BaseModel):
    """一次评测的完整结果（契约 §5.6）。"""

    model_config = ConfigDict(extra="forbid")

    dataset: str
    """数据集名。"""

    profile: str = ""
    """本次评测使用的 Profile 名（§5.6 有、§3.20 没有，故带默认值）。"""

    started_at: datetime
    """开始时间（UTC, tz-aware）。"""

    finished_at: datetime
    """结束时间（UTC, tz-aware）。"""

    results: list[EvalResult] = Field(default_factory=list)
    """逐用例结果。"""

    thresholds: dict[str, float] = Field(default_factory=dict)
    """本次生效的通过门槛，随报告一起落盘（否则"上次为什么算通过"无从复原）。"""

    notes: list[str] = Field(default_factory=list)
    """人工备注（例如"本次跳过了 LLM-as-judge"）。"""

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------
    @property
    def duration_s(self) -> float:
        """评测总墙钟时间（秒）。

        Returns:
            `float`: 秒数。
        """
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    @property
    def pass_rate(self) -> float:
        """通过率。

        Returns:
            `float`: ``通过数 / 总数``；空报告返回 ``0.0``。
        """
        if not self.results:
            return 0.0
        return sum(1 for item in self.results if item.ok) / len(self.results)

    def failures(self) -> list[EvalResult]:
        """失败（``ok=False`` 或 ``error`` 非空）的用例。

        Returns:
            `list[EvalResult]`: 失败清单。
        """
        return [item for item in self.results if not item.ok or item.error]

    def metric_names(self) -> list[str]:
        """报告里出现过的指标名（去掉 ``*_applicable`` 计数项）。

        Returns:
            `list[str]`: 排序后的指标名。
        """
        names = {key for item in self.results for key in item.scores}
        return sorted(name for name in names if not name.endswith("_applicable"))

    def summary(self) -> dict[str, float]:
        """汇总指标（契约 §3.20 的 ``summary()``）。

        包含：每个指标的均值与其适用条数、``pass_rate``、
        ``latency_p50_ms`` / ``latency_p95_ms`` / ``latency_mean_ms``、
        ``tokens_input`` / ``tokens_output`` / ``tokens_total``、
        ``cost_usd``、``cases`` / ``errors``。

        Returns:
            `dict[str, float]`: 汇总字典（值一律是 ``float``，方便直接落 JSON）。
        """
        out: dict[str, float] = score_summary([item.scores for item in self.results])
        latencies = [item.latency_ms for item in self.results]
        out["cases"] = float(len(self.results))
        out["passed"] = float(sum(1 for item in self.results if item.ok))
        out["pass_rate"] = round(self.pass_rate, 6)
        out["errors"] = float(sum(1 for item in self.results if item.error))
        out["latency_p50_ms"] = round(percentile(latencies, 0.5), 3)
        out["latency_p95_ms"] = round(percentile(latencies, 0.95), 3)
        out["latency_mean_ms"] = round(
            sum(latencies) / len(latencies) if latencies else 0.0,
            3,
        )
        out["tokens_input"] = float(sum(item.tokens.input_tokens for item in self.results))
        out["tokens_output"] = float(sum(item.tokens.output_tokens for item in self.results))
        out["tokens_total"] = float(sum(item.tokens.total_tokens for item in self.results))
        out["cost_usd"] = round(sum(item.cost_usd for item in self.results), 8)
        out["duration_s"] = round(self.duration_s, 3)
        return out

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def to_markdown(self, *, max_failures: int = 10) -> str:
        """渲染成 Markdown 报告（契约 §3.20 的 ``to_markdown()``）。

        结构固定：概览表 → 指标表 → 失败清单 → 结论。

        Args:
            max_failures (`int`): 失败清单最多列几条。

        Returns:
            `str`: Markdown 文本。
        """
        stats = self.summary()
        lines: list[str] = [
            f"# 评测报告：{self.dataset}",
            "",
            f"- Profile: `{self.profile or '(未指定)'}`",
            f"- 用例数: {int(stats['cases'])}（通过 {int(stats['passed'])}，"
            f"通过率 {stats['pass_rate']:.1%}）",
            f"- 时间: {self.started_at.isoformat()} → {self.finished_at.isoformat()}"
            f"（{stats['duration_s']:.1f}s）",
            f"- 延迟: P50 {stats['latency_p50_ms']:.0f}ms / "
            f"P95 {stats['latency_p95_ms']:.0f}ms / 均值 {stats['latency_mean_ms']:.0f}ms",
            f"- Token: 输入 {int(stats['tokens_input'])} / 输出 {int(stats['tokens_output'])}"
            f" / 合计 {int(stats['tokens_total'])}",
            f"- 估算成本: ${stats['cost_usd']:.6f}（按内置示例价目表，非官方报价）",
            "",
            "## 指标",
            "",
            "| 指标 | 均值 | 适用用例 | 门槛 | 结论 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name in self.metric_names():
            mean = stats.get(name, 0.0)
            applicable = int(stats.get(f"{name}_applicable", 0.0))
            threshold = self.thresholds.get(name)
            if threshold is None:
                verdict = "（未设门槛）"
                threshold_text = "—"
            else:
                verdict = "达标" if mean >= threshold else "未达标"
                threshold_text = f"{threshold:g}"
            lines.append(
                f"| {name} | {mean:.3f} | {applicable}/{int(stats['cases'])} "
                f"| {threshold_text} | {verdict} |",
            )
        if not self.metric_names():
            lines.append("| （无） | 0 | 0 | — | — |")

        lines.extend(["", "## 失败用例", ""])
        failures = self.failures()
        if not failures:
            lines.append("无。")
        else:
            lines.extend(["| 用例 | 原因 | 输出预览 |", "| --- | --- | --- |"])
            for item in failures[:max_failures]:
                reason = item.error or self._reason_of(item)
                lines.append(
                    f"| {item.case_id} | {_cell(reason, 60)} | {_cell(item.output, 80)} |",
                )
            if len(failures) > max_failures:
                lines.append(f"| … | 还有 {len(failures) - max_failures} 条 | |")

        lines.extend(["", "## 结论", ""])
        lines.extend(f"- {line}" for line in self.conclusions())
        if self.notes:
            lines.extend(["", "## 备注", ""])
            lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)

    def conclusions(self) -> list[str]:
        """给出人话结论（报告"结论"小节的内容）。

        Returns:
            `list[str]`: 结论行。
        """
        stats = self.summary()
        lines: list[str] = []
        if not self.results:
            return ["本次没有跑任何用例 —— 检查数据集是否为空。"]

        if stats["pass_rate"] >= 0.9:
            lines.append(
                f"整体通过率 {stats['pass_rate']:.1%}，达到可发布水平（阈值 90%）。",
            )
        elif stats["pass_rate"] >= 0.6:
            lines.append(
                f"整体通过率 {stats['pass_rate']:.1%}，可以继续迭代但不宜发布。",
            )
        else:
            lines.append(
                f"整体通过率 {stats['pass_rate']:.1%}，明显不达标，先定位失败用例再谈其它指标。",
            )

        for name in self.metric_names():
            threshold = self.thresholds.get(name)
            mean = stats.get(name, 0.0)
            applicable = int(stats.get(f"{name}_applicable", 0.0))
            if applicable == 0:
                lines.append(
                    f"指标 {name} 没有任何适用用例（全是「不适用」），本次不构成证据。",
                )
                continue
            if threshold is not None and mean < threshold:
                lines.append(
                    f"指标 {name} 均值 {mean:.3f} 低于门槛 {threshold:g}（{applicable} 条适用），需要改进。",
                )

        if stats["latency_p95_ms"] > 0:
            lines.append(
                f"延迟 P95 = {stats['latency_p95_ms']:.0f}ms，"
                f"P50 = {stats['latency_p50_ms']:.0f}ms（P95/P50 = "
                f"{stats['latency_p95_ms'] / max(stats['latency_p50_ms'], 1e-9):.2f}）；"
                "比值明显大于 2 说明存在长尾，通常是工具调用或重试造成的。",
            )
        if stats["errors"]:
            lines.append(f"有 {int(stats['errors'])} 条用例抛了异常，已计入失败，先看失败清单。")
        lines.append(
            f"总消耗 {int(stats['tokens_total'])} token，估算 ${stats['cost_usd']:.6f}。",
        )
        return lines

    def _reason_of(self, item: EvalResult) -> str:
        """为一条未达标但没抛异常的用例生成原因文本。

        Args:
            item (`EvalResult`): 结果。

        Returns:
            `str`: 原因文本。
        """
        below = [
            f"{name}={value:.3f}<{self.thresholds[name]:g}"
            for name, value in sorted(item.scores.items())
            if value >= 0.0
            and name in self.thresholds
            and value < self.thresholds[name]
        ]
        return "未达标: " + ", ".join(below) if below else "未达标（无门槛明细）"

    def to_json(self, *, indent: int | None = 2) -> str:
        """序列化成 JSON（契约 §3.20 的 ``to_json()``）。

        Args:
            indent (`int | None`): 缩进；``None`` 输出紧凑单行。

        Returns:
            `str`: JSON 文本。
        """
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "EvalReport":
        """从 JSON 反序列化（:meth:`compare` 需要读历史报告）。

        Args:
            text (`str`): JSON 文本。

        Returns:
            `EvalReport`: 报告。

        Raises:
            `ValueError`: JSON 非法或结构不符。
        """
        payload = json.loads(text)
        return cls.model_validate(payload)

    def save(self, path: str | Path, *, markdown: bool = True) -> dict[str, Path]:
        """把报告落盘（JSON + Markdown）。

        Args:
            path (`str | Path`): 目标路径。以 ``.json`` 结尾时 JSON 用该路径，
                Markdown 换后缀；否则当成"输出目录 + 前缀"。
            markdown (`bool`): 是否同时写 Markdown。

        Returns:
            `dict[str, Path]`: ``{"json": ..., "markdown": ...}``（未写的不含）。
        """
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        json_path = target if target.suffix.lower() == ".json" else target.with_suffix(".json")
        json_path.write_text(self.to_json(), encoding="utf-8")
        written["json"] = json_path.resolve()
        if markdown:
            md_path = target if target.suffix.lower() == ".md" else target.with_suffix(".md")
            md_path.write_text(self.to_markdown(), encoding="utf-8")
            written["markdown"] = md_path.resolve()
        logger.bind(dataset=self.dataset).info(
            "评测报告已落盘: {}",
            {key: str(value) for key, value in written.items()},
        )
        return written

    # ------------------------------------------------------------------
    # 对比
    # ------------------------------------------------------------------
    def compare(self, baseline: "EvalReport") -> str:
        """与基线报告对比，产出 Markdown 表格 + 结论（契约 §3.20 的"对比报告"）。

        Args:
            baseline (`EvalReport`): 基线报告（上一次的）。

        Returns:
            `str`: Markdown 文本。
        """
        before = baseline.summary()
        after = self.summary()
        keys = [
            "pass_rate",
            "latency_p50_ms",
            "latency_p95_ms",
            "tokens_total",
            "cost_usd",
        ]
        keys.extend(self.metric_names())
        lines = [
            f"# 对比：{baseline.dataset} vs {self.dataset}",
            "",
            f"- 基线: profile=`{baseline.profile or '(未指定)'}` "
            f"{baseline.started_at.isoformat()}（{int(before['cases'])} 条）",
            f"- 当前: profile=`{self.profile or '(未指定)'}` "
            f"{self.started_at.isoformat()}（{int(after['cases'])} 条）",
            "",
            "| 指标 | 基线 | 当前 | 差值 | 结论 |",
            "| --- | --- | --- | --- | --- |",
        ]
        deltas: list[MetricDelta] = []
        for key in keys:
            if key not in before and key not in after:
                continue
            delta = MetricDelta(
                name=key,
                before=float(before.get(key, 0.0)),
                after=float(after.get(key, 0.0)),
            )
            deltas.append(delta)
            lines.append(
                f"| {key} | {delta.before:.4f} | {delta.after:.4f} "
                f"| {delta.delta:+.4f} | {delta.verdict} |",
            )

        lines.extend(["", "## 结论", ""])
        lines.extend(f"- {line}" for line in self._comparison_conclusions(deltas, before, after))
        return "\n".join(lines)

    @staticmethod
    def _comparison_conclusions(
        deltas: Sequence[MetricDelta],
        before: Mapping[str, float],
        after: Mapping[str, float],
    ) -> list[str]:
        """由差值给出结论行。

        Args:
            deltas (`Sequence[MetricDelta]`): 差值列表。
            before (`Mapping[str, float]`): 基线汇总。
            after (`Mapping[str, float]`): 当前汇总。

        Returns:
            `list[str]`: 结论行。
        """
        lines: list[str] = []
        case_gap = int(after.get("cases", 0.0)) - int(before.get("cases", 0.0))
        if case_gap != 0:
            lines.append(
                f"两次报告用例数不同（{int(before.get('cases', 0.0))} → "
                f"{int(after.get('cases', 0.0))}），指标均值不可直接比较，"
                "只有共同的用例子集才有说服力。",
            )

        wanted = {"pass_rate", "latency_p95_ms", "latency_p50_ms", "tokens_total", "cost_usd"}
        degraded: list[str] = []
        improved: list[str] = []
        for delta in deltas:
            if delta.name in wanted:
                # 越低越好的指标：方向要反过来读
                lower_is_better = delta.name in {"latency_p50_ms", "latency_p95_ms", "tokens_total", "cost_usd"}
                good = delta.delta < 0 if lower_is_better else delta.delta > 0
                if abs(delta.delta) < 1e-9:
                    continue
                text = f"{delta.name} {delta.before:.4f} → {delta.after:.4f}"
                (improved if good else degraded).append(text)
            elif delta.name.endswith("_applicable"):
                continue
            else:
                if delta.delta < 0:
                    degraded.append(f"{delta.name} 均值下降 {abs(delta.delta):.4f}")
                elif delta.delta > 0:
                    improved.append(f"{delta.name} 均值提升 {delta.delta:.4f}")

        if improved:
            lines.append("变好: " + "；".join(improved) + "。")
        if degraded:
            lines.append("变差: " + "；".join(degraded) + "。")
        if not improved and not degraded:
            lines.append("两项报告的所有指标都持平 —— 检查这次改动是否真的生效了。")
        if not degraded and improved:
            lines.append("本次没有出现任何指标退化，可以进入下一轮。")
        return lines


def format_table(rows: Sequence[Mapping[str, Any]], headers: Sequence[str]) -> str:
    """把若干行渲染成 Markdown 表格（CLI 输出用，避免引 rich 依赖）。

    Args:
        rows (`Sequence[Mapping[str, Any]]`): 行数据。
        headers (`Sequence[str]`): 表头。

    Returns:
        `str`: Markdown 表格文本。
    """
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def _cell(text: str, width: int) -> str:
    """把单元格内容压成单行且不破坏表格。

    Args:
        text (`str`): 原始文本。
        width (`int`): 最大宽度。

    Returns:
        `str`: 处理后的文本。
    """
    collapsed = " ".join(str(text).split()).replace("|", "\\|")
    return collapsed if len(collapsed) <= width else collapsed[:width] + "…"
