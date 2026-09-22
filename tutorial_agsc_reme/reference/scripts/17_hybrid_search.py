#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第 17 讲《记忆检索：BM25 + 向量 + 标签 + 图扩展》验证脚本。

跑法（在仓库根，或任何地方用绝对路径）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/17_hybrid_search.py

加 ``--live`` 会多跑 F 段（真实 deepseek-flash 用检索结果回答一次，实测 1 次补全调用）。

六段的结构与「消耗几次模型调用」：

===  ==============================================================  ============
段   内容                                                            模型调用
===  ==============================================================  ============
A    融合层纯函数：RRF 手算 / 三态 / 离线重排 / 公式自解释              0（纯计算）
B    单路 BM25：真实 search job、candidates 上限、min_score、标签、去重 0（本地 ReMe）
C    图扩展：traverse 取 answer 里的图、回查 chunk、expand_links 渲染    0
D    注入确定性假 embedder → 真·混合检索（RRF）                        0（假 embedder）
E    引用与预算：CitationBuilder / merge_intervals / MemoryBudget       0
F    （``--live``）把 E 段渲染出的注入块交给真实 deepseek-flash          1（实测）
===  ==============================================================  ============

**A~E 段全部离线**：ReMe 是本地嵌入式装配（``reme.ReMe(**config)`` + ``run_job``，
既不起 HTTP 服务也不占端口），LLM 组件装配上了但一次都不调用。

**F 段是唯一花钱的一段**：一次 ``OpenAICompatChatModel`` 调用。

**为什么 D 段要注入假 embedder**：本仓库的 ``.env`` 只有 DeepSeek 的 key，
而 DeepSeek **不提供** ``/embeddings`` 端点（实测 ``POST {base}/embeddings`` 返回
HTTP 404）。所以真实向量通路在这里必然答空 —— 这不是 harness 的问题，是供应商的问题。
D 段的做法是注入一个**字符哈希的确定性 embedder**，装配路径、``vector_search``、
``_rrf_merge``、metadata 全部走真实代码，只有「向量从哪来」这一件事被替换掉。
教程里会明确标注：**真实 embedding 供应商的端到端检索在本环境未验证**。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# 路径与 .env
# ----------------------------------------------------------------------
#: ``<repo>/tutorial_agsc_reme/reference``
REF: Path = Path(__file__).resolve().parents[1]
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根
REPO: Path = REF.parents[1]
#: 本地 ReMe 克隆必须排在 ``sys.path`` 最前（压住 site-packages 里的 0.3.1.10）
REME_SRC: Path = REPO / "third_party" / "ReMe"

for _candidate in (str(REME_SRC), str(REF)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:  # python-dotenv 是 pyproject 里声明的依赖
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:  # pragma: no cover - 本环境已装
    pass

import numpy as np  # noqa: E402
from loguru import logger  # noqa: E402

#: 默认 INFO 日志会把每一次检索都打出来；A~E 段是离线断言，压到 WARNING。
logger.remove()
logger.add(sys.stderr, level="WARNING")

from agentscope.message import UserMsg  # noqa: E402

from reme.enumeration import ComponentEnum  # noqa: E402
from reme.schema.file_chunk import FileChunk  # noqa: E402
from reme.steps.index.search import SearchStep  # noqa: E402

from harness_kit.memory import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    RRF_K,
    CitationBuilder,
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
    to_memory_hits,
)

#: 是否跑真实模型那一段。
LIVE: bool = "--live" in sys.argv

def model_name() -> str:
    """当前要用的模型名（``.env`` 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。

    **为什么是函数而不是模块级常量**：``.env`` 可能是在 import 之后才被
    ``harness_kit.settings.Settings.from_env()`` 灌进 ``os.environ`` 的
    （它自己会 ``load_dotenv`` 一次），模块级常量会在这之前就把名字定死，
    于是日志里打出来的模型名和真正调用的模型名可能不是同一个。

    Returns:
        `str`: 模型名。
    """
    return os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-chat"

#: 全部 ``ReMeWorkspace`` 的落地根（跑完不删，方便读者去看真实文件）。
SANDBOX: Path = Path(tempfile.mkdtemp(prefix="lesson17_")).resolve()

#: 本脚本建过的所有客户端，``main`` 统一收尾（ReMe 的组件有后台任务）。
_CLIENTS: list[MemoryClient] = []


