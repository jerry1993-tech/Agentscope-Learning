# -*- coding: utf-8 -*-
"""file_catalog 与目录管理，以及 tag_index 的规范化（契约 §3.16）。

**catalog 和 file_store 是两个不同的东西，别混**

ReMe 的工作区里有两套"文件清单"：

===================== =====================================================
组件                  它记什么
===================== =====================================================
``file_catalog``      文件名 → ``FileNode``（mtime / links / chunk_ids /
                      front_matter）。**没有正文**。
``file_store``        全部 chunk（正文 + embedding + BM25 索引 + tag 索引 +
                      wikilink 图）。
===================== =====================================================

catalog 是"轻量台账"，file_store 是"重索引"。所以：

- 只想登记"这个文件存在"（比如给 ``resource_watch_loop`` 提供扫描基线）→ 只需要 catalog；
- 想让内容可检索 → 必须进 file_store。

``config/default.yaml`` 里声明了 5 个 catalog：``default`` / ``digest`` / ``dream`` /
``proactive`` / ``resource``（``components.file_catalog`` 段）。它们都是 ``local``
后端（``third_party/ReMe/reme/components/file_catalog/local_file_catalog.py:11``），
落盘成 ``<workspace>/metadata/file_catalog/<name>.jsonl.zst``
（``local_file_catalog.py:25``）。

**``ensure_catalog`` 到底能做什么、不能做什么**

``Application._init_components``（``third_party/ReMe/reme/application.py:79-87``）
在启动时把配置里声明的 catalog 一次性实例化：

.. code-block:: python

    for ctype, group in self.config.components.items():
        self.context.components[ctype] = {}
        for name, cfg in group.items():
            self.context.components[ctype][name] = self._instantiate(...)

启动之后**没有**"新增 catalog"的公开 API。但实例化本身是可复用的
（``application.py:103`` 的 ``_instantiate`` 只有 10 行：查注册表 → 传
``app_context`` → 构造 → 类型检查），而 ``BaseComponent.start()``
（``components/base_component.py:225``）是公开的、会把 ``bind()`` 的依赖一起拉起来。
所以 :meth:`CatalogManager.ensure_catalog` 的实现是：

1. 已在 ``context.components["file_catalog"]`` 里 → 直接返回；
2. 不在 → 用 ``Application._instantiate`` 造一个 ``local`` 后端实例，
   ``await instance.start()``，再挂进 ``context.components``。

第 2 步用了带下划线的 ``_instantiate``，这是本模块对 ReMe 的一处依赖**私有 API**，
已记入 ``unresolved``。用它的理由：它是 Application 自己的装配逻辑，
照抄一遍等于把这段逻辑复制进 harness，一旦 ReMe 改了装配顺序（比如将来给
``_instantiate`` 加上依赖注入），复制出来的那份会静默过期。

**标签规范化的两个方向**

:meth:`CatalogManager.set_tags` 走**写入侧**（:func:`~harness_kit.memory.frontmatter.normalize_tags`，
每文件最多 3 个），:meth:`CatalogManager.normalize_query` 走**查询侧**
（:func:`~harness_kit.memory.frontmatter.normalize_query_tags`，不限条数）。
这条不对称是 ReMe 刻意设计的（``local_tag_index.py:132-134``），
harness 保留它、且把它提到 API 表面，就是为了让教程能指着它讲清楚。

**增量扫描与对账：为什么 harness 还要自己做一遍**

ReMe 自己**有**增量扫描 —— ``index_update_loop``（background job）里跑
``init_changes_step`` → ``watch_changes_step``。但它是一条**常驻循环**：
第二跳的 ``WatchChangesStep.execute()`` 是 ``async for ... in awatch(...)``
（``steps/index/watch_changes.py:93``），只有 ``stop_event`` 被设置才退出，
所以前台 ``run_job`` 会永久等待（实测 6s 超时仍不返回），
而且 ``init_changes_step`` 的产物落在 ``context["changes"]``
（``init_changes.py:75``）里，**job 结束就没了**，调用方拿不到"扫了什么"。

所以 :meth:`CatalogManager.scan_changes` 的做法是：**复用 ReMe 的算法，不复用它的循环**。

- 目录遍历用 ``collect_existing``（``steps/index/_watch_rules.py:63``）
  —— 与 ReMe 用同一个 walker，规则对象也是它的 ``WatchRule``；
- 差异计算用 ``InitChangesStep.diff``（``steps/index/init_changes.py:47``）
  —— 这是一个 **staticmethod**，不依赖任何组件，可以直接当纯函数调；
- 快照来源由我们指定：:meth:`CatalogManager.scan_changes` 用 **file_catalog**
  （轻量、只记 path + mtime），因为 harness 的常驻索引者是
  :class:`~harness_kit.memory.ingest.MemoryIngestor`，它自己握着
  ``ingest_state.json`` 这份更精细的快照。

有一处**必须**与 ReMe 的调用方式不同：``diff`` 的第三个参数是
``workspace_path``，它用来把 ``FileNode.path``（相对）拼成绝对路径再与磁盘比对。
ReMe 传的是 ``self.workspace_path``（``init_changes.py:79``），
而 ``Application`` 自己**不是**工作区绑定组件，它的 ``workspace_path``
是**进程 CWD**（实测：``app.workspace_path`` 得到仓库根，不是 ReMe 工作区）。
传错这一个参数，结果是"磁盘上的文件全被当成 added、索引里的全被当成 deleted"——
扫描结果永远全量重来，而且不报错。harness 一律传
:attr:`~harness_kit.memory.workspace.ReMeWorkspace.root`（已经 ``resolve()``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from .client import MemoryClient, MemoryUnavailableError
from .frontmatter import normalize_query_tags, normalize_tags

__all__ = [
    "CatalogManager",
    "ChangeSet",
    "DEFAULT_CATALOG",
    "DEFAULT_SCAN_SUFFIXES",
    "KEYS_PER_CATALOG",
    "ReconcileReport",
]

#: 默认 catalog 名（``config/default.yaml`` 的 ``components.file_catalog.default``）。
DEFAULT_CATALOG: str = "default"

#: 配置里声明的 catalog 名（harness 侧的便利常量，方便教程直接引用）。
#: 值来自 ``third_party/ReMe/reme/config/default.yaml`` 的 ``components.file_catalog``。
KEYS_PER_CATALOG: tuple[str, ...] = ("default", "digest", "dream", "proactive", "resource")

#: 增量扫描默认看的后缀。与 ``config/default.yaml`` 的 ``watch_suffixes: [md]`` 一致。
DEFAULT_SCAN_SUFFIXES: tuple[str, ...] = ("md",)


class ChangeSet(BaseModel):
    """一次增量扫描的结果（路径一律**工作区相对**）。

    ReMe 的 ``InitChangesStep.diff()`` 返回的是绝对路径
    （``third_party/ReMe/reme/steps/index/init_changes.py:47-62``）；
    harness 把它翻回工作区相对形式，因为契约里的 :class:`~harness_kit.memory.citations.MemoryHit`
    / ``IngestResult.path`` 全是相对路径，混用两种形式是标签索引报
    ``Invalid workspace-relative tag-index path`` 的直接原因。
    """

    model_config = ConfigDict(extra="forbid")

    added: list[str] = Field(default_factory=list)
    """磁盘上有、索引里没有的文件。"""

    modified: list[str] = Field(default_factory=list)
    """两边都有但 ``st_mtime`` 不一致的文件。"""

    deleted: list[str] = Field(default_factory=list)
    """索引里有、磁盘上已经没有的文件。"""

    counts: dict[str, int] = Field(default_factory=dict)
    """``{"added": n, "modified": n, "deleted": n}``，与 ReMe 的 ``diff`` 返回一致。"""

    def is_empty(self) -> bool:
        """是否没有任何变更。

        Returns:
            `bool`: 三类都为空时为 ``True``。
        """
        return not (self.added or self.modified or self.deleted)

    def total(self) -> int:
        """三类变更的条数之和。

        Returns:
            `int`: 条数。
        """
        return len(self.added) + len(self.modified) + len(self.deleted)

    def paths(self) -> list[str]:
        """需要**重新索引**的路径（added + modified，不含 deleted）。

        Returns:
            `list[str]`: 排好序的工作区相对路径。
        """
        return sorted([*self.added, *self.modified])


class ReconcileReport(BaseModel):
    """四方对账的结果：磁盘 / file_catalog / file_graph / 写入状态。

    四方各自回答一个不同的问题，**任何一方单独看都可能骗到你**：

    ==================== ====================================================
    来源                  它回答什么
    ==================== ====================================================
    磁盘（``rglob``）       "现在真实存在哪些文件"
    ``file_catalog``       "我登记过哪些文件存在的说法"
    ``file_graph``         "哪些文件真的进了检索索引（有 chunk）"
    ``ingest_state``       "我上次写的时候算出来的内容摘要是什么"
    ==================== ====================================================

    一个典型的不一致长这样：文件写进 ``resource/`` 之后**先** ``register()``
    进 catalog、**再**去 ``upsert``，中间进程被杀 → catalog 里有、graph 里没有。
    此时 ``InitChangesStep`` 如果用 ``monitor_type="file_catalog"`` 做快照，
    会认为"这个文件已是最新"，索引**永久少一个文件**。
    :meth:`CatalogManager.reconcile` 存在的意义就是把这种撕裂**说清楚**。
    """

    model_config = ConfigDict(extra="forbid")

    scanned_files: int = 0
    """磁盘上扫到的文件数。"""

    catalog_files: int = 0
    """``file_catalog`` 里的节点数。"""

    graph_files: int = 0
    """``file_graph`` 里的节点数。"""

    ingest_files: int = 0
    """``ingest_state.json`` 里记录的文件数。"""

    missing_in_catalog: list[str] = Field(default_factory=list)
    """磁盘上有、catalog 里没有（= 需要 ``register``）。"""

    missing_in_graph: list[str] = Field(default_factory=list)
    """磁盘上有、graph 里没有（= 需要 ``file_store.upsert``，否则检索不到）。"""

    catalog_mtime_drift: list[str] = Field(default_factory=list)
    """catalog 记的 ``st_mtime`` 与磁盘不一致（= 文件被改过）。"""

    graph_mtime_drift: list[str] = Field(default_factory=list)
    """graph 记的 ``st_mtime`` 与磁盘不一致。"""

    stale_in_catalog: list[str] = Field(default_factory=list)
    """catalog 里有、磁盘上已经没有（= 残留台账项，会让增量扫描漏掉同名新文件）。"""

    stale_in_graph: list[str] = Field(default_factory=list)
    """graph 里有、磁盘上已经没有（= **检索会召回一个不存在的文件**，最危险的一类）。"""

    untracked_by_ingest: list[str] = Field(default_factory=list)
    """在索引里、但写入状态里没有（= 不是走 :class:`~harness_kit.memory.ingest.MemoryIngestor` 进来的）。"""

    orphaned_in_ingest: list[str] = Field(default_factory=list)
    """写入状态里有、磁盘上已经没有了（= 被手动删了，幂等判据会永远跳过它）。"""

    @property
    def clean(self) -> bool:
        """八类不一致是否全空。

        Returns:
            `bool`: 全空为 ``True``。
        """
        return not (
            self.missing_in_catalog
            or self.missing_in_graph
            or self.catalog_mtime_drift
            or self.graph_mtime_drift
            or self.stale_in_catalog
            or self.stale_in_graph
            or self.untracked_by_ingest
            or self.orphaned_in_ingest
        )

    def summary(self) -> str:
        """一行人类可读的结论。

        Returns:
            `str`: 形如 ``"对账 OK: 磁盘 3 / catalog 3 / graph 3 / ingest 3"``。
        """
        head = "对账 OK" if self.clean else "发现不一致"
        return (
            f"{head}: 磁盘 {self.scanned_files} / catalog {self.catalog_files} / "
            f"graph {self.graph_files} / ingest {self.ingest_files}"
        )


class CatalogManager:
    """catalog 与标签的管理入口（契约 §3.16）。

    Example::

        catalogs = CatalogManager(client)
        await catalogs.ensure_catalog("project_a")     # 运行时新造一个 local catalog
        effective = await catalogs.set_tags("resource/ops.md", ["Ops", "runbook", "x", "y"])
        print(effective)                                # ['ops', 'runbook', 'x'] —— 第 4 个被裁掉
    """

    def __init__(self, client: MemoryClient) -> None:
        """构造管理器。

        Args:
            client (`MemoryClient`): 已 start 的客户端。
        """
        self.client = client

    # ------------------------------------------------------------------
    # catalog
    # ------------------------------------------------------------------
    async def ensure_catalog(self, name: str) -> None:
        """确保名为 ``name`` 的 catalog 存在（存在则什么都不做）。

        Args:
            name (`str`): catalog 名。

        Raises:
            `ValueError`: ``name`` 为空。
            `MemoryUnavailableError`: ``local`` 后端未注册，或实例化/启动失败。
        """
        catalog_name = str(name or "").strip()
        if not catalog_name:
            raise ValueError("catalog 名不能为空")

        context = self.client.application.context
        group = context.components.setdefault("file_catalog", {})
        if catalog_name in group:
            return

        try:
            from reme.components.file_catalog import BaseFileCatalog
            from reme.enumeration import ComponentEnum
            from reme.schema import ComponentConfig
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise MemoryUnavailableError(f"reme 不可导入: {exc}") from exc

        instantiate = getattr(self.client.application, "_instantiate", None)
        if instantiate is None:
            raise MemoryUnavailableError(
                "Application 没有 _instantiate，无法在运行时新增 catalog；"
                f"请改在装配期声明（HarnessMemoryConfig.with_components）。需要: {catalog_name}",
            )

        cfg = ComponentConfig(backend="local")
        try:
            instance = instantiate(
                ComponentEnum.FILE_CATALOG,
                cfg,
                label=f"Component '{catalog_name}'",
                expected_type=BaseFileCatalog,
                name=catalog_name,
            )
            await instance.start()
        except Exception as exc:  # noqa: BLE001 - 统一成 harness 自己的异常类型
            raise MemoryUnavailableError(f"创建 catalog {catalog_name!r} 失败: {exc}") from exc

        group[catalog_name] = instance
        logger.info("ensure_catalog: 运行时新建 local catalog {}", catalog_name)

    async def list_catalogs(self) -> list[str]:
        """列出当前可用的 catalog（契约 §3.16）。

        Returns:
            `list[str]`: 名字，已排序。
        """
        context = self.client.application.context
        return sorted((context.components.get("file_catalog") or {}).keys())

    async def catalog_paths(self, name: str = DEFAULT_CATALOG) -> list[str]:
        """列出一个 catalog 里登记的文件路径。

        ``LocalFileCatalog`` 把节点放在 ``_nodes`` 里，没有公开的"列全部"方法，
        所以这里用 ``get_nodes``；如果后端没实现它，就退化成读私有字典。
        退化路径存在的原因是：``BaseFileCatalog`` 的抽象方法集里并没有
        "列出全部"，不同后端能力不一样。

        Args:
            name (`str`): catalog 名。

        Returns:
            `list[str]`: 已排序的路径列表；catalog 不存在时返回空列表。
        """
        context = self.client.application.context
        catalog = (context.components.get("file_catalog") or {}).get(name)
        if catalog is None:
            return []
        getter = getattr(catalog, "get_nodes", None)
        try:
            if getter is not None:
                nodes = await getter(None)
                return sorted(node.path for node in nodes)
        except Exception as exc:  # noqa: BLE001 - 退化到私有字典
            logger.debug("catalog.get_nodes 失败（{}），退化读 _nodes", exc)
        nodes = getattr(catalog, "_nodes", None)
        if isinstance(nodes, dict):
            return sorted(nodes)
        logger.warning("catalog {!r} 既无 get_nodes 也无 _nodes，无法列举", name)
        return []

    async def register(self, path: str, *, catalog: str = DEFAULT_CATALOG) -> bool:
        """把一个文件登记进 catalog（**不建索引**，只记存在）。

        Args:
            path (`str`): 工作区相对路径。
            catalog (`str`): catalog 名；不存在会自动创建。

        Returns:
            `bool`: 登记成功返回 ``True``。
        """
        await self.ensure_catalog(catalog)
        context = self.client.application.context
        target = context.components["file_catalog"][catalog]
        try:
            from reme.schema import FileNode
        except ImportError as exc:  # pragma: no cover
            raise MemoryUnavailableError(f"reme 不可导入: {exc}") from exc

        absolute = self._absolute(path)
        stat = await _stat(absolute)
        if stat is None:
            logger.warning("register: {} 不存在，跳过", absolute)
            return False
        await target.upsert([FileNode(path=str(path), st_mtime=stat)])
        await target.dump()
        return True

    # ------------------------------------------------------------------
    # 标签
    # ------------------------------------------------------------------
    async def set_tags(self, path: str, tags: list[str]) -> list[str]:
        """给一个文件设置标签，返回**实际生效**的标签（契约 §3.16）。

        真正干活的是 ``tag_index``。它从 ``file_graph`` 的节点上取
        ``front_matter.model_extra[tag_key]``
        （``third_party/ReMe/reme/components/tag_index/local_tag_index.py:77-78``），
        而节点是 ``file_store`` 维护的。所以本方法按顺序做两件事：

        1. 把标签写进**磁盘上的 front matter**（这个文件必须已在工作区里）；
        2. 重新分块并 ``file_store.upsert``，让新 front matter 进图 → 进 tag_index。

        第 2 步会复用 :class:`~harness_kit.memory.ingest.MemoryIngestor`，
        因为在 ReMe 里"改标签"本质上就是"文件内容变了，重新索引"。

        Args:
            path (`str`): 工作区相对路径（或绝对路径，但必须在工作区内）。
            tags (`list[str]`): 期望的标签。

        Returns:
            `list[str]`: 实际生效的标签。

        Raises:
            `FileNotFoundError`: 文件不存在。
            `ValueError`: 文件在工作区外。
        """
        from .ingest import MemoryIngestor
        from .workspace import ReMeWorkspace

        workspace = ReMeWorkspace(root=self.client.workspace_dir)
        absolute = self._absolute(path)
        if not workspace.is_inside(absolute):
            raise ValueError(
                f"set_tags 只能改工作区内的文件: {absolute}（工作区 {workspace.root}）。"
                "区外文件请先 ingest.add_file(copy_into_workspace=True) 复制进来。",
            )
        if not await _exists(absolute):
            raise FileNotFoundError(f"文件不存在: {absolute}")

        ingestor = MemoryIngestor(self.client, workspace=workspace)
        effective = await ingestor.apply_tags(absolute, tags)
        # 重新入库一次：apply_tags 改了文件字节，digest 变了，
        # 所以 add_file 不会走"未变跳过"分支，会把新 front matter 推进图与 tag_index。
        result = await ingestor.add_file(absolute)
        if not result.added and result.skipped_reason is not None:
            logger.warning("set_tags: {} 重新索引被跳过（{}）", path, result.skipped_reason)
        logger.info("set_tags: {} → {}", path, effective)
        return effective

    async def list_tags(
        self,
        *,
        page_size: int = 200,
        order_by: str = "tag",
        order: str | None = None,
    ) -> dict[str, int]:
        """统计各标签出现次数（自动翻页汇总）。

        ``LocalTagIndex.list_tags`` 是**分页**接口
        （``third_party/ReMe/reme/components/tag_index/local_tag_index.py:150-199``），
        返回 ``{"total_tags", "total_pages", "page", "range", "items"}``，
        ``items`` 是 ``[(tag, count), ...]``，``page_size`` 上限 1000。
        本方法按页取完再合成一个 dict —— 记忆工作区的标签量级是几十个，
        一次翻页就够了，但接口本身必须是"要全部"的语义，
        否则调用方拿到的是"前 100 个标签"，而它自己不知道。

        Args:
            page_size (`int`): 每页大小，上限 1000。
            order_by (`str`): ``"tag"`` 或 ``"file_count"``。
            order (`str | None`): ``"asc"`` / ``"desc"``；``None`` 走 ReMe 的默认
                （``order_by="tag"`` 时升序，``"file_count"`` 时降序）。

        Returns:
            `dict[str, int]`: 标签 → 文件数。

        Raises:
            `ValueError`: ``page_size`` 不合法（由 ReMe 抛出）。
            `RuntimeError`: tag_index 不健康（由 ReMe 抛出）。
        """
        tag_index = self.client.component("tag_index", "default")
        result: dict[str, int] = {}
        page = 1
        while page <= 1000:
            payload = await tag_index.list_tags(
                page=page,
                page_size=int(page_size),
                order_by=str(order_by),
                order=order,
            )
            for tag, count in payload.get("items") or []:
                result[str(tag)] = int(count)
            total_pages = int(payload.get("total_pages", 0) or 0)
            if page >= max(1, total_pages):
                break
            page += 1
        return result

    async def tags_page(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        order_by: str = "tag",
        order: str | None = None,
    ) -> dict[str, Any]:
        """直接透出 ReMe 的分页结构（要给 UI 做分页时用）。

        Args:
            page (`int`): 页码（1-based）。
            page_size (`int`): 每页大小。
            order_by (`str`): ``"tag"`` 或 ``"file_count"``。
            order (`str | None`): ``"asc"`` / ``"desc"``。

        Returns:
            `dict[str, Any]`: ``{"total_tags", "total_pages", "page", "range", "items"}``。
        """
        tag_index = self.client.component("tag_index", "default")
        return dict(
            await tag_index.list_tags(
                page=int(page),
                page_size=int(page_size),
                order_by=str(order_by),
                order=order,
            ),
        )

    async def paths_for_tags(self, tags: Sequence[str], *, match_all: bool = True) -> list[str]:
        """按标签反查文件路径。

        Args:
            tags (`Sequence[str]`): 标签。
            match_all (`bool`): ``True`` 是 AND（必须全含），``False`` 是 OR。
                对应 ``local_tag_index.py:129`` 的 ``match_all`` 参数。

        Returns:
            `list[str]`: 文件路径。
        """
        tag_index = self.client.component("tag_index", "default")
        return list(await tag_index.paths_for_tags(list(tags), match_all=bool(match_all)))

    @staticmethod
    def normalize_write(tags: object) -> list[str]:
        """按**写入侧**规则规范化标签（最多 3 个）。

        Args:
            tags (`object`): 原始标签。

        Returns:
            `list[str]`: 规范化结果。
        """
        return normalize_tags(tags)

    @staticmethod
    def normalize_query(tags: object) -> list[str]:
        """按**查询侧**规则规范化标签（不截断条数）。

        Args:
            tags (`object`): 原始标签。

        Returns:
            `list[str]`: 规范化结果。
        """
        return normalize_query_tags(tags)

    # ------------------------------------------------------------------
    # 增量扫描与对账
    # ------------------------------------------------------------------
    async def scan_changes(
        self,
        *,
        dirs: Sequence[str | Path] | None = None,
        suffixes: Sequence[str] | None = None,
        catalog: str = DEFAULT_CATALOG,
        recursive: bool = True,
    ) -> ChangeSet:
        """扫一遍磁盘，和 catalog 快照 diff，返回 added / modified / deleted。

        用的是 ReMe 自己的 walker 与 diff 算法（见模块 docstring），
        harness 只做两件事：**换快照**（catalog 而不是 file_store）、
        **把绝对路径翻回工作区相对路径**。

        Args:
            dirs (`Sequence[str | Path] | None`): 要扫的目录；``None`` 时扫
                ``resource/`` / ``daily/`` / ``digest/`` 三个"用户可写"目录
                （与 ``config/default.yaml`` 的 ``index_update_loop`` 监控范围一致，
                但那里还有 ``session/``，见 ``default.yaml`` 的 ``watch_dirs``）。
            suffixes (`Sequence[str] | None`): 后缀白名单（不带点），
                默认 :data:`DEFAULT_SCAN_SUFFIXES`。
            catalog (`str`): 用哪个 catalog 当"已索引快照"。
            recursive (`bool`): 是否递归子目录。

        Returns:
            `ChangeSet`: 变更集。

        Raises:
            `MemoryUnavailableError`: ``reme`` 不可导入。
        """
        try:
            from reme.steps.index._watch_rules import WatchRule, collect_existing
            from reme.steps.index.init_changes import InitChangesStep
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise MemoryUnavailableError(f"reme 不可导入: {exc}") from exc

        workspace = self._workspace()
        rules = self._watch_rules(dirs, suffixes)
        existing = self._resolved_existing(collect_existing(rules, recursive=recursive))
        nodes = await self._catalog_nodes(catalog)
        changes, counts = InitChangesStep.diff(existing, nodes, Path(workspace.root))
        result = ChangeSet(counts=dict(counts))
        for item in changes:
            bucket = {
                "added": result.added,
                "modified": result.modified,
                "deleted": result.deleted,
            }.get(str(item.get("change")))
            if bucket is None:
                continue
            bucket.append(workspace.relative(str(item.get("path", ""))))
        logger.info("scan_changes: {} catalog={!r}", result.counts, catalog)
        return result

    async def reconcile(
        self,
        *,
        dirs: Sequence[str | Path] | None = None,
        suffixes: Sequence[str] | None = None,
        catalog: str = DEFAULT_CATALOG,
        ingested: Mapping[str, Any] | None = None,
    ) -> ReconcileReport:
        """四方对账：磁盘 / file_catalog / file_graph / 写入状态。

        Args:
            dirs (`Sequence[str | Path] | None`): 要扫的目录，同 :meth:`scan_changes`。
            suffixes (`Sequence[str] | None`): 后缀白名单，同 :meth:`scan_changes`。
            catalog (`str`): 用哪个 catalog 当台账。
            ingested (`Mapping[str, Any] | None`): 写入状态，
                一般传 ``await MemoryIngestor(...).ingested()``；
                ``None`` 时跳过与写入状态相关的两项检查。

        Returns:
            `ReconcileReport`: 对账报告。

        Raises:
            `MemoryUnavailableError`: ``reme`` 不可导入。
        """
        try:
            from reme.steps.index._watch_rules import collect_existing
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise MemoryUnavailableError(f"reme 不可导入: {exc}") from exc

        workspace = self._workspace()
        rules = self._watch_rules(dirs, suffixes)
        existing = self._resolved_existing(collect_existing(rules, recursive=True))
        on_disk = {
            workspace.relative(path): mtime
            for path, mtime in existing.items()
        }

        catalog_nodes = {node.path: float(node.st_mtime) for node in await self._catalog_nodes(catalog)}
        graph_nodes = {
            node.path: float(node.st_mtime)
            for node in await self.client.component("file_store", "default").file_graph.get_nodes()
        }

        report = ReconcileReport(
            scanned_files=len(on_disk),
            catalog_files=len(catalog_nodes),
            graph_files=len(graph_nodes),
        )
        for path in sorted(on_disk):
            if path not in catalog_nodes:
                report.missing_in_catalog.append(path)
            elif catalog_nodes[path] != on_disk[path]:
                report.catalog_mtime_drift.append(path)
            if path not in graph_nodes:
                report.missing_in_graph.append(path)
            elif graph_nodes[path] != on_disk[path]:
                report.graph_mtime_drift.append(path)

        report.stale_in_catalog = sorted(path for path in catalog_nodes if path not in on_disk)
        report.stale_in_graph = sorted(path for path in graph_nodes if path not in on_disk)

        if ingested is not None:
            report.ingest_files = len(ingested)
            indexed = set(graph_nodes) | set(catalog_nodes)
            report.orphaned_in_ingest = sorted(path for path in ingested if path not in on_disk)
            report.untracked_by_ingest = sorted(path for path in indexed if path not in ingested)

        logger.info("reconcile: {} missing_catalog={} missing_graph={}", report.summary(), len(report.missing_in_catalog), len(report.missing_in_graph))
        return report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _workspace(self) -> Any:
        """惰性构造工作区模型（``ReMeWorkspace`` 不依赖 ReMe，构造很便宜）。

        Returns:
            `Any`: :class:`~harness_kit.memory.workspace.ReMeWorkspace`。
        """
        from .workspace import ReMeWorkspace

        return ReMeWorkspace(root=self.client.workspace_dir)

    def _watch_rules(self, dirs: Sequence[str | Path] | None, suffixes: Sequence[str] | None) -> list[Any]:
        """构造 ReMe 的 ``WatchRule`` 列表。

        Args:
            dirs (`Sequence[str | Path] | None`): 目录；``None`` 走默认三目录。
            suffixes (`Sequence[str] | None`): 后缀白名单。

        Returns:
            `list[Any]`: ``WatchRule`` 列表。
        """
        from reme.steps.index._watch_rules import WatchRule

        workspace = self._workspace()
        if dirs is None:
            roots: list[Path] = [
                Path(workspace.resource_path()),
                Path(workspace.daily_path()),
                Path(workspace.digest_path()),
            ]
        else:
            roots = [
                Path(item) if Path(item).is_absolute() else Path(workspace.resolve_relative(item))
                for item in dirs
            ]
        wanted = [str(s).strip(".") for s in (suffixes or DEFAULT_SCAN_SUFFIXES)]
        return [WatchRule(path=root, suffixes=list(wanted)) for root in roots]

    @staticmethod
    def _resolved_existing(existing: dict[str, float]) -> dict[str, float]:
        """把 ``collect_existing`` 的 key 统一 ``resolve()``。

        ``collect_existing`` 用的是 ``Path.absolute()``（``_watch_rules.py:69``），
        它**不解析符号链接**。在 macOS 上 ``/tmp`` 是 ``/private/tmp`` 的软链，
        而 ``ReMeWorkspace.root`` 已经 ``resolve()`` 过 —— 两边不 resolve 就会
        各拿到一套前缀，``diff`` 于是把每个文件同时算成 added 和 deleted。
        这是本仓库第 10 讲记录过的同一个坑，在写入路径上又出现了一次。

        Args:
            existing (`dict[str, float]`): 绝对路径 → mtime。

        Returns:
            `dict[str, float]`: 已 resolve 的同一份映射。
        """
        return {str(Path(path).resolve()): mtime for path, mtime in existing.items()}

    async def _catalog_nodes(self, catalog: str) -> list[Any]:
        """取一个 catalog 的全部节点（不存在时抛错，而不是静默返回空）。

        Args:
            catalog (`str`): catalog 名。

        Returns:
            `list[Any]`: ``FileNode`` 列表。

        Raises:
            `MemoryUnavailableError`: catalog 不存在。
        """
        group = self.client.application.context.components.get("file_catalog") or {}
        target = group.get(catalog)
        if target is None:
            raise MemoryUnavailableError(
                f"catalog {catalog!r} 不存在；可用: {sorted(group)}。"
                "运行时新增请用 CatalogManager.ensure_catalog()。",
            )
        return list(await target.get_nodes(None))

    def _absolute(self, path: str) -> Any:
        """把路径解析成绝对的 ``Path``。

        Args:
            path (`str`): 相对或绝对路径。

        Returns:
            `Any`: ``pathlib.Path``。
        """
        from pathlib import Path

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self.client.workspace_dir) / candidate
        return candidate


async def _exists(path: Any) -> bool:
    """异步判断文件是否存在。

    Args:
        path (`Any`): ``Path``。

    Returns:
        `bool`: 是否存在。
    """
    import asyncio

    return await asyncio.to_thread(path.is_file)


async def _stat(path: Any) -> float | None:
    """异步取 mtime。

    Args:
        path (`Any`): ``Path``。

    Returns:
        `float | None`: mtime 秒；文件不存在返回 ``None``。
    """
    import asyncio

    def _run() -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    return await asyncio.to_thread(_run)
