# -*- coding: utf-8 -*-
"""SQLite 事件溯源实现（契约 §3.9，第 9 讲）。

**为什么在同一天里既写 JSONL 又写 SQLite**

JSONL 版（:mod:`harness_kit.session.jsonl_store`）是**零依赖、单文件、可 grep、
可 rsync** 的形态：适合"一个开发者一台机器"和"日志要能直接 cat 出来看"的场合。
它有两个业务上真实的短板：

1. **按会话之外的维度查询很贵**：想"列出所有跑过 grep 工具、且 token 超过 1M 的会话"，
   JSONL 只能把所有文件解压扫一遍；
2. **单个会话的所有事件在同一个文件里**：一个跑了 20 万轮的长会话，
   尾部追加仍然快（O(1)），但"只读第 100~120 条"要从头解压。

SQLite 版把"事件表"变成**真正可索引的行**：``PRIMARY KEY(session_id, seq)`` 让
"按会话 + 按 seq 区间"取数变成索引扫描，``sessions`` 表让"列出会话"不用碰事件正文。

**三张表的职责**

=========================================== ============================================================
``sessions``                                  会话索引卡片（对应 :class:`~harness_kit.session.models.SessionMeta`）
``events``                                    不可变事件行，``PRIMARY KEY(session_id, seq)``
``blobs``                                     大 payload 的字节外置（内容寻址：``sha256`` 为主键）
``snapshots``                                 状态快照，``PRIMARY KEY(session_id, seq)``
=========================================== ============================================================

**为什么要有 ``blobs`` 表（blob 外置）**

``events.payload`` 里偶尔会出现大块文本：一次 grep 的完整输出、一次模型返回的长代码块。
把它们和"这一行事件发生了什么"混在一张表里，会让每一次
``SELECT seq FROM events WHERE session_id=?``（本模块最热的一条查询）都被迫
跨页读大字段。做法与 AgentScope ``app/storage/_sql/_tables.py:90`` 的
``payload: Mapped[dict] = mapped_column(JSON)`` 思路一致 —— 只是它把大字段留在行里，
我们把它外置：

- 事件行只留 ``blob_sha``（64 字符的十六进制）与 ``size``；
- 真正的字节进 ``blobs``，``sha256`` 内容寻址 → **同一份内容写两次只占一份空间**
  （这正是 AgentScope 的 blob 层没有做、而事件溯源场景最需要的一件事：
  失败回放时同一段工具输出会被反复记录）。

阈值由构造参数 ``blob_threshold`` 决定，默认 32 KiB ——
小于一个典型模型上下文的 1%，大于 99% 的单条事件。

**并发与持久性（写清楚，不含糊）**

- **同进程**：所有读写都在一把 :class:`asyncio.Lock` 下串行 —— SQLite 的写本来就是
  串行的，伪装成并发只会把"库忙"变成随机失败；
- **跨进程**：交给 SQLite 自己的文件锁。遇到
  ``sqlite3.OperationalError: database is locked`` 时翻译成
  :class:`~harness_kit.session.store.SessionLockedError`，与 JSONL 实现口径一致
  （**显式失败，不静默写坏**）；
- **持久性**：``journal_mode=WAL`` + ``synchronous=FULL``。
  WAL 让读不阻塞写；FULL 让每次 commit 都 fsync —— 事件日志的语义是
  "append 成功即不可丢"，这里不能为了吞吐把它降成 NORMAL。

**已知取舍**：``sqlite3`` 是标准库，本模块**不引入任何新依赖**
（不用 ``aiosqlite``，用 ``asyncio.to_thread`` 把同步 API 挪出事件循环）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from harness_kit.events import EventRecord, EventKind, utc_now
from harness_kit.session.models import (
    SESSION_SCHEMA_VERSION,
    SessionEvent,
    SessionInvariantError,
    SessionMeta,
    SessionSnapshot,
)
from harness_kit.session.store import (
    SessionLockedError,
    SessionStoreBase,
)

__all__ = [
    "DEFAULT_BLOB_THRESHOLD",
    "SqliteSessionStore",
]

DEFAULT_BLOB_THRESHOLD: int = 32 * 1024
"""超过这个字节数的单条事件会被外置成 blob（契约 §3.9 的 ``blob_threshold`` 默认值）。"""

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    event_count  INTEGER NOT NULL DEFAULT 0,
    tags_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS events (
    session_id   TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    v            INTEGER NOT NULL,
    event_id     TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    payload_json TEXT,
    blob_sha     TEXT,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_events_kind ON events (kind);
CREATE INDEX IF NOT EXISTS ix_events_ts   ON events (ts);

CREATE TABLE IF NOT EXISTS blobs (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    content    BLOB    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    session_id  TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    state_json  TEXT,
    blob_sha    TEXT,
    PRIMARY KEY (session_id, seq)
);
"""
"""建表语句。幂等（全部 ``IF NOT EXISTS``），因此可以每次打开库时无脑执行一遍。"""


