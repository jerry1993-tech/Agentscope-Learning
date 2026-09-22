# -*- coding: utf-8 -*-
"""主动读取：在用户没问之前，按置信度阈值把可能有用的记忆准备好（契约 §3.18）。

**主动读取要回答的第一个问题不是"阈值多少"，而是"拿什么去查"**

被动检索有明确的查询串（用户这句话）。主动读取没有 —— 所以本模块的本体其实是
:meth:`ProactiveReader.query_for`，它按**两级**把一个会话变成查询串：

1. **会话对话文件**（首选）：``auto_memory`` 每次执行都会先调
   ``_save_session_messages``（``third_party/ReMe/reme/steps/evolve/auto_memory.py:378``），
   把这一轮的 Msg 逐行写到
   ``<workspace>/session/dialog/<session_id>.jsonl``
   （路径由 ``_session_source_path`` 生成，``auto_memory.py:81-82``）。
   这个文件**无条件**写，与 create / update 分支无关，所以它是可靠的证据源：
   取最后若干条 user 消息的文本，就是"这个人最近在关心什么"。
2. 对话文件不存在（还没蒸馏过）→ 返回 ``None``，:meth:`suggest` 直接返回空列表
   并打一条 warning。**不**去猜、也不退回"用一个固定字符串查一遍"：
   固定查询串会让每一次主动读取都返回同一批记忆，看起来在工作，
   实际上等于把"没有输入"伪装成了"有输入"。

**为什么 ``min_confidence`` 必须配 metrics**

``min_confidence`` 一过滤，函数就返回空列表 —— 而"空列表"的语义是**过载**的：
可能是"这次没有相关的"，也可能是"有一条但分数不够"，还可能是"根本没查到"。
契约 §3.18 的已知坑点名了这件事，本模块的落点是：**每一条被阈值挡掉的 hit
都记一次 :meth:`~harness_kit.memory.metrics.MemoryMetrics.record_gate_rejection`
（``reason="below_min_score"``），并在日志里给出"候选 N 条、放行 M 条"**。
于是"什么都没发生"在指标上不再等于"什么都没做"。

**置信度用的是归一化分数，不是原始 score**

理由与 :mod:`harness_kit.memory.gating` 完全一致（RRF 融合分在 0.016 量级，
直接和 0.35 比会全军覆没）。本模块直接复用
:meth:`~harness_kit.memory.gating.MemoryGate.normalize_scores`，
保证"门控"与"主动读取"两处对"置信度"的定义不会漂移成两个意思。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from .citations import MemoryHit
from .gating import MemoryGate
from .search import MemorySearch

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MIN_CONFIDENCE",
    "DIALOG_DIR",
    "ProactiveReader",
]

#: 置信度阈值（契约 §3.18）。
DEFAULT_MIN_CONFIDENCE: float = 0.35

#: 回溯天数（契约 §3.18）。
DEFAULT_LOOKBACK_DAYS: int = 7

#: 会话对话文件所在目录（工作区相对）。
#: 取自 ``auto_memory.py:82`` 的 ``f"{session_dir}/dialog/{session_id}.jsonl"``。
DIALOG_DIR: str = "session/dialog"

#: 从对话文件里最多回看多少条消息来拼查询串。
_MAX_QUERY_MESSAGES: int = 20

#: 拼出来的查询串最长多少个字符（防止把整段会话塞进 BM25）。
_MAX_QUERY_CHARS: int = 600


class ProactiveReader:
    """主动读取器（契约 §3.18）。

    Example::

        reader = ProactiveReader(MemorySearch(client, workspace=ws), min_confidence=0.35)
        hits = await reader.suggest(session_id="s-1", limit=5)
        for hit in hits:
            print(hit.path, f"{hit.score:.4f}")

    ``min_confidence`` 与 ``lookback_days`` 都在构造期固定（契约如此），
    而 ``limit`` 是每次调用的参数 —— 这个分工是合理的：
    阈值与时间窗是**策略**（一个部署一套），条数是**当场决定**的。
    """

    def __init__(
        self,
        search: MemorySearch,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        query_of: Callable[[str], Awaitable[str | None]] | None = None,
        metrics: Any | None = None,
        today_of: Callable[[], dt.date] | None = None,
    ) -> None:
        """配置主动读取器。

        Args:
            search (`MemorySearch`): 检索器（它自带 ``client``，本模块从那里取）。
            min_confidence (`float`): 归一化置信度下限，取值 ``[0.0, 1.0]``。
            lookback_days (`int`): 只看最近多少天的记忆（传给 ``search`` 的
                ``start_date``）。必须为正。
            query_of (`Callable[[str], Awaitable[str | None]] | None`):
                自定义"会话 → 查询串"的推导函数（异步，返回 ``None`` 表示推不出来）。
                ``None`` 时用 :meth:`query_for` 的默认推导。
                这个注入点是给"把会话存在别处"的部署用的，也是给测试用的
                （不必造对话文件）。
            metrics (`Any | None`): 可选的
                :class:`~harness_kit.memory.metrics.MemoryMetrics`。
                **强烈建议给**，理由见模块 docstring。
            today_of (`Callable[[], dt.date] | None`): 取"今天"的函数；
                ``None`` 时用 :meth:`datetime.date.today`。注入它是为了测试可复现。

        Raises:
            `ValueError`: ``min_confidence`` 不在 ``[0, 1]``，或 ``lookback_days <= 0``。
        """
        if not 0.0 <= float(min_confidence) <= 1.0:
            raise ValueError(f"min_confidence 必须在 [0, 1]，收到 {min_confidence}")
        if int(lookback_days) <= 0:
            raise ValueError(f"lookback_days 必须为正，收到 {lookback_days}")
        self.search = search
        self.min_confidence: float = float(min_confidence)
        self.lookback_days: int = int(lookback_days)
        self.query_of = query_of
        self.metrics = metrics
        self._today_of = today_of or dt.date.today

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def suggest(self, *, session_id: str, limit: int = 5) -> list[MemoryHit]:
        """给一个会话准备可能用得上的记忆（契约 §3.18）。

        Args:
            session_id (`str`): 会话 id。
            limit (`int`): 最多返回几条；必须为正。

        Returns:
            `list[MemoryHit]`: 通过置信度阈值的命中（按分数降序）。

        Raises:
            `ValueError`: ``session_id`` 为空，或 ``limit <= 0``。
        """
        caller = str(session_id or "").strip()
        if not caller:
            raise ValueError("suggest 需要非空 session_id")
        if limit <= 0:
            raise ValueError(f"limit 必须为正，收到 {limit}")

        query = await self.query_for(caller)
        if not query:
            logger.warning(
                "proactive: session={!r} 推不出查询串（对话文件 {} 不存在或为空），返回空列表。"
                "这不是失败，但也不代表'没有相关记忆'。",
                caller,
                f"{DIALOG_DIR}/{caller}.jsonl",
            )
            return []

        today = self._today_of()
        start = today - dt.timedelta(days=self.lookback_days)
        result = await self.search.search(
            query,
            limit=int(limit),
            start_date=start.isoformat(),
            end_date=today.isoformat(),
        )
        if self.metrics is not None:
            try:
                self.metrics.record_search(
                    session_id=caller,
                    hits=len(result.hits),
                    elapsed_ms=result.elapsed_ms,
                )
            except Exception as exc:  # noqa: BLE001 - 指标不该让读取失败
                logger.debug("record_search 失败: {}", exc)

        scores = MemoryGate.normalize_scores(result.hits)
        kept: list[MemoryHit] = []
        for hit, confidence in zip(result.hits, scores):
            if confidence >= self.min_confidence:
                kept.append(hit)
                continue
            self._record_below_threshold(caller, hit, confidence)

        logger.info(
            "proactive: session={!r} query={!r} 时间窗={}..{} 候选 {} 条，放行 {} 条（阈值 {}）",
            caller,
            query[:60],
            start.isoformat(),
            today.isoformat(),
            len(result.hits),
            len(kept),
            self.min_confidence,
        )
        return kept

    # ------------------------------------------------------------------
    # 查询串推导
    # ------------------------------------------------------------------
    async def query_for(self, session_id: str) -> str | None:
        """把一个会话推导成查询串。

        先用构造期注入的 ``query_of``；没有就用
        :meth:`query_from_dialog` 读会话对话文件。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `str | None`: 查询串；推不出来时 ``None``。
        """
        if self.query_of is not None:
            try:
                derived = await self.query_of(session_id)
            except Exception as exc:  # noqa: BLE001 - 注入的函数坏了不该让读取崩
                logger.warning("proactive: 注入的 query_of 失败: {}", exc)
                return None
            text = (derived or "").strip()
            return text or None
        return await self.query_from_dialog(session_id)

    async def query_from_dialog(self, session_id: str) -> str | None:
        """从 ``session/dialog/<session_id>.jsonl`` 推导查询串。

        只取 **user 角色**的最后若干条消息：assistant 的回复里会有
        "根据检索到的记忆…"这类由记忆自己产生的内容，拿它当查询串等于
        用记忆查记忆，会把结果锁死在同一个主题上。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `str | None`: 查询串；文件不存在 / 没有 user 文本时 ``None``。
        """
        path = self._dialog_path(session_id)
        if path is None:
            return None

        def _read() -> str | None:
            if not path.is_file():
                return None
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.debug("proactive: 读 {} 失败: {}", path, exc)
                return None
            texts: list[str] = []
            for line in lines[-_MAX_QUERY_MESSAGES * 2 :]:
                text = _user_text(line)
                if text:
                    texts.append(text)
            if not texts:
                return None
            return "\n".join(texts[-_MAX_QUERY_MESSAGES:])[:_MAX_QUERY_CHARS]

        return (await asyncio.to_thread(_read)) or None

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def explain(self) -> str:
        """把当前配置展开成人话（阈值、时间窗、查询来源）。

        Returns:
            `str`: 多行说明。
        """
        source = "注入的 query_of" if self.query_of is not None else f"{DIALOG_DIR}/<session_id>.jsonl 的 user 消息"
        return "\n".join(
            [
                f"ProactiveReader(min_confidence={self.min_confidence}, lookback_days={self.lookback_days})",
                f"  查询来源：{source}",
                f"  置信度：归一化（score / max(score)），与 MemoryGate.normalize_scores 同一定义",
                f"  被阈值挡下的条数会记入 metrics（reason='below_min_score'）",
                "  时间窗：search 的 start_date = 今天 - lookback_days，end_date = 今天",
            ],
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _dialog_path(self, session_id: str) -> Path | None:
        """算出对话文件的绝对路径。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `Path | None`: 路径；拿不到工作区时 ``None``。
        """
        client = getattr(self.search, "client", None)
        workspace_dir = getattr(client, "workspace_dir", None)
        if not workspace_dir:
            logger.debug("proactive: MemorySearch 没有绑定 client.workspace_dir，无法定位对话文件")
            return None
        return Path(workspace_dir) / DIALOG_DIR / f"{session_id}.jsonl"

    def _record_below_threshold(self, session_id: str, hit: MemoryHit, confidence: float) -> None:
        """记录一条被阈值挡下的命中。

        Args:
            session_id (`str`): 会话 id。
            hit (`MemoryHit`): 被挡下的命中。
            confidence (`float`): 它的归一化置信度。
        """
        logger.info(
            "proactive: 挡下一条（置信度 {:.4f} < {:.2f}）path={}",
            confidence,
            self.min_confidence,
            hit.path,
        )
        if self.metrics is None:
            return
        try:
            self.metrics.record_gate_rejection(reason="below_min_score")
        except Exception as exc:  # noqa: BLE001 - 指标不该让读取失败
            logger.debug("record_gate_rejection 失败: {}", exc)


def _user_text(line: str) -> str:
    """从对话文件的一行 JSON 里取出 user 消息的文本。

    ReMe 用 ``Msg.model_dump_json()`` 逐行写（``auto_memory.py:219``），
    所以每行是一个带 ``role`` 与 ``content``（块列表）的 JSON 对象。
    这里**只认** ``role == "user"`` 且块类型为 ``text`` 的内容；
    形状不对的行静默跳过 —— 脏行不该让主动读取整体失败。

    Args:
        line (`str`): JSONL 的一行。

    Returns:
        `str`: 文本；不是 user 消息或解析失败时返回空串。
    """
    raw = line.strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(payload, dict) or str(payload.get("role", "")) != "user":
        return ""
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type", "")) != "text":
            continue
        text = str(block.get("text", "") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)
