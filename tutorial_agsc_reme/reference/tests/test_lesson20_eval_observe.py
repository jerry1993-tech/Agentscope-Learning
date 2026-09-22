# -*- coding: utf-8 -*-
"""第 20 讲的 pytest：评测引擎 / 可观测 / 服务化 / Profile / Demo / 反馈闭环。

八条纪律（延续第 15~19 讲，本讲的重点是"**评测可复现、观测不炸主流程**"）：

1. **0 次 LLM 调用**。评测引擎的语义（并发、超时、单条失败不中断、
   指标"不适用"是 -1.0 而不是 0.0）与模型无关：用替身 Agent 就能全部钉住。
   真实模型的端到端在 ``scripts/20_eval_observe_service.py --live``
   （实测 2 条用例 4 次调用）。
2. **服务层用 ASGI TestClient，不 bind 端口**。契约铁律是"不要起常驻服务"；
   ``TestClient`` 直接把请求喂进 ASGI app，``uvicorn`` 一次都不启动。
3. **可观测层必须"没有 collector 也能跑"**。测试里**不**指定
   ``exporter="otlp"``：本环境**装了** ``opentelemetry-sdk``，
   指定 otlp 会让 ``BatchSpanProcessor`` 去连 ``localhost:4318``，
   然后以 1s/2s/4s 退避重试，测试既慢又脏。验证 OTel 通道改用
   ``InMemorySpanExporter`` 注入（``exporter="custom"``）：完全离线。
4. **每个测试自己建临时目录，绝不共享**。ReMe 的 ``Application._start()``
   会建 ``asyncio.Lock``，而 ``asyncio_default_fixture_loop_scope = "function"``
   意味着每个测试一个事件循环。
5. **回归测试钉住已经踩过的坑**：
   ``test_chat_request_model_must_live_at_module_level`` 对应"请求模型放进
   工厂函数内部会让 FastAPI 把请求体当查询参数（422）"；
   ``test_metrics_never_emit_bucket_lines`` 对应"我们把 histogram 渲染成 summary"；
   ``test_sse_headers_disable_proxy_buffering`` 对应"少了
   ``X-Accel-Buffering: no``，Nginx 会把 SSE 攒成一坨"。
6. **枚举与常量对齐要断言到值**：``DEFAULT_PORT`` 必须 ≥ 18000、
   ``HEARTBEAT_INTERVAL_S`` 必须为正、``NOT_APPLICABLE`` 必须是 ``-1.0``。
7. **失败路径一条都不能少**：空数据集、重复 id、``concurrency=0``、
   ``timeout_s=0``、``head(0)``、空 ``message``、超长 ``message``、
   不存在的会话、存储里有历史但内存里没有活 Agent。
8. **闭环的两条不变式**（§7 补遗）：台账**只追加**（同一条会话写两次就是
   两行）；``FeedbackLedgerEntry.allowed`` 的 ``None`` 表示"只挖掘、未判定"，
   不许被当成 ``False``。另外 ``harness_kit/eval/__init__.py`` 的
   ``__all__`` 与 ``_LAZY_EXPORTS`` 必须一一对应，每个名字都解析得动。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson20_eval_observe.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness_kit.eval import EvalCase, EvalDataset, EvalRunner
from harness_kit.eval import metrics as M
from harness_kit.eval.dataset import synthesize_cases
from harness_kit.eval.report import EvalReport, percentile
from harness_kit.eval.runner import EvalResult
from harness_kit.eval.synthesize import extract_turn_samples, synthesize_from_session
from harness_kit.events.types import EventKind, EventRecord
from harness_kit.observe import MetricsRegistry, Tracer
from harness_kit.service.app import (
    DEFAULT_PORT,
    HEARTBEAT_INTERVAL_S,
    ChatRequest,
    StoreEventBus,
    create_harness_app,
    describe_routes,
    sse_frame,
)
from harness_kit.session.jsonl_store import JsonlSessionStore

# ======================================================================
# 共用替身与夹具
# ======================================================================


class _FakeAgent:
    """替身 Agent：只实现 :func:`collect_observed_run` 真正用到的那几个接口。

    真实 Agent 是 ``third_party/agentscope/src/agentscope/agent/_agent.py:117``
    的 ``Agent``。``reply_stream`` 的 yield 序列照抄真货：
    ``ToolCallStartEvent`` → ``TextBlockDeltaEvent`` → ``ModelCallEndEvent``
    → ``ReplyEndEvent`` →（``yield_final_msg=True`` 时）最终 ``Msg``。
    """

    def __init__(self, text: str = "答案是 2", *, fail_on_reply: bool = False) -> None:
        self._text = text
        self._fail = fail_on_reply
        self.observed: list[Any] = []
        self.closed = False
        self.model = type("M", (), {"model": "deepseek-flash"})()

    async def observe(self, msgs: Any) -> None:
        """记录先导消息。"""
        self.observed.append(msgs)

    async def reply_stream(self, message: Any, *, yield_final_msg: bool = False) -> Any:
        """按真实事件序列 yield 一遍。"""
        from agentscope.event import (
            ModelCallEndEvent,
            ReplyEndEvent,
            ReplyFinishedReason,
            TextBlockDeltaEvent,
            ToolCallStartEvent,
        )
        from agentscope.message import Msg, TextBlock

        if self._fail:
            raise RuntimeError("替身 Agent 的故障注入")
        reply_id = "r-fake"
        yield ToolCallStartEvent(reply_id=reply_id, tool_call_id="tc-1", tool_call_name="Read")
        yield TextBlockDeltaEvent(reply_id=reply_id, block_id="b1", delta=self._text)
        yield ModelCallEndEvent(reply_id=reply_id, input_tokens=100, output_tokens=20)
        yield ReplyEndEvent(
            session_id="fake-session",
            reply_id=reply_id,
            finished_reason=ReplyFinishedReason.COMPLETED,
        )
        if yield_final_msg:
            yield Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text=self._text)],
            )

    async def reply(self, msg: Any) -> Any:
        """非流式入口；基类这里只是占位，真正的替身见 :class:`_ReplyAgent`。

        Returns:
            `Any`: 不会走到（子类覆盖）。
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """标记已关闭。

        ``EvalRunner.aclose_agent`` 的清理顺序是 ``aclose`` → ``close`` →
        ``model.close``（``harness_kit/eval/runner.py``），所以只要实现
        ``aclose`` 就能被它清掉。
        """
        self.closed = True


