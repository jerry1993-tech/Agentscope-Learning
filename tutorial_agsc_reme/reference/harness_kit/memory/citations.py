# -*- coding: utf-8 -*-
"""检索结果的**结构化视图**与**来源引用**（契约 §3.17 + §5.3）。

这个模块承担两件事，因为它们是同一件事的两面：

1. 定义 :class:`MemoryHit` —— ReMe ``search`` 的原始输出
   （``Response.metadata["results"]``，一串 ``FileChunk.model_dump()``）
   在 harness 侧的**强类型视图**。没有它，后面每一个模块都要写
   ``item.get("scores", {}).get("score", 0.0)`` 这种防御式代码，
   而 ReMe 改一个字段名就会在半夜炸。
2. 把 ``MemoryHit`` 变成**可以印在回答里的引用**（``[1] path:12-18``）。
   引用之所以要落到**行号**，是因为 ReMe 的记忆是文件原生的：
   记忆卡片就是磁盘上的 Markdown，行号是用户能自己打开文件核对的锚点。
   这一点和"向量库里一段没有出处的文本"有本质区别，也是 ReMe 选文件原生存储的收益。

**``hash_id`` 为什么可以跨会话稳定引用**

``third_party/ReMe/reme/schema/file_chunk.py:21-26`` 的 ``set_hash_id``：

.. code-block:: python

    self.id = hash_text(" ".join([self.path, str(self.start_line), str(self.end_line), self.text]))

输入只有 (path, 起行, 止行, 文本) 四项，**不含时间戳、不含会话 id、不含序号**。
所以同一段文字只要没被改动，在今天的检索和三个月后的检索里 ``chunk_id`` 完全一致
—— 这正是"引用可追溯、可跨会话复用"的技术前提。
反过来说：**改了文件名、挪了行、动了一个字，id 就变了**。
所以引用里必须同时带 ``path`` 和行号，只存 id 是没法给人看的。

**``source`` 字段的三态**

ReMe 的 ``SearchStep`` 用 RRF 融合两路召回
（``third_party/ReMe/reme/steps/index/search.py:54-85`` 的 ``_rrf_merge``）。
融合后每个 chunk 的 ``scores`` 字典里可能有 ``vector`` / ``keyword`` 两项，
也可能只有其中一项（另一路没召回它，或者该路整个没启用）。
:meth:`to_memory_hit` 据此判出三态：

============ ==========================================================
``source``   判定条件
============ ==========================================================
``"fused"``  两路都有分（``scores`` 里同时有 ``vector`` 与 ``keyword``）
``"keyword"`` 只有 ``keyword`` 分
``"vector"``  只有 ``vector`` 分
============ ==========================================================

对齐的真实 API：

- ``third_party/ReMe/reme/schema/file_chunk.py:8`` ``FileChunk``
  （``path`` / ``start_line`` / ``end_line`` / ``text`` / ``scores`` / ``score`` / ``id``）
- ``third_party/ReMe/reme/steps/index/search.py:363-372`` 写回
  ``metadata["results"]`` 与 ``metadata["counts"]`` 的那几行
- ``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_utils.py:51-103``
  ``_extract_memory_texts``（官方只取 ``text``，**丢掉了 path 和行号** —— 这就是缺口）
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Citation",
    "CitationBuilder",
    "MemoryHit",
    "SearchResult",
    "merge_intervals",
    "to_memory_hit",
    "to_memory_hits",
]


class MemoryHit(BaseModel):
    """一条检索命中的结构化视图（契约 §5.3）。

    字段逐字来自契约，含义与 ReMe ``FileChunk`` 的对应关系：

    ================== ====================================================
    MemoryHit          FileChunk
    ================== ====================================================
    ``chunk_id``       ``id``（``set_hash_id()`` 的确定性哈希）
    ``path``           ``path``（工作区相对路径；越界时会是绝对路径）
    ``start_line``     ``start_line``（1-based，含）
    ``end_line``       ``end_line``（1-based，含）
    ``text``           ``text``（**整块**文本，可能上千字）
    ``score``          ``score``（= ``scores["score"]``，融合后总分）
    ``source``         —— 由 ``scores`` 里有哪些键推导（见模块 docstring）
    ``rank_keyword``   —— 由结果列表里的出现次序推导，**不是** ReMe 给的
    ``rank_vector``    —— 同样由出现次序推导（见 ``to_memory_hits``）
    ``tags``           —— ReMe 的 search 结果**不含**标签，需另外查 tag_index
    ================== ====================================================
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    """``FileChunk.hash_id``：path + 行区间 + 文本的确定性哈希，可跨会话稳定引用。"""

    path: str
    """工作区相对路径（用 :meth:`~harness_kit.memory.workspace.ReMeWorkspace.relative` 归一过）。"""

    start_line: int
    """起始行（1-based，含）。"""

    end_line: int
    """结束行（1-based，含）。"""

    text: str
    """片段原文。"""

    score: float
    """融合后的总分（``scores["score"]``）。"""

    source: Literal["keyword", "vector", "fused"]
    """命中来自哪一路。"""

    rank_keyword: int | None = None
    """在关键词路里的名次（1-based）；没走这一路时为 ``None``。"""

    rank_vector: int | None = None
    """在向量路里的名次（1-based）；没走这一路时为 ``None``。"""

    tags: list[str] = Field(default_factory=list)
    """该文件当前生效的标签（需要调用方用 tag_index 填充；search 结果本身不带）。"""


