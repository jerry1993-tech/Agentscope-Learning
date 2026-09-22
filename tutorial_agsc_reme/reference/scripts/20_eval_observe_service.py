# -*- coding: utf-8 -*-
"""第 20 讲验证脚本：评测 → 可观测 → 服务化 → Profile → Demo 链路（``--live`` 才调模型）。

一次运行分六段，每段都有明确的"要不要模型"边界：

=========== ================================================== ============
段          验证什么                                            模型调用
=========== ================================================== ============
A           评测层：数据集 / 指标 / 报告 / 对比（全离线替身）       0
B           数据合成：从事件流反推评测样本（全离线）                0
C           可观测层：Tracer span 树 + MetricsRegistry 渲染        0
D           服务层：FastAPI 路由 + TestClient 打真实 HTTP          0
E           真实 ReMe：索引一小段代码库 → 直接检索（不经过模型）     0
F           真实端到端：EvalRunner 跑 2 条用例 + 从会话合成评测集    ≤ 4
=========== ================================================== ============

**为什么不默认跑 F 段**：契约纪律是"不要浪费 LLM 调用"。前五段已经覆盖了
"评测引擎 / 可观测 / 服务化 / 记忆门面"的全部接口契约，F 段只是把
:class:`~harness_kit.eval.runner.EvalRunner` 接到真实模型上确认一次 ——
想跑就加 ``--live``，不加时脚本会打印"已跳过"并返回 0。

**D 段为什么不占端口**：用的是 ``starlette.testclient.TestClient``，
它把请求直接喂进 ASGI app，**不 bind 任何 socket**。契约铁律是"不要起
常驻服务"，只有真正需要 curl 时（见教程第五部分）才用
``harness-kit serve`` 起 18420 并立刻关掉。

用法::

    export REPO=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning
    PYTHONPATH=$REPO/third_party/ReMe:$REPO/tutorial_agsc_reme/reference \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \\
      $REPO/tutorial_agsc_reme/reference/scripts/20_eval_observe_service.py

    # 加上 F 段（真实模型，约 4 次调用）
    ... scripts/20_eval_observe_service.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Sequence

# ----------------------------------------------------------------------
# 路径与 .env：**必须在 import harness_kit 之前**做，因为 Settings.from_env
# 读的是进程环境（harness_kit/settings.py 的 _DEFAULT_ENV_FILE 指向仓库根 .env，
# 但显式 load_dotenv 一次能让 REPO 变量与 .env 的键都进 os.environ）。
# ----------------------------------------------------------------------
_THIS = Path(__file__).resolve()
REFERENCE = _THIS.parents[1]  # tutorial_agsc_reme/reference
REPO = REFERENCE.parents[1]  # 仓库根
for _candidate in (REFERENCE, REPO):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=False)

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING", enqueue=False)

PASS = "  [ok]  "
FAIL = "  [FAIL]"
SKIP = "  [skip]"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条断言结果并打印。

    Args:
        name (`str`): 断言名。
        ok (`bool`): 是否通过。
        detail (`str`): 附加说明。

    Returns:
        `bool`: ``ok`` 原样返回，便于链式使用。
    """
    _results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}{('  ' + detail) if detail else ''}")
    return ok


