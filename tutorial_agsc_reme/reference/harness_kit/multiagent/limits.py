# -*- coding: utf-8 -*-
"""并发与预算限制（契约 §3.13，第 13 讲）。

**这一层为什么必须有**：AgentScope 2.0.8 的 app 服务层里有子 Agent 的原语
（``app/_tool/_agent_create.py:145`` 的 ``AgentCreate``、``_team_create.py:30``
的 ``TeamCreate``），但**核心 SDK 层没有任何派生上限**
（``_recon/02_agentscope_agent_loop.md:1234``：「核心 ``Agent`` 本身没有
``spawn_subagent`` 之类的能力」）。只要有一个「Agent 可以创建 Agent」的工具，
模型就能写出自复制循环：A 派 B，B 派 C，C 又派 A —— 每一层都合法，合起来是
指数爆炸。token 账单是唯一的事后信号，而那太晚了。

三个互相独立的闸门，任何一个单独设都不够：

=================== ================================================================
``max_spawn``       整条流程一共允许派生多少次。防「宽度爆炸」：一次并行 32 个。
``max_depth``       派生的层数。防「深度爆炸」：A→B→C→D→…，每一层都合规但永远不停。
``max_concurrent``  同时活着的派生数。防「把上游 provider 的 QPS 打满」。
=================== ================================================================

**为什么 ``release()`` 是同步的**：它必须在 ``finally`` 里可靠执行，包括
``asyncio.CancelledError``（取消任务）这条路径 —— 而 ``finally`` 里的
``await`` 在协程被取消时可能立刻再次抛出。计数器加锁用 ``threading.Lock``，
临界区只有几次整数运算，不会阻塞事件循环。
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from loguru import logger
from pydantic import BaseModel, ConfigDict

__all__ = [
    "SpawnLimitExceeded",
    "SpawnLimiter",
    "SpawnLimiterStats",
    "SpawnTicket",
]


class SpawnLimitExceeded(RuntimeError):
    """触发了派生闸门。

    带 ``gate`` 字段标明是哪一道门：``"max_spawn"`` / ``"max_depth"`` /
    ``"max_concurrent"``。区分它们很重要：``max_concurrent`` 撞上通常是
    「稍后重试就行」，而 ``max_spawn`` 撞上意味着**这一轮的策略本身有问题**，
    重试没有意义。
    """

    def __init__(
        self,
        gate: str,
        message: str,
        *,
        limit: int = 0,
        current: int = 0,
    ) -> None:
        """构造错误。

        Args:
            gate (`str`): 闸门名。
            message (`str`): 人读说明。
            limit (`int`): 该闸门的限额。
            current (`int`): 触发时的实际值。
        """
        self.gate = gate
        self.limit = limit
        self.current = current
        super().__init__(message)


class SpawnLimiterStats(BaseModel):
    """派生统计快照，供日志与指标使用。"""

    model_config = ConfigDict(extra="forbid")

    max_spawn: int = 0
    """派生总数上限。"""
    max_depth: int = 0
    """派生深度上限。"""
    max_concurrent: int = 0
    """同时存活上限。"""
    active: int = 0
    """当前存活（已 acquire 未 release）数。"""
    spawned: int = 0
    """累计发出的票据数。"""
    rejected: int = 0
    """累计被拒绝次数。"""
    peak_active: int = 0
    """``active`` 的历史峰值 —— 判断「限额是不是设小了」就看它。"""
    deepest: int = 0
    """实际到达过的最大深度。"""


class SpawnTicket:
    """一张派生许可。

    **必须 ``release()``**：不释放会让 ``max_concurrent`` 永远占满，后续派生
    全被拒。正常用法是 ``async with limiter.slot(depth=d) as ticket:``，异常与
    取消路径都会自动释放。手动用时务必 ``try/finally``。

    ``release()`` 是幂等的：重复调用只算一次（``Ticket`` 释放两次会让
    ``active`` 变成负数，比泄漏更难查）。
    """

    def __init__(
        self,
        limiter: "SpawnLimiter",
        *,
        depth: int,
        ticket_id: int,
    ) -> None:
        """构造票据（只应由 :meth:`SpawnLimiter.try_acquire` 调用）。"""
        self._limiter = limiter
        self.depth = depth
        """本票据的深度。"""
        self.ticket_id = ticket_id
        """自增票据号，便于日志里追踪。"""
        self._released = False

    @property
    def released(self) -> bool:
        """是否已释放。

        Returns:
            `bool`: 已释放为 ``True``。
        """
        return self._released

    def release(self) -> None:
        """归还票据（幂等）。"""
        if self._released:
            return
        self._released = True
        self._limiter._on_release(self)  # pylint: disable=protected-access

    def __enter__(self) -> "SpawnTicket":
        """支持同步 ``with``。

        Returns:
            `SpawnTicket`: ``self``。
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """退出时释放。"""
        self.release()

    def __repr__(self) -> str:
        """调试表示。"""
        state = "released" if self._released else "active"
        return (
            f"SpawnTicket(id={self.ticket_id}, depth={self.depth}, {state})"
        )