class Citation(BaseModel):
    """可渲染的来源引用（契约 §5.3）。"""

    model_config = ConfigDict(extra="forbid")

    index: int
    """从 1 开始的引用序号，渲染成 ``[1]`` ``[2]``。"""

    path: str
    """来源文件（工作区相对路径）。"""

    start_line: int
    """起始行。"""

    end_line: int
    """结束行。"""

    quote: str
    """截断后的原文摘录。"""

    score: float
    """该来源的分数。"""


class SearchResult(BaseModel):
    """一次检索的完整结果（契约 §5.3）。"""

    model_config = ConfigDict(extra="forbid")

    query: str
    """原始查询串。"""

    hits: list[MemoryHit]
    """命中的结构化结果。"""

    counts: dict[str, int]
    """ReMe 给的计数，键为 ``vector`` / ``keyword`` / ``returned`` / ``hybrid``。

    **必须注意**：``counts["vector"] == 0`` 且 ``hybrid=True``（原文如此，
    ReMe 把它放在 ``counts`` 里而不是顶层）是**合法**状态 ——
    没配 embedding 时向量路就是答空，``success`` 仍然是 ``True``。
    """

    hybrid: bool
    """两路是否都召回了东西（ReMe ``counts["hybrid"]``）。"""

    elapsed_ms: float
    """本次检索的墙上时间（毫秒）。"""


def to_memory_hit(
    raw: dict[str, Any],
    *,
    workspace: Any | None = None,
    rank_keyword: int | None = None,
    rank_vector: int | None = None,
    tags: Sequence[str] | None = None,
) -> MemoryHit:
    """把一个 ReMe 的 ``metadata["results"]`` 元素转成 :class:`MemoryHit`。

    Args:
        raw (`dict[str, Any]`): ``FileChunk.model_dump()`` 的结果。
        workspace (`Any | None`): 可选的
            :class:`~harness_kit.memory.workspace.ReMeWorkspace`；给了就把绝对路径
            归一成工作区相对路径（macOS 的 ``/tmp`` 陷阱就靠它兜住）。
        rank_keyword (`int | None`): 关键词路名次。
        rank_vector (`int | None`): 向量路名次。
        tags (`Sequence[str] | None`): 该文件当前生效的标签。

    Returns:
        `MemoryHit`: 结构化命中。

    Raises:
        `ValueError`: ``raw`` 里连 ``path`` 都没有（说明不是 chunk 形状）。
    """
    if not isinstance(raw, dict) or "path" not in raw:
        raise ValueError(f"不是 ReMe 的 FileChunk 形状，缺少 path: {type(raw).__name__}")

    scores = raw.get("scores") or {}
    if not isinstance(scores, dict):
        scores = {}
    has_vector = scores.get("vector") is not None
    has_keyword = scores.get("keyword") is not None
    if has_vector and has_keyword:
        source: Literal["keyword", "vector", "fused"] = "fused"
    elif has_keyword:
        source = "keyword"
    else:
        source = "vector"

    path = str(raw.get("path", ""))
    if workspace is not None:
        path = workspace.relative(path)

    score = scores.get("score")
    if score is None:
        # FileChunk.score 属性在 scores 里没有 "score" 时返回 0.0；
        # 单路召回时 ReMe 会把原始分放进 vector/keyword 那一路，这里兜一下。
        score = scores.get("keyword", scores.get("vector", 0.0))

    return MemoryHit(
        chunk_id=str(raw.get("id", "")),
        path=path,
        start_line=int(raw.get("start_line", 0) or 0),
        end_line=int(raw.get("end_line", 0) or 0),
        text=str(raw.get("text", "")),
        score=float(score or 0.0),
        source=source,
        rank_keyword=rank_keyword,
        rank_vector=rank_vector,
        tags=list(tags or []),
    )