def banner(title: str) -> None:
    """打一条段落标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, value: Any) -> None:
    """打一行 ``标签 = 值``。

    Args:
        label (`str`): 标签。
        value (`Any`): 值。
    """
    print(f"  {label:26s} = {value}")


# ======================================================================
# 夹具：一个隔离的嵌入式 ReMe
# ======================================================================
class FakeEmbedder:
    """**确定性**的假 embedder：字符哈希词袋 + L2 归一，不联网、可复现。

    它的形状是照着 ``reme/components/as_embedding/__init__.py:23`` 的
    ``BaseAsEmbedding`` 的**对外契约**做的（``dimensions`` / ``vector_space_id``
    / ``initialize_model`` / ``async __call__``），但不继承它 ——
    因为 ``LocalEmbeddingStore`` 在 :func:`LocalEmbeddingStore.get_embeddings`
    里只按这四个成员鸭子调用，而 ``Application.update_component`` 是
    ``setattr``（``application.py:235-248``），不做 isinstance 校验。

    为什么需要它：本环境没有可用的 embedding 供应商（DeepSeek 无 ``/embeddings``），
    而向量通路必须验证。用它替换掉 ``as_embedding`` 之后，
    ``vector_search`` / ``_rrf_merge`` / metadata 全是真实代码。

    Attributes:
        DIM (`int`): 向量维度，必须与 ``component.embedding_dimensions`` 一致，
            否则 ``_embedding_dim_matches`` 会把每条向量都判成 stale。
        calls (`int`): 真实发生的 ``__call__`` 次数（用来验证批处理与 LRU 缓存）。
    """

    DIM: int = 64

    def __init__(self, space: str = "lesson17-fake-charhash-v1") -> None:
        """初始化。

        Args:
            space (`str`): 向量空间名；``vector_space_id`` 是它的哈希
                （``local_embedding_store.py:54`` 的缓存文件名要用它）。
        """
        self.calls: int = 0
        self._space: str = space

    @property
    def dimensions(self) -> int:
        """向量维度。

        Returns:
            `int`: 维度。
        """
        return self.DIM

    @property
    def vector_space_id(self) -> str:
        """向量空间指纹（12 位十六进制）。

        Returns:
            `str`: 指纹。
        """
        return hashlib.sha256(self._space.encode()).hexdigest()[:12]

    def initialize_model(self) -> None:
        """假实现：没有要初始化的模型。"""
        return None

    async def __call__(self, inputs: list[str], **kwargs: Any) -> list[list[float]]:
        """把一批文本编码成 64 维单位向量。

        Args:
            inputs (`list[str]`): 待编码文本。
            **kwargs (`Any`): 真实后端会用到（如 ``dimensions``），这里忽略。

        Returns:
            `list[list[float]]`: 与 ``inputs`` 等长的向量列表。
        """
        self.calls += 1
        out: list[list[float]] = []
        for text in inputs:
            vec = np.zeros(self.DIM, dtype=np.float32)
            for char in text:
                if char.strip():
                    vec[int(hashlib.md5(char.encode()).hexdigest(), 16) % self.DIM] += 1.0
            norm = float(np.linalg.norm(vec))
            out.append((vec / norm).tolist() if norm else vec.tolist())
        return out


async def make_client(
    name: str,
    *,
    embedding_dimensions: int | None = None,
    **components: Any,
) -> tuple[MemoryClient, ReMeWorkspace]:
    """起一个隔离的嵌入式 ReMe（**不占端口、不起服务**）。

    Args:
        name (`str`): 工作区子目录名，每个段落一个，互不干扰。
        embedding_dimensions (`int | None`): ``None`` = 不装配 embedding
            （D 段之外都用它）；给了就装配向量组件（``harness_kit/memory/config.py:473``）。
        **components (`Any`): 额外的组件覆盖，透传 ``with_components``。

    Returns:
        `tuple[MemoryClient, ReMeWorkspace]`: 已 start 的客户端与工作区。
    """
    workspace = ReMeWorkspace(root=SANDBOX / name)
    workspace.ensure()
    builder = HarnessMemoryConfig(
        workspace=workspace,
        embedding_dimensions=embedding_dimensions,
    ).with_jobs(
        # 本讲只用到检索侧，白名单按「实际要跑的 job」给。
        # 这里的每个名字都在 ``third_party/ReMe/reme/config/default.yaml`` 里，
        # 且后端都不是 background / cron —— 否则 with_jobs 会抛
        # MemoryConfigError（那是第 15 讲的重点）。
        "search",
        "traverse",
        "reindex",
        "read",
        "node_search",
    )
    if components:
        builder = builder.with_components(**components)
    client = MemoryClient(builder.build())
    await client.start()
    _CLIENTS.append(client)
    return client, workspace


async def close_all() -> None:
    """把所有客户端关掉（ReMe 的 ``aclose`` 会等后台任务退出）。"""
    for client in _CLIENTS:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 收尾失败不该盖住真正的断言
            logger.warning("aclose 失败: {}", exc)
    _CLIENTS.clear()


# ======================================================================
# 语料：4 个 Markdown 文件，故意带上标签与 wikilink
# ======================================================================
DEPLOY_DOC = """---
name: 部署手册
description: 上线前的检查清单
memory_tags: [ops, deploy]
---

# 部署手册

总览段：上线分三步走 —— 预检、切换、观察。

## 预检

预检要跑 `uv sync` 与 `pytest -q`，确认依赖锁文件与测试全绿。

## 切换

