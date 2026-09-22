#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第 19 讲《合体：把 ReMe 做成 Harness 的长期记忆中间件》验证脚本。

跑法（在仓库根，或任何地方用绝对路径）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/19_memory_middleware.py

加 ``--live`` 会多跑一次真实 deepseek-flash 调用（实测 1 次）：G 段的写回。

七段的结构与「消耗几次模型调用」：

===  ==============================================================  ============
段   内容                                                            模型调用
===  ==============================================================  ============
A    装配层：``build_memory_middleware`` 的键校验与标量→对象构造      0（纯逻辑）
B    召回门控与写入门控的判定矩阵（四种拒绝原因 + 量纲）              0（纯逻辑）
C    端到端注入：真 Agent + 真嵌入式 ReMe + 脚本化 echo 模型          0（本地 ReMe）
D    注入形状与 KV Cache：prompt 前缀稳定性 + 注入点位置              0（本地 ReMe）
E    写回：门控拦截 / 异步化 / 超时 / ``close()`` 排空                0（假 job）
F    多租户隔离与命中率指标                                           0（纯逻辑）
G    （``--live``）一次真实写回，按 ``created``/``modified`` 判落盘    1（实测）
===  ==============================================================  ============

**A、B、F 段连 ReMe 都不需要**：它们验证的是 harness 自己的判定逻辑与路径模型，
用纯计算就能断言。C、D、E 段需要真 ReMe（**嵌入式**：``reme.ReMe(**config)`` +
``run_job``，既不起 HTTP 服务也不占端口），但**一次模型都不调** ——
``EchoChatModel`` 按脚本回放，ReMe 的 ``as_llm`` 组件被换成同一个 echo 模型。

**为什么 C 段必须跑真 Agent 而不是手写一个假的**：本讲的全部结论都依赖
"中间件挂在 AgentScope 的哪个 hook 上、注入的消息长什么样、在 context 的哪个位置"。
这些只有让真的 ``agentscope.agent.Agent`` 跑一遍 ReAct 循环才能观察到 ——
手写一个"等价"的循环，观察到的就只是我自己的假设。

**D 段为什么可信**：它不看 `state.context` 的最终结果（那是"事后重建"），
而是拦在 **provider 调用**（``ChatModelBase._call_api``）上，把模型**真正收到的
prompt** 逐次记下来，再断言 ``prompt[k]`` 是 ``prompt[k+1]`` 的前缀 ——
这正是 KV Cache 能复用的充分条件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
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

#: 默认 INFO 日志会把每一次检索/写回都打出来；本脚本用 print 汇报，压到 WARNING。
logger.remove()
logger.add(sys.stderr, level="WARNING")

from agentscope.agent import Agent  # noqa: E402
from agentscope.message import AssistantMsg, Msg, TextBlock, UserMsg  # noqa: E402
from agentscope.model import ChatResponse  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402

from harness_kit.memory import (  # noqa: E402
    DEFAULT_MIN_SCORE,
    HarnessMemoryConfig,
    MemoryBudget,
    MemoryClient,
    MemoryGate,
    MemoryHit,
    MemoryIngestor,
    MemoryMetrics,
    MemoryWriteGate,
    ReMeWorkspace,
    TenantError,
    TenantRouter,
    estimate_tokens_heuristic,
)
from harness_kit.memory.middleware import (  # noqa: E402
    LongTermMemoryMiddleware,
    build_memory_middleware,
)
from harness_kit.models.adapters.echo import EchoChatModel  # noqa: E402
from harness_kit.tools.builtin_pack import calc  # noqa: E402

# AgentScope / ReMe 在 import 时会自己往 loguru 上挂一个 INFO sink，
# 把上面那次 ``logger.remove()`` 的效果盖掉。所以在**全部 import 之后**
# 再清一次，本脚本的输出才只剩自己 print 的断言结果。
logger.remove()
logger.add(sys.stderr, level="WARNING")


def quieten_logs() -> None:
    """把 loguru 的全局 sink 重置成"只有本脚本的 WARNING sink"。

    **为什么需要反复调用**（这是本讲一个真实的集成坑，不是脚本洁癖）：
    ``import reme`` 这一个动作就会执行 ``logger.remove()`` 并往 **stdout**
    挂一个 INFO sink（``third_party/ReMe/reme/utils/logger_utils.py:63-70``），
    把宿主配好的日志整个冲掉。而中间件的嵌入式 app 在 ``start()`` 时又会
    按自己的 config 重新初始化一次 —— 所以"配好日志"这件事必须
    **在每次生命周期跃迁之后重做**。

    对生产系统来说，这意味着：如果宿主用 loguru 记审计日志，
    ReMe 一 import 就会把它静音（或反过来，把 ReMe 的 INFO 灌进宿主的审计流）。
    """
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

#: 是否跑真实模型那一段。
LIVE: bool = "--live" in sys.argv

#: 全部工作区落地的根（跑完不删，方便读者去看真实文件）。
SANDBOX: Path = Path(tempfile.mkdtemp(prefix="lesson19_")).resolve()

#: 本脚本建过的所有 ReMe 客户端，``main`` 统一收尾。
_CLIENTS: list[MemoryClient] = []


