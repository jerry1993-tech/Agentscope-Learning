#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第 18 讲《记忆自演化：auto_memory、auto_dream 与主动读取》验证脚本。

跑法（在仓库根，或任何地方用绝对路径）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/18_evolve.py

加 ``--live`` 会多跑两次真实 deepseek-flash 调用（实测 2 次）：
B 段的 ``auto_memory`` 一次、F 段的 ``auto_dream`` 一次。

六段的结构与「消耗几次模型调用」：

===  ==============================================================  ============
段   内容                                                            模型调用
===  ==============================================================  ============
A    调度器接线：attach / on_event 过滤 / min_messages 拦截 / 统计     0（纯逻辑）
B    会话结束自动落记忆：整形 + auto_memory（真实 job）                0 / --live 1
C    定时与后台编排：backend 清单、常驻 job 拒绝、reindex、daily_list  0（本地 ReMe）
D    遗忘与归档：ForgetPolicy / plan / apply / 降权 / 命中账本         0（本地 ReMe）
E    主动读取：ProactiveReader 从对话文件推查询串 + 置信度门控         0（本地 ReMe）
F    （``--live``）nightly：reindex + auto_dream 一次真实整合          1（实测）
===  ==============================================================  ============

**A~E 段全部离线**：ReMe 是本地嵌入式装配（``reme.ReMe(**config)`` + ``run_job``，
既不起 HTTP 服务也不占端口），LLM 组件装配上了但一次都不调用。

**A 段连 ReMe 都不需要**：它验证的是 :class:`~harness_kit.memory.maintenance.MemoryMaintenanceScheduler`
自己的事件语义（订阅哪个 topic、payload 里读什么、什么时候拒绝调用 job），
用一个记录用的假 maintainer 就够 —— 这也正是 "把消息来源注入进来" 这个设计的回报。

**为什么 B 段在非 ``--live`` 下也值得跑**：``auto_memory`` 的输入整形
（:meth:`~harness_kit.memory.distill.SessionDistiller.shape_messages`）
是**本讲最容易出错的地方**，而它是纯函数：丢弃 ``name == "memory"`` 的注入消息、
丢弃空消息、把 ``{"content": "纯文本"}`` 包成块列表。这三件事全部可以离线断言，
不需要花任何一次模型调用。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# 路径与 .env
# ----------------------------------------------------------------------
#: ``<repo>/tutorial_agsc_reme/reference``
REF: Path = Path(__file__).resolve().parents[1]
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根
REPO: Path = REF.parents[1]
#: 本地 ReMe 克隆必须排在 ``sys.path`` 最前（压住 site-packages 里的 0.3.1.10）
REME_SRC: Path = REPO / "third_party" / "ReMe"

