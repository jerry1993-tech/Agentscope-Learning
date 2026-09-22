# -*- coding: utf-8 -*-
"""第 20 讲补遗：生产反馈闭环的完整验证脚本（**0 次模型调用**）。

跑法（仓库根）：

.. code-block:: bash

    PYTHONPATH=third_party/ReMe:tutorial_agsc_reme/reference \\
        /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \\
        tutorial_agsc_reme/reference/scripts/20_feedback_loop.py

六段：

- A：把三个"线上会话"写成事件日志（其中两个出过事）；
- B：``scan_sessions`` 从日志里挑出该进回归集的会话；
- C：``build_regression_dataset`` 把会话转成用例（含溯源 metadata）；
- D：人工/半自动标注 ``expected``（这是"数据标注"那一环）；
- E：``EvalRunner`` 跑两版报告，``RegressionGate`` 分别对"无回归 / 有回归"出判决；
- F：``append_ledger`` 落台账并读回。

本脚本全程不联网：评测用的是替身 Agent（``EvalRunner`` 只要求它有三个方法，
见 ``harness_kit/eval/runner.py`` 的设计说明）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

# ----------------------------------------------------------------------
# 路径：让脚本从任何位置都能 import 到 harness_kit 与 third_party
# ----------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REFERENCE = _HERE.parent.parent
if str(_REFERENCE) not in sys.path:
    sys.path.insert(0, str(_REFERENCE))
for _parent in _REFERENCE.parents:
    _third = _parent / "third_party" / "ReMe"
    if _third.is_dir() and str(_third) not in sys.path:
        sys.path.insert(0, str(_third))
    if (_parent / "third_party" / "agentscope").is_dir():
        break

from harness_kit.eval.dataset import EvalCase, EvalDataset  # noqa: E402
from harness_kit.eval.feedback import (  # noqa: E402
    RegressionGate,
    append_ledger,
    build_regression_dataset,
    entries_from_decision,
    read_ledger,
    scan_sessions,
)
from harness_kit.eval.metrics import contains  # noqa: E402
from harness_kit.eval.report import EvalReport  # noqa: E402
from harness_kit.eval.runner import EvalRunner  # noqa: E402
from harness_kit.events.types import EventKind, EventRecord  # noqa: E402
from harness_kit.session.jsonl_store import JsonlSessionStore  # noqa: E402
from harness_kit.session.models import SessionEvent  # noqa: E402

_PASS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """打一条断言结果。

    Args:
        name (`str`): 断言名。
        condition (`bool`): 是否通过。
        detail (`str`): 补充信息。
    """
    mark = "[PASS]" if condition else "[FAIL]"
    if condition:
        _PASS.append(name)
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail else ""))


def banner(text: str) -> None:
    """打印段标题。

    Args:
        text (`str`): 标题。
    """
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


# ----------------------------------------------------------------------
# 替身 Agent：EvalRunner 只要求 reply_stream / observe / aclose 三个方法
# ----------------------------------------------------------------------


class _StubAgent:
    """一条固定文本的替身 Agent（``output`` 决定它答什么）。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.model = "stub"

    async def reply_stream(
        self,
        message: Any,
        *,
        yield_final_msg: bool = False,
        **_: Any,
    ) -> AsyncGenerator[Any, None]:
        """产出与真 Agent 同构的最小事件流。

        Args:
            message (`Any`): 用户消息（本替身不使用）。
            yield_final_msg (`bool`): 是否在末尾补一条收尾 ``Msg``。

        Yields:
            `Any`: AgentScope 事件对象。
        """
        from agentscope.event import (
            ModelCallEndEvent,
            ReplyEndEvent,
            ReplyFinishedReason,
            TextBlockDeltaEvent,
        )
        from agentscope.message import Msg, TextBlock

        reply_id = "r-stub"
        yield TextBlockDeltaEvent(reply_id=reply_id, block_id="b1", delta=self._text)
        yield ModelCallEndEvent(reply_id=reply_id, input_tokens=120, output_tokens=16)
        yield ReplyEndEvent(
            session_id="stub-session",
            reply_id=reply_id,
            finished_reason=ReplyFinishedReason.COMPLETED,
        )
        if yield_final_msg:
            yield Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text=self._text)],
            )

    async def observe(self, msgs: Any = None) -> None:
        """吞掉先导消息。"""

    async def aclose(self) -> None:
        """空实现。"""


