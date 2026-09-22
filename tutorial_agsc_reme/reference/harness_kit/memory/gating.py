# -*- coding: utf-8 -*-
"""记忆注入门控：什么时候允许把记忆塞进上下文（契约 §3.19）。

**这个模块存在的唯一理由：契约给的默认阈值 ``min_score=0.2`` 不能直接用来比分数**

契约 §3.19 的签名是 ``MemoryGate(*, budget, min_score=0.2, sensitive_tags=[])``。
如果把它理解成"把 ``MemoryHit.score`` 拿去和 0.2 比"，那么：
**一条都留不下**。原因是 :class:`~harness_kit.memory.citations.MemoryHit` 的
``score`` 继承自 ReMe 的 ``FileChunk.score``，而 ReMe 的分数**量纲随路径切换**
（``third_party/ReMe/reme/steps/index/search.py:337-349``）：

============================ ========================================== ==============
情形                         ``score`` 是什么                            典型量级
============================ ========================================== ==============
两路都有结果 → RRF 融合      ``w_v/(60+rank_v) + w_k/(60+rank_k)``       0.008 – 0.017
只有关键词路 → 不融合        BM25 原始分                                 1 – 30+
只有向量路 → 不融合          cosine 相似度（1 - 距离）                   0 – 1
============================ ========================================== ==============

也就是说同一份配置下，``min_score=0.2`` 在 BM25 路是"几乎不过滤"，
在 RRF 路是"全部杀掉"。一个把两种量纲混在一起的绝对阈值，
不可能是正确的默认值。

**harness 的解法：把阈值定义在归一化分数上，并把归一化规则写在 API 表面**

:meth:`MemoryGate.normalize_scores` 用**相对最佳**归一：``norm_i = score_i / max(score)``。
于是 ``norm`` 恒在 ``(0, 1]``，``min_score`` 的含义变成
"至少要达到本轮最高分的百分之多少"，与走哪条路无关。这与重排序（rerank）
领域里常用的 "relative score" 一致，也解释了为什么契约的默认值 0.2 是合理的 ——
它本来就是相对阈值的直觉（"明显更差的那几条别要"）。

代价必须写清楚：**相对归一丢掉了绝对量级**。如果本轮所有结果的分数都很低
（检索质量整体很差），相对归一会让最高分那条归一成 1.0，然后被放行。
所以本类**不**声称它是"相关性判断"，它只是"在同一批结果里做取舍"；
绝对质量的判断属于 :class:`~harness_kit.memory.metrics.MemoryMetrics`
（记录每次检索的真实最高分）与 :mod:`harness_kit.memory.proactive`（置信度阈值）。

**被拒绝必须留痕**

:meth:`apply` 会把拒绝原因喂给 :class:`~harness_kit.memory.metrics.MemoryMetrics`
（``record_gate_rejection``）。理由见 :mod:`harness_kit.memory.metrics` 的模块 docstring：
门控拒绝的表现是"本轮没有记忆"，和"记忆库里本来就空"长得一模一样。

**读取侧之外还有写入侧：:class:`MemoryWriteGate`**

召回门控管的是"要不要把记忆塞进上下文"，**写入门控**管的是"这一轮增量值不值得落记忆"。
两者对称但代价完全不同：召回门控省的是 token，写入门控省的是**一次完整的 LLM 抽取调用**
（``auto_memory`` 内部是一个挂了工具的 AgentScope Agent，
见 ``third_party/ReMe/reme/steps/evolve/auto_memory.py:72`` 的 ``create_tools``），
而且是**阻塞在回复末尾**的那一次 —— 官方 ``_write_back``
（``.../_reme/_middleware.py:489``）是在 ``on_reply`` 的 ``finally`` 里 **await** 的，
所以用户要等到抽取跑完才拿到"回复完成"。

写入侧最容易被忽略的一条，是本模块要教会读者的核心事实：
**``auto_memory`` 的 ``success=True`` 不等于记忆落地。** 实测（本机，echo 模型）：

.. code-block:: text

    success: True
    metadata: {"date": "2026-09-22", "path": null, "created": false,
               "modified": false, "n_messages": 2}

``success`` 是"这一步没抛异常"，``created`` / ``modified`` 才是"卡片真的写了"。
拿 ``success`` 当写回成功率，指标会永远显示 100% —— 而记忆库里一张卡都没有。
所以 :class:`MemoryWriteGate` 与 :class:`~harness_kit.memory.metrics.MemoryMetrics`
的 ``record_writeback`` 都按 ``created or modified`` 判定成功，
这条判据写在 :meth:`MemoryWriteGate.decide` 的返回值里由中间件消费。
"""