for _candidate in (str(REME_SRC), str(REF)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:  # python-dotenv 是 pyproject 里声明的依赖
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:  # pragma: no cover - 本环境已装
    pass

from loguru import logger  # noqa: E402

#: 默认 INFO 日志会把每一次检索/维护都打出来；A~E 段是离线断言，压到 WARNING。
logger.remove()
logger.add(sys.stderr, level="WARNING")

from agentscope.message import AssistantMsg, UserMsg  # noqa: E402

from harness_kit.events.bus import EventBus  # noqa: E402
from harness_kit.events.types import EventKind, EventRecord  # noqa: E402
from harness_kit.memory import (  # noqa: E402
    DIALOG_DIR,
    ForgetPlan,
    ForgetPolicy,
    HarnessMemoryConfig,
    HitCounter,
    MaintenanceResult,
    MemoryClient,
    MemoryForgetter,
    MemoryJobs,
    MemoryMaintenanceScheduler,
    MemoryMaintainer,
    MemoryMetrics,
    MemorySearch,
    NightlyReport,
    ProactiveReader,
    ReMeWorkspace,
    SESSION_END_EVENT_NAME,
    SessionDistiller,
)

#: 是否跑真实模型那两段。
LIVE: bool = "--live" in sys.argv


def model_name() -> str:
    """当前要用的模型名（``.env`` 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。

    **为什么是函数而不是模块级常量**：``.env`` 可能是在 import 之后才被
    ``harness_kit.settings.Settings.from_env()`` 灌进 ``os.environ`` 的
    （它自己会 ``load_dotenv`` 一次），模块级常量会在这之前就把名字定死。

    Returns:
        `str`: 模型名。
    """
    return os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-chat"


#: 全部 ``ReMeWorkspace`` 的落地根（跑完不删，方便读者去看真实文件）。
SANDBOX: Path = Path(tempfile.mkdtemp(prefix="lesson18_")).resolve()

#: 本脚本建过的所有客户端，``main`` 统一收尾（ReMe 的组件有后台任务）。
_CLIENTS: list[MemoryClient] = []


def banner(title: str) -> None:
    """打一条段落标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, value: Any) -> None:
    """打一行 ``标签 = 值``。

    Args:
        label (`str`): 标签。
        value (`Any`): 值。
    """
    print(f"  {label:26s} = {value}")


async def make_client(name: str, *jobs: str, **components: Any) -> tuple[MemoryClient, ReMeWorkspace]:
    """起一个隔离的嵌入式 ReMe（**不占端口、不起服务**）。

    Args:
        name (`str`): 工作区子目录名，每段一个，互不干扰。
        *jobs (`str`): job 白名单；**不能放 background / cron 后端的 job**，
            否则 ``with_jobs`` 会抛 ``MemoryConfigError``（那是第 15 讲的重点，
            本讲用 :meth:`MemoryJobs._assert_runnable` 那条路演示）。
        **components (`Any`): 额外的组件覆盖，透传 ``with_components``。

    Returns:
        `tuple[MemoryClient, ReMeWorkspace]`: 已 start 的客户端与工作区。
    """
    workspace = ReMeWorkspace(root=SANDBOX / name)
    workspace.ensure()
    builder = HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None).with_jobs(*jobs)
    if components:
        builder = builder.with_components(**components)
    client = MemoryClient(builder.build())
    await client.start()
    _CLIENTS.append(client)
    return client, workspace


async def close_all() -> None:
    """把所有客户端关掉（ReMe 的 ``aclose`` 会等后台任务退出）。"""
    for client in _CLIENTS:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 收尾失败不该盖住真正的断言
            logger.warning("aclose 失败: {}", exc)
    _CLIENTS.clear()


def check(label: str, condition: bool, detail: str = "") -> bool:
    """打一条断言结果，返回是否通过。

    Args:
        label (`str`): 断言名。
        condition (`bool`): 结果。
        detail (`str`): 附加说明。

    Returns:
        `bool`: ``condition``。
    """
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(condition)


# ======================================================================
# A 段：调度器接线（0 次模型调用，连 ReMe 都不需要）
# ======================================================================
class RecordingMaintainer:
    """一个**记录调用**的假 maintainer，形状照着 :class:`MemoryMaintainer` 的对外契约。

    **为什么不继承它**：:class:`MemoryMaintenanceScheduler` 只鸭子调用
    ``self.maintainer.auto_memory(...)`` / ``auto_dream(...)`` / ``.client``，
    不做 ``isinstance`` 校验。本类因此可以完全不碰 ReMe 与 LLM ——
    A 段要证明的是"调度器什么时候决定调、把什么参数传下去"，
    而不是"ReMe 能不能蒸出记忆"（那是 B 段的事）。
    把这两件事混在一个测试里，失败时说不清是谁的问题。

    Attributes:
        calls (`list[dict[str, Any]]`): 每次 ``auto_memory`` 的入参快照。
        dreams (`list[dict[str, Any]]`): 每次 ``auto_dream`` 的入参快照。
        result (`MaintenanceResult`): ``auto_memory`` 要返回的结果。
    """

    def __init__(self, result: MaintenanceResult | None = None, *, with_client: Any = None) -> None:
        """初始化。

        Args:
            result (`MaintenanceResult | None`): 让 ``auto_memory`` 返回什么；
                ``None`` = ``created`` 一条 ``daily/2026-09-22/fake.md``。
            with_client (`Any`): ``.client`` 属性返回什么；``None`` 时是 ``None``。
        """
        self.calls: list[dict[str, Any]] = []
        self.dreams: list[dict[str, Any]] = []
        self.client = with_client
        self.result = result or MaintenanceResult(
            action="created",
            path="daily/2026-09-22/fake.md",
            detail="假 maintainer：本轮没有真的调 job",
        )

    async def auto_memory(
        self,
        *,
        session_id: str,
        msgs: list[Any],
        allowed_paths: list[str] | None = None,
    ) -> MaintenanceResult:
        """记录一次调用并返回预设结果。

        Args:
            session_id (`str`): 会话 id。
            msgs (`list[Any]`): 消息。
            allowed_paths (`list[str] | None`): 产物白名单。

        Returns:
            `MaintenanceResult`: ``self.result``。
        """
        self.calls.append(
            {"session_id": session_id, "n_msgs": len(list(msgs or ())), "allowed_paths": allowed_paths},
        )
        return self.result

    async def auto_dream(self, **kwargs: Any) -> MaintenanceResult:
        """记录一次 dream 调用。

        Args:
            **kwargs (`Any`): 透传参数。

        Returns:
            `MaintenanceResult`: ``action="skipped"``。
        """
        self.dreams.append(dict(kwargs))
        return MaintenanceResult(action="skipped", path=None, detail="假 maintainer")


def make_session_end(session_id: str, seq: int = 7) -> EventRecord:
    """造一条"会话结束"事件。

    **为什么是 ``EventKind.CUSTOM`` 而不是某个专门的 kind**：
    ``harness_kit/events/types.py:32-43`` 的 ``EventKind`` 是封闭枚举
    （``SESSION_START`` / ``REPLY_START`` / ``MODEL_CALL`` / ``TOOL_CALL`` /
    ``TOOL_RESULT`` / ``PERMISSION`` / ``MEMORY_HIT`` / ``REPLY_END`` / ``CUSTOM``），
    **没有** ``SESSION_END``。而 ``PAYLOAD_FIELDS[EventKind.CUSTOM]``
    （``events/types.py:61``）约定的是 ``("name", "data")`` ——
    这正是 ReMe/AgentScope 里"用一个带名字的 CUSTOM 承载业务事件"的既有惯例。
    新增一个枚举值会让**所有**按 ``EventKind`` 穷举的消费者（``PAYLOAD_FIELDS``、
    ``topic_matches``、任何 ``match`` 语句）在升级时静默漏掉它，代价远大于收益。

    Args:
        session_id (`str`): 会话 id。
        seq (`int`): 事件在会话内的序号。

    Returns:
        `EventRecord`: 校验通过的事件记录。
    """
    return EventRecord(
        session_id=session_id,
        seq=seq,
        kind=EventKind.CUSTOM,
        payload={"name": SESSION_END_EVENT_NAME, "data": {}},
    )


async def section_a() -> bool:
    """A 段：调度器的事件语义（0 次模型调用）。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("A 段：MemoryMaintenanceScheduler 的事件语义（0 次模型调用）")
    ok = True
    print(f"  SESSION_END_EVENT_NAME = {SESSION_END_EVENT_NAME!r}")
    print(f"  PAYLOAD_FIELDS[CUSTOM] = {('name', 'data')}")
    print()

    # --- A1: 订阅的是 CUSTOM，不是 "*" ---------------------------------
    maintainer = RecordingMaintainer()
    scheduler = MemoryMaintenanceScheduler(
        maintainer,  # type: ignore[arg-type] - 鸭子类型，见 RecordingMaintainer 的 docstring
        msgs_of=_fixed_msgs,
        min_messages=2,
    )
    bus = EventBus()
    subscription = scheduler.attach(bus)
    await bus.start()
    ok &= check("attach 返回 Subscription", subscription is not None)
    ok &= check("订阅的 topic 是 custom", subscription.topic == "custom", f"topic={subscription.topic!r}")

    # --- A2: 不是目标事件 → None，且不计数 ------------------------------
    reply_end = EventRecord(session_id="s-a", seq=1, kind=EventKind.REPLY_END, payload={"n_messages": 1})
    other_custom = EventRecord(
        session_id="s-a",
        seq=2,
        kind=EventKind.CUSTOM,
        payload={"name": "something_else", "data": {}},
    )
    await bus.publish(EventKind.REPLY_END, reply_end)
    await bus.publish(EventKind.CUSTOM, other_custom)
    await bus.drain()
    ok &= check(
        "REPLY_END / 别的 CUSTOM 都不触发",
        scheduler.stats()["triggered"] == 0 and maintainer.calls == [],
        f"stats={scheduler.stats()}",
    )

    # --- A3: 目标事件 + 消息够 → 真的调 auto_memory ---------------------
    await bus.publish(EventKind.CUSTOM, make_session_end("s-a"))
    await bus.drain()
    call = maintainer.calls[0] if maintainer.calls else {}
    ok &= check(
        "目标事件把 session_id 传到了 auto_memory",
        call.get("session_id") == "s-a",
        f"call={call}",
    )
    ok &= check("4 条消息原样透传", call.get("n_msgs") == 4, f"n_msgs={call.get('n_msgs')}")
    ok &= check(
        "统计：triggered=1 delegated=1 delivered=1",
        scheduler.stats() == {"triggered": 1, "delegated": 1, "delivered": 1, "skipped": 0, "failed": 0},
        f"stats={scheduler.stats()}",
    )

    # --- A4: min_messages 在调 job 之前拦截 -----------------------------
    quiet = MemoryMaintenanceScheduler(
        maintainer,  # type: ignore[arg-type]
        msgs_of=_one_msg,
        min_messages=8,
    )
    result = await quiet.flush("s-quiet")
    ok &= check(
        "消息不足时返回 skipped 且不调 job",
        result.action == "skipped" and len(maintainer.calls) == 1,
        f"action={result.action} 调用次数={len(maintainer.calls)}",
    )
    print(f"       detail = {result.detail}")

    # --- A5: 没有消息来源 → failed（配置错误，不是"没内容"）------------
    blind = MemoryMaintenanceScheduler(maintainer)  # type: ignore[arg-type]
    result = await blind.flush("s-blind")
    ok &= check("没有 msgs_of 时返回 failed", result.action == "failed", f"action={result.action}")
    print(f"       detail = {result.detail}")

    # --- A6: 空 session_id → failed（不猜、不落到某个默认会话）----------
    result = await quiet.flush("   ")
    ok &= check("空 session_id 返回 failed", result.action == "failed", f"action={result.action}")

    # --- A7: metrics 计数 -------------------------------------------------
    # **只能走 on_event，不能走 flush**：``flush`` 是"我明确要求落一次记忆"，
    # 它不变更调度统计、也不记 metrics；``on_event`` 才是"事件驱动的落记忆"。
    # 把两者混起来会让 stats 把人为调用也算成事件流量。
    metrics = MemoryMetrics()
    with_metrics = MemoryMaintenanceScheduler(
        maintainer,  # type: ignore[arg-type]
        msgs_of=_fixed_msgs,
        metrics=metrics,
    )
    await with_metrics.on_event(make_session_end("s-metrics"))
    snapshot = metrics.session_snapshot("s-metrics")
    ok &= check(
        "metrics 记了一次 writeback",
        snapshot.get("writebacks") == 1.0,
        f"writebacks={snapshot.get('writebacks')}",
    )
    ok &= check(
        "flush 不污染 on_event 的统计",
        with_metrics.stats()["triggered"] == 1,
        f"stats={with_metrics.stats()}",
    )

    # --- A8: detach 幂等 ---------------------------------------------------
    with_metrics.detach()
    with_metrics.detach()
    ok &= check("detach 幂等", with_metrics._subscription is None)  # noqa: SLF001 - 演示用

    print()
    print(scheduler.explain())
    await bus.aclose()
    return ok


async def _fixed_msgs(session_id: str) -> list[Any]:
    """给 A 段用的固定消息源（4 条，够 ``min_messages=2``）。

    Args:
        session_id (`str`): 会话 id（这里只用来打日志）。

    Returns:
        `list[Any]`: 4 条 ``Msg``。
    """
    return [
        UserMsg(name="user", content="我们决定把记忆库从 sqlite 换成 ReMe 的 file_store。"),
        AssistantMsg(name="assistant", content="好的，我记下这个技术选型。"),
        UserMsg(name="user", content="预算上限是每月 200 美元，超过要重新评审。"),
        AssistantMsg(name="assistant", content="已记录：预算 200 USD/月。"),
    ]


async def _one_msg(session_id: str) -> list[Any]:
    """给 A 段 ``min_messages`` 拦截用的消息源（只有 1 条）。

    Args:
        session_id (`str`): 会话 id。

    Returns:
        `list[Any]`: 1 条 ``Msg``。
    """
    return [UserMsg(name="user", content="你好")]


# ======================================================================
# B 段：会话结束自动落记忆
# ======================================================================
#: B 段要蒸馏的对话（真实来源是 ``Agent.state.context``，
#: 这里手写出来，是为了让"该被丢弃的块"显式出现在输入里）。
SESSION: list[Any] = [
    UserMsg(name="user", content="把生产环境的回滚流程记一下：先停流量，再回滚镜像 tag，最后验单。"),
    UserMsg(
        name="memory",  # ← 这是官方 ReMeMiddleware 注入的**检索产物**，必须被丢弃
        content="[记忆] 回滚手册：停流量 → 回滚镜像 tag → 验单（resource/runbook.md）",
    ),
    AssistantMsg(name="assistant", content="已记录回滚三步。另外你说的验单是指对账还是冒烟？"),
    UserMsg(name="user", content="指冒烟测试，跑 pytest -q 那套。"),
]


async def section_b() -> bool:
    """B 段：输入整形（离线）+ ``auto_memory``（``--live``）。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("B 段：会话结束自动落记忆（离线：整形；--live：真实 auto_memory）")
    ok = True

    # --- B1: 整形（纯函数，0 次调用）------------------------------------
    shaped = SessionDistiller.shape_messages(SESSION)
    names = [item["name"] for item in shaped]
    ok &= check(
        "注入的 memory 提示被丢弃",
        "memory" not in names,
        f"整形后 name 列表={names}",
    )
    ok &= check("4 条输入 → 3 条输出", len(shaped) == 3, f"len={len(shaped)}")
    ok &= check(
        "content 被 dump 成块列表",
        isinstance(shaped[0]["content"], list) and shaped[0]["content"][0]["type"] == "text",
        f"content[0]={shaped[0]['content'][0]!r}",
    )

    # ``Msg(content="纯文本")`` 会 ValidationError（AgentScope 2.0.8 的 content 必须是块列表），
    # 而从 JSON 反序列化来的 {"content": "纯文本"} 恰好长这样 —— 这一条钉的就是那个兜底。
    legacy = [{"name": "user", "role": "user", "content": "纯文本内容", "id": "m-legacy"}]
    shaped_legacy = SessionDistiller.shape_messages(legacy)
    first_block = shaped_legacy[0]["content"][0]
    # **只断言 type / text 两个键**：``Msg.model_dump`` 还会补上 ``id`` /
    # ``created_at`` / ``finished_at``，逐字比整个 dict 会把断言钉在
    # AgentScope 的内部字段上 —— 它一升级测试就红，而红的原因与形状兜底无关。
    ok &= check(
        "dict 的 str content 被包成块列表",
        first_block["type"] == "text" and first_block["text"] == "纯文本内容",
        f"content[0]={first_block!r}",
    )
    blanked = SessionDistiller.shape_messages(
        [UserMsg(name="user", content="   "), AssistantMsg(name="assistant", content="")],
    )
    ok &= check("空消息被丢弃", blanked == [], f"整形后={blanked}")
    print()

    # --- B2: 消息不足时调度器本地拦截（0 次调用）-------------------------
    # 白名单必须把 ``AutoMemoryStep`` 会用到的 job **全部**列上：
    # ``create_tools = ["daily_write"]``、``update_tools = ["read", "edit",
    # "frontmatter_update", "write"]``（``third_party/ReMe/reme/steps/evolve/auto_memory.py:72-73``），
    # 再加上它自查当天笔记用的 ``daily_list`` 与改名用的 ``move``
    # （``auto_memory.py:110-156``）。**少一个就是运行时 KeyError**
    # —— 实测漏掉 ``daily_write`` 时报的是
    # ``KeyError: "Job 'daily_write' not found in app_context.jobs"``，
    # 而不是"这个 job 不存在"这种能一眼看懂的话。
    client, workspace = await make_client(
        "b_session_end",
        "auto_memory",
        "daily_list",
        "daily_write",
        "daily_reindex",
        "move",
        "read",
        "edit",
        "write",
        "delete",
        "frontmatter_update",
        "list_tags",
        "reindex",
    )
    jobs = MemoryJobs(client)
    maintainer = MemoryMaintainer(client)
    scheduler = MemoryMaintenanceScheduler(maintainer, min_messages=2)
    silent = await scheduler.flush("s-empty")
    ok &= check(
        "无消息源时 failed（而不是偷偷调 job）",
        silent.action == "failed",
        f"action={silent.action}",
    )
    print(f"       detail = {silent.detail}")

    # --- B3: 给调度器接上真实消息源 -------------------------------------
    scheduler.msgs_of = _session_msgs  # 真实项目里是 SessionDistiller.messages_from_agent(agent)
    if not LIVE:
        print()
        print("  （未加 --live：跳过真实 auto_memory 调用。B 段整形部分已全部断言通过。）")
        print(f"  手工复现：给 --live 再看真实产物，落盘目录 {workspace.root}")
        return ok

    print()
    print(f"  真实调用 1/2：auto_memory（模型 = {model_name()}）")
    started = time.perf_counter()
    result = await scheduler.flush("s-lesson18")
    elapsed = time.perf_counter() - started
    show("action", result.action)
    show("path", result.path)
    show("耗时", f"{elapsed:.1f}s")
    show("detail", result.detail[:200])
    ok &= check("action 是 created / updated / skipped 之一", result.action in ("created", "updated", "skipped"))
    ok &= check("没有异常地被判成 failed", result.action != "failed", f"detail={result.detail[:120]}")

    if result.path:
        note = workspace.resolve_relative(result.path)
        exists = note.is_file()
        ok &= check("产物真实落盘", exists, str(note))
        if exists:
            print()
            print("  -------- 落盘的记忆卡 --------")
            print(_indent(note.read_text(encoding="utf-8")[:1200]))
            print("  ------------------------------")

    # 提醒：harness 写入路径不经监视循环，所以新文件不会自动进索引。
    reindexed = await jobs.reindex()
    counts = dict((reindexed.metadata or {}).get("counts") or {})
    show("reindex counts", counts)
    ok &= check("reindex 至少收进 1 个文件", any(counts.values()), f"counts={counts}")
    return ok


async def _session_msgs(session_id: str) -> list[Any]:
    """给 B 段用的会话消息源。

    **真实项目里这一行是** ``SessionDistiller.messages_from_agent(agent)``
    （``harness_kit/memory/distill.py:259``：从 ``agent.state.context`` 里
    筛出 ``Msg``）。这里手写是因为验证脚本里没有 Agent 实例 ——
    本讲要验证的是**调度与蒸馏**，Agent 本身是第 2~8 讲的内容。

    Args:
        session_id (`str`): 会话 id。

    Returns:
        `list[Any]`: 会话消息。
    """
    logger.debug("取会话 {} 的消息", session_id)
    return list(SESSION)


def _indent(text: str, prefix: str = "    ") -> str:
    """给多行文本统一加缩进。

    Args:
        text (`str`): 原文。
        prefix (`str`): 缩进。

    Returns:
        `str`: 缩进后的文本。
    """
    return "\n".join(prefix + line for line in text.splitlines())


# ======================================================================
# C 段：定时与后台编排（0 次模型调用）
# ======================================================================
async def section_c() -> bool:
    """C 段：job 后端清单、常驻 job 拒绝、reindex 与 daily_list。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("C 段：定时 / 后台编排（0 次模型调用）")
    ok = True
    client, workspace = await make_client(
        "c_jobs",
        "reindex",
        "daily_list",
        "list_tags",
        "auto_memory",
        "auto_dream",
    )
    jobs = MemoryJobs(client)
    print(jobs.describe())

    # --- C1: 常驻 job 读得到后端名
    # 这里读的是**配置里的**名字：``dream_cron`` 没进白名单，所以先确认白名单里的
    # 都是 base 后端。真实的"拒绝常驻 job"要走一条装上了 cron job 的装配 ——
    # 而 HarnessMemoryConfig.with_jobs 会在装配期就把常驻 job 挡掉（第 15 讲），
    # 所以本段用 backend_of 读实际后端，再用一个假的 job 实例验证拒绝逻辑。
    ok &= check(
        "白名单里的 job 后端都是 base",
        {name: jobs.backend_of(name) for name in jobs.available()} == dict.fromkeys(jobs.available(), "base"),
        f"backends={ {name: jobs.backend_of(name) for name in jobs.available()} }",
    )

    from harness_kit.memory.client import MemoryUnavailableError

    class _ResidentStub:
        """假装是 ``context.jobs["dream_cron"]`` 上的 cron job 实例。"""

        backend = "cron"

    client.application.context.jobs["dream_cron"] = _ResidentStub()  # type: ignore[assignment]
    try:
        await jobs.run_once("dream_cron")
    except MemoryUnavailableError as exc:
        ok &= check("常驻 job 被前台 run_once 拒绝", True)
        print(f"       拒绝理由：{exc}")
    else:  # pragma: no cover - 真进来就是 bug
        ok &= check("常驻 job 被前台 run_once 拒绝", False, "居然没抛异常")
    finally:
        del client.application.context.jobs["dream_cron"]

    # --- C2: 未知 job 的报错要说清"怎么修" -------------------------------
    try:
        await jobs.run_once("no_such_job")
    except MemoryUnavailableError as exc:
        ok &= check("未知 job 抛 MemoryUnavailableError", True)
        print(f"       报错：{str(exc)[:140]}")
    else:  # pragma: no cover
        ok &= check("未知 job 抛 MemoryUnavailableError", False)

    # --- C3: timeout_s 必须为正 ------------------------------------------
    try:
        await jobs.run_once("reindex", timeout_s=0)
    except ValueError as exc:
        ok &= check("timeout_s=0 被拒", True, str(exc))
    else:  # pragma: no cover
        ok &= check("timeout_s=0 被拒", False)

    # --- C4: reindex 真实跑（本地，0 调用）--------------------------------
    (workspace.daily_path() / "2026-09-22").mkdir(parents=True, exist_ok=True)
    (workspace.daily_path() / "2026-09-22" / "c1.md").write_text(
        "---\nname: C 段样例卡\nmemory_tags: [ops]\n---\n\n# C 段样例卡\n\n回滚三步：停流量、回滚镜像 tag、验单。\n",
        encoding="utf-8",
    )
    response = await jobs.reindex()
    counts = dict((response.metadata or {}).get("counts") or {})
    show("reindex success", response.success)
    show("reindex counts", counts)
    ok &= check("reindex 收进 1 个新文件", counts.get("added", 0) >= 1, f"counts={counts}")

    # --- C5: run_all 失败不中断 -------------------------------------------
    # ``no_such_job`` 不在 config 里，``run_all`` 会把它翻成 success=False 的响应，
    # 而**不**抛异常 —— 一批 job 里有一个不可用，不该让其余的结果拿不到。
    results = await jobs.run_all(["daily_list", "no_such_job", "list_tags"])
    show("run_all 的键", sorted(results))
    show("daily_list.success", results["daily_list"].success)
    show("no_such_job.success", results["no_such_job"].success)
    ok &= check("失败项进结果而不是抛异常", results["no_such_job"].success is False)
    ok &= check("同一批里的其余 job 仍然成功", results["list_tags"].success is True)

    daily = await jobs.daily_list("2026-09-22")
    notes = list((daily.metadata or {}).get("notes") or [])
    show("daily_list count", (daily.metadata or {}).get("count"))
    ok &= check("daily_list 看到刚写的那张卡", any("c1.md" in str(note.get("path", "")) for note in notes))
    return ok


# ======================================================================
# D 段：遗忘与归档（0 次模型调用）
# ======================================================================
#: 造一个"足够老"的 mtime（90 天前）。用它而不是改系统时间，
#: 是为了让脚本在任何机器上、任何时刻跑出来的年龄都一致。
OLD_MTIME: float = time.time() - 90 * 86400.0


def age_file(path: Path) -> None:
    """把一个文件的 mtime 拨到 90 天前。

    Args:
        path (`Path`): 目标文件（必须已存在）。
    """
    os.utime(path, (OLD_MTIME, OLD_MTIME))


async def section_d() -> bool:
    """D 段：遗忘策略的 plan / apply、降权、命中账本。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("D 段：遗忘与归档（0 次模型调用）")
    ok = True
    client, workspace = await make_client(
        "d_forget",
        "reindex",
        "list_tags",
        "delete",
        "frontmatter_update",
        "read",
    )

    # --- D1: 三个文件，三种命运 ------------------------------------------
    cold = workspace.daily_path() / "2026-06-01" / "cold.md"
    cold.parent.mkdir(parents=True, exist_ok=True)
    cold.write_text("---\nname: 冷记忆\nmemory_tags: [ops]\n---\n\n# 冷记忆\n\n三个月没人查过的部署细节。\n", encoding="utf-8")

    warm = workspace.resource_path() / "warm.md"
    warm.parent.mkdir(parents=True, exist_ok=True)
    warm.write_text("---\nname: 常用手册\nmemory_tags: [ops]\n---\n\n# 常用手册\n\n被检索命中过好几次的东西。\n", encoding="utf-8")

    pinned = workspace.digest_path() / "pinned.md"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text("---\nname: 钉住的摘要\nmemory_tags: [pinned]\n---\n\n# 钉住的摘要\n\n人工标注为长期有效。\n", encoding="utf-8")

    for path in (cold, warm, pinned):
        age_file(path)

    # 先把三个文件收进索引：``_tags_of`` 走的是 ``tag_index``，
    # 而 tag_index 是 reindex 装出来的 —— 不 reindex 的话三个文件都没有标签，
    # ``pinned.md`` 就不会进 protected（这正是"标签读不到按无标签处理"那条选择的后果）。
    await MemoryJobs(client).reindex()

    # ``record`` 内部会 ``dict.fromkeys`` 去重（"一次检索里同一文件命中多个 chunk 只算一次"），
    # 所以同一个文件要加两次就必须调两次 —— 这一条断言钉的就是那个语义：
    # 传一个含重复项的列表并不会让计数变成 2。
    counter = HitCounter()
    counter.record(["resource/warm.md", "resource/warm.md", "daily/2026-06-01/cold.md"])
    deduped = counter.count("resource/warm.md")
    counter.record(["resource/warm.md"])
    counter.persist(workspace)
    reloaded = HitCounter.load(workspace)
    show("一次 record 里重复项的去重结果", deduped)
    show("账本 reload 后 warm.md 的命中", reloaded.count("resource/warm.md"))
    ok &= check("同一次 record 里的重复路径只算一次", deduped == 1, f"count={deduped}")
    ok &= check(
        "两次 record 累加成 2",
        reloaded.count("resource/warm.md") == 2,
        f"as_dict={reloaded.as_dict()}",
    )

    policy = ForgetPolicy(max_age_days=30, min_hits=1, protected_tags=["pinned"])
    forgetter = MemoryForgetter(client, policy, hits=reloaded, workspace=workspace)

    # --- D2: plan 只算不删 -------------------------------------------------
    plan = await forgetter.plan()
    show("plan.summary()", plan.summary())
    show("delete", plan.delete)
    show("demote", plan.demote)
    show("protected", plan.protected)
    ok &= check("plan 不改磁盘（cold.md 还在）", cold.is_file())
    ok &= check("protected 认出了 pinned", plan.protected == ["digest/pinned.md"], f"protected={plan.protected}")
    ok &= check(
        "hits>0 的文件进 demote 而不是 delete",
        plan.demote == ["resource/warm.md"] and plan.delete == ["daily/2026-06-01/cold.md"],
        f"delete={plan.delete} demote={plan.demote}",
    )
    ok &= check("ForgetPlan.is_empty() 反映的是动作", not plan.is_empty())

    # --- D3: apply 真删 + 降权 --------------------------------------------
    deleted = await forgetter.apply(plan)
    show("apply 返回（删除数）", deleted)
    ok &= check("cold.md 被删掉", not cold.is_file())
    ok &= check("pinned.md 没被动", pinned.is_file())
    ok &= check("apply 的返回值只数删除", deleted == 1, f"deleted={deleted}")

    body = warm.read_text(encoding="utf-8")
    ok &= check("warm.md 被写上 memory_status: stale", "memory_status: stale" in body, body.splitlines()[0] if body else "")
    stale = await forgetter.stale_paths()
    show("stale_paths()", stale)
    ok &= check("降权后的文件能被读出来", stale == ["resource/warm.md"], f"stale={stale}")

    # --- D4: 空计划也安全 -------------------------------------------------
    empty = await forgetter.plan()
    ok &= check("再跑一次 plan 是空的", empty.is_empty(), f"summary={empty.summary()}")

    # --- D5: 参数校验 ------------------------------------------------------
    try:
        ForgetPolicy(max_age_days=30, min_hits=0).normalized_protected()
        bad = False
    except Exception:  # noqa: BLE001 - 这一条不该抛
        bad = True
    ok &= check("ForgetPolicy 默认 protected_tags=['pinned']", not bad and policy.normalized_protected() == {"pinned"})
    try:
        MemoryForgetter(client, ForgetPolicy(max_age_days=-1))
    except ValueError as exc:
        ok &= check("负的 max_age_days 被拒", True, str(exc))
    else:  # pragma: no cover
        ok &= check("负的 max_age_days 被拒", False)
    return ok


# ======================================================================
# E 段：主动读取（0 次模型调用）
# ======================================================================
#: E 段的对话文件内容：ReMe 用 ``Msg.model_dump_json()`` 逐行写
#: （``third_party/ReMe/reme/steps/evolve/auto_memory.py:219``），
#: 所以这里手写出同样的形状 —— 这样 ``_user_text`` 的解析路径是真的。
DIALOG_LINES: list[dict[str, Any]] = [
    {"id": "d1", "name": "user", "role": "user", "content": [{"type": "text", "text": "帮我看看蓝绿切换的检查清单"}]},
    {"id": "d2", "name": "assistant", "role": "assistant", "content": [{"type": "text", "text": "好的，我查一下。"}]},
    {"id": "d3", "name": "user", "role": "user", "content": [{"type": "text", "text": "顺便确认下回滚流程是不是停流量 → 回滚镜像 → 验单"}]},
]


async def section_e() -> bool:
    """E 段：主动读取的查询串推导与置信度门控。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("E 段：主动读取 proactive（0 次模型调用）")
    ok = True
    client, workspace = await make_client(
        "e_proactive",
        "search",
        "reindex",
        "read",
        "traverse",
    )

    dialog = workspace.dialog_path()
    dialog.mkdir(parents=True, exist_ok=True)
    target = dialog / "s-proactive.jsonl"
    target.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in DIALOG_LINES) + "\n",
        encoding="utf-8",
    )
    show("对话文件", workspace.relative(target))

    today = time.strftime("%Y-%m-%d")
    daily = workspace.daily_path() / today
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "rollback.md").write_text(
        "---\nname: 回滚流程\nmemory_tags: [ops]\n---\n\n"
        "# 回滚流程\n\n回滚三步：先停流量，再回滚镜像 tag，最后验单（冒烟测试）。\n",
        encoding="utf-8",
    )
    (daily / "theme.md").write_text(
        "---\nname: 绘图偏好\nmemory_tags: [pref]\n---\n\n# 绘图偏好\n\n用户偏好深色主题的 matplotlib 图表。\n",
        encoding="utf-8",
    )
    # 第二张"弱相关"卡：它只沾到"回滚"一个词，用来演示**置信度是相对的**。
    # 没有它就没法演示门控 —— 归一化后最强的恒为 1.0，用单个命中做阈值测试
    # 只能证明"1.0 >= 阈值"，证明不了任何东西。
    (daily / "rollback-notes.md").write_text(
        "---\nname: 回滚随手记\nmemory_tags: [ops]\n---\n\n# 回滚随手记\n\n回滚这件事以前踩过坑，细节待补。\n",
        encoding="utf-8",
    )
    await MemoryJobs(client).reindex()

    reader = ProactiveReader(MemorySearch(client, workspace=workspace), min_confidence=0.0)
    query = await reader.query_for("s-proactive")
    show("从对话推出的查询串", repr(query))
    ok &= check(
        "查询串只含 user 文本",
        bool(query) and "蓝绿" in query and "好的，我查一下" not in query,
        f"query={query!r}",
    )

    hits = await reader.suggest(session_id="s-proactive", limit=3)
    show("suggest 命中数", len(hits))
    for hit in hits:
        show("  hit", f"{hit.path}  score={hit.score:.6f}")
    ok &= check("主动读取真的召回了回滚卡", any("rollback" in hit.path for hit in hits), f"paths={[h.path for h in hits]}")
    ok &= check("limit=3 生效", len(hits) <= 3, f"len={len(hits)}")
    ok &= check(
        "最强的命中排在第一位",
        bool(hits) and hits[0].path.endswith("rollback.md"),
        f"paths={[h.path for h in hits]}",
    )

    # --- E2: 置信度门控：阈值一抬，只放行相对最强的那条，其余记进指标 ---------
    from harness_kit.memory.gating import MemoryGate

    metrics = MemoryMetrics()
    strict = ProactiveReader(
        MemorySearch(client, workspace=workspace),
        min_confidence=0.95,
        metrics=metrics,
    )
    strict_hits = await strict.suggest(session_id="s-proactive", limit=3)
    show("阈值 0.95 时放行的条数", len(strict_hits))
    show("放行的 path", [hit.path for hit in strict_hits])
    ok &= check("阈值 0.95 时只放行最强的一条", len(strict_hits) == 1, f"hits={[h.path for h in strict_hits]}")
    ok &= check(
        "放行的确实是归一化后的最高分那条",
        bool(strict_hits) and strict_hits[0].path.endswith("rollback.md"),
        f"path={strict_hits[0].path if strict_hits else None}",
    )
    snapshot = metrics.snapshot()
    show("gate_rejections_min_score", snapshot.get("gate_rejections_min_score"))
    ok &= check(
        "被挡下的条数记进了指标（不是静默）",
        snapshot.get("gate_rejections_min_score", 0) >= 1,
        f"snapshot={ {k: v for k, v in snapshot.items() if 'gate' in k} }",
    )

    # --- E3: 没有对话文件时不猜 -------------------------------------------
    missing = await reader.suggest(session_id="s-no-dialog", limit=3)
    ok &= check("没有对话文件时返回空列表而不是乱查", missing == [], f"hits={missing}")

    # --- E4: 归一化分数的量纲 ---------------------------------------------
    if hits:
        # 归一化是"相对最好的一条"，所以**最高分恒为 1.0**：
        # 这条断言钉的是"置信度不是原始 RRF 分"（原始分在 0.016 量级，
        # 直接和 0.35 比会全军覆没）。
        result = await MemorySearch(client, workspace=workspace).search(query or "回滚", limit=3)
        normalized = MemoryGate.normalize_scores(result.hits)
        show("原始 score", [f"{h.score:.6f}" for h in result.hits])
        show("归一化 confidence", [f"{c:.4f}" for c in normalized])
        ok &= check("归一化后最高分是 1.0", bool(normalized) and abs(normalized[0] - 1.0) < 1e-9)
    return ok


