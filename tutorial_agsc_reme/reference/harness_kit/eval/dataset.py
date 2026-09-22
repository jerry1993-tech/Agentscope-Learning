# -*- coding: utf-8 -*-
"""评测集结构（``EvalCase`` / ``EvalDataset``）与 JSONL 读写（契约 §3.20 / §5.6）。

**为什么需要自己的一层？** AgentScope 2.0.8 全库没有 benchmark / eval 引擎
（``_recon/12_reme_storage_graph.md`` 的"不存在的能力"清单第 4 条已记录；
实测 ``ls third_party/agentscope/src/agentscope/`` 也没有 ``eval`` 或
``benchmark`` 子包），ReMe 侧只有 ``steps/evaluate`` 这类单步组件，
没有"批量跑用例 + 出报告"的东西。所以这是 harness_kit 要补的真缺口
（契约 §1.3 缺口 3），不是重造轮子。

**字段级真值是契约 §5.6**，它比 §3.20 多了两个评测专用字段
（``expected_tools`` / ``expected_citations``），本模块按 §5.6 实现：
§3.20 的五个字段是 §5.6 的子集，因此两份契约在本实现上同时成立。

JSONL 落盘格式（本模块定义的唯一真值）::

    {"type": "dataset", "name": "code_qa", "tags": ["demo"], "metadata": {...}}   # 第 1 行，可省
    {"type": "case", "id": "c1", "input": "...", "expected": "...", ...}          # 之后每行一个用例

第 1 行是**头**：:meth:`EvalDataset.from_jsonl` 见到 ``type=="dataset"`` 就读
``name``；没有头（裸用例行）时用文件主名当数据集名。这样手写的
"一行一个用例" 文件也能直接吃进来，不必先补一个头。

Example:
    >>> from pathlib import Path
    >>> import tempfile
    >>> case = EvalCase(id="c1", input="1+1=?", expected="2")
    >>> dataset = EvalDataset(name="arith", cases=[case])
    >>> tmp = Path(tempfile.mkdtemp()) / "arith.jsonl"
    >>> _ = dataset.to_jsonl(tmp)
    >>> EvalDataset.from_jsonl(tmp).cases[0].expected
    '2'
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EvalCase",
    "EvalDataset",
    "RewriterFn",
    "dataset_from_cases",
    "synthesize_cases",
]

RewriterFn = Callable[["EvalCase"], Awaitable["EvalCase"]]
"""改写器签名（契约 §3.20）：``EvalCase -> Awaitable[EvalCase]``。"""

_HEADER_TYPE: str = "dataset"
_CASE_TYPE: str = "case"


class EvalCase(BaseModel):
    """一条评测用例（契约 §5.6，字段名不得改）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    """用例 id，数据集内唯一（:meth:`EvalDataset.validate_unique_ids` 会查重）。"""

    input: str
    """喂给 Agent 的用户输入。"""

    expected: str | None = None
    """期望输出文本；``None`` 表示"只看行为不看文本"（如只校验工具调用）。"""

    tags: list[str] = Field(default_factory=list)
    """标签，用于 :meth:`EvalDataset.filter` 挑子集。"""

    context: list[str] = Field(default_factory=list)
    """预先塞进上下文的参考资料。

    语义是"这段对话是在这些资料给定的前提下发生的"，因此
    :class:`~harness_kit.eval.runner.EvalRunner` 会把它**作为独立消息
    先 observe 给 Agent**，而不是拼进用户那句话里 —— 拼进去会改变
    用户输入的措辞，让 ``exact_match`` 之类的指标失去意义。
    """

    metadata: dict[str, Any] = Field(default_factory=dict)
    """任意附加信息；:mod:`harness_kit.eval.metrics` 会读
    ``latency_budget_ms`` / ``require_citation`` / ``judge_criteria`` 三个键。"""

    expected_tools: list[str] = Field(default_factory=list)
    """:class:`~harness_kit.eval.metrics.tool_call_accuracy` 期望出现的工具名。"""

    expected_citations: list[str] = Field(default_factory=list)
    """:class:`~harness_kit.eval.metrics.citation_coverage` 期望出现的来源路径。"""

    def digest(self, limit: int = 120) -> str:
        """返回一行摘要（日志 / 报告用）。

        Args:
            limit (`int`): 输入预览长度上限。

        Returns:
            `str`: ``"c1 [qa] 1+1=? -> 2"`` 形态的摘要。
        """
        text = self.input.replace("\n", " ").strip()
        preview = text if len(text) <= limit else text[:limit] + "…"
        tag = ",".join(self.tags)
        return f"{self.id} [{tag}] {preview}"


