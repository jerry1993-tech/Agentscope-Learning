# -*- coding: utf-8 -*-
"""生产反馈闭环（第 20 讲，参考架构 L3 的「真实世界反馈闭环」）。

**这一层要回答的问题**：第 20 讲前半段造出来的评测引擎是一台"离线跑步机"——
它能告诉你"我这份手写的 20 条用例过了几条"，但它**不知道线上发生了什么**。
于是 Harness 的迭代会退化成一种信仰行为：改一版 prompt，重新跑一遍那
20 条手写用例，看着分数没掉就上线。这套流程有两个致命缺陷：

1. **用例不是从真实流量来的**。手写的 20 条，覆盖的是"我以为用户会问什么"，
   而不是"用户实际在问什么"。线上真正翻车的那一类输入，往往一条用例都没有。
2. **没有回归闸门**。上一版跑出 0.85、这一版跑出 0.83，谁来决定"能不能发"？
   靠人看报告，等于没有闸门。

本模块补的就是这两件事，并且**全部复用前 19 讲已经落盘的事实**：

- 事实来源是第 09 讲的**会话事件日志**（``SessionStoreBase``），
  不是新造一套埋点。线上每一次 response 都已经在日志里了；
- 失败信号是**事件 payload 里的真值**（``TOOL_RESULT.state`` / ``PERMISSION.behavior`` /
  ``REPLY_END.iterations``），不是模型自评；
- 样本转换直接调第 20 讲自己的 :func:`~harness_kit.eval.synthesize.synthesize_from_session`，
  **不重复实现**事件流解析；
- 回归判定直接调第 20 讲自己的 :meth:`~harness_kit.eval.report.EvalReport.compare`。

**闭环的四步**（也是本模块四个公开入口）：

.. code-block:: text

    scan_sessions()            线上日志 → 挑出"出过事"的会话 + 理由
            ↓
    build_regression_dataset() 这些会话 → 回归评测集（带溯源 metadata）
            ↓
    EvalRunner.run()           跑（这一步是第 20 讲已有的，本模块不碰）
            ↓
    RegressionGate.evaluate()  新报告 vs 基线报告 → 放行 / 拦截 + 理由
            ↓
    append_ledger()            把"哪条线上会话 → 变成哪条用例 → 闸门怎么判"落盘

``append_ledger`` 那一步是"闭环"这个词的**字面含义**：没有它，你没法回答
"这条用例当初是因为线上的哪一次事故才被加进来的"。台账是只追加的，
和 :class:`~harness_kit.session.store.SessionStoreBase` 的事件日志同一个哲学。

**边界（这一层明确不做什么）**：

- 不接真实的反馈通道（点赞 / 点踩 / 工单）。那些是业务系统的职责，
  本模块只认**已经写进事件日志**的 ``CUSTOM`` 事件（``name="user_feedback"``），
  业务侧要用就自己往日志里写一条 —— 写日志的口是
  :meth:`~harness_kit.events.bus.EventBus.publish`，第 03 讲已经交付了。
- 不做自动改 prompt / 自动调参。闭环的最后一公里（**改什么**）必须由人决定，
  本模块只负责把"该改"这件事变成一条不可抵赖的判决。
- 不重新实现事件解析与样本合成，只做"选哪些会话"和"判能不能发"。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.eval.dataset import EvalCase, EvalDataset
from harness_kit.eval.report import EvalReport
from harness_kit.eval.synthesize import synthesize_from_session
from harness_kit.events.types import utc_now
from harness_kit.session.models import SessionEvent

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from harness_kit.session.store import SessionStoreBase

__all__ = [
    "DEFAULT_MAX_METRIC_DROP",
    "DEFAULT_OVER_ITERATIONS",
    "DEFAULT_TRIGGERS",
    "FAILURE_TRIGGERS",
    "FeedbackLedgerEntry",
    "GateDecision",
    "RegressionGate",
    "SessionPick",
    "append_ledger",
    "build_regression_dataset",
    "scan_sessions",
]


# ----------------------------------------------------------------------
# 失败信号：四种，全部来自事件 payload 的真值
# ----------------------------------------------------------------------

FAILURE_TRIGGERS: tuple[str, ...] = (
    "tool_error",
    "permission_denied",
    "over_iterations",
    "user_flagged",
)
"""内置的四种失败信号名（顺序即报告里的展示顺序）。"""

DEFAULT_TRIGGERS: tuple[str, ...] = FAILURE_TRIGGERS
"""默认启用全部四种信号。"""

DEFAULT_OVER_ITERATIONS: int = 6
"""``REPLY_END.iterations`` 达到它就算"绕圈子"（默认 6；``AgentSpec.max_iters`` 默认 20）。"""

DEFAULT_MAX_METRIC_DROP: float = 0.02
"""回归闸门允许的单指标最大跌幅（绝对差，2 个百分点）。"""


class SessionPick(BaseModel):
    """一个"值得变成回归用例"的会话，以及它被选中的理由。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    """会话 id。"""

    reasons: list[str] = Field(default_factory=list)
    """命中的信号名（``FAILURE_TRIGGERS`` 的子集），按首次出现顺序去重。"""

    evidence: list[str] = Field(default_factory=list)
    """人类可读的证据行，形如 ``"seq=12 TOOL_RESULT(state=error) bash: 权限不足"``。"""

    n_events: int = 0
    """该会话的事件总条数。"""

    n_replies: int = 0
    """收尾了的回合数（有 ``REPLY_END`` 的）。"""

    def reason_text(self) -> str:
        """把理由拼成一行摘要。

        Returns:
            `str`: ``"tool_error+user_flagged"`` 形态。
        """
        return "+".join(self.reasons) if self.reasons else "(无)"


