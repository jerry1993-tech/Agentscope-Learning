# -*- coding: utf-8 -*-
"""记忆侧指标：检索命中率、注入 token、被门控拒绝次数、写回成功率（契约 §3.19）。

**为什么这一层必须存在**

ReMe 的每一次失败都是**静默**的：

- 没配 embedding → 向量路返回 0 条，``success`` 仍然是 ``True``
  （``third_party/ReMe/reme/steps/index/search.py:337``）；
- 标签过滤没命中 → step 走早退分支，``answer`` 是空串、``results`` 是空列表；
- ``min_score`` 用量纲不对（见 :mod:`harness_kit.memory.hybrid`）→ 全被滤掉；
- 门控拒绝 → 调用方拿到的就是"没有记忆"，看起来和"记忆库里本来就没有"一模一样。

这些状态没有一个会抛异常，所以"记忆到底有没有在工作"只能靠**计数器**回答。
:class:`MemoryMetrics` 就是那套计数器：它是纯内存的、无依赖的、可在任何地方调用的
（middleware 的每个 hook、gate 的每次判定、写回的每次成败），
并且 :meth:`MemoryMetrics.snapshot` 给出的是一张**扁平的 float 字典** ——
因为它的下游是日志行与教程里的对照表，不是监控系统。

**刻意不做的事**

- 不引 prometheus / opentelemetry：`prometheus_client` 在本环境**确实没装**；
  `opentelemetry` 装了（1.44.0，是 AgentScope 的依赖），但教程刻意不往它上面接 ——
  一来加一个 exporter 就多一条会失败的网络路径，二来一张 ``dict[str, float]``
  已经足够回答"命中率是多少"（任务约束："不要引入未安装的第三方库"）。
- 不做时间窗口聚合：调用方自己决定何时 ``reset()``。窗口聚合属于"谁来解释这些数字"，
  不属于"记录这些数字"。

**零除的处理**

没有任何检索时 ``hit_rate`` 等比率返回 ``0.0`` 而不是抛 ``ZeroDivisionError``，
并且**同时**给出分母（``searches`` / ``injections`` / ``writebacks``）——
只给比率不给分母的指标表，读者无法判断"0% 是因为一次都没发生，还是因为全失败了"。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

__all__ = [
    "MemoryMetrics",
    "SessionMetrics",
]


class SessionMetrics:
    """单个会话的计数（``__slots__`` 值对象，不进 :meth:`MemoryMetrics.snapshot`）。

    会话级明细之所以不塞进扁平快照：快照的消费者是"全局健康度"的日志行，
    而会话级明细的消费者是"这个会话的记忆行为"的排查；两张表混在一起，
    键名会立刻爆炸（``session_a.hit_rate`` / ``session_b.hit_rate`` / ...）。
    """

    __slots__ = ("session_id", "searches", "searches_with_hits", "hits", "search_ms", "injections", "gated", "tokens", "writebacks", "writeback_failures")

    def __init__(self, session_id: str) -> None:
        """建一个空的会话计数器。

        Args:
            session_id (`str`): 会话 id。
        """
        self.session_id: str = session_id
        self.searches: int = 0
        self.searches_with_hits: int = 0
        self.hits: int = 0
        self.search_ms: float = 0.0
        self.injections: int = 0
        self.gated: int = 0
        self.tokens: int = 0
        self.writebacks: int = 0
        self.writeback_failures: int = 0

    def as_dict(self) -> dict[str, float]:
        """导出为扁平字典（含比率与分母）。

        Returns:
            `dict[str, float]`: 该会话的全部计数。
        """
        return {
            "searches": float(self.searches),
            "searches_with_hits": float(self.searches_with_hits),
            "hit_rate": _ratio(self.searches_with_hits, self.searches),
            "hits": float(self.hits),
            "mean_hits": _ratio(self.hits, self.searches),
            "search_ms_total": round(self.search_ms, 3),
            "mean_search_ms": round(_ratio(self.search_ms, self.searches), 3),
            "injections": float(self.injections),
            "gated": float(self.gated),
            "gated_rate": _ratio(self.gated, self.injections),
            "injected_tokens": float(self.tokens),
            "writebacks": float(self.writebacks),
            "writeback_failures": float(self.writeback_failures),
            "writeback_success_rate": _ratio(self.writebacks - self.writeback_failures, self.writebacks),
        }


class MemoryMetrics:
    """记忆侧计数器（契约 §3.19）。

    三个契约方法（:meth:`record_search` / :meth:`record_injection` /
    :meth:`snapshot`）覆盖"检索—注入"这条主链；另外补了两个方法，
    因为契约 §3.19 的说明里明确点名了"写回成功率"这个指标
    （:meth:`record_writeback`），以及会话级排查入口（:meth:`session_snapshot`）。

    Example::

        metrics = MemoryMetrics()
        metrics.record_search(session_id="s1", hits=3, elapsed_ms=42.0)
        metrics.record_injection(session_id="s1", tokens=180, gated=False)
        print(metrics.snapshot())      # {'searches': 1.0, 'hit_rate': 1.0, ...}

    线程/协程安全：本类只在单事件循环里被调用（AgentScope 的 hook 全在同一个
    loop 里），所以不加锁。如果将来要跨线程用，调用方必须自己加锁 ——
    这条限制写在 docstring 里，而不是假装它天生安全。
    """

    def __init__(self) -> None:
        """建一个空计数器集合。"""
        self._sessions: dict[str, SessionMetrics] = {}
        self._totals: SessionMetrics = SessionMetrics("__total__")
        self._min_score_rejections: int = 0
        self._budget_rejections: int = 0

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def record_search(self, *, session_id: str, hits: int, elapsed_ms: float) -> None:
        """记录一次检索（契约 §3.19）。

        Args:
            session_id (`str`): 会话 id；空串会被规整成 ``"<unknown>"``，
                而不是新建一个键为 ``""`` 的会话 —— 否则所有"拿不到 session_id"
                的调用会挤在同一个匿名桶里，看起来像同一个会话。
            hits (`int`): 命中条数；负数按 0 计。
            elapsed_ms (`float`): 墙上时间（毫秒）；负数按 0 计。
        """
        bucket = self._bucket(session_id)
        count = max(0, int(hits))
        cost = max(0.0, float(elapsed_ms))
        for target in (bucket, self._totals):
            target.searches += 1
            target.hits += count
            target.search_ms += cost
            if count > 0:
                target.searches_with_hits += 1

    def record_injection(self, *, session_id: str, tokens: int, gated: bool) -> None:
        """记录一次注入决策（契约 §3.19）。

        ``gated=True`` 表示"本轮本来要注入，被门控拦下了"。**被拦下也要记**，
        这正是 :mod:`harness_kit.memory.gating` 模块 docstring 里那条
        "被过滤项必须计入 metrics" 的落点：只统计成功的注入，
        会让"门控把所有记忆都拦了"这种故障表现为"一切正常，只是没记忆"。

        Args:
            session_id (`str`): 会话 id。
            tokens (`int`): 本次注入的 token 数；被拦下时传 0 或预估量都行，
                但 ``gated=True`` 时**不计入** ``injected_tokens``。
            gated (`bool`): 是否被门控拒绝。
        """
        bucket = self._bucket(session_id)
        for target in (bucket, self._totals):
            target.injections += 1
            if gated:
                target.gated += 1
            else:
                target.tokens += max(0, int(tokens))

    def record_writeback(self, *, session_id: str, ok: bool) -> None:
        """记录一次写回（AutoMemoryStep）的成败。

        Args:
            session_id (`str`): 会话 id。
            ok (`bool`): 是否成功。
        """
        bucket = self._bucket(session_id)
        for target in (bucket, self._totals):
            target.writebacks += 1
            if not ok:
                target.writeback_failures += 1
        if not ok:
            logger.debug("memory writeback 失败: session={}", session_id)

    def record_gate_rejection(self, *, reason: str) -> None:
        """记录一次门控拒绝的原因（便于回答"为什么没注入"）。

        Args:
            reason (`str`): :class:`~harness_kit.memory.gating.GateDecision` 的 ``reason``。
        """
        if reason == "below_min_score":
            self._min_score_rejections += 1
        elif reason == "over_budget":
            self._budget_rejections += 1

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, float]:
        """导出全局扁平静态（契约 §3.19）。

        Returns:
            `dict[str, float]`: 键含义 ——

            ``searches`` / ``searches_with_hits`` / ``hit_rate``
                检索次数、其中至少命中 1 条的次数、两者的比值。
            ``hits`` / ``mean_hits``
                命中总条数、每次检索的平均命中条数。
            ``search_ms_total`` / ``mean_search_ms``
                检索总耗时与平均耗时（毫秒）。
            ``injections`` / ``gated`` / ``gated_rate``
                注入决策次数、被门控拒绝的次数、拒绝率。
            ``injected_tokens``
                实际注入的 token 总量（不含被拒绝的）。
            ``writebacks`` / ``writeback_failures`` / ``writeback_success_rate``
                写回次数、失败次数、成功率。
            ``gate_rejections_min_score`` / ``gate_rejections_over_budget``
                按原因拆分的拒绝次数。
            ``sessions``
                出现过活动的会话数（float，因为快照是 float 字典）。
        """
        result = dict(self._totals.as_dict())
        result["gate_rejections_min_score"] = float(self._min_score_rejections)
        result["gate_rejections_over_budget"] = float(self._budget_rejections)
        result["sessions"] = float(len(self._sessions))
        return result

    def session_snapshot(self, session_id: str) -> dict[str, float]:
        """导出单个会话的计数（不存在时返回全 0）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `dict[str, float]`: 该会话的扁平字典。
        """
        bucket = self._sessions.get(str(session_id))
        if bucket is None:
            return SessionMetrics(str(session_id)).as_dict()
        return bucket.as_dict()

    def sessions(self) -> list[str]:
        """列出出现过活动的会话 id。

        Returns:
            `list[str]`: 已排序的会话 id。
        """
        return sorted(self._sessions)

    def reset(self) -> None:
        """清零全部计数（含会话级明细与拒绝原因）。"""
        self._sessions.clear()
        self._totals = SessionMetrics("__total__")
        self._min_score_rejections = 0
        self._budget_rejections = 0

    def log(self, *, level: str = "INFO") -> None:
        """把快照写成一行日志。

        Args:
            level (`str`): loguru 的级别名，如 ``"INFO"`` / ``"DEBUG"``。
        """
        payload: dict[str, Any] = self.snapshot()
        logger.log(level, "memory metrics: {}", payload)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _bucket(self, session_id: str) -> SessionMetrics:
        """取（或建）一个会话桶。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMetrics`: 会话计数器。
        """
        key = str(session_id or "").strip() or "<unknown>"
        bucket = self._sessions.get(key)
        if bucket is None:
            bucket = SessionMetrics(key)
            self._sessions[key] = bucket
        return bucket


def _ratio(numerator: float, denominator: float) -> float:
    """安全除法。

    Args:
        numerator (`float`): 分子。
        denominator (`float`): 分母。

    Returns:
        `float`: ``numerator / denominator``；分母为 0 时返回 ``0.0``。
    """
    if not denominator:
        return 0.0
    return float(numerator) / float(denominator)
