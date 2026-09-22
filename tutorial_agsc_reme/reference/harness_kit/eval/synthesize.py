# -*- coding: utf-8 -*-
"""从会话事件流里反推可复用的评测样本（第 20 讲）。

**为什么放在这一层**：真实生产里的评测集不是凭空写出来的，而是从
"线上跑过的会话"里挑出来的 —— 一个跑了三个月的助手，事件流里躺着几千个
真实回合，把它们变成回归集比手写 20 条用例有用得多。这正是
`harness_kit.session.replay`（第 9 讲）的直接下游。

**能从事件流里拿到什么、拿不到什么**（这一条决定了本模块的形状）：

- 拿得到：``REPLY_START.payload["input_preview"]``（输入预览）、
  ``REPLY_END.payload["tool_calls"]``（本回合真实用过的工具）、
  ``MODEL_CALL``（token / 轮数）、``MEMORY_HIT``（记忆命中）、
  ``PERMISSION``（权限决策）。字段表见
  ``harness_kit/events/types.py:46`` 的 ``PAYLOAD_FIELDS``。
- **拿不到**：Agent 的**输出文本**。事件流里根本没有这一列字段 ——
  ``PAYLOAD_FIELDS[REPLY_END]`` 只有 ``reply_id`` / ``iterations`` /
  ``tool_calls``。想拿到对话文本只有一条路：
  :meth:`~harness_kit.session.replay.SessionReplayer.context_from_snapshot`
  （``harness_kit/session/replay.py:518``），而它给的是**快照那一刻**的整段
  ``AgentState.context``，不是"每回合各一段"。

所以本模块的默认产出是**行为回归集**：``input`` 有真值、``expected_tools``
有真值、``expected`` 为空。想连输出文本一起评，就打开
``expected_from_snapshot=True`` —— 它会尝试把快照里的 assistant 消息按顺序
与回合配对，**只在数量严格相等时才配对**，对不上就全部留空并打一条
warning（宁可没有真值，也不要错位的真值）。

Example:
    >>> import asyncio
    >>> from pathlib import Path
    >>> from harness_kit.session.store import JsonlSessionStore
    >>> from harness_kit.eval.synthesize import synthesize_from_session  # doctest: +SKIP
    >>> store = JsonlSessionStore(Path("/tmp/harness/sessions"))         # doctest: +SKIP
    >>> dataset = asyncio.run(synthesize_from_session(store, "s-1"))     # doctest: +SKIP
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from harness_kit.eval.dataset import EvalCase, EvalDataset, RewriterFn
from harness_kit.eval.dataset import synthesize_cases as synthesize_cases
from harness_kit.events.types import EventKind, EventRecord
from harness_kit.session.replay import ReplayTurn, SessionReplayer

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from harness_kit.session.store import SessionStoreBase

__all__ = [
    "TurnSample",
    "extract_turn_samples",
    "make_rewriter",
    "synthesize_cases",
    "synthesize_from_session",
]

_INPUT_PREVIEW_LIMIT = 500
"""``input_preview`` 的截断长度，与 ``harness_kit/session/models.py:56`` 保持一致。"""

_TRUNCATION_SUFFIX = "..."
"""``harness_kit/session/models.py:82`` 给超长输入的截断后缀。"""


class TurnSample(BaseModel):
    """一个回合里能被事件流证实的全部事实。

    Attributes:
        reply_id (`str`): 回合 id。
        start_seq (`int`): ``REPLY_START`` 的 ``seq``。
        input (`str`): 输入预览（可能被截断，见 :attr:`truncated`）。
        truncated (`bool`): 输入是否被事件层截断过。
        tool_calls (`list[str]`): 本回合真实调用过的工具名（保序、含重复）。
        iterations (`int`): 循环轮数。
        model_calls (`int`): 模型调用次数。
        prompt_tokens (`int`): 输入 token 合计。
        completion_tokens (`int`): 输出 token 合计。
        memory_hits (`int`): 记忆命中条数。
        permission_denials (`int`): 被拒的权限决策数。
        closed (`bool`): 是否收到过 ``REPLY_END``。
    """

    model_config = ConfigDict(extra="forbid")

    reply_id: str
    """回合 id。"""

    start_seq: int = Field(ge=0)
    """``REPLY_START`` 的 ``seq``。"""

    input: str = ""
    """输入预览。"""

    truncated: bool = False
    """输入是否被截断（截断的输入不适合做 ``exact_match`` 用例）。"""

    tool_calls: list[str] = Field(default_factory=list)
    """本回合调用过的工具名。"""

    iterations: int = Field(default=0, ge=0)
    """循环轮数。"""

    model_calls: int = Field(default=0, ge=0)
    """模型调用次数。"""

    prompt_tokens: int = Field(default=0, ge=0)
    """输入 token。"""

    completion_tokens: int = Field(default=0, ge=0)
    """输出 token。"""

    memory_hits: int = Field(default=0, ge=0)
    """记忆命中条数。"""

    permission_denials: int = Field(default=0, ge=0)
    """被拒的权限决策条数。"""

    closed: bool = False
    """是否已收尾。"""

    @property
    def unique_tools(self) -> list[str]:
        """保序去重后的工具名。

        Returns:
            `list[str]`: 去重工具名。
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for name in self.tool_calls:
            if name in seen:
                continue
            seen.add(name)
            ordered.append(name)
        return ordered

    def tags(self) -> list[str]:
        """由行为自动派生标签（用于 :meth:`EvalDataset.filter` 挑子集）。

        规则：永远带 ``from_session``；用到工具就带 ``tool_use`` 和
        每个工具名；命中记忆带 ``memory``；多轮带 ``multi_turn``；
        有被拒的权限决策带 ``permission_denied``；输入被截断带
        ``truncated_input``。

        Returns:
            `list[str]`: 排序去重后的标签。
        """
        tags: set[str] = {"from_session"}
        if self.tool_calls:
            tags.add("tool_use")
            tags.update(self.unique_tools)
        if self.memory_hits:
            tags.add("memory")
        if self.iterations > 1:
            tags.add("multi_turn")
        if self.permission_denials:
            tags.add("permission_denied")
        if self.truncated:
            tags.add("truncated_input")
        return sorted(tags)