# ======================================================================
# 基础设施
# ======================================================================
def model_name() -> str:
    """当前要用的模型名（``.env`` 里的 ``LLM_MODEL``，本项目实测为 ``deepseek-flash``）。

    做成函数而不是模块级常量：``.env`` 可能在 import 之后才被灌进
    ``os.environ``，常量会在那之前就把名字定死。

    Returns:
        `str`: 模型名。
    """
    return os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-chat"


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
    print(f"  {label:30s} = {value}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    """打一条断言结果并返回它。

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


async def make_client(name: str, *jobs: str) -> tuple[MemoryClient, ReMeWorkspace]:
    """起一个隔离的嵌入式 ReMe（**不占端口、不起服务**）。

    Args:
        name (`str`): 工作区子目录名。
        *jobs (`str`): job 白名单（不能放 background / cron 后端的 job）。

    Returns:
        `tuple[MemoryClient, ReMeWorkspace]`: 已 start 的客户端与工作区。
    """
    workspace = ReMeWorkspace(root=SANDBOX / name)
    workspace.ensure()
    builder = HarnessMemoryConfig(workspace=workspace, embedding_dimensions=None)
    client = MemoryClient(builder.with_jobs(*jobs).build())
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


def hit(path: str, text: str, score: float) -> MemoryHit:
    """造一条 ``MemoryHit``（纯数据，用于门控的判定矩阵）。

    为什么不从真 ReMe 拿命中来测门控：门控的四种拒绝原因里，
    ``below_min_score`` 与 ``over_budget`` 都要**精确控制分数与长度**，
    而真实检索的分数取决于分块器与 RRF 融合 —— 用它测门控等于让被测量的东西
    自己决定测量条件。这里只喂数据。

    Args:
        path (`str`): 工作区相对路径。
        text (`str`): 片段正文。
        score (`float`): 融合分数。

    Returns:
        `MemoryHit`: 命中对象。
    """
    return MemoryHit(
        chunk_id=hashlib.sha1(f"{path}:{text}".encode()).hexdigest()[:16],
        path=path,
        start_line=1,
        end_line=max(1, text.count("\n") + 1),
        text=text,
        score=score,
        source="fused",
    )


def spec_of(**params: Any) -> SimpleNamespace:
    """造一个最小可用的 ``MiddlewareSpec`` 替身（只有 ``params``）。

    Args:
        **params (`Any`): 要透传的 ``params``。

    Returns:
        `SimpleNamespace`: 带 ``params`` 属性的对象。
    """
    return SimpleNamespace(params=dict(params))


# ======================================================================
# A 装配层：键校验与"标量 → 对象"
# ======================================================================
def section_a() -> bool:
    """验证 ``build_memory_middleware`` 的参数契约（不起 app、不调模型）。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("A 装配层：Profile 的标量参数怎么变成中间件的对象参数")
    ok = True

    # ---- A1 未知键必须报错 -------------------------------------------
    # 为什么这条最重要：Profile 是 YAML，拼错键名在 YAML 里是合法的，
    # 如果只 warning 不报错，"配了但没接线"就会静默生效。
    try:
        build_memory_middleware(spec_of(write_asnyc=False))
    except ValueError as exc:
        ok &= check(
            "A1 未知键 ValueError",
            "write_asnyc" in str(exc),
            str(exc)[:90],
        )
    else:
        ok &= check("A1 未知键 ValueError", False, "没有抛异常")

    # ---- A2 mode 白名单 ----------------------------------------------
    try:
        build_memory_middleware(spec_of(mode="static"))
    except ValueError as exc:
        ok &= check("A2 mode 白名单", "static_control" in str(exc), str(exc)[:70])
    else:
        ok &= check("A2 mode 白名单", False, "没有抛异常")

    # ---- A3 默认值：不配门控时与官方行为一致 -------------------------
    plain = build_memory_middleware(spec_of())
    p = plain._parameters
    ok &= check(
        "A3 默认不门控",
        p.gate is None and p.write_gate is None,
        f"gate={p.gate} write_gate={p.write_gate}",
    )
    ok &= check(
        "A4 默认异步写回 + 30s 超时",
        p.write_async is True and p.write_timeout_s == 30.0,
        f"write_async={p.write_async} write_timeout_s={p.write_timeout_s}",
    )
    ok &= check(
        "A5 默认 mode/top_k 与官方一致",
        p.mode == "static_control" and p.top_k == 5,
        f"mode={p.mode} top_k={p.top_k}",
    )

    # ---- A6 标量 → 对象：gate_min_score 构造出 MemoryGate -------------
    gated = build_memory_middleware(
        spec_of(gate_min_score=0.4, sensitive_tags=["pii"], session_tags=["acme"]),
    )
    gate = gated._parameters.gate
    ok &= check(
        "A6 gate_min_score → MemoryGate",
        isinstance(gate, MemoryGate) and gate.min_score == 0.4,
        f"type={type(gate).__name__} min_score={getattr(gate, 'min_score', None)}",
    )
    ok &= check(
        "A7 sensitive_tags 归一化进 gate",
        isinstance(gate, MemoryGate) and gate.sensitive_tags == ["pii"],
        str(getattr(gate, "sensitive_tags", None)),
    )
    ok &= check(
        "A8 session_tags 收进 Parameters",
        gated._parameters.session_tags == ("acme",),
        str(gated._parameters.session_tags),
    )
    ok &= check(
        "A9 只给 sensitive_tags 也会建 gate（默认阈值兜底）",
        isinstance(
            build_memory_middleware(spec_of(sensitive_tags=["pii"]))._parameters.gate,
            MemoryGate,
        ),
        f"DEFAULT_MIN_SCORE={DEFAULT_MIN_SCORE}",
    )

    # ---- A10 标量 → 对象：write_min_messages 构造出 MemoryWriteGate ----
    writing = build_memory_middleware(
        spec_of(write_min_messages=3, write_min_chars=20, write_async=False),
    )
    wg = writing._parameters.write_gate
    ok &= check(
        "A10 write_min_messages → MemoryWriteGate",
        isinstance(wg, MemoryWriteGate) and wg.min_messages == 3 and wg.min_chars == 20,
        f"type={type(wg).__name__}",
    )
    ok &= check(
        "A11 write_async=False 透传",
        writing._parameters.write_async is False,
        str(writing._parameters.write_async),
    )
    ok &= check(
        "A12 只给 write_async 不会建 write_gate",
        build_memory_middleware(spec_of(write_async=False))._parameters.write_gate is None,
        "write_gate 仍为 None",
    )

    # ---- A13 预算：0 表示不裁剪 ---------------------------------------
    ok &= check(
        "A13 budget_tokens=0 → budget=None（与官方一致，不裁剪）",
        build_memory_middleware(spec_of(budget_tokens=0))._parameters.budget is None,
        "官方 ReMeMiddleware 从没有长度控制",
    )
    ok &= check(
        "A14 budget_tokens=800 → MemoryBudget",
        isinstance(
            build_memory_middleware(spec_of(budget_tokens=800))._parameters.budget,
            MemoryBudget,
        ),
        "800",
    )

    # ---- A15 兼容补丁是"懒"的 -----------------------------------------
    # 为什么断言"懒"：补丁必须在构造 app **之前**打（见 _build_app），
    # 而构造 app 发生在第一次 hook 调用时。如果这里的补丁列表非空，
    # 说明有人把它挪到了 __init__ 里 —— 那 app 可能早就建好了。
    ok &= check(
        "A15 compat_patched 在未建 app 时为空",
        plain.compat_patched == [],
        f"compat_patched={plain.compat_patched}",
    )

    print()
    show("_MEMORY_MIDDLEWARE_PARAMS 键数", len(_param_keys()))
    show("可配键", ", ".join(_param_keys()))
    return ok


def _param_keys() -> tuple[str, ...]:
    """读回中间件允许的 Profile 键（用于打印）。

    Returns:
        `tuple[str, ...]`: 键名元组。
    """
    from harness_kit.memory import middleware as mw_module

    return mw_module._MEMORY_MIDDLEWARE_PARAMS  # noqa: SLF001 - 教程要展示它


# ======================================================================
# B 门控判定矩阵（纯逻辑）
# ======================================================================
def section_b() -> bool:
    """验证召回门控与写入门控的每一条判定分支。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("B 门控判定矩阵：四种拒绝原因 + 归一化量纲")
    ok = True

    hits = [
        hit("pref.md", "用户偏好深色主题的图表。" * 8, 0.90),
        hit("ops.md", "部署令牌放在 ~/.secrets/token。" * 8, 0.45),
        hit("misc.md", "与查询弱相关的杂项。" * 8, 0.10),
    ]

    # ---- B1 归一化是"相对最佳分"而不是绝对分 --------------------------
    # 这是本讲最容易错的一处：ReMe 融合后的原始分数量纲（RRF 是 1/(k+rank)，
    # 量级 1e-2；纯余弦是 [0,1]）会随融合路数变化，直接拿来跟 0.2 比毫无意义。
    norms = MemoryGate.normalize_scores(hits)
    ok &= check(
        "B1 normalize_scores 以最高分为 1.0",
        abs(norms[0] - 1.0) < 1e-9 and abs(norms[1] - 0.5) < 1e-9,
        f"raw={[h.score for h in hits]} → norm={[round(n, 4) for n in norms]}",
    )
    ok &= check(
        "B2 归一化的量纲与原始分数无关",
        MemoryGate.normalize_scores([hit("a.md", "x", 100.0), hit("b.md", "y", 50.0)])
        == [1.0, 0.5],
        "100/50 与 0.9/0.45 得到同一组比例",
    )

    # ---- B3 no_hits --------------------------------------------------
    gate = MemoryGate(budget=MemoryBudget(max_tokens=1200), min_score=0.2)
    d = gate.decide([])
    ok &= check("B3 空结果 → no_hits", d.reason == "no_hits" and not d.allow, d.reason)

    # ---- B4 敏感会话（在分数之前判定）---------------------------------
    # 敏感判定必须在分数之前：就算检索结果完美命中，敏感会话也不该注入。
    sensitive = MemoryGate(
        budget=MemoryBudget(max_tokens=1200),
        min_score=0.2,
        sensitive_tags=["PII", "hr"],
    )
    d = sensitive.decide(hits, session_tags=["pii"])
    ok &= check(
        "B4 敏感标签命中 → sensitive_session",
        d.reason == "sensitive_session" and not d.allow,
        f"reason={d.reason}",
    )
    ok &= check(
        "B4b 敏感判定优先于分数（高分也照拒）",
        sensitive.decide([hit("a.md", "完美命中", 999.0)], session_tags=["pii"]).reason
        == "sensitive_session",
        "分数再高也先看敏感",
    )
    d = sensitive.decide(hits, session_tags=["hr"])
    ok &= check(
        "B5 敏感标签大小写不敏感",
        d.reason == "sensitive_session",
        "session_tags=['hr'] 命中 sensitive_tags=['PII','hr']",
    )

    # ---- B6 below_min_score ------------------------------------------
    # **关键量纲事实**：`normalize_scores` 是"相对最佳分"（norm_i = s_i / max s），
    # 所以只要有一批非零分数，最高分那条的归一化值就恒为 1.0 ≥ min_score ——
    # 阈值**永远**不会把整批拒掉。唯一能触发 below_min_score 的情形是
    # **全 0 分**（best <= 0 → 全 0.0）。
    #
    # 这不是缺陷而是必须讲清的量纲后果：想按"绝对相关性"过滤，必须把
    # min_score 交给 ReMe 的 search job 在**原始分数**上过滤
    # （search.py:340 的 min_score 分支），而不是只在归一化分上卡一道。
    # 中间件两处都传，正是这个原因。
    d = gate.decide([hit("a.md", "无分命中", 0.0)], session_tags=[])
    ok &= check(
        "B6 全 0 分 → below_min_score",
        d.reason == "below_min_score" and not d.allow,
        f"reason={d.reason}（归一化后全 0.0 < {gate.min_score}）",
    )
    d = gate.decide([hit("a.md", "全文", 1.0), hit("b.md", "边缘", 0.01)], session_tags=[])
    ok &= check(
        "B7 多条命中时阈值裁掉尾部",
        d.allow and len(d.dropped_low_score) == 1,
        f"allowed={d.allow} kept={len(d.kept)} dropped_low_score={d.dropped_low_score}",
    )

    # ---- B8 over_budget ----------------------------------------------
    tiny = MemoryGate(budget=MemoryBudget(max_tokens=1), min_score=0.0)
    d = tiny.decide([hit("a.md", "很长的一段正文" * 50, 1.0)], session_tags=[])
    ok &= check(
        "B8 预算装不下任何一条 → over_budget",
        d.reason == "over_budget",
        f"budget.max_tokens=1 → {d.reason}",
    )

    # ---- B9 allowed ---------------------------------------------------
    d, kept = gate.apply(hits[:2], session_tags=[])
    ok &= check(
        "B9 正常放行",
        d.allow and len(kept) >= 1,
        f"kept={len(kept)}/{len(hits[:2])} reason={d.reason}",
    )

    # ---- B10 写入门控：五种 reason ------------------------------------
    wg = MemoryWriteGate(min_messages=2, min_chars=12)
    matrix: list[tuple[str, list[Any], str]] = [
        ("empty", [], "empty"),
        ("not_enough_messages", [UserMsg("u", "一句话")], "not_enough_messages"),
        (
            "no_user_text",
            [AssistantMsg("a", "我答了"), AssistantMsg("a", "我又答了")],
            "no_user_text",
        ),
        (
            "too_short",
            [UserMsg("u", "嗯"), AssistantMsg("a", "好的")],
            "too_short",
        ),
        (
            "allowed",
            [
                UserMsg("u", "把部署令牌放到 ~/.secrets/token，权限 600。"),
                AssistantMsg("a", "记下了，会用 600 权限。"),
            ],
            "allowed",
        ),
    ]
    for label, messages, expected in matrix:
        decision = wg.decide(messages)
        ok &= check(
            f"B10 写入门控 {label}",
            decision.reason == expected,
            f"reason={decision.reason} chars={decision.chars}",
        )

    # ---- B11 注入的 memory 消息不计入写入门控 -------------------------
    from agentscope.message import HintBlock  # noqa: PLC0415 - 只在断言里用到

    memory_msg = AssistantMsg(
        name="memory",
        content=[HintBlock(hint="## 相关长期记忆\n- 某条注入")],
    )
    decision = wg.decide(
        [
            UserMsg("u", "把部署令牌放到 ~/.secrets/token，权限 600。"),
            AssistantMsg("a", "记下了。"),
            memory_msg,
        ],
    )
    ok &= check(
        "B11 name='memory' 的注入消息被排除在增量之外",
        decision.reason == "allowed" and decision.messages == 2,
        f"messages={decision.messages}（投入 3 条，算 2 条）",
    )

    # ---- B12 关掉门控时 reason 是 disabled 而不是 allowed --------------
    # 分开的理由：日志里要能区分"检查通过了"与"根本没做检查"。
    off = MemoryWriteGate(min_messages=0)
    decision = off.decide([UserMsg("u", "一句话")])
    ok &= check(
        "B12 min_messages<=0 → disabled（不是 allowed）",
        decision.reason == "disabled" and decision.allow,
        f"reason={decision.reason} allow={decision.allow}",
    )
    ok &= check(
        "B12b 但空增量仍报 empty（disabled 排在 empty 之后）",
        off.decide([]).reason == "empty",
        f"reason={off.decide([]).reason}",
    )

    # ---- B13 传单条 Msg 而不是序列必须报错 ----------------------------
    # 为什么这条重要：`Msg` 是可迭代的（content 是列表）或至少容易被误传，
    # 静默接受一条 Msg 会让"条数"永远是 1，门控于是永远拒绝。
    try:
        wg.decide(UserMsg("u", "一句话"))  # type: ignore[arg-type]
    except TypeError as exc:
        ok &= check("B13 传单条 Msg → TypeError", True, str(exc)[:70])
    else:
        ok &= check("B13 传单条 Msg → TypeError", False, "没有抛异常")

    # ---- B14 拒绝时记指标 --------------------------------------------
    metrics = MemoryMetrics()
    wg_metrics = MemoryWriteGate(min_messages=3, metrics=metrics)
    wg_metrics.decide([UserMsg("u", "一句话"), AssistantMsg("a", "回答")], session_id="s1")
    snap = metrics.snapshot()
    ok &= check(
        "B14 写入门控拒绝也记 writeback 失败",
        snap["writebacks"] == 1.0 and snap["writeback_failures"] == 1.0,
        f"writebacks={snap['writebacks']} failures={snap['writeback_failures']}",
    )
    return ok


# ======================================================================
# C 端到端注入（真 Agent + 真嵌入式 ReMe，0 次模型调用）
# ======================================================================
#: 语料：一条用户偏好 + 一条运维事实，用来验证"检索回来的东西真的进了 context"。
CORPUS_PREF: str = """---
name: 绘图偏好
description: 用户对 matplotlib 图表的偏好
memory_tags: [pref, viz]
---

# 绘图偏好

用户偏好**深色主题**的 matplotlib 图表，坐标轴标签一律用中文。
配色优先 viridis，避免红绿同时出现（色盲不友好）。
"""

CORPUS_OPS: str = """---
name: 部署令牌位置
description: 部署令牌存放路径与权限
memory_tags: [ops]
---

# 部署令牌位置

生产部署令牌放在 `~/.secrets/token`，文件权限必须是 600。
轮换周期 90 天，轮换后要重启 `harness-gateway`。
"""


class RecordingEcho(EchoChatModel):
    """``EchoChatModel`` + 把**每次 provider 调用真正收到的 prompt**记下来。

    为什么要记 prompt 而不是看 ``state.context``：中间件把记忆注入到
    ``state.context`` 只是"意图"，真正决定 KV Cache 能不能复用的是
    **格式化之后发给 provider 的消息序列**。两者之间隔着一个 formatter
    （它会把 ``HintBlock`` 转成 user 消息、可能重写 system 消息），
    只有在 ``_call_api`` 这一层看才作数。

    Args:
        **kwargs (`Any`): 透传 :class:`~harness_kit.models.adapters.echo.EchoChatModel`。
    """

    def __init__(self, **kwargs: Any) -> None:
        """初始化并建空记录表。"""
        super().__init__(**kwargs)
        #: 每次 ``_call_api`` 收到的消息序列（序列化成 JSON 字符串）。
        self.prompts: list[list[str]] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Any:
        """记录本次 prompt，然后照常回放脚本。

        Args:
            model_name (`str`): 模型名。
            messages (`list[Msg]`): 输入消息。
            tools (`list[dict] | None`): 工具 schema。
            tool_choice (`Any`): 工具选择。
            **kwargs (`Any`): 透传。

        Returns:
            `Any`: :class:`EchoChatModel` 的返回值（流式时是 async generator）。
        """
        self.prompts.append([m.model_dump_json() for m in messages])
        return await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)

    # ------------------------------------------------------------------
    def first_memory_index(self, call: int = -1) -> int | None:
        """某次调用里第一条"记忆注入"消息的下标（找不到返回 ``None``）。

        ``HintBlock`` 会被 formatter 转成 user 消息，正文里带着注入块的标题，
        所以按标题文本找是最稳的定位方式。

        Args:
            call (`int`): 第几次调用（默认最后一次）。

        Returns:
            `int | None`: 下标。
        """
        marker = "相关长期记忆"
        for index, dumped in enumerate(self.prompts[call]):
            if marker in dumped:
                return index
        return None


