# -*- coding: utf-8 -*-
"""按能力路由的 Agent 注册表与选择（契约 §3.13，第 13 讲）。

**为什么不用 LLM 来选人**：让模型「从下面 6 个 Agent 里选一个」是最诱人也最
贵的做法。三个问题：

1. **贵且慢**：每次派活多一次 LLM 往返；
2. **不可复现**：同一个任务两次派给不同的人，评测里噪声直接盖过信号；
3. **可解释性差**：事后审计「为什么是 B 接的」只能贴一段模型输出。

本模块用**确定性打分**替代：能力标签命中 + 历史成功率，平局按名字字典序决胜，
因此同一个输入永远给出同一个答案（契约 §3.13 原文：「score 由 capability
命中数 + 历史成功率共同决定，确定性可复现」）。**LLM 该做的是「这个任务是
什么类别」，那是分类问题；「谁来做」是查表问题** —— 分类交给出题人（调用方
的 ``required`` 参数或上游拆解器的 ``capability`` 字段），查表交给这里。
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CapabilityRouter",
    "MemberScore",
    "NoRouteError",
]

_TOKEN_RE = re.compile(r"[a-zA-Z0-9一-鿿]+")
"""分词：拉丁词 / 数字 / 汉字。**不引入分词库**：路由的任务文本是「能力标签 +
一句话」，按字符类切已经够用，多一个依赖不值。