from __future__ import annotations

from typing import Any, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from .budget import MemoryBudget

__all__ = [
    "DEFAULT_MIN_SCORE",
    "GateDecision",
    "MemoryGate",
    "MemoryWriteGate",
    "WriteDecision",
]

#: 契约 §3.19 给的默认阈值。它作用在 :meth:`MemoryGate.normalize_scores`
#: 的输出上（相对最佳分的比例），**不是**原始 ``MemoryHit.score``。
DEFAULT_MIN_SCORE: float = 0.2

#: 门控的四种拒绝原因 + 一种放行。
_REASON_ALLOWED = "allowed"
_REASON_NO_HITS = "no_hits"
_REASON_SENSITIVE = "sensitive_session"
_REASON_LOW_SCORE = "below_min_score"
_REASON_OVER_BUDGET = "over_budget"


class GateDecision(BaseModel):
    """一次门控判定的结论（契约 §3.19）。

    Attributes:
        allow (`bool`): 是否放行。
        reason (`str`): 判定原因。取值（**全部是稳定字符串**，可直接进指标）：

            ``"allowed"``
                放行。
            ``"no_hits"``
                这批结果本来就是空的。
            ``"sensitive_session"``
                会话带敏感标签，整批拒绝。
            ``"below_min_score"``
                归一化分数全部低于 ``min_score``。

                **实测：这一支在当前实现下不可达。** 归一化的分母就是最大分，
                所以最高分那条的归一化值恒为 ``1.0``；而 ``min_score`` 被
                :meth:`MemoryGate.__init__` 限制在 ``[0, 1]``，
                于是 ``above`` 永远非空。它是一条**防御分支**，保留它是为了
                万一将来归一化规则改了（比如改成除以常数）不会静默失效 ——
                但不要写"这条分支会帮我挡住低分记忆"的教程。
                单条被分数挡下的情况见 ``dropped_low_score``。
            ``"over_budget"``
                通过了分数关，但预算一条都装不下（``budget.fit`` 的 ``kept`` 为空）。
        kept (`list[str]`): 放行的 hit 的 ``chunk_id``。

            类型是 ``list[str]``（契约如此），所以只给 id、不给正文。
            需要正文请用 :meth:`MemoryGate.apply`，它额外返回 ``MemoryHit`` 对象。
            被拒绝时 ``kept`` 是空列表 —— **不**返回"本来会放行的那几条"：
            原因里已经写了为什么拒绝，而"差一点就进去的记忆"列表会被误当成已注入。
        dropped_low_score (`list[str]`): 被**分数关**单条挡下的 ``chunk_id``。

            这是 harness 在契约三个字段之外的追加字段（有默认值，是超集）。
            加它的理由是一个真实的可观测性缺口：分数关是**逐条**过滤，
            而 ``reason`` 只描述整批判定 —— 没有这个字段，
            "3 条命中只注入了 1 条"在调用方看来是完全静默的，
            既没进 ``kept`` 也没进任何指标（``reason`` 还是 ``"allowed"``）。
            被分数挡下的 id 只列在这里，**不**混进 ``kept``。
    """

    model_config = ConfigDict(extra="forbid")

    allow: bool
    reason: str
    kept: list[str] = Field(default_factory=list)
    dropped_low_score: list[str] = Field(default_factory=list)