# ----------------------------------------------------------------------
# 第一跳：线上日志 → 挑出出过事的会话
# ----------------------------------------------------------------------


def _classify(
    events: Sequence[SessionEvent],
    *,
    triggers: Sequence[str],
    over_iterations: int,
) -> tuple[list[str], list[str], int]:
    """扫一个会话的事件流，判定它命中了哪些失败信号。

    Args:
        events (`Sequence[SessionEvent]`): 该会话的事件（按 seq 升序）。
        triggers (`Sequence[str]`): 启用的信号名。
        over_iterations (`int`): ``REPLY_END.iterations`` 的告警阈值。

    Returns:
        `tuple[list[str], list[str], int]`: ``(命中信号, 证据行, 收尾回合数)``。
    """
    enabled = set(triggers)
    reasons: list[str] = []
    evidence: list[str] = []
    n_replies = 0

    for event in events:
        payload = event.record.payload
        kind = event.kind.value

        if kind == "tool_result" and "tool_error" in enabled:
            # payload 约定见 harness_kit/events/types.py 的 PAYLOAD_FIELDS：
            # TOOL_RESULT 带 ("call_id", "state", "chars", "error")。
            state = str(payload.get("state", "")).lower()
            if state and state != "success":
                if "tool_error" not in reasons:
                    reasons.append("tool_error")
                evidence.append(
                    f"seq={event.seq} TOOL_RESULT(state={state}) "
                    f"{str(payload.get('error') or '')[:80]}",
                )

        elif kind == "permission" and "permission_denied" in enabled:
            behavior = str(payload.get("behavior", "")).lower()
            if behavior in {"deny", "denied"}:
                if "permission_denied" not in reasons:
                    reasons.append("permission_denied")
                evidence.append(
                    f"seq={event.seq} PERMISSION(behavior={behavior}) "
                    f"tool={payload.get('tool_name')} reason={str(payload.get('reason') or '')[:60]}",
                )

        elif kind == "reply_end":
            n_replies += 1
            if "over_iterations" in enabled:
                iterations = int(payload.get("iterations") or 0)
                if iterations >= over_iterations:
                    if "over_iterations" not in reasons:
                        reasons.append("over_iterations")
                    evidence.append(
                        f"seq={event.seq} REPLY_END(iterations={iterations} "
                        f">= {over_iterations})",
                    )

        elif kind == "custom" and "user_flagged" in enabled:
            # 业务侧写进来的显式差评。约定：name="user_feedback"，
            # data.rating 为负数或 data.verdict 取 "bad"/"down"。
            if str(payload.get("name", "")) != "user_feedback":
                continue
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                continue
            rating = data.get("rating")
            verdict = str(data.get("verdict", "")).lower()
            bad = (isinstance(rating, (int, float)) and rating < 0) or verdict in {"bad", "down"}
            if bad:
                if "user_flagged" not in reasons:
                    reasons.append("user_flagged")
                evidence.append(
                    f"seq={event.seq} CUSTOM(user_feedback rating={rating} verdict={verdict})",
                )

    return reasons, evidence, n_replies