async def _seed_and_reindex(
    client: MemoryClient,
    workspace: ReMeWorkspace,
    **texts: str,
) -> list[str]:
    """把语料入工作区并跑 ``reindex``。

    Args:
        client (`MemoryClient`): 已 start 的客户端。
        workspace (`ReMeWorkspace`): 工作区。
        **texts (`str`): ``名字 → 正文``。

    Returns:
        `list[str]`: 每个名字的入库路径。
    """
    ingestor = MemoryIngestor(client, workspace=workspace, chunker="markdown")
    written: list[str] = []
    for name, text in texts.items():
        result = await ingestor.add_text(text, name=name, tags=[])
        written.append(str(getattr(result, "path", name)))
    await client.run_job("reindex")
    return written


def _make_spy(mw: LongTermMemoryMiddleware) -> list[tuple[str, dict[str, Any]]]:
    """给中间件的 ``_run_job`` 装一个记录用的壳。

    为什么必须在这里拦：``min_score`` / ``tool_context_id`` 有没有真的
    传给 ReMe 的 search job，除了 job 入参之外**没有任何别的可观测量**
    —— 分数和去重桶都是 job 内部的事。官方的 ``_search``
    （``_middleware.py:475-487``）恰好在这一点上是空的。

    Args:
        mw (`LongTermMemoryMiddleware`): 中间件。

    Returns:
        `list[tuple[str, dict[str, Any]]]`: 追加式的 ``(job 名, 入参)`` 记录。
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    original = mw._run_job  # noqa: SLF001 - 故意包住私有方法

    async def spy(name: str, **kwargs: Any) -> Any:
        calls.append((name, dict(kwargs)))
        return await original(name, **kwargs)

    mw._run_job = spy  # type: ignore[method-assign]  # noqa: SLF001
    return calls


async def section_c() -> tuple[bool, dict[str, Any]]:
    """跑通"检索 → 注入 → 回复"的完整路径，并检查 job 入参。

    Returns:
        `tuple[bool, dict[str, Any]]`: ``(是否全部通过, 供 D/F 段复用的观察值)``。
    """
    banner("C 端到端注入：真 Agent + 真嵌入式 ReMe + 脚本化 echo（0 次真实模型调用）")
    ok = True
    observed: dict[str, Any] = {}

    client, workspace = await make_client("mw", "search", "reindex")
    written = await _seed_and_reindex(
        client,
        workspace,
        pref=CORPUS_PREF,
        ops=CORPUS_OPS,
    )
    show("已入库", written)

    metrics = MemoryMetrics()
    echo = RecordingEcho(
        model_name="echo",
        stream=True,
        script=[
            # 第 1 次调用：调一次 Calc —— 存在的意义是**多给一次推理步**，
            # 让 on_reasoning 有机会第二次轮询后台检索任务。
            {
                "text": "我先确认一下预算。",
                "tool_calls": [
                    {"id": "call-1", "name": "Calc", "input": {"expression": "2+2"}},
                ],
            },
            # 第 2 次调用：收尾。
            {"text": "画图建议用深色主题。"},
        ],
    )
    mw = LongTermMemoryMiddleware(
        workspace_dir=str(SANDBOX / "mw"),
        mode="static_control",
        top_k=3,
        min_score=0.2,
        tool_context_id="lesson19-c",
        budget=MemoryBudget(max_tokens=600),
        metrics=metrics,
        chat_model=echo,
    )
    show("兼容补丁", mw.compat_patched)
    show("中间件已 start", await _start(mw))

    calls = _make_spy(mw)
    agent = Agent(
        name="probe",
        system_prompt="你是助手。",
        model=echo,
        toolkit=Toolkit(
            tools=[
                FunctionTool(calc, name="Calc", is_read_only=True, is_concurrency_safe=True),
            ],
        ),
        middlewares=[mw],
    )
    out = await agent.reply(UserMsg("alice", "画图用什么主题？"))
    show("回复", out.get_text_content())

    # ---- C1 job 入参里真的带了 min_score / tool_context_id -----------
    searches = [kwargs for name, kwargs in calls if name == "search"]
    ok &= check(
        "C1 检索真的发生了",
        len(searches) >= 1,
        f"job 调用序列 = {[name for name, _ in calls]}",
    )
    if searches:
        first = searches[0]
        ok &= check(
            "C2 search job 收到 min_score（官方从不传）",
            first.get("min_score") == 0.2,
            f"min_score={first.get('min_score')}（官方 _search 只传 query/limit）",
        )
        ok &= check(
            "C3 search job 收到 tool_context_id（官方从不传）",
            first.get("tool_context_id") == "lesson19-c",
            f"tool_context_id={first.get('tool_context_id')!r}",
        )
        ok &= check(
            "C4 limit 来自 top_k",
            first.get("limit") == 3,
            f"limit={first.get('limit')}",
        )
        observed["search_kwargs"] = first

    # ---- C5 注入消息的形状 -------------------------------------------
    memory_msgs = [
        m
        for m in agent.state.context
        if isinstance(m, Msg) and getattr(m, "name", None) == "memory"
    ]
    ok &= check(
        "C5 单次 reply 只注入一条 memory 消息",
        len(memory_msgs) == 1,
        f"注入 {len(memory_msgs)} 条",
    )
    if memory_msgs:
        injected = memory_msgs[0]
        block_types = [getattr(b, "type", None) for b in (injected.content or [])]
        ok &= check(
            "C6 注入是 assistant 角色 + 只有 HintBlock",
            injected.role == "assistant" and block_types == ["hint"],
            f"role={injected.role} blocks={block_types}",
        )
        hint_text = injected.content[0].hint
        ok &= check(
            "C7 注入正文带引用行（预算开启时的渲染形态）",
            hint_text.startswith("## 相关长期记忆") and "### " in hint_text,
            f"首行={hint_text.splitlines()[0]!r}",
        )
        injected_tokens = estimate_tokens_heuristic(hint_text)
        ok &= check(
            "C8 注入正文不超过预算",
            injected_tokens <= 600,
            f"实际 {injected_tokens} token / 上限 600",
        )
        observed["hint_text"] = hint_text
        observed["hint_tokens"] = injected_tokens

    # ---- C9 注入位置：context 的倒数第二条 ----------------------------
    names = [
        (getattr(m, "name", None), m.role) for m in agent.state.context if isinstance(m, Msg)
    ]
    show("context 序列", names)
    ok &= check(
        "C9 注入落在 context 尾部（回复正文之前）",
        len(names) >= 2 and names[-2][0] == "memory",
        f"倒数第二条={names[-2] if len(names) >= 2 else None}",
    )

    # ---- C10 指标：命中率与注入量 -------------------------------------
    snap = metrics.snapshot()
    show("metrics.snapshot()", _short(snap))
    ok &= check(
        "C10 命中率与注入量都记下来了",
        snap["searches"] >= 1.0 and snap["hits"] >= 1.0 and snap["injected_tokens"] > 0.0,
        f"hits={snap['hits']} hit_rate={snap['hit_rate']} injected={snap['injected_tokens']}",
    )
    observed["metrics"] = snap
    observed["echo"] = echo
    observed["agent"] = agent
    observed["middleware"] = mw
    observed["client"] = client
    return ok, observed


async def _start(mw: LongTermMemoryMiddleware) -> str:
    """把嵌入式 ReMe app 起起来（幂等），返回状态字符串。

    Args:
        mw (`LongTermMemoryMiddleware`): 中间件。

    Returns:
        `str`: ``"started=True app=<类名>"``。
    """
    await mw._ensure_started()  # noqa: SLF001 - 官方就是这么用的
    # app.start() 会按它自己的 config 重新初始化 loguru（见 quieten_logs）。
    quieten_logs()
    return f"started={mw._started} app={type(mw._app).__name__}"  # noqa: SLF001


def _short(snap: dict[str, float]) -> str:
    """把指标快照压成一行（只留非零项）。

    Args:
        snap (`dict[str, float]`): :meth:`MemoryMetrics.snapshot` 的产物。

    Returns:
        `str`: 形如 ``{hits: 3.0, hit_rate: 1.0}``。
    """
    kept = {k: round(v, 3) for k, v in snap.items() if v}
    return "{" + ", ".join(f"{k}: {v}" for k, v in kept.items()) + "}"


# ======================================================================
# D 注入形状与 KV Cache
# ======================================================================
#: 序列化后**每次调用都会变**的字段。比较 prompt 前缀时必须先剥掉它们，
#: 否则 ``Msg.id`` / ``created_at`` 这些和"内容有没有变"无关的东西会让
#: 前缀断言永远失败。
_VOLATILE_KEYS: tuple[str, ...] = (
    "id",
    "created_at",
    "finished_at",
    "source",
)


def _stable(dump: str) -> str:
    """剥掉易变字段后的规范 JSON（用于比较两次调用之间"内容"是否相同）。

    Args:
        dump (`str`): ``Msg.model_dump_json()`` 的产物。

    Returns:
        `str`: 规范化（键排序、易变字段已剔除）的 JSON。
    """

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: strip(value)
                for key, value in sorted(node.items())
                if key not in _VOLATILE_KEYS
            }
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return json.dumps(strip(json.loads(dump)), ensure_ascii=False, sort_keys=True)


def _role_name(dump: str) -> str:
    """取一条消息的 ``name(role)`` 标签（打印用）。

    Args:
        dump (`str`): 消息的 JSON。

    Returns:
        `str`: 形如 ``alice(user)``。
    """
    obj = json.loads(dump)
    return f"{obj.get('name')}({obj.get('role')})"


def section_d(observed: dict[str, Any]) -> bool:
    """用 C 段记下的真实 prompt 序列，验证记忆是"追加在尾部"而不是"改写历史"。

    **这一段的结论不是想当然的**，它来自实测：

    1. 注入的 ``AssistantMsg(name="memory")`` 出现在 prompt 的**倒数第一位**，
       且只在**最后一次**调用里出现 —— 说明它是"检索就绪后追加"的，不是
       每轮重算的；
    2. system 消息与第一条 user 消息在两次调用之间**内容完全没变**
       （第一条 user 甚至还是同一个对象，连 ``id`` 都一样）—— 这是 KV Cache
       能复用的部分；
    3. 但**同一个 turn 内**的 prompt 不是严格"逐条追加"：AgentScope 会把
       runtime-state 提示（``agent/_config.py:288-296`` 的
       ``<system-reminder>`` 模板）注入到一个 assistant 消息里，而这条消息
       随后又累积了本轮的 text / tool_call / tool_result，于是它在两次
       调用之间**长大了**。这不是中间件造成的，是 Agent Loop 自己的行为 ——
       把它算进"KV Cache 能省多少"会高估。

    Args:
        observed (`dict[str, Any]`): C 段的观察值（含 ``echo`` / ``hint_text``）。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("D 注入形状与 KV Cache：prompt 前缀稳定性（实测，不是推断）")
    ok = True
    echo: RecordingEcho = observed["echo"]

    show("provider 被调用次数", len(echo.prompts))
    for index, prompt in enumerate(echo.prompts):
        show(f"  prompt[{index}]", " → ".join(_role_name(d) for d in prompt))

    ok &= check(
        "D1 至少发生了两次 provider 调用（否则测不到前缀）",
        len(echo.prompts) >= 2,
        f"{len(echo.prompts)} 次",
    )
    if len(echo.prompts) < 2:
        return ok

    first, last = echo.prompts[0], echo.prompts[-1]

    # ---- D2 system 消息内容不变（KV Cache 的第一段稳定前缀）-------------
    ok &= check(
        "D2 system 消息在两次调用之间内容不变",
        _stable(first[0]) == _stable(last[0]),
        f"{_role_name(last[0])} content={_stable(last[0])[:70]}…",
    )

    # ---- D3 第一条 user 消息逐字节相同 ---------------------------------
    # 逐字节（含 id）相同 ⇒ 两次调用里的就是**同一个 Msg 对象**，
    # 中间件 / Agent 都没有重建过它。历史被改写的话这里一定失败。
    ok &= check(
        "D3 第一条 user 消息逐字节相同（连 id 都一样）",
        first[1] == last[1],
        f"{_role_name(last[1])} id={json.loads(last[1])['id'][:12]}…",
    )

    # ---- D4 稳定前缀有多长 ---------------------------------------------
    stable_prefix = 0
    for index, dumped in enumerate(first):
        if index < len(last) and _stable(dumped) == _stable(last[index]):
            stable_prefix += 1
        else:
            break
    show("两次调用之间的稳定前缀长度", f"{stable_prefix} / {len(first)}")
    ok &= check(
        "D4 稳定前缀覆盖 system + user（≥2）",
        stable_prefix >= 2,
        f"{stable_prefix} 条内容逐字节等价；第 {stable_prefix} 条是本轮在飞的消息"
        "（AgentScope 会往里累积 text/tool_call/tool_result）",
    )
    ok &= check(
        "D5 不稳定的那条正是**最后一条**（历史没有被改写）",
        stable_prefix == min(len(first), len(last)) - 1,
        f"第 {stable_prefix} 条之后只剩在飞消息；注入也没插到前面去",
    )

    # ---- D6 注入出现在最后一次调用、且在尾部窗口 -----------------------
    inject_idx = echo.first_memory_index(-1)
    ok &= check(
        "D6 注入只在最后一次调用里出现",
        inject_idx is not None and echo.first_memory_index(0) is None,
        f"prompt[-1] 注入下标={inject_idx}，prompt[0] 注入下标={echo.first_memory_index(0)}",
    )
    if inject_idx is not None:
        ok &= check(
            "D7 注入落在 prompt 尾部窗口内",
            inject_idx >= len(last) - 2,
            f"下标 {inject_idx} / 长度 {len(last)}",
        )
        ok &= check(
            "D8 注入**没有**混进 system 消息（与 agentic_memory 路线相反）",
            "相关长期记忆" not in json.dumps(json.loads(last[0]).get("content")),
            "AgenticMemoryMiddleware 走 on_system_prompt（_middleware.py:513），"
            "每轮重写 system ⇒ 整条 KV Cache 失效",
        )

    # ---- D9 注入正文能在真实 prompt 里找到 ------------------------------
    hint = observed.get("hint_text") or ""
    ok &= check(
        "D9 注入正文能在真实 prompt 里找到（不是只活在 context 里）",
        any("相关长期记忆" in dumped for dumped in last),
        f"首行={hint.splitlines()[0]!r}",
    )
    ok &= check(
        "D10 注入形态是 hint 块（formatter 会把它转成 user 消息）",
        '"type":"hint"' in last[inject_idx] if inject_idx is not None else False,
        "官方 _build_memory_message 的注入形状，未改动",
    )

    # ---- D11 注入量记账与渲染文本一致 -----------------------------------
    snap = observed["metrics"]
    hint_tokens = observed.get("hint_tokens")
    ok &= check(
        "D11 injected_tokens == 渲染后文本的估计量（不是全量命中）",
        hint_tokens is not None and abs(snap["injected_tokens"] - hint_tokens) < 1e-6,
        f"metric={snap['injected_tokens']} 直接估计={hint_tokens}"
        "（两个数相等 ⇒ 预算计量的对象就是注入的对象）",
    )
    return ok