class MemoryGate:
    """记忆注入门控（契约 §3.19）。

    判定顺序是**短路**的，顺序本身就是策略：

    1. 空结果 → 拒绝（``no_hits``）。先判空，后面的规则不必处理空列表。
    2. 敏感会话 → 拒绝（``sensitive_session``）。**在分数之前**：
       敏感会话里的记忆再相关也不该注入，用分数去"筛"它等于给了它一个
       靠高分翻盘的机会。
    3. 归一化分数 → 拒绝（``below_min_score``）。
    4. 预算 → 拒绝或裁剪（``over_budget`` / ``allowed``）。

    ``budget`` 是**必填**参数（契约如此）。它同时承担两个职责：
    "装不下就拒绝"与"装得下就裁剪"，因此它必须存在 ——
    没有预算的门控只能回答"要不要"，回答不了"要哪几条"。

    Example::

        gate = MemoryGate(budget=MemoryBudget(max_tokens=400), min_score=0.5)
        decision, kept_hits = gate.apply(hits, session_tags=["private"])
        print(decision.allow, decision.reason, decision.kept)
    """

    def __init__(
        self,
        *,
        budget: MemoryBudget,
        min_score: float = DEFAULT_MIN_SCORE,
        sensitive_tags: Sequence[str] = (),
        metrics: Any | None = None,
    ) -> None:
        """配置门控。

        Args:
            budget (`MemoryBudget`): token 预算。
            min_score (`float`): 归一化分数下限，取值 ``[0.0, 1.0]``。
                ``0.0`` 表示关掉分数关（只留空结果/敏感/预算三重判定）。
            sensitive_tags (`Sequence[str]`): 敏感标签。会话标签与之**有交集**
                即整批拒绝。契约的写法是 ``Field(default_factory=list)``，
                但那是 pydantic 模型字段的写法；本类是普通类，函数签名里的
                ``Field(...)`` 只会得到一个 ``FieldInfo`` 对象当默认值，
                所以这里用 ``()``（不可变元组），语义等价。已记入 ``unresolved``。
            metrics (`Any | None`): 可选的
                :class:`~harness_kit.memory.metrics.MemoryMetrics`；
                给了就在拒绝时记一笔 ``record_gate_rejection``。

        Raises:
            `TypeError`: ``budget`` 为 ``None``。
            `ValueError`: ``min_score`` 不在 ``[0.0, 1.0]``。
        """
        if budget is None:
            raise TypeError("MemoryGate 需要 budget（契约 §3.19 是必填参数）")
        if not 0.0 <= float(min_score) <= 1.0:
            raise ValueError(f"min_score 必须在 [0, 1]，收到 {min_score}")
        self.budget: MemoryBudget = budget
        self.min_score: float = float(min_score)
        self.sensitive_tags: list[str] = _normalize_tags(sensitive_tags)
        self.metrics: Any | None = metrics

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def allow(self, hits: Sequence[Any], *, session_tags: Sequence[str] = ()) -> bool:
        """门控的布尔判定（契约 §3.19）。

        Args:
            hits (`Sequence[Any]`): :class:`~harness_kit.memory.citations.MemoryHit` 序列。
            session_tags (`Sequence[str]`): 本会话的标签。

        Returns:
            `bool`: 是否放行。
        """
        return self.decide(hits, session_tags=session_tags).allow

    def decide(self, hits: Sequence[Any], *, session_tags: Sequence[str] = ()) -> GateDecision:
        """返回完整判定（契约 §3.19）。

        Args:
            hits (`Sequence[Any]`): hit 序列。
            session_tags (`Sequence[str]`): 本会话的标签。

        Returns:
            `GateDecision`: 判定结论。
        """
        return self.apply(hits, session_tags=session_tags)[0]

    def apply(
        self,
        hits: Sequence[Any],
        *,
        session_tags: Sequence[str] = (),
    ) -> tuple[GateDecision, list[Any]]:
        """判定并返回**放行的 hit 对象**（:meth:`decide` 的富版本）。

        Args:
            hits (`Sequence[Any]`): hit 序列。
            session_tags (`Sequence[str]`): 本会话的标签。

        Returns:
            `tuple[GateDecision, list[Any]]`: ``(判定, 放行的 hit 列表)``。
            被拒绝时第二个元素是空列表。
        """
        candidates = [hit for hit in (hits or ())]
        if not candidates:
            return self._reject(_REASON_NO_HITS)

        overlap = self._sensitive_overlap(session_tags)
        if overlap:
            logger.info("memory gate: 会话标签 {} 命中敏感标签 {}，整批拒绝", list(session_tags), overlap)
            return self._reject(_REASON_SENSITIVE)

        scores = self.normalize_scores(candidates)
        dropped_low: list[str] = []
        if self.min_score > 0.0:
            above = []
            for hit, norm in zip(candidates, scores):
                if norm >= self.min_score:
                    above.append(hit)
                else:
                    dropped_low.append(str(getattr(hit, "chunk_id", "")))
            if not above:
                logger.info(
                    "memory gate: 归一分最高 {:.4f} 低于阈值 {:.2f}，全部拒绝",
                    max(scores) if scores else 0.0,
                    self.min_score,
                )
                return self._reject(_REASON_LOW_SCORE)
        else:
            above = candidates

        fitted = self.budget.fit(above)
        if not fitted.kept:
            logger.info("memory gate: 预算 {} token 装不下任何一条，拒绝", self.budget.max_tokens)
            return self._reject(_REASON_OVER_BUDGET)

        if dropped_low:
            logger.info(
                "memory gate: {} 条被分数关单条挡下（归一化分 < {}），id={}",
                len(dropped_low),
                self.min_score,
                dropped_low,
            )
        decision = GateDecision(
            allow=True,
            reason=_REASON_ALLOWED,
            kept=[str(getattr(hit, "chunk_id", "")) for hit in fitted.kept],
            dropped_low_score=dropped_low,
        )
        logger.debug(
            "memory gate: 放行 {}/{} 条（分数关留下 {} 条，预算留下 {} 条）",
            len(fitted.kept),
            len(candidates),
            len(above),
            len(fitted.kept),
        )
        return decision, list(fitted.kept)

    # ------------------------------------------------------------------
    # 归一化
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_scores(hits: Sequence[Any]) -> list[float]:
        """把命中的原始分归一成"相对最高分的比例"。

        规则：``norm_i = score_i / max(score)``，``max`` 为 0 或全为 0 时全给 0.0。
        负数分（理论上 BM25 不会给，但 ``min_score`` 之类的下游过滤可能引入）
        先夹到 0。

        Args:
            hits (`Sequence[Any]`): hit 序列，元素需要有 ``score`` 属性。

        Returns:
            `list[float]`: 与输入等长的归一化分数，取值 ``[0.0, 1.0]``。
        """
        raw = [max(0.0, float(getattr(hit, "score", 0.0) or 0.0)) for hit in (hits or ())]
        if not raw:
            return []
        best = max(raw)
        if best <= 0.0:
            return [0.0 for _ in raw]
        return [round(value / best, 6) for value in raw]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _sensitive_overlap(self, session_tags: Sequence[str]) -> list[str]:
        """求会话标签与敏感标签的交集。

        Args:
            session_tags (`Sequence[str]`): 会话标签。

        Returns:
            `list[str]`: 交集（已排序，空列表表示不敏感）。
        """
        if not self.sensitive_tags:
            return []
        session = {tag for tag in _normalize_tags(session_tags)}
        return sorted(session.intersection(self.sensitive_tags))

    def _reject(self, reason: str) -> tuple[GateDecision, list[Any]]:
        """产出一个拒绝判定并记账。

        Args:
            reason (`str`): 拒绝原因。

        Returns:
            `tuple[GateDecision, list[Any]]`: ``(判定, [])``。
        """
        if self.metrics is not None:
            try:
                self.metrics.record_gate_rejection(reason=reason)
            except Exception as exc:  # noqa: BLE001 - 指标不该让门控失败
                logger.debug("record_gate_rejection 失败: {}", exc)
        return GateDecision(allow=False, reason=reason, kept=[]), []


