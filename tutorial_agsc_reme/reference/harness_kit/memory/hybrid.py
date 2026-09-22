# -*- coding: utf-8 -*-
"""混合检索的**权重与融合控制**（契约 §3.17）。

**为什么需要这一层：ReMe 的 ``vector_weight`` 是三处配置里最深的一处**

ReMe 的 ``search`` job 是 ``files_store → search_step`` 两步
（job 的**定义**在 ``third_party/ReMe/reme/config/default.yaml:442-478``，
**实现**在 ``third_party/ReMe/reme/steps/index/search.py`` ——
``reme/`` 下**没有** ``jobs/`` 这个目录，job 的"名字 → 步骤链"映射全部写在 YAML 里），融合权重最终落在
``SearchStep`` 的 ``kwargs["vector_weight"]``。而 ``SearchStep`` 用一条**硬分界**决定走哪条路
（``third_party/ReMe/reme/steps/index/search.py:309-323``）：

.. code-block:: python

    text_weight = 1.0 - vector_weight
    use_vector = vector_weight > 0.0
    use_keyword = text_weight > 0.0

    if use_vector and use_keyword:
        vector_results, keyword_results = await asyncio.gather(
            self.file_store.vector_search(query, candidates, search_filter),
            self.file_store.keyword_search(query, candidates, search_filter),
        )
    elif use_vector:
        vector_results = await self.file_store.vector_search(...)
        keyword_results = []
    else:
        vector_results = []
        keyword_results = await self.file_store.keyword_search(...)

也就是说 ``vector_weight`` 取 ``0.0`` 或 ``1.0`` 会**整路跳过**（连 embedding 都不算），
这对成本影响巨大；取了中间值则两路都跑、再融合。

**ReMe 的融合只做两件事**（``search.py:330-338``，读一遍就懂）：

.. code-block:: python

    hybrid = bool(vector_results) and bool(keyword_results)
    if not vector_results and not keyword_results:
        fused = []
    elif not keyword_results:
        fused = vector_results          # 单路：原样返回，**不融合**
    elif not vector_results:
        fused = keyword_results         # 单路：原样返回，**不融合**
    else:
        fused = self._rrf_merge(vector_results, keyword_results, vector_weight)

所以"三态"是**批级**的判定，而不是逐条的：两路都有结果才进 RRF；
否则整批退化成那一路的原始分数（BM25 分 / cosine 分）。
:meth:`HybridRetriever.fuse` 把这个三态**逐字复刻**。

**RRF 的分数尺度陷阱（本模块最重要的教学点）**

``search.py:14`` ``_RRF_K: Final = 60``，融合分是

.. code-block:: text

    fused(d) = w_v / (60 + rank_v(d)) + (1 - w_v) / (60 + rank_k(d))

两路都排第 1（``vector_weight=0.5``）才 ``0.5/61 + 0.5/61 ≈ 0.0164``。
**融合分会比 BM25 原始分小两三个数量级**，所以：

- 拿 ``min_score=0.2`` 去过滤融合结果 → 一条都留不下（这正是 harness 的
  :class:`~harness_kit.memory.gating.MemoryGate` 必须做**归一化**的原因）；
- 拿融合分和 BM25 分放在同一张表里比较 → 也是错的（量纲不同）。

这条"量纲随路径切换"的性质是 ``SearchStep`` 的固有行为
（``search.py:340-341``：``if min_score > 0.0: fused = [c for c in fused if c.score >= min_score]``
—— 它比的是**融合后**的 ``c.score``），harness 只能适配、不能假装不存在。
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from loguru import logger

__all__ = [
    "FusedEntry",
    "FusionMode",
    "HybridRetriever",
    "RRF_K",
]

#: RRF 的平滑常数，与 ``third_party/ReMe/reme/steps/index/search.py:14`` 的 ``_RRF_K`` 一致。
RRF_K: int = 60

#: 三态融合模式。
FusionMode = Literal["rrf", "keyword_only", "vector_only", "empty"]


class FusedEntry:
    """一条融合后的条目：``(key, score, mode, detail)``。

    这是一个**轻量值对象**而不是 pydantic 模型：融合是纯计算、可能被高频调用
    （每轮对话一次检索），没必要为它付一遍校验开销。它实现了 ``__eq__``/``__hash__``
    只为方便测试与去重。

    Attributes:
        key (`str`): 条目标识（这里用 chunk id；RRF 的关键就是"用 id 对齐两路"）。
        score (`float`): 融合分。
        mode (`FusionMode`): 这条来自哪种融合。
        rank_keyword (`int | None`): 关键词路名次。
        rank_vector (`int | None`): 向量路名次。
        raw_keyword (`float | None`): 关键词路原始分。
        raw_vector (`float | None`): 向量路原始分。
    """

    __slots__ = ("key", "score", "mode", "rank_keyword", "rank_vector", "raw_keyword", "raw_vector")

    def __init__(
        self,
        key: str,
        score: float,
        mode: FusionMode,
        *,
        rank_keyword: int | None = None,
        rank_vector: int | None = None,
        raw_keyword: float | None = None,
        raw_vector: float | None = None,
    ) -> None:
        self.key = key
        self.score = float(score)
        self.mode = mode
        self.rank_keyword = rank_keyword
        self.rank_vector = rank_vector
        self.raw_keyword = raw_keyword
        self.raw_vector = raw_vector

    def __repr__(self) -> str:
        return (
            f"FusedEntry(key={self.key!r}, score={self.score:.6f}, mode={self.mode!r}, "
            f"rank_v={self.rank_vector}, rank_k={self.rank_keyword})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FusedEntry):
            return NotImplemented
        return (self.key, self.score, self.mode) == (other.key, other.score, other.mode)

    def __hash__(self) -> int:
        """与 :meth:`__eq__` 同源：参与比较的三个字段才是哈希键。

        定义了 ``__eq__`` 却不定义 ``__hash__``，Python 会把类标成不可哈希
        （``__hash__ = None``），于是 ``{entry, ...}`` 和 ``set`` 去重直接
        ``TypeError``。这里必须显式补上 —— 上面 docstring 承诺了"方便测试与去重"。

        Returns:
            `int`: 哈希值。
        """
        return hash((self.key, self.score, self.mode))

    def as_tuple(self) -> tuple[str, float]:
        """返回契约里的二元组形态。

        Returns:
            `tuple[str, float]`: ``(key, score)``。
        """
        return (self.key, self.score)


class HybridRetriever:
    """混合检索的权重与融合控制（契约 §3.17）。

    它做三件事，每一件都对应一个真实可讲的问题：

    1. **融合**（:meth:`fuse`）：把 ``(id, 分)`` 两条有序列表按 RRF 合成一条。
       逐字复刻 ``SearchStep._rrf_merge`` 的三态语义。
    2. **重排**（:meth:`refuse`）：ReMe 一次检索只用一个 ``vector_weight``。
       如果先用中性权重取回两路结果，后续想换权重，
       重新调一次 ``search`` 的成本是**又一次 embedding 调用**；
       而 :class:`~harness_kit.memory.citations.MemoryHit` 里已经带了
       ``rank_keyword`` / ``rank_vector``，**用排名就能离线重算 RRF**，零成本。
       这就是本类相对"直接调 ReMe"的增量价值。
    3. **解释**（:meth:`explain`）：把当前权重下的公式展开成人话。
       调参调不动的时候，能一眼看出"现在到底是几路在跑"。

    Example::

        retriever = HybridRetriever(vector_weight=0.7)
        fused = retriever.fuse(keyword=[("a", 12.3), ("b", 9.1)],
                               vector=[("b", 0.88), ("c", 0.71)])
        print(retriever.explain())
        for key, score in fused:
            print(key, f"{score:.6f}")
    """

    #: 契约要求暴露的常数，转发到模块级 :data:`RRF_K`，保证只有一处真值。
    RRF_K: int = RRF_K

    def __init__(self, *, vector_weight: float = 0.5) -> None:
        """配置两路的权重。

        Args:
            vector_weight (`float`): 向量路权重，取值 ``[0.0, 1.0]``。
                ``0.0`` → 只跑关键词（``SearchStep`` 会整路跳过向量）；
                ``1.0`` → 只跑向量；中间值 → 两路都跑 + RRF。

        Raises:
            `ValueError`: 不在 ``[0.0, 1.0]`` 内。
        """
        if not 0.0 <= float(vector_weight) <= 1.0:
            raise ValueError(f"vector_weight 必须在 [0,1]，收到 {vector_weight}")
        self.vector_weight: float = float(vector_weight)

    # ------------------------------------------------------------------
    # 融合
    # ------------------------------------------------------------------
    @property
    def text_weight(self) -> float:
        """关键词路权重，恒等于 ``1 - vector_weight``（``search.py:309``）。

        Returns:
            `float`: ``1 - vector_weight``。
        """
        return 1.0 - self.vector_weight

    def mode(self, keyword: Sequence[Any], vector: Sequence[Any]) -> FusionMode:
        """判定这批结果会走哪种融合（与 ``search.py:330-338`` 的分支一一对应）。

        Args:
            keyword (`Sequence[Any]`): 关键词路结果。
            vector (`Sequence[Any]`): 向量路结果。

        Returns:
            `FusionMode`: ``"rrf"`` / ``"keyword_only"`` / ``"vector_only"`` / ``"empty"``。
        """
        has_keyword = bool(keyword) and self.text_weight > 0.0
        has_vector = bool(vector) and self.vector_weight > 0.0
        if has_keyword and has_vector:
            return "rrf"
        if has_keyword:
            return "keyword_only"
        if has_vector:
            return "vector_only"
        return "empty"

    def fuse(
        self,
        keyword: list[tuple[str, float]],
        vector: list[tuple[str, float]],
    ) -> list[tuple[str, float]]:
        """融合两路结果，返回按分数降序的 ``(key, score)``（契约 §3.17）。

        语义逐字对齐 ``SearchStep._rrf_merge``（``search.py:54-85``）+ 三态分支
        （``search.py:330-338``）：

        - 两路都有结果 → RRF：``w_v/(K+rank_v) + w_k/(K+rank_k)``；
        - 只有一路 → **不融合**，直接返回那一路的原始分（保持原顺序，因为
          单路结果本来就已经按分数排好了）；
        - 两路都空 → 空列表。

        Args:
            keyword (`list[tuple[str, float]]`): 关键词路 ``[(key, 原始分), ...]``，
                **必须已按分数降序**（ReMe 的 ``keyword_search`` 保证这一点，
                ``local_file_store.py:1015`` 的 ``scores={"keyword": score, "score": score}``）。
            vector (`list[tuple[str, float]]`): 向量路，同样按分数降序。

        Returns:
            `list[tuple[str, float]]`: 降序的 ``(key, 融合分)``，长度等于两路 key 的并集大小。
        """
        mode = self.mode(keyword, vector)
        if mode == "empty":
            return []
        if mode == "keyword_only":
            return [(key, float(score)) for key, score in keyword]
        if mode == "vector_only":
            return [(key, float(score)) for key, score in vector]
        return [entry.as_tuple() for entry in self.fuse_entries(keyword, vector)]

    def fuse_entries(
        self,
        keyword: Sequence[tuple[str, float]],
        vector: Sequence[tuple[str, float]],
    ) -> list[FusedEntry]:
        """同 :meth:`fuse`，但保留名次与原始分（用于诊断与离线重排）。

        Args:
            keyword (`Sequence[tuple[str, float]]`): 关键词路（降序）。
            vector (`Sequence[tuple[str, float]]`): 向量路（降序）。

        Returns:
            `list[FusedEntry]`: 降序条目。
        """
        merged: dict[str, FusedEntry] = {}

        for rank, (key, raw) in enumerate(vector, start=1):
            contribution = self.vector_weight / (self.RRF_K + rank)
            merged[key] = FusedEntry(
                key,
                contribution,
                "rrf",
                rank_vector=rank,
                raw_vector=float(raw),
            )

        for rank, (key, raw) in enumerate(keyword, start=1):
            contribution = self.text_weight / (self.RRF_K + rank)
            existing = merged.get(key)
            if existing is None:
                merged[key] = FusedEntry(
                    key,
                    contribution,
                    "rrf",
                    rank_keyword=rank,
                    raw_keyword=float(raw),
                )
            else:
                existing.score += contribution
                existing.rank_keyword = rank
                existing.raw_keyword = float(raw)

        # 平局按 key 兜底排序：RRF 的取值是离散的（只有 K+rank 这几种），
        # 平局很常见。让它随 dict 插入顺序漂移会让引用顺序变得不可复现，
        # 而引用顺序会影响模型对"哪条更重要"的第一印象。
        return sorted(merged.values(), key=lambda entry: (-entry.score, entry.key))

    def refuse(
        self,
        hits: Sequence[Any],
        *,
        drop_empty: bool = True,
    ) -> list[Any]:
        """**离线重排**：用 :class:`MemoryHit` 里已有的名次重算 RRF，不再打 ReMe。

        什么时候用：先用 ``vector_weight=0.5`` 取回一批结果，然后发现
        "这批问句其实更适合关键词"，于是改用 ``vector_weight=0.0`` 重排。
        如果重新调 ``search``，代价是一次 embedding + 一次 BM25；
        用本方法则是纯内存计算。

        前提：``hits`` 必须来自 :func:`~harness_kit.memory.citations.to_memory_hits`
        （它才会填 ``rank_keyword`` / ``rank_vector``）。
        只用关键词路的 hit 会走 ``keyword_only`` 分支，用它的 ``score`` 原样返回。

        Args:
            hits (`Sequence[Any]`): ``MemoryHit`` 序列。
            drop_empty (`bool`): ``True`` 时丢弃两路名次都为空的 hit。

        Returns:
            `list[Any]`: 重排后的 hit 列表（**新对象**，用 ``model_copy(update=...)``
            产生，不改原列表）。

        Raises:
            `TypeError`: 元素既没有 ``rank_keyword`` 也没有 ``rank_vector``
                （说明不是 :class:`MemoryHit`）。
        """
        candidates = list(hits)
        if not candidates:
            return []

        keywords: list[tuple[str, float]] = []
        vectors: list[tuple[str, float]] = []
        for index, hit in enumerate(candidates):
            has_ranks = hasattr(hit, "rank_keyword") and hasattr(hit, "rank_vector")
            if not has_ranks:
                raise TypeError(
                    "refuse() 需要带 rank_keyword/rank_vector 的 MemoryHit；"
                    f"收到 {type(hit).__name__}。请先用 to_memory_hits() 转换。",
                )
            key = str(index)
            if hit.rank_keyword is not None:
                keywords.append((key, -float(hit.rank_keyword)))
            if hit.rank_vector is not None:
                vectors.append((key, -float(hit.rank_vector)))

        # 名次越小越好，而 fuse() 假定输入按分数降序，所以上面用 -rank 当"分数"。
        #
        # 但"用 -rank 当分数"只解决了一半：fuse_entries() 的 name rank 是
        # **列表位置**（``for rank, (key, raw) in enumerate(vector, start=1)``，
        # hybrid.py:270），传入顺序错了名次就全错。而上面是按 **candidates 的顺序**
        # 追加的，与各路真实名次无关 —— 不排序的话 refuse() 会把"第 1 名"发给
        # 所有人，重排退化成恒等变换。这一步不能省。
        keywords.sort(key=lambda pair: pair[1], reverse=True)
        vectors.sort(key=lambda pair: pair[1], reverse=True)

        if self.mode(keywords, vectors) != "rrf":
            if self.mode(keywords, vectors) == "empty" and drop_empty:
                logger.warning("refuse(): 所有 hit 都没有名次信息，返回空")
                return []
            return candidates

        fused = self.fuse(keywords, vectors)
        by_index = {key: score for key, score in fused}
        # 平局用原始位置兜底：RRF 的分数是离散的（只有 K+rank 这几种取值），
        # 平局很常见，如果让它随 dict 的插入顺序决定，同一批数据在不同版本的
        # Python 上可能给出不同的引用顺序 —— 引用顺序会影响模型看到的第一印象，
        # 所以必须确定。
        ordered_pairs = sorted(fused, key=lambda pair: (-pair[1], int(pair[0])))
        ordered: list[Any] = []
        for key, score in ordered_pairs:
            hit = candidates[int(key)]
            ordered.append(hit.model_copy(update={"score": score}))
        if not drop_empty:
            fused_keys = {key for key, _ in fused}
            for index, hit in enumerate(candidates):
                if str(index) not in fused_keys:
                    ordered.append(hit)
        return ordered

    def explain(self) -> str:
        """把当前权重下的行为展开成人话（契约 §3.17）。

        Returns:
            `str`: 多行说明。包含：会走哪几路、公式、以及融合分的量级警告。
        """
        lines = [
            f"HybridRetriever(vector_weight={self.vector_weight}, text_weight={self.text_weight})",
            f"  RRF_K = {self.RRF_K}（与 ReMe SearchStep._RRF_K 一致）",
        ]
        if self.vector_weight <= 0.0:
            lines.append("  路由：仅关键词（vector_weight=0.0 → SearchStep 整路跳过向量，不产生 embedding 调用）")
        elif self.text_weight <= 0.0:
            lines.append("  路由：仅向量（vector_weight=1.0 → SearchStep 整路跳过 BM25）")
        else:
            lines.append("  路由：向量 + 关键词并行，再 RRF 融合")
            lines.append(
                f"  公式：fused(d) = {self.vector_weight}/({self.RRF_K}+rank_v(d))"
                f" + {self.text_weight}/({self.RRF_K}+rank_k(d))",
            )
            best = self.vector_weight / (self.RRF_K + 1) + self.text_weight / (self.RRF_K + 1)
            lines.append(f"  量级警告：两路都排第 1 也只有 {best:.4f}，")
            lines.append(
                "            远小于 BM25 分或 cosine 分；直接用 min_score=0.2 之类的绝对阈值"
                "会把融合结果全过滤掉。",
            )
        return "\n".join(lines)