**下划线必须当分隔符，不能进字符类。** 这是踩过的坑：写成
``[a-zA-Z0-9_...]`` 时 ``_normalize("code_review")`` 会原样返回
``"code_review"``，而 ``_normalize("Code-Review")`` 返回 ``"codereview"``
—— 于是「写标签的人用连字符、写 required 的人用下划线」这种情况下
``required="code_review"`` 匹配不到标签 ``"Code-Review"``，路由静默失败，
最后表现为一句莫名其妙的 ``NoRouteError``。验证脚本里有一条专门守着它。"""


def _normalize(text: str) -> str:
    """归一化：小写 + 去掉分隔符。

    ``code_review`` / ``code-review`` / ``CodeReview`` 在标签空间里是同一个
    能力，归一化后都是 ``codereview``。不做这一步，「写标签的人」和「写任务
    的人」之间的连字符差异会静默地让路由失效。

    Args:
        text (`str`): 原始文本。

    Returns:
        `str`: 归一化结果（只含字母数字与汉字）。
    """
    return "".join(_TOKEN_RE.findall(text.lower()))


def _tokens(text: str) -> list[str]:
    """切成 token 列表（小写）。

    Args:
        text (`str`): 原始文本。

    Returns:
        `list[str]`: token 列表。
    """
    return [_.lower() for _ in _TOKEN_RE.findall(text)]


class NoRouteError(LookupError):
    """找不到能接这个任务的成员，或成员本身不存在。

    两种语义共用这一个异常，用 ``kind`` 区分（``"route"`` / ``"member"``）：
    调用方 ``except NoRouteError`` 时二者都该被抓住（「查不到人」是同一种失败），
    但**报错文本必须不同** —— 早期版本在「查成员」时也套用路由的文案，
    于是 ``team.agent("boss")`` 报出「没有成员能接这个任务：required='boss'」，
    让人以为是路由配置问题而不是「这个人根本不在队里」。
    """

    def __init__(
        self,
        task: str,
        required: str | None,
        available: Iterable[str],
        *,
        kind: str = "route",
    ) -> None:
        """构造错误。

        Args:
            task (`str`): 原始任务文本（会截断到 120 字）；``kind="member"`` 时
                传成员名。
            required (`str | None`): 要求的必需能力；``kind="member"`` 时传成员名。
            available (`Iterable[str]`): 现有成员名。
            kind (`str`, defaults to ``"route"``): ``"route"`` = 路由不到人，
                ``"member"`` = 成员不存在。
        """
        self.task = task
        self.required = required
        self.kind = kind
        if kind == "member":
            super().__init__(
                f"成员 {task!r} 不存在；现有成员 {sorted(available)}。",
            )
            return
        preview = task if len(task) <= 120 else task[:120] + "…"
        super().__init__(
            f"没有成员能接这个任务：required={required!r}, task={preview!r}；"
            f"现有成员 {sorted(available)}。"
            "要么加上 required 说的那个能力标签，要么别指定 required。",
        )

    @classmethod
    def for_member(
        cls,
        member: str,
        available: Iterable[str],
    ) -> "NoRouteError":
        """造一个「成员不存在」形态的错误。

        Args:
            member (`str`): 找不到的成员名。
            available (`Iterable[str]`): 现有成员名。

        Returns:
            `NoRouteError`: ``kind="member"`` 的异常实例。
        """
        return cls(member, member, available, kind="member")


class MemberScore(BaseModel):
    """一次打分的中间结果，供 ``explain`` 与调试使用。"""

    model_config = ConfigDict(extra="forbid")

    member: str
    """成员名。"""
    score: float = 0.0
    """总分（0~1）。"""
    capability_score: float = 0.0
    """能力命中分（0~1）。"""
    success_rate: float = 0.0
    """历史成功率（0~1；没有历史时取 0.5，见 :meth:`CapabilityRouter.route`）。"""
    attempts: int = 0
    """历史被调用次数。"""
    matched: list[str] = Field(default_factory=list)
    """命中的能力标签。"""


class CapabilityRouter:
    """能力注册表 + 确定性选人。

    Args:
        capabilities (`dict[str, list[str]]`): ``{成员名: [能力标签]}``。
            能力标签大小写与连字符不敏感（见 :func:`_normalize`）。
        history_weight (`float`, defaults to `0.3`): 历史成功率在总分里的权重，
            ``0.0`` = 纯看能力标签，``1.0`` = 纯看历史（那会让冷启动全平局，
            不推荐）。取值必须落在 ``[0, 1]``。

    Raises:
        ValueError: ``history_weight`` 不在 ``[0, 1]``，或能力表为空。
    """

    def __init__(
        self,
        capabilities: dict[str, list[str]],
        *,
        history_weight: float = 0.3,
    ) -> None:
        """初始化注册表。"""
        if not 0.0 <= history_weight <= 1.0:
            raise ValueError(
                f"history_weight 必须在 [0, 1]，收到 {history_weight}。",
            )
        if not capabilities:
            raise ValueError("能力表为空：没有任何成员可路由。")
        self.history_weight = float(history_weight)
        self._capabilities: dict[str, list[str]] = {
            name: list(tags) for name, tags in capabilities.items()
        }
        self._normalized: dict[str, set[str]] = {
            name: {_normalize(_) for _ in tags if _}
            for name, tags in self._capabilities.items()
        }
        self._ok: dict[str, int] = {name: 0 for name in capabilities}
        self._total: dict[str, int] = {name: 0 for name in capabilities}

    # ==================================================================
    # 注册表
    # ==================================================================
    @property
    def members(self) -> list[str]:
        """成员名（字典序，确定性）。

        Returns:
            `list[str]`: 成员名列表。
        """
        return sorted(self._capabilities)

    def capabilities_of(self, member: str) -> list[str]:
        """查一个成员的能力标签。

        Args:
            member (`str`): 成员名。

        Returns:
            `list[str]`: 能力标签（原始写法）。

        Raises:
            NoRouteError: 成员不存在。
        """
        self._require(member)
        return list(self._capabilities[member])

    def add_member(self, member: str, capabilities: Sequence[str]) -> None:
        """登记一个新成员。

        Args:
            member (`str`): 成员名。
            capabilities (`Sequence[str]`): 能力标签。

        Raises:
            ValueError: 成员已存在（**不静默覆盖**：覆盖会悄悄改掉路由结果）。
        """
        if member in self._capabilities:
            raise ValueError(
                f"成员 {member!r} 已登记；如需改能力，请先 remove_member。",
            )
        self._capabilities[member] = list(capabilities)
        self._normalized[member] = {_normalize(_) for _ in capabilities if _}
        self._ok[member] = 0
        self._total[member] = 0
        logger.debug("CapabilityRouter: +{} {}", member, capabilities)

    def remove_member(self, member: str) -> bool:
        """移除一个成员。

        Args:
            member (`str`): 成员名。

        Returns:
            `bool`: 真删掉了为 ``True``，本来就没有为 ``False``。

        Raises:
            ValueError: 移除后一个成员都不剩。
        """
        if member not in self._capabilities:
            return False
        if len(self._capabilities) == 1:
            raise ValueError("不能移除最后一个成员：路由表会变成空的。")
        del self._capabilities[member]
        del self._normalized[member]
        del self._ok[member]
        del self._total[member]
        return True

    # ==================================================================
    # 打分
    # ==================================================================
    def success_rate(self, member: str) -> float:
        """历史成功率；没有历史时返回 ``0.5``。

        **为什么冷启动是 0.5 而不是 0**：给 0 会让所有新成员在第一次打分里
        被历史权重压死，永远轮不到它们出一次场 —— 于是「历史成功率」这个维度
        变成了自我实现的预言，整个路由退化成「第一次选谁就永远是它」。
        0.5 是「未知」的中性先验。

        Args:
            member (`str`): 成员名。

        Returns:
            `float`: ``0.0 ~ 1.0``。
        """
        self._require(member)
        total = self._total[member]
        if total == 0:
            return 0.5
        return self._ok[member] / total

    def score(self, member: str, task: str) -> MemberScore:
        """给一个成员打分。

        能力分 = **命中的能力标签数 / 该成员的能力标签总数**。
        用「比例」而不是「个数」是为了不让「标签写得多的人」作弊 ——
        一个挂了 20 个标签的成员如果只命中 1 个，不该赢过精准命中 1/1 的人。

        Args:
            member (`str`): 成员名。
            task (`str`): 任务文本。

        Returns:
            `MemberScore`: 打分明细。
        """
        self._require(member)
        tags = self._normalized[member]
        haystack = _normalize(task)
        words = set(_tokens(task))
        matched = [
            tag
            for tag in tags
            if tag and (tag in haystack or tag in words)
        ]
        cap = len(matched) / len(tags) if tags else 0.0
        rate = self.success_rate(member)
        total = (
            (1.0 - self.history_weight) * cap
            + self.history_weight * rate
        )
        return MemberScore(
            member=member,
            score=total,
            capability_score=cap,
            success_rate=rate,
            attempts=self._total[member],
            matched=sorted(matched),
        )

    def rank(self, task: str, *, required: str | None = None) -> list[MemberScore]:
        """给出所有候选成员的排序（确定性）。

        排序键：``(-score, attempts, member)`` —— 分数降序，其次**调用次数少的
        优先**（轮转，避免一个强成员吃掉所有活），最后按名字字典序保证平局
        可复现。

        Args:
            task (`str`): 任务文本。
            required (`str | None`, optional): 必需能力。给了它就只有具备该能力
                的成员进入候选（精确匹配归一化后的标签）。

        Returns:
            `list[MemberScore]`: 排序后的打分明细。

        Raises:
            NoRouteError: 候选集为空。
        """
        candidates = self._candidates(required)
        if not candidates:
            raise NoRouteError(task, required, self.members)
        scores = [self.score(_, task) for _ in candidates]
        scores.sort(key=lambda s: (-s.score, s.attempts, s.member))
        return scores

    def route(self, task: str, *, required: str | None = None) -> str:
        """选一个成员接这个任务。

        Args:
            task (`str`): 任务文本。
            required (`str | None`, optional): 必需能力。

        Returns:
            `str`: 成员名。

        Raises:
            NoRouteError: 没有成员具备 ``required`` 指定的能力。
        """
        ranked = self.rank(task, required=required)
        chosen = ranked[0]
        logger.info(
            "CapabilityRouter.route: {} <- required={!r} score={:.3f} "
            "(cap={:.2f} hist={:.2f} matched={})",
            chosen.member,
            required,
            chosen.score,
            chosen.capability_score,
            chosen.success_rate,
            chosen.matched,
        )
        return chosen.member

    def route_many(
        self,
        task: str,
        *,
        k: int,
        required: str | None = None,
        unique_capability: bool = False,
    ) -> list[str]:
        """选 ``k`` 个成员（``broadcast`` 用）。

        Args:
            task (`str`): 任务文本。
            k (`int`): 需要几个成员。
            required (`str | None`, optional): 必需能力。
            unique_capability (`bool`, defaults to `False`): 为真时每个成员只能
                占一个「主能力」（取命中列表的第一个），避免选出 3 个能力完全
                相同的成员 —— 那等于同一件事做 3 遍。

        Returns:
            `list[str]`: 成员名，按打分从高到低；可用成员不足 ``k`` 时返回
            全部可用成员（**不报错**：广播给「能广播的所有人」是合理语义）。

        Raises:
            NoRouteError: 一个候选都没有。
        """
        ranked = self.rank(task, required=required)
        if not unique_capability:
            return [_.member for _ in ranked[: max(1, k)]]
        out: list[str] = []
        used: set[str] = set()
        for item in ranked:
            key = item.matched[0] if item.matched else item.member
            if key in used:
                continue
            used.add(key)
            out.append(item.member)
            if len(out) >= max(1, k):
                break
        return out or [_.member for _ in ranked[:1]]

    # ==================================================================
    # 历史
    # ==================================================================
    def record(self, member: str, *, ok: bool) -> None:
        """记一次结果，供后续打分使用。

        Args:
            member (`str`): 成员名。
            ok (`bool`): 是否成功。

        Raises:
            NoRouteError: 成员不在注册表里（不静默丢弃，否则成功率会失真）。
        """
        self._require(member)
        self._total[member] += 1
        if ok:
            self._ok[member] += 1

    def history(self) -> dict[str, dict[str, float]]:
        """导出历史统计。

        Returns:
            `dict[str, dict[str, float]]`: ``{成员: {"attempts", "ok", "success_rate"}}``。
        """
        return {
            name: {
                "attempts": float(self._total[name]),
                "ok": float(self._ok[name]),
                "success_rate": self.success_rate(name),
            }
            for name in self.members
        }

    def reset_history(self) -> None:
        """清空历史成功率（每轮评测之间复位）。"""
        for name in self._capabilities:
            self._ok[name] = 0
            self._total[name] = 0

    # ==================================================================
    # 展示 / 内部
    # ==================================================================
    def explain(self, task: str, *, required: str | None = None) -> str:
        """逐成员打印打分明细（``--explain`` 用）。

        Args:
            task (`str`): 任务文本。
            required (`str | None`, optional): 必需能力。

        Returns:
            `str`: 多行文本；没有候选时给一行说明而不是抛异常。
        """
        lines = [
            f"CapabilityRouter(history_weight={self.history_weight}, "
            f"members={len(self._capabilities)})",
            f"  task: {task[:100]!r}  required={required!r}",
        ]
        try:
            ranked = self.rank(task, required=required)
        except NoRouteError as exc:
            lines.append(f"  -> NoRouteError: {exc}")
            return "\n".join(lines)
        for item in ranked:
            lines.append(
                f"  {item.member:<16} score={item.score:.3f} "
                f"cap={item.capability_score:.2f} hist={item.success_rate:.2f} "
                f"n={item.attempts} matched={item.matched}",
            )
        return "\n".join(lines)

    def _candidates(self, required: str | None) -> list[str]:
        """按 ``required`` 过滤候选成员。

        Args:
            required (`str | None`): 必需能力。

        Returns:
            `list[str]`: 候选成员名（字典序）。
        """
        if required is None:
            return self.members
        key = _normalize(required)
        return [
            name
            for name in self.members
            if key in self._normalized[name]
            or any(key in tag for tag in self._normalized[name])
        ]

    def _require(self, member: str) -> None:
        """断言成员存在。

        Args:
            member (`str`): 成员名。

        Raises:
            NoRouteError: 成员不存在。
        """
        if member not in self._capabilities:
            raise NoRouteError.for_member(member, self.members)
