# -*- coding: utf-8 -*-
"""JSONL 追加式事件溯源实现（契约 §3.9，第 9 讲）。

**物理布局**

::

    {session_dir}/
    ├── {session_id}.jsonl.zst              # 事件流，一条事件一行 JSON，一行一个 zstd frame
    ├── {session_id}.meta.json              # 索引卡片（派生数据，丢了可从事件流重建）
    ├── {session_id}.lock                   # 跨进程单写者检测用的 flock 文件
    └── snapshots/
        └── {session_id}.jsonl.zst          # 快照流（同样追加式，可保留历史多个快照）

**为什么一行一个 zstd frame**

合约要求"追加 + flush，崩溃后最多丢最后一行"。ReMe 的
``write_jsonl_zst``（``third_party/ReMe/reme/utils/jsonl_zst.py:23``）是
**"写临时文件 + os.replace" 的整文件重写**，不是追加，直接复用会让每次 append 变成
O(文件大小) 的全量重写。所以本模块复用的是它的**格式约定**（每行一段 JSON、
zstd 流式压缩），而追加逻辑自己写：每次 append 单独开一个 ``ab`` 句柄、单独压一个
frame、``flush + fsync``。zstd 的多 frame 拼接在解压侧是透明的
（``ZstdDecompressor().stream_reader(raw)`` 默认就能跨 frame 读出全部内容，
已实测：连续 3 次 append 后一次读出三行）。

**并发语义（写清楚，不含糊）**

- **同进程并发**：每个 ``session_id`` 一把 :class:`asyncio.Lock`，``seq`` 的
  "读最后一条 → 校验 → 写"整体串行，因此协程并发 append 是安全的；
- **跨进程并发**：事件流是单写者结构。本实现用 ``{session_id}.lock`` 上的
  ``flock(LOCK_EX | LOCK_NB)`` 检测冲突，冲突时抛
  :class:`~harness_kit.session.store.SessionLockedError`（**显式失败，不静默写坏**）；
- **崩溃恢复**：每次 append 都是一个独立的 zstd frame，最坏情况是最后一个
  frame 不完整 —— 读取时该行会被跳过（解压到一半报错即停），事件流本身不会错位。

**已知取舍**：:meth:`JsonlSessionStore._last_seq` 首次访问某个会话需要把整个文件
解压扫一遍（zstd 无法随机访问行）。扫完的结果缓存在内存里，后续 append 都是 O(1)。
冷启动成本换来的是"零索引依赖"——索引文件丢了也不影响正确性。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import zstandard as zstd
from loguru import logger

from harness_kit.events import EventRecord, EventKind, utc_now
from harness_kit.session.models import (
    SessionEvent,
    SessionMeta,
    SessionSnapshot,
    snapshot_from_state,
)
from harness_kit.session.store import (
    SessionLockedError,
    SessionStoreBase,
)

try:  # pragma: no cover - 平台分支，macOS / Linux 都有
    import fcntl
except ImportError:  # pragma: no cover - Windows 没有 fcntl
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_SESSION_DIR",
    "EVENT_SUFFIXES",
    "JsonlSessionStore",
]

DEFAULT_SESSION_DIR: Path = Path("./.harness/sessions")
"""默认会话目录（相对路径由调用方锚定，见 :meth:`~harness_kit.settings.Settings.resolve`）。"""

EVENT_SUFFIXES: tuple[str, ...] = (".jsonl.zst", ".jsonl")
"""事件流文件的两种后缀；压缩与不压缩共享同一套读路径。"""


def _iter_zstd_lines(path: Path, encoding: str = "utf-8") -> Iterator[str]:
    """按行读一个（可能由多 frame 拼接的）zstd JSONL 文件。

    刻意**不**用 ``read_jsonl_zst``（``third_party/ReMe/reme/utils/jsonl_zst.py:12``）：
    那个函数只读单个 frame（``stream_reader`` 在 PyPI ``zstandard`` 里的默认
    行为是读到第一个 frame 结束），而本模块的追加语义必然产生多 frame 文件。

    Args:
        path (`Path`): 目标文件。
        encoding (`str`): 文本编码。

    Yields:
        `str`: 每一行（含结尾换行符）。

    Note:
        尾部 frame 被截断（崩溃残留）时**不抛异常**，而是打一条 WARNING 后
        停止迭代 —— 调用方看到的就是"最多丢最后一行"，与本模块的崩溃语义一致。
        其余异常（磁盘 I/O 等）照常冒泡。
    """
    with path.open("rb") as raw:
        # ``read_across_frames=True`` 是关键：PyPI ``zstandard`` 的
        # ``stream_reader`` 默认只读到**第一个** frame 结束就收工，而本模块
        # 的追加语义必然写出多 frame 文件（每行一个 frame）。
        with zstd.ZstdDecompressor().stream_reader(
            raw,
            read_across_frames=True,
        ) as reader:
            text = io.TextIOWrapper(reader, encoding=encoding)
            try:
                yield from text
            except zstd.ZstdError as exc:
                # 崩溃时留下的半截 frame（"追加 + flush" 的最坏情况）。
                # 这里**吞掉并停止迭代**，而不是把异常抛给调用方：事件流是
                # 追加式的，尾部坏掉不影响前面已经写全的行；抛出去会让
                # "读取一个崩过一次的会话"直接报错，那是不可接受的降级。
                logger.bind(path=str(path), error=str(exc)).warning(
                    "事件文件尾部 frame 不完整（通常是崩溃时未写完），已跳过其后内容",
                )
            finally:
                try:
                    text.detach()  # 别让 TextIOWrapper 的析构去关 reader
                except ValueError:  # pragma: no cover - reader 已被提前关闭
                    pass


def _append_zstd_line(path: Path, line: str, *, fsync: bool = True) -> None:
    """把一行文本作为**独立的 zstd frame** 追加到文件尾。

    Args:
        path (`Path`): 目标文件。
        line (`str`): 一行文本（不含换行也可以，会补）。
        fsync (`bool`): 是否 ``os.fsync`` —— 关掉能快很多，但崩溃时可能丢更多行。
    """
    if not line.endswith("\n"):
        line = line + "\n"
    with path.open("ab") as raw:
        # ``closefd=False``：默认行为下 stream_writer 退出时会关掉底层 raw，
        # 于是紧随其后的 raw.flush()/os.fsync 会炸 "flush of closed file"。
        # 用 O_APPEND 打开的文件由我们自己在 with 退出时关闭。
        with zstd.ZstdCompressor(level=3).stream_writer(raw, closefd=False) as writer:
            text = io.TextIOWrapper(writer, encoding="utf-8")
            text.write(line)
            text.flush()
            text.detach()
        raw.flush()
        if fsync:
            os.fsync(raw.fileno())


def _append_plain_line(path: Path, line: str, *, fsync: bool = True) -> None:
    """非压缩模式下的追加：``O_APPEND`` + 单次 ``write`` + ``fsync``。

    单次 ``write`` 配合 ``O_APPEND`` 在 POSIX 上是原子的（不会与并发写者交叠），
    这是"崩溃后最多丢最后一行"的前提。

    Args:
        path (`Path`): 目标文件。
        line (`str`): 一行文本。
        fsync (`bool`): 是否 ``os.fsync``。
    """
    if not line.endswith("\n"):
        line = line + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


def _read_plain_lines(path: Path) -> Iterator[str]:
    """非压缩模式的按行读取。

    Args:
        path (`Path`): 目标文件。

    Yields:
        `str`: 每一行。
    """
    with path.open("r", encoding="utf-8") as handle:
        yield from handle


class JsonlSessionStore(SessionStoreBase):
    """按 ``session_id`` 分文件的追加式事件溯源存储。

    契约 §3.9 的签名：``__init__(self, session_dir: Path, *, compress: bool = True)``。

    Example:
        >>> store = JsonlSessionStore(Path("/tmp/harness/sessions"))
        >>> await store.append(EventRecord(session_id="s1", seq=0, kind=EventKind.SESSION_START))
        >>> [item.seq for item in await store.read("s1")]
        [0]
        >>> await store.aclose()
    """

    def __init__(self, session_dir: Path, *, compress: bool = True) -> None:
        """构造 JSONL 存储。

        Args:
            session_dir (`Path`): 会话目录；不存在时在首次写入时创建。
            compress (`bool`): 是否用 zstd 压缩（``True`` → ``*.jsonl.zst``）。
        """
        self.session_dir: Path = Path(session_dir)
        self.compress: bool = compress
        self.snapshots_dir: Path = self.session_dir / "snapshots"

        self._locks: dict[str, asyncio.Lock] = {}
        self._last_seq: dict[str, int] = {}
        self._closed: bool = False

        logger.bind(store="jsonl", dir=str(self.session_dir), compress=compress).debug(
            "JsonlSessionStore 已构造",
        )

    # ------------------------------------------------------------------
    # 路径工具
    # ------------------------------------------------------------------
    @property
    def suffix(self) -> str:
        """事件流文件后缀（``.jsonl.zst`` 或 ``.jsonl``）。

        Returns:
            `str`: 后缀。
        """
        return ".jsonl.zst" if self.compress else ".jsonl"

    def _event_path(self, session_id: str) -> Path:
        """事件流文件路径。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `Path`: ``{session_dir}/{session_id}.jsonl[.zst]``。
        """
        return self.session_dir / f"{session_id}{self.suffix}"

    def _snapshot_path(self, session_id: str) -> Path:
        """快照流文件路径。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `Path`: ``{session_dir}/snapshots/{session_id}.jsonl[.zst]``。
        """
        return self.snapshots_dir / f"{session_id}{self.suffix}"

    def _meta_path(self, session_id: str) -> Path:
        """索引卡片路径。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `Path`: ``{session_dir}/{session_id}.meta.json``。
        """
        return self.session_dir / f"{session_id}.meta.json"

    def _lock_path(self, session_id: str) -> Path:
        """写锁文件路径。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `Path`: ``{session_dir}/{session_id}.lock``。
        """
        return self.session_dir / f"{session_id}.lock"

    def session_ids(self) -> list[str]:
        """扫描目录，列出全部会话 id（事件文件存在即算一个会话）。

        Returns:
            `list[str]`: 去重后的会话 id。
        """
        if not self.session_dir.exists():
            return []
        found: set[str] = set()
        for suffix in EVENT_SUFFIXES:
            for path in self.session_dir.glob(f"*{suffix}"):
                found.add(path.name[: -len(suffix)])
        return sorted(found)

    def _lines(self, path: Path) -> Iterator[str]:
        """按压缩设置选择读行实现。

        Args:
            path (`Path`): 目标文件。

        Yields:
            `str`: 每一行。
        """
        if self.compress:
            yield from _iter_zstd_lines(path)
        else:
            yield from _read_plain_lines(path)

    def _append_line(self, path: Path, line: str, *, fsync: bool = True) -> None:
        """按压缩设置选择追加实现。

        Args:
            path (`Path`): 目标文件。
            line (`str`): 一行文本。
            fsync (`bool`): 是否落盘。
        """
        if self.compress:
            _append_zstd_line(path, line, fsync=fsync)
        else:
            _append_plain_line(path, line, fsync=fsync)

    # ------------------------------------------------------------------
    # 内部：锁与缓存
    # ------------------------------------------------------------------
    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """取（或建）该会话的进程内锁。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `asyncio.Lock`: 该会话专属的锁。
        """
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _cached_last_seq(self, session_id: str) -> int:
        """不碰磁盘地取缓存的 ``last_seq``。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 最后一个 ``seq``；无缓存时返回 ``-1``。
        """
        return self._last_seq.get(session_id, -1)

    def _scan_last_seq(self, session_id: str) -> int:
        """扫描事件文件，得到最后一个 ``seq``（无缓存时的冷路径）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 最后一个 ``seq``；文件不存在或为空时 ``-1``。
        """
        path = self._event_path(session_id)
        if not path.exists() or path.stat().st_size == 0:
            return -1
        last = -1
        try:
            for line in self._lines(path):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    # 崩溃时留下的半行 frame：按"最多丢最后一行"处理
                    logger.bind(session_id=session_id).warning(
                        "跳过事件文件里无法解析的一行（通常是崩溃时未写完的 frame）",
                    )
                    continue
                seq = payload.get("record", {}).get("seq")
                if isinstance(seq, int):
                    last = max(last, seq)
        except (ValueError, zstd.ZstdError) as exc:  # pragma: no cover - 截断文件
            logger.bind(session_id=session_id, error=str(exc)).warning(
                "事件文件尾部 frame 不完整，已跳过",
            )
        return last

    def _guarded_append(self, session_id: str, line: str) -> None:
        """跨进程单写者保护下的追加。

        ``flock(LOCK_EX | LOCK_NB)``：拿不到锁说明**另一个进程**正在写这个会话，
        直接抛 :class:`~harness_kit.session.store.SessionLockedError`。用非阻塞版本
        是刻意的 —— 阻塞会把"运维事故"变成"请求卡死"，更难排查。

        Args:
            session_id (`str`): 会话 id。
            line (`str`): 一行文本。

        Raises:
            SessionLockedError: 另一个进程正持有该会话的写锁。
        """
        if fcntl is None:  # pragma: no cover - Windows
            self._append_line(self._event_path(session_id), line)
            return

        lock_path = self._lock_path(session_id)
        with lock_path.open("ab") as lock_fd:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SessionLockedError(
                    f"另一个进程正在写会话 {session_id}（锁文件 {lock_path}）；"
                    "事件流是单写者结构，请勿并发追加同一会话",
                ) from exc
            try:
                self._append_line(self._event_path(session_id), line)
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # 事件：写
    # ------------------------------------------------------------------
    async def append(self, record: EventRecord) -> None:
        """追加一条事件（契约 §3.9）。

        整体流程在 :meth:`_lock_for` 的锁内完成：
        ``读 last_seq`` → ``校验 seq == last + 1`` → ``追加 + fsync`` → ``刷新索引卡片``。

        Args:
            record (`EventRecord`): 事件记录。

        Raises:
            SessionInvariantError: ``seq`` 跳号 / 重复 / 倒序。
            SessionLockedError: 另一个进程正在写同一个会话。
            RuntimeError: 存储已 ``aclose()``。
        """
        if self._closed:
            raise RuntimeError("JsonlSessionStore 已 aclose，不能再 append")

        session_id = record.session_id
        async with self._lock_for(session_id):
            last_seq = self._last_seq.get(session_id)
            if last_seq is None:
                last_seq = await asyncio.to_thread(self._scan_last_seq, session_id)
            self.check_appendable(record, last_seq)

            self.session_dir.mkdir(parents=True, exist_ok=True)
            line = SessionEvent.wrap(record).to_json_line()
            await asyncio.to_thread(self._guarded_append, session_id, line)
            self._last_seq[session_id] = record.seq
            await asyncio.to_thread(self._refresh_meta, record)

        logger.bind(
            session_id=session_id,
            seq=record.seq,
            kind=record.kind.value,
        ).trace("事件已落盘")

    async def append_many(self, records: Sequence[EventRecord]) -> None:  # type: ignore[override]
        """批量追加（按 ``session_id`` 分组后逐条写，语义与 :meth:`append` 一致）。

        Args:
            records (`Sequence[EventRecord]`): 事件序列（同会话内必须按 ``seq`` 升序）。

        Raises:
            SessionInvariantError: 任一条违反 ``seq`` 不变式。
        """
        for record in records:
            await self.append(record)

    # ------------------------------------------------------------------
    # 事件：读
    # ------------------------------------------------------------------
    async def read(
        self,
        session_id: str,
        *,
        since_seq: int = 0,
        limit: int | None = None,
    ) -> list[SessionEvent]:
        """读取 ``seq >= since_seq`` 的事件（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。
            since_seq (`int`): 起始 ``seq``（闭区间）。
            limit (`int | None`): 最多返回条数。

        Returns:
            `list[SessionEvent]`: 事件列表；会话不存在时为空列表。

        Raises:
            ValueError: ``limit`` 非正。
        """
        if limit is not None and limit <= 0:
            raise ValueError(f"limit 必须为正或 None，收到 {limit}")

        path = self._event_path(session_id)
        if not path.exists():
            return []

        def _read() -> list[SessionEvent]:
            collected: list[SessionEvent] = []
            for line in self._lines(path):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = SessionEvent.model_validate_json(stripped)
                except (json.JSONDecodeError, ValueError):
                    logger.bind(session_id=session_id).warning(
                        "跳过事件文件里无法解析的一行（崩溃残留或 schema 不兼容）",
                    )
                    continue
                if event.seq < since_seq:
                    continue
                collected.append(event)
                if limit is not None and len(collected) >= limit:
                    break
            return collected

        events = await asyncio.to_thread(_read)
        if self._last_seq.get(session_id) is None and events:
            self._last_seq[session_id] = max(item.seq for item in events)
        return events

    async def latest_seq(self, session_id: str) -> int:
        """该会话最后一个 ``seq``（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 最后一条事件的 ``seq``；空会话为 ``-1``。
        """
        cached = self._last_seq.get(session_id)
        if cached is not None:
            return cached
        if self._closed:
            # 关闭后不再碰缓存以外的世界：内存里有多少就说多少
            return cached if cached is not None else -1
        scanned = await asyncio.to_thread(self._scan_last_seq, session_id)
        self._last_seq[session_id] = scanned
        return scanned

    async def list_sessions(self) -> list[SessionMeta]:
        """列出全部会话卡片（契约 §3.9），按 ``updated_at`` 倒序。

        Returns:
            `list[SessionMeta]`: 卡片列表。
        """

        def _collect() -> list[SessionMeta]:
            metas: list[SessionMeta] = []
            for session_id in self.session_ids():
                meta = self._load_meta(session_id)
                if meta is not None:
                    metas.append(meta)
            metas.sort(key=lambda item: item.updated_at, reverse=True)
            return metas

        return await asyncio.to_thread(_collect)

    async def meta(self, session_id: str) -> SessionMeta | None:
        """取单个会话卡片（优先读 sidecar，缺失时从事件流重建）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMeta | None`: 卡片；会话不存在时 ``None``。
        """
        return await asyncio.to_thread(self._load_meta, session_id)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    async def save_snapshot(self, snapshot: SessionSnapshot) -> None:
        """落盘一个快照（追加式，旧快照保留）。

        Args:
            snapshot (`SessionSnapshot`): 快照。

        Raises:
            SessionInvariantError: 锚点 ``seq`` 不在事件流上（契约 §5.2 不变式 3）。
        """
        if self._closed:
            raise RuntimeError("JsonlSessionStore 已 aclose，不能再 save_snapshot")

        session_id = snapshot.session_id
        if snapshot.seq >= 0:
            anchor = await self.read(session_id, since_seq=snapshot.seq, limit=1)
            self.check_snapshot_anchor(snapshot, anchor)

        def _write() -> None:
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            self._append_line(
                self._snapshot_path(session_id),
                snapshot.model_dump_json(),
            )

        await asyncio.to_thread(_write)
        logger.bind(session_id=session_id, seq=snapshot.seq).debug("快照已落盘")

    async def load_snapshot(
        self,
        session_id: str,
        *,
        at_or_before: int | None = None,
    ) -> SessionSnapshot | None:
        """读取 ``seq <= at_or_before`` 的最新快照。

        Args:
            session_id (`str`): 会话 id。
            at_or_before (`int | None`): 上界（含）；``None`` 表示不限。

        Returns:
            `SessionSnapshot | None`: 快照或 ``None``。
        """
        path = self._snapshot_path(session_id)
        if not path.exists():
            return None

        def _read() -> SessionSnapshot | None:
            best: SessionSnapshot | None = None
            for line in self._lines(path):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    snapshot = SessionSnapshot.model_validate_json(stripped)
                except (json.JSONDecodeError, ValueError):
                    logger.bind(session_id=session_id).warning("跳过无法解析的快照行")
                    continue
                if at_or_before is not None and snapshot.seq > at_or_before:
                    continue
                if best is None or snapshot.seq >= best.seq:
                    best = snapshot
            return best

        return await asyncio.to_thread(_read)

    async def list_snapshots(self, session_id: str) -> list[SessionSnapshot]:
        """列出该会话全部快照，按 ``seq`` 升序。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `list[SessionSnapshot]`: 快照列表。
        """
        path = self._snapshot_path(session_id)
        if not path.exists():
            return []

        def _read() -> list[SessionSnapshot]:
            found: list[SessionSnapshot] = []
            for line in self._lines(path):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    found.append(SessionSnapshot.model_validate_json(stripped))
                except (json.JSONDecodeError, ValueError):
                    continue
            found.sort(key=lambda item: item.seq)
            return found

        return await asyncio.to_thread(_read)

    async def snapshot_of_state(
        self,
        agent_state: Any,
        *,
        session_id: str | None = None,
    ) -> SessionSnapshot:
        """便捷方法：把 ``AgentState`` 包成快照并落盘（锚点取当前 ``latest_seq``）。

        Args:
            agent_state (`Any`): AgentScope ``AgentState``。
            session_id (`str | None`): 覆盖会话 id（默认取 ``agent_state.session_id``）。

        Returns:
            `SessionSnapshot`: 已落盘的快照。
        """
        sid = session_id or agent_state.session_id
        seq = await self.latest_seq(sid)
        snapshot = snapshot_from_state(agent_state, seq=seq)
        await self.save_snapshot(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        """关闭存储：清空缓存与锁（契约 §3.9）。

        Returns:
            `None`
        """
        self._last_seq.clear()
        self._locks.clear()
        self._closed = True
        logger.bind(store="jsonl").debug("JsonlSessionStore 已关闭")

    # ------------------------------------------------------------------
    # 索引卡片（派生数据）
    # ------------------------------------------------------------------
    def _load_meta(self, session_id: str) -> SessionMeta | None:
        """读索引卡片；sidecar 缺失或损坏时从事件流重建并回写。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMeta | None`: 卡片；会话不存在时 ``None``。
        """
        if not self._event_path(session_id).exists():
            return None

        meta_path = self._meta_path(session_id)
        if meta_path.exists():
            try:
                return SessionMeta.model_validate_json(meta_path.read_text("utf-8"))
            except (json.JSONDecodeError, ValueError):
                logger.bind(session_id=session_id).warning(
                    "索引卡片损坏，从事件流重建（卡片是派生数据，不影响事件正确性）",
                )

        rebuilt = self._rebuild_meta(session_id)
        if rebuilt is not None:
            self._write_meta(rebuilt)
        return rebuilt

    def _rebuild_meta(self, session_id: str) -> SessionMeta | None:
        """扫描事件流重建索引卡片。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMeta | None`: 卡片；事件文件为空时 ``None``。
        """
        profile_name = "unknown"
        created_at = None
        updated_at = None
        count = 0
        tags: list[str] = []

        for line in self._lines(self._event_path(session_id)):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = SessionEvent.model_validate_json(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            count += 1
            if created_at is None:
                created_at = event.record.ts
            updated_at = event.record.ts
            if event.record.kind is EventKind.SESSION_START:
                payload = event.record.payload
                profile_name = str(payload.get("profile") or profile_name)
                raw_tags = payload.get("tags")
                if isinstance(raw_tags, list):
                    tags = [str(item) for item in raw_tags]

        if created_at is None or updated_at is None:
            return None

        return SessionMeta(
            session_id=session_id,
            profile_name=profile_name,
            created_at=created_at,
            updated_at=updated_at,
            event_count=count,
            tags=tags,
        )

    def _write_meta(self, meta: SessionMeta) -> None:
        """原子写索引卡片（先写临时文件再 ``os.replace``）。

        Args:
            meta (`SessionMeta`): 卡片。
        """
        self.session_dir.mkdir(parents=True, exist_ok=True)
        meta_path = self._meta_path(meta.session_id)
        tmp = meta_path.with_name(f".{meta_path.name}.tmp")
        tmp.write_text(meta.model_dump_json(), encoding="utf-8")
        os.replace(tmp, meta_path)

    def _refresh_meta(self, record: EventRecord) -> None:
        """``append`` 之后更新索引卡片（O(1)，不扫描事件流）。

        Args:
            record (`EventRecord`): 刚追加的事件。
        """
        session_id = record.session_id
        existing = self._load_meta_without_rebuild(session_id)

        if existing is None:
            meta = SessionMeta(
                session_id=session_id,
                profile_name=(
                    str(record.payload.get("profile") or "unknown")
                    if record.kind is EventKind.SESSION_START
                    else "unknown"
                ),
                created_at=record.ts,
                updated_at=record.ts,
                event_count=1,
                tags=(
                    [str(item) for item in record.payload.get("tags", [])]
                    if record.kind is EventKind.SESSION_START
                    and isinstance(record.payload.get("tags"), list)
                    else []
                ),
            )
        else:
            meta = existing.touched(event_count=existing.event_count + 1)

        self._write_meta(meta)

    def _load_meta_without_rebuild(self, session_id: str) -> SessionMeta | None:
        """只读 sidecar，不做重建（``_refresh_meta`` 内部用，避免 O(n²)）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMeta | None`: 卡片或 ``None``。
        """
        meta_path = self._meta_path(session_id)
        if not meta_path.exists():
            return None
        try:
            return SessionMeta.model_validate_json(meta_path.read_text("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return None

    def describe(self) -> dict[str, Any]:
        """返回存储的当前状态摘要（诊断 / 测试用）。

        Returns:
            `dict[str, Any]`: 目录、压缩开关、已跟踪的会话数与缓存。
        """
        return {
            "session_dir": str(self.session_dir),
            "compress": self.compress,
            "sessions_in_memory": sorted(self._last_seq),
            "cached_last_seq": dict(self._last_seq),
            "closed": self._closed,
            "now": utc_now().isoformat(),
        }
