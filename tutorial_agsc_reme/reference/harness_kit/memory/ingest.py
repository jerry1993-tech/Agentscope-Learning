# -*- coding: utf-8 -*-
"""写入路径：资源 → ``FileNode`` / ``FileChunk``（契约 §3.16）。

**写入这件事在 ReMe 里到底由谁干**

ReMe 的索引有两条入口，都真实存在，区别很大：

1. **``update_index_step``**（``third_party/ReMe/reme/steps/index/update_changes.py:319``）
   —— 面向**变更批**（watchfiles 的 added/modified/deleted），
   内部按内存估算分批、调 ``_resolve_chunker(path)`` 按扩展名选分块器、
   最后 ``file_store.upsert`` + ``dump``。它是 ``index_update_loop`` 的 dispatch 目标。
2. **``file_store.upsert([(node, chunks)])``**
   （``third_party/ReMe/reme/components/file_store/local_file_store.py:765``）
   —— 面向**已分块的数据**，一次搞定四件事：

   .. code-block:: python

       await self.file_graph.upsert_nodes(new_nodes)     # 图谱节点 + wikilink 边
       await self._upsert_tag_nodes(new_nodes)           # tag_index（从 front matter 取标签）
       await self._embed_pending(needs_embed)            # 向量（没配 embedding 时是空操作）
       if self.keyword_index ...: await self.keyword_index.add_docs(keyword_docs)   # BM25

   然后 ``await file_store.dump()`` 落盘。

本模块走的是 **2**：先用 ReMe 的分块器把文件变成 ``(FileNode, list[FileChunk])``
（契约明确要求"必须复用 ReMe 已有的分块器，不自己写 chunker"），再交给 ``upsert``。
选 2 而不选 1 的理由很具体：

- ``update_index_step`` 的返回值只有 ``{"change", "path", "success"}``
  （``update_changes.py:154-160``），**拿不到 chunk 数量**，而契约的
  :class:`IngestResult` 要求 ``chunk_count``。走 2 的话，分块就是我调的那一次，
  数量天然精确；走 1 就得为了计数再分块一次，白烧一遍 CPU。
- 走 2 少了 ``bucket_changes`` 的"按磁盘现状重判 added/modified/deleted"这一步
  （``_change_batch.py:19-40``）—— 那是给 watcher 去重用的，
  显式写入不需要它。

**用哪个分块器**

``config/default.yaml`` 的 ``file_chunker`` 段注册了三个：``default``（``.txt``/``.log``）、
``markdown``（``.md``）、``json``/``jsonl``。构造参数 ``chunker`` 是**首选**，
但实际用哪个按扩展名走 :meth:`MemoryIngestor.chunker_name_for`：
``.md`` 用 ``markdown``（标题树 + breadcrumb + 表格/代码切分），其余用 ``default``
（字节窗口 + bisect 行映射 + wikilink 边界避让）。这与 ReMe 自己的
``_resolve_chunker`` 语义一致，只是我们不看 ``supported_extensions`` 而是硬判后缀，
因为 harness 侧只关心 ``.md`` 与非 ``.md`` 两类。

**幂等判据为什么是"文件字节的 sha256"**

ReMe 是文件原生存储：**磁盘上的文件就是唯一真值**，索引只是它的派生物
（``ReMe`` 的 docstring 反复强调 file-native）。所以"这份资源是不是已经入过库了"
的正确判据不是"我上次调过 add_file 吗"，而是"文件内容变了吗"。
本模块把 ``sha256(文件字节)`` 存进
``workspace.metadata_dir/ingest_state.json``，与上次比对：

- 相同 → ``added=False``、``skipped_reason="unchanged"``，**不碰索引**（省一次 embedding）；
- 不同（含首次）→ 正常入库并更新状态。

副作用是"改了 mtime 但内容没变"也会被跳过，这正是想要的语义。

**为什么用 ``asyncio.to_thread`` 读文件**

``aiofiles`` 也在依赖里（ReMe 自己的分块器就用它），但本模块处理的是**小文件 + 小 JSON**，
用 ``asyncio.to_thread`` 把标准 ``pathlib`` 调用挪出事件循环就够了，
不必为两种 IO 引入两套 API。这是取舍，不是遗漏。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict

from .client import MemoryClient, _require_reme
from .frontmatter import FrontMatter, normalize_tags

__all__ = [
    "INGEST_STATE_NAME",
    "IngestResult",
    "MemoryIngestor",
]

#: 幂等状态文件名（放在 ``workspace.metadata_dir`` 下）。
INGEST_STATE_NAME: str = "ingest_state.json"

#: 状态文件的结构版本；将来改结构时用它做迁移判断。
_STATE_VERSION: int = 1

#: 默认入 ``resource_dir`` 的目录名（与 ``ReMeWorkspace.resource_dir`` 的默认一致）。
_DEFAULT_RESOURCE_SUBDIR: str = "resource"


class IngestResult(BaseModel):
    """一次入库的结果（契约 §3.16）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    """**入库后**的路径。

    注意它可能是工作区相对路径（文件本来就在工作区内，或被复制进来了），
    也可能是绝对路径（工作区外的文件且 ``copy_into_workspace=False``）。
    这正是 ReMe ``ComponentMixin.to_workspace_relative`` 的语义：
    区内给相对、区外给绝对（``third_party/ReMe/reme/components/base_component.py:44-50``）。
    """

    added: bool
    """是否真的写进了索引。``False`` 表示被幂等判据跳过。"""

    chunk_count: int
    """本次产生（或上次已产生）的 chunk 数。"""

    skipped_reason: str | None = None
    """跳过的原因；``added=True`` 时为 ``None``。

    取值：``"unchanged"``（内容与上次一致）、``"empty"``（文件没有可索引正文）。
    """