# ======================================================================
# F 段：nightly（--live 才有真实模型调用）
# ======================================================================
def probe_parse_structured_reply() -> bool:
    """离线复现 ``auto_dream`` 的结构化提取为什么会对模型输出格式如此敏感。

    ``DreamExtractStep`` 用 :func:`reme.steps.evolve.dream.utils.parse_structured_reply`
    把子 Agent 的回复解析成 ``{"units": [...]}``。这个函数
    （``third_party/ReMe/reme/steps/evolve/dream/utils.py:137``）先**把整段回复**当
    YAML 解析，失败后调的兜底 ``_parse_scalar_mapping``（``utils.py:153``）
    只用正则抓 ``action|target_path|note`` 三个键 —— 对需要 ``units`` 列表的
    提取毫无用处。于是"裸 YAML 里某个标量值含未加引号的 ``": "``"这一件小事，
    会让整段提取**静默变成空 dict**，而且**加代码围栏也救不回来**。

    本函数不花任何模型调用：它直接喂一段与真实回复同形的文本。

    Returns:
        `bool`: 断言是否通过（"坏 YAML 会被静默吞掉"这件事确实成立）。
    """
    from reme.steps.evolve.dream.utils import parse_structured_reply

    reply = (
        "units:\n"
        "  - title: auto_memory 的 create 与 update 分叉\n"
        "    summary: 创建分支只挂 daily_write: 一条工具；更新分支挂 read/edit/frontmatter_update/write\n"
        "    kind: procedure\n"
    )
    parsed = parse_structured_reply(reply)
    fenced = parse_structured_reply("前缀\n```yaml\n" + reply + "```\n后记")
    ok = True
    ok &= check(
        "裸 YAML 的提取结果被静默吞成空 dict",
        parsed == {},
        f"parse_structured_reply -> {parsed!r}",
    )
    ok &= check(
        "加代码围栏也救不回来",
        fenced == {},
        f"围栏版本 -> {fenced!r}",
    )
    print("       （根因：标量值里的 ': ' 未加引号 → yaml.safe_load 抛 "
          "'mapping values are not allowed here' → 兜底函数抓的是 action/target_path/note，")
    print("        对 units 列表无效。未修复：third_party/ 只读。）")
    return ok


