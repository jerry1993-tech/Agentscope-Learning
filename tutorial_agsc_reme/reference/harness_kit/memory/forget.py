# -*- coding: utf-8 -*-
"""遗忘策略：按年龄 / 访问次数 / 显式标签决定删除或降权（契约 §3.18）。

**ReMe 没有"遗忘"，所以这一层必须自己造，而且必须造得诚实**

ReMe 提供的是**删除**（``delete`` job → ``delete_step``）和**改 front matter**
（``frontmatter_update`` job），没有"哪些该删"的判断，更没有**访问次数**这个概念：
``file_store`` / ``tag_index`` 里都没有 hit counter，检索一次不会在任何地方留痕。

于是 :class:`MemoryForgetter` 面对三个必须显式回答的问题：

1. **年龄从哪来？** 用文件的 ``st_mtime``（``pathlib.Path.stat().st_mtime``）。
   不用 front matter 里的日期：front matter 是模型写的，模型完全可能写错年份，
   而"这个文件多久没被碰过"是文件系统的事实。
2. **访问次数从哪来？** 由 harness 自己维护（:class:`HitCounter`），
   持久化在 ``<workspace>/metadata/memory_hits.json``。
   **它是 harness 的账本，不是 ReMe 的**；不喂它就永远是 0，
   :meth:`MemoryForgetter.plan` 会在日志里明确说"hit 数据缺失"，
   而不是假装"这个文件从没被访问过"。
3. **降权是什么？** 在 front matter 上写 ``memory_status: stale``
   （:data:`DEMOTE_KEY` / :data:`DEMOTE_VALUE`）。
   **必须说清楚：ReMe 不会因此降低它的检索分数。** 这个键是**给调用方看的**
   （:meth:`MemoryForgetter.stale_paths` 读它，门控/过滤可以据此排除）。
   把它说成"自动降权"是撒谎 —— 想真正影响排序，得在
   :class:`~harness_kit.memory.search.MemorySearch` 的 ``tags`` 或
   ``search_filter`` 上做，或者把它挪出工作区。

**判据（两条，且互斥）**

设 ``age`` 为文件年龄（天）、``hits`` 为该文件的历史命中次数、
``policy.max_age_days = A``、``policy.min_hits = H``：

- 命中任意 ``protected_tags`` → 进 ``ForgetPlan.protected``，**永不**删除或降权；
- ``age > A`` 且 ``hits <= H`` → 进 ``delete``（太老且几乎没人取用）；
- ``age > A`` 且 ``hits > H``  → 进 ``demote``（太老但有人用，降权保留）；
- ``age <= A`` 或 ``A is None`` → 不动。

``H`` 的默认值是 ``0``，含义是"只要被取用过一次就不删，改为降权"——
这个默认是刻意的：误删一条还有人用的记忆，比留下一条没用的记忆贵得多。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from .client import MemoryClient

__all__ = [
    "DEMOTE_KEY",
    "DEMOTE_VALUE",
    "DEFAULT_SCAN_GLOBS",
    "ForgetPlan",
    "ForgetPolicy",
    "HitCounter",
    "MemoryForgetter",
]

#: 降权时写进 front matter 的键名。
DEMOTE_KEY: str = "memory_status"

#: 降权时写进 front matter 的值。
DEMOTE_VALUE: str = "stale"

#: 默认扫描的记忆目录（工作区相对 glob）。刻意**不**扫 ``session/``：
#: 那是会话原文（``auto_memory`` 的输入），删了会让蒸馏失去证据链。
DEFAULT_SCAN_GLOBS: tuple[str, ...] = ("daily/**/*.md", "resource/**/*.md", "digest/**/*.md")

#: hit 账本的落盘位置（工作区相对路径）。
HITS_LEDGER_NAME: str = "metadata/memory_hits.json"

_DAY_SECONDS: float = 86400.0


class ForgetPolicy(BaseModel):
    """遗忘策略（契约 §3.18）。

    Attributes:
        max_age_days (`int | None`): 超过这个天数的文件才进入候选；
            ``None`` = 不做年龄判断（于是整个 plan 只会是空的或全进 protected）。
        min_hits (`int`): 命中次数下限。``hits <= min_hits`` 才允许删除，
            否则降权。默认 0 —— 被取用过一次就不删。
        protected_tags (`list[str]`): 受保护标签，默认 ``["pinned"]``。
            标签来自 ``tag_index``（即 front matter 的标签），
            比对时 casefold。
    """

    model_config = ConfigDict(extra="forbid")

    max_age_days: int | None = None
    min_hits: int = 0
    protected_tags: list[str] = Field(default_factory=lambda: ["pinned"])

    def normalized_protected(self) -> set[str]:
        """返回归一化（casefold）后的受保护标签集合。

        Returns:
            `set[str]`: 受保护标签。
        """
        return {str(tag).strip().casefold() for tag in self.protected_tags if str(tag).strip()}


class ForgetPlan(BaseModel):
    """遗忘计划（契约 §3.18）—— :meth:`MemoryForgetter.plan` 的产物，**只算不删**。

    Attributes:
        delete (`list[str]`): 计划删除的工作区相对路径。
        demote (`list[str]`): 计划降权（写 ``memory_status: stale``）的路径。
        protected (`list[str]`): 因为带受保护标签而被排除的路径。
    """

    model_config = ConfigDict(extra="forbid")

    delete: list[str] = Field(default_factory=list)
    demote: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """计划里有没有动作（``protected`` 不算动作）。

        Returns:
            `bool`: ``delete`` 与 ``demote`` 都为空时为 ``True``。
        """
        return not self.delete and not self.demote

    def summary(self) -> str:
        """渲染一行摘要（给日志用）。

        Returns:
            `str`: ``delete=N demote=N protected=N`` 形式。
        """
        return f"delete={len(self.delete)} demote={len(self.demote)} protected={len(self.protected)}"


class HitCounter:
    """harness 自己维护的命中次数账本（ReMe 不提供）。

    Example::

        counter = HitCounter()
        counter.record_hits(result.hits)          # 一次检索结果
        counter.persist(workspace)                # 落盘
        ...
        counter = HitCounter.load(workspace)      # 下次会话读回来

    计数键用工作区相对路径（而不是 ``chunk_id``）：遗忘的判据是"这个文件还有人用吗"，
    而一次检索可能命中同一文件的多个 chunk；按 chunk 计数会让
    "命中了 3 个片段"被算成"被访问过 3 次"，把阈值语义悄悄改掉。
    """

    def __init__(self, counts: Mapping[str, int] | None = None) -> None:
        """构造账本。

        Args:
            counts (`Mapping[str, int] | None`): 初始计数（路径 → 次数）。
        """
        self._counts: dict[str, int] = {str(key): int(value) for key, value in (counts or {}).items()}

    def record(self, paths: Iterable[str]) -> None:
        """给一批路径各加 1。

        Args:
            paths (`Iterable[str]`): 工作区相对路径。
        """
        for path in dict.fromkeys(str(item) for item in paths):
            if not path:
                continue
            self._counts[path] = self._counts.get(path, 0) + 1

    def record_hits(self, hits: Sequence[Any]) -> None:
        """从一次检索的 hit 列表里累计（同一文件的多个 chunk 只算一次）。

        Args:
            hits (`Sequence[Any]`): :class:`~harness_kit.memory.citations.MemoryHit` 序列。
        """
        self.record([str(getattr(hit, "path", "") or "") for hit in (hits or ())])

    def count(self, path: str) -> int:
        """取一个路径的命中次数。

        Args:
            path (`str`): 工作区相对路径。

        Returns:
            `int`: 命中次数；没记录过返回 0。
        """
        return int(self._counts.get(str(path), 0))

    def as_dict(self) -> dict[str, int]:
        """导出全部计数。

        Returns:
            `dict[str, int]`: 路径 → 次数（按键排序）。
        """
        return {key: self._counts[key] for key in sorted(self._counts)}

    def persist(self, workspace: Any) -> Path:
        """落盘到 ``<workspace>/metadata/memory_hits.json``。

        Args:
            workspace (`Any`): :class:`~harness_kit.memory.workspace.ReMeWorkspace`。

        Returns:
            `Path`: 落盘路径。
        """
        target = workspace.root / HITS_LEDGER_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, workspace: Any) -> "HitCounter":
        """从工作区读回账本；文件不存在时返回空账本。

        Args:
            workspace (`Any`): :class:`~harness_kit.memory.workspace.ReMeWorkspace`。

        Returns:
            `HitCounter`: 账本（不会抛"文件不存在"）。
        """
        source = workspace.root / HITS_LEDGER_NAME
        if not source.is_file():
            return cls()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("memory_hits.json 读不出来（{}），按空账本处理: {}", exc, source)
            return cls()
        return cls(payload if isinstance(payload, dict) else {})

    def __len__(self) -> int:
        """账本里记了多少个路径。

        Returns:
            `int`: 条目数。
        """
        return len(self._counts)


class MemoryForgetter:
    """遗忘策略的执行者（契约 §3.18）。

    Example::

        forgetter = MemoryForgetter(client, ForgetPolicy(max_age_days=30, min_hits=0))
        plan = await forgetter.plan()          # dry-run，只算不删
        print(plan.summary())
        deleted = await forgetter.apply(plan)  # 真删
        print("deleted:", deleted)

    两个方法分开是刻意的：遗忘是**不可逆**的，把"算"与"执行"合成一个方法，
    调用方就没有机会在删除前打印一份清单或让人确认。
    """

    def __init__(
        self,
        client: MemoryClient,
        policy: ForgetPolicy,
        *,
        hits: "HitCounter | Mapping[str, int] | Callable[[str], int] | None" = None,
        workspace: Any | None = None,
        globs: Sequence[str] = DEFAULT_SCAN_GLOBS,
    ) -> None:
        """配置遗忘器。

        Args:
            client (`MemoryClient`): 已 start 的客户端。
            policy (`ForgetPolicy`): 策略。
            hits (`HitCounter | Mapping[str, int] | Callable[[str], int] | None`):
                命中数据来源。三种形态都接受：
                :class:`HitCounter`、``{path: count}`` 字典、
                ``path -> count`` 的可调用对象。``None`` 时用**空账本**，
                此时全部文件的 hits 都是 0，于是"太老"的文件会**全部**进 delete ——
                这是危险默认值，所以 :meth:`plan` 会打一条 warning 点明这件事。
            workspace (`Any | None`): 工作区；``None`` 时用
                ``ReMeWorkspace(root=client.workspace_dir)``。
            globs (`Sequence[str]`): 扫描哪些目录（工作区相对 glob）。

        Raises:
            `ValueError`: ``policy.max_age_days`` 为负数。
        """
        if policy.max_age_days is not None and policy.max_age_days < 0:
            raise ValueError(f"max_age_days 不能为负，收到 {policy.max_age_days}")
        if policy.min_hits < 0:
            raise ValueError(f"min_hits 不能为负，收到 {policy.min_hits}")
        self.client = client
        self.policy = policy
        self.globs = tuple(globs)
        if workspace is None:
            from .workspace import ReMeWorkspace

            workspace = ReMeWorkspace(root=self.client.workspace_dir)
        self.workspace = workspace
        self._hits_source = hits
        self._explicit_hits = hits is not None

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    async def plan(self) -> ForgetPlan:
        """算出遗忘计划（**不改磁盘**，契约 §3.18）。

        Returns:
            `ForgetPlan`: 三类路径清单。

        Raises:
            `WorkspaceError`: 工作区根不存在。
        """
        policy = self.policy
        if not self._explicit_hits:
            logger.warning(
                "MemoryForgetter 没有 hit 数据源（hits=None）：所有文件的命中次数按 0 处理，"
                "于是所有超过 {} 天的文件都会被判为 delete。"
                "生产用法请传 HitCounter / 字典 / 可调用对象。",
                policy.max_age_days,
            )

        now = time.time()
        protected_tags = policy.normalized_protected()
        deletion: list[str] = []
        demotion: list[str] = []
        protected: list[str] = []
        scanned = 0

        for relative, age_days in await self._scan(now):
            scanned += 1
            tags = await self._tags_of(relative)
            if protected_tags and {tag.casefold() for tag in tags} & protected_tags:
                protected.append(relative)
                continue
            if policy.max_age_days is None or age_days <= policy.max_age_days:
                continue
            hits = self._hits(relative)
            if hits <= policy.min_hits:
                deletion.append(relative)
            else:
                demotion.append(relative)

        plan = ForgetPlan(
            delete=sorted(deletion),
            demote=sorted(demotion),
            protected=sorted(protected),
        )
        logger.info("forget plan: 扫描 {} 个文件，{}", scanned, plan.summary())
        return plan

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def apply(self, plan: ForgetPlan) -> int:
        """执行计划（契约 §3.18）。

        删除走 ReMe 自己的 ``delete`` job（``delete_step``），
        因为它顺手清理了 ``file_store`` / catalog 里的登记并返回幸存的反向 wikilink；
        只有在该 job 不在装配白名单里时才退化成 :meth:`Path.unlink` ——
        退化路径会留下 store 里的孤儿 chunk，所以它记一条 warning，
        并明确说出"索引需要 reindex 才能收敛"。

        降权走 ``frontmatter_update`` job（写 ``memory_status: stale``）。

        Args:
            plan (`ForgetPlan`): :meth:`plan` 的产物（也可以人工改过的）。

        Returns:
            `int`: **实际删除成功**的文件数。

            降权不计入返回值 —— 契约的 ``int`` 没有说它是什么，
            而"删了几个"是唯一不会引起歧义的读法；
            降权的条数写在日志里。
        """
        deleted = 0
        for relative in plan.delete:
            if await self._delete(relative):
                deleted += 1

        demoted = 0
        for relative in plan.demote:
            if await self._demote(relative):
                demoted += 1
        logger.info(
            "forget apply: 删除 {}/{}，降权 {}/{}",
            deleted,
            len(plan.delete),
            demoted,
            len(plan.demote),
        )
        return deleted

    async def purge(self) -> int:
        """``plan()`` + ``apply()`` 的便捷组合（**不可逆**）。

        Returns:
            `int`: 实际删除的文件数。
        """
        return await self.apply(await self.plan())

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def stale_paths(self) -> list[str]:
        """列出已被降权（``memory_status: stale``）的路径。

        降权不会自动影响检索排序（见模块 docstring），所以"哪些已经过期"
        必须能被读出来，否则这个标记写了等于没写。

        Returns:
            `list[str]`: 工作区相对路径。
        """
        from .frontmatter import FrontMatter

        stale: list[str] = []
        for relative, _age in await self._scan(time.time()):
            try:
                front, _body = await asyncio.to_thread(
                    FrontMatter.from_file,
                    self.workspace.resolve_relative(relative),
                )
            except Exception as exc:  # noqa: BLE001 - 读不了就当没标记
                logger.debug("stale_paths: 读 {} 的 front matter 失败: {}", relative, exc)
                continue
            if str((front.extra or {}).get(DEMOTE_KEY, "")).strip().casefold() == DEMOTE_VALUE:
                stale.append(relative)
        return sorted(stale)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _scan(self, now: float) -> list[tuple[str, float]]:
        """扫描候选文件，返回 ``(相对路径, 年龄天数)``。

        Args:
            now (`float`): 当前时间戳。

        Returns:
            `list[tuple[str, float]]`: 已排序的候选列表。

        Raises:
            `WorkspaceError`: 工作区根目录不存在。
        """
        root = self.workspace.root
        if not root.is_dir():
            from .workspace import WorkspaceError

            raise WorkspaceError(f"工作区根目录不存在: {root}（先调 workspace.ensure()）")

        def _collect() -> list[tuple[str, float]]:
            found: dict[str, float] = {}
            for pattern in self.globs:
                for path in root.glob(pattern):
                    if not path.is_file():
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    relative = self.workspace.relative(path)
                    found[relative] = max(0.0, (now - mtime) / _DAY_SECONDS)
            return sorted(found.items())

        return await asyncio.to_thread(_collect)

    def _hits(self, relative: str) -> int:
        """读一个路径的命中次数。

        Args:
            relative (`str`): 工作区相对路径。

        Returns:
            `int`: 命中次数。
        """
        source = self._hits_source
        if source is None:
            return 0
        if isinstance(source, HitCounter):
            return source.count(relative)
        if isinstance(source, Mapping):
            return int(source.get(relative, 0) or 0)
        if callable(source):
            try:
                return int(source(relative) or 0)
            except Exception as exc:  # noqa: BLE001 - 数据源坏了不该让 plan 崩
                logger.warning("hits 数据源对 {} 求值失败: {}", relative, exc)
                return 0
        return 0

    async def _tags_of(self, relative: str) -> list[str]:
        """读一个文件的标签（直接问 ``tag_index`` 的 ``tags_for_path``）。

        不走 :class:`~harness_kit.memory.catalog.CatalogManager`：
        那个门面负责的是 catalog 与**写标签**，而这里只需要读一个索引值。
        直接调组件是 ReMe 的公开接口
        （``third_party/ReMe/reme/components/tag_index/local_tag_index.py:143``），
        少绕一层也少一处可能漂移的适配。

        Args:
            relative (`str`): 工作区相对路径。

        Returns:
            `list[str]`: 标签；读不到时返回空列表。

            返回空而不是抛异常，是一个**有方向的选择**：标签读不到意味着
            "判不出是否受保护"，而两种处理方式（当受保护 / 当不受保护）
            各会误伤一边。这里选"当不受保护"（即空列表），
            因为它只是"少了一层保护"，而另一种会让**所有**文件都免于遗忘、
            让整个遗忘机制静默失效。相应地，日志级别是 warning 而不是 debug。
        """
        try:
            tag_index = self.client.component("tag_index", "default")
            return list(await tag_index.tags_for_path(relative))
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            logger.warning("读 {} 的标签失败（按无标签处理）: {}", relative, exc)
            return []

    async def _delete(self, relative: str) -> bool:
        """删除一个文件（优先走 ReMe 的 ``delete`` job）。

        Args:
            relative (`str`): 工作区相对路径。

        Returns:
            `bool`: 是否删掉了。
        """
        try:
            response = await self.client.run_job("delete", path=relative)
        except Exception as exc:  # noqa: BLE001 - 退化到文件系统删除
            logger.warning(
                "forget: delete job 不可用（{}），退化为直接 unlink {}；"
                "store 里会留下孤儿 chunk，需要 reindex 收敛。",
                exc,
                relative,
            )
            return await self._unlink(relative)
        answer = str(getattr(response, "answer", "") or "")
        logger.info("forget: 删除 {}（{}）", relative, answer[:120])
        return True

    async def _unlink(self, relative: str) -> bool:
        """直接删文件（降级路径）。

        Args:
            relative (`str`): 工作区相对路径。

        Returns:
            `bool`: 是否删掉了。
        """

        def _run() -> bool:
            try:
                self.workspace.resolve_relative(relative).unlink()
                return True
            except OSError as exc:
                logger.warning("forget: 删除 {} 失败: {}", relative, exc)
                return False

        return await asyncio.to_thread(_run)

    async def _demote(self, relative: str) -> bool:
        """降权：写 ``memory_status: stale``。优先 ``frontmatter_update`` job。

        Args:
            relative (`str`): 工作区相对路径。

        Returns:
            `bool`: 是否写入成功。
        """
        try:
            await self.client.run_job(
                "frontmatter_update",
                path=relative,
                metadata={DEMOTE_KEY: DEMOTE_VALUE},
            )
            logger.info("forget: 降权 {} → {}={}", relative, DEMOTE_KEY, DEMOTE_VALUE)
            return True
        except Exception as exc:  # noqa: BLE001 - 退化到本地写 front matter
            logger.debug("forget: frontmatter_update job 不可用（{}），改用本地 front matter 写入", exc)
            return await self._write_frontmatter(relative, DEMOTE_KEY, DEMOTE_VALUE)

    async def _write_frontmatter(self, relative: str, key: str, value: str) -> bool:
        """降级路径：用 harness 自己的 front matter 读写器落一个键。

        Args:
            relative (`str`): 工作区相对路径。
            key (`str`): 键名。
            value (`str`): 值。

        Returns:
            `bool`: 是否写入成功。
        """
        from .frontmatter import FrontMatter

        target = self.workspace.resolve_relative(relative)

        def _run() -> bool:
            try:
                front, body = FrontMatter.from_file(target)
                extra = dict(front.extra or {})
                extra[key] = value
                front.extra = extra
                target.write_text(front.render(body), encoding="utf-8")
                return True
            except Exception as exc:  # noqa: BLE001 - 写不进去就是失败
                logger.warning("forget: 本地写 {} 的 front matter 失败: {}", target, exc)
                return False

        return await asyncio.to_thread(_run)