切换用蓝绿发布，切换前必须确认 [[resource/runbook.md#回滚]] 可执行。
"""

RUNBOOK_DOC = """# 回滚手册

回滚三步：停流量、回滚镜像 tag、验单。

停流量用网关切到维护页，回滚镜像用上一个 tag。
"""

PREF_DOC = """# 绘图偏好

用户偏好深色主题的 matplotlib 图表，坐标轴标签用中文。

配色优先用 viridis，避免红绿同时出现。
"""

RETRIEVAL_DOC = """# 检索笔记

BM25 适合精确词匹配，向量适合语义近似。

融合两路召回用 RRF，K 取 60。部署相关的笔记见 [[resource/deploy.md]]。
"""

#: 4 篇文档，``add_text`` 的 ``name`` 决定落地路径 ``resource/<name>.md``。
CORPUS: list[tuple[str, str, list[str]]] = [
    ("deploy", DEPLOY_DOC, ["ops", "deploy"]),
    ("runbook", RUNBOOK_DOC, ["ops", "rollback"]),
    ("pref", PREF_DOC, ["pref"]),
    ("retrieval", RETRIEVAL_DOC, ["memory"]),
]

#: ``chunk_byte_size`` 调到 200 是为了让十来行的文档也能切出多个 chunk
#: （默认值 10000 见 ``components/file_chunker/default_file_chunker.py:25``，
#: 那个大小下一份小文档只会有一个 chunk，行锚点与区间合并就没得看了）。
#: 形状是 ``with_components(**SMALL_CHUNKER)`` 要的「组件类型 → 实例名 → 字段」。
SMALL_CHUNKER: dict[str, Any] = {"file_chunker": {"markdown": {"chunk_byte_size": 200}}}


async def build_corpus(client: MemoryClient, ws: ReMeWorkspace) -> MemoryIngestor:
    """把一个 4 篇文档的语料写进工作区。

    Args:
        client (`MemoryClient`): 已 start 的客户端。
        ws (`ReMeWorkspace`): 工作区。

    Returns:
        `MemoryIngestor`: 写入器（后面几个段落还要用它取分块器）。
    """
    ingestor = MemoryIngestor(client, workspace=ws, chunker="markdown")
    for name, text, tags in CORPUS:
        result = await ingestor.add_text(text, name=name, tags=tags)
        print(f"  {result.path:26s} added={result.added!s:5s} chunks={result.chunk_count}")
    return ingestor


# ======================================================================
# A · 融合层纯函数
# ======================================================================
def section_a() -> None:
    """A 段：RRF 手算 / 三态 / 离线重排，全部是纯计算。"""
    banner("A · 融合层：RRF 手算 / 三态融合 / 离线重排（0 次 ReMe、0 次模型调用）")

    print("\n--- A1 RRF 手算 vs harness.fuse vs ReMe._rrf_merge（三者必须一致）---")
    assert RRF_K == 60, RRF_K
    print(f"  harness RRF_K = {RRF_K}")
    # 两路结果：向量路 b > c > a，关键词路 a > d > b。
    vector = [("b", 0.88), ("c", 0.71), ("a", 0.55)]
    keyword = [("a", 12.5), ("d", 9.1), ("b", 3.2)]
    weight = 0.7  # 与 config/default.yaml:475 的 search step 配置一致
    retriever = HybridRetriever(vector_weight=weight)
    print(f"  向量路（降序）  = {vector}")
    print(f"  关键词路（降序）= {keyword}")
    print(f"  vector_weight   = {weight}（ReMe 的 search step 默认，default.yaml:475）")

    fused = retriever.fuse(keyword, vector)
    print("  fuse() 结果：")
    for key, score in fused:
        print(f"    {key}  {score:.6f}")

    # 手算：只有出现在该分支里才加分，且只用名次（rank 从 1 开始）
    ranks = {  # 每个 key 在两路里的名次（None = 没出现在那一路）
        # 向量路降序是 b, c, a；关键词路降序是 a, d, b
        "b": (1, 3),
        "c": (2, None),
        "a": (3, 1),
        "d": (None, 2),
    }
    print("  手算（fused(d) = w_v/(K+rank_v) + w_k/(K+rank_k)，rank 从 1 开始）：")
    for key, (rank_v, rank_k) in sorted(ranks.items()):
        hand = (weight / (RRF_K + rank_v) if rank_v else 0.0) + (
            (1.0 - weight) / (RRF_K + rank_k) if rank_k else 0.0
        )
        got = dict(fused)[key]
        print(f"    {key}  rank_v={rank_v} rank_k={rank_k}  手算={hand:.6f}  fuse={got:.6f}")
        assert abs(hand - got) < 1e-12, (key, hand, got)
    assert [key for key, _ in fused] == ["b", "a", "c", "d"], fused
    print("  >>> 两个反直觉的结论，都在这一张表里：")
    print("      (1) **只有出现在该分支里才加分** —— b 是向量路第 1、关键词路第 3，")
    print("          两项都加，所以夺冠；a 是关键词路第 1，但向量路只排第 3。")
    print("      (2) **只看名次、不看原始分** —— c（向量路 rank 2，0.011290）排在")
    print("          d（关键词路 rank 2，0.004839）前面，而 d 的原始 BM25 分是 9.1、")
    print("          c 的原始 cosine 分只有 0.71，差了 13 倍。")
    print("          这就是 RRF 的核心价值：两路的量纲差异被彻底消掉，不需要任何归一化。")

    # 再用 ReMe 自己的静态方法算一遍：harness 说自己是「逐字复刻」，这里钉死它。
    def chunk(key: str, scores: dict[str, float]) -> FileChunk:
        item = FileChunk(path=f"resource/{key}.md", start_line=1, end_line=2, text=f"text-{key}")
        item.scores = dict(scores)
        return item.set_hash_id()

    v_chunks = [chunk(key, {"vector": raw}) for key, raw in vector]
    k_chunks = [chunk(key, {"keyword": raw}) for key, raw in keyword]
    reme_fused = SearchStep._rrf_merge(v_chunks, k_chunks, weight)
    print("  ReMe SearchStep._rrf_merge 的结果（真 FileChunk 静态调用）：")
    for item in reme_fused:
        print(f"    {item.path:20s} scores={ {kk: round(vv, 6) for kk, vv in item.scores.items()} }")
    by_path = {item.path.split("/")[1].split(".")[0]: item.score for item in reme_fused}
    for key, score in fused:
        assert abs(by_path[key] - score) < 1e-12, (key, by_path[key], score)
    print("  >>> 两边**完全一致**：harness 的 fuse() 就是 _rrf_merge 的复刻，")
    print("      只多做了一件事 —— 平局时按 key 兜底排序（ReMe 用的是 sort(reverse=True)，")
    print("      Python 的 sort 是稳定的，所以 RRF 平局的相对顺序会随两路的插入顺序漂移）。")

    print("\n--- A2 三态融合：两路都有才进 RRF，只有一路就原样返回原始分 ---")
    cases: list[tuple[str, list[tuple[str, float]], list[tuple[str, float]]]] = [
        ("两路都有 -> rrf", keyword, vector),
        ("只有关键词 -> keyword_only（原样）", keyword, []),
        ("只有向量 -> vector_only（原样）", [], vector),
        ("两路都空 -> empty", [], []),
    ]
    for label, kws, vecs in cases:
        mode = retriever.mode(kws, vecs)
        out = retriever.fuse(kws, vecs)
        head = out[0] if out else None
        print(f"  {label:34s} mode={mode:12s} n={len(out)} 首条={head}")
    assert retriever.mode(keyword, vector) == "rrf"
    assert retriever.mode(keyword, []) == "keyword_only"
    assert retriever.mode([], vector) == "vector_only"
    assert retriever.mode([], []) == "empty"
    assert retriever.fuse(keyword, []) == keyword
    assert retriever.fuse([], vector) == vector
    print("  >>> 单路时**不融合**，返回的是 BM25 原始分（12.5 / 9.1…）。")
    print("      这就是「分数量纲会随路径切换」的根源：同一句 query，")
    print("      有没有 embedding、vector_weight 取多少，score 的量纲完全不同。")

    print("\n--- A3 vector_weight=0/1 是「整路跳过」，不是「权重为 0」 ---")
    for vw in (0.0, 1.0, 0.5):
        r = HybridRetriever(vector_weight=vw)
        print(f"  vector_weight={vw}  text_weight={r.text_weight}  "
              f"mode(keyword, vector)={r.mode(keyword, vector)}")
    assert HybridRetriever(vector_weight=0.0).mode(keyword, vector) == "keyword_only"
    assert HybridRetriever(vector_weight=1.0).mode(keyword, vector) == "vector_only"
    print("  >>> 对应 ``search.py:309-323`` 的 if/elif/else：两条路都在时 asyncio.gather 并行，")
    print("      否则整路跳过 —— 跳过向量意味着**连 embedding 都不算**，省一次远程调用；")
    print("      跳过关键词意味着 BM25 索引一次都不查。")

    print("\n--- A4 explain()：把当前权重下的行为展开成人话 ---")
    for line in retriever.explain().splitlines():
        print(f"  {line}")

    print("\n--- A5 refuse()：用已有名次离线重算 RRF，不再打 ReMe ---")
    raw_results = [
        {
            "id": "c-b",
            "path": "resource/b.md",
            "start_line": 1,
            "end_line": 3,
            "text": "b",
            "scores": {"vector": 0.88, "keyword": 3.2, "score": 0.016208},
        },
        {
            "id": "c-a",
            "path": "resource/a.md",
            "start_line": 1,
            "end_line": 3,
            "text": "a",
            "scores": {"vector": 0.55, "keyword": 12.5, "score": 0.011475},
        },
        {
            "id": "c-c",
            "path": "resource/c.md",
            "start_line": 1,
            "end_line": 3,
            "text": "c",
            "scores": {"vector": 0.71, "score": 0.011111},
        },
    ]
    hits = to_memory_hits(raw_results)
    for hit in hits:
        print(f"  hit {hit.path:16s} rank_v={hit.rank_vector} rank_k={hit.rank_keyword} "
              f"source={hit.source} score={hit.score:.6f}")
    assert [hit.source for hit in hits] == ["fused", "fused", "vector"]
    reranked = HybridRetriever(vector_weight=0.5).refuse(hits)
    print("  换成 vector_weight=0.5 后重排（纯内存，0 次检索）：")
    for hit in reranked:
        print(f"    {hit.path:16s} score={hit.score:.6f}（新对象，原列表未改）")
    assert reranked[0].score != hits[0].score
    assert hits[0].score == 0.016208
    print("  >>> 只用 ``rank_keyword`` / ``rank_vector`` 就能重算，")
    print("      而这两个字段是 ``to_memory_hits`` 从 ``scores`` 里**自己推出来**的")
    print("      （ReMe 不提供名次，只提供原始分）。这就是这一层相对「直接调 ReMe」的增量价值。")


# ======================================================================
# B · 单路 BM25：真实的 search job
# ======================================================================
async def section_b() -> None:
    """B 段：没有 embedding 时的真实检索链路。"""
    banner("B · 单路 BM25：真实 search job / candidates 上限 / min_score / 标签 / 去重")

    client, ws = await make_client("b_bm25", **SMALL_CHUNKER)
    ingestor = await build_corpus(client, ws)
    search = MemorySearch(client, workspace=ws)

    print("\n--- B1 真实检索：counts.hybrid=0 是**合法**状态，不是故障 ---")
    result = await search.search("回滚 停流量", limit=3)
    for hit in result.hits:
        print(f"  {hit.path:24s} {hit.start_line}-{hit.end_line}  source={hit.source:8s} "
              f"score={hit.score:.4f} rank_k={hit.rank_keyword}")
    show("counts", result.counts)
    show("hybrid", result.hybrid)
    show("elapsed_ms", f"{result.elapsed_ms:.2f}")
    assert result.counts["vector"] == 0 and result.counts["keyword"] > 0
    assert result.hybrid is False
    assert all(hit.source == "keyword" for hit in result.hits)
    print("  >>> 没配 embedding 时 ``_get_query_embedding`` 返回 None、")
    print("      ``vector_search`` 直接返回 ``[]``（local_file_store.py:936-943），")
    print("      于是三态判定落到 keyword_only：score 就是 **BM25 原始分**。")

    print("\n--- B2 ReMe 的 answer 长什么样（metadata 之外的那一份）---")
    response = await client.run_job("search", query="回滚 停流量", limit=2)
    for line in str(response.answer).splitlines()[:6]:
        print(f"  | {line}")
    print(f"  metadata 键 = {sorted(dict(response.metadata))}")
    print("  >>> ``answer`` 是给 LLM 看的（``search.py:354-362`` 拼的")
    print("      ``========== path:start-end [score=...] ==========`` + 正文），")
    print("      ``metadata`` 是给程序看的（results / counts / link_expansion / tag_filter / dedup）。")
    print("      两份内容同源但形状完全不同 —— 中间件里注入的是前者，harness 用的是后者。")

    print("\n--- B3 candidates：候选池 = limit × candidate_multiplier，上限 200 ---")
    print("  ReMe 侧常数：_MAX_CANDIDATES = 200（search.py:15）")
    for multiplier in (5.0, 1.0):
        one = await search.search("回滚 停流量 部署", limit=1, candidate_multiplier=multiplier)
        print(f"  candidate_multiplier={multiplier}  limit=1 -> candidates="
              f"{min(200, max(1, int(1 * multiplier))):3d}  counts={one.counts}")
    wide = await search.search("回滚 停流量 部署", limit=1, candidate_multiplier=5.0)
    narrow = await search.search("回滚 停流量 部署", limit=1, candidate_multiplier=1.0)
    assert wide.counts["keyword"] > narrow.counts["keyword"], (wide.counts, narrow.counts)
    print("  >>> 这两次调用走的是**直连 step** 的路径（传了 constructor 参数），")
    print("      因为 ``candidate_multiplier`` 在 ``self.kwargs`` 里读（search.py:225），")
    print("      ``run_job`` 的 kwargs 只进 ``RuntimeContext``，改不动它 ——")
    print("      这是 `MemorySearch` 存在的**唯一**理由（search.py 模块 docstring 有完整推导）。")

    print("\n--- B4 min_score 比的是**融合后**的 c.score：单路下等于 BM25 分 ---")
    for threshold in (0.0, 6.0, 100.0):
        got = await search.search("回滚 停流量", limit=5, min_score=threshold)
        print(f"  min_score={threshold:6.1f} -> counts={got.counts} 命中={len(got.hits)}")
    assert (await search.search("回滚 停流量", limit=5, min_score=100.0)).hits == []
    print("  >>> 单路 BM25 的量级是 5~15，所以 min_score=6 还能留下高分项、100 就全滤掉，")
    print("      而 ``success`` **仍然是 True**（``search.py:340-341`` 只做列表过滤）。")
    print("      D 段会看到：一旦走 RRF，score 掉到 0.016 量级，同一个 6.0 会一条不留。")

    print("\n--- B5 标签过滤：OR 语义，未知标签不是错误 ---")
    for tags in (["ops"], ["pref"], ["ops", "pref"], ["nope"]):
        got = await search.search("回滚 部署 图表", limit=20, tags=tags)
        print(f"  tags={str(tags):18s} -> counts={got.counts} 命中路径="
              f"{sorted({hit.path for hit in got.hits})}")
    ops = await search.search("回滚 部署 图表", limit=20, tags=["ops"])
    pref = await search.search("回滚 部署 图表", limit=20, tags=["pref"])
    either = await search.search("回滚 部署 图表", limit=20, tags=["ops", "pref"])
    assert {hit.path for hit in either.hits} == {hit.path for hit in [*ops.hits, *pref.hits]}
    assert ops.hits and pref.hits
    assert (await search.search("回滚 部署", limit=20, tags=["nope"])).hits == []
    print("  >>> ``_resolve_tag_filter``（search.py:144）走的是 **OR**（``match_all=False``），")
    print("      而 ``LocalTagIndex.paths_for_tags``（local_tag_index.py:129）默认是 **AND**。")
    print("      同一个仓库里两个相反默认值都合理（检索侧宁滥勿缺、管理侧宁缺勿滥），")
    print("      但**必须知道自己在哪一侧** —— 这是最容易写出「为什么标签过滤没效果」的地方。")
    print("      未知标签只导致 0 条命中，``success=True``；调用方要靠 ``counts.returned`` 自己发现。")

    print("\n--- B6 tool_context 去重：同一轮对话里给过的 chunk 不再给第二次 ---")
    first = await search.search("回滚 停流量", limit=5, tool_context_id="turn-1")
    again = await search.search("回滚 停流量", limit=5, tool_context_id="turn-1")
    fresh = await search.search("回滚 停流量", limit=5, tool_context_id="turn-2")
    print(f"  turn-1 第 1 次 counts={first.counts} 命中={len(first.hits)}")
    print(f"  turn-1 第 2 次 counts={again.counts} 命中={len(again.hits)}")
    print(f"  turn-2 第 1 次 counts={fresh.counts} 命中={len(fresh.hits)}")
    assert len(again.hits) == 0 and again.counts["keyword"] > 0
    assert len(fresh.hits) == len(first.hits)
    print("  >>> 关键：``counts['keyword']`` 仍是召回数（>0），只有 ``returned`` 变成 0 ——")
    print("      因为去重发生在 ``fused`` 之后、``limit`` 之前（search.py:344-347），")
    print("      而 counts 记的是三路召回与最终返回数。看 counts 要看清是哪一个。")

    print("\n--- B7 max_search_calls：把「一轮里最多检索几次」变成硬预算 ---")
    budgeted = await search.search(
        "回滚", limit=2, tool_context_id="turn-budget", max_search_calls=1,
    )
    print(f"  第 1 次 counts={budgeted.counts}（预算 1）")
    try:
        await search.search("回滚", limit=2, tool_context_id="turn-budget", max_search_calls=1)
    except MemoryJobError as exc:
        print(f"  第 2 次 -> MemoryJobError: {str(exc)[:80]}")
    else:  # pragma: no cover - 不该发生
        raise AssertionError("超过 max_search_calls 应该抛 MemoryJobError")
    print("  >>> ReMe 把预算记在 ``app_context.metadata['__search_call_budgets']`` 上")
    print("      （search.py:245-258），超了就把 ``success`` 置 False；")
    print("      ``MemoryClient.run_job`` 再把 success=False 翻成 ``MemoryJobError``。")
    print("      好处是「模型自己循环检索烧钱」这条路径被 harness 拦住了。")

    print("\n--- B8 反面教材：vector_weight=1.0 但没配 embedding -> 静默空 ---")
    empty = await search.search("回滚 停流量", limit=3, vector_weight=1.0)
    print(f"  vector_weight=1.0 -> counts={empty.counts} 命中={len(empty.hits)}")
    assert empty.hits == [] and empty.counts.get("keyword", 0) == 0
    print("  >>> ``use_keyword = (1 - vector_weight) > 0`` 为 False，两路**都不跑**，")
    print("      ``fused=[]``、``success=True``、没有任何异常（search.py:309-323）。")
    print("      这是本讲最该记住的一条：**降级必须显式化**。harness 的")
    print("      ``SearchResult.counts`` 就是为了让调用方能看见这件事。")


# ======================================================================
# C · 图扩展
# ======================================================================
async def section_c() -> None:
    """C 段：wikilink 图的两条消费路径（expand_links / traverse）。"""
    banner("C · 图扩展：answer 里的 traverse 图、回查 chunk、expand_links 渲染")

    client, ws = await make_client("c_traverse", **SMALL_CHUNKER)
    await build_corpus(client, ws)
    search = MemorySearch(client, workspace=ws)

    print("\n--- C1 traverse：结果在 answer 里，不在 metadata 里 ---")
    response = await client.run_job("traverse", path=["resource/deploy.md"], depth=1)
    print(f"  response.answer 的类型 = {type(response.answer).__name__}")
    print(f"  response.metadata      = {dict(response.metadata)}")
    assert isinstance(response.answer, dict)
    assert not dict(response.metadata)
    print("  >>> ``traverse_step`` 把图写进 ``answer``（traverse.py:176：")
    print("      ``self.context.response.answer = graph.model_dump()``），")
    print("      而 ``search_step`` 把结果写进 ``metadata['results']``。")
    print("      两个 job 的**响应约定相反** —— 照抄 search 的读法读 traverse，")
    print("      会得到「metadata 是空的、结果全丢」这种莫名其妙的现象。")

    print("\n--- C2 traverse 的图结构：nodes 带 depth，edges 带方向 ---")
    graph = await search.traverse_graph(start="resource/deploy.md", depth=1)
    for node in graph["nodes"]:
        print(f"  node depth={node['depth']} {node['path']}")
    for edge in graph["edges"]:
        print(f"  edge {edge}")
    show("seeds", graph["seeds"])
    show("direction", graph.get("direction"))
    assert {node["path"] for node in graph["nodes"]} == {
        "resource/deploy.md",
        "resource/retrieval.md",
        "resource/runbook.md",
    }
    print("  >>> ``direction: both`` 的含义在这里看得很清楚：起点 deploy.md 只有一条出链")
    print("      （→ runbook.md），但因为方向是 both，**入链也算**，")
    print("      所以检索笔记 retrieval.md（它链到 deploy.md）也被拉了进来。")
    print("      默认值见 default.yaml:373 的 depth=1 与 :383 的 direction=both；")
    print("      ``_traverse`` 是**最短路 BFS**，所以每个节点记的是「离起点最少几跳」，")
    print("      而不是「被访问的顺序」——同一个节点被两条路径同时可达时只算更近的那个。")

    print("\n--- C3 用节点路径回查正文：``MemorySearch.traverse`` 补的那一步 ---")
    chunks = await search.traverse(start="resource/deploy.md", depth=1)
    for item in chunks:
        print(f"  {item.path:24s} 行 {item.start_line}-{item.end_line}  "
              f"id={item.id[:12]}  首行={item.text.splitlines()[0][:24]!r}")
    assert [item.path for item in chunks][0] == "resource/deploy.md"
    assert {item.path for item in chunks} == {
        "resource/deploy.md",
        "resource/retrieval.md",
        "resource/runbook.md",
    }
    print("  >>> 图的节点只有路径，**没有正文**，所以封装层再补一步：")
    print("      ``file_store.get_nodes(paths)`` → ``node.chunk_ids`` → ``file_store.file_chunks[id]``，")
    print("      最后按 ``(depth, path, start_line)`` 排序（起点自身 depth=0 排最前）。")

    print("\n--- C4 depth=0：只返回起点自己 ---")
    only_seed = await search.traverse(start="resource/deploy.md", depth=0)
    print(f"  depth=0 -> {sorted({item.path for item in only_seed})}")
    assert {item.path for item in only_seed} == {"resource/deploy.md"}

    print("\n--- C5 悬空 wikilink 永远进不了图（[[runbook]] 没有 .md）---")
    store = client.component("file_store", "default")
    dangling = await search.traverse(start="resource/retrieval.md", depth=1)
    print(f"  从 retrieval.md 出发（它写的是 [[resource/deploy.md]]）-> "
          f"{sorted({item.path for item in dangling})}")
    nodes = await store.get_nodes(["resource/retrieval.md"])
    print(f"  retrieval.md 的 outlinks = "
          f"{[(link.target_path, link.target_anchor) for link in nodes[0].links]}")
    assert {item.path for item in dangling} == {"resource/deploy.md", "resource/retrieval.md"}

    print("\n--- C6 expand_links：search 顺手把邻居渲染成缩进行 ---")
    expanded = await client.run_job("search", query="部署 预检", limit=1)
    print("  answer（带展开行）：")
    for line in str(expanded.answer).splitlines():
        print(f"  | {line}")
    show("link_expansion 键", sorted(dict(expanded.metadata).get("link_expansion", {})))
    tight = await search.search("部署 预检", limit=1, expand_links=False)
    print(f"  直连 step 且 expand_links=False -> 命中 {len(tight.hits)} 条（answer 不再有 → 行）")
    assert dict(expanded.metadata).get("link_expansion")
    print("  >>> ``expand_links`` 是**构造参数**（search.py:226），")
    print("      所以只有直连 step 的路径能逐次关掉它；job 路径永远用配置里的值。")
    print("      渲染格式见 ``utils/link_expansion.py:97`` 的 ``render_expansion_lines``：")
    print("      先一行方向表头 ``outlinks (n):``，每个邻居一行 ``→ path  <meta>``，")
    print("      有锚点时再补一行 ``via anchor=#回滚``；meta 为空时打 ``(no meta)``。")
    print("      注意这里分方向（→ 出链 / ← 入链），因为两个方向对模型的含义不同：")
    print("      出链是「这条记忆指向哪里」，入链是「谁引用了这条记忆」。")


# ======================================================================
# D · 注入确定性假 embedder -> 真·混合检索
# ======================================================================
async def section_d() -> tuple[MemorySearch, FakeEmbedder, list[Any]]:
    """D 段：把向量路接上，验证 RRF 融合的真实行为。

    Returns:
        `tuple[MemorySearch, FakeEmbedder, list[Any]]`: 检索器、假 embedder
        与最后一轮命中的 ``MemoryHit`` 列表（E 段接着用）。
    """
    banner("D · 注入确定性假 embedder → 真·混合检索（vector_search + RRF）")

    client, ws = await make_client(
        "d_hybrid",
        embedding_dimensions=FakeEmbedder.DIM,
        **SMALL_CHUNKER,
    )
    ingestor = await build_corpus(client, ws)
    search = MemorySearch(client, workspace=ws)

    print("\n--- D1 装配期就有 embedding_store，但 as_embedding 是空的 openai 后端 ---")
    store = client.component("file_store", "default")
    emb_store = client.component("embedding_store", "default")
    print(f"  file_store.embedding_store = {type(store.embedding_store).__name__}")
    print(f"  embedding_store.dimensions = {emb_store.dimensions}")
    print(f"  as_embedding = {type(emb_store.as_embedding).__name__}")
    print("  >>> ``HarnessMemoryConfig._apply_embedding``（config.py:473）装的是")
    print("      ``backend: openai`` + 空凭据 —— 一个「占位」的 embedding 组件，")
    print("      它的 ``_ensure_model`` 要到第一次调用才会构造真模型，所以装配能过。")
    print("      这就是官方文档说的「start 前用 update_component 注入真实 embedding model」。")

    print("\n--- D2 update_component 注入假 embedder（官方给的运行时注入点）---")
    fake = FakeEmbedder()
    await client.application.update_component(
        ComponentEnum.EMBEDDING_STORE, "default", as_embedding=fake,
    )
    print(f"  application.py:235 的 update_component(...) -> as_embedding={type(fake).__name__}")
    print(f"  vector_space_id = {emb_store.vector_space_id}（缓存文件名要用它）")
    print("  >>> ``update_component`` 只做 ``setattr``（application.py:239-248），")
    print("      先校验 「组件存在」+「属性存在」，不做 isinstance ——")
    print("      所以鸭子类型的假实现可以塞进去。真实生产用它注入已构造好的模型。")

    print("\n--- D3 重新 upsert：让 chunk 拿到向量（这一步是索引侧的事）---")
    before = fake.calls
    for name, _text, _tags in CORPUS:
        path = ws.resolve_relative(f"resource/{name}.md")
        chunker = ingestor._chunker(path)
        node, chunks = await chunker.chunk(path)
        await store.upsert([(node, chunks)])
    nodes = await store.get_nodes([f"resource/{name}.md" for name, _, _ in CORPUS])
    total = sum(len(node.chunk_ids) for node in nodes)
    with_vec = sum(
        1
        for node in nodes
        for cid in node.chunk_ids
        if store.file_chunks[cid].embedding is not None
    )
    print(f"  chunks 总数 = {total}，其中带向量的 = {with_vec}，"
          f"embedder 调用 = {fake.calls - before} 次")
    assert with_vec == total == 5, (with_vec, total)
    print("  >>> ``upsert`` 里 ``_reuse_or_queue_embedding``（local_file_store.py:810）")
    print("      只把「没有向量且文本非空」的 chunk 送进 ``needs_embed``，")
    print("      再由 ``_embed_pending`` 调 ``get_node_embeddings`` **批量**编码 ——")
    print("      所以 chunk 向量的调用次数是「批数」，不是「chunk 数」。")

    print("\n--- D4 真·混合检索：counts.hybrid=1，source 三态同现 ---")
    result = await search.search("回滚 停流量 部署", limit=6, vector_weight=0.7)
    for hit in result.hits:
        print(f"  {hit.path:24s} {hit.start_line:2d}-{hit.end_line:2d}  source={hit.source:8s} "
              f"score={hit.score:.6f} rank_v={hit.rank_vector} rank_k={hit.rank_keyword}")
    show("counts", result.counts)
    show("hybrid", result.hybrid)
    assert result.hybrid is True and result.counts["vector"] > 0 and result.counts["keyword"] > 0
    assert {hit.source for hit in result.hits} & {"fused", "vector", "keyword"}

    response = await client.run_job("search", query="回滚 停流量 部署", limit=2, vector_weight=0.7)
    first_row = dict(response.metadata)["results"][0]
    print(f"  第一条 chunk 的 scores = "
          f"{ {k: round(v, 6) for k, v in first_row['scores'].items()} }")
    print("  >>> 这是本讲第二个必须记住的量纲事实：")
    print("      ``scores['score']`` 是 **RRF 融合分**（~0.016），")
    print("      ``scores['keyword']`` / ``scores['vector']`` 是各自那一路的**原始分**。")
    print("      同一个字段名 ``score`` 在不同路径下量纲差两个数量级。")

    print("\n--- D5 量纲陷阱：min_score 用 BM25 的尺度去卡融合结果 ---")
    dropped = await search.search("回滚 停流量", limit=5, min_score=6.0, vector_weight=0.7)
    kept = await search.search("回滚 停流量", limit=5, min_score=0.01, vector_weight=0.7)
    print(f"  min_score=6.0  -> 命中 {len(dropped.hits)} 条，counts={dropped.counts}")
    print(f"  min_score=0.01 -> 命中 {len(kept.hits)} 条，counts={kept.counts}")
    assert dropped.hits == [] and dropped.counts["keyword"] > 0
    assert kept.hits
    print("  >>> 同一个查询：B 段（单路 BM25）用 min_score=6.0 还能留下分高的，")
    print("      D 段（RRF）用 6.0 一条不留。所以 min_score 的正确用法是")
    print("      「先看 counts.hybrid，再决定阈值的量纲」 —— 或者干脆交给")
    print("      ``harness_kit.memory.gating.MemoryGate`` 做归一化（第 18 讲）。")

    print("\n--- D6 refuse()：换权重不重新检索（embedder 调用数不变）---")
    calls_before = fake.calls
    reranked = HybridRetriever(vector_weight=0.2).refuse(result.hits)
    calls_after = fake.calls
    print("  vector_weight=0.2 重排后前 3 条：")
    for hit in reranked[:3]:
        print(f"    {hit.path:24s} {hit.start_line}-{hit.end_line}  score={hit.score:.6f}")
    print(f"  embedder 调用：重排前 {calls_before} -> 重排后 {calls_after}（增量 0）")
    assert calls_after == calls_before
    assert {hit.chunk_id for hit in reranked} == {hit.chunk_id for hit in result.hits}
    print("  >>> 这是「检索一次、多种排序」的来源：名次已经在 hit 里，重排是纯内存的。")

    print("\n--- D7 LRU：同一个 query 再搜一次，不再产生 embedding 调用 ---")
    again_before = fake.calls
    await search.search("回滚 停流量 部署", limit=6, vector_weight=0.7)
    print(f"  第 2 次同 query：embedder 调用增量 = {fake.calls - again_before}")
    assert fake.calls - again_before <= 1
    print("  >>> ``LocalEmbeddingStore`` 的 ``_cache_key`` 是 ``sha256(text)``")
    print("      （local_embedding_store.py:278），所以 query 向量命中进程内 LRU。")
    print("      注意：**chunk 向量与 query 向量共用一个缓存**，这也是为什么")
    print("      第一次 upsert 之后再查不会重复付费。")

    return search, fake, list(result.hits)


# ======================================================================
# E · 引用与预算
# ======================================================================
def section_e(hits: list[Any]) -> None:
    """E 段：把命中的 chunk 变成「能核对的引用」与「装得下的注入块」。

    Args:
        hits (`list[Any]`): D 段拿到的 ``MemoryHit`` 列表。
    """
    banner("E · 引用与预算：CitationBuilder / merge_intervals / MemoryBudget")

    print("\n--- E1 引用：``[n] path:start-end  quote`` ---")
    builder = CitationBuilder(max_quote_chars=40)
    citations = builder.build(hits)
    for line in builder.render_lines(citations):
        print(f"  {line}")
    assert citations and citations[0].index == 1
    assert citations[0].path and citations[0].start_line >= 1
    assert any(citation.quote.endswith("…") for citation in citations), "40 字符预算下应出现截断"
    print("  >>> 引用的两个组成部分各有各的用：``[n]`` 让正文里的 ``[n]`` 能对上，")
    print("      ``path:start-end`` 是编辑器里可跳转的锚点（人可以去核对）。")
    print("      对比：官方中间件的 ``_extract_memory_texts``（_utils.py:94-103）")
    print("      只把 ``text`` 抽出来，path 和行号在那一层就被丢掉了 —— 缺口 3。")

    print("\n--- E2 同一文件的多个 chunk：渲染前先合并行区间 ---")
    same_file = [hit for hit in hits if hit.path == "resource/deploy.md"]
    print(f"  deploy.md 命中 {len(same_file)} 段：")
    for hit in same_file:
        print(f"    {hit.start_line}-{hit.end_line}")
    print(f"  merge_intervals(gap=0) = {merge_intervals(same_file)}")
    print(f"  merge_intervals(gap=2) = {merge_intervals(same_file, gap=2)}")
    merged = merge_intervals(same_file)
    assert merged == sorted(merged) and all(a[1] < b[0] for a, b in zip(merged, merged[1:])), merged
    print("  >>> 分块器是**字节窗口 + 重叠**（default_file_chunker.py:25 的")
    print("      ``chunk_byte_size`` / ``overlap_byte_size``），一次检索命中相邻两块时")
    print("      行区间会贴在一起；直接逐条渲染会让人以为有两处不同内容。")

    print("\n--- E3 MemoryBudget.fit：贪心前缀 + 整块重测 ---")
    for cap in (DEFAULT_MAX_TOKENS, 200, 0):
        budget = MemoryBudget(max_tokens=cap)
        fitted = budget.fit(hits)
        print(f"  max_tokens={cap:5d} -> kept={len(fitted.kept)} dropped={len(fitted.dropped)} "
              f"estimated={fitted.estimated_tokens} truncated={fitted.truncated} "
              f"per_hit={fitted.per_hit_tokens}")
        assert fitted.estimated_tokens == estimate_tokens_heuristic(render_memory_block(fitted.kept)) or not fitted.kept
    tight = MemoryBudget(max_tokens=200).fit(hits)
    assert tight.truncated is True and tight.dropped
    assert estimate_tokens_heuristic(tight.render()) <= 200
    zero = MemoryBudget(max_tokens=0).fit(hits)
    assert zero.kept == [] and zero.truncated is True
    print("  >>> ``estimated_tokens`` 量的就是 ``render_memory_block(kept)`` ——")
    print("      **计量与注入共用同一个渲染函数**，所以预算精确而不是近似：")
    print(f"      estimate(render()) = {estimate_tokens_heuristic(tight.render())} <= 200。")

    print("\n--- E4 预算里到底装了什么（就是将要注入 system prompt 的那段文本）---")
    small = MemoryBudget(max_tokens=200).fit(hits)
    for line in small.render().splitlines():
        print(f"  | {line}")
    print("  >>> 保留 ``### path:start-end`` 是刻意的：模型被允许引用来源，")
    print("      而人能顺着路径去核对。丢掉行号的注入（官方中间件就是这么做的）")
    print("      会让「模型说'你的笔记里写了 X'」变成一句无法验证的话。")

    print("\n--- E5 fit_or_truncate：第一条就超预算时的显式降级 ---")
    tiny = MemoryBudget(max_tokens=90)
    strict = tiny.fit(hits)
    degraded = tiny.fit_or_truncate(hits, min_hits=1)
    print(f"  严格 fit        -> kept={len(strict.kept)} truncated={strict.truncated} "
          f"estimated={strict.estimated_tokens}")
    print(f"  fit_or_truncate -> kept={len(degraded.kept)} truncated={degraded.truncated} "
          f"estimated={degraded.estimated_tokens}")
    tail = degraded.kept[-1]
    print(f"  最后一条正文结尾 = ...{tail.text[-18:]!r}")
    print(f"  它的来源仍然完整 = {tail.path}:{tail.start_line}-{tail.end_line}，"
          f"chunk_id 保留 = {bool(tail.chunk_id)}")
    assert tail.text.endswith("…（已截断）")
    assert tail.path and tail.start_line >= 1 and tail.chunk_id
    assert len(degraded.kept) >= 1
    print("  >>> 严格预算面对 10000 字节的 chunk 会返回空列表，外部看到的是")
    print("      「记忆功能好像坏了」。``fit_or_truncate`` 用二分找到「还能装下的最长正文前缀」，")
    print("      补 ``…（已截断）``，但**不动 path 与行号** —— 引用依然可核对。")

    print("\n--- E6 自定义 estimator：预算可以换成真 tokenizer ---")
    calls: list[int] = []

    def one_token_per_char(text: str) -> int:
        calls.append(len(text))
        return len(text)

    custom = MemoryBudget(max_tokens=120, estimator=one_token_per_char)
    got = custom.fit(hits)
    default = MemoryBudget(max_tokens=120).fit(hits)
    print(f"  1 字符 1 token 的估计器 -> kept={len(got.kept)} estimated={got.estimated_tokens} "
          f"（同一个预算、默认启发式下 kept={len(default.kept)} estimated={default.estimated_tokens}）")
    assert got.estimated_tokens <= 120 and default.estimated_tokens <= 120
    print("  >>> 预算相同、命中相同，**保留条数不同** —— 因为估计器不同。")
    print("      这就是为什么 ``fit`` 的参数里必须有 ``estimator`` 这个口子：")
    print("      ``TokenEstimator`` 就是 ``Callable[[str], int]``，")
    print("      换成 transformers 的 tokenizer 只是把参数换个对象，算法一行都不用改。")
    assert calls, "自定义估计器必须真的被调用过"


# ======================================================================
# F · （--live）真实模型读注入块
# ======================================================================
def text_of(response: object) -> str:
    """从 ``ChatResponse`` 里挑出纯文本（``ChatResponse`` 没有 ``get_text_content``）。

    Args:
        response (`object`): ``ChatResponse``。

    Returns:
        `str`: 拼接后的文本。
    """
    return "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
    )


async def section_f(hits: list[Any]) -> int:
    """F 段：真实 deepseek-flash 用「注入块 + 引用」回答一次。

    Args:
        hits (`list[Any]`): D 段拿到的 ``MemoryHit`` 列表。

    Returns:
        `int`: 实际发生的补全调用次数。

    Raises:
        `RuntimeError`: 缺少 ``OPENAI_API_KEY``。
    """
    banner("F ·（--live）真实 deepseek-flash：读注入块，按引用编号回答")

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key:
        raise RuntimeError("--live 需要 OPENAI_API_KEY（从 <repo>/.env 读）")

    from harness_kit.models.adapters.openai_compat import OpenAICompatChatModel
    from harness_kit.models.pricing import default_price_table

    block = MemoryBudget(max_tokens=600).fit(hits).render()
    citations = CitationBuilder(max_quote_chars=120).build(hits)
    cite_lines = CitationBuilder(max_quote_chars=120).render_lines(citations)
    question = "部署前要确认什么？切换阶段要注意什么？"
    prompt = (
        f"{block}\n"
        "来源清单：\n" + "\n".join(cite_lines) + "\n\n"
        f"只根据上面的长期记忆回答：{question}\n"
        "回答里用 [n] 标注你引用的来源编号。"
    )
    print(f"  注入块 {len(block)} 字符、引用 {len(citations)} 条、问题 {question!r}")

    chosen = model_name()
    model = OpenAICompatChatModel(
        model_name=chosen,
        api_key=api_key,
        base_url=base_url,
        stream=False,
        pricing=default_price_table(),
    )
    started = time.perf_counter()
    response = await model([UserMsg("user", prompt)])
    answer = text_of(response)
    print(f"  {chosen} 回答（{time.perf_counter() - started:.2f}s）：")
    for line in answer.splitlines():
        print(f"  | {line}")
    print(f"  usage = {dict(response.usage) if response.usage else None}")
    assert answer.strip()
    print("  >>> 这一段把本讲的三件事串起来了：**检索**（ReMe 的混合召回）→")
    print("      **引用**（path + 行号，可核对）→ **预算**（注入块不超过 max_tokens）。")
    print("      Agent = LLM 模型 + Agent Harness：模型只负责「读并回答」，")
    print("      「读什么、读多少、出处是谁」全是 harness 决定的。")
    return 1


# ======================================================================
# main
# ======================================================================
async def main() -> int:
    """跑全部段落，返回退出码。

    Returns:
        `int`: 0 = 全部通过。
    """
    print(f"沙箱目录 = {SANDBOX}")
    calls = 0
    hits: list[Any] = []
    try:
        section_a()
        await section_b()
        await section_c()
        _, _, hits = await section_d()
        section_e(hits)
        if LIVE:
            calls = await section_f(hits)
        else:
            print()
            print("=" * 78)
            print("跳过 F 段（真实 LLM）。加 --live 打开：1 次补全调用。")
            print("=" * 78)
    finally:
        await close_all()
    print()
    print("=" * 78)
    if LIVE:
        print(f"PASS · 第 17 讲全部断言通过（A~E 段 0 次 LLM 调用；F 段 {calls} 次补全）")
    else:
        print("PASS · 第 17 讲全部断言通过（A~E 段 0 次 LLM 调用）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