# ======================================================================
# E 写回：门控 / 异步 / 超时 / close 排空
# ======================================================================
def _increment() -> list[Msg]:
    """造一轮像样的"用户说 + 助手答"增量。

    Returns:
        `list[Msg]`: 增量消息。
    """
    return [
        UserMsg("alice", "把部署令牌放到 ~/.secrets/token，权限 600。"),
        AssistantMsg("probe", "记下了：路径 ~/.secrets/token，权限 600。"),
    ]


async def section_e() -> bool:
    """验证写回的三件事：门控拦截、异步化、超时与排空（全部离线）。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("E 写回：门控拦截 / 异步化 / 超时 / close() 排空（假 job，0 次真实调用）")
    ok = True
    workdir = SANDBOX / "writeback"

    # ---- E1 写入门控拦截：auto_memory 一次都不该被调 -------------------
    metrics = MemoryMetrics()
    mw_gated = LongTermMemoryMiddleware(
        workspace_dir=str(workdir),
        write_gate=MemoryWriteGate(min_messages=5, metrics=metrics),
        write_async=False,
    )
    calls: list[str] = []
    mw_gated._run_job = _recorder(calls)  # type: ignore[method-assign]  # noqa: SLF001
    await mw_gated._write_back(_increment(), "s1")  # noqa: SLF001
    ok &= check(
        "E1 写入门控拒绝 → 不调用 auto_memory",
        calls == [],
        f"job 调用={calls}；省掉一次 LLM 抽取",
    )
    snap = metrics.snapshot()
    ok &= check(
        "E2 被拒的写入记进 writeback_failures",
        snap["writebacks"] == 1.0 and snap["writeback_failures"] == 1.0,
        f"writebacks={snap['writebacks']} failures={snap['writeback_failures']}",
    )

    # ---- E3 放行时确实调用，且按 created/modified 判成功 ----------------
    # 这两条是**这次真实观测**（见 gating.py 模块 docstring）：auto_memory 在
    # "这轮没什么可记的"时会返回 success=True 但 created=false, modified=false。
    # 所以 success 不能当成功指标。
    metrics2 = MemoryMetrics()
    mw_ok = LongTermMemoryMiddleware(
        workspace_dir=str(workdir),
        write_async=False,
        metrics=metrics2,
    )
    calls2: list[str] = []
    mw_ok._run_job = _recorder(  # type: ignore[method-assign]  # noqa: SLF001
        calls2,
        response=_fake_response(success=True, created=False, modified=False),
    )
    landed = await mw_ok._do_write_back(_increment(), "s2")  # noqa: SLF001
    ok &= check(
        "E3 success=True 但 created/modified 都是 false → 判为未落盘",
        calls2 == ["auto_memory"] and landed is False,
        f"landed={landed}（官方只写了 success，没有任何指标能发现这件事）",
    )
    snap2 = metrics2.snapshot()
    ok &= check(
        "E4 未落盘记进 writeback_failures 而不是成功",
        snap2["writebacks"] == 1.0 and snap2["writeback_failures"] == 1.0,
        f"success_rate={snap2['writeback_success_rate']}",
    )

    metrics3 = MemoryMetrics()
    mw_landed = LongTermMemoryMiddleware(
        workspace_dir=str(workdir),
        write_async=False,
        metrics=metrics3,
    )
    mw_landed._run_job = _recorder(  # type: ignore[method-assign]  # noqa: SLF001
        [],
        response=_fake_response(success=True, created=True, modified=False),
    )
    landed2 = await mw_landed._do_write_back(_increment(), "s3")  # noqa: SLF001
    ok &= check(
        "E5 created=True → 判为落盘并记成功",
        landed2 is True and metrics3.snapshot()["writeback_success_rate"] == 1.0,
        f"landed={landed2} success_rate={metrics3.snapshot()['writeback_success_rate']}",
    )

    # ---- E6 没有 session_id 直接跳过（官方同款语义）--------------------
    metrics4 = MemoryMetrics()
    mw_nosession = LongTermMemoryMiddleware(workspace_dir=str(workdir), metrics=metrics4)
    calls4: list[str] = []
    mw_nosession._run_job = _recorder(calls4)  # type: ignore[method-assign]  # noqa: SLF001
    await mw_nosession._write_back(_increment(), None)  # noqa: SLF001
    ok &= check(
        "E6 没有 session_id → 跳过写回",
        calls4 == [] and metrics4.snapshot()["writebacks"] == 0.0,
        "官方同款：warning + return",
    )

    # ---- E7 异步写回：调用立刻返回，任务被持有 -------------------------
    metrics5 = MemoryMetrics()
    mw_async = LongTermMemoryMiddleware(
        workspace_dir=str(workdir),
        write_async=True,
        write_timeout_s=5.0,
        metrics=metrics5,
    )
    slow_calls: list[str] = []
    mw_async._run_job = _recorder(  # type: ignore[method-assign]  # noqa: SLF001
        slow_calls,
        delay=0.2,
        response=_fake_response(success=True, created=True),
    )
    started = time.perf_counter()
    await mw_async._write_back(_increment(), "s5")  # noqa: SLF001
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    ok &= check(
        "E7 write_async=True → 立即返回",
        elapsed_ms < 50.0 and len(mw_async._write_tasks) == 1,  # noqa: SLF001
        f"耗时 {elapsed_ms:.1f} ms（job 本身要 200 ms），在飞任务 {len(mw_async._write_tasks)}",  # noqa: SLF001
    )
    await asyncio.sleep(0.5)
    ok &= check(
        "E8 后台任务跑完后自动从集合里摘掉",
        slow_calls == ["auto_memory"] and len(mw_async._write_tasks) == 0,  # noqa: SLF001
        f"job={slow_calls} 剩余任务={len(mw_async._write_tasks)}",  # noqa: SLF001
    )
    ok &= check(
        "E9 异步写入的指标照样记账",
        metrics5.snapshot()["writebacks"] == 1.0,
        f"writebacks={metrics5.snapshot()['writebacks']}",
    )

    # ---- E10 超时：官方没有超时，一个卡住的 auto_memory 会挂住回复 -----
    metrics6 = MemoryMetrics()
    mw_timeout = LongTermMemoryMiddleware(
        workspace_dir=str(workdir),
        write_async=False,
        write_timeout_s=0.1,
        metrics=metrics6,
    )
    mw_timeout._run_job = _recorder(  # type: ignore[method-assign]  # noqa: SLF001
        [],
        delay=5.0,
        response=_fake_response(success=True, created=True),
    )
    started = time.perf_counter()
    landed3 = await mw_timeout._do_write_back(_increment(), "s6")  # noqa: SLF001
    timeout_ms = (time.perf_counter() - started) * 1000.0
    ok &= check(
        "E10 写回超时按失败记账并放行回复",
        landed3 is False and timeout_ms < 1000.0,
        f"耗时 {timeout_ms:.0f} ms（job 要 5000 ms），失败已记账",
    )
    ok &= check(
        "E11 超时记进 writeback_failures",
        metrics6.snapshot()["writeback_failures"] == 1.0,
        f"failures={metrics6.snapshot()['writeback_failures']}",
    )

    # ---- E12 close() 排空在飞写入 --------------------------------------
    metrics7 = MemoryMetrics()
    mw_drain = LongTermMemoryMiddleware(
        workspace_dir=str(workdir),
        write_async=True,
        write_timeout_s=5.0,
        metrics=metrics7,
    )
    drained: list[str] = []
    mw_drain._run_job = _recorder(  # type: ignore[method-assign]  # noqa: SLF001
        drained,
        delay=0.3,
        response=_fake_response(success=True, created=True),
    )
    await mw_drain._write_back(_increment(), "s7")  # noqa: SLF001
    ok &= check(
        "E12 close() 之前任务还在飞",
        len(mw_drain._write_tasks) == 1,  # noqa: SLF001
        "这就是异步化引入的窗口",
    )
    await mw_drain.close()
    ok &= check(
        "E13 close() 排空了在飞写入（记忆没丢）",
        drained == ["auto_memory"] and len(mw_drain._write_tasks) == 0,  # noqa: SLF001
        f"排空后 job 调用={drained}",
    )
    return ok


def _recorder(
    sink: list[str],
    *,
    response: Any = None,
    delay: float = 0.0,
) -> Any:
    """造一个假的 ``_run_job``：记下 job 名，按需延迟，返回固定响应。

    Args:
        sink (`list[str]`): 记录 `job` 名的列表。
        response (`Any`): 要返回的响应对象（``None`` 时返回一个成功响应）。
        delay (`float`): 每次调用前 ``await asyncio.sleep`` 的秒数。

    Returns:
        `Any`: 可直接赋给 ``mw._run_job`` 的协程函数。
    """

    async def _job(name: str, **kwargs: Any) -> Any:
        if delay:
            await asyncio.sleep(delay)
        sink.append(name)
        return response if response is not None else _fake_response(success=True, created=True)

    return _job


def _fake_response(
    *,
    success: bool = True,
    created: bool = False,
    modified: bool = False,
    n_messages: int = 2,
) -> SimpleNamespace:
    """造一个形如 ReMe ``Response`` 的替身（只带本脚本读的字段）。

    Args:
        success (`bool`): ``response.success``。
        created (`bool`): ``response.metadata["created"]``。
        modified (`bool`): ``response.metadata["modified"]``。
        n_messages (`int`): ``response.metadata["n_messages"]``。

    Returns:
        `SimpleNamespace`: 替身响应。
    """
    return SimpleNamespace(
        success=success,
        answer="ok",
        metadata={
            "created": created,
            "modified": modified,
            "n_messages": n_messages,
        },
    )


# ======================================================================
# F 多租户隔离与命中率指标
# ======================================================================
def section_f() -> bool:
    """验证租户路由的隔离性与 ``validate_tenant_id`` 的边界。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("F 多租户隔离与命中率指标")
    ok = True

    router = TenantRouter(root=SANDBOX / "tenants")
    acme = router.ensure_workspace("acme")
    globex = router.ensure_workspace("globex")

    ok &= check(
        "F1 两个租户的工作区物理隔离",
        acme.root != globex.root and not str(acme.root).startswith(str(globex.root)),
        f"acme={acme.root.name} globex={globex.root.name}",
    )
    router.assert_isolated("acme", "globex")
    ok &= check("F2 assert_isolated 通过", True, "两个根互不为前缀")

    bad = [
        ("..", "上跳"),
        ("a/b", "路径分隔符"),
        ("/abs", "绝对路径"),
        ("", "空串"),
        (".hidden", "点开头"),
        ("x" * 65, "超长"),
    ]
    rejected: list[str] = []
    for candidate, why in bad:
        try:
            router.validate_tenant_id(candidate)
        except TenantError:
            rejected.append(why)
    ok &= check(
        "F3 非法租户名全部被拒",
        len(rejected) == len(bad),
        f"{len(rejected)}/{len(bad)} 被拒：{', '.join(rejected)}",
    )
    ok &= check(
        "F4 合法租户名放行（含 . - _）",
        router.validate_tenant_id("acme-cn.dev_1") == "acme-cn.dev_1",
        "acme-cn.dev_1",
    )
    ok &= check(
        "F5 list_tenants 认得出建过的租户",
        set(router.list_tenants()) == {"acme", "globex"},
        str(sorted(router.list_tenants())),
    )

    # ---- F6 每租户一个目录 ⇒ 每租户一个中间件实例 ---------------------
    # 这是"多租户隔离"在中间件层的落法：ReMe 的工作区是中间件构造参数
    # （workspace_dir），而中间件的 Parameters 是构造后不可变的 ——
    # 所以隔离的单位就是实例，绝不能靠"运行时改 workspace_dir"。
    mw_acme = LongTermMemoryMiddleware(
        workspace_dir=str(acme.root),
        session_tags=("acme",),
    )
    mw_globex = LongTermMemoryMiddleware(
        workspace_dir=str(globex.root),
        session_tags=("globex",),
    )
    ok &= check(
        "F6 租户标签随实例固定（写回时的隔离依据）",
        mw_acme.session_tags_for(None) == ("acme",)  # type: ignore[arg-type]
        and mw_globex.session_tags_for(None) == ("globex",),  # type: ignore[arg-type]
        f"acme={mw_acme.session_tags_for(None)} globex={mw_globex.session_tags_for(None)}",  # type: ignore[arg-type]
    )
    ok &= check(
        "F7 实例的工作区目录不同",
        Path(mw_acme._workspace_dir) != Path(mw_globex._workspace_dir),  # noqa: SLF001
        f"{Path(mw_acme._workspace_dir).name} vs {Path(mw_globex._workspace_dir).name}",  # noqa: SLF001
    )

    # ---- F8 指标：命中率 / 门控率 / 写回率 -----------------------------
    metrics = MemoryMetrics()
    metrics.record_search(session_id="acme", hits=3, elapsed_ms=12.0)
    metrics.record_search(session_id="acme", hits=0, elapsed_ms=8.0)
    metrics.record_search(session_id="globex", hits=1, elapsed_ms=30.0)
    metrics.record_injection(session_id="acme", tokens=120, gated=False)
    metrics.record_injection(session_id="globex", tokens=0, gated=True)
    metrics.record_writeback(session_id="acme", ok=True)
    metrics.record_writeback(session_id="acme", ok=False)
    snap = metrics.snapshot()
    show("snapshot", _short(snap))
    ok &= check(
        "F8 hit_rate = 有命中的检索次数 / 总检索次数",
        snap["searches"] == 3.0 and snap["searches_with_hits"] == 2.0
        and abs(snap["hit_rate"] - 2.0 / 3.0) < 1e-9,
        f"hit_rate={snap['hit_rate']:.4f}",
    )
    ok &= check(
        "F9 gated_rate 能区分「检索正常但被门控拒」",
        snap["injections"] == 2.0 and snap["gated"] == 1.0 and snap["gated_rate"] == 0.5,
        f"injections={snap['injections']} gated={snap['gated']}",
    )
    ok &= check(
        "F10 writeback_success_rate 反映真实落盘率",
        snap["writebacks"] == 2.0 and snap["writeback_failures"] == 1.0
        and snap["writeback_success_rate"] == 0.5,
        f"success_rate={snap['writeback_success_rate']}",
    )
    ok &= check(
        "F11 会话维度可分开看（多租户下按租户核对）",
        metrics.session_snapshot("acme")["hits"] == 3.0
        and metrics.session_snapshot("globex")["hits"] == 1.0,
        f"acme.hits=3 globex.hits=1 sessions={sorted(metrics.sessions())}",
    )
    ok &= check(
        "F12 被拒的注入不计入 injected_tokens",
        snap["injected_tokens"] == 120.0,
        f"injected_tokens={snap['injected_tokens']}（gated 那次记 0）",
    )
    return ok