async def scan_sessions(
    store: "SessionStoreBase",
    *,
    triggers: Sequence[str] = DEFAULT_TRIGGERS,
    over_iterations: int = DEFAULT_OVER_ITERATIONS,
    min_replies: int = 1,
    max_sessions: int | None = None,
    max_evidence: int = 3,
) -> list[SessionPick]:
    """扫描会话存储，挑出"出过事"的会话。

    **为什么要 ``min_replies``**：一个只有 ``SESSION_START`` 就崩掉的会话
    （进程被 kill）同样值得关注，但它连一条 ``REPLY_END`` 都没有，
    合不出可用的评测用例 —— 它属于"可用性告警"，不属于"回归用例"。
    所以默认 ``min_replies=1``；要连崩溃会话一起捞就把它设成 0。

    Args:
        store (`SessionStoreBase`): 会话存储（第 09 讲的抽象，任何后端都行）。
        triggers (`Sequence[str]`): 启用的失败信号；传空序列则只按
            ``min_replies`` 过滤（"把全部会话都变成回归集"）。
        over_iterations (`int`): ``REPLY_END.iterations`` 阈值。
        min_replies (`int`): 至少要有几个收尾回合。
        max_sessions (`int | None`): 最多返回几个（``None`` 不限）。按命中信号
            数量降序、会话 id 升序稳定排序后截断 —— 不依赖 ``list_sessions``
            的返回顺序。
        max_evidence (`int`): 每个会话最多保留几条证据行。

    Returns:
        `list[SessionPick]`: 命中的会话，按"信号多的在前"排序。

    Raises:
        ValueError: ``over_iterations`` 或 ``min_replies`` 为负，``max_sessions`` 非正。
    """
    if over_iterations < 0:
        raise ValueError(f"over_iterations 不能为负，收到 {over_iterations}")
    if min_replies < 0:
        raise ValueError(f"min_replies 不能为负，收到 {min_replies}")
    if max_sessions is not None and max_sessions <= 0:
        raise ValueError(f"max_sessions 必须为正或 None，收到 {max_sessions}")

    unknown = [name for name in triggers if name not in FAILURE_TRIGGERS]
    if unknown:
        logger.bind(unknown=unknown).warning("scan_sessions 收到未知的失败信号，将被忽略")

    picks: list[SessionPick] = []
    for meta in await store.list_sessions():
        events = await store.read(meta.session_id)
        reasons, evidence, n_replies = _classify(
            events,
            triggers=triggers,
            over_iterations=over_iterations,
        )
        if n_replies < min_replies:
            continue
        if triggers and not reasons:
            continue
        picks.append(
            SessionPick(
                session_id=meta.session_id,
                reasons=reasons,
                evidence=evidence[:max_evidence],
                n_events=len(events),
                n_replies=n_replies,
            ),
        )

    picks.sort(key=lambda item: (-len(item.reasons), item.session_id))
    if max_sessions is not None:
        picks = picks[:max_sessions]

    logger.bind(
        scanned=len(picks),
        selected=len(picks),
        triggers=list(triggers),
    ).info("反馈闭环：会话扫描完成")
    return picks