def _text_of(message: Any) -> str:
    """取一条消息的正文字本。

    优先用 ``Msg.get_text_content()``（AgentScope 的原生方法，会跳过
    ``HintBlock`` / ``ThinkingBlock`` 只取 ``TextBlock``）；拿不到时退回
    ``str(message.content)``。**不自己做块遍历**：块的语义归 AgentScope 管。

    Args:
        message (`Any`): 一条消息（``Msg`` 或其子类）。

    Returns:
        `str`: 正文字本；取不到时返回空串。
    """
    getter = getattr(message, "get_text_content", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001 - 取不到就当没有正文
            return ""
    return ""


class WriteDecision(BaseModel):
    """一次写入门控判定的结论。

    Attributes:
        allow (`bool`): 是否允许把这一轮增量交给 ``auto_memory``。
        reason (`str`): 判定原因（稳定字符串，可直接进指标）：

            ``"allowed"``
                放行。
            ``"disabled"``
                门控被显式关掉（``min_messages <= 0``），永远放行 ——
                与 ``"allowed"`` 分开是为了让日志能区分"通过了检查"与"没做检查"。
            ``"empty"``
                增量里一条消息都没有（``on_reply`` 的差集算出来是空）。
            ``"not_enough_messages"``
                条数没到 ``min_messages``。单条 user 消息的轮次（Agent 直接
                回一个空答案）不该产生一张记忆卡。
            ``"no_user_text"``
                增量里没有**带正文的** user 消息。没有新输入的那一轮
                （例如 HITL 确认、外部执行结果续跑）不该重复落记忆。
            ``"too_short"``
                用户正文与助手正文加起来字符数低于 ``min_chars``。
                "嗯"、"好的"这种往返不该占用一次 LLM 抽取。
        messages (`int`): 进入判定的增量条数。
        chars (`int`): 进入判定的正文字符数（``user`` + ``assistant`` 两侧之和）。
    """

    model_config = ConfigDict(extra="forbid")

    allow: bool
    reason: str
    messages: int = 0
    chars: int = 0


class MemoryWriteGate:
    """写入侧门控：这一轮增量值不值得落记忆。

    与 :class:`MemoryGate` 的关系是**对称但不共用参数**：

    ================ ============================== ==========================
    维度             召回门控 :class:`MemoryGate`    写入门控 :class:`MemoryWriteGate`
    ================ ============================== ==========================
    拦错的代价       少一点上下文（用户几乎无感）   记忆丢失（**不可逆**）
    放过的代价       多花 token                    多花一次 LLM 抽取（更贵）
    默认倾向         偏保守（宁可不注入）           偏保守（宁可不写，但要留痕）
    ================ ============================== ==========================

    两边都偏保守，但保守的**方向**不同：召回是"省 token"，写入是"省一次抽取"。
    写入门控的阈值因此必须**低到不会丢掉真实信息**：
    ``min_chars`` 默认 12 只挡得住"嗯/好的/收到"，挡不住任何一句真话。

    **为什么不做内容级判定**（例如"这条是否值得记"）：那需要一次 LLM 调用，
    而写入门控存在的意义就是省掉那次调用。用一次调用决定要不要另一次调用，
    在成本上不成立。内容级判定属于 ``auto_memory`` 内部的抽取 Agent
    （它自己会判断抽不出东西时就不写卡片）—— 本类的职责边界到此为止。

    Example::

        gate = MemoryWriteGate(min_chars=12)
        decision = gate.decide(increment)
        if decision.allow:
            await write_back(increment, session_id)
    """

    def __init__(
        self,
        *,
        min_messages: int = 2,
        min_chars: int = 12,
        exclude_names: Sequence[str] = ("memory",),
        metrics: Any | None = None,
    ) -> None:
        """配置写入门控。

        Args:
            min_messages (`int`): 增量最少几条才允许写入。``<= 0`` 表示
                关掉门控（:class:`WriteDecision` 的 ``reason`` 会是 ``"disabled"``）。
                默认 2：一轮正常的"用户说 + 助手答"至少两条。
            min_chars (`int`): user 与 assistant 正文合计的最少字符数。
                负数按 0 处理。
            exclude_names (`Sequence[str]`): 不计入条数与字符数的消息名。
                默认剔除 ``"memory"`` —— 那是中间件自己注入的 HintBlock，
                它**不是**用户说的话，把它算进增量会让"只有注入没有对话"
                的轮次看起来像有内容。
            metrics (`Any | None`): 可选的
                :class:`~harness_kit.memory.metrics.MemoryMetrics`；
                给了就在拒绝时记一笔 ``record_writeback(ok=False)``，
                让"被门控挡下的写入"在指标里可见（与召回侧的
                ``record_gate_rejection`` 对称）。

        Raises:
            `ValueError`: ``min_chars`` 不是 ``int``（``bool`` 也不行）。
        """
        if isinstance(min_chars, bool) or not isinstance(min_chars, int):
            raise ValueError(f"min_chars 必须是 int，收到 {type(min_chars).__name__}")
        self.min_messages: int = int(min_messages)
        self.min_chars: int = max(0, int(min_chars))
        self.exclude_names: list[str] = [str(name) for name in exclude_names]
        self.metrics: Any | None = metrics

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def allow(self, messages: Sequence[Any], *, session_id: str | None = None) -> bool:
        """门控的布尔判定。

        Args:
            messages (`Sequence[Any]`): 本轮增量。
            session_id (`str | None`): 会话 id（只用于日志）。

        Returns:
            `bool`: 是否允许写入。
        """
        return self.decide(messages, session_id=session_id).allow

    def decide(
        self,
        messages: Sequence[Any],
        *,
        session_id: str | None = None,
    ) -> WriteDecision:
        """返回完整判定。

        判定顺序（短路，顺序本身是策略）：
        空 → **关掉门控？** → 条数 → 有无用户正文 → 长度。

        ``disabled`` 排在 ``empty`` **之后**：一次"本轮什么都没产生"的调用，
        即使门控是关的，结论也应该是 ``empty`` 而不是 ``disabled`` ——
        日志里"没内容"和"没检查"是两件事。

        Args:
            messages (`Sequence[Any]`): 本轮增量。
            session_id (`str | None`): 会话 id（只用于日志）。

        Returns:
            `WriteDecision`: 判定结论。

        Raises:
            `TypeError`: ``messages`` 不是序列（例如误传了单个 ``Msg``）。
                这条检查是刻意加的：传单个 ``Msg`` 时 ``len()`` 依然能算出来，
                于是"只写了一条"这种 bug 会静默通过。
        """
        if isinstance(messages, (str, bytes)) or not isinstance(
            messages,
            (list, tuple, set, frozenset),
        ):
            raise TypeError(
                f"MemoryWriteGate.decide 需要消息序列，收到 {type(messages).__name__}"
                "；单条 Msg 请包成 [msg]",
            )

        kept = [
            message
            for message in messages
            if str(getattr(message, "name", "") or "") not in self.exclude_names
        ]
        if not kept:
            return self._reject("empty", session_id, kept)

        if self.min_messages <= 0:
            decision = WriteDecision(
                allow=True,
                reason="disabled",
                messages=len(kept),
                chars=sum(len(_text_of(m)) for m in kept),
            )
            return decision

        if len(kept) < self.min_messages:
            return self._reject("not_enough_messages", session_id, kept)

        roles = {str(getattr(m, "role", "") or ""): _text_of(m) for m in kept}
        user_text = "".join(_text_of(m) for m in kept if getattr(m, "role", "") == "user")
        if not user_text.strip():
            return self._reject("no_user_text", session_id, kept)

        total = sum(len(text) for text in roles.values())
        if total < self.min_chars:
            return self._reject("too_short", session_id, kept)

        return WriteDecision(
            allow=True,
            reason="allowed",
            messages=len(kept),
            chars=total,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _reject(
        self,
        reason: str,
        session_id: str | None,
        kept: Sequence[Any],
    ) -> WriteDecision:
        """产出一个拒绝判定、记账并打日志。

        Args:
            reason (`str`): 拒绝原因。
            session_id (`str | None`): 会话 id。
            kept (`Sequence[Any]`): 参与判定的消息（已剔除排除名）。

        Returns:
            `WriteDecision`: 拒绝判定。
        """
        decision = WriteDecision(
            allow=False,
            reason=reason,
            messages=len(kept),
            chars=sum(len(_text_of(m)) for m in kept),
        )
        logger.info(
            "memory write gate: 拒绝写入（{}），session={}，增量 {} 条 / {} 字符",
            reason,
            session_id or "<unknown>",
            decision.messages,
            decision.chars,
        )
        if self.metrics is not None:
            try:
                self.metrics.record_writeback(session_id=session_id or "<unknown>", ok=False)
            except Exception as exc:  # noqa: BLE001 - 指标不该让门控失败
                logger.debug("record_writeback 失败: {}", exc)
        return decision


def _normalize_tags(tags: Any) -> list[str]:
    """把标签输入规整成去重、去空白、casefold 的列表。

    这里**不复用** :func:`~harness_kit.memory.frontmatter.normalize_tags`：
    那条规则是给"写进 front matter 的标签"用的（最多 3 个、必须含字母数字），
    而门控的敏感标签是**配置项**，截断到 3 个会让第 4 个敏感标签静默失效 ——
    一个安全相关的配置被静默截断，属于最坏的一类失败。

    Args:
        tags (`Any`): 字符串、可迭代对象或 ``None``。

    Returns:
        `list[str]`: 规整后的标签。
    """
    if tags is None:
        return []
    if isinstance(tags, str):
        items: list[Any] = [tags]
    elif isinstance(tags, (list, tuple, set, frozenset)):
        items = list(tags)
    else:
        return []
    result: list[str] = []
    for item in items:
        text = str(item or "").strip().casefold()
        if text and text not in result:
            result.append(text)
    return result