class SqliteSessionStore(SessionStoreBase):
    """SQLite 版不可变追加日志（契约 §3.9）。

    契约签名：``__init__(self, db_path: Path, *, blob_threshold: int = 32 * 1024)``。

    Example:
        >>> store = SqliteSessionStore(Path("/tmp/harness/session.db"))
        >>> await store.append(EventRecord(session_id="s1", seq=0, kind=EventKind.SESSION_START))
        >>> await store.latest_seq("s1")
        0
        >>> await store.aclose()
    """

    def __init__(self, db_path: Path, *, blob_threshold: int = DEFAULT_BLOB_THRESHOLD) -> None:
        """构造 SQLite 存储。

        Args:
            db_path (`Path`): 数据库文件；父目录不存在时在首次写入时创建。
                传 ``Path(":memory:")`` 会退化成进程内内存库（单测用），
                此时不建目录、不做 WAL。
            blob_threshold (`int`): 单条事件序列化后超过该字节数即外置成 blob。

        Raises:
            ValueError: ``blob_threshold`` 小于 1。
        """
        if blob_threshold < 1:
            raise ValueError(f"blob_threshold 必须 >= 1，收到 {blob_threshold}")

        self.db_path: Path = Path(db_path)
        self.blob_threshold: int = int(blob_threshold)
        self.is_memory: bool = str(self.db_path) == ":memory:"

        self._lock: asyncio.Lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed: bool = False

        self.blobs_written: int = 0
        """本进程内写入的 blob 个数（含重复内容 —— 重复内容不会真的多占空间）。"""

        logger.bind(store="sqlite", db=str(self.db_path)).debug("SqliteSessionStore 已构造")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """建立（或复用）连接并建表。

        Returns:
            `sqlite3.Connection`: 已建好 schema 的连接。

        Raises:
            RuntimeError: 存储已 ``aclose()``。
        """
        if self._closed:
            raise RuntimeError("SqliteSessionStore 已 aclose，不能再使用")
        if self._conn is not None:
            return self._conn

        if not self.is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        if not self.is_memory:
            # WAL：读不阻塞写；FULL：每次 commit 都 fsync（事件日志不能丢）。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    async def _run(self, fn: Any, *args: Any) -> Any:
        """在锁内把同步的 SQLite 调用挪到线程里执行。

        Args:
            fn (`Any`): 接收 ``(conn, *args)`` 的同步函数。
            *args (`Any`): 透传给 ``fn`` 的参数。

        Returns:
            `Any`: ``fn`` 的返回值。

        Raises:
            SessionLockedError: 另一个进程持着库锁（``database is locked``）。
        """

        def _call() -> Any:
            conn = self._connect()
            try:
                result = fn(conn)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise SessionLockedError(
                        f"SQLite 库被另一个进程占用（{self.db_path}）：{exc}；"
                        "事件流是单写者结构，请勿并发追加同一会话",
                    ) from exc
                raise
            return result

        async with self._lock:
            return await asyncio.to_thread(_call)

    # ------------------------------------------------------------------
    # 事件：写
    # ------------------------------------------------------------------
    async def append(self, record: EventRecord) -> None:
        """追加一条事件（契约 §3.9）。

        整个"读 last_seq → 校验 → 写行 → 外置 blob → 刷新卡片"在一个事务里完成，
        因此**不会**出现"事件写了、卡片没更新"的中间态。

        Args:
            record (`EventRecord`): 事件记录。

        Raises:
            SessionInvariantError: ``seq`` 跳号 / 重复 / 倒序。
            SessionLockedError: 另一个进程持着库锁。
        """
        session_id = record.session_id
        event = SessionEvent.wrap(record)
        raw = event.to_json_line()
        blob_sha: str | None = None
        payload_json: str | None = raw

        if len(raw.encode("utf-8")) > self.blob_threshold:
            blob_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            payload_json = None

        def _write(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS last FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            last_seq = int(row["last"])
            self.check_appendable(record, last_seq)

            if blob_sha is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO blobs (sha256, size, content, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        blob_sha,
                        len(raw.encode("utf-8")),
                        sqlite3.Binary(raw.encode("utf-8")),
                        utc_now().isoformat(),
                    ),
                )
                self.blobs_written += 1

            conn.execute(
                "INSERT INTO events (session_id, seq, v, event_id, kind, ts, source, "
                "payload_json, blob_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    record.seq,
                    SESSION_SCHEMA_VERSION,
                    record.event_id,
                    record.kind.value,
                    record.ts.isoformat(),
                    record.source,
                    payload_json,
                    blob_sha,
                ),
            )
            self._touch_meta(conn, record)
            conn.commit()

        await self._run(_write)
        logger.bind(session_id=session_id, seq=record.seq).trace("事件已落盘(SQLite)")

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

        sql = (
            "SELECT seq, payload_json, blob_sha FROM events "
            "WHERE session_id = ? AND seq >= ? ORDER BY seq ASC"
        )
        params: list[Any] = [session_id, int(since_seq)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        def _read(conn: sqlite3.Connection) -> list[SessionEvent]:
            return [self._row_to_event(conn, row) for row in conn.execute(sql, params)]

        return await self._run(_read)

    async def latest_seq(self, session_id: str) -> int:
        """该会话最后一个 ``seq``（契约 §3.9）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `int`: 最后一条事件的 ``seq``；空会话为 ``-1``。
        """

        def _read(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS last FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["last"])

        return await self._run(_read)

    async def list_sessions(self) -> list[SessionMeta]:
        """列出全部会话卡片（契约 §3.9），按 ``updated_at`` 倒序。

        Returns:
            `list[SessionMeta]`: 卡片列表。
        """

        def _read(conn: sqlite3.Connection) -> list[SessionMeta]:
            rows = conn.execute(
                "SELECT session_id, profile_name, created_at, updated_at, event_count, "
                "tags_json FROM sessions ORDER BY updated_at DESC",
            ).fetchall()
            return [self._row_to_meta(row) for row in rows]

        return await self._run(_read)

    async def meta(self, session_id: str) -> SessionMeta | None:
        """取单个会话卡片（O(1) 主键查询，不扫事件表）。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `SessionMeta | None`: 卡片；会话不存在时 ``None``。
        """

        def _read(conn: sqlite3.Connection) -> SessionMeta | None:
            row = conn.execute(
                "SELECT session_id, profile_name, created_at, updated_at, event_count, "
                "tags_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return None if row is None else self._row_to_meta(row)

        return await self._run(_read)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    async def save_snapshot(self, snapshot: SessionSnapshot) -> None:
        """落盘一个快照（``PRIMARY KEY(session_id, seq)``，同锚点覆盖）。

        Args:
            snapshot (`SessionSnapshot`): 快照。

        Raises:
            SessionInvariantError: 锚点 ``seq`` 不在事件流上（契约 §5.2 不变式 3）。
        """
        session_id = snapshot.session_id
        if snapshot.seq >= 0:
            anchor = await self.read(session_id, since_seq=snapshot.seq, limit=1)
            self.check_snapshot_anchor(snapshot, anchor)

        raw = snapshot.model_dump_json()
        blob_sha: str | None = None
        state_json: str | None = raw
        if len(raw.encode("utf-8")) > self.blob_threshold:
            blob_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            state_json = None

        def _write(conn: sqlite3.Connection) -> None:
            if blob_sha is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO blobs (sha256, size, content, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        blob_sha,
                        len(raw.encode("utf-8")),
                        sqlite3.Binary(raw.encode("utf-8")),
                        utc_now().isoformat(),
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (session_id, seq, created_at, state_json, "
                "blob_sha) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    snapshot.seq,
                    snapshot.created_at.isoformat(),
                    state_json,
                    blob_sha,
                ),
            )
            conn.commit()

        await self._run(_write)

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
        sql = (
            "SELECT seq, state_json, blob_sha FROM snapshots WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if at_or_before is not None:
            sql += " AND seq <= ?"
            params.append(int(at_or_before))
        sql += " ORDER BY seq DESC LIMIT 1"

        def _read(conn: sqlite3.Connection) -> SessionSnapshot | None:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return None
            raw = self._materialize(conn, row["state_json"], row["blob_sha"])
            return SessionSnapshot.model_validate_json(raw)

        return await self._run(_read)

    async def list_snapshots(self, session_id: str) -> list[SessionSnapshot]:
        """列出该会话全部快照，按 ``seq`` 升序。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `list[SessionSnapshot]`: 快照列表。
        """

        def _read(conn: sqlite3.Connection) -> list[SessionSnapshot]:
            rows = conn.execute(
                "SELECT seq, state_json, blob_sha FROM snapshots WHERE session_id = ? "
                "ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
            return [
                SessionSnapshot.model_validate_json(
                    self._materialize(conn, row["state_json"], row["blob_sha"]),
                )
                for row in rows
            ]

        return await self._run(_read)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    async def vacuum(self) -> dict[str, int]:
        """回收空间：删掉无人引用的 blob，然后 ``VACUUM``。

        事件日志"永不修改、永不删除"指的是 **events 表**；blob 表是它的
        派生缓存（内容寻址，可重建），因此**可以**清理孤儿。

        Returns:
            `dict[str, int]`: ``orphan_blobs`` / ``blobs_left`` / ``page_count`` /
            ``page_size`` 四个诊断值。
        """

        def _run(conn: sqlite3.Connection) -> dict[str, int]:
            deleted = conn.execute(
                "DELETE FROM blobs WHERE sha256 NOT IN ("
                "  SELECT blob_sha FROM events WHERE blob_sha IS NOT NULL "
                "  UNION SELECT blob_sha FROM snapshots WHERE blob_sha IS NOT NULL"
                ")",
            ).rowcount
            conn.commit()
            blobs_left = int(
                conn.execute("SELECT COUNT(*) AS c FROM blobs").fetchone()["c"],
            )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.commit()
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            return {
                "orphan_blobs": int(deleted),
                "blobs_left": blobs_left,
                "page_count": page_count,
                "page_size": page_size,
            }

        return await self._run(_run)

    async def aclose(self) -> None:
        """关闭连接（契约 §3.9）。幂等。

        Returns:
            `None`
        """

        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None
            self._closed = True
        logger.bind(store="sqlite").debug("SqliteSessionStore 已关闭")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _materialize(conn: sqlite3.Connection, inline: str | None, blob_sha: str | None) -> str:
        """把"行内 JSON 或 blob 引用"还原成 JSON 字符串。

        Args:
            conn (`sqlite3.Connection`): 连接。
            inline (`str | None`): 行内 JSON；外置时为 ``None``。
            blob_sha (`str | None`): blob 引用；未外置时为 ``None``。

        Returns:
            `str`: JSON 字符串。

        Raises:
            SessionInvariantError: blob 引用指向一个不存在的 blob（文件被外部改过）。
        """
        if blob_sha is None:
            if inline is None:  # pragma: no cover - 只可能由外部改库造成
                raise SessionInvariantError(
                    "事件行的 payload_json 与 blob_sha 同时为空，数据库被外部修改过",
                )
            return str(inline)
        row = conn.execute(
            "SELECT content FROM blobs WHERE sha256 = ?",
            (blob_sha,),
        ).fetchone()
        if row is None:
            raise SessionInvariantError(
                f"blob {blob_sha[:12]}… 被事件引用但不存在 —— "
                "blobs 表与 events 表不同源，数据库可能被手工改过",
            )
        return bytes(row["content"]).decode("utf-8")

    def _row_to_event(self, conn: sqlite3.Connection, row: sqlite3.Row) -> SessionEvent:
        """事件行 → :class:`~harness_kit.session.models.SessionEvent`。

        Args:
            conn (`sqlite3.Connection`): 连接。
            row (`sqlite3.Row`): ``events`` 表的一行。

        Returns:
            `SessionEvent`: 事件。
        """
        raw = self._materialize(conn, row["payload_json"], row["blob_sha"])
        return SessionEvent.model_validate_json(raw)

    @staticmethod
    def _row_to_meta(row: sqlite3.Row) -> SessionMeta:
        """``sessions`` 表行 → :class:`~harness_kit.session.models.SessionMeta`。

        Args:
            row (`sqlite3.Row`): ``sessions`` 表的一行。

        Returns:
            `SessionMeta`: 卡片。
        """
        return SessionMeta(
            session_id=str(row["session_id"]),
            profile_name=str(row["profile_name"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            event_count=int(row["event_count"]),
            tags=[str(item) for item in json.loads(row["tags_json"])],
        )

    @staticmethod
    def _touch_meta(conn: sqlite3.Connection, record: EventRecord) -> None:
        """``append`` 之后更新索引卡片（同一事务内）。

        Args:
            conn (`sqlite3.Connection`): 连接（调用方负责 commit）。
            record (`EventRecord`): 刚追加的事件。
        """
        session_id = record.session_id
        row = conn.execute(
            "SELECT profile_name, created_at, tags_json, event_count FROM sessions "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if row is None:
            profile_name = (
                str(record.payload.get("profile") or "unknown")
                if record.kind is EventKind.SESSION_START
                else "unknown"
            )
            raw_tags = record.payload.get("tags") if record.kind is EventKind.SESSION_START else None
            tags = [str(item) for item in raw_tags] if isinstance(raw_tags, list) else []
            conn.execute(
                "INSERT INTO sessions (session_id, profile_name, created_at, updated_at, "
                "event_count, tags_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    profile_name,
                    record.ts.isoformat(),
                    record.ts.isoformat(),
                    1,
                    json.dumps(tags, ensure_ascii=False),
                ),
            )
            return

        conn.execute(
            "UPDATE sessions SET updated_at = ?, event_count = ? WHERE session_id = ?",
            (utc_now().isoformat(), int(row["event_count"]) + 1, session_id),
        )

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """返回存储的当前状态摘要（诊断 / 测试用）。

        Returns:
            `dict[str, Any]`: 库路径、阈值、已写 blob 数、关闭标记。
        """
        return {
            "db_path": str(self.db_path),
            "blob_threshold": self.blob_threshold,
            "blobs_written": self.blobs_written,
            "closed": self._closed,
            "now": utc_now().isoformat(),
        }

    async def counts(self) -> dict[str, int]:
        """各表的行数（诊断用）。

        Returns:
            `dict[str, int]`: ``sessions`` / ``events`` / ``blobs`` / ``snapshots``。
        """

        def _read(conn: sqlite3.Connection) -> dict[str, int]:
            out: dict[str, int] = {}
            for table in ("sessions", "events", "blobs", "snapshots"):
                out[table] = int(
                    conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"],
                )
            return out

        return await self._run(_read)

    async def append_many(self, records: Sequence[EventRecord]) -> None:  # type: ignore[override]
        """批量追加（单个事务，比逐条 :meth:`append` 快得多）。

        Args:
            records (`Sequence[EventRecord]`): 事件序列（同会话内必须按 ``seq`` 升序）。

        Raises:
            SessionInvariantError: 任一条违反 ``seq`` 不变式（整个事务回滚）。
        """
        for record in records:
            await self.append(record)
