# -*- coding: utf-8 -*-
"""计划的持久化与断点续跑（契约 §3.12，第 12 讲）。

**它和 :mod:`harness_kit.session` 的分工**：会话事件溯源（第 9 讲）回答的是
「这个 Agent 聊了什么、上下文怎么重建」；本模块回答的是**「这张任务 DAG 跑到
哪一步了」**。二者是两个不同的持久化边界：

============================ ==================================================
维度                          持久化载体
============================ ==================================================
``AgentState``（对话上下文）  ``SessionSnapshot``（第 9 讲）
``TaskGraph``（任务 DAG）     ``{plan_dir}/{plan_id}.json``（本模块）
「DAG 在什么时候变成了什么样」 ``EventRecord(kind=CUSTOM)``（本模块写入会话日志）
============================ ==================================================

第三行是关键：**DAG 的每次快照都会往会话事件日志里追加一条 ``CUSTOM`` 记录**。
这样做的收益是「一个会话的时间线里既有对话也有计划变更，且共用同一个 ``seq``
单调递增的不可变日志」—— 排查线上事故时不需要在三个存储之间对时间戳。
代价是计划快照会占日志体积，所以 payload 里只放 ``plan_id`` / ``progress`` /
``plan_digest``，**不放整张图**；整张图在 ``{plan_id}.json`` 里，靠
``plan_digest`` 关联。

**为什么 ``resume`` 要把 ``running`` 打回 ``pending``**：``running`` 是**进程内
的瞬时状态**，它落盘只意味着「当时正在跑」。进程崩了或被 kill 之后，那个
``running`` 节点没有任何人在跑它 —— 若照原样恢复，调度器会认为「它还在飞」，
于是这一格永远等不到结果，整张图静默卡死。因此恢复时把 ``running`` 归一成
``pending``（重新跑一遍），并且**把 ``attempts`` 保留**，让上层能用
「尝试次数过多」做熔断。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.events.types import EventKind, EventRecord, utc_now
from harness_kit.planning.graph import TaskGraph, TaskStatus

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与第 9 讲硬耦合
    from harness_kit.session.store import SessionStoreBase

__all__ = [
    "DEFAULT_PLAN_DIRNAME",
    "PlanNotFoundError",
    "PlanRecord",
    "PlanStore",
    "plan_digest",
]

DEFAULT_PLAN_DIRNAME: str = "plans"
"""``session_dir`` 下存放计划的子目录名。"""

_PLAN_EVENT_NAME: str = "plan_snapshot"
"""计划快照写进会话日志时用的 ``CUSTOM`` 事件名。"""


class PlanNotFoundError(KeyError):
    """计划不存在。"""


def plan_digest(graph: TaskGraph) -> str:
    """给一张图算一个稳定的内容摘要。

    摘要里只含**结构与状态**（id / goal / status / depends_on / attempts），
    不含时间戳，因此「同一张图重新 dump 一次」摘要不变 —— 这是
    「事件日志里的 digest 能用来判定计划有没有真的变」的前提。

    Args:
        graph (`TaskGraph`): 任务图。

    Returns:
        `str`: sha256 前 16 位十六进制。
    """
    payload = [
        {
            "id": node.id,
            "goal": node.goal,
            "status": node.status.value,
            "depends_on": sorted(node.depends_on),
            "attempts": node.attempts,
        }
        for node in sorted(graph.nodes.values(), key=lambda _: _.id)
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class PlanRecord(BaseModel):
    """落盘形态：图 + 一点元数据。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    """计划 id。"""

    v: int = 1
    """schema 版本，便于将来迁移。"""

    graph: TaskGraph
    """任务图。"""

    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
    """最近一次保存时间（UTC ISO）。"""

    revisions: int = Field(default=1, ge=1)
    """这张图被保存过几次。断点续跑时用它判断「是不是只写了一次就崩了」。"""

    digest: str = ""
    """:func:`plan_digest` 的结果。"""

    def touch(self) -> "PlanRecord":
        """把 ``revisions`` 加一、刷新时间与摘要。

        Returns:
            `PlanRecord`: ``self``。
        """
        self.revisions += 1
        self.updated_at = utc_now().isoformat()
        self.digest = plan_digest(self.graph)
        return self