def _payload_int(payload: dict[str, Any], key: str) -> int:
    """从 payload 里取一个非负整数（坏值当 0）。

    Args:
        payload (`dict[str, Any]`): 事件 payload。
        key (`str`): 字段名。

    Returns:
        `int`: 值；缺失或非法时为 ``0``。
    """
    raw = payload.get(key, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def extract_turn_samples(records: Sequence[EventRecord]) -> list[TurnSample]:
    """把事件流里的回合抽成 :class:`TurnSample`（纯函数，可单测）。

    回合边界复用 :meth:`SessionReplayer.turns_from_records`
    （``harness_kit/session/replay.py:373``），不在本模块重切一遍 ——
    切回合的口径只能有一处，否则第 9 讲和第 20 讲会给出不同的答案。
    轮数、token、工具、记忆命中、权限决策都已经在 ``ReplayTurn`` 里按回合归好类了
    （``harness_kit/session/replay.py:139``），这里只做"搬运 + 计数"，
    **不重新扫一遍事件** —— 两处口径必然漂移。

    Args:
        records (`Sequence[EventRecord]`): 同一会话的事件记录（``seq`` 升序）。

    Returns:
        `list[TurnSample]`: 回合样本（保序）。
    """
    turns: list[ReplayTurn] = SessionReplayer.turns_from_records(list(records))
    if not turns:
        return []

    samples: list[TurnSample] = []
    for turn in turns:
        raw_input = turn.input_preview or ""
        denied = sum(
            1
            for decision in turn.permissions
            if str(decision.get("behavior", "")).lower() in {"deny", "denied", "reject"}
        )
        samples.append(
            TurnSample(
                reply_id=turn.reply_id,
                start_seq=turn.start_seq,
                input=raw_input,
                truncated=raw_input.endswith(_TRUNCATION_SUFFIX)
                and len(raw_input) == _INPUT_PREVIEW_LIMIT + len(_TRUNCATION_SUFFIX),
                tool_calls=list(turn.tool_calls),
                iterations=turn.iterations,
                model_calls=turn.model_calls,
                prompt_tokens=turn.token_usage.input_tokens,
                completion_tokens=turn.token_usage.output_tokens,
                memory_hits=len(turn.memory_hits),
                permission_denials=denied,
                closed=turn.closed,
            ),
        )
    return samples


def _assistant_texts(messages: Iterable[Any]) -> list[str]:
    """从快照消息里按顺序取出 assistant 文本。

    Args:
        messages (`Iterable[Any]`): AgentScope ``Msg`` 列表。

    Returns:
        `list[str]`: 非空 assistant 文本（保序）。
    """
    texts: list[str] = []
    for message in messages:
        role = str(getattr(message, "role", "") or "")
        if role != "assistant":
            continue
        getter = getattr(message, "get_text_content", None)
        text = getter() if callable(getter) else ""
        if text:
            texts.append(text)
    return texts


async def synthesize_from_session(
    store: "SessionStoreBase",
    session_id: str,
    *,
    name: str | None = None,
    tags: Sequence[str] | None = None,
    require_closed: bool = True,
    min_iterations: int = 0,
    skip_truncated: bool = False,
    expected_from_snapshot: bool = True,
    metadata: dict[str, Any] | None = None,
) -> EvalDataset:
    """从一个会话的事件流里生成评测集。

    Args:
        store (`SessionStoreBase`): 会话存储。
        session_id (`str`): 会话 id。
        name (`str | None`): 数据集名；``None`` 用 ``f"session-{session_id}"``。
        tags (`Sequence[str] | None`): 追加的数据集级标签。
        require_closed (`bool`): 只收有 ``REPLY_END`` 的回合
            （没收尾的回合没有 ``iterations`` 真值）。
        min_iterations (`int`): 只要迭代轮数不低于它的回合。
        skip_truncated (`bool`): 丢掉输入被截断的回合
            （截断输入做不了严格匹配，但**行为**回归仍然有效，所以默认不丢）。
        expected_from_snapshot (`bool`): 是否尝试从快照配对输出文本。
        metadata (`dict[str, Any] | None`): 追加的数据集级元信息。

    Returns:
        `EvalDataset`: 评测集。可能为空（会话里没有合格回合）—— 空集不是错误，
        调用方看 ``len(dataset)`` 决定下一步。

    Raises:
        `SessionNotFoundError`: 会话不存在（由 store 抛出）。
        `ValueError`: ``min_iterations`` 为负。
    """
    if min_iterations < 0:
        raise ValueError(f"min_iterations 不能为负，收到 {min_iterations}")

    records = [event.record for event in await store.read(session_id)]
    samples = extract_turn_samples(records)

    selected = [
        sample
        for sample in samples
        if (sample.closed or not require_closed)
        and sample.iterations >= min_iterations
        and (not skip_truncated or not sample.truncated)
        and sample.input.strip()
    ]
    dropped = len(samples) - len(selected)

    expected_texts: list[str | None] = [None] * len(selected)
    pairing_note: str | None = None
    if expected_from_snapshot and selected:
        replayer = SessionReplayer(store)
        messages = await replayer.context_from_snapshot(session_id)
        texts = _assistant_texts(messages)
        if len(texts) == len(selected):
            expected_texts = list(texts)
            pairing_note = "expected 来自快照 assistant 消息，按顺序与回合一一配对"
        else:
            pairing_note = (
                f"快照里有 {len(texts)} 条 assistant 消息，但合格回合有 {len(selected)} 条，"
                "数量不等，放弃配对（宁可 expected 为空也不要错位的真值）"
            )
            logger.warning("synthesize_from_session({}): {}", session_id, pairing_note)

    cases: list[EvalCase] = []
    for index, sample in enumerate(selected):
        expected = expected_texts[index]
        case_metadata: dict[str, Any] = {
            "session_id": session_id,
            "reply_id": sample.reply_id,
            "start_seq": sample.start_seq,
            "iterations": sample.iterations,
            "model_calls": sample.model_calls,
            "prompt_tokens": sample.prompt_tokens,
            "completion_tokens": sample.completion_tokens,
            "memory_hits": sample.memory_hits,
            "permission_denials": sample.permission_denials,
            "truncated_input": sample.truncated,
        }
        if sample.memory_hits:
            case_metadata["require_citation"] = True
        cases.append(
            EvalCase(
                id=f"{session_id}-t{sample.start_seq}",
                input=sample.input,
                expected=expected,
                tags=sample.tags(),
                metadata=case_metadata,
                expected_tools=sample.unique_tools,
                expected_citations=[],
            ),
        )

    dataset_metadata: dict[str, Any] = {
        "source_session": session_id,
        "source_turns": len(samples),
        "selected_turns": len(selected),
        "dropped_turns": dropped,
        "total_prompt_tokens": sum(s.prompt_tokens for s in selected),
        "total_completion_tokens": sum(s.completion_tokens for s in selected),
    }
    if pairing_note:
        dataset_metadata["expected_pairing"] = pairing_note
    dataset_metadata.update(metadata or {})

    logger.bind(session_id=session_id, cases=len(cases), dropped=dropped).info(
        "从会话合成评测集完成",
    )
    return EvalDataset(
        name=name or f"session-{session_id}",
        cases=cases,
        tags=sorted({"synthetic", "from_session", *(tags or [])}),
        metadata=dataset_metadata,
    )


def _dump_rewrite_prompt(case: EvalCase) -> str:
    """构造"改写用例"的提示词（模块级函数，方便被测试替换）。

    Args:
        case (`EvalCase`): 原始用例。

    Returns:
        `str`: 提示词。
    """
    payload = {
        "input": case.input,
        "expected": case.expected,
        "expected_tools": case.expected_tools,
    }
    return (
        "下面是一条 Agent 评测用例。请在不改变考察点的前提下，改写 input 的措辞"
        "（换一种问法、换一个具体例子），保持难度相当。\n"
        "只输出一个 JSON 对象，字段为 input（字符串）与 expected（字符串或 null），"
        "不要输出任何其他文字，也不要加 Markdown 代码块。\n\n"
        f"原用例：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_case_json(text: str) -> dict[str, Any]:
    """从模型回复里抠出一个 JSON 对象。

    模型经常把 JSON 包在 ```` ```json ```` 里，或者在前面加一句
    "好的，这是改写后的用例："。所以策略是：先整体试 ``json.loads``，
    失败就取**第一个 ``{`` 到最后一个 ``}``** 之间的子串再试。
    都失败就抛 :class:`ValueError`（由 ``synthesize_cases`` 捕获并跳过该条）。

    Args:
        text (`str`): 模型回复。

    Returns:
        `dict[str, Any]`: 解析出的对象。

    Raises:
        `ValueError`: 整段和花括号子串都不是合法 JSON 对象。
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        # 去掉围栏：```json\n{...}\n```
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        candidate = candidate.rsplit("```", 1)[0].strip()

    for attempt in (candidate, _braced(candidate)):
        if not attempt:
            continue
        try:
            payload = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError(f"模型回复里找不到合法的 JSON 对象: {text[:200]!r}")


def _braced(text: str) -> str:
    """取第一个 ``{`` 到最后一个 ``}`` 之间的子串。

    Args:
        text (`str`): 原始文本。

    Returns:
        `str`: 子串；找不到花括号时返回空串。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start : end + 1]


def make_rewriter(model: Any) -> RewriterFn:
    """造一个"用 LLM 改写用例"的 :data:`RewriterFn`（配合 ``synthesize_cases``）。

    这里用的是 AgentScope 的 ``ChatModelBase`` 扩展点，**不是**自己写模型层：
    任何满足 ``await model(messages) -> ChatResponse`` 的对象都能传进来
    （``third_party/agentscope/src/agentscope/model/_base.py:182`` 是
    ``__call__``，``:293`` 是抽象 ``_call_api``）。

    行为约定：

    - 模型返回的 JSON 解析失败 → 抛 :class:`ValueError`，
      由 :func:`~harness_kit.eval.dataset.synthesize_cases` 捕获并跳过该条；
    - ``expected_tools`` **原样保留**（改写措辞不该改变该不该调工具，
      真要改工具期望得人工来）；
    - 新用例的 ``metadata`` 里记 ``rewritten_by`` = 模型名。

    Args:
        model (`Any`): AgentScope 的 ChatModel 实例。

    Returns:
        `RewriterFn`: 异步改写器。

    Raises:
        `ValueError`: ``model`` 不是可调用的模型对象。
    """
    if not callable(model):
        raise ValueError(f"make_rewriter 需要一个可调用的 ChatModel，收到 {type(model).__name__}")

    model_name = ""
    for attr in ("model_name", "model", "name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            model_name = value
            break

    async def _rewrite(case: EvalCase) -> EvalCase:
        from agentscope.message import Msg, TextBlock

        from harness_kit.eval.metrics import _collect_text

        prompt = _dump_rewrite_prompt(case)
        response = await model(
            [Msg(name="user", role="user", content=[TextBlock(type="text", text=prompt)])],
        )
        text = _collect_text(response)
        if not text.strip():
            raise ValueError(f"改写模型没有返回文本（{type(response).__name__}）")
        payload = _parse_case_json(text)
        if not isinstance(payload, dict) or "input" not in payload:
            raise ValueError(f"改写模型返回的不是合法用例 JSON: {text[:200]}")
        new_input = str(payload["input"]).strip()
        if not new_input:
            raise ValueError("改写模型返回了空的 input")
        return case.model_copy(
            update={
                "input": new_input,
                "metadata": {
                    **case.metadata,
                    "rewritten_by": model_name or type(model).__name__,
                    "rewrite_source_input": case.input,
                },
            },
        )

    return _rewrite
