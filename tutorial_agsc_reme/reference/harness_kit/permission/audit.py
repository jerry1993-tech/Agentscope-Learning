# -*- coding: utf-8 -*-
"""权限决策审计日志（契约 §3.11，第 11 讲）。

**为什么要审计**

权限系统回答的是"这次调用放不放行"，审计日志回答的是"**上次为什么放行了**"。
出事故之后唯一的追责依据就是它，所以有三条硬性质：

1. **只追加**。没有 update / delete 接口 —— 日志一旦可改，就不再是证据。
2. **默认不落原始输入**。``Bash`` 的 ``command`` 里可能有 token，``Write`` 的
   ``content`` 里可能有密钥。默认只落 ``sha256`` 摘要，需要核对内容时用
   "拿候选输入重算摘要"的方式比对，而不是把原文存下来
   （:attr:`AuditLog.hash_inputs` 可以关掉，但那是**调试开关**，不是生产选项）。
3. **崩溃只丢最后一行**。与 :mod:`harness_kit.session.jsonl_store` 同一套落盘纪律：
   ``O_APPEND`` + 单次 ``write`` + ``fsync``（直接复用其
   ``_append_plain_line`` 辅助函数，保证两条日志链路的行为逐字一致）。

**为什么不用 zstd 压缩**：审计日志要能被 ``grep`` / ``jq`` / ``tail -f`` 直接消费，
这是运维的现实需求。事件流可以压缩（只有 harness 自己读），审计日志不行。

**摘要保护到哪一步为止（诚实说明）**

``hash_inputs=True`` 保证 ``AuditEntry.input_digest`` 里**不含** ``tool_input`` 的原文。
但 ``AuditEntry.reason`` 来自引擎的 ``decision_reason`` / ``message``，那是
**AgentScope 生成的字符串**，其中可能包含规则模式甚至输入片段
（例如 ``.../_engine.py:717`` 的 ``f"Rule: {rule.rule_content}"``）。
本模块**不**去改写它 —— 它承载的是"为什么拒"，抹掉就等于毁掉证据。
需要更强脱敏的场景（例如 Bash 命令里带 token）应当在
:class:`~harness_kit.middleware.redact.RedactMiddleware` 那一层做，
那里知道"哪些工具输入字段是敏感的"。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from loguru import logger
from pydantic import BaseModel, ConfigDict

from harness_kit.events import utc_now
from harness_kit.session.jsonl_store import (
    _append_plain_line,
    _read_plain_lines,
)

__all__ = [
    "AuditEntry",
    "AuditLog",
    "digest_input",
]


def digest_input(tool_input: dict[str, Any]) -> str:
    """把工具输入算成一个稳定的 ``sha256`` 摘要。

    "稳定"是这里的关键：``json.dumps(..., sort_keys=True)`` 保证同样的键值对
    无论插入顺序如何都得到同样的字节串，因此事后可以用"重算摘要"来验证
    "当时放行的到底是不是这条命令"，而无需把原文落盘。

    Args:
        tool_input (`dict[str, Any]`): 工具输入。

    Returns:
        `str`: ``"sha256:<hex>"``。

    Example:
        >>> digest_input({"a": 1, "b": 2}) == digest_input({"b": 2, "a": 1})
        True
        >>> digest_input({"a": 1})[:7]
        'sha256:'
    """
    canonical = json.dumps(
        tool_input,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditEntry(BaseModel):
    """一条权限决策记录（契约 §3.11）。

    Attributes:
        ts (`datetime`): 决策时刻（UTC, tz-aware）。
        session_id (`str`): 会话 id。
        tool_name (`str`): 工具名。
        input_digest (`str`): 输入摘要（``sha256:<hex>``），或关闭哈希时的原始 JSON。
        behavior (`PermissionBehavior`): 决策行为。
        reason (`str`): 决策原因（引擎的 ``decision_reason`` 或 ``message``）。
        mode (`str`): 决策时的权限模式（取证必需，见下）。
        bypass_immune (`bool`): 是否是不可被 allow 规则消解的"安全 ASK"。
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    session_id: str
    tool_name: str
    input_digest: str
    behavior: PermissionBehavior
    reason: str
    mode: str = ""
    """决策时的 ``PermissionMode.value``。

    契约 §3.11 的字段表里没有它，但"同一个工具在 DEFAULT 下被拒、在 BYPASS 下被放行"
    是最常见的争议场景，缺了模式就无法复盘。带默认值，不影响契约字段的用法。
    """

    bypass_immune: bool = False
    """是否 ``bypass_immune``（安全护栏级别，永不被 allow 规则覆盖）。"""