class PlanStore:
    """任务图的持久化 + 与会话事件日志的对接。

    Args:
        path (`Path`): 计划目录。契约 §3.12 约定为
            ``{session_dir}/plans``，因此 ``{plan_id}.json`` 落在它下面。
        store (`SessionStoreBase | None`, optional): 会话事件存储（第 9 讲）。
            给了它就**同时**把每次快照追加成一条 ``CUSTOM`` 事件；不给就退化成
            纯文件持久化。**鸭子类型**：只要求对象有 ``append(record)`` 与
            ``next_seq(session_id)`` 两个协程方法，不 import 第 9 讲的类型，
            免得「第 12 讲依赖第 9 讲的具体实现」。
        session_id (`str | None`, optional): 事件写进哪个会话。给了 ``store``
            就必须给 ``session_id``。

    Raises:
        ValueError: 给了 ``store`` 但没给 ``session_id``。
    """

    def __init__(
        self,
        path: Path,
        *,
        store: "SessionStoreBase | None" = None,
        session_id: str | None = None,
    ) -> None:
        """初始化。"""
        if store is not None and not session_id:
            raise ValueError(
                "给了 SessionStore 就必须给 session_id：EventRecord 的 seq 是"
                "会话内单调递增的，没有会话 id 就无法分配 seq。",
            )
        self.path = Path(path)
        self.store = store
        self.session_id = session_id

    # ==================================================================
    # 文件路径
    # ==================================================================
    def ensure_dir(self) -> Path:
        """幂等创建计划目录。

        Returns:
            `Path`: 计划目录。
        """
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def plan_path(self, plan_id: str) -> Path:
        """``plan_id`` → 文件路径。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `Path`: ``{path}/{plan_id}.json``。

        Raises:
            ValueError: ``plan_id`` 含路径分隔符（防目录穿越）。
        """
        if "/" in plan_id or "\\" in plan_id or plan_id in (".", ".."):
            raise ValueError(
                f"plan_id 不能包含路径分隔符：{plan_id!r}",
            )
        return self.path / f"{plan_id}.json"

    # ==================================================================
    # 读写
    # ==================================================================
    async def save(self, plan_id: str, graph: TaskGraph) -> Path:
        """保存（或覆盖）一张图，并追加一条事件。

        采用 **写临时文件 + ``os.replace``** 的原子替换：断点续跑最怕的就是
        「写一半进程死了」，留下一个半个 JSON 的文件，下次恢复直接
        ``ValidationError``。原子替换保证读到的要么是旧版本、要么是新版本。

        Args:
            plan_id (`str`): 计划 id。
            graph (`TaskGraph`): 任务图。

        Returns:
            `Path`: 写入的文件路径。
        """
        self.ensure_dir()
        target = self.plan_path(plan_id)
        existing = self._read_record(target)
        record = PlanRecord(plan_id=plan_id, graph=graph)
        record.digest = plan_digest(graph)
        if existing is not None:
            record.revisions = existing.revisions + 1

        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )
        tmp.replace(target)
        logger.debug(
            "PlanStore.save: {} (revisions={}, digest={})",
            plan_id,
            record.revisions,
            record.digest,
        )
        await self.record_snapshot(plan_id, graph, digest=record.digest)
        return target

    async def load(self, plan_id: str) -> TaskGraph:
        """读回一张图。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `TaskGraph`: 恢复出来的图。

        Raises:
            PlanNotFoundError: 文件不存在，或内容不是合法的 ``PlanRecord``。
        """
        record = self._read_record(self.plan_path(plan_id))
        if record is None:
            raise PlanNotFoundError(
                f"计划不存在：{plan_id!r}（目录 {self.path}）。"
                f"现有计划 {await self.list_plans()}。",
            )
        record.graph.validate()
        return record.graph

    async def load_record(self, plan_id: str) -> PlanRecord:
        """读回完整记录（含 ``revisions`` / ``digest`` / ``updated_at``）。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `PlanRecord`: 落盘记录。

        Raises:
            PlanNotFoundError: 计划不存在。
        """
        record = self._read_record(self.plan_path(plan_id))
        if record is None:
            raise PlanNotFoundError(f"计划不存在：{plan_id!r}。")
        return record

    async def list_plans(self) -> list[str]:
        """列出所有计划 id（按文件名排序，确定性）。

        Returns:
            `list[str]`: 计划 id 列表。
        """
        if not self.path.exists():
            return []
        return sorted(_.stem for _ in self.path.glob("*.json"))

    async def latest_plan_id(self) -> str | None:
        """返回最近更新的计划 id（按 ``updated_at``）。

        Returns:
            `str | None`: 计划 id；目录为空时 ``None``。
        """
        best: tuple[str, str] | None = None
        for plan_id in await self.list_plans():
            record = self._read_record(self.plan_path(plan_id))
            if record is None:
                continue
            key = (record.updated_at, plan_id)
            if best is None or key > best:
                best = key
        return best[1] if best else None

    async def delete(self, plan_id: str) -> bool:
        """删除一个计划。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `bool`: 真删掉了为 ``True``，本来就没有为 ``False``。
        """
        target = self.plan_path(plan_id)
        if not target.exists():
            return False
        target.unlink()
        logger.info("PlanStore.delete: {}", plan_id)
        return True

    # ==================================================================
    # 断点续跑
    # ==================================================================
    async def resume(self, plan_id: str) -> TaskGraph:
        """从落盘的图里恢复出一张**可以继续调度**的图。

        归一化规则：

        1. ``running`` → ``pending``（进程内瞬时状态，见模块 docstring）；
           ``attempts`` 保留，供上层熔断；
        2. ``blocked`` 重新按依赖算一遍（``TaskGraph._refresh_blocked``）——
           如果上次卡在 ``blocked``，但事后有人手工把上游改回了 ``done``，
           这里必须能自愈；
        3. 终态（``done`` / ``failed`` / ``skipped``）**原样保留**：已经做成的
           活绝不重跑。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `TaskGraph`: 归一化后的图（**与落盘的那张不是同一个对象**）。

        Raises:
            PlanNotFoundError: 计划不存在。
        """
        record = await self.load_record(plan_id)
        graph = record.graph
        revived: list[str] = []
        for node in graph.nodes.values():
            if node.status is TaskStatus.RUNNING:
                node.status = TaskStatus.PENDING
                revived.append(node.id)
        graph.refresh_blocked()
        if revived:
            logger.warning(
                "PlanStore.resume({}): {} 个 running 节点被打回 pending（进程崩溃"
                "留下的瞬时状态）：{}",
                plan_id,
                len(revived),
                revived,
            )
        await self.record_snapshot(
            plan_id,
            graph,
            digest=plan_digest(graph),
            extra={"resumed": True, "revived": revived},
        )
        return graph

    async def save_and_resume(self, plan_id: str) -> TaskGraph:
        """``resume`` 之后立刻把归一化结果写回磁盘。

        用途：恢复动作本身也是一次状态变更，必须落盘 —— 否则「崩了两次」
        会得到两次一样的 ``running`` 节点，日志上看不出恢复过。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `TaskGraph`: 归一化并已落盘的图。
        """
        graph = await self.resume(plan_id)
        await self.save(plan_id, graph)
        return graph

    # ==================================================================
    # 事件溯源对接
    # ==================================================================
    async def record_snapshot(
        self,
        plan_id: str,
        graph: TaskGraph,
        *,
        digest: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> EventRecord | None:
        """把一次计划快照追加成会话日志里的 ``CUSTOM`` 事件。

        payload **不含整张图**（会撑爆日志），只有
        ``plan_id`` / ``digest`` / ``progress`` / ``nodes`` 四样，够回答
        「这次保存时计划整体是什么状态」。整张图在 JSON 文件里，靠 ``digest``
        关联。

        Args:
            plan_id (`str`): 计划 id。
            graph (`TaskGraph`): 任务图。
            digest (`str | None`, optional): 预先算好的摘要；``None`` 时现算。
            extra (`dict[str, Any] | None`, optional): 额外塞进 ``data`` 的键
                （例如 ``{"resumed": True}``）。

        Returns:
            `EventRecord | None`: 写进去的事件；没配 ``store`` 时 ``None``。
        """
        if self.store is None or self.session_id is None:
            return None
        data: dict[str, Any] = {
            "plan_id": plan_id,
            "digest": digest or plan_digest(graph),
            "progress": graph.progress(),
            "nodes": sorted(graph.nodes),
        }
        if extra:
            data.update(extra)
        record = EventRecord(
            session_id=self.session_id,
            seq=await self.store.next_seq(self.session_id),
            kind=EventKind.CUSTOM,
            ts=utc_now(),
            payload={"name": _PLAN_EVENT_NAME, "data": data},
            source="harness_kit.planning.resume",
        )
        record.warn_if_incomplete()
        await self.store.append(record)
        return record

    async def history(self, plan_id: str) -> list[EventRecord]:
        """从会话日志里取出该计划的所有快照事件（按 ``seq`` 升序）。

        这是「事件溯源」在本模块的落点：**文件只留最新版，历史在日志里**。
        想知道「这张图是第几次保存时变成 3 个 done 的」，就要读这里。

        Args:
            plan_id (`str`): 计划 id。

        Returns:
            `list[EventRecord]`: 快照事件；没配 ``store`` 时是空列表。

        Raises:
            RuntimeError: 配了 ``store`` 但它没有 ``read`` 方法。
        """
        if self.store is None or self.session_id is None:
            return []
        reader = getattr(self.store, "read", None)
        if reader is None:
            raise RuntimeError(
                "SessionStore 没有 read(session_id, ...) 方法，无法取出计划历史。",
            )
        events = await reader(self.session_id)
        out: list[EventRecord] = []
        for item in events:
            record = getattr(item, "record", item)
            if (
                isinstance(record, EventRecord)
                and record.kind is EventKind.CUSTOM
                and record.payload.get("name") == _PLAN_EVENT_NAME
                and record.payload.get("data", {}).get("plan_id") == plan_id
            ):
                out.append(record)
        return out

    # ==================================================================
    # 内部
    # ==================================================================
    def _read_record(self, target: Path) -> PlanRecord | None:
        """读一个 ``PlanRecord``；文件不存在或损坏时返回 ``None``。

        **损坏返回 ``None`` 而不是抛异常**：崩溃留下的半个文件不该让
        ``save`` 也失败（否则再也修不回来）。

        Args:
            target (`Path`): 文件路径。

        Returns:
            `PlanRecord | None`: 记录；不存在 / 损坏时为 ``None``。
        """
        if not target.exists():
            return None
        try:
            return PlanRecord.model_validate_json(target.read_text("utf-8"))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("计划文件损坏，按不存在处理：{} ({})", target, exc)
            return None

    def describe(self) -> str:
        """一行描述，供 ``doctor`` 子命令打印。

        Returns:
            `str`: 目录、是否接了会话日志。
        """
        return (
            f"PlanStore(path={self.path}, "
            f"session_id={self.session_id!r}, "
            f"event_sourcing={'on' if self.store is not None else 'off'})"
        )
