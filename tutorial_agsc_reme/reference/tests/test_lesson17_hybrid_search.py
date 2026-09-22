# -*- coding: utf-8 -*-
"""第 17 讲的 pytest：检索链路的融合、引用与预算。

六条纪律（延续第 15/16 讲，本讲的重点是"分数只在有量纲的前提下才有意义"）：

1. **0 次 LLM 调用**。检索链路一次模型调用都不需要：融合是纯算术，
   召回是本地 BM25 + 本地向量索引，标签与图是本地文件 IO。
   真实模型的用法在 ``scripts/17_hybrid_search.py --live``（1 次补全）。
2. **纯函数部分不启动 ReMe**。``hybrid.py`` / ``citations.py`` 的
   :func:`~harness_kit.memory.citations.merge_intervals` / ``budget.py``
   完全不依赖 ReMe，所以它们跑在毫秒级测试里；只有"真实召回"那 5 条才需要
   ``await client.start()`` —— 因为它们要证明的恰恰是"ReMe 真的按这个语义召回"。
3. **每个测试自己建工作区，绝不共享**。ReMe 的 ``Application._start()`` 会建
   ``asyncio.Lock``，而 ``pyproject.toml`` 里
   ``asyncio_default_fixture_loop_scope = "function"`` 意味着**每个测试一个事件循环**：
   跨测试复用一个 client 就是跨事件循环复用锁。
4. **回归测试钉住已经踩过的坑**：
   ``test_min_score_filters_fused_scale`` 对应"拿 BM25 尺度去卡 RRF 分"；
   ``test_traverse_reads_answer_not_metadata`` 对应"两个 job 的响应约定相反"；
   ``test_vector_weight_one_without_embedding_is_silent`` 对应"降级不彻底"。
5. **与 ReMe 对齐的地方要断言到数值**，不能只断"有结果"：
   ``test_fuse_matches_reme_static_merge`` 直接把 harness 的融合结果与
   ReMe ``SearchStep._rrf_merge`` 的结果逐条比对 —— 这样 ReMe 升级改了公式，
   测试会红，而不是让两个实现悄悄漂开。
6. **失败路径一条都不能少**：空 query、非正的 limit、越界的 vector_weight、
   未知标签、缺 embedding、空 budget、``max_tokens=True`` 这种手滑输入。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson17_hybrid_search.py -v
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from reme.schema.file_chunk import FileChunk
from reme.steps.index.search import SearchStep

from harness_kit.memory import (
    RRF_K,
    Citation,
    CitationBuilder,
    FusedEntry,
    HarnessMemoryConfig,
    HybridRetriever,
    MemoryBudget,
    MemoryClient,
    MemoryIngestor,
    MemoryJobError,
    MemorySearch,
    ReMeWorkspace,
    estimate_tokens_heuristic,
    merge_intervals,
    render_memory_block,
    to_memory_hit,
    to_memory_hits,
)

# ======================================================================
# 共用夹具
# ======================================================================
CORPUS: list[tuple[str, str, list[str]]] = [
    (
        "deploy",
        "---\nname: 部署手册\nmemory_tags: [ops, deploy]\n---\n\n"
        "# 部署手册\n\n上线分三步：预检、切换、观察。\n\n"
        "预检要跑 uv sync 与 pytest -q。\n\n"
        "切换用蓝绿发布，见 [[resource/runbook.md#回滚]]。\n",
        ["ops", "deploy"],
    ),
    (
        "runbook",
        "# 回滚手册\n\n回滚三步：停流量、回滚镜像 tag、验单。\n",
        ["ops", "rollback"],
    ),
    (
        "pref",
        "# 绘图偏好\n\n用户偏好深色主题的 matplotlib 图表。\n",
        ["pref"],
    ),
]


@pytest.fixture()
async def memory(tmp_path: Path) -> AsyncIterator[tuple[MemorySearch, ReMeWorkspace]]:
    """一个隔离的嵌入式 ReMe（**不占端口**）+ 一个已入库的小语料。

    Args:
        tmp_path (`Path`): pytest 给的临时目录（每个测试一个）。

    Yields:
        `tuple[MemorySearch, ReMeWorkspace]`: 检索器与工作区。
    """
    workspace = ReMeWorkspace(root=tmp_path / "ws")
    workspace.ensure()
    client = MemoryClient(
        HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None)
        .with_jobs("search", "traverse", "reindex")
        .with_components(file_chunker={"markdown": {"chunk_byte_size": 200}})
        .build(),
    )
    await client.start()
    try:
        ingestor = MemoryIngestor(client, workspace=workspace, chunker="markdown")
        for name, text, tags in CORPUS:
            await ingestor.add_text(text, name=name, tags=tags)
        yield MemorySearch(client, workspace=workspace), workspace
    finally:
        await client.aclose()


def fake_chunk(key: str, scores: dict[str, float]) -> FileChunk:
    """造一个 ``id`` 确定、``scores`` 可控的 ``FileChunk``（不碰任何组件）。

    Args:
        key (`str`): 后缀，用来生成 path。
        scores (`dict[str, float]`): 该 chunk 的分数字典。

    Returns:
        `FileChunk`: 已 ``set_hash_id()`` 的 chunk。
    """
    item = FileChunk(path=f"resource/{key}.md", start_line=1, end_line=2, text=f"text-{key}")
    item.scores = dict(scores)
    return item.set_hash_id()


# ======================================================================
# A 组：融合层（纯计算）
# ======================================================================
def test_rrf_k_is_the_same_constant_as_reme() -> None:
    """``RRF_K`` 必须与 ReMe 的 ``_RRF_K`` 一致（只有一处真值）。"""
    assert RRF_K == 60
    assert HybridRetriever.RRF_K == RRF_K
    # ReMe 的 ``_RRF_K`` 是模块级 ``Final``，不挂在类上；直接读模块属性对齐。
    from reme.steps.index import search as reme_search

    assert reme_search._RRF_K == RRF_K
    assert reme_search._MAX_CANDIDATES == 200
    # ``_rrf_merge`` 是 staticmethod，取到类属性时已经是普通函数，可直接当纯函数调。
    assert callable(SearchStep._rrf_merge)


def test_hand_computed_rrf_matches_fuse() -> None:
    """手算的 RRF 与 ``fuse()`` 必须逐条相等（含"只出现在一路"的情况）。"""
    weight = 0.7
    vector = [("b", 0.88), ("c", 0.71), ("a", 0.55)]
    keyword = [("a", 12.5), ("d", 9.1), ("b", 3.2)]
    fused = dict(HybridRetriever(vector_weight=weight).fuse(keyword, vector))
    assert set(fused) == {"a", "b", "c", "d"}
    # 名次（1-based）：向量路 b=1 c=2 a=3；关键词路 a=1 d=2 b=3
    expected = {
        "b": weight / (RRF_K + 1) + (1 - weight) / (RRF_K + 3),
        "a": weight / (RRF_K + 3) + (1 - weight) / (RRF_K + 1),
        "c": weight / (RRF_K + 2),
        "d": (1 - weight) / (RRF_K + 2),
    }
    for key, score in expected.items():
        assert fused[key] == pytest.approx(score, abs=1e-12)
    # 降序：b(两项都加) > a(关键词第 1) > c(向量第 2) > d(关键词第 2)
    assert [key for key, _ in HybridRetriever(vector_weight=weight).fuse(keyword, vector)] == [
        "b",
        "a",
        "c",
        "d",
    ]


def test_fuse_matches_reme_static_merge() -> None:
    """harness 的 ``fuse()`` 与 ReMe ``SearchStep._rrf_merge`` 的分数逐条相同。

    这条测试的价值：harness 的 hybrid.py 声称自己"逐字复刻" ReMe 的融合，
    那就必须**对着 ReMe 本人的实现**验证，而不是对着自己手抄的公式验证。
    """
    weight = 0.7
    vector = [("b", 0.88), ("c", 0.71), ("a", 0.55)]
    keyword = [("a", 12.5), ("d", 9.1), ("b", 3.2)]
    mine = dict(HybridRetriever(vector_weight=weight).fuse(keyword, vector))
    reme = SearchStep._rrf_merge(
        [fake_chunk(key, {"vector": raw}) for key, raw in vector],
        [fake_chunk(key, {"keyword": raw}) for key, raw in keyword],
        weight,
    )
    theirs = {item.path.split("/")[-1].split(".")[0]: item.score for item in reme}
    assert set(theirs) == set(mine)
    for key in mine:
        assert mine[key] == pytest.approx(theirs[key], abs=1e-12)


def test_three_state_fusion_modes() -> None:
    """三态是**批级**判定：两路都有才进 RRF，只有一路就原样返回。"""
    retriever = HybridRetriever(vector_weight=0.5)
    keyword = [("a", 12.5), ("b", 9.1)]
    vector = [("b", 0.88), ("c", 0.71)]
    assert retriever.mode(keyword, vector) == "rrf"
    assert retriever.mode(keyword, []) == "keyword_only"
    assert retriever.mode([], vector) == "vector_only"
    assert retriever.mode([], []) == "empty"
    # 单路时**不融合**：返回的就是原始分
    assert retriever.fuse(keyword, []) == keyword
    assert retriever.fuse([], vector) == vector
    assert retriever.fuse([], []) == []


def test_vector_weight_endpoints_skip_a_branch() -> None:
    """``0.0`` / ``1.0`` 是"整路跳过"，不是"权重为 0"。"""
    keyword = [("a", 12.5)]
    vector = [("b", 0.88)]
    assert HybridRetriever(vector_weight=0.0).mode(keyword, vector) == "keyword_only"
    assert HybridRetriever(vector_weight=1.0).mode(keyword, vector) == "vector_only"
    assert HybridRetriever(vector_weight=0.0).text_weight == 1.0
    assert HybridRetriever(vector_weight=1.0).text_weight == 0.0


def test_vector_weight_out_of_range_raises() -> None:
    """越界权重必须在构造时就炸，而不是算出个没人看得懂的数。"""
    for bad in (-0.01, 1.01):
        with pytest.raises(ValueError):
            HybridRetriever(vector_weight=bad)


def test_fused_entry_is_a_value_object() -> None:
    """``FusedEntry`` 是轻量值对象：可比较、可哈希、能转成二元组。"""
    one = FusedEntry("k", 0.5, "rrf", rank_vector=1, raw_vector=0.9)
    two = FusedEntry("k", 0.5, "rrf")
    assert one == two
    assert one.as_tuple() == ("k", 0.5)
    # 只有 (key, score, mode) 参与判等与哈希；明细字段不影响身份。
    assert len({one, two}) == 1
    assert one != FusedEntry("k", 0.5, "keyword_only")
    assert repr(one).startswith("FusedEntry(key='k', score=0.500000, mode='rrf'")
    assert one.rank_keyword is None and one.raw_keyword is None


def test_explain_mentions_max_score_magnitude() -> None:
    """``explain()`` 必须把"融合分的量级"讲出来（那是本讲的头号坑）。"""
    text = HybridRetriever(vector_weight=0.5).explain()
    assert "RRF_K = 60" in text
    assert "0.0164" in text
    assert "min_score" in text
    assert "仅关键词" in HybridRetriever(vector_weight=0.0).explain()
    assert "仅向量" in HybridRetriever(vector_weight=1.0).explain()


# ======================================================================
# B 组：citations（纯计算）
# ======================================================================
def test_to_memory_hit_source_three_states() -> None:
    """``source`` 由 ``scores`` 里有哪些键推导 —— 三态都覆盖。"""
    both = to_memory_hit(
        {"id": "x", "path": "a.md", "start_line": 1, "end_line": 2, "text": "t",
         "scores": {"vector": 0.9, "keyword": 3.0, "score": 0.016}},
    )
    only_k = to_memory_hit(
        {"id": "y", "path": "b.md", "start_line": 1, "end_line": 2, "text": "t",
         "scores": {"keyword": 3.0, "score": 3.0}},
    )
    only_v = to_memory_hit(
        {"id": "z", "path": "c.md", "start_line": 1, "end_line": 2, "text": "t",
         "scores": {"vector": 0.9, "score": 0.9}},
    )
    assert both.source == "fused"
    assert only_k.source == "keyword"
    assert only_v.source == "vector"
    with pytest.raises(ValueError):
        to_memory_hit({"id": "w", "text": "没有 path"})


def test_to_memory_hits_derives_ranks_from_branch_scores() -> None:
    """名次是 harness **派生**的：按各路原始分排序取序号（ReMe 不提供名次）。"""
    rows = [
        {"id": "1", "path": "a.md", "start_line": 1, "end_line": 1, "text": "a",
         "scores": {"vector": 0.5, "keyword": 9.0, "score": 0.016}},
        {"id": "2", "path": "b.md", "start_line": 1, "end_line": 1, "text": "b",
         "scores": {"vector": 0.9, "score": 0.011}},
        {"id": "3", "path": "c.md", "start_line": 1, "end_line": 1, "text": "c",
         "scores": {"keyword": 12.0, "score": 12.0}},
    ]
    hits = to_memory_hits(rows)
    assert (hits[0].rank_keyword, hits[0].rank_vector) == (2, 2)
    assert (hits[1].rank_keyword, hits[1].rank_vector) == (None, 1)
    assert (hits[2].rank_keyword, hits[2].rank_vector) == (1, None)
    assert [hit.source for hit in hits] == ["fused", "vector", "keyword"]


def test_refuse_reranks_offline_without_touching_the_input() -> None:
    """``refuse()`` 只用名次离线重算 RRF：换权重就换第一名，且不动入参。

    构造让两条命中在各路的强弱**正好相反**（a 关键词强、b 向量强），
    这样"权重一动、第一名就换人"是肉眼可见的。
    """
    rows = [
        {"id": "a", "path": "a.md", "start_line": 1, "end_line": 1, "text": "a",
         "scores": {"keyword": 12.0, "vector": 0.50, "score": 0.0161}},
        {"id": "b", "path": "b.md", "start_line": 1, "end_line": 1, "text": "b",
         "scores": {"keyword": 9.0, "vector": 0.90, "score": 0.0163}},
    ]
    hits = to_memory_hits(rows)
    assert [(hit.rank_keyword, hit.rank_vector) for hit in hits] == [(1, 2), (2, 1)]
    before = [hit.score for hit in hits]

    text_heavy = HybridRetriever(vector_weight=0.2).refuse(hits)
    vector_heavy = HybridRetriever(vector_weight=0.8).refuse(hits)
    assert [hit.path for hit in text_heavy] == ["a.md", "b.md"]
    assert [hit.path for hit in vector_heavy] == ["b.md", "a.md"]
    # 重算的分数与手算的 RRF 一致（名次没变，只有权重变了）。
    assert text_heavy[0].score == pytest.approx(0.2 / (RRF_K + 2) + 0.8 / (RRF_K + 1))
    assert vector_heavy[0].score == pytest.approx(0.8 / (RRF_K + 1) + 0.2 / (RRF_K + 2))
    # 零额外 IO：入参本身一个字节都没改（它们是 file_store 里的共享对象）。
    assert [hit.score for hit in hits] == before
    assert {hit.chunk_id for hit in text_heavy} == {hit.chunk_id for hit in hits}

    # 端点权重下 ReMe 会整路跳过，两路都凑不齐 -> 不是 rrf 三态，refuse 原样返回。
    assert HybridRetriever(vector_weight=0.0).refuse(hits) == hits
    assert HybridRetriever(vector_weight=1.0).refuse(hits) == hits

    with pytest.raises(TypeError):
        HybridRetriever().refuse([{"not": "a hit"}])
    assert HybridRetriever().refuse([]) == []


def test_merge_intervals_uses_gap_and_orders() -> None:
    """行区间合并：不相交、按起点排序、``gap`` 控制"多近算相邻"。"""
    chunks = [
        fake_chunk("a", {}).model_copy(update={"start_line": 30, "end_line": 40}),
        fake_chunk("b", {}).model_copy(update={"start_line": 1, "end_line": 10}),
        fake_chunk("c", {}).model_copy(update={"start_line": 9, "end_line": 12}),
    ]
    assert merge_intervals(chunks) == [(1, 12), (30, 40)]
    assert merge_intervals(chunks, gap=20) == [(1, 40)]
    assert merge_intervals([]) == []
    blank = fake_chunk("d", {}).model_copy(update={"start_line": 0, "end_line": 0})
    assert merge_intervals([blank]) == []


def test_citation_builder_dedups_truncates_and_renders() -> None:
    """同 ``chunk_id`` 只出一条；超长摘录截断并补省略号；渲染格式可核对。"""
    rows = [
        {"id": "same", "path": "daily/2026-09-21.md", "start_line": 3, "end_line": 6,
         "text": "深色主题的 matplotlib 图表，" * 20, "scores": {"score": 0.9}},
        {"id": "same", "path": "daily/2026-09-21.md", "start_line": 3, "end_line": 6,
         "text": "重复出现的同一条", "scores": {"score": 0.9}},
        {"id": "blank", "path": "daily/2026-09-22.md", "start_line": 1, "end_line": 2,
         "text": "   \n ", "scores": {"score": 0.5}},
    ]
    hits = to_memory_hits(rows)
    builder = CitationBuilder(max_quote_chars=30)
    citations = builder.build(hits)
    assert len(citations) == 1, "重复 id 与空文本都不该成为引用"
    assert isinstance(citations[0], Citation)
    assert citations[0].quote.endswith("…") and len(citations[0].quote) == 31
    lines = builder.render_lines(citations)
    assert lines == [f"[1] daily/2026-09-21.md:3-6  {citations[0].quote}"]
    with pytest.raises(ValueError):
        CitationBuilder(max_quote_chars=0)


# ======================================================================
# C 组：budget（纯计算）
# ======================================================================
def simple_hits(lengths: list[int]) -> list[Any]:
    """造一批正文长度可控的 ``MemoryHit``。

    Args:
        lengths (`list[int]`): 每条的字符数。

    Returns:
        `list[Any]`: ``MemoryHit`` 列表。
    """
    return to_memory_hits(
        [
            {
                "id": f"id-{index}",
                "path": f"resource/doc{index}.md",
                "start_line": index + 1,
                "end_line": index + 2,
                "text": "记" * length,
                "scores": {"score": 1.0 - index * 0.1},
            }
            for index, length in enumerate(lengths)
        ],
    )


def test_estimate_tokens_heuristic_counts_cjk_per_char() -> None:
    """CJK 按 1 字符 1 token（偏保守），非 CJK 按 4 字符 1 token。"""
    assert estimate_tokens_heuristic("") == 0
    assert estimate_tokens_heuristic("中文") == 2
    assert estimate_tokens_heuristic("abcd") == 1
    assert estimate_tokens_heuristic("abcde") == 2  # 向上取整，不低估
    # 混合：2 个 CJK + 4 个 ASCII
    assert estimate_tokens_heuristic("中文abcd") == 2 + 1


def test_budget_estimated_tokens_matches_what_would_be_injected() -> None:
    """``estimated_tokens`` 与 ``render()`` 必须同源 —— 否则预算就是假的。"""
    hits = simple_hits([40, 40, 40])
    budget = MemoryBudget(max_tokens=200)
    result = budget.fit(hits)
    assert result.kept, "200 tokens 至少应装下一条"
    assert result.estimated_tokens == estimate_tokens_heuristic(render_memory_block(result.kept))
    assert result.estimated_tokens == estimate_tokens_heuristic(result.render())
    assert result.estimated_tokens <= 200
    assert result.budget_tokens == 200


def test_budget_keeps_a_greedy_prefix() -> None:
    """裁剪是保序的：kept 是输入的高分前缀，dropped 是剩下的后缀。"""
    hits = simple_hits([60, 60, 60, 60])
    result = MemoryBudget(max_tokens=200).fit(hits)
    assert result.truncated is True
    assert result.kept == hits[: len(result.kept)]
    assert result.dropped == hits[len(result.kept) :]
    assert len(result.kept) + len(result.dropped) == len(hits)
    assert result.per_hit_tokens and all(token > 0 for token in result.per_hit_tokens)


def test_budget_zero_drops_everything() -> None:
    """``max_tokens <= 0`` 表示"不允许注入任何记忆"，且必须显式标记截断。"""
    hits = simple_hits([10, 10])
    result = MemoryBudget(max_tokens=0).fit(hits)
    assert result.kept == [] and result.dropped == hits
    assert result.truncated is True and result.estimated_tokens == 0
    assert MemoryBudget(max_tokens=0).fits([]) is True


def test_budget_rejects_non_int_and_bool() -> None:
    """``max_tokens=True`` 显然是手滑：``bool`` 要单独拒掉（它是 ``int`` 的子类）。"""
    with pytest.raises(ValueError):
        MemoryBudget(max_tokens=True)
    with pytest.raises(ValueError):
        MemoryBudget(max_tokens=1.5)  # type: ignore[arg-type]


def test_budget_fit_or_truncate_keeps_a_floor() -> None:
    """第一条就超预算时降级为"留一条并截断"，且**保留来源信息**。"""
    hits = simple_hits([5000])
    tight = MemoryBudget(max_tokens=60)
    assert tight.fit(hits).kept == [], "严格预算下一条都装不下"
    degraded = tight.fit_or_truncate(hits, min_hits=1)
    assert len(degraded.kept) == 1 and degraded.truncated is True
    tail = degraded.kept[0]
    assert tail.text.endswith("…（已截断）")
    assert tail.path == hits[0].path and tail.chunk_id == hits[0].chunk_id
    assert tail.start_line == hits[0].start_line and tail.end_line == hits[0].end_line


def test_render_memory_block_is_empty_for_unusable_hits() -> None:
    """没有正文的 hit 渲染出来是空串 —— 而不是一个只有标题的空块。"""
    assert render_memory_block([]) == ""
    assert render_memory_block(simple_hits([0])) == ""
    block = render_memory_block(simple_hits([3]))
    assert block.startswith("## 相关长期记忆")
    assert block.startswith("## 相关长期记忆")
    assert "### resource/doc0.md:1-2" in block


# ======================================================================
# D 组：真实召回（嵌入式 ReMe，0 次 LLM）
# ======================================================================
async def test_search_validates_arguments_before_touching_reme(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """参数校验发生在调 ReMe 之前，且是友好异常不是 ``AssertionError``。"""
    search, _ws = memory
    with pytest.raises(ValueError, match="query 不能为空"):
        await search.search("   ")
    with pytest.raises(ValueError, match="limit"):
        await search.search("回滚", limit=0)
    with pytest.raises(ValueError, match="vector_weight"):
        await search.search("回滚", vector_weight=1.5)
    with pytest.raises(ValueError, match="candidate_multiplier"):
        await search.search("回滚", candidate_multiplier=0)
    with pytest.raises(ValueError, match="tool_context_id"):
        await search.search("回滚", max_search_calls=2)


async def test_search_keyword_only_when_no_embedding(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """没配 embedding 时：向量路答空、``hybrid=False``、``success`` 仍为 True。"""
    search, _ws = memory
    result = await search.search("回滚 停流量", limit=3)
    assert result.counts["vector"] == 0
    assert result.counts["keyword"] > 0
    assert result.hybrid is False
    assert result.hits and all(hit.source == "keyword" for hit in result.hits)
    assert result.hits[0].path == "resource/runbook.md"
    assert result.hits[0].start_line >= 1 and result.hits[0].end_line >= result.hits[0].start_line
    assert result.elapsed_ms > 0
    assert result.query == "回滚 停流量"


async def test_min_score_filters_fused_scale(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """``min_score`` 比的是**融合后**的 ``c.score``：阈值必须按当前路径的量纲给。

    这条测试刻意**不写死数字**，而是先用 ``min_score=0`` 探出本语料真实的
    BM25 分数，再用它上下各取 1% 当阈值 —— 因为 BM25 的分值取决于语料
    （命中几次、文档多长、idf 多大），写死 "6.0" 只是把当时的语料固化进测试。
    要断言的语义是"阈值卡在 top1 的分数之上就只剩 0 条"，与数值无关。
    """
    search, _ws = memory
    baseline = await search.search("回滚 停流量", limit=5, min_score=0.0)
    assert baseline.hits and baseline.hybrid is False
    top = baseline.hits[0].score
    assert top > 0.0
    kept = await search.search("回滚 停流量", limit=5, min_score=top * 0.99)
    dropped = await search.search("回滚 停流量", limit=5, min_score=top * 1.01)
    assert kept.hits[0].chunk_id == baseline.hits[0].chunk_id
    assert len(kept.hits) <= len(baseline.hits)
    assert dropped.hits == []
    # 关键点：召回本身没变（keyword 计数还在），只是被阈值滤掉了 ——
    # 于是 counts["returned"] == 0 而 counts["keyword"] > 0，这个组合是"阈值给太高"
    # 的指纹，而不是"索引里没东西"。
    assert dropped.counts["keyword"] == baseline.counts["keyword"] > 0
    assert dropped.counts["returned"] == 0
    print(f"\n  min_score 量纲：本语料 BM25 top1 = {top:.4f}；阈值 {top * 1.01:.4f} 时 0 条")


async def test_tag_filter_is_or_semantics_and_unknown_tag_is_not_an_error(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """标签过滤是 OR；未知标签只导致 0 条命中，不抛异常。"""
    search, _ws = memory
    ops = await search.search("回滚 部署", limit=10, tags=["ops"])
    pref = await search.search("回滚 部署", limit=10, tags=["pref"])
    assert {"resource/runbook.md", "resource/deploy.md"} <= {hit.path for hit in ops.hits}
    assert {hit.path for hit in pref.hits} == {"resource/pref.md"} or pref.hits == []
    either = await search.search("回滚 部署", limit=10, tags=["ops", "pref"])
    assert len(either.hits) >= len(ops.hits)
    nothing = await search.search("回滚", limit=5, tags=["no-such-tag"])
    assert nothing.hits == [] and nothing.counts["returned"] == 0


async def test_traverse_reads_answer_not_metadata(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """``traverse`` 的结果在 ``answer`` 里（与 ``search`` 正好相反）。"""
    search, _ws = memory
    graph = await search.traverse_graph(start="resource/deploy.md", depth=1)
    assert graph["seeds"] == ["resource/deploy.md"]
    assert {node["path"] for node in graph["nodes"]} == {
        "resource/deploy.md",
        "resource/runbook.md",
    }
    depths = {node["path"]: node["depth"] for node in graph["nodes"]}
    assert depths["resource/deploy.md"] == 0 and depths["resource/runbook.md"] == 1
    chunks = await search.traverse(start="resource/deploy.md", depth=1)
    assert {chunk.path for chunk in chunks} == {
        "resource/deploy.md",
        "resource/runbook.md",
    }
    assert chunks[0].path == "resource/deploy.md", "起点（depth=0）排最前"
    assert all(chunk.text for chunk in chunks)
    with pytest.raises(ValueError):
        await search.traverse(start="  ", depth=1)
    with pytest.raises(ValueError):
        await search.traverse(start="resource/deploy.md", depth=-1)


async def test_vector_weight_one_without_embedding_is_silent_empty(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """反面教材：``vector_weight=1.0`` 但没配 embedding -> 空结果、不报错。"""
    search, _ws = memory
    result = await search.search("回滚 停流量", limit=3, vector_weight=1.0)
    assert result.hits == []
    assert result.counts == {"vector": 0, "keyword": 0, "returned": 0, "hybrid": 0}
    assert result.hybrid is False


async def test_candidate_multiplier_shrinks_the_pool_via_direct_step(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """``candidate_multiplier`` 只能通过直连 step 覆盖，且真的会改变召回数。"""
    search, _ws = memory
    wide = await search.search("回滚 停流量 部署", limit=1, candidate_multiplier=5.0)
    narrow = await search.search("回滚 停流量 部署", limit=1, candidate_multiplier=1.0)
    assert wide.counts["keyword"] > narrow.counts["keyword"]
    assert narrow.counts["keyword"] == 1


async def test_tool_context_dedup_and_call_budget(
    memory: tuple[MemorySearch, ReMeWorkspace],
) -> None:
    """同一 ``tool_context_id`` 内：重复的 chunk 不再返回，调用次数超限会抛异常。"""
    search, _ws = memory
    first = await search.search("回滚", limit=5, tool_context_id="turn-1")
    assert first.hits
    second = await search.search("回滚", limit=5, tool_context_id="turn-1")
    assert second.hits == [] and second.counts["keyword"] > 0
    fresh = await search.search("回滚", limit=5, tool_context_id="turn-2")
    assert len(fresh.hits) == len(first.hits)
    await search.search("回滚", limit=2, tool_context_id="turn-3", max_search_calls=1)
    with pytest.raises(MemoryJobError):
        await search.search("回滚", limit=2, tool_context_id="turn-3", max_search_calls=1)