# ======================================================================
# G（--live）真实写回
# ======================================================================
async def section_g() -> bool:
    """用一次真实 deepseek-flash 调用验证"记忆真的落盘了"。

    Returns:
        `bool`: 全部断言是否通过。
    """
    banner("G（--live）真实写回：按 created/modified 判落盘")
    if not LIVE:
        print("  跳过（加 --live 才跑；本段会消耗 1 次 deepseek-flash 调用）")
        return True

    ok = True
    client, workspace = await make_client("live", "search", "reindex", "auto_memory")
    metrics = MemoryMetrics()
    mw = LongTermMemoryMiddleware(
        workspace_dir=str(SANDBOX / "live"),
        write_async=False,
        write_timeout_s=120.0,
        metrics=metrics,
    )
    # 这一步把 ReMe 的 as_llm 换成本仓库 .env 里的真实模型
    # （LLM_API_KEY / LLM_BASE_URL 由 harness 的 Settings 摊平；见中间件
    # 的 _export_llm_env 与官方 _config.py:288-289）。
    _export_live_env()
    await mw._ensure_started()  # noqa: SLF001
    quieten_logs()
    show("模型", model_name())

    messages = [
        UserMsg(
            "alice",
            "以后给我画图都用深色主题，坐标轴标签用中文，配色优先 viridis。",
        ),
        AssistantMsg("probe", "好的：深色主题 + 中文轴标签 + viridis 配色。"),
    ]
    landed = await mw._do_write_back(messages, "lesson19-live")  # noqa: SLF001
    show("created/modified 判定落盘", landed)
    show("写回指标", _short(metrics.snapshot()))
    ok &= check(
        "G1 真实写回被判定为落盘（created 或 modified）",
        landed is True,
        f"landed={landed}（False 表示这次抽取没产出卡片，见第 6 节踩坑表）",
    )
    written = sorted(
        str(p.relative_to(SANDBOX / "live"))
        for p in (SANDBOX / "live").rglob("*.md")
    )
    show("工作区新增文件", written[:6])
    ok &= check(
        "G2 工作区里真的多出了文件",
        len(written) >= 1,
        f"{len(written)} 个 .md",
    )
    await mw.close()

    # 回检之前必须 reindex：**刚写下的文件此刻还不在索引里**。
    # 生产路径上这件事由常驻的 `index_update_loop` 后台 job 负责
    # （第 17 讲的主题），这里手动跑一次，好让断言是确定性的。
    await mw._ensure_started()  # noqa: SLF001
    quieten_logs()
    await mw._run_job("reindex")  # noqa: SLF001
    hits = await mw._retrieve("画图用什么主题和配色？", session_id="lesson19-live")  # noqa: SLF001
    show("回检命中", len(hits))
    show("命中路径", [h.path for h in hits][:3])
    ok &= check(
        "G3 reindex 之后刚写入的记忆能被检索回来",
        len(hits) >= 1,
        f"命中 {len(hits)} 条：{[h.path for h in hits][:3]}",
    )
    ok &= check(
        "G4 回检命中带可核对的引用（path:start-end）",
        all(h.path and h.start_line >= 1 for h in hits) if hits else False,
        "预算开启时它会渲染成 ### path:start-end",
    )
    await mw.close()
    del client, workspace
    return ok