class _FakeReply:
    """带 ``get_text_content`` 的替身回复。"""

    def __init__(self, text: str) -> None:
        self._t = text

    def get_text_content(self) -> str:
        """返回文本。"""
        return self._t


class _ReplyAgent(_FakeAgent):
    """带可用 ``reply`` 的替身（服务层的非流式分支要用）。"""

    async def reply(self, msg: Any) -> _FakeReply:
        """返回预置文本。"""
        if self._fail:
            raise RuntimeError("替身 Agent 的故障注入")
        return _FakeReply(self._text)


def _events() -> list[EventRecord]:
    """造一个"一个完整回合"的事件流。

    Returns:
        `list[EventRecord]`: 5 条事件。
    """
    return [
        EventRecord(
            session_id="s-1",
            seq=0,
            kind=EventKind.REPLY_START,
            payload={"reply_id": "r1", "input_preview": "Toolkit.add_tool 重名时怎样？"},
            source="test",
        ),
        EventRecord(
            session_id="s-1",
            seq=1,
            kind=EventKind.TOOL_CALL,
            payload={"tool_name": "Grep", "tool_input_digest": "abc", "call_id": "x1"},
            source="test",
        ),
        EventRecord(
            session_id="s-1",
            seq=2,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "x1", "state": "success", "chars": 120, "error": None},
            source="test",
        ),
        EventRecord(
            session_id="s-1",
            seq=3,
            kind=EventKind.MODEL_CALL,
            payload={
                "model": "deepseek-flash",
                "prompt_tokens": 900,
                "completion_tokens": 120,
                "latency_ms": 3200.0,
                "finished_reason": "stop",
            },
            source="test",
        ),
        EventRecord(
            session_id="s-1",
            seq=4,
            kind=EventKind.REPLY_END,
            payload={"reply_id": "r1", "iterations": 2, "tool_calls": ["Grep"]},
            source="test",
        ),
    ]


@pytest.fixture()
def dataset() -> EvalDataset:
    """一个三用例的小评测集（含一条"没有 expected"的用例）。

    Returns:
        `EvalDataset`: 数据集。
    """
    return EvalDataset(
        name="unit",
        cases=[
            EvalCase(id="c1", input="1+1=?", expected="2", tags=["arith"], expected_tools=["Read"]),
            EvalCase(id="c2", input="只回答 OK", expected="OK", tags=["arith"]),
            EvalCase(id="c3", input="不用回答", tags=["bare"]),
        ],
    )


# ======================================================================
# A 组：数据集（纯逻辑，不碰模型、不碰存储）
# ======================================================================


def test_dataset_jsonl_roundtrip(tmp_path: Path, dataset: EvalDataset) -> None:
    """JSONL 往返必须逐字段相等，且头行的 name/tags 要带回来。"""
    path = tmp_path / "d.jsonl"
    dataset.to_jsonl(path)
    head = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert head["type"] == "dataset"
    assert head["name"] == "unit"

    back = EvalDataset.from_jsonl(path)
    assert [case.id for case in back] == ["c1", "c2", "c3"]
    assert back.cases[0].expected_tools == ["Read"]
    assert back.cases[2].expected is None


def test_dataset_from_jsonl_accepts_bare_lines(tmp_path: Path) -> None:
    """没有头行时用文件主名当数据集名 —— 手写的"一行一个用例"文件要能直接吃。"""
    path = tmp_path / "handwritten.jsonl"
    path.write_text('{"id": "a", "input": "hi"}\n\n{"id": "b", "input": "yo"}\n', encoding="utf-8")
    loaded = EvalDataset.from_jsonl(path)
    assert loaded.name == "handwritten"
    assert len(loaded) == 2


def test_dataset_from_jsonl_reports_bad_line_with_number(tmp_path: Path) -> None:
    """坏行必须带行号报错，不能静默跳过（"少了 3 条"没人会发现）。"""
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a", "input": "hi"}\n{not json}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.jsonl:2"):
        EvalDataset.from_jsonl(path)