class EvalDataset(BaseModel):
    """一个具名评测集（契约 §3.20）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """数据集名，进报告标题与文件名。"""

    cases: list[EvalCase] = Field(default_factory=list)
    """用例列表。"""

    tags: list[str] = Field(default_factory=list)
    """数据集级标签（JSONL 头里有就带回来）。"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """数据集级元信息。"""

    # ------------------------------------------------------------------
    # 校验与派生
    # ------------------------------------------------------------------
    def validate_unique_ids(self) -> list[str]:
        """检查用例 id 是否唯一。

        Returns:
            `list[str]`: 重复的 id（已去重、已排序）；空列表表示合法。
        """
        seen: set[str] = set()
        duplicated: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                duplicated.add(case.id)
            seen.add(case.id)
        return sorted(duplicated)

    def filter(
        self,
        *,
        tags: Sequence[str] | None = None,
        ids: Sequence[str] | None = None,
        match_all: bool = False,
    ) -> "EvalDataset":
        """按标签 / id 挑子集。

        Args:
            tags (`Sequence[str] | None`): 标签过滤；``None`` 表示不过滤。
            ids (`Sequence[str] | None`): id 过滤；``None`` 表示不过滤。
            match_all (`bool`): 标签过滤是"全命中"还是"任一命中"。

        Returns:
            `EvalDataset`: 新数据集（原对象不变）。
        """
        wanted_ids = set(ids) if ids is not None else None
        wanted_tags = set(tags) if tags else None

        def _keep(case: EvalCase) -> bool:
            if wanted_ids is not None and case.id not in wanted_ids:
                return False
            if wanted_tags is not None:
                hit = wanted_tags & set(case.tags)
                if match_all and hit != wanted_tags:
                    return False
                if not match_all and not hit:
                    return False
            return True

        return self.model_copy(update={"cases": [case for case in self.cases if _keep(case)]})

    def head(self, n: int) -> "EvalDataset":
        """取前 ``n`` 条（小规模冒烟评测用）。

        Args:
            n (`int`): 条数；必须为正。

        Returns:
            `EvalDataset`: 新数据集。

        Raises:
            `ValueError`: ``n <= 0``。
        """
        if n <= 0:
            raise ValueError(f"head 的 n 必须为正，收到 {n}")
        return self.model_copy(update={"cases": self.cases[:n]})

    def stats(self) -> dict[str, Any]:
        """数据集概况（报告与 CLI 用）。

        Returns:
            `dict[str, Any]`: ``case_count`` / ``by_tag`` / ``with_expected`` /
            ``with_tools`` / ``with_citations``。
        """
        by_tag: dict[str, int] = {}
        for case in self.cases:
            for tag in case.tags:
                by_tag[tag] = by_tag.get(tag, 0) + 1
        return {
            "case_count": len(self.cases),
            "by_tag": by_tag,
            "with_expected": sum(1 for case in self.cases if case.expected is not None),
            "with_tools": sum(1 for case in self.cases if case.expected_tools),
            "with_citations": sum(1 for case in self.cases if case.expected_citations),
        }

    def __len__(self) -> int:
        """用例数。

        Returns:
            `int`: 用例数。
        """
        return len(self.cases)

    def __iter__(self) -> Iterator[EvalCase]:  # type: ignore[override]
        """迭代用例。

        Returns:
            `Iterator[EvalCase]`: 用例迭代器。
        """
        return iter(self.cases)

    # ------------------------------------------------------------------
    # JSONL 读写
    # ------------------------------------------------------------------
    @classmethod
    def from_jsonl(cls, path: Path) -> "EvalDataset":
        """从 JSONL 装载。

        容忍两种形态：带 ``type=="dataset"`` 头行的，和一行一个裸用例的。
        **空行会被跳过**（手工编辑过的文件经常留空行），坏行会带着行号抛错
        （静默跳过坏行会让"少了 3 条用例"变成一个没人发现的问题）。

        Args:
            path (`Path`): JSONL 文件路径。

        Returns:
            `EvalDataset`: 数据集。

        Raises:
            `FileNotFoundError`: 文件不存在。
            `ValueError`: 某行不是合法 JSON，或结构不对。
        """
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"评测集文件不存在: {target}")

        name = target.stem
        dataset_tags: list[str] = []
        dataset_meta: dict[str, Any] = {}
        cases: list[EvalCase] = []

        for lineno, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{target}:{lineno} 不是合法 JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{target}:{lineno} 期望一个 JSON 对象，收到 {type(payload).__name__}")

            kind = payload.get("type", _CASE_TYPE)
            if kind == _HEADER_TYPE:
                name = str(payload.get("name") or name)
                dataset_tags = list(payload.get("tags") or [])
                dataset_meta = dict(payload.get("metadata") or {})
                continue

            body = {key: value for key, value in payload.items() if key != "type"}
            if "id" not in body or "input" not in body:
                raise ValueError(f"{target}:{lineno} 用例缺 id / input: {sorted(body)}")
            cases.append(EvalCase.model_validate(body))

        logger.bind(path=str(target), cases=len(cases)).debug("评测集已装载")
        return cls(name=name, cases=cases, tags=dataset_tags, metadata=dataset_meta)

    def to_jsonl(self, path: Path) -> None:
        """写出 JSONL（首行是头）。

        Args:
            path (`Path`): 目标路径；父目录会被创建。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            json.dumps(
                {
                    "type": _HEADER_TYPE,
                    "name": self.name,
                    "tags": self.tags,
                    "metadata": self.metadata,
                },
                ensure_ascii=False,
            ),
        ]
        for case in self.cases:
            payload = case.model_dump(mode="json")
            payload["type"] = _CASE_TYPE
            lines.append(json.dumps(payload, ensure_ascii=False))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.bind(path=str(target), cases=len(self.cases)).debug("评测集已落盘")

    @classmethod
    def from_cases(
        cls,
        cases: Iterable[EvalCase],
        *,
        name: str = "adhoc",
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "EvalDataset":
        """从用例列表构造（便捷入口）。

        Args:
            cases (`Iterable[EvalCase]`): 用例。
            name (`str`): 数据集名。
            tags (`Sequence[str] | None`): 数据集级标签。
            metadata (`dict[str, Any] | None`): 数据集级元信息。

        Returns:
            `EvalDataset`: 数据集。
        """
        return cls(
            name=name,
            cases=list(cases),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
        )

    def to_markdown(self, limit: int = 20) -> str:
        """渲染成 Markdown 表格（人工审阅用例用）。

        Args:
            limit (`int`): 最多打印多少行。

        Returns:
            `str`: Markdown 文本。
        """
        lines = [
            f"# 评测集 {self.name}",
            "",
            f"- 用例数: {len(self.cases)}",
            f"- 标签: {', '.join(self.tags) or '(无)'}",
            f"- 期望工具/引用的用例数: {sum(1 for c in self.cases if c.expected_tools or c.expected_citations)}",
            "",
            "| id | 输入 | 期望 | 标签 | 期望工具 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for case in self.cases[:limit]:
            lines.append(
                "| {id} | {input} | {expected} | {tags} | {tools} |".format(
                    id=case.id,
                    input=_cell(case.input, 60),
                    expected=_cell(case.expected or "", 40),
                    tags=",".join(case.tags),
                    tools=",".join(case.expected_tools),
                ),
            )
        if len(self.cases) > limit:
            lines.append(f"| … | 还有 {len(self.cases) - limit} 条 | | | |")
        return "\n".join(lines)


def _cell(text: str, width: int) -> str:
    """把单元格内容压成单行且不破坏 Markdown 表格。

    Args:
        text (`str`): 原始文本。
        width (`int`): 最大宽度。

    Returns:
        `str`: 处理后的文本。
    """
    collapsed = " ".join(text.split()).replace("|", "\\|")
    return collapsed if len(collapsed) <= width else collapsed[:width] + "…"


def dataset_from_cases(
    cases: Iterable[EvalCase],
    *,
    name: str = "adhoc",
) -> EvalDataset:
    """``EvalDataset.from_cases`` 的函数式别名（契约里没写，但脚本里更好读）。

    Args:
        cases (`Iterable[EvalCase]`): 用例。
        name (`str`): 数据集名。

    Returns:
        `EvalDataset`: 数据集。
    """
    return EvalDataset.from_cases(cases, name=name)


async def synthesize_cases(
    seed: list[EvalCase],
    *,
    n: int,
    rewriter: RewriterFn,
) -> list[EvalCase]:
    """数据合成：把少量种子用例扩成 ``n`` 条（契约 §3.20）。

    **契约偏离（必须记住）**：契约 §3.20 把它写成同步的
    ``def synthesize_cases(...) -> list[EvalCase]``，但同一个签名里的
    ``rewriter`` 是 ``Callable[[EvalCase], Awaitable[EvalCase]]`` —— 同步函数
    没法 ``await`` 一个异步改写器，除非在里面 ``asyncio.run()``，
    而契约 §七.2 明令禁止库代码出现 ``asyncio.run()``。两者不可兼得，
    因此这里把它实现成 ``async def``。以真实 API 为准，记在偏离清单里。

    语义：

    - 对 ``seed`` 做**轮转**（``seed[i % len(seed)]``）直到产出 ``n`` 条，
      这样即使 ``n`` 远大于种子数也能均匀扩；
    - 第 ``k`` 条新用例的 id 是 ``f"{seed_case.id}-syn{k}"``，**同一次调用的
      内 k 唯一**（跨次调用可能重号，调用方自行加前缀）；
    - ``rewriter`` 抛异常时**跳过该条并继续**（合成是"尽力而为"，
      一条坏样本不该让整批白干），跳过的记录会打 warning；
    - 产出条数**可能少于 n**（全部失败时为空列表）—— 返回值就是真实条数，
      调用方必须自己看 ``len()``。

    Args:
        seed (`list[EvalCase]`): 种子用例。
        n (`int`): 期望产出条数；必须为正。
        rewriter (`RewriterFn`): 异步改写器，通常内部调一次 LLM。

    Returns:
        `list[EvalCase]`: 合成出的用例（可能少于 ``n``）。

    Raises:
        `ValueError`: ``seed`` 为空，或 ``n <= 0``。
    """
    if n <= 0:
        raise ValueError(f"synthesize_cases 的 n 必须为正，收到 {n}")
    if not seed:
        raise ValueError("synthesize_cases 的 seed 不能为空（没有种子就没有合成）")

    produced: list[EvalCase] = []
    failures = 0
    for index in range(n):
        source = seed[index % len(seed)]
        try:
            rewritten = await rewriter(source)
        except asyncio.CancelledError:  # pragma: no cover - 取消必须继续往上抛
            raise
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            failures += 1
            logger.warning("synthesize_cases: 第 {} 条改写失败（已跳过）: {}", index, exc)
            continue
        if not isinstance(rewritten, EvalCase):
            failures += 1
            logger.warning(
                "synthesize_cases: 第 {} 条改写器返回了 {}，不是 EvalCase（已跳过）",
                index,
                type(rewritten).__name__,
            )
            continue
        produced.append(
            rewritten.model_copy(
                update={
                    "id": f"{source.id}-syn{index}",
                    "tags": sorted(set(rewritten.tags) | {"synthetic"}),
                    "metadata": {
                        **rewritten.metadata,
                        "synthesized_from": source.id,
                    },
                },
            ),
        )

    if failures:
        logger.warning(
            "synthesize_cases: {}/{} 条失败，实际产出 {} 条",
            failures,
            n,
            len(produced),
        )
    return produced