# ----------------------------------------------------------------------
# 第二跳：出过事的会话 → 回归评测集
# ----------------------------------------------------------------------


def _normalise_input(text: str) -> str:
    """把输入归一化成一个去重键（大小写、空白、尾部标点都不算差异）。

    Args:
        text (`str`): 原始输入。

    Returns:
        `str`: 归一化后的字符串。
    """
    return " ".join(text.lower().split()).strip("。.!?！？ \t\n")


def _case_fingerprint(case: EvalCase) -> str:
    """用例指纹：``sha1(归一化输入)[:12]``。

    用法是"同一条问题只留一条用例"。**刻意不把 ``expected`` 算进去**：
    如果两次线上事故是同一句提问、模型答得不一样，我们想要的是**一条**用例
    外加"它出过两次事"这个事实，而不是两条内容冲突的用例。

    Args:
        case (`EvalCase`): 用例。

    Returns:
        `str`: 12 位十六进制指纹。
    """
    key = _normalise_input(case.input)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


async def build_regression_dataset(
    store: "SessionStoreBase",
    picks: Sequence[SessionPick],
    *,
    name: str = "regression-from-production",
    tags: Sequence[str] = ("regression", "from-production"),
    max_cases: int | None = None,
    require_closed: bool = True,
    skip_truncated: bool = False,
) -> tuple[EvalDataset, dict[str, str]]:
    """把选中的会话逐条转成回归用例，去重后合成一个评测集。

    **溯源是这一跳的重点**：每条用例的 ``metadata`` 里会写上
    ``origin_session`` / ``origin_reasons`` / ``origin_fingerprint``，
    于是"这条用例当初为什么进来"永远查得到。这正是
    :func:`append_ledger` 后半段用来对账的键。

    Args:
        store (`SessionStoreBase`): 会话存储。
        picks (`Sequence[SessionPick]`): :func:`scan_sessions` 的产物。
        name (`str`): 数据集名。
        tags (`Sequence[str]`): 数据集级标签；每条用例也会带上同样的标签。
        max_cases (`int | None`): 最多保留多少条用例（``None`` 不限）。
        require_closed (`bool`): 只收有 ``REPLY_END`` 的回合（透传给
            :func:`~harness_kit.eval.synthesize.synthesize_from_session`）。
        skip_truncated (`bool`): 是否丢掉输入被截断的回合。

    Returns:
        `tuple[EvalDataset, dict[str, str]]`: ``(数据集, 指纹 → 会话 id)``。
        第二个元素是**去重账本**：最终的用例只保留了第一次命中的那次会话，
        但"另一个会话也出过同一句提问"这件事必须留下来，
        否则台账会撒谎说"这次线上事故没进回归集"。
    """
    dedup_owner: dict[str, str] = {}
    also_seen: dict[str, list[str]] = {}
    cases: list[EvalCase] = []

    for pick in picks:
        try:
            dataset = await synthesize_from_session(
                store,
                pick.session_id,
                name=f"session-{pick.session_id}",
                tags=list(tags),
                require_closed=require_closed,
                skip_truncated=skip_truncated,
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.bind(session_id=pick.session_id, error=str(error)).warning(
                "反馈闭环：该会话合成失败，跳过（不影响其它会话）",
            )
            continue

        if len(dataset) == 0:
            logger.bind(session_id=pick.session_id).debug(
                "反馈闭环：该会话没有合格回合，跳过",
            )
            continue

        for case in dataset.cases:
            fingerprint = _case_fingerprint(case)
            owner = dedup_owner.get(fingerprint)
            if owner is not None:
                # 同一句提问已经在别的会话里进过集了：记账，不重复建用例。
                also_seen.setdefault(fingerprint, []).append(pick.session_id)
                continue
            dedup_owner[fingerprint] = pick.session_id
            case.metadata = {
                **case.metadata,
                "origin_session": pick.session_id,
                "origin_reasons": list(pick.reasons),
                "origin_fingerprint": fingerprint,
            }
            if "regression" not in case.tags:
                case.tags = [*case.tags, "regression"]
            cases.append(case)

    # 同一句提问的"别的会话也出过事"合并进 owner 用例的 metadata。
    for case in cases:
        fingerprint = str(case.metadata.get("origin_fingerprint", ""))
        others = also_seen.get(fingerprint)
        if others:
            case.metadata["also_seen_in"] = others

    if max_cases is not None:
        if max_cases <= 0:
            raise ValueError(f"max_cases 必须为正或 None，收到 {max_cases}")
        cases = cases[:max_cases]

    merged = EvalDataset.from_cases(
        cases,
        name=name,
        tags=list(tags),
        metadata={
            "source": "session-events",
            "n_sessions": len(picks),
            "n_cases": len(cases),
            "deduped": sum(len(v) for v in also_seen.values()),
        },
    )

    logger.bind(
        dataset=merged.name,
        n_cases=len(merged),
        n_sessions=len(picks),
        deduped=sum(len(v) for v in also_seen.values()),
    ).info("反馈闭环：回归评测集构建完成")
    return merged, dedup_owner


# ----------------------------------------------------------------------
# 第三跳（判定）：新报告 vs 基线报告
# ----------------------------------------------------------------------


class GateDecision(BaseModel):
    """回归闸门的判决。"""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    """是否放行。"""

    baseline: str = "compare"
    """基线来源：``"compare"`` 表示与基线比过，``"none"`` 表示第一次跑。

    没有基线时一律放行，所以这个字段是"**这次判决的含金量**"的唯一标记 ——
    它把"没有基线也算过"落进**机器可读的判决**里，而不只是一条日志：
    下游（台账、CI 门禁、发布脚本）可以据此拒绝把一次
    ``baseline="none"`` 的放行当成"验证通过"。
    """

    reasons: list[str] = Field(default_factory=list)
    """拦截理由（``allowed=True`` 时为空）。"""

    deltas: dict[str, float] = Field(default_factory=dict)
    """指标名 → ``新值 - 基线值``；只在两边都算得出该指标时出现。"""

    pass_rate_delta: float = 0.0
    """通过率的变化量。"""

    def to_line(self) -> str:
        """一行摘要。

        Returns:
            `str`: ``"ALLOW pass_rate +0.0000 · 无拦截"`` 形态。
        """
        head = "ALLOW" if self.allowed else "BLOCK"
        tail = "无拦截" if self.allowed else "; ".join(self.reasons)
        return f"{head} pass_rate {self.pass_rate_delta:+.4f} · {tail}"


class RegressionGate(BaseModel):
    """把"能不能发"变成一条可复现的判决。

    **它不是"分数高就放行"**。它只回答一个问题：*这一版相对基线，
    有没有退步？* 所以：

    - 没有基线（第一次跑）时**默认放行**，但会把判决的 ``baseline``
      标成 ``"none"`` —— "没有基线也算过"这件事必须显式可见，
      否则第一次评测就会给人一种"已经验证过了"的错觉；
    - ``min_pass_rate`` 是**绝对下限**，用来拦"基线本来就烂、
      这一版更烂但没跌破相对阈值"的情形；
    - 指标缺失（一边有、一边没有）**不算退步**。指标集合是会长大的，
      新增一个指标不该把上一版的成绩判成退步。
    """

    model_config = ConfigDict(extra="forbid")

    max_metric_drop: float = Field(default=DEFAULT_MAX_METRIC_DROP, ge=0)
    """单指标允许的最大绝对跌幅。"""

    min_pass_rate: float = Field(default=0.0, ge=0, le=1)
    """通过率的绝对下限（``0`` 表示不设）。"""

    ignore_metrics: list[str] = Field(default_factory=list)
    """豁免的指标名（例如波动本来就大的 LLM-as-judge 分）。"""

    def evaluate(
        self,
        current: EvalReport,
        baseline: EvalReport | None = None,
    ) -> GateDecision:
        """出判决。

        Args:
            current (`EvalReport`): 本次评测报告。
            baseline (`EvalReport | None`): 基线报告；``None`` 表示第一次。

        Returns:
            `GateDecision`: 判决。
        """
        reasons: list[str] = []
        deltas: dict[str, float] = {}

        if baseline is None:
            logger.bind(dataset=current.dataset).warning(
                "回归闸门：没有基线报告，本次一律放行（判决里会记下这件事）",
            )
            # 没有基线时仍然检查绝对下限：这是唯一一条不依赖基线的规则。
            if self.min_pass_rate > 0 and current.pass_rate < self.min_pass_rate:
                return GateDecision(
                    allowed=False,
                    baseline="none",
                    reasons=[
                        f"无基线，但通过率 {current.pass_rate:.4f} "
                        f"低于绝对下限 {self.min_pass_rate:.4f}",
                    ],
                    pass_rate_delta=0.0,
                )
            return GateDecision(allowed=True, baseline="none", reasons=[], pass_rate_delta=0.0)

        pass_rate_delta = current.pass_rate - baseline.pass_rate
        if pass_rate_delta < -self.max_metric_drop:
            reasons.append(
                f"通过率退步 {-pass_rate_delta:.4f}（> {self.max_metric_drop:.4f}）："
                f"{baseline.pass_rate:.4f} → {current.pass_rate:.4f}",
            )

        if self.min_pass_rate > 0 and current.pass_rate < self.min_pass_rate:
            reasons.append(
                f"通过率 {current.pass_rate:.4f} 低于绝对下限 {self.min_pass_rate:.4f}",
            )

        # 指标对比走 EvalReport.summary()（真实 API，见
        # harness_kit/eval/report.py:178），但只取 metric_names() 里那几项：
        # summary() 还带 latency_p50_ms / tokens_total / cost_usd 这些
        # **运行开销**指标，把"这一版慢了 3ms"也算成回归会让闸门天天误报。
        after = current.summary()
        before = baseline.summary()
        for key in baseline.metric_names():
            if key not in after or key in self.ignore_metrics:
                continue
            delta = after[key] - before[key]
            deltas[key] = delta
            if delta < -self.max_metric_drop:
                reasons.append(
                    f"指标 {key} 退步 {-delta:.4f}（> {self.max_metric_drop:.4f}）："
                    f"{before[key]:.4f} → {after[key]:.4f}",
                )

        decision = GateDecision(
            allowed=not reasons,
            reasons=reasons,
            deltas=deltas,
            pass_rate_delta=pass_rate_delta,
        )
        logger.bind(
            dataset=current.dataset,
            allowed=decision.allowed,
            n_reasons=len(reasons),
        ).info("反馈闭环：回归闸门判决 {}", decision.to_line())
        return decision


# ----------------------------------------------------------------------
# 第四跳：台账（闭环的字面含义）
# ----------------------------------------------------------------------


class FeedbackLedgerEntry(BaseModel):
    """一行台账：一次"线上事故 → 回归用例 → 判决"的完整记录。"""

    model_config = ConfigDict(extra="forbid")

    ts: datetime = Field(default_factory=utc_now)
    """写入时间（UTC, tz-aware）。"""

    session_id: str
    """来源会话 id。"""

    fingerprint: str
    """用例指纹（:func:`_case_fingerprint`）。"""

    case_id: str
    """生成的用例 id。"""

    reasons: list[str] = Field(default_factory=list)
    """该会话命中的失败信号。"""

    dataset: str = ""
    """归属的回归集名。"""

    allowed: bool | None = None
    """闸门判决；``None`` 表示**尚未判定**（只做了挖掘，没跑回归）。

    这个字段刻意允许 ``None``：``harness-kit feedback`` 可以在没有基线、
    也不跑评测的情况下先把用例挖出来入库，此时把 ``allowed`` 硬写成
    ``False`` 会让台账撒谎说"这一版被拦了"。未判定就是未判定。
    """

    gate_reasons: list[str] = Field(default_factory=list)
    """闸门给出的拦截理由；未判定时为空。"""

    gate_baseline: str = ""
    """判决的含金量：``"compare"`` / ``"none"``；未判定时为空串。

    和 :attr:`allowed` 一样，这个字段是为了让台账**不许含糊**：
    ``allowed=True`` 配 ``gate_baseline="none"`` 的意思是"放行了，
    但没有基线可比"，与"比过基线、确实没退步"是两件事。
    """


def append_ledger(
    path: str | Path,
    entries: Iterable[FeedbackLedgerEntry],
) -> int:
    """把台账**追加**写进 JSONL（一行一条，永不重写）。

    Args:
        path (`str | Path`): 台账文件路径；父目录会自动创建。
        entries (`Iterable[FeedbackLedgerEntry]`): 台账条目。

    Returns:
        `int`: 实际写入的条数。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(entries)
    with target.open("a", encoding="utf-8") as handle:
        for entry in rows:
            handle.write(entry.model_dump_json())
            handle.write("\n")
    logger.bind(path=str(target), written=len(rows)).info("反馈闭环：台账已追加")
    return len(rows)


def read_ledger(path: str | Path) -> list[FeedbackLedgerEntry]:
    """读回台账（只读，不修改）。

    Args:
        path (`str | Path`): 台账文件路径。

    Returns:
        `list[FeedbackLedgerEntry]`: 台账条目；文件不存在时返回空列表。

    Raises:
        `ValueError`: 某一行无法解析（说明台账被外部改坏了，必须显式暴露）。
    """
    target = Path(path)
    if not target.exists():
        return []
    entries: list[FeedbackLedgerEntry] = []
    for lineno, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(FeedbackLedgerEntry.model_validate_json(stripped))
        except ValueError as error:
            raise ValueError(f"{target}:{lineno} 台账行无法解析: {error}") from error
    return entries


def entries_from_decision(
    picks: Sequence[SessionPick],
    owner: dict[str, str],
    dataset: EvalDataset,
    decision: GateDecision | None = None,
) -> list[FeedbackLedgerEntry]:
    """把扫描结果 + 判决折成台账条目。

    Args:
        picks (`Sequence[SessionPick]`): 扫描结果（**全部**被选中的会话，
            不只是进了用例的那些 —— 没进用例的必须留痕，否则台账会漏事）。
        owner (`dict[str, str]`): ``指纹 → 会话 id``（:func:`build_regression_dataset`
            的第二个返回值）。
        dataset (`EvalDataset`): 最终的回归集。
        decision (`GateDecision | None`): 闸门判决；``None`` 表示只挖掘、
            未判定（台账里 ``allowed`` 会是 ``None``）。

    Returns:
        `list[FeedbackLedgerEntry]`: 台账条目，每个会话一条。

    Notes:
        ``owner`` 用来回答"这个会话的用例是不是被别人抢了"：当一个会话的
        输入指纹已经归别的会话时，它不会生成用例，台账里 ``case_id`` 为空，
        但 ``session_id`` 与 ``reasons`` 仍然留痕。
    """
    by_session: dict[str, EvalCase] = {}
    for case in dataset.cases:
        session_id = str(case.metadata.get("origin_session", ""))
        if session_id:
            by_session[session_id] = case

    entries: list[FeedbackLedgerEntry] = []
    for pick in picks:
        case = by_session.get(pick.session_id)
        entries.append(
            FeedbackLedgerEntry(
                session_id=pick.session_id,
                fingerprint=(
                    str(case.metadata.get("origin_fingerprint", "")) if case else ""
                ),
                case_id=case.id if case else "",
                reasons=list(pick.reasons),
                dataset=dataset.name,
                allowed=decision.allowed if decision is not None else None,
                gate_reasons=list(decision.reasons) if decision is not None else [],
                gate_baseline=decision.baseline if decision is not None else "",
            ),
        )
    return entries