def test_dataset_from_jsonl_missing_file(tmp_path: Path) -> None:
    """文件不存在时抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        EvalDataset.from_jsonl(tmp_path / "nope.jsonl")


def test_dataset_filter_and_head(dataset: EvalDataset) -> None:
    """filter 是"新对象"，head 必须有正数校验。"""
    assert len(dataset.filter(tags=["arith"])) == 2
    assert len(dataset.filter(ids=["c1", "c3"])) == 2
    assert len(dataset.head(1)) == 1
    with pytest.raises(ValueError):
        dataset.head(0)


def test_dataset_duplicate_ids_are_detectable() -> None:
    """重号必须能被查出来 —— 报告按 case_id 索引，重号会互相覆盖。"""
    dup = EvalDataset(name="x", cases=[EvalCase(id="a", input="1"), EvalCase(id="a", input="2")])
    assert dup.validate_unique_ids() == ["a"]


def test_dataset_stats(dataset: EvalDataset) -> None:
    """stats 的四个计数要与构造时一致。"""
    stats = dataset.stats()
    assert stats["case_count"] == 3
    assert stats["by_tag"] == {"arith": 2, "bare": 1}
    assert stats["with_expected"] == 2
    assert stats["with_tools"] == 1


# ======================================================================
# B 组：指标（不适用 = -1.0 这条硬约定）
# ======================================================================


async def test_not_applicable_is_minus_one_not_zero() -> None:
    """没有 expected 时返回 -1.0（不适用），**不是** 0.0（考砸了）。

    这是本讲最容易搞错的一条：哨兵取 1.0 会让"没考这项"和"考了满分"混在
    一起，聚合均值必然算错。
    """
    case = EvalCase(id="bare", input="hi")
    assert await M.exact_match(case, "随便什么") == M.NOT_APPLICABLE
    assert await M.contains(case, "随便什么") == M.NOT_APPLICABLE
    assert M.NOT_APPLICABLE == -1.0
    assert M.NOT_APPLICABLE < 0.0


async def test_exact_match_is_strict_by_default_loose_on_request() -> None:
    """``exact_match`` 默认严格（``strip()`` 后逐字相等）；``metadata["loose"]``
    才抹空白标点。这条差异必须钉死 —— 否则"评测口径"会悄悄漂移。"""
    strict = EvalCase(id="c", input="2+2", expected="4")
    assert await M.exact_match(strict, "4") == 1.0
    assert await M.exact_match(strict, " 4 ") == 1.0
    assert await M.exact_match(strict, "4。") == 0.0
    assert await M.exact_match(strict, "答案是 4") == 0.0

    loose = EvalCase(id="c", input="2+2", expected="4", metadata={"loose": True})
    assert await M.exact_match(loose, "4。") == 1.0
    assert await M.exact_match(loose, "答案是 4") == 0.0
    assert await M.contains(loose, "答案是 4") == 1.0


async def test_evidence_metrics_read_the_contextvar() -> None:
    """证据型指标从 :func:`current_run` 取证据，签名只有 (case, output)。"""
    case = EvalCase(id="c1", input="x", expected_tools=["Read"])
    run = M.ObservedRun(case_id="c1", output="ok", tool_calls=["Read"], latency_ms=1000.0)
    with M.observed_run(run):
        assert await M.tool_call_accuracy(case, "ok") == 1.0
        assert 0.0 < await M.latency_score(case, "ok") <= 1.0

    # 不在上下文里就退化成"不适用"，而不是抛异常
    assert await M.tool_call_accuracy(case, "ok") == M.NOT_APPLICABLE


async def test_evidence_metrics_penalize_extra_tool_calls() -> None:
    """多调一个工具扣一次分：1 / (1 + 0.25) = 0.8（metrics.py 的打分公式）。"""
    case = EvalCase(id="c1", input="x", expected_tools=["Read"])
    run = M.ObservedRun(case_id="c1", output="ok", tool_calls=["Read", "Grep"], latency_ms=1.0)
    with M.observed_run(run):
        assert await M.tool_call_accuracy(case, "ok") == 0.8


async def test_latency_score_decays_after_budget() -> None:
    """超预算后线性衰减：超一倍归零。"""
    case = EvalCase(id="c", input="x", metadata={"latency_budget_ms": 1000.0})
    on_budget = M.ObservedRun(case_id="c", output="", latency_ms=1000.0)
    over = M.ObservedRun(case_id="c", output="", latency_ms=3000.0)
    with M.observed_run(on_budget):
        assert await M.latency_score(case, "") == 1.0
    with M.observed_run(over):
        assert await M.latency_score(case, "") == 0.0


async def test_citation_coverage_needs_expected_citations() -> None:
    """没有 expected_citations 就是"不适用"；有则按命中比例给分。"""
    bare = EvalCase(id="c", input="x")
    assert await M.citation_coverage(bare, "[1]") == M.NOT_APPLICABLE

    case = EvalCase(id="c", input="x", expected_citations=["resource/a.py.md"])
    run = M.ObservedRun(case_id="c", output="见 resource/a.py.md", latency_ms=1.0)
    with M.observed_run(run):
        assert await M.citation_coverage(case, run.output) == 1.0


# ======================================================================
# C 组：EvalRunner（并发 / 超时 / 单条失败不中断）
# ======================================================================


async def test_runner_isolates_each_case_with_a_fresh_agent(dataset: EvalDataset) -> None:
    """每条用例一次 factory —— 共用 Agent 会把上一条的问答带进下一条。"""
    made: list[_FakeAgent] = []

    async def factory() -> Any:
        agent = _FakeAgent()
        made.append(agent)
        return agent

    runner = EvalRunner(agent_factory=factory, concurrency=2, timeout_s=10.0)
    report = await runner.run(dataset, metrics=[M.contains])
    assert len(made) == 3
    assert all(agent.closed for agent in made), "runner 必须尝试清理每条用例的 Agent"


async def test_runner_survives_a_failing_case(dataset: EvalDataset) -> None:
    """单用例失败收敛成 EvalResult(error=...)，**不**往上抛。"""
    calls = {"n": 0}

    async def factory() -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("构造失败（故意的）")
        return _FakeAgent()

    runner = EvalRunner(agent_factory=factory, concurrency=3, timeout_s=10.0)
    report = await runner.run(dataset, metrics=[M.contains])
    assert calls["n"] == 3
    failed = [item for item in report.results if item.error]
    assert len(failed) == 1
    assert "RuntimeError" in (failed[0].error or "")
    assert report.pass_rate == pytest.approx(2 / 3)


async def test_runner_timeout_is_recorded_not_raised() -> None:
    """超时也要被记成结果（``TimeoutError: 超过 Ns 未返回``）。"""
    import asyncio

    async def factory() -> Any:
        await asyncio.sleep(0.2)
        return _FakeAgent()

    data = EvalDataset(name="slow", cases=[EvalCase(id="c1", input="x")])
    runner = EvalRunner(agent_factory=factory, concurrency=1, timeout_s=0.05)
    report = await runner.run(data, metrics=[])
    assert report.results[0].ok is False
    assert "TimeoutError" in (report.results[0].error or "")


async def test_runner_rejects_bad_parameters() -> None:
    """``concurrency`` / ``timeout_s`` 必须为正。"""
    async def factory() -> Any:
        return _FakeAgent()

    with pytest.raises(ValueError):
        EvalRunner(agent_factory=factory, concurrency=0)
    with pytest.raises(ValueError):
        EvalRunner(agent_factory=factory, timeout_s=0)


async def test_runner_rejects_empty_dataset_and_duplicate_ids() -> None:
    """空数据集与重号都在开跑前拦住，而不是跑完才发现。"""
    async def factory() -> Any:
        return _FakeAgent()

    runner = EvalRunner(agent_factory=factory, concurrency=1, timeout_s=5.0)
    with pytest.raises(ValueError, match="是空的"):
        await runner.run(EvalDataset(name="empty", cases=[]), metrics=[])

    dup = EvalDataset(name="dup", cases=[EvalCase(id="a", input="1"), EvalCase(id="a", input="2")])
    with pytest.raises(ValueError, match="重复"):
        await runner.run(dup, metrics=[])


async def test_runner_collects_tokens_and_tools(dataset: EvalDataset) -> None:
    """token / 工具 / 迭代次数都从事件流里收，不从输出文本里猜。"""

    async def factory() -> Any:
        return _FakeAgent("答案是 2")

    runner = EvalRunner(
        agent_factory=factory,
        concurrency=1,
        timeout_s=10.0,
        model="deepseek-flash",
    )
    report = await runner.run(dataset.head(1), metrics=[M.contains, M.tool_call_accuracy])
    item = report.results[0]
    assert item.tokens.input_tokens == 100
    assert item.tokens.output_tokens == 20
    assert item.tool_calls == ["Read"]
    assert item.scores["tool_call_accuracy"] == 1.0
    assert item.cost_usd > 0.0, "内置价目表认得 deepseek-flash，应该折得出成本"


async def test_runner_observes_context_as_separate_messages() -> None:
    """``case.context`` 走 ``agent.observe``（先导消息），不拼进用户输入。"""
    made: list[_FakeAgent] = []

    async def factory() -> Any:
        agent = _FakeAgent()
        made.append(agent)
        return agent

    data = EvalDataset(
        name="ctx",
        cases=[EvalCase(id="c1", input="问题", context=["资料 A", "资料 B"])],
    )
    runner = EvalRunner(agent_factory=factory, concurrency=1, timeout_s=10.0)
    await runner.run(data, metrics=[])
    assert len(made[0].observed) == 1
    assert len(made[0].observed[0]) == 2


# ======================================================================
# D 组：报告与对比
# ======================================================================


def _report() -> EvalReport:
    """造一份两用例的报告。

    Returns:
        `EvalReport`: 报告。
    """
    return EvalReport(
        dataset="unit",
        profile="default",
        started_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        finished_at=datetime(2026, 9, 22, 0, 0, 10, tzinfo=timezone.utc),
        results=[
            EvalResult(case_id="c1", output="2", ok=True, scores={"contains": 1.0}, latency_ms=1000.0),
            EvalResult(case_id="c2", output="x", ok=False, scores={"contains": 0.0}, latency_ms=3000.0, error="boom"),
        ],
    )


def test_report_summary_and_markdown() -> None:
    """summary 要有通过率、P50/P95、token 与成本；Markdown 要有失败清单。"""
    report = _report()
    summary = report.summary()
    assert summary["pass_rate"] == 0.5
    # 只有两条样本时 P50 走线性插值，落回两条的中点 2000.0（不是第一条）。
    # 想验证"分位数怎么算"，必须用样本量足够的数据集，见 test_percentile_edge_cases。
    assert summary["latency_p50_ms"] == 2000.0
    # P95 同样插值：1000 + 0.95 × (3000 − 1000) = 2900.0。两级分位数
    # 用同一个函数算，所以"报出来的 P95 比最大值小"是正常现象，不是 bug。
    assert summary["latency_p95_ms"] == 2900.0
    assert len(report.failures()) == 1

    text = report.to_markdown()
    assert "评测报告" in text and "## 失败用例" in text and "boom" in text


def test_report_json_roundtrip() -> None:
    """JSON 往返后关键字段不丢。"""
    report = _report()
    back = EvalReport.from_json(report.to_json())
    assert back.dataset == "unit"
    assert [item.case_id for item in back.results] == ["c1", "c2"]
    assert back.summary()["pass_rate"] == 0.5


def test_report_save_writes_json_and_markdown(tmp_path: Path) -> None:
    """``save`` 的参数是"文件名前缀"，会产出 ``.json`` + ``.md``。"""
    written = _report().save(tmp_path / "run1", markdown=True)
    assert written["json"].name == "run1.json"
    assert written["markdown"].name == "run1.md"
    assert written["json"].is_file() and written["markdown"].is_file()


def test_report_compare_produces_table_and_verdict() -> None:
    """对比表要能说清"哪一项变好 / 变差"。"""
    baseline = EvalReport(
        dataset="unit",
        profile="baseline",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc),
        results=[
            EvalResult(case_id="c1", output="2", ok=True, scores={"contains": 1.0}, latency_ms=8000.0),
            EvalResult(case_id="c2", output="2", ok=True, scores={"contains": 1.0}, latency_ms=8000.0),
        ],
    )
    diff = _report().compare(baseline)
    assert "|" in diff
    assert "pass_rate" in diff or "通过率" in diff


def test_percentile_edge_cases() -> None:
    """单样本退回自身；空序列返回 0.0（不装样子做插值）。"""
    assert percentile([5.0], 0.5) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([], 0.95) == 0.0


# ======================================================================
# E 组：数据合成（把第 9 讲的事件流变成评测样本）
# ======================================================================


def test_extract_turn_samples_reads_reality_from_events() -> None:
    """工具、token、轮数都是真值；**输出文本拿不到**（事件流里没有这一列）。"""
    samples = extract_turn_samples(_events())
    assert len(samples) == 1
    sample = samples[0]
    assert "add_tool" in sample.input
    assert sample.unique_tools == ["Grep"]
    assert sample.prompt_tokens == 900
    assert sample.completion_tokens == 120
    assert sample.iterations == 2
    assert sample.closed is True
    assert "from_session" in sample.tags() and "tool_use" in sample.tags()


async def test_synthesize_from_session(tmp_path: Path) -> None:
    """从真实会话存储里合成评测集：input 有真值、expected_tools 有真值、expected 为空。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    try:
        for record in _events():
            await store.append(record)
        dataset = await synthesize_from_session(store, "s-1", name="from-events")
    finally:
        await store.aclose()

    assert len(dataset) == 1
    case = dataset.cases[0]
    assert case.input
    assert case.expected_tools == ["Grep"]
    assert case.expected is None
    assert case.metadata["session_id"] == "s-1"