def _export_live_env() -> None:
    """把仓库 ``.env`` 里的凭据摊平到 ReMe 认识的变量名上。

    ``.env`` 里是 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``，而 AgentScope 的
    ReMe minimal config 读的是 ``LLM_API_KEY`` / ``LLM_BASE_URL``
    （``.../_reme/_config.py:288-289``）。不补的话，app 能起、检索能跑，
    但 ``auto_memory`` 会在第一次调用时打 ``Missing credentials`` 然后**静默**跳过。
    """
    pairs = (
        ("OPENAI_API_KEY", "LLM_API_KEY"),
        ("OPENAI_BASE_URL", "LLM_BASE_URL"),
        ("LLM_MODEL", "LLM_MODEL_NAME"),
    )
    written: list[str] = []
    for source, target in pairs:
        value = os.getenv(source)
        if value and os.getenv(target) != value:
            os.environ[target] = value
            written.append(target)
    show("补齐的 LLM 环境变量", written or "（已就绪）")


# ======================================================================
# main
# ======================================================================
async def main() -> int:
    """跑完全部段落。

    Returns:
        `int`: 0 表示全部断言通过，1 表示有失败。
    """
    print(f"python          = {sys.executable}")
    print(f"沙箱            = {SANDBOX}")
    print(f"LIVE            = {LIVE}")
    try:
        import reme

        print(f"reme            = {reme.__version__}  ({Path(reme.__file__).parent})")
    except Exception as exc:  # noqa: BLE001 - 打不出来不该让脚本挂掉
        print(f"reme            = <{exc}>（PYTHONPATH 里没有本地克隆？）")
    print(f"LLM_MODEL       = {model_name()}")
    print(f"OPENAI_BASE_URL = {os.getenv('OPENAI_BASE_URL')}")
    print(f"OPENAI_API_KEY  = {'已设置' if os.getenv('OPENAI_API_KEY') else '缺失'}")
    # `import reme` 刚刚把 loguru 的 sink 换成了它自己的 stdout INFO sink，
    # 这里夺回来（见 quieten_logs 的 docstring）。
    quieten_logs()

    results: dict[str, bool] = {}
    try:
        results["A 装配层"] = section_a()
        results["B 门控矩阵"] = section_b()
        passed_c, observed = await section_c()
        results["C 端到端注入"] = passed_c
        results["D 前缀稳定"] = section_d(observed)
        results["E 写回与异步"] = await section_e()
        results["F 租户与指标"] = section_f()
        results["G 真实写回"] = await section_g()
    finally:
        # 中间件持有的嵌入式 app 也要关（C 段那个），否则 ReMe 的后台任务
        # 会让解释器退出时挂住。
        mw = None
        try:
            mw = observed.get("middleware")  # type: ignore[assignment]
        except NameError:  # pragma: no cover - C 段没跑完
            mw = None
        if mw is not None:
            try:
                await mw.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("middleware close 失败: {}", exc)
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
    # 本脚本只在 macOS / Linux 上验证过；ReMe 的组件在 Windows 的事件循环下
    # 行为未测，显式写明免得读者误以为跨平台已测。
    if sys.platform == "win32":  # pragma: no cover
        raise SystemExit("本脚本在 macOS / Linux 上验证；Windows 未验证。")
    raise SystemExit(asyncio.run(main()))