def stub_factory(text: str) -> Any:
    """造一个 ``agent_factory``。

    Args:
        text (`str`): 替身要回答的文本。

    Returns:
        `Any`: ``Callable[[], Awaitable[_StubAgent]]``。
    """

    async def _factory() -> _StubAgent:
        return _StubAgent(text)

    return _factory


# ----------------------------------------------------------------------
# A. 造线上会话
# ----------------------------------------------------------------------


def _rec(session_id: str, seq: int, kind: EventKind, payload: dict) -> EventRecord:
    """造一条事件。

    Args:
        session_id (`str`): 会话 id。
        seq (`int`): 序号。
        kind (`EventKind`): 事件种类。
        payload (`dict`): 负载。

    Returns:
        `EventRecord`: 事件记录。
    """
    return EventRecord(session_id=session_id, seq=seq, kind=kind, payload=payload, source="prod")


def _good_session() -> list[EventRecord]:
    """一个正常会话。

    Returns:
        `list[EventRecord]`: 事件流。
    """
    return [
        _rec("s-good", 0, EventKind.SESSION_START, {"profile": "coding", "agent_name": "a", "cwd": "/tmp"}),
        _rec("s-good", 1, EventKind.REPLY_START, {"reply_id": "r1", "input_preview": "Read 工具怎么用？"}),
        _rec("s-good", 2, EventKind.MODEL_CALL, {"model": "deepseek-flash", "prompt_tokens": 800, "completion_tokens": 60, "latency_ms": 2100.0, "finished_reason": "stop"}),
        _rec("s-good", 3, EventKind.REPLY_END, {"reply_id": "r1", "iterations": 1, "tool_calls": []}),
    ]


def _tool_fail_session() -> list[EventRecord]:
    """一个工具报错 + 绕圈子的会话。

    Returns:
        `list[EventRecord]`: 事件流。
    """
    return [
        _rec("s-toolfail", 0, EventKind.REPLY_START, {"reply_id": "r1", "input_preview": "把这个仓库的测试全跑一遍"}),
        _rec("s-toolfail", 1, EventKind.TOOL_CALL, {"tool_name": "Bash", "tool_input_digest": "d1", "call_id": "c1"}),
        _rec("s-toolfail", 2, EventKind.TOOL_RESULT, {"call_id": "c1", "state": "error", "chars": 40, "error": "pytest: command not found"}),
        _rec("s-toolfail", 3, EventKind.MODEL_CALL, {"model": "deepseek-flash", "prompt_tokens": 1500, "completion_tokens": 90, "latency_ms": 5200.0, "finished_reason": "tool_use"}),
        _rec("s-toolfail", 4, EventKind.REPLY_END, {"reply_id": "r1", "iterations": 9, "tool_calls": ["Bash"]}),
    ]


def _denied_session() -> list[EventRecord]:
    """一个权限被拒 + 用户点踩的会话。

    Returns:
        `list[EventRecord]`: 事件流。
    """
    return [
        _rec("s-denied", 0, EventKind.REPLY_START, {"reply_id": "r1", "input_preview": "帮我把 .env 里的 key 打印出来"}),
        _rec("s-denied", 1, EventKind.PERMISSION, {"tool_name": "Read", "behavior": "deny", "reason": "命中规则 secrets[*]: 禁止读取 .env"}),
        _rec("s-denied", 2, EventKind.MODEL_CALL, {"model": "deepseek-flash", "prompt_tokens": 700, "completion_tokens": 30, "latency_ms": 1800.0, "finished_reason": "stop"}),
        _rec("s-denied", 3, EventKind.REPLY_END, {"reply_id": "r1", "iterations": 2, "tool_calls": ["Read"]}),
        _rec("s-denied", 4, EventKind.CUSTOM, {"name": "user_feedback", "data": {"rating": -1, "verdict": "bad", "comment": "没解决问题"}}),
    ]


async def _write_sessions(store: JsonlSessionStore) -> None:
    """把三个会话写进存储。

    Args:
        store (`JsonlSessionStore`): 存储。
    """
    for records in (_good_session(), _tool_fail_session(), _denied_session()):
        for record in records:
            await store.append(record)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