async def test_synthesize_from_session_requires_closed_by_default(tmp_path: Path) -> None:
    """默认只收已经 ``REPLY_END`` 的回合；``require_closed=False`` 才连未收尾的一起收。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    try:
        for record in _events()[:-1]:  # 丢掉 REPLY_END
            await store.append(record)
        strict = await synthesize_from_session(store, "s-1")
        loose = await synthesize_from_session(store, "s-1", name="loose", require_closed=False)
    finally:
        await store.aclose()

    assert len(strict) == 0
    assert len(loose) == 1


async def test_synthesize_cases_skips_failures() -> None:
    """合成是"尽力而为"：一条坏样本不该让整批白干。"""
    seed = [EvalCase(id="s1", input="原问题", tags=["seed"])]
    seen: list[str] = []

    async def rewriter(case: EvalCase) -> EvalCase:
        seen.append(case.id)
        if len(seen) == 2:
            raise RuntimeError("第二条失败（故意的）")
        return case.model_copy(update={"input": f"{case.input}#{len(seen)}"})

    produced = await synthesize_cases(seed, n=3, rewriter=rewriter)
    assert len(produced) == 2
    assert all("synthetic" in item.tags for item in produced)
    assert all(item.metadata["synthesized_from"] == "s1" for item in produced)


async def test_synthesize_cases_rejects_empty_seed() -> None:
    """没有种子就没有合成；``n`` 必须为正。"""

    async def rewriter(case: EvalCase) -> EvalCase:
        return case

    with pytest.raises(ValueError):
        await synthesize_cases([], n=1, rewriter=rewriter)
    with pytest.raises(ValueError):
        await synthesize_cases([EvalCase(id="a", input="x")], n=0, rewriter=rewriter)


# ======================================================================
# F 组：可观测（Tracer / MetricsRegistry）
# ======================================================================


def test_tracer_defaults_to_in_process_only() -> None:
    """默认 ``exporter=None``：只留进程内 span 树，不碰任何网络。"""
    tracer = Tracer(service_name="unit-test")
    assert tracer.exporter_kind == "none"
    with tracer.span("reply", session_id="s") as root:
        root.set_attribute("k", "v")
        with tracer.span("model_call") as child:
            child.add_event("first_token")
    assert tracer.span_count() == 2
    assert tracer.roots()[0].name == "reply"
    assert child.parent_id == root.span_id
    tracer.shutdown()


def test_tracer_degrades_on_unknown_exporter() -> None:
    """写错 exporter 名字：只打 warning 并退化为进程内，**绝不抛异常**。"""
    tracer = Tracer(service_name="unit-test", exporter="jaeger-that-does-not-exist")
    assert tracer.exporter_kind == "none"
    with tracer.span("x"):
        pass
    assert tracer.span_count() == 1
    tracer.shutdown()


def test_tracer_can_double_write_to_injected_exporter() -> None:
    """注入 ``InMemorySpanExporter`` 时，双写通道要真的把 span 送出去。

    这是"不装 collector 也能验证 OTel 通道"的办法：不需要网络、
    不需要 4318 端口，也不会有后台线程一直重试。
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    tracer = Tracer(service_name="unit-test", otel_exporter=memory)
    try:
        assert tracer.exporter_kind == "custom"
        with tracer.span("reply", session_id="s"):
            pass
        exported = memory.get_finished_spans()
    finally:
        tracer.shutdown()

    assert [span.name for span in exported] == ["reply"]
    assert dict(exported[0].attributes or {})["session_id"] == "s"
    # 进程内那份也还在（双写，不是替换）
    assert tracer.span_count() == 1