def to_memory_hits(
    results: Iterable[dict[str, Any]] | None,
    *,
    workspace: Any | None = None,
    tags_by_path: dict[str, list[str]] | None = None,
) -> list[MemoryHit]:
    """批量转换，并**按路给名次**。

    ReMe 的 ``metadata["results"]`` 是融合后已经排好序的列表，
    但每条 chunk 的 ``scores`` 里仍然保留了该路自己的原始分。所以
    "关键词路第几名" 可以这样还原：把结果里所有**有 keyword 分**的项
    按 keyword 分从高到低排序，取它的序号。这是派生信息，不是 ReMe 给的，
    因此写在这里而不是假装是 ReMe 的字段。

    Args:
        results (`Iterable[dict[str, Any]] | None`): ``metadata["results"]``。
        workspace (`Any | None`): 可选工作区（用于路径归一）。
        tags_by_path (`dict[str, list[str]] | None`): 路径 → 标签。

    Returns:
        `list[MemoryHit]`: 结构化命中，顺序与输入一致（= 融合后的名次）。
    """
    if not results:
        return []

    rows = [row for row in results if isinstance(row, dict) and "path" in row]
    keyword_order = sorted(
        (row for row in rows if (row.get("scores") or {}).get("keyword") is not None),
        key=lambda row: float((row.get("scores") or {}).get("keyword") or 0.0),
        reverse=True,
    )
    vector_order = sorted(
        (row for row in rows if (row.get("scores") or {}).get("vector") is not None),
        key=lambda row: float((row.get("scores") or {}).get("vector") or 0.0),
        reverse=True,
    )
    keyword_rank = {id(row): index for index, row in enumerate(keyword_order, start=1)}
    vector_rank = {id(row): index for index, row in enumerate(vector_order, start=1)}

    hits: list[MemoryHit] = []
    for row in rows:
        tags = (tags_by_path or {}).get(str(row.get("path", "")))
        hit = to_memory_hit(
            row,
            workspace=workspace,
            rank_keyword=keyword_rank.get(id(row)),
            rank_vector=vector_rank.get(id(row)),
            tags=tags,
        )
        hits.append(hit)
    return hits


def merge_intervals(chunks: Sequence[Any], *, gap: int = 0) -> list[tuple[int, int]]:
    """把若干 chunk 的行区间合并成不相交的区间列表（契约 §3.17）。

    为什么需要它：ReMe 的默认分块器是**字节窗口 + 重叠**
    （``default_file_chunker.py:25-26`` 的 ``chunk_byte_size=10000`` /
    ``overlap_byte_size=100``），一次检索里很可能命中同一个文件的多个相邻 chunk。
    直接逐条渲染引用，读者会看到 "12-40" 和 "38-70" 两条几乎重叠的引用，
    既啰嗦又让人以为有两处不同内容。

    Args:
        chunks (`Sequence[Any]`): 任何有 ``start_line`` / ``end_line`` 的对象
            （``FileChunk`` 或 :class:`MemoryHit` 都行）。
        gap (`int`): 允许的空隙；两个区间距离 ``<= gap`` 行时也合并。默认 0。

    Returns:
        `list[tuple[int, int]]`: 按起点排序、两两不相交且不相邻的 ``(start, end)``。
    """
    spans: list[tuple[int, int]] = []
    for chunk in chunks:
        start = int(getattr(chunk, "start_line", 0) or 0)
        end = int(getattr(chunk, "end_line", 0) or 0)
        if end < start:
            start, end = end, start
        if start <= 0 and end <= 0:
            continue
        spans.append((start, end))
    if not spans:
        return []

    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + gap + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