class AuditLog:
    """权限决策的追加式落盘（契约 §3.11）。

    Example:
        >>> log = AuditLog(Path("/tmp/harness/audit.jsonl"))
        >>> await log.record(tool_name="Bash", tool_input={"command": "rm -rf /"},
        ...                  decision=decision, session_id="s1")
        >>> [e.tool_name for e in await log.read(session_id="s1")]
        ['Bash']
    """

    def __init__(self, path: Path, *, hash_inputs: bool = True) -> None:
        """构造审计日志。

        Args:
            path (`Path`): 落盘路径（JSONL）。父目录不存在时在首次写入前创建。
            hash_inputs (`bool`): ``True``（默认）只落 ``sha256`` 摘要；
                ``False`` 落原始 JSON —— **仅供本地调试**，生产环境不要关。
        """
        self.path: Path = Path(path)
        self.hash_inputs: bool = hash_inputs
        self._lock = asyncio.Lock()
        self.entries_written: int = 0
        """本进程内写入的条数（诊断用）。"""

        if not hash_inputs:
            logger.warning(
                "AuditLog(hash_inputs=False) 会把工具原始输入写入 {}；"
                "工具输入可能含密钥/token，请仅在本地调试时使用",
                self.path,
            )

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    async def record(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        decision: PermissionDecision,
        session_id: str,
    ) -> AuditEntry:
        """记录一次权限决策（契约 §3.11 的签名）。

        Args:
            tool_name (`str`): 工具名。
            tool_input (`dict[str, Any]`): 工具输入。
            decision (`PermissionDecision`): 引擎的决策结果。
            session_id (`str`): 会话 id。

        Returns:
            `AuditEntry`: 落盘的那条记录（方便调用方同步打日志 / 断言）。

        Raises:
            OSError: 落盘失败（磁盘满、权限不足）。**调用方不应吞掉它** ——
                审计日志写不进去而请求继续放行，等于审计失效。
        """
        reason = decision.decision_reason or decision.message
        entry = AuditEntry(
            ts=utc_now(),
            session_id=session_id,
            tool_name=tool_name,
            input_digest=(
                digest_input(tool_input)
                if self.hash_inputs
                else json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
            ),
            behavior=decision.behavior,
            reason=reason,
            mode=self._mode_of(decision),
            bypass_immune=bool(decision.bypass_immune),
        )

        async with self._lock:
            await asyncio.to_thread(self._append, entry)
            self.entries_written += 1

        logger.bind(
            session_id=session_id,
            tool=tool_name,
            behavior=entry.behavior.value,
            digest=entry.input_digest[:19],
        ).info("权限决策已审计")
        return entry

    def _append(self, entry: AuditEntry) -> None:
        """同步落盘（在 ``asyncio.to_thread`` 里跑，避免阻塞事件循环）。

        Args:
            entry (`AuditEntry`): 待落盘的记录。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _append_plain_line(self.path, entry.model_dump_json(), fsync=True)

    @staticmethod
    def _mode_of(decision: PermissionDecision) -> str:
        """从决策原因里把模式名抠出来（``"Mode: default"`` → ``"default"``）。

        引擎的兜底决策会把模式写进 ``decision_reason``
        （``third_party/agentscope/src/agentscope/permission/_engine.py:206``），
        这是唯一一个能在**不查状态**的情况下反推模式的地方。
        :class:`~harness_kit.permission.policy.HarnessPermissionEngine`
        会在记录前把当前模式写进 ``decision_reason``，因此常规路径下这里总能取到值；
        取不到时返回空串，而不是编造一个。

        Args:
            decision (`PermissionDecision`): 决策。

        Returns:
            `str`: 模式名或 ``""``。
        """
        reason = decision.decision_reason or ""
        marker = "Mode: "
        index = reason.find(marker)
        if index < 0:
            return ""
        tokens = reason[index + len(marker) :].split()
        return tokens[0] if tokens else ""

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    async def read(
        self,
        *,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """读最近的审计记录（契约 §3.11 的签名）。

        Args:
            session_id (`str | None`): 只读该会话的记录；``None`` 表示全部会话。
            limit (`int`): 最多返回多少条（**取最近的** ``limit`` 条）。

        Returns:
            `list[AuditEntry]`: 文件顺序（旧 → 新）排列的记录。

        Raises:
            ValueError: ``limit`` 小于 1。
        """
        if limit < 1:
            raise ValueError(f"limit 必须 >= 1，收到 {limit}")
        if not self.path.exists():
            return []

        kept: deque[AuditEntry] = deque(maxlen=limit)
        for line in await asyncio.to_thread(self._load_lines):
            if session_id is not None and line.session_id != session_id:
                continue
            kept.append(line)
        return list(kept)

    def _load_lines(self) -> list[AuditEntry]:
        """同步读全部记录（在 ``asyncio.to_thread`` 里跑）。

        坏行（被截断的尾行）跳过并告警，而不是让整个读取失败 ——
        审计日志的价值在于"尽量读出来"，不在一行不差。

        Returns:
            `list[AuditEntry]`: 全部可解析的记录。
        """
        entries: list[AuditEntry] = []
        for index, raw in enumerate(_read_plain_lines(self.path), start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                entries.append(AuditEntry.model_validate_json(text))
            except ValueError as exc:
                # pydantic 的 ValidationError 会把每个字段的错误都展开，几十行起步；
                # 日志里只留一行摘要，完整原因交给调用方按需重新解析。
                detail = str(exc).splitlines()[0]
                logger.warning("审计日志第 {} 行无法解析，已跳过: {}", index, detail)
        return entries

    async def tail(self, *, limit: int = 20) -> list[str]:
        """取最后 ``limit`` 行的**原始文本**（人工排障时直接打印）。

        返回原始文本而非 :class:`AuditEntry`，是因为排障现场最需要的往往是
        "那行到底是什么样"——包括解析失败的那一行。

        Args:
            limit (`int`): 行数。

        Returns:
            `list[str]`: 原始行（不含行尾换行符）。

        Raises:
            ValueError: ``limit`` 小于 1。
        """
        if limit < 1:
            raise ValueError(f"limit 必须 >= 1，收到 {limit}")
        if not self.path.exists():
            return []

        raw_lines = await asyncio.to_thread(lambda: list(_read_plain_lines(self.path)))
        return [line.rstrip("\n") for line in raw_lines[-limit:]]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        """收尾。

        本实现每次写入即时开关文件句柄（与
        :class:`~harness_kit.session.jsonl_store.JsonlSessionStore` 同策略），
        因此这里是空操作；保留它是为了让调用方能用统一的
        ``async with`` / ``try-finally`` 形态管理所有落盘组件。
        """
        logger.bind(path=str(self.path)).debug(
            "AuditLog 已关闭（本进程共写入 {} 条）",
            self.entries_written,
        )

    def describe(self) -> dict[str, Any]:
        """返回人类可读的自述（CLI ``harness inspect`` 用）。

        Returns:
            `dict[str, Any]`: 路径、是否哈希、已写条数、文件是否存在、大小。
        """
        return {
            "path": str(self.path),
            "hash_inputs": self.hash_inputs,
            "entries_written": self.entries_written,
            "exists": self.path.exists(),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