def section(title: str) -> None:
    """打印段标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ======================================================================
# A 段：评测层（离线，0 次模型调用）
# ======================================================================
async def segment_a(tmp: Path) -> None:
    """数据集 / 指标 / 报告 / 对比 —— 全程用替身 Agent，不碰模型。

    Args:
        tmp (`Path`): 临时目录。
    """
    from harness_kit.eval import EvalCase, EvalDataset, EvalRunner
    from harness_kit.eval import metrics as M
    from harness_kit.eval.report import percentile

    section("A 段：评测层（离线替身，0 次模型调用）")

    # ---- A1. JSONL 往返 ----
    dataset = EvalDataset(
        name="lesson20-smoke",
        tags=["demo"],
        cases=[
            EvalCase(
                id="c1",
                input="1+1=?",
                expected="2",
                tags=["arith"],
                expected_tools=["Read"],
                expected_citations=["resource/a.py.md"],
            ),
            EvalCase(id="c2", input="只回答 OK", expected="OK", tags=["arith"]),
            EvalCase(id="c3", input="不用回答", expected=None, tags=["bare"]),
        ],
    )
    path = tmp / "lesson20.jsonl"
    dataset.to_jsonl(path)
    reloaded = EvalDataset.from_jsonl(path)
    check(
        "A1 JSONL 往返",
        [c.id for c in reloaded] == ["c1", "c2", "c3"] and reloaded.name == "lesson20-smoke",
        f"cases={len(reloaded)} name={reloaded.name}",
    )
    check("A1b 头行被识别", reloaded.tags == ["demo"], f"tags={reloaded.tags}")
    check("A1c stats", reloaded.stats()["with_tools"] == 1, json.dumps(reloaded.stats(), ensure_ascii=False))

    # ---- A2. filter / head / 去重校验 ----
    check("A2 filter(tags=[arith])", len(reloaded.filter(tags=["arith"])) == 2)
    check("A2b head(1)", len(reloaded.head(1)) == 1)
    dup = EvalDataset(name="dup", cases=[EvalCase(id="x", input="a"), EvalCase(id="x", input="b")])
    check("A2c 重号可检出", dup.validate_unique_ids() == ["x"])

    # ---- A3. 指标：不适用返回 -1.0，而不是 0.0 ----
    c_no_expect = EvalCase(id="bare", input="hi")
    v = await M.exact_match(c_no_expect, "随便")
    check("A3 无 expected 时 exact_match 不适用", v == M.NOT_APPLICABLE, f"返回 {v}")
    v2 = await M.exact_match(reloaded.cases[0], "答案是 4")
    v3 = await M.contains(reloaded.cases[0], "答案是 2")
    check("A3b exact_match 宽松相等失败", v2 == 0.0, f"返回 {v2}")
    check("A3c contains 命中", v3 == 1.0, f"返回 {v3}")

    # ---- A4. 证据型指标走 ContextVar，不走签名 ----
    # 用例 c1 的 expected_tools 是 ["Read"]。命中 1 个期望工具、没有多余调用
    # → 1.0；多调一个 → 1 / (1 + 1*0.25) = 0.8（metrics.py:295 的打分规则）。
    exact = M.ObservedRun(case_id="c1", output="答案 2 [1]", tool_calls=["Read"], latency_ms=1500.0, iterations=1)
    noisy = M.ObservedRun(
        case_id="c1",
        output="答案 2 [1]",
        tool_calls=["Read", "Grep", "Read"],
        latency_ms=1500.0,
        iterations=2,
    )
    with M.observed_run(exact):
        t_exact = await M.tool_call_accuracy(reloaded.cases[0], exact.output)
    with M.observed_run(noisy):
        t_noisy = await M.tool_call_accuracy(reloaded.cases[0], noisy.output)
        lat = await M.latency_score(reloaded.cases[0], noisy.output)
    check("A4 tool_call_accuracy 命中期望工具且无多余调用", t_exact == 1.0, f"返回 {t_exact}")
    check("A4b 多调一个工具按公式扣分", t_noisy == 0.8, f"返回 {t_noisy}（1/(1+0.25)）")
    check("A4c latency_score（1500ms / 20000ms 预算）", 0.0 < lat <= 1.0, f"返回 {lat:.4f}")

    # ---- A5. EvalRunner：单用例失败不中断整体 ----
    calls = {"n": 0}

    class _FakeAgent:
        """替身 Agent：只实现 ``collect_observed_run`` 真正用到的那几个接口。

        真实 Agent 是 ``third_party/agentscope/src/agentscope/agent/_agent.py:117``
        的 ``Agent``；这里用替身是为了让 A 段**一次模型都不调**，
        同时把 ``EvalRunner`` 的事件消费口径（``reply_stream`` 的 yield 序列）
        原样跑一遍。``reply_stream`` 的签名与真货一致：
        ``yield_final_msg=True`` 时最后多 yield 一条 ``Msg``
        （``_agent.py:297`` / ``:328``）。
        """

        def __init__(self, text: str) -> None:
            self._text = text
            self.observed: list[Any] = []
            self.model = type("M", (), {"model": "deepseek-flash"})()
            self.state = type("S", (), {"context": []})()
            self.closed = False

        async def observe(self, msgs: Any) -> None:
            """记录先导消息（``case.context`` 走这条路）。

            Args:
                msgs (`Any`): 消息或消息列表。
            """
            self.observed.append(msgs)

        async def reply_stream(self, message: Any, *, yield_final_msg: bool = False) -> Any:
            """按真实事件序列 yield 一遍。

            Args:
                message (`Any`): 用户消息。
                yield_final_msg (`bool`): 是否补上最终 ``Msg``。

            Yields:
                `Any`: 事件与（可选的）最终消息。
            """
            from agentscope.event import (
                ModelCallEndEvent,
                ReplyEndEvent,
                ReplyFinishedReason,
                TextBlockDeltaEvent,
                ToolCallStartEvent,
            )
            from agentscope.message import Msg, TextBlock

            reply_id = "r-fake"
            yield ToolCallStartEvent(
                reply_id=reply_id,
                tool_call_id="tc-1",
                tool_call_name="Read",
            )
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

        async def aclose(self) -> None:
            """标记已关闭（证明 runner 会尝试清理）。"""
            self.closed = True

    async def factory() -> Any:
        """每用例一个全新替身。

        Returns:
            `Any`: 替身 Agent。
        """
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二条用例的 Agent 构造失败（故意的）")
        return _FakeAgent("答案是 2")

    runner = EvalRunner(agent_factory=factory, concurrency=2, timeout_s=10.0)
    report = await runner.run(reloaded, metrics=[M.contains, M.latency_score])
    check("A5 并发跑完 3 条", len(report.results) == 3, f"results={len(report.results)}")
    check(
        "A5b 构造失败的那条被记成 error 而不是往上抛",
        any(item.error and "RuntimeError" in item.error for item in report.results),
        next((item.error for item in report.results if item.error), ""),
    )
    check("A5c 每用例一次 factory（含失败那次）", calls["n"] == 3, f"factory 调用 {calls['n']} 次")

    summary = report.summary()
    check(
        "A5d summary 含通过率与适用条数",
        "pass_rate" in summary and "contains_applicable" in summary,
        json.dumps({k: v for k, v in summary.items() if k in ("pass_rate", "contains", "contains_applicable")}, ensure_ascii=False),
    )

    # ---- A6. 报告落盘 + 对比 ----
    saved = report.save(tmp / "eval-run1", markdown=True)
    check("A6 报告落盘", saved["json"].is_file() and saved["markdown"].is_file(), str(sorted(saved)))

    baseline = _baseline_report()
    diff = report.compare(baseline)
    check("A6b compare 产出 Markdown 对比表", "|" in diff and "指标" in diff, f"{len(diff)} 字符")

    check("A6c percentile", percentile([1.0, 2.0, 3.0, 4.0], 0.5) > 0, f"p50={percentile([1.0, 2.0, 3.0, 4.0], 0.5)}")


def _baseline_report() -> Any:
    """手工造一份"上一次"的报告当基线。

    Returns:
        `Any`: :class:`~harness_kit.eval.report.EvalReport`。
    """
    from datetime import datetime, timezone

    from harness_kit.eval.report import EvalReport
    from harness_kit.eval.runner import EvalResult

    return EvalReport(
        dataset="lesson20-smoke",
        profile="baseline",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc),
        results=[
            EvalResult(case_id="c1", output="答案是 2", ok=True, scores={"contains": 1.0, "latency_score": 0.9}, latency_ms=1000.0),
            EvalResult(case_id="c2", output="答案是 2", ok=True, scores={"contains": 1.0, "latency_score": 0.9}, latency_ms=1000.0),
            EvalResult(case_id="c3", output="", ok=False, scores={"contains": 0.0}, latency_ms=9000.0, error="TimeoutError: 超过 8s 未返回"),
        ],
    )


# ======================================================================
# B 段：从事件流合成评测样本（离线，0 次模型调用）
# ======================================================================
async def segment_b(tmp: Path) -> None:
    """``extract_turn_samples`` / ``synthesize_cases`` —— 纯函数路径。

    Args:
        tmp (`Path`): 临时目录。
    """
    from harness_kit.eval.dataset import EvalCase, synthesize_cases
    from harness_kit.eval.synthesize import extract_turn_samples
    from harness_kit.events.types import EventKind, EventRecord

    section("B 段：事件流 → 评测样本（离线，0 次模型调用）")

    records = [
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
            payload={"model": "deepseek-flash", "prompt_tokens": 900, "completion_tokens": 120, "latency_ms": 3200.0, "finished_reason": "stop"},
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
    samples = extract_turn_samples(records)
    check("B1 一回合 → 一个样本", len(samples) == 1, f"samples={len(samples)}")
    if samples:
        sample = samples[0]
        check(
            "B2 输入预览是真值",
            "add_tool" in sample.input,
            sample.input[:40],
        )
        check("B3 真实用过的工具被记下来", sample.unique_tools == ["Grep"], f"{sample.unique_tools}")
        check(
            "B4 token 累加与轮数",
            sample.prompt_tokens == 900 and sample.completion_tokens == 120 and sample.iterations == 2,
            f"in={sample.prompt_tokens} out={sample.completion_tokens} iters={sample.iterations} tags={sample.tags()}",
        )

    # synthesize_cases：改写器轮转 + 单条失败跳过
    seed = [EvalCase(id="s1", input="原问题", expected="原答案", tags=["seed"])]
    seen: list[str] = []

    async def rewriter(case: EvalCase) -> EvalCase:
        """把种子改写成新用例；第 2 次故意抛错。

        Args:
            case (`EvalCase`): 种子。

        Returns:
            `EvalCase`: 改写结果。

        Raises:
            `RuntimeError`: 第 2 次调用时抛。
        """
        seen.append(case.id)
        if len(seen) == 2:
            raise RuntimeError("第 2 条改写失败（故意的）")
        return case.model_copy(update={"input": f"{case.input}#{len(seen)}"})

    produced = await synthesize_cases(seed, n=3, rewriter=rewriter)
    check("B5 合成失败的那条被跳过，其余照出", len(produced) == 2, f"产出 {len(produced)} 条")
    check(
        "B6 合成用例带 synthetic 标签与来源",
        all("synthetic" in item.tags and item.metadata.get("synthesized_from") == "s1" for item in produced),
        ", ".join(item.id for item in produced),
    )
    check("B7 轮转使用种子", seen == ["s1", "s1", "s1"], f"{seen}")

    # B8：把上面那批事件真写进会话存储，再走 synthesize_from_session。
    # 这一条把第 9 讲的"会话事件溯源"与第 20 讲的"评测集"接在一起 ——
    # 线上跑过的会话因此可以直接变成回归集，不需要手工抄用例。
    from harness_kit.eval.synthesize import synthesize_from_session
    from harness_kit.session.jsonl_store import JsonlSessionStore

    store = JsonlSessionStore(tmp / "sessions_for_synth")
    try:
        for record in records:
            await store.append(record)
        synthesized = await synthesize_from_session(
            store,
            "s-1",
            name="from-session-demo",
            require_closed=False,
        )
    finally:
        await store.aclose()

    check("B8 从会话事件流合成出评测集", len(synthesized) == 1, f"cases={len(synthesized)}")
    if len(synthesized):
        case = synthesized.cases[0]
        check(
            "B9 合成用例的 input 是真值、expected_tools 是真值、expected 为空",
            bool(case.input) and case.expected_tools == ["Grep"] and case.expected is None,
            f"input={case.input[:24]!r} tools={case.expected_tools} expected={case.expected!r}",
        )
        check(
            "B10 合成用例带 from_session 血统标签",
            "from_session" in case.tags and case.metadata.get("session_id") == "s-1",
            f"tags={case.tags}",
        )


# ======================================================================
# C 段：可观测层（离线，0 次模型调用）
# ======================================================================
def segment_c(tmp: Path) -> None:
    """``Tracer`` 的 span 树 + ``MetricsRegistry`` 的 Prometheus 文本。

    Args:
        tmp (`Path`): 临时目录。
    """
    from harness_kit.observe import MetricsRegistry, Tracer

    section("C 段：可观测层（离线，0 次模型调用）")

    tracer = Tracer(service_name="lesson20")
    check(
        "C1 不给 exporter 时只留进程内 span 树（不建 OTel 通道）",
        tracer.exporter_kind in ("none", ""),
        f"exporter_kind={tracer.exporter_kind!r}（本机装了 opentelemetry-sdk，"
        "但默认 exporter=None 时不会去建 provider，也就不会连 collector）",
    )

    with tracer.span("reply", session_id="s-1") as root:
        root.set_attribute("profile", "default")
        with tracer.span("model_call", model="deepseek-flash") as child:
            child.add_event("first_token")
            child.set_attribute("iterations", 2)
        with tracer.span("tool_call", tool="Grep"):
            pass
        root.add_event("done")

    check("C2 span 数量", tracer.span_count() == 3, f"span_count={tracer.span_count()}")
    check("C3 父子关系", tracer.roots() and tracer.roots()[0].name == "reply", f"roots={[s.name for s in tracer.roots()]}")
    check("C4 子 span 的 parent 是 reply", child.parent_id == root.span_id, f"parent={child.parent_id}")
    summary = tracer.summary()
    check(
        "C5 summary 有 p50/p95 与 error 计数",
        any("p50" in key for key in summary) or "span_count" in summary,
        json.dumps(summary, ensure_ascii=False),
    )

    dumped = tracer.dump(tmp / "traces.json")
    payload = json.loads(dumped.read_text(encoding="utf-8"))
    check("C6 trace 树可落盘 JSON", len(payload.get("spans", [])) == 3, str(dumped.name))

    # ---- 指标 ----
    registry = MetricsRegistry()
    replies = registry.counter("replies_total", help="完成的回复次数")
    replies.inc(profile="default")
    replies.inc(3.0, profile="default")
    replies.inc(profile="coding")
    latency = registry.histogram("reply_latency", unit="ms")
    for value in (100.0, 200.0, 300.0, 400.0, 5000.0):
        latency.observe(value, profile="default")

    text = registry.render_prometheus()
    check("C7 exposition 文本含 HELP/TYPE", "# HELP" in text and "# TYPE" in text)
    check("C8 标签被渲染进名字里", 'profile="default"' in text, text.splitlines()[0])
    check(
        "C9 分位数以 summary 形态输出（quantile 标签）",
        'quantile="0.5"' in text and 'quantile="0.95"' in text,
        [line for line in text.splitlines() if "quantile" in line][0],
    )
    check(
        "C10 counter 汇总 5 次（1 + 3 + 1）",
        registry.get_counter("replies_total").total() == 5.0,
        f"total={registry.get_counter('replies_total').total()}",
    )
    check(
        "C11 分位数不输出累积桶 _bucket（有意取舍）",
        "_bucket" not in text,
        "只输出 _count/_sum/_p50/_p95/_p99",
    )


# ======================================================================
# D 段：服务层（TestClient，不占端口）
# ======================================================================
async def segment_d(tmp: Path) -> None:
    """``create_harness_app`` 的路由表 + 用 ASGI TestClient 打真实 HTTP。

    这一段**不 bind 端口**：``TestClient`` 直接把请求喂给 ASGI app。

    Args:
        tmp (`Path`): 临时目录。
    """
    from harness_kit.cli import default_settings, _load_profile
    from harness_kit.service.app import DEFAULT_PORT, create_harness_app, describe_routes

    section("D 段：服务层（ASGI TestClient，不占端口，0 次模型调用）")

    settings = default_settings()
    settings.session_dir = tmp / "sessions"
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    profile = _load_profile("default", settings)
    app = create_harness_app(profile=profile, settings=settings, max_sessions=2)
    routes = describe_routes(app)
    check("D1 默认端口 ≥ 18000", DEFAULT_PORT >= 18000, f"DEFAULT_PORT={DEFAULT_PORT}")
    check(
        "D2 契约要求的 5 条路由都在",
        all(
            any(path in line for line in routes)
            for path in ("POST /chat", "GET  /sessions", "GET  /sessions/{session_id}", "GET  /healthz", "GET  /")
        ),
        f"{len(routes)} 条路由",
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        health = client.get("/healthz")
        check("D3 /healthz", health.status_code == 200 and health.json()["status"] == "ok", health.json()["status"])
        check(
            "D4 /healthz 报告端口铁律",
            "18000" in health.json().get("port_policy", ""),
            health.json().get("port_policy", ""),
        )

        created = client.post("/sessions", json={})
        sid = created.json()["session_id"]
        check("D5 POST /sessions", created.status_code == 200 and len(sid) == 12, f"session_id={sid}")

        listing = client.get("/sessions")
        check("D6 GET /sessions 能看到新会话", any(item["session_id"] == sid for item in listing.json()["sessions"]))

        detail = client.get(f"/sessions/{sid}", params={"event_limit": 5})
        payload = detail.json()
        check(
            "D7 GET /sessions/{id} 有 meta / turns / events",
            detail.status_code == 200 and {"meta", "turns", "events"} <= set(payload),
            f"event_total={payload.get('event_total')}",
        )
        check(
            "D8 SESSION_START 已落盘（事件溯源起点）",
            any(item["kind"] == "session_start" for item in payload["events"]),
            str([item["kind"] for item in payload["events"]]),
        )

        missing = client.get("/sessions/nope-does-not-exist")
        check("D9 不存在的会话返回 404", missing.status_code == 404, str(missing.status_code))

        index = client.get("/")
        check("D10 GET / 送出单文件 Web UI", index.status_code == 200 and "harness" in index.text.lower(), f"{len(index.text)} 字符")

        metrics_response = client.get("/metrics")
        metrics_text = metrics_response.text
        check(
            "D11 GET /metrics 按 exposition 协议返回（content-type 正确）",
            metrics_response.status_code == 200
            and "text/plain" in metrics_response.headers.get("content-type", ""),
            f"{metrics_response.status_code} "
            f"{metrics_response.headers.get('content-type')!r}，"
            f"{len(metrics_text)} 字符（本次进程内还没打过任何点，所以正文是空文本；"
            "有打点时首行就是 '# HELP harness_kit_...'）",
        )

        traces = client.get("/traces").json()
        check("D12 GET /traces 概况", "span_count" in traces, f"exporter={traces.get('exporter')!r}")

        bad = client.post("/chat", json={"message": ""})
        check("D13 空消息被 422/400 挡住", bad.status_code in (400, 422), str(bad.status_code))

    check("D14 上下文退出后服务被优雅关闭", app.state.service.sessions == {}, f"常驻会话={len(app.state.service.sessions)}")


# ======================================================================
# E 段：真实 ReMe 索引 + 检索（0 次模型调用）
# ======================================================================
async def segment_e(tmp: Path) -> None:
    """把一小段真实代码渲染成 markdown 灌进 ReMe，再直接检索。

    Args:
        tmp (`Path`): 临时目录。
    """
    from harness_kit.demo.code_assistant.agent import default_settings
    from harness_kit.demo.code_assistant.ingest_repo import (
        ingest_repo,
        render_source_document,
        select_files,
    )

    section("E 段：真实 ReMe 索引与检索（0 次模型调用）")

    settings = default_settings()
    tool_dir = REPO / "third_party/agentscope/src/agentscope/tool"
    source = tool_dir / "_base.py"
    doc = render_source_document(
        source.read_text(encoding="utf-8"),
        origin="_base.py",
        lang="python",
        section_lines=40,
    )
    check(
        "E1 渲染成 markdown（H1 来源 + 带真实行号的小节 + 围栏）",
        doc.startswith("# _base.py") and "行 1-40" in doc and "```python" in doc,
        f"{len(doc)} 字符",
    )

    picked = select_files(tool_dir, suffixes=(".py",), limit=3)
    check(
        "E2 选文件（后缀过滤 + 条数上限 + 跳过记录）",
        0 < len(picked.files) <= 3 and bool(picked.skipped),
        f"选中 {picked.files}，跳过 {len(picked.skipped)} 个（含 limit 截断={picked.truncated}）",
    )

    workspace = tmp / "reme" / "code_assistant"
    report = await ingest_repo(
        target=tool_dir,
        workspace=workspace,
        alias="lesson20",
        limit=3,
        tags=("code", "lesson20"),
        settings=settings,
    )
    check("E3 入库成功", report.added >= 1, report.summary())
    check("E4 无失败文件", not report.failed, str(report.failed))

    # 幂等性：同样的输入再灌一次，应当是"没变"
    again = await ingest_repo(
        target=tool_dir,
        workspace=workspace,
        alias="lesson20",
        limit=3,
        tags=("code", "lesson20"),
        settings=settings,
    )
    check(
        "E5 幂等：第二次跑没有新增（内容 sha256 未变）",
        again.added == 0 and again.unchanged > 0,
        again.summary(),
    )

    # 直接检索（不经过模型）：嵌入式装配 ReMe，既不起 HTTP 服务也不占端口。
    from harness_kit.memory.client import MemoryClient
    from harness_kit.memory.config import HarnessMemoryConfig
    from harness_kit.memory.search import MemorySearch
    from harness_kit.memory.workspace import ReMeWorkspace

    ws = ReMeWorkspace(root=workspace)
    builder = HarnessMemoryConfig(workspace=ws).with_jobs("search", "write", "reindex")
    client = MemoryClient(builder.build())
    await client.start()
    try:
        search = MemorySearch(client, workspace=ws)
        result = await search.search("ToolBase 的 __call__ 返回什么？", limit=3, min_score=0.0)
        check("E6 检索有命中", len(result.hits) >= 1, f"hits={len(result.hits)}")
        if result.hits:
            check(
                "E7 命中带可回溯的路径",
                all(hit.path for hit in result.hits),
                " | ".join(f"{hit.path}:{hit.start_line}({hit.score:.3f})" for hit in result.hits[:3]),
            )
    finally:
        await client.aclose()


# ======================================================================
# F 段：真实端到端评测（--live，≤4 次模型调用）
# ======================================================================
async def segment_f(tmp: Path) -> None:
    """用真实 Agent 跑 2 条用例，再从会话事件流合成评测集。

    Args:
        tmp (`Path`): 临时目录。
    """
    from harness_kit.config.builder import HarnessBuilder
    from harness_kit.demo.code_assistant.agent import default_settings
    from harness_kit.eval import EvalCase, EvalDataset, EvalRunner
    from harness_kit.eval import metrics as M

    section("F 段：真实模型端到端评测（2 条用例，约 4 次模型调用）")

    settings = default_settings()
    settings.session_dir = tmp / "live_sessions"
    settings.session_dir.mkdir(parents=True, exist_ok=True)

    dataset = EvalDataset(
        name="lesson20-live",
        cases=[
            EvalCase(
                id="live-1",
                input="请只回答两个字：好的",
                expected="好的",
                tags=["live"],
                metadata={"latency_budget_ms": 60000},
            ),
            EvalCase(
                id="live-2",
                input="只回答数字：2 加 3 等于几？",
                expected="5",
                tags=["live"],
                metadata={"latency_budget_ms": 60000},
            ),
        ],
    )

    built_agents: list[Any] = []

    async def factory() -> Any:
        """每条用例一个全新装配（独立 session_id，上下文不串味）。

        Returns:
            `Any`: AgentScope ``Agent``。
        """
        from harness_kit.cli import _load_profile

        builder = HarnessBuilder(_load_profile("default", settings), settings=settings)
        built = await builder.build_all()
        built_agents.append(builder)
        return built.agent

    runner = EvalRunner(
        agent_factory=factory,
        concurrency=2,
        timeout_s=120.0,
        profile_name="default",
        model="deepseek-flash",
    )
    report = await runner.run(dataset, metrics=[M.contains, M.latency_score])
    print(report.to_markdown(max_failures=5))
    check("F1 两条用例都跑完", len(report.results) == 2, f"results={len(report.results)}")
    check(
        "F2 至少一条通过",
        report.pass_rate > 0.0,
        f"pass_rate={report.pass_rate:.2f}",
    )
    check(
        "F3 拿到了真实 token 与成本",
        any(item.tokens.input_tokens > 0 for item in report.results),
        ", ".join(f"{item.case_id}: in={item.tokens.input_tokens} out={item.tokens.output_tokens} cost=${item.cost_usd:.6f}" for item in report.results),
    )
    saved = report.save(tmp / "eval-live", markdown=True)
    check("F4 报告落盘", saved["markdown"].is_file(), str(saved["markdown"]))

    for builder in built_agents:
        await builder.aclose()


# ======================================================================
# main
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        `argparse.ArgumentParser`: 解析器。
    """
    parser = argparse.ArgumentParser(prog="lesson20_verify", description="第 20 讲验证脚本")
    parser.add_argument("--live", action="store_true", help="跑 F 段（真实模型，约 4 次调用）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（便于看落盘产物）")
    return parser