class SpawnLimiter:
    """派生闸门（契约 §3.13）。

    Args:
        max_spawn (`int`, defaults to `8`): 整条流程允许派生的总次数。
        max_depth (`int`, defaults to `3`): 允许的最大深度。``depth`` 从 0 起算，
            因此 ``max_depth=3`` 允许 depth 为 0 / 1 / 2 的三层，``depth=3``
            的请求被拒。
        max_concurrent (`int`, defaults to `4`): 同时存活的票据数上限。

    Raises:
        ValueError: 任一限额非正。
    """

    def __init__(
        self,
        *,
        max_spawn: int = 8,
        max_depth: int = 3,
        max_concurrent: int = 4,
    ) -> None:
        """初始化三道闸门。"""
        if max_spawn <= 0 or max_depth <= 0 or max_concurrent <= 0:
            raise ValueError(
                "max_spawn / max_depth / max_concurrent 必须为正数，"
                f"收到 {(max_spawn, max_depth, max_concurrent)}。"
                "要「不限」请给一个大数，不要给 0 —— 0 会让所有派生静默失败。",
            )
        self.max_spawn = int(max_spawn)
        self.max_depth = int(max_depth)
        self.max_concurrent = int(max_concurrent)
        self._lock = threading.Lock()
        self._active = 0
        self._spawned = 0
        self._rejected = 0
        self._peak_active = 0
        self._deepest = 0
        self._next_id = 1

    # ==================================================================
    # 核心
    # ==================================================================
    def try_acquire(self, *, depth: int) -> SpawnTicket:
        """尝试拿一张票据，失败立刻抛。

        **fail-fast 而不是排队等待**是刻意的：任务书的约束是「防止子 Agent
        爆炸」，等待会把爆炸变成「卡住」—— 卡住比报错更难定位。需要等待语义
        用 :meth:`slot`（它同样 fail-fast，只是把释放写进 ``finally``）。

        Args:
            depth (`int`): 本次派生的深度，从 0 开始。被派生的 Agent 再派生时
                应传 ``depth + 1``。

        Returns:
            `SpawnTicket`: 已占用的票据。

        Raises:
            SpawnLimitExceeded: 撞上任一闸门。
        """
        with self._lock:
            if depth >= self.max_depth:
                self._rejected += 1
                raise SpawnLimitExceeded(
                    "max_depth",
                    f"派生深度 {depth} 已达上限 {self.max_depth - 1}"
                    f"（max_depth={self.max_depth}，depth 从 0 起算）。"
                    "继续派下去只会得到一条越来越长的、永远回不到根的链。",
                    limit=self.max_depth,
                    current=depth,
                )
            if self._spawned >= self.max_spawn:
                self._rejected += 1
                raise SpawnLimitExceeded(
                    "max_spawn",
                    f"派生总数已达上限 {self.max_spawn}。"
                    "这是「宽度爆炸」闸门：撞上它说明整体策略有问题，"
                    "重试没有意义，应该减少并行或改为串行。",
                    limit=self.max_spawn,
                    current=self._spawned,
                )
            if self._active >= self.max_concurrent:
                self._rejected += 1
                raise SpawnLimitExceeded(
                    "max_concurrent",
                    f"并发派生数已达上限 {self.max_concurrent}。"
                    "这是「打满上游 QPS」闸门：稍后重试或降低并行度即可。",
                    limit=self.max_concurrent,
                    current=self._active,
                )
            self._spawned += 1
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            self._deepest = max(self._deepest, depth)
            ticket = SpawnTicket(self, depth=depth, ticket_id=self._next_id)
            self._next_id += 1
        logger.debug(
            "SpawnLimiter: acquire {} (active={}/{}, spawned={}/{}, depth={})",
            ticket.ticket_id,
            self._active,
            self.max_concurrent,
            self._spawned,
            self.max_spawn,
            depth,
        )
        return ticket

    def _on_release(self, ticket: SpawnTicket) -> None:
        """票据归还（只应由 :meth:`SpawnTicket.release` 调用）。

        Args:
            ticket (`SpawnTicket`): 被归还的票据。
        """
        with self._lock:
            self._active -= 1
            if self._active < 0:
                # 不该发生；发生说明有票据被重复释放。钳回 0 并报警，
                # 免得后续所有派生都被一条负数永久放行。
                logger.error(
                    "SpawnLimiter: active 变成负数（票据 {} 重复释放？），已钳回 0。",
                    ticket.ticket_id,
                )
                self._active = 0
        logger.debug(
            "SpawnLimiter: release {} (active={})",
            ticket.ticket_id,
            self._active,
        )

    @asynccontextmanager
    async def slot(self, *, depth: int) -> AsyncIterator[SpawnTicket]:
        """``async with`` 形式的票据，退出时自动释放。

        Args:
            depth (`int`): 派生深度。

        Yields:
            `SpawnTicket`: 票据。

        Raises:
            SpawnLimitExceeded: 撞上任一闸门（在进入 ``with`` 体之前就抛）。
        """
        ticket = self.try_acquire(depth=depth)
        try:
            yield ticket
        finally:
            # 取消 / 异常路径都会走到这里；release 幂等，重复调用无害。
            ticket.release()

    @contextmanager
    def guard(self, *, depth: int) -> Iterator[SpawnTicket]:
        """同步版的票据守卫，给非 async 代码用（``with limiter.guard(depth=0) as t:``）。

        **必须有 ``@contextmanager``**：没有它时这个函数只是个生成器函数，
        ``with`` 会直接 ``TypeError: 'generator' object does not support the
        context manager protocol`` —— 这个坑真实发生过，测试用例里有一条专门
        守着它。

        Args:
            depth (`int`): 派生深度。

        Yields:
            `SpawnTicket`: 票据。
        """
        ticket = self.try_acquire(depth=depth)
        try:
            yield ticket
        finally:
            ticket.release()

    # ==================================================================
    # 观测
    # ==================================================================
    @property
    def active(self) -> int:
        """当前存活票据数。

        Returns:
            `int`: 已 acquire 未 release 的数量。
        """
        with self._lock:
            return self._active

    @property
    def spawned(self) -> int:
        """累计发出的票据数。

        Returns:
            `int`: 总数。
        """
        with self._lock:
            return self._spawned

    def remaining(self) -> int:
        """还剩多少派生配额。

        Returns:
            `int`: ``max_spawn - spawned``（不小于 0）。
        """
        with self._lock:
            return max(0, self.max_spawn - self._spawned)

    def snapshot(self) -> SpawnLimiterStats:
        """统计快照。

        Returns:
            `SpawnLimiterStats`: 当前统计。
        """
        with self._lock:
            return SpawnLimiterStats(
                max_spawn=self.max_spawn,
                max_depth=self.max_depth,
                max_concurrent=self.max_concurrent,
                active=self._active,
                spawned=self._spawned,
                rejected=self._rejected,
                peak_active=self._peak_active,
                deepest=self._deepest,
            )

    def reset(self) -> None:
        """清零计数器（**不**动已经发出去的票据）。

        用途：一轮评测 / 一次请求之间复位。已经 acquire 的票据仍能正常 release
        （``active`` 会被减到 0 并触发那条钳位日志），所以正常做法是**先等所有
        票据归还再 reset**。
        """
        with self._lock:
            self._active = 0
            self._spawned = 0
            self._rejected = 0
            self._peak_active = 0
            self._deepest = 0
        logger.info("SpawnLimiter.reset: 计数器已清零。")

    def describe(self) -> str:
        """一行人读统计。

        Returns:
            `str`: 形如 ``"spawn=3/8 active=1/4 depth=1/3 rejected=0"``。
        """
        s = self.snapshot()
        return (
            f"spawn={s.spawned}/{s.max_spawn} "
            f"active={s.active}/{s.max_concurrent} "
            f"depth={s.deepest}/{s.max_depth - 1} "
            f"rejected={s.rejected} peak_active={s.peak_active}"
        )