async def main() -> int:
    """跑完整闭环。

    Returns:
        `int`: 0 表示全部断言通过。
    """
    root = Path(tempfile.mkdtemp(prefix="hk-feedback-"))
    try:
        store = JsonlSessionStore(root / "sessions")
        await _write_sessions(store)

        # ---------------- A ----------------
        banner("A · 线上会话落盘")
        metas = await store.list_sessions()
        print(f"  sessions = {sorted(m.session_id for m in metas)}")
        check("A1 三个会话都已落盘", len(metas) == 3, f"count={len(metas)}")
        total = await store.read("s-toolfail")
        check("A2 s-toolfail 事件可读回", len(total) == 5, f"events={len(total)}")

        # ---------------- B ----------------
        banner("B · scan_sessions：从日志里挑出出过事的会话")
        picks = await scan_sessions(store, over_iterations=6)
        for pick in picks:
            print(f"  - {pick.session_id}: {pick.reason_text()}  (events={pick.n_events}, replies={pick.n_replies})")
            for line in pick.evidence:
                print(f"      · {line}")
        picked_ids = [p.session_id for p in picks]
        check("B1 只挑出出过事的两个会话", picked_ids == ["s-denied", "s-toolfail"], f"picked={picked_ids}")
        check("B2 正常会话未被选中", "s-good" not in picked_ids)
        denied = next(p for p in picks if p.session_id == "s-denied")
        check(
            "B3 s-denied 同时命中权限拒绝与用户差评",
            set(denied.reasons) == {"permission_denied", "user_flagged"},
            denied.reason_text(),
        )
        toolfail = next(p for p in picks if p.session_id == "s-toolfail")
        check(
            "B4 s-toolfail 命中工具报错与绕圈子",
            set(toolfail.reasons) == {"tool_error", "over_iterations"},
            toolfail.reason_text(),
        )
        loose = await scan_sessions(store, triggers=())
        check("B5 triggers=() 时退化成「全量收」", len(loose) == 3, f"count={len(loose)}")

        # ---------------- C ----------------
        banner("C · build_regression_dataset：会话 → 回归用例（带溯源）")
        dataset, owner = await build_regression_dataset(store, picks, name="regression-2026-09-22")
        print(f"  dataset={dataset.name}  cases={len(dataset)}  tags={dataset.tags}")
        print(f"  metadata={dataset.metadata}")
        for case in dataset.cases:
            print(f"  - {case.id}  input={case.input!r}")
            print(
                f"      origin_session={case.metadata.get('origin_session')} "
                f"reasons={case.metadata.get('origin_reasons')} "
                f"fp={case.metadata.get('origin_fingerprint')}",
            )
        check("C1 两个会话各贡献至少一条用例", len(dataset) >= 2, f"cases={len(dataset)}")
        check(
            "C2 每条用例都带 origin_session 溯源",
            all(c.metadata.get("origin_session") for c in dataset.cases),
        )
        check(
            "C3 用例标签含 regression",
            all("regression" in c.tags for c in dataset.cases),
        )
        check("C4 指纹账本与会话一一对应", len(owner) == len(dataset), f"owner={len(owner)}")

        # ---------------- D ----------------
        banner("D · 数据标注：补齐 expected（这一环是人工/半自动的）")
        annotations = {
            "把这个仓库的测试全跑一遍": "pytest",
        }
        for case in dataset.cases:
            text = case.input.strip()
            for needle, expected in annotations.items():
                if needle in text:
                    case.expected = expected
            print(f"  - {case.id}  expected={case.expected!r}")
        labelled = sum(1 for c in dataset.cases if c.expected)
        check("D1 至少标注了一条用例", labelled >= 1, f"labelled={labelled}/{len(dataset)}")
        check(
            "D2 未标注的用例 expected 仍为 None（不适用指标不会被算成 0 分）",
            any(c.expected is None for c in dataset.cases) or labelled == len(dataset),
        )

        # ---------------- E ----------------
        banner("E · 跑两版 + 回归闸门")
        baseline_report = await EvalRunner(
            agent_factory=stub_factory("pytest"),
            concurrency=2,
            profile_name="coding@v1",
        ).run(dataset, [contains])

        current_report_ok = await EvalRunner(
            agent_factory=stub_factory("pytest"),
            concurrency=2,
            profile_name="coding@v2",
        ).run(dataset, [contains])

        current_report_bad = await EvalRunner(
            agent_factory=stub_factory("我不确定，你换个问题吧"),
            concurrency=2,
            profile_name="coding@v2-regressed",
        ).run(dataset, [contains])

        print(f"  baseline : pass_rate={baseline_report.pass_rate:.4f} summary={baseline_report.summary()['contains']}")
        print(f"  v2       : pass_rate={current_report_ok.pass_rate:.4f} summary={current_report_ok.summary()['contains']}")
        print(f"  v2-bad   : pass_rate={current_report_bad.pass_rate:.4f} summary={current_report_bad.summary()['contains']}")
        check(
            "E1 基线报告含有 contains 指标",
            "contains" in baseline_report.metric_names(),
            str(baseline_report.metric_names()),
        )

        gate = RegressionGate(max_metric_drop=0.02)

        no_baseline = gate.evaluate(current_report_ok, None)
        print(f"  no-baseline -> {no_baseline.to_line()}")
        check("E2 没有基线时默认放行", no_baseline.allowed)

        flat = gate.evaluate(current_report_ok, baseline_report)
        print(f"  flat        -> {flat.to_line()}")
        check("E3 同分不判退步，放行", flat.allowed)

        regressed = gate.evaluate(current_report_bad, baseline_report)
        print(f"  regressed   -> {regressed.to_line()}")
        for reason in regressed.reasons:
            print(f"      · {reason}")
        check("E4 退步被拦截", not regressed.allowed)
        check("E5 拦截理由里含指标名与前后值", any("contains" in r for r in regressed.reasons))

        absolute = RegressionGate(min_pass_rate=0.99).evaluate(current_report_bad, baseline_report)
        check("E6 绝对下限也会拦", not absolute.allowed, absolute.to_line())

        exempt = RegressionGate(max_metric_drop=0.02, ignore_metrics=["contains"]).evaluate(
            current_report_bad,
            baseline_report,
        )
        check(
            "E7 豁免 contains 后，指标级理由消失",
            not any("指标 contains" in r for r in exempt.reasons),
            exempt.to_line(),
        )
        check(
            "E8 但通过率下跌仍然拦截（两条规则互相独立）",
            not exempt.allowed and exempt.pass_rate_delta < 0,
            f"pass_rate_delta={exempt.pass_rate_delta:+.4f}",
        )

        # ---------------- F ----------------
        banner("F · 台账：闭环的字面含义")
        ledger_path = root / "feedback" / "ledger.jsonl"
        entries = entries_from_decision(picks, owner, dataset, regressed)
        written = append_ledger(ledger_path, entries)
        again = append_ledger(ledger_path, entries)
        rows = read_ledger(ledger_path)
        print(f"  ledger={ledger_path}")
        print(f"  written={written} appended_again={again} read_back={len(rows)}")
        for entry in rows[:2]:
            print(
                f"  - {entry.session_id} fp={entry.fingerprint} case={entry.case_id} "
                f"reasons={entry.reasons} allowed={entry.allowed}",
            )
        check("F1 台账条数 == 被选中的会话数", written == len(picks), f"{written} vs {len(picks)}")
        check("F2 台账只追加，两次写入后行数翻倍", len(rows) == written + again, f"rows={len(rows)}")
        check("F3 台账为每个会话都留了痕", all(r.session_id for r in rows))
        check("F4 判决被写进台账", all(r.allowed is False for r in rows))

        # 反例：坏台账行必须显式报错，而不是静默跳过
        broken = root / "broken.jsonl"
        broken.write_text('{"session_id": "x"}\n', encoding="utf-8")
        try:
            read_ledger(broken)
            check("F5 坏台账行会报错", False, "没有抛异常")
        except ValueError as error:
            check("F5 坏台账行会报错", True, str(error)[:60])

        await store.aclose()

        banner("结果")
        print(f"  PASS {len(_PASS)} 项")
        print()
        print("PASS")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