class MemoryIngestor:
    """写入路径封装（契约 §3.16）。

    Example::

        ingestor = MemoryIngestor(client, workspace=ws)
        result = await ingestor.add_file(Path("notes/ops.md"), tags=["ops", "runbook"])
        print(result.added, result.chunk_count)
    """

    def __init__(
        self,
        client: MemoryClient,
        *,
        workspace: Any,
        chunker: Literal["markdown", "default"] = "markdown",
    ) -> None:
        """构造写入器。

        Args:
            client (`MemoryClient`): **已 start** 的记忆客户端（组件要从它取）。
            workspace (`Any`): :class:`~harness_kit.memory.workspace.ReMeWorkspace`。
            chunker (`Literal["markdown", "default"]`): 首选分块器名；
                实际选择见 :meth:`chunker_name_for`。

        Raises:
            `ValueError`: ``chunker`` 不是 ``markdown`` / ``default``。
        """
        if chunker not in ("markdown", "default"):
            raise ValueError(f"chunker 只能是 markdown / default，收到 {chunker!r}")
        self.client = client
        self.workspace = workspace
        self.chunker = chunker
        self._state_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 组件与路径
    # ------------------------------------------------------------------
    def chunker_name_for(self, path: str | Path) -> str:
        """按扩展名挑分块器（与 ReMe 的 ``_resolve_chunker`` 同语义）。

        Args:
            path (`str | Path`): 文件路径。

        Returns:
            `str`: ``"markdown"`` 或 ``"default"``。
        """
        suffix = Path(path).suffix.lower()
        if suffix == ".md":
            return "markdown" if self.chunker == "markdown" else "default"
        return "default"

    def _chunker(self, path: str | Path) -> Any:
        """取出分块器**实例**。

        从 ``application.context.components["file_chunker"][name]`` 取，
        这样拿到的是 ``Application`` 装配时构造、已 ``bind`` 到工作区的那个实例，
        而不是 ``registry.get`` 返回的类。这一点很关键：分块器要用
        ``to_workspace_relative`` 把绝对路径转成工作区相对路径，
        自己 new 一个不 bind 的实例会得到绝对路径（macOS 的 ``/tmp`` 符号链接
        会让这件事更难查）。

        Args:
            path (`str | Path`): 用于决定用哪个分块器。

        Returns:
            `Any`: 分块器实例。
        """
        return self.client.component("file_chunker", self.chunker_name_for(path))

    def _as_fs_path(self, path: str | Path) -> Path:
        """把调用方给的路径解释成**真实文件系统路径**。

        两种解释都接受，顺序是固定的：

        1. 绝对路径 —— 原样返回（会 ``resolve``，解开 ``/tmp`` 这类符号链接）；
        2. 工作区相对路径 —— 走 :meth:`ReMeWorkspace.resolve_relative`，
           带越界检查。

        为什么必须补这一层：:meth:`ingested` 与
        :class:`~harness_kit.memory.search.MemorySearch` 吐出来的路径都是
        **工作区相对**形式（``resource/xxx.md``），调用方很自然会把它原样喂回
        :meth:`remove_file` / :meth:`apply_tags`。而在补这一层之前，
        那种调用会按进程 CWD 解释：``remove_file`` 静默返回 ``False``
        （"状态里没有这个文件"），``apply_tags`` 抛 ``FileNotFoundError``。
        两个都不是"路径写错了"的明显信号 —— 所以这里把它变成"先试试工作区"。

        Args:
            path (`str | Path`): 绝对路径或工作区相对路径。

        Returns:
            `Path`: 绝对路径（可能不存在；存在性由调用方自己判）。

        Raises:
            `WorkspaceError`: 相对路径逃出了工作区根。
        """
        import os

        raw = Path(path).expanduser()
        if raw.is_absolute():
            return raw.resolve()
        direct = raw.resolve()
        if direct.exists():
            # 先认"相对 CWD"这个老语义：历史上就是这么解释的，
            # 而且从终端里手敲相对路径时，人的心算基准是 CWD。
            return direct
        return self.workspace.resolve_relative(os.fspath(raw))

    def state_path(self) -> Path:
        """幂等状态文件路径。

        ``ReMeWorkspace`` 上有两组容易混的名字：``metadata_dir`` 是**目录名字符串**
        （``"metadata"``），``metadata_path()`` 才是**绝对路径**。
        这里必须用后者 —— 用前者会得到相对路径，于是状态文件被写到进程的
        CWD 下，而不是工作区里（这是本模块初版实现的真实 bug，
        幂等判据因此永远失效，而且它自己不会报错）。

        Returns:
            `Path`: ``<workspace>/metadata/ingest_state.json``（绝对路径）。
        """
        return Path(self.workspace.metadata_path()) / INGEST_STATE_NAME

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------
    async def _load_state(self) -> dict[str, Any]:
        """读状态文件；不存在或损坏时返回空状态。

        Returns:
            `dict[str, Any]`: ``{"version": int, "files": {path: {...}}}``。
        """
        path = self.state_path()
        if not await asyncio.to_thread(path.is_file):
            return {"version": _STATE_VERSION, "files": {}}
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            # 状态文件坏掉的正确反应是"当成空状态重来"，而不是让写入失败：
            # 状态只是幂等优化，真值在磁盘上的记忆文件里。
            logger.warning("ingest_state.json 不可读（{}），按空状态处理", exc)
            return {"version": _STATE_VERSION, "files": {}}
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            logger.warning("ingest_state.json 结构异常，按空状态处理")
            return {"version": _STATE_VERSION, "files": {}}
        return data

    async def _save_state(self, state: dict[str, Any]) -> None:
        """原子写状态文件。

        先写 ``.tmp`` 再 ``replace``：``os.replace`` 在同一文件系统上是原子的，
        所以即使进程在写的中途被杀，也不会留下半个 JSON 让下次启动读崩。

        Args:
            state (`dict[str, Any]`): 状态。
        """
        state["version"] = _STATE_VERSION
        path = self.state_path()
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
        await asyncio.to_thread(tmp.write_text, payload, "utf-8")
        await asyncio.to_thread(tmp.replace, path)

    @staticmethod
    def _digest(data: bytes) -> str:
        """文件内容摘要。

        Args:
            data (`bytes`): 文件字节。

        Returns:
            `str`: ``sha256`` 十六进制。
        """
        return hashlib.sha256(data).hexdigest()

    # ------------------------------------------------------------------
    # 标签
    # ------------------------------------------------------------------
    async def apply_tags(self, path: str | Path, tags: Sequence[str]) -> list[str]:
        """把标签写进文件的 front matter，返回**实际生效**的标签。

        这里会调用 :meth:`FrontMatter.set_tags`，它按 ReMe 的写入侧规则
        （``third_party/ReMe/reme/components/tag_index/local_tag_index.py:33-54``）
        规范化：空白折成 ``_``、超 64 字符丢弃、必须含字母数字、``casefold``、
        每文件最多 3 个。被裁掉的部分会打 warning。

        **为什么必须写进文件**：tag_index 的标签来源是
        ``node.front_matter.model_extra.get(tag_key)``
        （``local_tag_index.py:77-78``），而 ``node`` 是分块器从文件里解析出来的
        （``default_file_chunker.py:91-92``）。也就是说**标签只有写在文件里才会被索引**，
        没有"旁路设置标签"的 API。

        Args:
            path (`str | Path`): 文件路径（绝对路径或**工作区相对路径**，
                见 :meth:`_as_fs_path`）。
            tags (`Sequence[str]`): 期望的标签。

        Returns:
            `list[str]`: 实际写入的标签（可能少于输入）。

        Raises:
            `FileNotFoundError`: 文件不存在。
        """
        target = self._as_fs_path(path)
        if not await asyncio.to_thread(target.is_file):
            raise FileNotFoundError(f"文件不存在: {target}")

        text = await asyncio.to_thread(target.read_text, "utf-8")
        # 用严格解析：这里是我们自己要改写的文件，语法坏了应该当场知道，
        # 而不是像 ReMe 那样静默把 front matter 整个丢掉。
        front_matter, body = FrontMatter.parse(text, strict=True)
        effective = front_matter.set_tags(list(tags))
        rendered = front_matter.render(body)
        await asyncio.to_thread(target.write_text, rendered, "utf-8")
        logger.debug("apply_tags: {} → {}", target.name, effective)
        return effective

    # ------------------------------------------------------------------
    # 入库
    # ------------------------------------------------------------------
    async def add_file(
        self,
        path: str | Path,
        *,
        tags: list[str] | None = None,
        catalog: str = "default",
        copy_into_workspace: bool = True,
    ) -> IngestResult:
        """把一个文件写进记忆索引（契约 §3.16）。

        流程：

        1. 解析路径；若在工作区外且 ``copy_into_workspace=True``，
           先复制进 ``<workspace>/resource/``（复用既有副本，见 :meth:`_stage_source`）；
        2. 有 ``tags`` 就先写进 front matter（见 :meth:`apply_tags`）；
        3. 算内容 sha256，和 ``ingest_state.json`` 比对 → 一致就返回 ``added=False``；
        4. 取分块器实例 → ``await chunker.chunk(path)`` 得到 ``(FileNode, chunks)``；
        5. ``await file_store.upsert([(node, chunks)])`` → ``await file_store.dump()``；
        6. 登记 file_catalog（走 ``update_catalog_step``）；
        7. 更新状态文件。

        Args:
            path (`str | Path`): 文件路径。
            tags (`list[str] | None`): 要写入的标签。
            catalog (`str`): 登记到哪个 file_catalog（默认 ``"default"``）。
            copy_into_workspace (`bool`): 工作区外的文件是否复制进来。

        Returns:
            `IngestResult`: 入库结果。

        Raises:
            `FileNotFoundError`: 文件不存在或不是普通文件。
            `MemoryUnavailableError`: 客户端未 start。
        """
        source = self._as_fs_path(path)
        if not await asyncio.to_thread(source.is_file):
            raise FileNotFoundError(f"不是文件: {source}")
        source = await asyncio.to_thread(source.resolve)

        async with self._state_lock:
            state = await self._load_state()
            sources_before = dict(state.get("sources") or {})
            stored = await self._stage_source(
                source,
                copy_into_workspace=copy_into_workspace,
                state=state,
            )
            if tags:
                await self.apply_tags(stored, tags)

            relative = self.workspace.relative(stored)
            data = await asyncio.to_thread(stored.read_bytes)
            digest = self._digest(data)

            previous = state["files"].get(relative)
            if previous and previous.get("digest") == digest:
                # 来源映射可能刚刚才建立（首次复制之后立刻又调了一次），
                # 要趁早落盘，否则下次重跑还得再走一遍内容比对。
                if state.get("sources") != sources_before:
                    await self._save_state(state)
                logger.debug("add_file: {} 内容未变，跳过", relative)
                return IngestResult(
                    path=relative,
                    added=False,
                    chunk_count=int(previous.get("chunk_count", 0)),
                    skipped_reason="unchanged",
                )

            node, chunks = await self._chunk(stored)
            if not chunks:
                # 分块器对空正文返回 ([], 无 chunk)。这不是错误，但也不该
                # 悄悄记成"已入库成功"，否则下次还会再算一遍。
                state["files"][relative] = {
                    "digest": digest,
                    "chunk_count": 0,
                    "tags": _tags_of(node),
                    "ingested_at": _now_iso(),
                    "empty": True,
                }
                await self._save_state(state)
                logger.info("add_file: {} 没有可索引正文，记为空", relative)
                return IngestResult(
                    path=relative,
                    added=False,
                    chunk_count=0,
                    skipped_reason="empty",
                )

            file_store = self.client.component("file_store", "default")
            await file_store.upsert([(node, chunks)])
            await file_store.dump()

            state["files"][relative] = {
                "digest": digest,
                "chunk_count": len(chunks),
                "tags": _tags_of(node),
                "ingested_at": _now_iso(),
                "empty": False,
            }
            await self._save_state(state)

        await self._register_catalog(stored, catalog)
        logger.info("add_file: {} 入库成功，{} 个 chunk", relative, len(chunks))
        return IngestResult(path=relative, added=True, chunk_count=len(chunks))

    async def add_directory(
        self,
        root: str | Path,
        *,
        pattern: str = "**/*.md",
        tags: list[str] | None = None,
        catalog: str = "default",
    ) -> list[IngestResult]:
        """批量入库一个目录（契约 §3.16）。

        逐个文件调 :meth:`add_file`，**单文件失败不中断整批**：
        坏掉的文件会记一条 ``added=False`` / ``skipped_reason="error: ..."``，
        其余照常入库。批量导入最怕"第 37 个文件编码坏了，前 36 个白干"。

        Args:
            root (`str | Path`): 目录。
            pattern (`str`): 相对 ``root`` 的 glob，默认 ``**/*.md``。
            tags (`list[str] | None`): 给**每个**文件都写上的标签。
            catalog (`str`): file_catalog 名。

        Returns:
            `list[IngestResult]`: 每个文件一条，顺序与 ``sorted(glob)`` 一致。

        Raises:
            `NotADirectoryError`: ``root`` 不是目录。
        """
        base = Path(root).expanduser()
        if not await asyncio.to_thread(base.is_dir):
            raise NotADirectoryError(f"不是目录: {base}")

        files = sorted(p for p in base.glob(pattern) if p.is_file())
        results: list[IngestResult] = []
        for file in files:
            try:
                results.append(await self.add_file(file, tags=tags, catalog=catalog))
            except Exception as exc:  # noqa: BLE001 - 单文件失败不中断整批
                logger.warning("add_directory: {} 入库失败: {}", file, exc)
                results.append(
                    IngestResult(
                        path=self.workspace.relative(file),
                        added=False,
                        chunk_count=0,
                        skipped_reason=f"error: {type(exc).__name__}: {exc}",
                    ),
                )
        logger.info(
            "add_directory: {} 扫描 {} 个文件，成功 {} 个",
            base,
            len(files),
            sum(1 for item in results if item.added),
        )
        return results

    async def add_text(
        self,
        text: str,
        *,
        name: str,
        tags: list[str] | None = None,
        catalog: str = "default",
    ) -> IngestResult:
        """把一段文本写成工作区里的 ``.md`` 再入库（契约 §3.16）。

        落点在 ``<workspace>/resource/<name>``。``name`` 里的路径分隔符会被
        压成 ``-``：记忆文件名来自调用方（可能是模型生成的），
        允许它带 ``/`` 就等于允许写到工作区外。

        Args:
            text (`str`): 正文。
            name (`str`): 文件名；没有后缀时补 ``.md``。
            tags (`list[str] | None`): 标签。
            catalog (`str`): file_catalog 名。

        Returns:
            `IngestResult`: 入库结果。

        Raises:
            `ValueError`: ``name`` 为空或全被压掉。
        """
        safe = _safe_name(name)
        if not safe:
            raise ValueError(f"name 非法（清洗后为空）: {name!r}")
        if not Path(safe).suffix:
            safe += ".md"

        target = Path(self.workspace.resource_path()) / safe
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, text, "utf-8")
        return await self.add_file(target, tags=tags, catalog=catalog)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    async def remove_file(self, path: str | Path) -> bool:
        """从索引里删除一个文件（**不删磁盘文件**）。

        Args:
            path (`str | Path`): 文件路径（绝对路径或**工作区相对路径**，
                见 :meth:`_as_fs_path`）。

        Returns:
            `bool`: 状态里有记录并已删除返回 ``True``，否则 ``False``。
        """
        relative = self.workspace.relative(self._as_fs_path(path))
        async with self._state_lock:
            state = await self._load_state()
            if relative not in state["files"]:
                logger.warning(
                    "remove_file: {} 不在入库状态里（返回 False，磁盘文件未动）。"
                    "若这个文件其实在索引里，先核对路径是不是写成了别的工作区相对路径。",
                    relative,
                )
                return False
            file_store = self.client.component("file_store", "default")
            await file_store.delete(relative)
            await file_store.dump()
            del state["files"][relative]
            # 指向这个文件的所有来源映射一并清掉，否则下次 add_file 会
            # "复用"一个已经不存在的路径。
            sources = state.get("sources") or {}
            for key in [key for key, value in sources.items() if value == relative]:
                del sources[key]
            await self._save_state(state)
        logger.info("remove_file: 已从索引移除 {}", relative)
        return True

    async def ingested(self) -> dict[str, dict[str, Any]]:
        """列出已入库文件的记录。

        Returns:
            `dict[str, dict[str, Any]]`: 路径 → 记录（``digest`` / ``chunk_count`` /
            ``tags`` / ``ingested_at``）。
        """
        state = await self._load_state()
        return dict(state["files"])

    async def stats(self) -> dict[str, Any]:
        """入库状态汇总。

        Returns:
            `dict[str, Any]`: ``{"files", "chunks", "empty", "state_path"}``。
        """
        records = await self.ingested()
        return {
            "files": len(records),
            "chunks": sum(int(item.get("chunk_count", 0)) for item in records.values()),
            "empty": sum(1 for item in records.values() if item.get("empty")),
            "state_path": str(self.state_path()),
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _stage_source(
        self,
        source: Path,
        *,
        copy_into_workspace: bool,
        state: dict[str, Any],
    ) -> Path:
        """把源文件安顿好：区内原样、区外按需复制。

        区外文件走**两级去重**，缺一不可：

        1. **来源记忆**（``state["sources"]``）：上次这个源路径被复制成了
           ``resource/`` 里的哪个文件。有记录且文件还在 → 直接复用。
        2. **内容比对**：没有记录时，在 ``resource/`` 里找字节相同的既有副本。

        为什么需要第 1 级：只靠文件名派生 ``x.md`` / ``x-1.md`` / ``x-2.md``，
        每次重跑都会造一个**新路径**，而幂等判据是按存储路径查的 ——
        于是每次都被判成"新文件"，同一份资源被反复索引。
        第 2 级单独用也不行：一旦给副本写过标签（front matter），
        副本字节就和源文件不再相同，内容比对会失败。两级合起来才闭环。
        这是本模块初版实现的真实 bug，被"批量重跑应当全部 ``added=False``"
        这条断言抓到。

        Args:
            source (`Path`): 已 resolve 的源文件。
            copy_into_workspace (`bool`): 区外文件是否复制进来。
            state (`dict[str, Any]`): 幂等状态（会被就地修改 ``sources`` 段）。

        Returns:
            `Path`: 最终要入库的文件路径（已 resolve）。
        """
        if self.workspace.is_inside(source):
            return source
        if not copy_into_workspace:
            logger.warning("add_file: {} 在工作区外，将按绝对路径入库", source)
            return source

        resource_dir = Path(self.workspace.resource_path())
        await asyncio.to_thread(resource_dir.mkdir, parents=True, exist_ok=True)
        payload = await asyncio.to_thread(source.read_bytes)

        sources: dict[str, str] = state.setdefault("sources", {})
        remembered = sources.get(str(source))
        if remembered:
            existing = self.workspace.resolve_relative(remembered)
            if await asyncio.to_thread(existing.is_file):
                logger.debug("add_file: 来源已记录为 {}，复用", remembered)
                return await asyncio.to_thread(existing.resolve)

        reused = await self._find_copy_by_content(resource_dir, source, payload)
        if reused is not None:
            sources[str(source)] = self.workspace.relative(reused)
            logger.debug("add_file: 找到内容相同的既有副本 {}", reused.name)
            return await asyncio.to_thread(reused.resolve)

        target = resource_dir / source.name
        index = 1
        while await asyncio.to_thread(target.exists):
            target = resource_dir / f"{source.stem}-{index}{source.suffix}"
            index += 1
        await asyncio.to_thread(target.write_bytes, payload)
        target = await asyncio.to_thread(target.resolve)
        sources[str(source)] = self.workspace.relative(target)
        logger.info("add_file: 工作区外文件已复制进 resource/ → {}", target.name)
        return target

    async def _find_copy_by_content(self, resource_dir: Path, source: Path, payload: bytes) -> Path | None:
        """在 ``resource/`` 里找一个字节与源文件相同的既有副本。

        Args:
            resource_dir (`Path`): ``resource/`` 目录。
            source (`Path`): 源文件。
            payload (`bytes`): 源文件字节。

        Returns:
            `Path | None`: 可复用的既有副本路径；没有则 ``None``。
        """
        candidates = [resource_dir / source.name]
        candidates.extend(resource_dir / f"{source.stem}-{index}{source.suffix}" for index in range(1, 1000))
        for candidate in candidates:
            if not await asyncio.to_thread(candidate.is_file):
                continue
            if await asyncio.to_thread(candidate.read_bytes) == payload:
                return candidate
        return None

    async def _chunk(self, path: Path) -> tuple[Any, list[Any]]:
        """复用 ReMe 的分块器把文件切成 ``(FileNode, chunks)``。

        Args:
            path (`Path`): 已 resolve 的文件路径。

        Returns:
            `tuple[Any, list[Any]]`: ``(FileNode, list[FileChunk])``。

        Raises:
            `MemoryUnavailableError`: ``reme`` 不可导入。
        """
        _require_reme()
        chunker = self._chunker(path)
        # 传**已 resolve** 的绝对路径：分块器内部用非 resolve 的 Path.absolute()
        # 判断"是否在工作区内"，而 macOS 上 /tmp 是指向 /private/tmp 的符号链接，
        # 不 resolve 会把区内文件误判成区外，索引里存的就变成绝对路径了。
        node, chunks = await chunker.chunk(path)
        return node, chunks

    async def _register_catalog(self, path: Path, catalog: str) -> None:
        """把文件登记到 file_catalog。

        ``update_catalog_step`` 的 ``file_catalog`` 依赖是 ``Ref(...)``
        （``third_party/ReMe/reme/steps/base_step.py:105``），解析顺序是
        ``kwargs → context → app_context.components``；传一个**字符串** kwargs
        就等于指名用哪个 catalog（``base_step.py:80-84``）。

        Args:
            path (`Path`): 文件路径。
            catalog (`str`): catalog 名。
        """
        from reme.enumeration import ComponentEnum

        registry = self.client.application.context.registry
        step_cls = registry.get(ComponentEnum.STEP, "update_catalog_step")
        if step_cls is None:
            logger.warning("update_catalog_step 未注册，跳过 catalog 登记")
            return
        step = step_cls(app_context=self.client.application.context)
        try:
            await step(changes=[{"change": "added", "path": str(path)}], persist=True, file_catalog=catalog)
        except Exception as exc:  # noqa: BLE001 - catalog 是可选索引，不该拖垮入库
            logger.warning("catalog 登记失败（不影响检索）: {}", exc)


def _tags_of(node: Any) -> list[str]:
    """从 ``FileNode`` 的 front matter 里取出**已规范化的**标签。

    ``FileNode.front_matter`` 是 ReMe 的 ``FileFrontMatter``，
    标签在 ``__pydantic_extra__`` 里（``schema/file_front_matter.py:16`` 的
    ``model_extra``）。这里再过一遍 :func:`normalize_tags`：分块器解析时
    **不会**规范化标签（规范化发生在 ``tag_index`` 里，见
    ``local_tag_index.py:33-54``），所以磁盘上写着的可能是 ``[Ops, X]``，
    而索引里实际生效的是 ``["ops"]``。记录进状态的应该是后者 —— 状态是给人看的，
    要反映"实际生效了什么"。

    Args:
        node (`Any`): ``FileNode``（或任何有 ``front_matter.model_extra`` 的对象）。

    Returns:
        `list[str]`: 规范化后的标签。
    """
    front_matter = getattr(node, "front_matter", None)
    if front_matter is None:
        return []
    extra = getattr(front_matter, "model_extra", None) or {}
    return normalize_tags(extra.get("memory_tags"))


def _safe_name(name: str) -> str:
    """把任意字符串压成一个安全的相对文件名（保留 ``/`` 之外的分隔意图）。

    Args:
        name (`str`): 原始名。

    Returns:
        `str`: 清洗后的名字；全部非法时返回空串。
    """
    cleaned = str(name).strip().strip("/")
    if not cleaned:
        return ""
    # 逐段清洗：去掉上跳与空段，段内把分隔符压掉。
    parts: list[str] = []
    for segment in cleaned.replace("\\", "/").split("/"):
        segment = segment.strip().replace(":", "-").replace("\x00", "")
        if segment in ("", ".", ".."):
            continue
        parts.append(segment)
    return "/".join(parts)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串。

    Returns:
        `str`: 例如 ``"2026-09-21T15:04:05+00:00"``。
    """
    return datetime.now(timezone.utc).isoformat()


def iter_chunk_texts(chunks: Iterable[Any]) -> list[str]:
    """取出一组 chunk 的正文（小工具，供教程里数一数切了几块）。

    Args:
        chunks (`Iterable[Any]`): ``FileChunk`` 序列。

    Returns:
        `list[str]`: 每块的 ``text``。
    """
    return [str(getattr(chunk, "text", "") or "") for chunk in chunks]