class CitationBuilder:
    """把 :class:`MemoryHit` 变成可渲染引用（契约 §3.17）。

    设计上做三件事，都在 "让人能自己去核对" 这个目标下：

1. **截断**：``max_quote_chars`` 控制摘录长度。ReMe 的 chunk 可能上千字
       （``chunk_byte_size=10000``），原样印进回答会把真正的回答淹掉。
    2. **去重**：同一 ``chunk_id`` 只出一条引用（``hash_id`` 跨会话稳定，
       所以这是可靠的去重键）。同一文件的**多个不重叠区间**会各自成条 ——
       因为它们确实是不同的来源位置。
    3. **丢弃空文本**：ReMe 的索引块有时只有元数据没有正文，引用它没有意义。

    Example::

        builder = CitationBuilder(max_quote_chars=160)
        citations = builder.build(result.hits)
        for line in builder.render_lines(citations):
            print(line)          # [1] daily/2026-09-21/pref.md:3-6  深色主题 matplotlib
    """

    def __init__(self, *, max_quote_chars: int = 240) -> None:
        """配置摘录长度。

        Args:
            max_quote_chars (`int`): 单条引用最多几个字符。

        Raises:
            `ValueError`: ``max_quote_chars <= 0``。
        """
        if max_quote_chars <= 0:
            raise ValueError(f"max_quote_chars 必须为正，收到 {max_quote_chars}")
        self.max_quote_chars: int = int(max_quote_chars)

    def build(self, chunks: Sequence[Any]) -> list[Citation]:
        """构造引用列表。

        Args:
            chunks (`Sequence[Any]`): :class:`MemoryHit`（也容忍 ``FileChunk``）。

        Returns:
            `list[Citation]`: 从 1 开始编号的引用，按输入顺序。
        """
        citations: list[Citation] = []
        seen: set[str] = set()
        for chunk in chunks:
            text = str(getattr(chunk, "text", "") or "")
            if not text.strip():
                continue

            chunk_id = str(getattr(chunk, "chunk_id", "") or getattr(chunk, "id", "") or "")
            path = str(getattr(chunk, "path", "") or "")
            start = int(getattr(chunk, "start_line", 0) or 0)
            end = int(getattr(chunk, "end_line", 0) or 0)
            # 没给 id 的（例如裸 FileChunk）退化成 path+行 当去重键。
            key = chunk_id or f"{path}:{start}-{end}"
            if key in seen:
                continue
            seen.add(key)

            raw_score = getattr(chunk, "score", None)
            if raw_score is None:
                scores = getattr(chunk, "scores", None) or {}
                raw_score = scores.get("score", 0.0) if isinstance(scores, dict) else 0.0

            citations.append(
                Citation(
                    index=len(citations) + 1,
                    path=path,
                    start_line=start,
                    end_line=end,
                    quote=self._quote(text),
                    score=float(raw_score or 0.0),
                ),
            )
        return citations

    def render_lines(self, citations: Sequence[Citation]) -> list[str]:
        """渲染成引用块的行列表（契约 §3.17）。

        格式刻意选择 ``[n] path:start-end  quote``，因为：

        - ``[n]`` 与正文里的 ``[n]`` 能对上；
        - ``path:start-end`` 是 "编辑器里可直接跳转" 的形态（VS Code 的终端里可点）；
        - quote 单行化，避免引用块比正文还长。

        Args:
            citations (`Sequence[Citation]`): 引用列表。

        Returns:
            `list[str]`: 每行一条，形如 ``"[1] daily/x.md:3-6  ..."``。
        """
        lines: list[str] = []
        for citation in citations:
            location = f"{citation.path}:{citation.start_line}-{citation.end_line}"
            quote = citation.quote.replace("\n", " ").strip()
            lines.append(f"[{citation.index}] {location}  {quote}")
        return lines

    def _quote(self, text: str) -> str:
        """按预算截断摘录。

        Args:
            text (`str`): 原文。

        Returns:
            `str`: 截断后的摘录；超长时以 ``"…"`` 结尾。
        """
        collapsed = " ".join(text.split())
        if len(collapsed) <= self.max_quote_chars:
            return collapsed
        return collapsed[: self.max_quote_chars].rstrip() + "…"