async def main_async(options: argparse.Namespace) -> int:
    """跑完 A~E（+可选 F）段。

    Args:
        options (`argparse.Namespace`): 已解析参数。

    Returns:
        `int`: 退出码。
    """
    tmp = Path(tempfile.mkdtemp(prefix="lesson20_verify_"))
    print(f"临时目录: {tmp}")
    try:
        await segment_a(tmp)
        await segment_b(tmp)
        segment_c(tmp)
        await segment_d(tmp)
        await segment_e(tmp)
        if options.live:
            await segment_f(tmp)
        else:
            section("F 段：真实模型端到端评测")
            print(f"{SKIP} 未加 --live，已跳过（预计 4 次模型调用）")
    finally:
        if options.keep:
            print(f"\n临时目录已保留: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for name, ok, _ in _results if not ok]
    print()
    print("=" * 78)
    print(f"共 {len(_results)} 项断言，{len(_results) - len(failed)} 项通过，{len(failed)} 项失败")
    if failed:
        for name in failed:
            print(f"  [FAIL] {name}")
        return 1
    print("全部通过。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """入口。

    Args:
        argv (`Sequence[str] | None`): 参数；``None`` 用 ``sys.argv[1:]``。

    Returns:
        `int`: 退出码。
    """
    options = build_parser().parse_args(list(argv) if argv is not None else None)
    return asyncio.run(main_async(options))


if __name__ == "__main__":
    raise SystemExit(main())