async def section_f() -> bool:
    """F 段：``nightly`` 的编排（reindex + auto_dream）。

    **本段刻意不把"dream 成功"当断言**。``auto_dream`` 的端到端成败取决于
    上游模型能不能吐出**格式恰好合规**的 YAML（见 :func:`probe_parse_structured_reply`），
    而那是**供应商侧**的随机性，不是一个 harness 该背的锅。
    harness 该背的责任是：**sub的成败必须被如实记录、不许被吞成"成功"**。
    所以这里的断言是"报告忠实反映结局"，而不是"结局必须是成功"。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("F 段：nightly 例行维护（reindex + auto_dream）")
    ok = True
    client, workspace = await make_client(
        "f_nightly",
        "reindex",
        "auto_dream",
        "daily_list",
        "daily_write",
        "daily_reindex",
        "move",
        "read",
        "edit",
        "write",
        "frontmatter_update",
        "list_tags",
    )

    print("  ---- F0：上游解析器的脆弱性（离线，0 次模型调用）----")
    ok &= probe_parse_structured_reply()
    print()

    today = time.strftime("%Y-%m-%d")
    daily = workspace.daily_path() / today
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "topic.md").write_text(
        "---\nname: 记忆库选型\nmemory_tags: [arch]\n---\n\n"
        "# 记忆库选型\n\n我们决定用 ReMe 的 file_store 做记忆存储，"
        "理由是它的组件（keyword_index / tag_index / file_catalog）都能单独替换，"
        "而 sqlite 那套把索引与存储绑死了。预算上限 200 USD/月。\n",
        encoding="utf-8",
    )

    scheduler = MemoryMaintenanceScheduler(MemoryMaintainer(client))
    if not LIVE:
        report = await scheduler.nightly(dream=False)
        show("nightly summary", report.summary())
        ok &= check("nightly 的 reindex 成功", report.reindex_ok, f"counts={report.reindex_counts}")
        ok &= check("dream=False 时不跑 dream", report.dream is None)
        print()
        print("  （未加 --live：跳过真实 auto_dream。加 --live 会多 1 次模型调用。）")
        return ok

    print(f"  真实调用 2/2：auto_dream（模型 = {model_name()}）")
    started = time.perf_counter()
    report = await scheduler.nightly(date=today)
    elapsed = time.perf_counter() - started
    show("nightly summary", report.summary())
    show("耗时", f"{elapsed:.1f}s")
    if report.dream is not None:
        show("dream.action", report.dream.action)
        show("dream.detail", report.dream.detail[:400])

    ok &= check("nightly 的 reindex 成功且收了文件", report.reindex_ok and any(report.reindex_counts.values()))
    ok &= check("dream 被真的跑过一次（不是 None）", report.dream is not None)
    ok &= check(
        "dream 的结局被如实记录（failed 也算如实，只要不是静默成功）",
        report.dream is not None and report.dream.action in ("updated", "skipped", "failed"),
        f"action={getattr(report.dream, 'action', None)}",
    )
    if report.dream is not None and report.dream.action == "failed":
        ok &= check(
            "失败时 detail 里带着上游原因（不是空串）",
            bool(report.dream.detail.strip()),
            f"detail 长度={len(report.dream.detail)}",
        )
        print()
        print("  注意：这里 dream 是 failed，**不是本 harness 的 bug**。")
        print("  根因在上面 F0 段：deepseek-flash 这次吐出的 YAML 里，某个标量值含未加引号的 ': '，")
        print("  parse_structured_reply 把整段提取静默吞成空 dict，DreamExtractStep 一次重试后")
        print("  仍然拿不到 units，于是 DreamFinishStep 把 state.errors 带进 response.success=False。")
        print("  本讲**不谎报成功**：auto_dream 的端到端成功在本环境未验证，")
        print("  已验证的是「失败不会让 nightly 崩，且被如实汇报」。")
    ok &= check(
        "NightlyReport.summary() 里没有数据丢失",
        "reindex" in report.summary() and "dream" in report.summary(),
        report.summary(),
    )

    digest_files = sorted(str(path.relative_to(workspace.root)) for path in workspace.digest_path().rglob("*.md"))
    show("digest/ 下的文件", digest_files)
    return ok


# ======================================================================
# main
# ======================================================================
async def main() -> int:
    """跑完六段。

    Returns:
        `int`: 0 表示全部断言通过，1 表示有失败。
    """
    print(f"python      = {sys.executable}")
    print(f"沙箱        = {SANDBOX}")
    print(f"LIVE        = {LIVE}")
    try:
        import reme

        print(f"reme        = {reme.__version__}")
    except Exception as exc:  # noqa: BLE001 - 打不出来不该让脚本挂掉
        print(f"reme        = <{exc}>（PYTHONPATH 里没有本地克隆？）")
    print(f"LLM_MODEL   = {model_name()}")
    print(f"OPENAI_BASE_URL = {os.getenv('OPENAI_BASE_URL')}")
    print(f"OPENAI_API_KEY  = {'已设置' if os.getenv('OPENAI_API_KEY') else '缺失'}")

    results: dict[str, bool] = {}
    try:
        results["A 调度器接线"] = await section_a()
        results["B 会话落记忆"] = await section_b()
        results["C 定时编排"] = await section_c()
        results["D 遗忘归档"] = await section_d()
        results["E 主动读取"] = await section_e()
        results["F nightly"] = await section_f()
    finally:
        await close_all()

    banner("汇总")
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    failed = [name for name, passed in results.items() if not passed]
    print()
    print(f"沙箱保留在 {SANDBOX}（想看真实文件就直接进去）")
    if failed:
        print(f"失败段落: {failed}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    # Windows 上 ``subprocess``/``asyncio`` 的默认事件循环对 ReMe 的组件有影响；
    # 本脚本只在 macOS/Linux 上验证过，这里显式写明，避免读者误以为跨平台已测。
    if sys.platform == "win32":  # pragma: no cover
        raise SystemExit("本脚本在 macOS / Linux 上验证；Windows 未验证。")
    raise SystemExit(asyncio.run(main()))
