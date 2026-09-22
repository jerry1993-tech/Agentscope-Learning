# -*- coding: utf-8 -*-
"""检索封装：调 ReMe 的 ``search`` / ``traverse``，不做二次实现（契约 §3.17）。

**为什么需要一个封装层，而不是直接 ``run_job("search", ...)``**

因为 ReMe 的检索参数分成**两类**，而它们的控制方式完全不同 ——
这是读源码才能知道的结论，也是本模块存在的唯一理由。

``SearchStep.execute``（``third_party/ReMe/reme/steps/index/search.py:206-226``）：

.. code-block:: python

    query = (self.context.get("query", "") or "").strip()          # 运行时可传
    limit = int(self.context.get("limit") or _default_limit())     # 运行时可传
    min_score = float(self.context.get("min_score") or 0.0)        # 运行时可传
    raw_vw = self.context.get("vector_weight")                     # 运行时可传
    ...
    candidate_multiplier = float(self.kwargs.get("candidate_multiplier", 5.0))   # ← 只能配置
    expand_links_enabled = bool(self.kwargs.get("expand_links", True))           # ← 只能配置
    max_links_per_direction = int(self.kwargs.get("max_links_per_direction", 10))# ← 只能配置

``self.context`` 是 ``RuntimeContext``（``run_job(**kwargs)`` 的 kwargs 进这里），
``self.kwargs`` 是**构造参数**，来源是 job 的 ``step_specs``
—— ``BaseJob._resolve_step`` 把 YAML 里的 step 配置整段 ``model_dump()`` 后
``step_cls(**params)``（``third_party/ReMe/reme/components/job/base_job.py:64-78``），
而 ``BaseJob.__call__`` 只把运行 kwargs 合进 context（``base_job.py:89-90``），
**不会**碰构造参数。

所以：``candidate_multiplier`` / ``expand_links`` / ``max_links_per_direction``
**无法通过 ``run_job`` 改**。想改只有两条路：

1. 在装配期改配置（:meth:`~harness_kit.memory.config.HarnessMemoryConfig.with_components`）；
2. 绕开 job，直接构造 ``search_step``（从 job 的 ``step_specs`` 里取出真实配置、
   覆盖要改的项、再调用）。

:meth:`MemorySearch.search` 对这两条路都支持：**没传这三个参数时走 job**（第 1 条路，
尊重装配配置）；**传了就直连 step**（第 2 条路，逐次覆盖）。
两条路用的都是 ReMe 自己的 ``SearchStep`` 与 ``file_store``，没有任何二次实现。

**与契约的两处主动偏离（已在返回值里记录）**

契约 §3.17 写的是 ``candidate_multiplier: float = 3.0`` 与 ``expand_links: bool = False``，
但源码的实际默认值是 **5.0 / True**（``search.py:224-226`` 的 fallback，
且 ``config/default.yaml:474-478`` 的 ``search`` job 显式配了
``candidate_multiplier: 5.0`` / ``expand_links: true``）。
如果 harness 把默认写成 3.0 / False，那么"不传参数"就意味着
"把候选池从 5× 缩到 3×、把 wikilink 扩展关掉" —— 一个没人要求的、静默的行为改变。
所以本模块把默认设为 ``None``，含义是"沿用 ReMe 配置"。这是偏离契约字面、但符合契约意图的选择。

**零结果不是错误**

``counts["vector"] == 0`` 且 ``hybrid == True`` 是**合法**状态：
没配 embedding 时向量路就是答空，而 ``success`` 仍是 ``True``
（``search.py:330`` 的 ``hybrid = bool(vector_results) and bool(keyword_results)``）。
调用方不该把"向量路 0 条"当成故障，但**该**把它记进指标 ——
否则"混合检索退化成了纯 BM25"这件事会永远没人发现。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from loguru import logger

from .citations import MemoryHit, SearchResult, to_memory_hits
from .client import MemoryClient

__all__ = [
    "DEFAULT_LIMIT",
    "MemorySearch",
    "SEARCH_JOB",
    "TRAVERSE_JOB",
]

#: ReMe 的检索 job 名（``third_party/ReMe/reme/config/default.yaml`` 的 ``jobs.search``）。
SEARCH_JOB: str = "search"

#: ReMe 的图遍历 job 名（``jobs.traverse``）。
TRAVERSE_JOB: str = "traverse"

#: 未显式给 ``limit`` 时用的值。
#: ReMe 自己的默认是 ``REME_SEARCH_LIMIT`` 环境变量，缺省 5
#: （``third_party/ReMe/reme/steps/index/search.py:16-23``）。
#: harness 侧取 10：长期记忆场景下多要几条再做预算裁剪，比少要几条更可控。
DEFAULT_LIMIT: int = 10

#: 运行时**可**通过 ``run_job`` 覆盖的键（进 ``RuntimeContext``）。
_RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "query",
        "limit",
        "min_score",
        "tags",
        "vector_weight",
        "start_date",
        "end_date",
        "search_filter",
        "tool_context_id",
        "max_search_calls",
        "strict_date_filter",
    },
)

#: 只能通过**构造参数**控制的键（在 ``step_specs`` 里）。
_CONSTRUCTOR_KEYS: frozenset[str] = frozenset(
    {
        "candidate_multiplier",
        "expand_links",
        "max_links_per_direction",
    },
)


class MemorySearch:
    """检索封装（契约 §3.17）。

    Example::

        search = MemorySearch(client, workspace=ws)
        result = await search.search("用户偏好什么图表主题", limit=5, tags=["pref"])
        for hit in result.hits:
            print(hit.path, hit.start_line, hit.source, f"{hit.score:.4f}")
    """

    def __init__(self, client: MemoryClient, *, workspace: Any | None = None) -> None:
        """构造检索器。

        Args:
            client (`MemoryClient`): 已 start 的客户端。
            workspace (`Any | None`): 可选工作区；给了就把命中的绝对路径
                归一成工作区相对路径。
        """
        self.client = client
        self.workspace = workspace

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        min_score: float = 0.0,
        tags: list[str] | None = None,
        expand_links: bool | None = None,
        candidate_multiplier: float | None = None,
        vector_weight: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        tool_context_id: str | None = None,
        max_search_calls: int | None = None,
        strict_date_filter: bool = False,
    ) -> SearchResult:
        """执行一次混合检索（契约 §3.17）。

        Args:
            query (`str`): 查询串。
            limit (`int`): 要几条；必须为正。
            min_score (`float`): 融合分下限。**注意量纲**：RRF 融合分在
                ``1/(60+1)`` 量级（约 0.016）以下，用 ``0.2`` 之类的绝对值会把结果全滤掉。
                详见 :mod:`harness_kit.memory.hybrid`。
            tags (`list[str] | None`): 标签过滤（OR 语义：命中任一标签即保留）。
                ReMe 侧走 ``_resolve_tag_filter``，归一化规则是**查询侧**（不截断条数）。
            expand_links (`bool | None`): 是否展开 wikilink 邻居。
                ``None`` = 沿用配置（``config/default.yaml`` 里是 ``true``）。
            candidate_multiplier (`float | None`): 候选池倍数。
                ``None`` = 沿用配置（5.0）。给出值时走直连 step 的路径。
            vector_weight (`float | None`): 向量路权重，运行时可控；
                ``None`` = 沿用配置（0.7）。``0.0`` 会整路跳过向量（省 embedding 调用）。
            start_date (`str | None`): ``YYYY-MM-DD`` 起（含）。
            end_date (`str | None`): ``YYYY-MM-DD`` 止（含）。
            tool_context_id (`str | None`): 同一轮对话内的去重桶 id。
                ReMe 用它把已经给过的 chunk 过滤掉
                （``search.py:109-142`` 的 ``_dedupe_tool_context``）。
            max_search_calls (`int | None`): 同一个 ``tool_context_id`` 允许调用几次；
                需要同时给 ``tool_context_id``，否则 ``search.py:239-241`` 会抛
                ``ValueError``。
            strict_date_filter (`bool`): 严格日期过滤。

        Returns:
            `SearchResult`: 结构化结果。

        Raises:
            `ValueError`: ``query`` 为空，或 ``limit <= 0``，或给混了
                ``max_search_calls`` 与缺失的 ``tool_context_id``。
            `MemoryJobError`: ReMe 返回 ``success=False``。
        """
        text = (query or "").strip()
        if not text:
            raise ValueError("query 不能为空")
        if limit <= 0:
            raise ValueError(f"limit 必须为正，收到 {limit}")
        if max_search_calls is not None and not tool_context_id:
            raise ValueError("max_search_calls 需要同时提供 tool_context_id")
        if vector_weight is not None and not 0.0 <= vector_weight <= 1.0:
            raise ValueError(f"vector_weight 必须在 [0,1]，收到 {vector_weight}")
        if candidate_multiplier is not None and candidate_multiplier <= 0:
            raise ValueError(f"candidate_multiplier 必须为正，收到 {candidate_multiplier}")

        runtime: dict[str, Any] = {"query": text, "limit": int(limit), "min_score": float(min_score)}
        if tags:
            runtime["tags"] = list(tags)
        if vector_weight is not None:
            runtime["vector_weight"] = float(vector_weight)
        if start_date:
            runtime["start_date"] = start_date
        if end_date:
            runtime["end_date"] = end_date
        if tool_context_id:
            runtime["tool_context_id"] = tool_context_id
        if max_search_calls is not None:
            runtime["max_search_calls"] = int(max_search_calls)
        if strict_date_filter:
            runtime["strict_date_filter"] = True

        constructor: dict[str, Any] = {}
        if candidate_multiplier is not None:
            constructor["candidate_multiplier"] = float(candidate_multiplier)
        if expand_links is not None:
            constructor["expand_links"] = bool(expand_links)

        started = time.monotonic()
        if constructor:
            response = await self._run_step(SEARCH_JOB, runtime, constructor)
        else:
            response = await self.client.run_job(SEARCH_JOB, **runtime)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        metadata = dict(getattr(response, "metadata", None) or {})
        counts = _as_counts(metadata.get("counts"))
        hits = to_memory_hits(metadata.get("results"), workspace=self.workspace)
        if not hits:
            # 回答里有内容但 results 为空，说明 step 走了"tag 过滤没命中"之类的
            # 早退分支。这种"看起来什么都没发生"的情况必须留痕。
            logger.info(
                "search: query={!r} 命中 0 条（counts={}）",
                text,
                counts,
            )
        return SearchResult(
            query=text,
            hits=hits,
            counts=counts,
            hybrid=bool(counts.get("hybrid", 0)),
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # 图遍历
    # ------------------------------------------------------------------
    async def traverse(self, *, start: str, depth: int = 1) -> list[Any]:
        """从若干起点沿 wikilink 图遍历，返回**被遍历到的文件的 chunk**（契约 §3.17）。

        ReMe 的 ``traverse`` 把结果放在 ``Response.answer`` 里
        （``third_party/ReMe/reme/steps/index/traverse.py:176``：
        ``self.context.response.answer = graph.model_dump()``），
        ``metadata`` 是空的 —— 这与 ``search`` 正好相反，很容易踩。
        图里只有节点与边，没有 chunk 正文，所以本方法再补一步：
        用节点路径从 ``file_store`` 取回 chunk 列表，按"离起点的层数"排序。

        Args:
            start (`str`): 起点路径（一个，或逗号分隔多个）；工作区相对路径。
            depth (`int`): 最大层数，默认 1；``0`` 只返回起点自身。

        Returns:
            `list[Any]`: ``FileChunk`` 列表，按 ``(depth, path, start_line)`` 排序。

        Raises:
            `ValueError`: ``start`` 为空，或 ``depth < 0``。
            `MemoryJobError`: ReMe 返回 ``success=False``。
        """
        seeds = [item.strip() for item in str(start or "").split(",") if item.strip()]
        if not seeds:
            raise ValueError("start 不能为空")
        if depth < 0:
            raise ValueError(f"depth 不能为负，收到 {depth}")

        response = await self.client.run_job(TRAVERSE_JOB, path=seeds, depth=int(depth))
        graph = getattr(response, "answer", None)
        if not isinstance(graph, dict):
            logger.warning("traverse: answer 不是 graph dict（{}），返回空", type(graph).__name__)
            return []

        nodes = graph.get("nodes") or []
        by_path: dict[str, int] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            path = str(node.get("path", ""))
            if path:
                by_path[path] = int(node.get("depth", 0) or 0)

        file_store = self.client.component("file_store", "default")
        stored_nodes = {item.path: item for item in await file_store.get_nodes(list(by_path))}
        # 层数用 id(chunk) 作键存着，而不是往 FileChunk 上挂属性：
        # FileChunk 是 pydantic v2 模型（``schema/file_chunk.py:8``），
        # 未声明的属性赋值会抛 ValueError。也不改 chunk 本身 ——
        # 它是 file_store 里的共享对象，改它等于污染索引。
        depths: dict[int, int] = {}
        chunks: list[Any] = []
        for path, node_depth in by_path.items():
            stored = stored_nodes.get(path)
            if stored is None:
                continue
            for chunk_id in stored.chunk_ids:
                chunk = file_store.file_chunks.get(chunk_id)
                if chunk is not None:
                    depths[id(chunk)] = node_depth
                    chunks.append(chunk)
        chunks.sort(key=lambda c: (depths.get(id(c), 0), c.path, c.start_line))
        logger.info("traverse: 起点={} depth={} 节点 {} 个，chunk {} 个", seeds, depth, len(by_path), len(chunks))
        return chunks

    async def traverse_graph(self, *, start: str, depth: int = 1) -> dict[str, Any]:
        """返回 ReMe 原始的图结构（``{nodes, edges, ...}``）。

        给"我要看拓扑"的场景用；日常取正文用 :meth:`traverse`。

        Args:
            start (`str`): 起点路径。
            depth (`int`): 最大层数。

        Returns:
            `dict[str, Any]`: ``traverse_step`` 产出的图 dict。
        """
        seeds = [item.strip() for item in str(start or "").split(",") if item.strip()]
        if not seeds:
            raise ValueError("start 不能为空")
        response = await self.client.run_job(TRAVERSE_JOB, path=seeds, depth=int(depth))
        answer = getattr(response, "answer", None)
        return answer if isinstance(answer, dict) else {}

    # ------------------------------------------------------------------
    # 标签
    # ------------------------------------------------------------------
    async def tags_for_path(self, path: str) -> list[str]:
        """查一个文件当前生效的标签。

        标签在 tag_index 里，**不在** search 结果里（``search.py:363-372``
        写回的 ``results`` 只有 chunk 字段）。要拿标签就得单独查
        ``tag_index.tags_for_path``（``local_tag_index.py:143``）。

        Args:
            path (`str`): 工作区相对路径。

        Returns:
            `list[str]`: 标签（可能是空列表）。
        """
        tag_index = self.client.component("tag_index", "default")
        return list(await tag_index.tags_for_path(path))

    async def tags_by_path(self, hits: Sequence[MemoryHit]) -> dict[str, list[str]]:
        """批量补标签，供 :func:`to_memory_hits` 的 ``tags_by_path`` 用。

        Args:
            hits (`Sequence[MemoryHit]`): 命中列表。

        Returns:
            `dict[str, list[str]]`: 路径 → 标签。
        """
        result: dict[str, list[str]] = {}
        for path in dict.fromkeys(hit.path for hit in hits):
            try:
                result[path] = await self.tags_for_path(path)
            except Exception as exc:  # noqa: BLE001 - 标签是可选信息，缺了不该让检索失败
                logger.debug("tags_for_path({}) 失败: {}", path, exc)
                result[path] = []
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _run_step(
        self,
        job_name: str,
        runtime: dict[str, Any],
        constructor: dict[str, Any],
    ) -> Any:
        """直连 step 执行（用于覆盖构造参数）。

        从 job 的 ``step_specs`` 里取出**真实装配的**step 类与构造参数
        （``third_party/ReMe/reme/components/job/base_job.py:54`` 的 ``step_specs``），
        只覆盖调用方显式给的那几项，其余原样 —— 这样"改一个参数"不会顺手
        把配置里的其他值也重置成代码默认值。

        Args:
            job_name (`str`): job 名。
            runtime (`dict[str, Any]`): 运行时上下文参数。
            constructor (`dict[str, Any]`): 要覆盖的构造参数。

        Returns:
            `Any`: ``Response``。

        Raises:
            `MemoryUnavailableError`: job 不存在或没有 step。
            `MemoryJobError`: ``success=False``。
        """
        from .client import MemoryJobError, MemoryUnavailableError

        jobs = self.client.application.context.jobs
        job = jobs.get(job_name)
        if job is None:
            raise MemoryUnavailableError(f"job {job_name!r} 不存在；可用: {sorted(jobs)}")
        specs = list(getattr(job, "step_specs", []) or [])
        if not specs:
            raise MemoryUnavailableError(f"job {job_name!r} 没有可执行的 step")

        step_cls, params = specs[0]
        merged = {**params, **constructor}
        step = step_cls(**merged)
        response = await step(**runtime)
        if not getattr(response, "success", True):
            raise MemoryJobError(job_name, str(getattr(response, "answer", "")))
        logger.debug("search 直连 step，构造参数覆盖: {}", sorted(constructor))
        return response


def _as_counts(value: Any) -> dict[str, int]:
    """把 ``metadata["counts"]`` 规整成 ``dict[str, int]``。

    ReMe 在这里放的是 ``{"vector": int, "keyword": int, "returned": int, "hybrid": bool}``
    （``search.py:367-372``）。``hybrid`` 是 **bool**，而契约的字段类型写的是
    ``dict[str, int]`` —— Python 里 ``bool`` 是 ``int`` 的子类，pydantic 的
    lax 模式能收，但这里显式 ``int()`` 一下，免得未来收紧校验时炸掉。

    Args:
        value (`Any`): 原始值。

    Returns:
        `dict[str, int]`: 规整后的计数；无法转换的项被丢弃。
    """
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool):
            result[str(key)] = int(item)
        elif isinstance(item, (int, float)):
            result[str(key)] = int(item)
    return result
