# -*- coding: utf-8 -*-
"""检索结果的 **token 预算裁剪** —— 补齐官方中间件的缺口 4（契约 §3.17）。

**缺口是什么，先把它钉死**

AgentScope 2.0.8 自带的 ReMe 长期记忆中间件
（``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_middleware.py``）
在检索完之后**不做任何裁剪**。侦察报告
``tutorial_agsc_reme/_recon/15_integration_agentscope_reme.md`` 对这一点的结论是
"对 ``_longterm_memory/_reme/`` 全文检索 ``truncat`` / ``budget`` 均无命中"，
而 ``_utils.py:51-103`` 的 ``_extract_memory_texts`` 就是把
``metadata["results"]`` 里的 ``text`` 一条不落地抽出来
（``_utils.py:94-103``，原文照抄）：

.. code-block:: python

    out: list[str] = []
    for item in results:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            text = (
                item.get("text") or item.get("memory") or item.get("content")
            )
            if text:
                out.append(str(text))
    return out

—— 有多少塞多少，既不裁剪也不带来源。而 ReMe 的默认分块器 ``chunk_byte_size=10000``
（``third_party/ReMe/reme/components/file_chunker/default_file_chunker.py:25``），
``top_k=5`` 就是最多 **5 万字节** 的原文一次性进入 system prompt。
对话长了必然爆 context，而且爆的时候是在 API 调用那一侧报错，
排查起来会绕很远。

本模块就是补这个洞：**在注入之前，按 token 预算把检索结果裁到能装下**。

**关键设计：预算必须量的是"真正会被注入的那段文本"**

很多实现的写法是"估算每段 ``hit.text`` 的 token 数、累加到预算"，然后注入时
另外拼一个 header + 分隔符。这两处一旦不一致，预算就是假的。
本模块的 :meth:`MemoryBudget.fit` 计量的对象，就是
:meth:`MemoryBudget.render` 产出的**完整注入文本**的前缀 ——
计量与注入共用同一个渲染函数，因此预算**精确**而非近似。

**估 token 器为什么不引第三方库**

``TokenEstimator`` 是 ``Callable[[str], int]``，真实 tokenizer 可以从外面注入
（例如换成 ``transformers`` 的 ``deepseek`` tokenizer）。
默认实现 :func:`estimate_tokens_heuristic` 只做**分字符类别**的启发式：

- CJK 字符（含中文标点）按 **1 字符 ≈ 1 token** —— 这是刻意取**偏大**的一侧，
  因为预算宁可保守也不要超；
- 其余字符按 **4 字符 ≈ 1 token**（GPT/DeepSeek 系 BPE 对英文的常见比例）；
- 每段固定加一点结构开销。

对 DeepSeek 的中文文本，这个估计通常略高于真实值（保守方向正确）；
教程里的用法是"把它当预算的**上界**"，而不是当精确 token 数。要精确就注入真 tokenizer。

**被丢掉的 hit 不能默默消失**

:class:`MemoryBudgetResult` 里 ``dropped`` 是显式字段。原因和契约里
``ProactiveReader`` 那条已知坑同源：任何"过滤导致看起来什么都没发生"的路径，
都必须留下可观测的痕迹，否则线上只能看到"记忆好像没生效"这种没法查的现象。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "MemoryBudget",
    "MemoryBudgetResult",
    "TokenEstimator",
    "estimate_tokens_heuristic",
    "render_memory_block",
]

#: 契约 §3.17 给的默认预算；与 ``MemorySpec.inject_budget_tokens`` 的默认值一致
#: （``tutorial_agsc_reme/reference/harness_kit/config/schema.py:359``）。
DEFAULT_MAX_TOKENS: int = 1200

#: 每段记忆渲染时的固定结构开销（``### path:1-20`` 这一行 + 空行）。
#: 数值是经验值，取整到 8 是为了让预算表好看；它只影响估计的保守程度。
_PER_HIT_OVERHEAD_TOKENS: int = 8

#: 整块渲染的头部与尾部固定开销。
_BLOCK_OVERHEAD_TOKENS: int = 12

#: 单个 CJK 字符的 token 估计（刻意取 1，偏保守）。
CJK_TOKENS_PER_CHAR: float = 1.0

#: 非 CJK 字符的 token 估计（4 字符 1 token）。
OTHER_CHARS_PER_TOKEN: int = 4


def _is_cjk(char: str) -> bool:
    """判断一个字符是否属于 CJK / 全角标点区（按 token 密度高的那一侧算）。

    Args:
        char (`str`): 单字符。

    Returns:
        `bool`: 是否按 "1 字符 1 token" 估计。
    """
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 基本区
        or 0xF900 <= code <= 0xFAFF  # 兼容表意
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角形式
        or 0x3040 <= code <= 0x30FF  # 日文假名（同属高密度）
    )


def estimate_tokens_heuristic(text: str) -> int:
    """默认的 token 估计器：按字符类别加权（契约 §3.17 的 ``TokenEstimator`` 默认实现）。

    刻意**不引第三方 tokenizer**：本仓库不允许新增未安装的依赖，而
    ``transformers`` 这类库的加载成本（几百 MB）远大于它带来的精度收益。
    需要精确计数的场景请自行注入 ``estimator``。

    Args:
        text (`str`): 待估文本。

    Returns:
        `int`: 估计 token 数；空串返回 ``0``。
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    # 非 CJK 部分按 4 字符 1 token 向上取整，保证不低估。
    return int(cjk * CJK_TOKENS_PER_CHAR) + -(-other // OTHER_CHARS_PER_TOKEN)


#: 契约 §3.17 的类型别名。
TokenEstimator = Callable[[str], int]


def render_memory_block(
    hits: Sequence[Any],
    *,
    header: str = "## 相关长期记忆",
    hint: str = "以下记忆来自长期记忆库，可能与本轮问题相关；与用户当前说法冲突时以用户为准。",
) -> str:
    """把若干 hit 渲染成"将要注入 system prompt 的那段文本"。

    **这是全流程唯一的渲染入口**：:meth:`MemoryBudget.fit` 计量它，
    :class:`~harness_kit.memory.middleware.LongTermMemoryMiddleware` 注入它。
    两处共用同一个函数，预算才不会与实际注入漂移。

    渲染形态（刻意保留 ``path:start-end``）：

    .. code-block:: text

        ## 相关长期记忆
        <hint>

        ### daily/2026-09-21/pref.md:3-6
        用户偏好深色主题的 matplotlib 图表。

        ### resource/handbook.md:1-12
        ...

    保留路径与行号的理由与 :mod:`harness_kit.memory.citations` 一致：
    模型被允许"引用来源"，而人能顺着路径去核对。

    Args:
        hits (`Sequence[Any]`): :class:`~harness_kit.memory.citations.MemoryHit` 序列。
        header (`str`): 块标题。
        hint (`str`): 使用说明（告诉模型这些内容该怎么用）。

    Returns:
        `str`: 完整的注入文本；``hits`` 为空时返回空串（**不**返回只有标题的块）。
    """
    usable = [hit for hit in hits if str(getattr(hit, "text", "") or "").strip()]
    if not usable:
        return ""

    lines: list[str] = [header, hint, ""]
    for hit in usable:
        path = str(getattr(hit, "path", "") or "")
        start = int(getattr(hit, "start_line", 0) or 0)
        end = int(getattr(hit, "end_line", 0) or 0)
        lines.append(f"### {path}:{start}-{end}")
        lines.append(str(getattr(hit, "text", "") or "").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class MemoryBudgetResult(BaseModel):
    """预算裁剪的结果（契约 §3.17）。

    ``kept`` / ``dropped`` 的划分是**保序**的：``kept`` 是输入的高分前缀，
    ``dropped`` 是剩下的后缀。不会出现"高分被丢、低分被留"。
    """

    model_config = ConfigDict(extra="forbid")

    kept: list[Any]
    """保留下来的 hit（按输入顺序）。"""

    dropped: list[Any]
    """因超出预算被丢弃的 hit（按输入顺序）。"""

    estimated_tokens: int
    """``kept`` 渲染成文本后的估计 token 数。"""

    truncated: bool
    """是否**因为预算**发生了裁剪。

    只有整块渲染不出内容的 hit（``text`` 为空或纯空白）被丢弃时**不**算截断：
    它们本来就不会被注入，把它计入 ``truncated`` 会让日志里的
    "预算不足" 变成假警报。真正的判定见 :meth:`MemoryBudget.fit`。
    """

    budget_tokens: int = DEFAULT_MAX_TOKENS
    """本次使用的预算上限（便于日志里一眼比对）。"""

    per_hit_tokens: list[int] = Field(default_factory=list)
    """每条 ``kept`` 的**增量** token 数（含 header 与分隔符），供教程展示"预算被谁吃掉了"。"""

    def render(self, **kwargs: Any) -> str:
        """把 ``kept`` 渲染成注入文本。

        Args:
            **kwargs: 透传给 :func:`render_memory_block`。

        Returns:
            `str`: 注入文本。
        """
        return render_memory_block(self.kept, **kwargs)


class MemoryBudget:
    """按 token 预算裁剪检索结果（契约 §3.17）。

    Example::

        budget = MemoryBudget(max_tokens=300)
        result = budget.fit(hits)
        print(result.estimated_tokens, len(result.kept), len(result.dropped))
        system_prompt += result.render()
    """

    def __init__(
        self,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        estimator: TokenEstimator | None = None,
    ) -> None:
        """配置预算。

        Args:
            max_tokens (`int`): 预算上限；``<= 0`` 视为"不允许注入任何记忆"。
            estimator (`TokenEstimator | None`): 自定义估计器；
                默认 :func:`estimate_tokens_heuristic`。

        Raises:
            `ValueError`: ``max_tokens`` 不是 ``int``（``bool`` 也不行 —— 写
                ``MemoryBudget(max_tokens=True)`` 显然是手滑）。
        """
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise ValueError(f"max_tokens 必须是 int，收到 {type(max_tokens).__name__}")
        self.max_tokens: int = max_tokens
        self.estimator: TokenEstimator = estimator or estimate_tokens_heuristic

    # ------------------------------------------------------------------
    # 契约方法
    # ------------------------------------------------------------------
    def fit(self, hits: Sequence[Any]) -> MemoryBudgetResult:
        """按预算裁剪（契约 §3.17）。

        算法：**贪心前缀**。逐条把 hit 加进渲染文本，重新度量整块的 token 数；
        一旦超预算就停止，剩下的全部进 ``dropped``。

        为什么是"重新度量整块"而不是"累加每条的 token"：
        因为渲染是有结构的（标题、空行、可能的尾部换行），
        逐条独立估算会产生累积误差；整块重测在 ``top_k`` 只有个位数的场景下
        开销可以忽略（估计器是纯字符串遍历，微秒级），但结果**精确**。

        Args:
            hits (`Sequence[Any]`): ``MemoryHit`` 序列（顺序即优先级）。

        Returns:
            `MemoryBudgetResult`: 裁剪结果。``max_tokens <= 0`` 时全丢。
        """
        candidates = list(hits)
        if not candidates:
            return MemoryBudgetResult(
                kept=[],
                dropped=[],
                estimated_tokens=0,
                truncated=False,
                budget_tokens=self.max_tokens,
            )

        if self.max_tokens <= 0:
            logger.debug("MemoryBudget: max_tokens<=0，全部丢弃（{} 条）", len(candidates))
            return MemoryBudgetResult(
                kept=[],
                dropped=candidates,
                estimated_tokens=0,
                truncated=True,
                budget_tokens=self.max_tokens,
            )

        # 只有标题没有正文的块渲染出来是空串，先剔掉，否则会污染 per_hit_tokens。
        # 注意用 id() 而不是 == 判等：pydantic 模型是按值比较的，
        # 两条内容完全相同的 hit 用 == 会被判成同一条，而它们其实是两个位置。
        renderable = [hit for hit in candidates if str(getattr(hit, "text", "") or "").strip()]
        renderable_ids = {id(hit) for hit in renderable}
        unrenderable = [hit for hit in candidates if id(hit) not in renderable_ids]

        kept: list[Any] = []
        per_hit: list[int] = []
        previous_tokens = _BLOCK_OVERHEAD_TOKENS
        for hit in renderable:
            trial = render_memory_block([*kept, hit])
            tokens = self.estimator(trial)
            if tokens > self.max_tokens:
                break
            kept.append(hit)
            per_hit.append(tokens - previous_tokens)
            previous_tokens = tokens

        kept_ids = {id(hit) for hit in kept}
        dropped = [
            hit for hit in candidates if id(hit) not in kept_ids
        ]
        # 只有"有正文却没进 kept"才是被预算挤掉的；纯空白的 hit 不算截断。
        truncated = any(id(hit) in renderable_ids for hit in dropped)
        rendered = render_memory_block(kept)
        estimated = self.estimator(rendered) if rendered else 0

        result = MemoryBudgetResult(
            kept=kept,
            dropped=dropped,
            estimated_tokens=estimated,
            truncated=truncated,
            budget_tokens=self.max_tokens,
            per_hit_tokens=per_hit,
        )
        if dropped:
            logger.info(
                "MemoryBudget: 保留 {} 条 / 丢弃 {} 条（估计 {} tokens / 预算 {}）",
                len(kept),
                len(dropped),
                estimated,
                self.max_tokens,
            )
        if unrenderable:
            logger.debug("MemoryBudget: {} 条无正文，未参与渲染", len(unrenderable))
        return result

    # ------------------------------------------------------------------
    # 额外能力
    # ------------------------------------------------------------------
    def fits(self, hits: Sequence[Any]) -> bool:
        """不裁剪，只判断整批是否装得下。

        Args:
            hits (`Sequence[Any]`): ``MemoryHit`` 序列。

        Returns:
            `bool`: 是否在预算内。
        """
        rendered = render_memory_block(hits)
        if not rendered:
            return True
        return self.estimator(rendered) <= self.max_tokens

    def count(self, hits: Sequence[Any]) -> int:
        """度量一批 hit 渲染后的估计 token 数。

        Args:
            hits (`Sequence[Any]`): ``MemoryHit`` 序列。

        Returns:
            `int`: 估计 token 数。
        """
        rendered = render_memory_block(hits)
        return self.estimator(rendered) if rendered else 0

    def fit_or_truncate(
        self,
        hits: Sequence[Any],
        *,
        min_hits: int = 1,
        ellipsis: str = "\n…（已截断）",
    ) -> MemoryBudgetResult:
        """在 :meth:`fit` 之上加一个下限：**至少保留 ``min_hits`` 条**。

        为什么需要：:meth:`fit` 是严格预算。如果**第一条** hit 就超预算
        （ReMe 的 ``chunk_byte_size=10000`` 完全可能），严格预算会返回空列表 ——
        外部看到的现象是"记忆功能好像坏了"，而实际是预算太小。
        教学演示里这种"静默失败"最难排查，所以给一个显式的降级路径：
        保留前 ``min_hits`` 条，并把**最后一条**按剩余预算硬截断（补 ``ellipsis``）。

        截断只动文本、不丢来源信息：``path`` / 行号区间仍在，所以
        :class:`~harness_kit.memory.citations.CitationBuilder` 依然能给出可核对的引用。

        Args:
            hits (`Sequence[Any]`): ``MemoryHit`` 序列。
            min_hits (`int`): 至少保留几条；``0`` 时等价于 :meth:`fit`。
            ellipsis (`str`): 截断标记。

        Returns:
            `MemoryBudgetResult`: 结果；``truncated=True`` 包含"文本被砍"的情况。
        """
        base = self.fit(hits)
        if len(base.kept) >= max(1, min_hits) or not base.dropped:
            return base

        candidates = [hit for hit in hits if str(getattr(hit, "text", "") or "").strip()]
        if not candidates:
            return base

        floor = max(1, min_hits)
        prefix = candidates[:floor]
        # 先看前 floor 条能不能装下；装不下就把最后一条的正文削到装得下为止。
        while len(prefix) > 1 and not self.fits(prefix):
            prefix = prefix[:-1]

        tail = prefix[-1]
        prefix_without_tail = prefix[:-1]
        base_tokens = self.count(prefix_without_tail) if prefix_without_tail else _BLOCK_OVERHEAD_TOKENS
        room = self.max_tokens - base_tokens
        text = str(getattr(tail, "text", "") or "")
        # 二分找最长可用前缀，避免逐字去掉的重度循环。
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            trial_hit = tail.model_copy(update={"text": text[:mid] + ellipsis})
            if self.estimator(render_memory_block([*prefix_without_tail, trial_hit])) <= self.max_tokens:
                low = mid
            else:
                high = mid - 1
        truncated_hit = tail.model_copy(update={"text": text[:low].rstrip() + ellipsis})
        kept = [*prefix_without_tail, truncated_hit]

        kept_ids = {id(hit) for hit in kept}
        dropped = [hit for hit in hits if id(hit) not in kept_ids]
        rendered = render_memory_block(kept)
        estimated = self.estimator(rendered) if rendered else 0
        logger.warning(
            "MemoryBudget.fit_or_truncate: 严格预算下一条都装不下（预算 {}），"
            "降级为保留 {} 条并截断正文（估计 {} tokens）",
            self.max_tokens,
            len(kept),
            estimated,
        )
        if estimated > self.max_tokens:
            # 这是 min_hits 保证的必然代价：块头的固定开销（标题 + 使用说明 +
            # 每段的结构行）本身就超过了预算。此时预算已经不可满足，
            # 唯一诚实的做法是把话说清楚，而不是假装守住了预算。
            logger.warning(
                "MemoryBudget: 预算 {} 低于块头固定开销（估计 {} tokens），"
                "min_hits={} 的保证优先于预算；请调大 max_tokens 或调小 min_hits",
                self.max_tokens,
                estimated,
                min_hits,
            )
        return MemoryBudgetResult(
            kept=kept,
            dropped=dropped,
            estimated_tokens=estimated,
            truncated=True,
            budget_tokens=self.max_tokens,
            per_hit_tokens=[],
        )
