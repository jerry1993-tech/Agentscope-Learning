# -*- coding: utf-8 -*-
"""``harness-kit`` 命令行（契约 §3.20 的第 20 讲交付物）。

**它是这一层的"总闸门"**：前面 19 讲交付的模块都能被单独 import，但把
"装配 → 对话 → 体检 → 评测 → 服务 → 回放 → 记忆"串成一条**可复现的命令**，
才是运营层该有的样子 —— 教程里的每一条验证命令都直接引这里的子命令，
读者照着敲就能复现。

子命令与契约 §3.20 的关系（契约只列了 ``run`` / ``doctor`` / ``eval`` /
``serve`` / ``profile explain``，任务书另外要求 ``replay`` / ``memory``，
``feedback`` 是第 20 讲 §7 补遗新增的能力 —— 契约没要求，见那一节的说明）：

=============== ==========================================================
子命令          做什么
=============== ==========================================================
``run``        跑一次对话；``chat`` 是它的别名。``--stream`` 逐字打印
``doctor``     体检：Profile 能不能装、Agent 装出来长什么样、记忆配置
               能不能被 ReMe 校验（``--memory`` 深挖到 :class:`MemoryDoctor`）
``eval``       跑评测：``--dataset`` 读 jsonl，或 ``--from-session`` 从
               会话事件流合成用例；产出 ``report.json`` + ``report.md``
``serve``      起 HTTP 服务（默认 ``127.0.0.1:18420``，铁律：≥ 18000）
``replay``     回放一个会话的事件流：回合骨架 / token / 工具调用 / 错误
``memory``     ``search`` / ``ingest`` / ``stats`` / ``doctor`` 四个动作
``profile``    ``list`` 列出全部 Profile，``explain`` 打印合并来源
``feedback``   生产反馈闭环的**挖掘半程**（第 20 讲 §7 补遗）：扫失败
               会话 → 建回归集 → 落只追加台账；判定交给 ``eval`` + 闸门
=============== ==========================================================

**``repo_root`` 是这个文件最需要解释的一处设计。** ``Settings.resolve()``
把相对路径锚在 ``repo_root`` 上，而 ``repo_root`` 的默认值是
``_REPO_ROOT_FALLBACK``（``settings.py`` 的模块常量，指向本仓库根）。本仓库的
``harness_kit`` 包在 ``tutorial_agsc_reme/reference/`` 下面，Profile 里写的
``./harness_kit/profiles``、``./harness_kit/permission/rules/research.yaml``、
``./.harness/...`` 都是**相对 reference 目录**的。所以这里默认把 ``repo_root``
钉到 ``cli.py`` 的父目录（也就是 reference），并允许用环境变量
``HARNESS_REPO_ROOT`` 覆盖 —— 部署到别处时目录结构会变，这条必须可改。

**``asyncio.run()`` 出现在这里是被契约明确允许的**
（``_contract.md`` 第七章第 2 条：只允许出现在 ``scripts/`` 与 ``cli.py``）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

__all__ = ["main", "build_parser", "default_settings", "REFERENCE_ROOT"]

#: ``tutorial_agsc_reme/reference``（= ``harness_kit`` 包的**父目录**）。
#: Profile 里的相对路径（``./harness_kit/profiles``、``./.harness/...``）
#: 全部以它为基准，见模块 docstring。
#:
#: 注意这里是 ``parent.parent`` 而不是 ``parent``：``__file__`` 指向
#: ``.../reference/harness_kit/cli.py``，``parent`` 是包目录本身，
#: 以此为 ``repo_root`` 会把 ``./harness_kit/profiles`` 解析成
#: ``reference/harness_kit/harness_kit/profiles``（实测：一条 Profile 都找不到）。
REFERENCE_ROOT: Path = Path(__file__).resolve().parent.parent

#: 覆盖 ``repo_root`` 的环境变量名。
REPO_ROOT_ENV: str = "HARNESS_REPO_ROOT"


# ======================================================================
# 公共小工具
# ======================================================================
def default_settings(**overrides: Any) -> Any:
    """构造锚定到本 reference 目录的 :class:`~harness_kit.settings.Settings`。

    为什么不是裸 ``Settings.from_env()``：那样 ``repo_root`` 是仓库根，
    ``./harness_kit/profiles`` 会被解析到 ``<repo>/harness_kit/profiles``
    （不存在），Profile 一个也找不到。

    Args:
        **overrides (`Any`): 透传给 :meth:`Settings.from_env` 的覆盖项，
            优先级最高。

    Returns:
        `Settings`: 完成 ``.env`` 装载与目录创建的设置对象。
    """
    from harness_kit.settings import Settings

    root = os.environ.get(REPO_ROOT_ENV) or str(REFERENCE_ROOT)
    extra = dict(overrides)
    extra.setdefault("repo_root", Path(root).resolve())
    return Settings.from_env(**extra)


def _profile_search_dir(settings: Any) -> Path:
    """Profile 搜索目录（``settings.profile_dir`` 锚定后的绝对路径）。

    Args:
        settings (`Any`): :class:`~harness_kit.settings.Settings`。

    Returns:
        `Path`: 绝对路径。
    """
    return Path(settings.resolve(settings.profile_dir))


def _load_profile(name: str, settings: Any) -> Any:
    """装载一个已解析（合并完毕）的 Profile。

    Args:
        name (`str`): Profile 名（不含 ``.yaml``）。
        settings (`Any`): 设置对象。

    Returns:
        `ResolvedProfile`: 冻结后的 Profile。

    Raises:
        `FileNotFoundError`: 名字在搜索目录里找不到。
    """
    from harness_kit.config.loader import load_resolved_profile

    return load_resolved_profile(name, search_dir=_profile_search_dir(settings))


def _configure_logging(level: str) -> None:
    """把 loguru 的输出级别调到 ``level``（默认 INFO）。

    库代码里**不**配置 sink（谁的程序谁负责），只有 CLI 这个进程入口配。

    Args:
        level (`str`): 级别名，大小写无所谓。
    """
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), enqueue=False)


def _print_json(payload: Any) -> None:
    """打印一份 JSON（``ensure_ascii=False``，中文不转义）。

    Args:
        payload (`Any`): 可被 ``json.dumps`` 序列化的对象。
    """
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _user_msg(text: str) -> Any:
    """把纯文本包成 AgentScope ``Msg``。

    Args:
        text (`str`): 用户输入。

    Returns:
        `Any`: ``Msg``。
    """
    from agentscope.message import Msg, TextBlock

    return Msg(name="user", role="user", content=[TextBlock(type="text", text=text)])


def _reply_text(reply: Any) -> str:
    """从 ``Msg`` 里取纯文本（取不到就退回 ``str()``）。

    Args:
        reply (`Any`): ``agent.reply`` 的返回值。

    Returns:
        `str`: 文本内容。
    """
    getter = getattr(reply, "get_text_content", None)
    if callable(getter):
        text = getter()
        if text:
            return str(text)
    return str(getattr(reply, "content", reply))


async def _close_builder(builder: Any) -> None:
    """尽力关闭一个 :class:`HarnessBuilder`（含其记忆客户端）。

    Args:
        builder (`Any`): 装配器。
    """
    closer = getattr(builder, "aclose", None)
    if closer is None:
        closer = getattr(builder, "close", None)
    if closer is None:
        return
    try:
        outcome = closer()
        if asyncio.iscoroutine(outcome):
            await outcome
    except Exception as exc:  # noqa: BLE001 - 关闭失败不该让命令返回非 0
        logger.warning("关闭装配器失败: {}", exc)


# ======================================================================
# run / chat
# ======================================================================
async def _run_once(options: argparse.Namespace) -> int:
    """``harness-kit run "你好"``：装一个 Profile，跑一次对话。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: 进程退出码（``0`` 成功 / ``1`` 失败）。
    """
    from harness_kit.config.builder import HarnessBuilder

    settings = default_settings()
    profile = _load_profile(options.profile, settings)
    builder = HarnessBuilder(profile, settings=settings, session_id=options.session_id)
    try:
        built = await builder.build_all()
        prompt = " ".join(options.message)
        if not prompt.strip():
            print("错误：没有输入。用法：harness-kit run \"你的问题\"", file=sys.stderr)
            return 1

        if options.stream:
            chunks: list[str] = []
            stream = built.agent.reply_stream(_user_msg(prompt))
            async for chunk in stream:
                text = getattr(chunk, "delta", None)
                if text:
                    chunks.append(str(text))
                    sys.stdout.write(str(text))
                    sys.stdout.flush()
            if chunks:
                sys.stdout.write("\n")
            answer = "".join(chunks)
        else:
            reply = await built.agent.reply(_user_msg(prompt))
            answer = _reply_text(reply)
            print(answer)

        if options.json:
            _print_json(
                {
                    "profile": profile.name,
                    "agent": profile.agent.name,
                    "session_id": built.session_id,
                    "input": prompt,
                    "output": answer,
                    "state": {
                        "context_len": len(built.agent.state.context),
                        "session_id": built.agent.state.session_id,
                    },
                },
            )
        return 0
    finally:
        await _close_builder(builder)


# ======================================================================
# doctor
# ======================================================================
async def _doctor(options: argparse.Namespace) -> int:
    """``harness-kit doctor``：Profile 装配体检（可选记忆深挖）。

    检查表刻意"逐项带结论"而不是"一个 ok/not ok"：装配失败在这套栈里
    大多**不是异常而是静默降级**（技能没生效、记忆没接线、Docker 策略退回
    本地），只有把每一项单独判一遍才看得见。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: ``0`` 表示所有检查项都通过，``1`` 表示有失败项。
    """
    from harness_kit.config.builder import HarnessBuilder, summarize_agent
    from harness_kit.registry import HarnessRegistry

    settings = default_settings()
    checks: list[tuple[str, bool, str]] = []

    registry = HarnessRegistry.default()
    snapshot = registry.snapshot()
    checks.append(
        (
            "registry",
            bool(snapshot.get("middleware")) and bool(snapshot.get("model")),
            f"model={snapshot.get('model')} middleware={snapshot.get('middleware')}",
        ),
    )

    search_dir = _profile_search_dir(settings)
    checks.append(("profile_dir", search_dir.is_dir(), str(search_dir)))

    names = sorted(path.stem for path in search_dir.glob("*.yaml"))
    checks.append(("profiles", bool(names), ", ".join(names)))

    for name in options.profiles or [options.profile]:
        try:
            profile = _load_profile(name, settings)
        except Exception as exc:  # noqa: BLE001 - 体检要把失败变成一行
            checks.append((f"profile[{name}]", False, f"{type(exc).__name__}: {exc}"))
            continue
        builder = HarnessBuilder(profile, settings=settings, session_id=f"doctor-{name}")
        try:
            built = await builder.build_all()
            summary = summarize_agent(built.agent)
            checks.append(
                (
                    f"profile[{name}]",
                    True,
                    f"{summary['model']}/{summary['model_name']} "
                    f"perms={summary['permission_mode']} tools={len(summary['tools'])} "
                    f"middlewares={[type(m).__name__ for m in built.middlewares]}",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                (f"profile[{name}]", False, f"{type(exc).__name__}: {exc}"),
            )
        finally:
            await _close_builder(builder)

    if options.memory:
        checks.extend(await _memory_checks(settings, options.profile))

    failed = [name for name, ok, _ in checks if not ok]
    width = max(len(name) for name, _, _ in checks)
    print("harness-kit doctor")
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else '!!'}] {name.ljust(width)}  {detail}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 项通过")
    return 1 if failed else 0


async def _memory_checks(settings: Any, profile_name: str) -> list[tuple[str, bool, str]]:
    """记忆子系统的体检项（``--memory``）。

    直接用第 15 讲的 :class:`~harness_kit.memory.doctor.MemoryDoctor`
    （它不复用 Profile 里 memory 为 ``false`` 的情形，故先判 enabled）。

    Args:
        settings (`Any`): 设置对象。
        profile_name (`str`): Profile 名。

    Returns:
        `list[tuple[str, bool, str]]`: ``(项名, 是否通过, 说明)`` 列表。
    """
    from harness_kit.memory.config import HarnessMemoryConfig
    from harness_kit.memory.doctor import MemoryDoctor

    results: list[tuple[str, bool, str]] = []
    try:
        profile = _load_profile(profile_name, settings)
        spec = profile.memory
        if not getattr(spec, "enabled", False):
            return [("memory.enabled", True, f"{profile_name} 未启用记忆（跳过深挖）")]
        config = HarnessMemoryConfig.from_spec(spec, settings=settings).build()
        for item in MemoryDoctor(config).check():
            results.append((f"memory.{item.name}", item.ok, item.detail))
    except Exception as exc:  # noqa: BLE001
        results.append(("memory", False, f"{type(exc).__name__}: {exc}"))
    return results


# ======================================================================
# eval
# ======================================================================
async def _eval(options: argparse.Namespace) -> int:
    """``harness-kit eval``：跑评测并产出报告。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: ``0`` 表示全部用例通过，``1`` 表示有失败或参数有问题。
    """
    from harness_kit.config.builder import HarnessBuilder
    from harness_kit.eval import EvalDataset, EvalRunner
    from harness_kit.eval import metrics as metrics_module

    settings = default_settings()
    profile = _load_profile(options.profile, settings)

    dataset: Any
    if options.dataset:
        dataset = EvalDataset.from_jsonl(Path(options.dataset))
    elif options.from_session:
        from harness_kit.session import JsonlSessionStore
        from harness_kit.eval.synthesize import synthesize_from_session

        store = JsonlSessionStore(Path(settings.resolve(settings.session_dir)))
        try:
            dataset = await synthesize_from_session(
                store,
                options.from_session,
                name=f"session-{options.from_session[:8]}",
            )
        finally:
            await store.aclose()
    else:
        print(
            "错误：需要 --dataset <jsonl> 或 --from-session <session_id>。\n"
            "  提示：harness-kit replay --session <id> 可以先看看会话里有什么。",
            file=sys.stderr,
        )
        return 1

    if options.limit:
        dataset = dataset.head(options.limit)
    if not len(dataset):
        print("错误：数据集为空。", file=sys.stderr)
        return 1

    async def factory() -> Any:
        """每条用例一个**全新**装配（含独立 session_id）。

        Returns:
            `Any`: 装好的 Agent。
        """
        builder = HarnessBuilder(profile, settings=settings)
        built = await builder.build_all()
        return built.agent

    metric_fns = [metrics_module.contains, metrics_module.latency_score]
    if any(case.expected_tools for case in dataset):
        metric_fns.append(metrics_module.tool_call_accuracy)
    if any(case.expected_citations for case in dataset):
        metric_fns.append(metrics_module.citation_coverage)

    runner = EvalRunner(
        agent_factory=factory,
        concurrency=options.concurrency,
        timeout_s=options.timeout,
        profile_name=profile.name,
        model=profile.model.model_name,
    )
    report = await runner.run(dataset, metric_fns)

    print(report.to_markdown())
    out_dir = Path(options.report_dir or settings.resolve("./.harness/eval")).resolve()
    # ``EvalReport.save`` 的参数是"文件名前缀"而不是目录（见 report.py:377）：
    # 传目录会让它写出 ``eval.json`` / ``eval.md``，多跑几次互相覆盖。
    # 这里带上 UTC 时间戳，让每次评测的报告都可追溯。
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    saved = report.save(out_dir / f"eval-{stamp}", markdown=True)
    for kind, path in saved.items():
        print(f"[{kind}] {path}")
    print("\n结论:")
    for line in report.conclusions():
        print(f"  - {line}")
    return 0 if report.pass_rate >= 1.0 else 1


# ======================================================================
# feedback
# ======================================================================
async def _feedback(options: argparse.Namespace) -> int:
    """``harness-kit feedback``：生产反馈闭环的挖掘 + 判定两步。

    **这一步不调模型**，定位是闭环的**前半程（挖掘）**：

    1. 扫 ``.harness/sessions`` 下的会话事件日志，挑出命中失败信号的会话；
    2. 把它们转成回归评测集落盘，并往台账追加"哪个会话 → 哪条用例"。

    判定（跑评测 + :class:`~harness_kit.eval.feedback.RegressionGate`）是
    后半程，需要真跑模型，所以不在本命令里自动做：``--baseline`` 只用来
    载入基线报告并提示下一步命令，避免用户误以为"跑一下 feedback 就等于
    做了回归"。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: ``0`` 正常，``1`` 没有可挖的会话或参数有问题。
    """
    from harness_kit.eval.feedback import (
        DEFAULT_TRIGGERS,
        FAILURE_TRIGGERS,
        append_ledger,
        build_regression_dataset,
        entries_from_decision,
        scan_sessions,
    )
    from harness_kit.session import JsonlSessionStore

    settings = default_settings()

    triggers = list(options.triggers) if options.triggers else list(DEFAULT_TRIGGERS)
    unknown = [name for name in triggers if name not in FAILURE_TRIGGERS]
    if unknown:
        print(
            f"错误：未知的失败信号 {unknown}；可用：{list(FAILURE_TRIGGERS)}",
            file=sys.stderr,
        )
        return 1

    store = JsonlSessionStore(Path(settings.resolve(settings.session_dir)))
    try:
        picks = await scan_sessions(
            store,
            triggers=triggers,
            over_iterations=options.over_iterations,
            min_replies=options.min_replies,
            max_sessions=options.max_sessions,
        )
        if not picks:
            print(
                "没有找到命中的会话。\n"
                f"  扫描目录：{settings.resolve(settings.session_dir)}\n"
                f"  启用信号：{triggers}\n"
                "  提示：会话日志要先有内容 —— 跑几次 `harness-kit run \"...\"`。",
                file=sys.stderr,
            )
            return 1

        print(f"命中 {len(picks)} 个会话：")
        for pick in picks:
            print(
                f"  - {pick.session_id[:16]}  [{pick.reason_text()}]  "
                f"events={pick.n_events} replies={pick.n_replies}",
            )
            for line in pick.evidence:
                print(f"      · {line}")

        dataset, owner = await build_regression_dataset(store, picks)
        print()
        print(f"回归集：{dataset.name}  cases={len(dataset)}  tags={dataset.tags}")
        for case in dataset.cases:
            print(
                f"  - {case.id}  {case.input[:50]!r}  "
                f"<- {case.metadata.get('origin_session')} "
                f"{case.metadata.get('origin_reasons')}",
            )

        out = Path(
            options.out or settings.resolve("./.harness/eval/regression.jsonl"),
        ).resolve()
        dataset.to_jsonl(out)
        print(f"\n回归集已写入：{out}")

        if options.baseline:
            from harness_kit.eval.report import EvalReport

            baseline = EvalReport.from_json(Path(options.baseline).read_text(encoding="utf-8"))
            print(
                f"基线报告：{options.baseline}（数据集 {baseline.dataset}，"
                f"{len(baseline.results)} 条，pass_rate={baseline.pass_rate:.4f}）",
            )
            print(
                "  注意：本命令**不跑**评测。判定请用：\n"
                f"    harness-kit eval --dataset {out}   # 出本次报告\n"
                "  再把两份报告交给 RegressionGate（见第 20 讲 §7.4 的代码）。",
            )

        ledger = Path(
            options.ledger or settings.resolve("./.harness/eval/feedback_ledger.jsonl"),
        ).resolve()
        written = append_ledger(ledger, entries_from_decision(picks, owner, dataset))
        print(f"台账（只追加，未判定）：{ledger}  +{written} 行")

        return 0 if len(dataset) else 1
    finally:
        await store.aclose()


# ======================================================================
# serve
# ======================================================================
async def _serve(options: argparse.Namespace) -> int:
    """``harness-kit serve``：起 HTTP 服务（默认 127.0.0.1:18420）。

    **不自己写事件循环、不自己写 HTTP**：`create_harness_app` 返回的是标准
    ``FastAPI`` 实例，交给 ``uvicorn.Server`` 跑；优雅关闭（Ctrl-C）由
    uvicorn 负责，而 harness 侧的收尾（关闭常驻会话、释放 ReMe 客户端）
    挂在 app 的 ``lifespan`` 上（见 :mod:`harness_kit.service.app`）。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: 退出码。
    """
    import uvicorn

    from harness_kit.service.app import DEFAULT_HOST, DEFAULT_PORT, create_harness_app

    host = options.host or DEFAULT_HOST
    port = int(options.port or DEFAULT_PORT)
    if port < 18000:
        print(
            f"错误：端口 {port} 低于铁律下限 18000（见 _contract.md §3.20）。",
            file=sys.stderr,
        )
        return 1

    settings = default_settings()
    profile = _load_profile(options.profile, settings)
    app = create_harness_app(
        profile=profile,
        settings=settings,
        max_sessions=options.max_sessions,
    )
    print(f"harness-kit serve → http://{host}:{port}  （profile={profile.name}）")
    print("  GET  /            单文件 Web UI")
    print("  POST /chat        SSE 流式对话（stream=false 退化为 JSON）")
    print("  GET  /sessions    会话列表      GET /healthz  健康检查")
    print("  GET  /metrics     Prometheus    Ctrl-C 优雅关闭")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=(options.log_level or "info").lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()
    return 0


# ======================================================================
# replay
# ======================================================================
async def _replay(options: argparse.Namespace) -> int:
    """``harness-kit replay``：回放一个会话的事件流。

    回放**不重新调用模型**：事件流里没有消息体（见
    :mod:`harness_kit.session.replay` 的说明），所以能给出的是"回合骨架 +
    工具调用 + token + 结构性问题"。要重建消息内容必须走快照
    （:func:`~harness_kit.session.replay.context_from_snapshot`）。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: 退出码。
    """
    from harness_kit.session import JsonlSessionStore, SessionReplayer

    settings = default_settings()
    store = JsonlSessionStore(Path(settings.resolve(settings.session_dir)))
    try:
        replayer = SessionReplayer(store)
        result = await replayer.fold(options.session)
        turns = await replayer.turns_from_records(
            await replayer.timeline(options.session),
        )

        if options.json:
            _print_json(
                {
                    "session_id": result.session_id,
                    "event_count": result.event_count,
                    "token_usage": result.token_usage.model_dump(),
                    "tool_calls": result.tool_calls,
                    "errors": result.errors,
                    "turns": [turn.model_dump() for turn in turns],
                },
            )
            return 0

        print(f"会话 {result.session_id}")
        print(f"  事件 {result.event_count} 条 / 回合 {len(turns)} 个")
        print(
            f"  token: in={result.token_usage.input_tokens} "
            f"out={result.token_usage.output_tokens} "
            f"total={result.token_usage.total_tokens}",
        )
        if result.errors:
            print("  结构性问题:")
            for item in result.errors:
                print(f"    - {item}")
        if options.turns:
            print("\n回合:")
            for turn in turns:
                tools = ", ".join(turn.tool_calls) or "无"
                print(
                    f"  seq {turn.start_seq}→{turn.end_seq if turn.end_seq is not None else '未收尾'} "
                    f"iters={turn.iterations} tools=[{tools}] "
                    f"tokens={turn.token_usage.total_tokens}",
                )
                print(f"    输入: {turn.input_preview}")

        if options.export:
            path = Path(options.export).resolve()
            path.write_text(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "turns": [turn.model_dump() for turn in turns],
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print(f"\n已导出到 {path}")
        return 0
    finally:
        await store.aclose()


# ======================================================================
# memory
# ======================================================================
async def _memory(options: argparse.Namespace) -> int:
    """``harness-kit memory <search|ingest|stats|doctor>``。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: 退出码。
    """
    from harness_kit.memory.client import MemoryClient
    from harness_kit.memory.config import HarnessMemoryConfig
    from harness_kit.memory.workspace import ReMeWorkspace

    settings = default_settings()
    profile = _load_profile(options.profile, settings)
    spec = profile.memory
    if not getattr(spec, "enabled", False):
        print(
            f"错误：Profile {profile.name} 的 memory.enabled 为 false；"
            "请用 --profile 指向一个启用记忆的 Profile（例如 researcher_with_memory）。",
            file=sys.stderr,
        )
        return 1

    workspace = ReMeWorkspace(
        root=Path(spec.workspace_root)
        if str(spec.workspace_root).startswith("/")
        else settings.resolve(spec.workspace_root),
    )
    builder = HarnessMemoryConfig.from_spec(spec, settings=settings).build()

    if options.action == "doctor":
        from harness_kit.memory.doctor import MemoryDoctor

        doctor = MemoryDoctor(builder)
        print(doctor.report())
        return 0 if doctor.ok() else 1

    client = MemoryClient(builder)
    await client.start()
    try:
        if options.action == "stats":
            from harness_kit.memory.ingest import MemoryIngestor

            stats = await MemoryIngestor(client, workspace=workspace).stats()
            _print_json({"workspace": str(workspace.root), **stats})
            return 0

        if options.action == "ingest":
            from harness_kit.memory.ingest import MemoryIngestor

            if not options.path:
                print("错误：ingest 需要 --path <文件或目录>。", file=sys.stderr)
                return 1
            target = Path(options.path).resolve()
            ingestor = MemoryIngestor(client, workspace=workspace)
            tags = list(options.tags or []) or None
            if target.is_dir():
                records = await ingestor.add_directory(
                    target,
                    pattern=options.pattern,
                    tags=tags,
                    catalog=spec.catalog,
                )
            else:
                records = [
                    await ingestor.add_file(
                        target,
                        tags=tags,
                        catalog=spec.catalog,
                    ),
                ]
            added = sum(1 for item in records if item.added)
            chunks = sum(item.chunk_count for item in records)
            for item in records:
                flag = "ok" if item.added else "skip"
                print(f"  [{flag}] {item.path} chunks={item.chunk_count} {item.skipped_reason or ''}")
            print(f"\n入库 {added}/{len(records)} 个文件，共 {chunks} 个 chunk")
            print(f"工作区: {workspace.root}")
            return 0 if added else 1

        # search（默认动作）
        if not options.query:
            print("错误：search 需要 --query <查询串>。", file=sys.stderr)
            return 1
        from harness_kit.memory.search import MemorySearch

        result = await MemorySearch(client, workspace=workspace).search(
            options.query,
            limit=options.limit,
            min_score=options.min_score,
        )
        if options.json:
            _print_json(result.model_dump())
            return 0
        print(f"query: {result.query!r}  counts={result.counts}  hybrid={result.hybrid}")
        if not result.hits:
            print("  （0 条命中：确认目标文件已经 ingest 过 —— reindex 只重建已入库的 chunk）")
            return 1
        for index, hit in enumerate(result.hits, start=1):
            print(f"  [{index}] score={hit.score:.4f} source={hit.source} {hit.path}:{hit.start_line}")
            preview = hit.text.strip().replace("\n", " ")[:120]
            print(f"      {preview}")
        return 0
    finally:
        await client.aclose()


# ======================================================================
# profile
# ======================================================================
def _profile(options: argparse.Namespace) -> int:
    """``harness-kit profile list|explain``。

    Args:
        options (`argparse.Namespace`): 已解析的命令行参数。

    Returns:
        `int`: 退出码。
    """
    settings = default_settings()
    search_dir = _profile_search_dir(settings)

    if options.action == "list":
        names = sorted(path.stem for path in search_dir.glob("*.yaml"))
        print(f"Profile 搜索目录: {search_dir}")
        for name in names:
            profile = _load_profile(name, settings)
            print(
                f"  {name.ljust(24)} model={profile.model.model_name} "
                f"perms={profile.permission.mode} "
                f"memory={'on' if profile.memory.enabled else 'off'} "
                f"middleware={[m.name for m in profile.middleware]}",
            )
        return 0

    target = options.name or options.profile
    profile = _load_profile(target, settings)
    # ``ResolvedProfile.explain()`` 返回 str（config/schema.py:587 的方法，
    # 不是属性）—— 它逐项列出"这个值来自哪个 Profile / Bundle / 默认值"。
    print(profile.explain())
    return 0


# ======================================================================
# 参数解析
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（模块级，便于测试与 ``--help`` 生成）。

    Returns:
        `argparse.ArgumentParser`: 解析器。
    """
    parser = argparse.ArgumentParser(
        prog="harness-kit",
        description="AgentScope + ReMe 之上的企业级 Harness：装配 / 对话 / 体检 / 评测 / 服务 / 回放。",
    )
    parser.add_argument("--profile", default="default", help="默认 Profile 名（默认 default）")
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", aliases=["chat"], help="跑一次对话")
    run.add_argument("message", nargs="*", help="用户输入")
    run.add_argument("--stream", action="store_true", help="流式逐字打印")
    run.add_argument("--session-id", default=None, help="指定会话 id")
    run.add_argument("--json", action="store_true", help="额外打印一行 JSON 结果")

    doctor = subparsers.add_parser("doctor", help="装配体检")
    doctor.add_argument("--profiles", nargs="*", default=None, help="要体检的 Profile 列表")
    doctor.add_argument("--memory", action="store_true", help="额外跑 MemoryDoctor")

    evaluation = subparsers.add_parser("eval", help="跑评测并产出报告")
    evaluation.add_argument("--dataset", default=None, help="EvalCase 的 jsonl 路径")
    evaluation.add_argument("--from-session", default=None, help="从会话事件流合成用例")
    evaluation.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全跑）")
    evaluation.add_argument("--concurrency", type=int, default=2, help="并发用例数（默认 2）")
    evaluation.add_argument("--timeout", type=float, default=120.0, help="单用例超时秒数")
    evaluation.add_argument("--report-dir", default=None, help="报告输出目录")

    feedback = subparsers.add_parser(
        "feedback",
        help="生产反馈闭环：会话日志 → 回归集 → 闸门判决 → 台账",
    )
    feedback.add_argument("--triggers", nargs="*", default=None, help="启用的失败信号（默认全部四种）")
    feedback.add_argument("--over-iterations", type=int, default=6, help="REPLY_END.iterations 阈值（默认 6）")
    feedback.add_argument("--min-replies", type=int, default=1, help="会话至少要有几个收尾回合（默认 1）")
    feedback.add_argument("--max-sessions", type=int, default=20, help="最多挑几个会话（默认 20）")
    feedback.add_argument("--out", default=None, help="回归集 jsonl 输出路径")
    feedback.add_argument("--ledger", default=None, help="台账 jsonl 路径（只追加）")
    feedback.add_argument("--baseline", default=None, help="基线报告 json 路径（只用于提示下一步，不触发评测）")

    serve = subparsers.add_parser("serve", help="起 HTTP 服务（默认 18420）")
    serve.add_argument("--host", default=None, help="监听地址")
    serve.add_argument("--port", type=int, default=None, help="监听端口（≥ 18000）")
    serve.add_argument("--max-sessions", type=int, default=32, help="常驻会话上限")

    replay = subparsers.add_parser("replay", help="回放会话事件流")
    replay.add_argument("--session", required=True, help="会话 id")
    replay.add_argument("--turns", action="store_true", help="打印每个回合的骨架")
    replay.add_argument("--json", action="store_true", help="输出 JSON")
    replay.add_argument("--export", default=None, help="把回合骨架导出到该路径")

    memory = subparsers.add_parser("memory", help="记忆：search / ingest / stats / doctor")
    memory.add_argument(
        "action",
        choices=["search", "ingest", "stats", "doctor"],
        nargs="?",
        default="search",
        help="子动作（默认 search）",
    )
    memory.add_argument("--query", default=None, help="检索串")
    memory.add_argument("--limit", type=int, default=8, help="返回条数上限")
    memory.add_argument("--min-score", type=float, default=0.0, help="融合分下限（注意 RRF 量纲）")
    memory.add_argument("--path", default=None, help="ingest 的目标文件或目录")
    memory.add_argument("--pattern", default="**/*.md", help="目录入库时的 glob")
    memory.add_argument("--tags", nargs="*", default=None, help="标签")
    memory.add_argument("--json", action="store_true", help="输出 JSON")

    profile = subparsers.add_parser("profile", help="Profile：list / explain")
    profile.add_argument("action", choices=["list", "explain"], nargs="?", default="list")
    profile.add_argument("name", nargs="?", default=None, help="explain 的 Profile 名")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口（契约 §3.20）。

    Args:
        argv (`Sequence[str] | None`): 参数列表；``None`` 时用 ``sys.argv[1:]``。

    Returns:
        `int`: 进程退出码。
    """
    parser = build_parser()
    options = parser.parse_args(list(argv) if argv is not None else None)
    _configure_logging(options.log_level)

    command = options.command
    try:
        if command in ("run", "chat"):
            return asyncio.run(_run_once(options))
        if command == "doctor":
            return asyncio.run(_doctor(options))
        if command == "eval":
            return asyncio.run(_eval(options))
        if command == "feedback":
            return asyncio.run(_feedback(options))
        if command == "serve":
            return asyncio.run(_serve(options))
        if command == "replay":
            return asyncio.run(_replay(options))
        if command == "memory":
            return asyncio.run(_memory(options))
        if command == "profile":
            return _profile(options)
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 的职责是把异常变成退出码 + 一行原因
        logger.error("{}: {}", type(exc).__name__, exc)
        return 1

    parser.error(f"未知子命令 {command!r}")  # pragma: no cover - argparse 已挡住
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