def test_tracer_summary_and_dump(tmp_path: Path) -> None:
    """summary 按 span 名聚合，dump 出的 JSON 能被解析。"""
    tracer = Tracer(service_name="unit-test")
    for _ in range(3):
        with tracer.span("model_call"):
            pass
    summary = tracer.summary()
    assert summary["model_call.count"] == 3.0
    assert "model_call.p95_ms" in summary

    path = tracer.dump(tmp_path / "traces.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["spans"]) == 3


def test_metrics_registry_exposition_format() -> None:
    """exposition 文本要有 HELP/TYPE、标签、以及 summary 形态的分位数。"""
    registry = MetricsRegistry()
    counter = registry.counter("replies_total", help="完成的回复次数")
    counter.inc(profile="default")
    counter.inc(2.0, profile="default")
    histogram = registry.histogram("reply_latency", unit="ms")
    for value in (100.0, 200.0, 300.0, 400.0, 5000.0):
        histogram.observe(value, profile="default")

    text = registry.render_prometheus()
    assert "# HELP harness_kit_replies_total 完成的回复次数" in text
    assert "# TYPE harness_kit_reply_latency summary" in text
    assert 'profile="default"' in text
    assert 'quantile="0.5"' in text and 'quantile="0.95"' in text
    assert registry.get_counter("replies_total").total() == 3.0


def test_metrics_never_emit_bucket_lines() -> None:
    """我们**有意不输出** ``_bucket``：分位数是进程内算的。

    这一点必须写在测试里，否则下一个读者会以为它是标准 histogram、
    拿去喂 ``histogram_quantile()``，得到一个永远算不对的值。
    """
    registry = MetricsRegistry()
    histogram = registry.histogram("latency", unit="ms")
    histogram.observe(10.0)
    text = registry.render_prometheus()
    assert "_bucket" not in text
    assert "latency_ms_count" in text and "latency_ms_sum" in text


def test_metrics_label_escaping() -> None:
    """标签值里的引号与反斜杠必须转义 —— 否则暴露文本语法就坏了。"""
    registry = MetricsRegistry()
    registry.counter("c").inc(tool='he said "hi"')
    text = registry.render_prometheus()
    assert 'tool="he said \\"hi\\""' in text


# ======================================================================
# G 组：服务层（ASGI TestClient，不 bind 端口）
# ======================================================================


def test_port_and_heartbeat_constants() -> None:
    """契约铁律：默认端口 ≥ 18000；心跳间隔必须为正。"""
    assert DEFAULT_PORT >= 18000
    assert HEARTBEAT_INTERVAL_S > 0


def test_sse_frame_shape() -> None:
    """SSE 帧就是 ``data: {...}\\n\\n``，中文不转义。"""
    frame = sse_frame({"event": "x", "data": {"文本": "值"}})
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert "文本" in frame


def test_chat_request_model_must_live_at_module_level() -> None:
    """请求模型定义在模块级 —— 放进工厂函数里会让 FastAPI 把请求体当查询参数。

    这条测试对着"POST /chat 恒 422 ``loc: ['query','payload']``"那个真实坑。
    """
    import harness_kit.service.app as module

    assert ChatRequest.__module__ == module.__name__
    assert "message" in ChatRequest.model_fields
    assert ChatRequest(message="hi").stream is True


def _app(tmp_path: Path) -> Any:
    """按 minimal Profile 建一个 app（不监听端口）。

    Args:
        tmp_path (`Path`): 临时目录（会话存储落在这里）。

    Returns:
        `Any`: FastAPI 实例。
    """
    from harness_kit.cli import _load_profile, default_settings

    settings = default_settings()
    settings.session_dir = tmp_path / "sessions"
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    profile = _load_profile("default", settings)
    return create_harness_app(profile=profile, settings=settings, max_sessions=2)


def test_create_harness_app_routes(tmp_path: Path) -> None:
    """契约要求的 5 条路由一条都不能少。"""
    app = _app(tmp_path)
    routes = describe_routes(app)
    assert any("POST /chat" in line for line in routes)
    assert any("GET  /sessions" in line for line in routes)
    assert any("GET  /sessions/{session_id}" in line for line in routes)
    assert any("GET  /healthz" in line for line in routes)
    assert any(line.strip().endswith("/") for line in routes)


def test_healthz_reports_environment(tmp_path: Path) -> None:
    """``/healthz`` 要能回答"跑的是哪个 Profile、模型配好了没有"。"""
    from starlette.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["profile"] == "default"
    assert "18000" in body["port_policy"]
    assert body["active_sessions"] == 0


def test_session_lifecycle_over_http(tmp_path: Path) -> None:
    """建会话 → 列出 → 看详情：``SESSION_START`` 必须已经落盘。"""
    from starlette.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/sessions", json={})
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        assert session_id in [item["session_id"] for item in client.get("/sessions").json()["sessions"]]

        detail = client.get(f"/sessions/{session_id}", params={"event_limit": 10}).json()
        kinds = [item["kind"] for item in detail["events"]]
        assert "session_start" in kinds
        assert detail["active"] is True

    # TestClient 的上下文退出会跑 lifespan 的 finally → 服务必须被关干净
    assert app.state.service.sessions == {}


def test_missing_session_returns_404(tmp_path: Path) -> None:
    """不存在的会话是 404（``KeyError`` 被翻译成 HTTP 语义）。"""
    from starlette.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/sessions/definitely-not-here").status_code == 404


def test_empty_message_is_rejected(tmp_path: Path) -> None:
    """空消息在 pydantic 层就被挡（``min_length=1``），不会打到模型。"""
    from starlette.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/chat", json={"message": ""}).status_code == 422


def test_webui_is_served(tmp_path: Path) -> None:
    """``GET /`` 送出单文件 Web UI。"""
    from starlette.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "<html" in page.text.lower()


def test_metrics_endpoint_is_exposition_text(tmp_path: Path) -> None:
    """``GET /metrics`` 的 content-type 必须是 exposition 协议要求的那个。"""
    from starlette.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


async def test_store_event_bus_renumbers_seq(tmp_path: Path) -> None:
    """seq 的权威归属是存储层：``StoreEventBus.publish`` 会丢弃 producer 的 seq。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    try:
        bus = StoreEventBus(store, "s-1")
        first = await bus.publish(
            EventKind.CUSTOM,
            EventRecord(session_id="s-1", seq=999, kind=EventKind.CUSTOM, payload={"name": "x"}, source="t"),
        )
        second = await bus.publish(
            EventKind.CUSTOM,
            EventRecord(session_id="s-1", seq=999, kind=EventKind.CUSTOM, payload={"name": "y"}, source="t"),
        )
    finally:
        await store.aclose()

    assert (first, second) == (0, 1)
    assert bus.published == 2
    assert bus.dropped == 0


async def test_store_event_bus_swallows_storage_errors() -> None:
    """存储写失败**不往上抛** —— 事件溯源是旁路，不该打断用户正在等的回复。"""

    class _BrokenStore:
        """任何 append 都失败的假存储。"""

        async def next_seq(self, session_id: str) -> int:
            """返回 0。

            Returns:
                `int`: 0。
            """
            return 0

        async def append(self, record: Any) -> None:
            """抛异常。

            Raises:
                `RuntimeError`: 永远抛。
            """
            raise RuntimeError("磁盘满了（故意的）")

    bus = StoreEventBus(_BrokenStore(), "s-1")  # type: ignore[arg-type]
    seq = await bus.publish(
        EventKind.CUSTOM,
        EventRecord(session_id="s-1", seq=0, kind=EventKind.CUSTOM, payload={"name": "x"}, source="t"),
    )
    assert seq == -1
    assert bus.dropped == 1
    assert bus.published == 0


async def test_chat_service_refuses_implicit_resume(tmp_path: Path) -> None:
    """存储里有历史、内存里没有活 Agent 时**报错说清楚**，不悄悄开个同名新会话。"""
    from harness_kit.cli import _load_profile, default_settings
    from harness_kit.service.app import ChatService

    settings = default_settings()
    settings.session_dir = tmp_path / "sessions"
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    store = JsonlSessionStore(settings.session_dir)
    try:
        await store.append(
            EventRecord(
                session_id="ghost",
                seq=0,
                kind=EventKind.SESSION_START,
                payload={"profile": "default", "agent_name": "a", "cwd": "/tmp"},
                source="test",
            ),
        )
        service = ChatService(profile=_load_profile("default", settings), settings=settings, store=store)
        assert await service.list_sessions()
        with pytest.raises(ValueError, match="SessionResumer|历史事件"):
            await service.ensure_session("ghost")
    finally:
        await store.aclose()


async def test_create_session_rejects_duplicate_id(tmp_path: Path) -> None:
    """同名会话必须报错，不能悄悄覆盖旧事件流。"""
    from harness_kit.cli import _load_profile, default_settings
    from harness_kit.service.app import ChatService

    settings = default_settings()
    settings.session_dir = tmp_path / "sessions"
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    service = ChatService(profile=_load_profile("default", settings), settings=settings, max_sessions=4)
    try:
        await service.create_session(session_id="dup")
        with pytest.raises(ValueError, match="已存在"):
            await service.create_session(session_id="dup")
    finally:
        await service.aclose()


def test_chat_service_rejects_bad_max_sessions(tmp_path: Path) -> None:
    """``max_sessions`` 必须为正。"""
    from harness_kit.cli import _load_profile, default_settings
    from harness_kit.service.app import ChatService

    settings = default_settings()
    settings.session_dir = tmp_path / "sessions"
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        ChatService(profile=_load_profile("default", settings), settings=settings, max_sessions=0)


# ======================================================================
# H 组：Profile 声明式配置（一键切换 Agent 实例）
# ======================================================================


def test_profiles_load_and_explain_sources() -> None:
    """五个 Profile 都能解析；``extends`` 的合并来源要有记录可查。"""
    from harness_kit.cli import _load_profile, default_settings
    from harness_kit.config.loader import load_resolved_profile

    settings = default_settings()
    search_dir = Path(settings.resolve(settings.profile_dir))
    for name in ("default", "coding", "readonly_coder", "research", "researcher_with_memory"):
        profile = load_resolved_profile(name, search_dir=search_dir)
        assert profile.name == name

    coding = _load_profile("coding", settings)
    assert coding.agent.name == "coding-agent"
    assert coding.permission.mode == "accept_edits"
    # coding extends default，但 middleware 是列表替换语义
    assert [item.name for item in coding.middleware] == ["budget", "guards"]
    # tools.packs 用 !append，所以 builtin 与 repo 都在
    assert "repo" in coding.tools.packs


def test_readonly_profile_is_dont_ask() -> None:
    """``enable_hitl: false`` 被翻译成 DONT_ASK —— 只读助手要能无人值守地跑。

    这是"配置里的一个字会改变 Agent 的行为"的最好例子。
    """
    from harness_kit.cli import _load_profile, default_settings

    profile = _load_profile("readonly_coder", default_settings())
    assert profile.permission.mode == "explore"
    assert profile.agent.enable_hitl is False


def test_demo_profile_repoints_memory_workspace() -> None:
    """Demo 必须把"写入侧"和"检索侧"指向同一个工作区，否则检索静默返回 0 条。"""
    from harness_kit.demo.code_assistant.agent import demo_profile, default_settings

    settings = default_settings()
    profile = demo_profile(index_root="./.harness/reme/unit_demo", settings=settings)
    memory_dirs = [item.params.get("workspace_dir") for item in profile.middleware if item.name == "reme_memory"]
    assert memory_dirs == [str(profile.memory.workspace_root)]
    assert profile.agent.name == "code-assistant"


def test_demo_profile_requires_memory_middleware() -> None:
    """基础 Profile 里没有 ``reme_memory`` 时必须在装配前报错。"""
    from harness_kit.demo.code_assistant.agent import demo_profile, default_settings

    with pytest.raises(ValueError, match="reme_memory"):
        demo_profile(index_root="./.harness/reme/x", settings=default_settings(), profile_name="default")


def test_citation_cross_check() -> None:
    """引用核对是"文件级"交叉核对：同名文件会互相冒充（已知上限）。"""
    from harness_kit.demo.code_assistant.agent import citation_markers, source_candidates

    assert citation_markers("见 [1] 与 [2]，还有 [1]") == [1, 2]
    candidates = source_candidates("resource/agentscope/_agent.py.md")
    assert "resource/agentscope/_agent.py.md" in candidates
    assert "_agent.py.md" in candidates
    assert "_agent.py" in candidates


def test_cli_parser_has_lesson20_subcommands() -> None:
    """CLI 是运营层总闸门：契约里的五个子命令 + §7 补遗的 ``feedback`` 都要在。"""
    from harness_kit.cli import build_parser

    parser = build_parser()
    actions = [action for action in parser._actions if hasattr(action, "choices") and action.choices]
    names: set[str] = set()
    for action in actions:
        names.update(action.choices)
    assert {"run", "doctor", "eval", "serve", "profile", "feedback"} <= names


def test_serve_refuses_port_below_18000() -> None:
    """``serve`` 拒绝低于 18000 的端口 —— 铁律要写在代码里，不只是文档里。"""
    from harness_kit.cli import _serve, build_parser

    options = build_parser().parse_args(["serve", "--port", "8000"])
    import asyncio

    assert asyncio.run(_serve(options)) == 1
# ======================================================================
# I 组：反馈闭环（线上失败 → 回归集 → 闸门 → 台账，见第 20 讲 §7 补遗）
# ======================================================================


def _failed_session() -> list[EventRecord]:
    """一个"工具报错 + 绕圈子"的线上会话。

    Returns:
        `list[EventRecord]`: 3 条事件。
    """
    return [
        EventRecord(
            session_id="s-bad",
            seq=0,
            kind=EventKind.REPLY_START,
            payload={"reply_id": "r1", "input_preview": "把测试全跑一遍"},
            source="prod",
        ),
        EventRecord(
            session_id="s-bad",
            seq=1,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "c1", "state": "error", "chars": 40, "error": "pytest: command not found"},
            source="prod",
        ),
        EventRecord(
            session_id="s-bad",
            seq=2,
            kind=EventKind.REPLY_END,
            payload={"reply_id": "r1", "iterations": 9, "tool_calls": ["Bash"]},
            source="prod",
        ),
    ]


def _clean_session() -> list[EventRecord]:
    """一个正常会话（**不该**被挑中）。

    Returns:
        `list[EventRecord]`: 2 条事件。
    """
    return [
        EventRecord(
            session_id="s-ok",
            seq=0,
            kind=EventKind.REPLY_START,
            payload={"reply_id": "r1", "input_preview": "你好"},
            source="prod",
        ),
        EventRecord(
            session_id="s-ok",
            seq=1,
            kind=EventKind.REPLY_END,
            payload={"reply_id": "r1", "iterations": 1, "tool_calls": []},
            source="prod",
        ),
    ]


async def _store_with(tmp_path: Path, groups: list[list[EventRecord]]) -> JsonlSessionStore:
    """把一个或多个会话写进临时存储。

    Args:
        tmp_path (`Path`): pytest 的临时目录。
        groups (`list[list[EventRecord]]`): 每个元素是一个会话的事件流。

    Returns:
        `JsonlSessionStore`: 已经写好事件的存储（调用方负责 ``aclose``）。
    """
    store = JsonlSessionStore(tmp_path / "sessions")
    for records in groups:
        for record in records:
            await store.append(record)
    return store


async def test_scan_sessions_picks_only_failed_sessions(tmp_path: Path) -> None:
    """扫描只挑"出过事"的会话，并且把**理由与证据**一起带出来。"""
    from harness_kit.eval.feedback import scan_sessions

    store = await _store_with(tmp_path, [_failed_session(), _clean_session()])
    try:
        picks = await scan_sessions(store)
    finally:
        await store.aclose()

    assert [pick.session_id for pick in picks] == ["s-bad"]
    pick = picks[0]
    # TOOL_RESULT(state=error) + REPLY_END(iterations=9 >= 6)：两条都命中
    assert pick.reasons == ["tool_error", "over_iterations"]
    assert pick.reason_text() == "tool_error+over_iterations"
    assert pick.n_replies == 1
    assert any("pytest: command not found" in line for line in pick.evidence)


async def test_scan_sessions_validates_parameters(tmp_path: Path) -> None:
    """三个带约束的参数都要在**进扫描之前**报错。"""
    from harness_kit.eval.feedback import scan_sessions

    store = await _store_with(tmp_path, [_failed_session()])
    try:
        with pytest.raises(ValueError, match="over_iterations"):
            await scan_sessions(store, over_iterations=-1)
        with pytest.raises(ValueError, match="min_replies"):
            await scan_sessions(store, min_replies=-1)
        with pytest.raises(ValueError, match="max_sessions"):
            await scan_sessions(store, max_sessions=0)
        # 空 triggers = "全量收"：不再按信号过滤，只剩 min_replies 这条门槛
        assert len(await scan_sessions(store, triggers=())) == 1
        assert len(await scan_sessions(store, triggers=(), min_replies=0)) == 1
    finally:
        await store.aclose()


async def test_build_regression_dataset_carries_provenance(tmp_path: Path) -> None:
    """回归用例必须能回答"我是从哪次线上事故来的"。"""
    from harness_kit.eval.feedback import build_regression_dataset, scan_sessions

    store = await _store_with(tmp_path, [_failed_session(), _clean_session()])
    try:
        picks = await scan_sessions(store)
        dataset, owner = await build_regression_dataset(store, picks)
    finally:
        await store.aclose()

    assert len(dataset) == 1
    assert "regression" in dataset.tags
    case = dataset.cases[0]
    assert case.id == "s-bad-t0"
    assert case.metadata["origin_session"] == "s-bad"
    assert case.metadata["origin_reasons"] == ["tool_error", "over_iterations"]
    fingerprint = case.metadata["origin_fingerprint"]
    assert len(fingerprint) == 12
    # 去重账本：指纹 → 会话 id，台账靠它对账
    assert owner == {fingerprint: "s-bad"}
    # 线上事件的 input_preview 没有快照配对，所以 expected 必须为空 ——
    # 宁可"这条用例没考这一项"（NOT_APPLICABLE），也不要一个错位的真值
    assert case.expected is None


def _gate_report(pass_rate: float, contains: float) -> EvalReport:
    """造一份两份用例的报告（一份通过、一份不通过）。

    Args:
        pass_rate (`float`): 期望的通过率（由 ok 决定，这里只为可读性传入）。
        contains (`float`): ``contains`` 指标的均值。

    Returns:
        `EvalReport`: 报告。
    """
    n_ok = round(pass_rate * 2)
    results = []
    for index in range(2):
        ok = index < n_ok
        results.append(
            EvalResult(
                case_id=f"c{index}",
                output="pytest",
                ok=ok,
                scores={"contains": contains if ok else 0.0},
                latency_ms=1000.0,
            ),
        )
    return EvalReport(
        dataset="regression-from-production",
        profile="default",
        started_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        finished_at=datetime(2026, 9, 22, 0, 0, 10, tzinfo=timezone.utc),
        results=results,
    )


def test_regression_gate_blocks_a_drop() -> None:
    """退步必须被拦，且拦截理由要含指标名与前后值。"""
    from harness_kit.eval.feedback import RegressionGate

    baseline = _gate_report(1.0, 1.0)
    current = _gate_report(0.5, 0.5)
    decision = RegressionGate().evaluate(current, baseline)

    assert decision.allowed is False
    assert decision.pass_rate_delta == -0.5
    text = "; ".join(decision.reasons)
    assert "通过率退步 0.5000" in text and "1.0000 → 0.5000" in text
    assert "指标 contains 退步" in text
    # 同分不判退步（只有真的掉下去才拦）
    assert RegressionGate().evaluate(baseline, baseline).allowed is True


def test_regression_gate_allows_first_run_but_records_it() -> None:
    """没有基线时默认放行 —— 但这件事不能是隐形的。"""
    from harness_kit.eval.feedback import RegressionGate

    current = _gate_report(0.5, 0.5)
    first = RegressionGate().evaluate(current, None)
    assert first.allowed is True
    assert first.reasons == []
    # 放行必须带着"这次没有基线可比"这个标记，否则下游没法区分
    # "验证通过"与"第一次跑，没人可比"
    assert first.baseline == "none"
    assert RegressionGate().evaluate(current, current).baseline == "compare"

    # 绝对下限是唯一一条不依赖基线的规则
    strict = RegressionGate(min_pass_rate=0.99).evaluate(current, None)
    assert strict.allowed is False
    assert strict.baseline == "none"
    assert any("无基线" in reason and "绝对下限" in reason for reason in strict.reasons)

    # 豁免某个指标之后，指标级理由消失，但通过率下跌仍然拦
    waived = RegressionGate(ignore_metrics=["contains"]).evaluate(_gate_report(0.5, 0.5), _gate_report(1.0, 1.0))
    assert waived.allowed is False
    assert all("指标 contains" not in reason for reason in waived.reasons)
    assert any("通过率退步" in reason for reason in waived.reasons)


def test_gate_rejects_unknown_parameters() -> None:
    """``extra="forbid"``：写错的参数在构造期就报错，而不是跑完才炸。"""
    from harness_kit.eval.feedback import RegressionGate

    with pytest.raises(Exception):
        RegressionGate(max_metric_drop=0.1, max_metric_drop_pct=0.1)


async def test_ledger_is_append_only_and_records_unjudged(tmp_path: Path) -> None:
    """台账只追加；"未判定"必须是 ``None``，不许被写成 ``False``。"""
    from harness_kit.eval.feedback import (
        append_ledger,
        build_regression_dataset,
        entries_from_decision,
        read_ledger,
        scan_sessions,
    )

    store = await _store_with(tmp_path, [_failed_session()])
    try:
        picks = await scan_sessions(store)
        dataset, owner = await build_regression_dataset(store, picks)
    finally:
        await store.aclose()

    ledger = tmp_path / "feedback" / "ledger.jsonl"
    entries = entries_from_decision(picks, owner, dataset)
    assert append_ledger(ledger, entries) == 1
    assert append_ledger(ledger, entries) == 1

    rows = read_ledger(ledger)
    assert len(rows) == 2  # 追加两次就是两行，永不重写
    assert rows[0].session_id == "s-bad"
    assert rows[0].case_id == "s-bad-t0"
    assert rows[0].allowed is None  # 只挖掘、未判定
    assert rows[0].gate_reasons == []
    assert rows[0].gate_baseline == ""  # 未判定就没有"含金量"可记
    assert read_ledger(tmp_path / "missing.jsonl") == []


def test_ledger_reports_a_bad_line(tmp_path: Path) -> None:
    """台账被外部改坏了必须显式报错（带行号），而不是静默跳过。"""
    from harness_kit.eval.feedback import read_ledger

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"session_id": "s-bad"}\n不是 JSON\n', encoding="utf-8")
    with pytest.raises(Exception):
        read_ledger(ledger)


def test_eval_package_exports_are_consistent() -> None:
    """``__all__`` 与惰性表一一对应，且每个名字都解析得动。"""
    import harness_kit.eval as eval_pkg

    assert set(eval_pkg.__all__) == set(eval_pkg._LAZY_EXPORTS)
    for name in eval_pkg.__all__:
        assert getattr(eval_pkg, name) is not None
    assert "RegressionGate" in dir(eval_pkg)
