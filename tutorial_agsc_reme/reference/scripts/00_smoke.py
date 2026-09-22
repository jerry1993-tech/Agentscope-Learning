# -*- coding: utf-8 -*-
"""第 1 讲冒烟脚本：环境自检 + 首个 AgentScope Agent + 首次 ReMe 检索。

这个脚本是整套 20 讲教程的**地基验证器**。它只做四件事，每一件都必须 PASS：

1. **版本隔离自检**：断言 ``sys.path`` 里解析到的 ``reme`` 是仓库内的
   0.4.1.13（``third_party/ReMe``），而不是 ``site-packages`` 里那个会在导入期
   就 ``ModuleNotFoundError: No module named 'agentscope.token'`` 的旧版
   0.3.1.10。同时断言 ``agentscope`` 是我们 ``-e`` 安装的 2.0.8 本地源码。
2. **设置自检**：用 :meth:`harness_kit.settings.Settings.from_env` 读仓库根
   ``.env``，确认 LLM 三要素（key / base_url / model）齐备，且
   ``workspace_dir`` / ``session_dir`` 被幂等创建。
3. **首个 AgentScope Agent**：装配一个**真实**的 ``Agent``（带 ``Toolkit``，
   里面挂一个 ``FunctionTool``），跑一次 ``await agent.reply(...)``，
   走真 LLM（deepseek-flash）。**不自己写 Agent Loop**——``Agent`` 就是
   AgentScope 提供的 ReAct 状态机。
4. **首次 ReMe 检索**：用 ``reme.ReMe(**config)`` **嵌入式**装配（不起 HTTP
   服务），往工作区的 ``daily/<今天>/`` 写一篇 md，靠 ``_start()`` 拉起的后台
   ``index_update_loop`` 把文件吃进 BM25 索引，再 ``run_job("search", ...)``
   拿到带出处的命中片段。

用法（必须带 ``PYTHONPATH``，否则第 1 步就会失败）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/00_smoke.py

退出码：全部 PASS → 0；任意一项 FAIL → 1。

LLM 调用预算：本脚本只跑一次 ``reply``（内部约 2 次 model call，因为带了工具），
ReMe 侧全程 BM25，不调用 LLM。总计 **≤ 2 次**，远低于「单脚本 ≤ 6 次」的上限。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 0. 路径隔离：必须发生在任何 `import reme` / `import agentscope` 之前
# ---------------------------------------------------------------------------
_SCRIPTS_DIR: Path = Path(__file__).resolve().parent
_REFERENCE_ROOT: Path = _SCRIPTS_DIR.parent  # .../tutorial_agsc_reme/reference
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根，所以是 ``parents[1]``。
_REPO_ROOT: Path = _REFERENCE_ROOT.parents[1]

#: 本地 ReMe 克隆的源码根。把它放在 ``sys.path`` 最前面，才能压住
#: ``site-packages`` 里的 ``reme`` 0.3.1.10。
_REME_SRC: Path = _REPO_ROOT / "third_party" / "ReMe"

for _candidate in (str(_REME_SRC), str(_REFERENCE_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from dotenv import load_dotenv  # noqa: E402

#: ``override=False``：真实进程环境变量永远优先于 ``.env``。
load_dotenv(_REPO_ROOT / ".env", override=False)

import agentscope  # noqa: E402
import reme  # noqa: E402
from agentscope.agent import Agent  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.permission import (  # noqa: E402
    PermissionBehavior,
    PermissionDecision,
)
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402
from reme import ReMe  # noqa: E402
from reme.config import resolve_app_config  # noqa: E402

import harness_kit  # noqa: E402
from harness_kit.settings import Settings  # noqa: E402

EXPECTED_REME_VERSION: str = "0.4.1.13"
"""``third_party/ReMe/reme/__init__.py:3`` 里写的版本号。"""

EXPECTED_AGENTSCOPE_VERSION: str = "2.0.8"
"""``third_party/agentscope/src/agentscope/_version.py:4`` 里写的版本号。"""

_SMOKE_WS_NAME: str = "smoke"
"""冒烟用的 ReMe 工作区子目录名，落在 ``<repo>/.harness/reme/smoke``。"""

_RESULTS: list[tuple[str, bool, str]] = []
"""``(检查项, 是否通过, 详情)`` 三元组列表，最后统一打印。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def record(name: str, ok: bool, detail: str = "") -> bool:
    """登记一项检查结果并即时打印一行。

    Args:
        name (`str`): 检查项名字，例如 ``"reme-version"``。
        ok (`bool`): 是否通过。
        detail (`str`): 一行人类可读的补充说明。

    Returns:
        `bool`: 原样返回 ``ok``，方便调用处 ``return record(...)``。
    """
    _RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name:<24} {detail}")
    return ok


def section(title: str) -> None:
    """打印一条分节横幅。

    Args:
        title (`str`): 分节标题。
    """
    print(f"\n{'=' * 72}\n== {title}\n{'=' * 72}")


def note(text: str) -> None:
    """打印一条**信息性**输出（不计入 PASS/FAIL）。

    用于"想知道但不想让测试变脆"的观察项，例如"模型这一轮到底调没调工具"。

    Args:
        text (`str`): 要打印的内容。
    """
    print(f"       → {text}")


def is_local_source(module_file: str, marker: str) -> bool:
    """判断某个模块是不是从仓库内的本地源码导入的。

    **刻意不依赖 ``_REPO_ROOT``**：把本仓库整体拷到 ``/tmp`` 再跑时，
    ``agentscope`` 是 ``pip install -e`` 指向**原仓库**的，``_REPO_ROOT`` 与
    ``agentscope.__file__`` 的前缀对不上——那样写出来的断言是"位置耦合"的。

    判据只有两条：

    1. 路径里含 ``marker``（例如 ``third_party/agentscope/src/agentscope``）；
    2. 路径里**不含** ``site-packages`` / ``dist-packages``。

    第 2 条是关键：PyPI wheel 一定装在 ``site-packages`` 下，而本地源码不会。

    Args:
        module_file (`str`): 模块的 ``__file__``。
        marker (`str`): 期望出现的相对路径片段。

    Returns:
        `bool`: 是否判定为本地源码。
    """
    normalized = str(module_file).replace("\\", "/")
    if "site-packages" in normalized or "dist-packages" in normalized:
        return False
    return marker in normalized


# ---------------------------------------------------------------------------
# 检查 1：版本与路径隔离
# ---------------------------------------------------------------------------
def check_versions() -> bool:
    """断言两个库都解析到了仓库内的源码，且版本号符合预期。

    Returns:
        `bool`: 全部命中返回 ``True``。
    """
    section("检查 1 · 版本与路径隔离")

    ok = True

    ok &= record(
        "agentscope-version",
        agentscope.__version__ == EXPECTED_AGENTSCOPE_VERSION,
        f"agentscope {agentscope.__version__} <- {agentscope.__file__}",
    )
    ok &= record(
        "agentscope-source",
        is_local_source(agentscope.__file__, "third_party/agentscope/src/agentscope"),
        "必须是 -e 安装的本地源码（third_party/agentscope/src/agentscope），"
        "而不是 site-packages 里的 PyPI wheel",
    )
    ok &= record(
        "reme-version",
        reme.__version__ == EXPECTED_REME_VERSION,
        f"reme {reme.__version__} <- {reme.__file__}",
    )
    ok &= record(
        "reme-source",
        is_local_source(reme.__file__, "third_party/ReMe/reme"),
        f"必须来自 third_party/ReMe（当前 {reme.__file__}）；"
        "漏了 PYTHONPATH 就会是 site-packages 的 0.3.1.10",
    )
    ok &= record(
        "harness-kit-version",
        harness_kit.__version__ == "0.1.0",
        f"harness_kit {harness_kit.get_version()}",
    )
    return ok


# ---------------------------------------------------------------------------
# 检查 2：Settings
# ---------------------------------------------------------------------------
def check_settings() -> tuple[bool, Settings]:
    """构造 :class:`Settings` 并校验目录与 LLM 三要素。

    Returns:
        `tuple[bool, Settings]`: 是否通过，以及构造出来的设置对象。
    """
    section("检查 2 · Settings 读取 .env")

    settings = Settings.from_env()
    ok = True

    ok &= record(
        "env-file",
        (_REPO_ROOT / ".env").is_file(),
        f"{_REPO_ROOT / '.env'}",
    )
    ok &= record(
        "llm-key-present",
        settings.has_llm(),
        f"llm_api_key={settings.redacted()['llm_api_key']}",
    )
    ok &= record(
        "llm-base-url",
        bool(settings.llm_base_url),
        f"llm_base_url={settings.llm_base_url}",
    )
    ok &= record(
        "llm-model-name",
        bool(settings.llm_model_name),
        f"llm_model_name={settings.llm_model_name}",
    )

    settings.ensure_dirs()
    ws = settings.resolve(settings.workspace_dir)
    sd = settings.resolve(settings.session_dir)
    ok &= record("workspace-dir", ws.is_dir(), str(ws))
    ok &= record("session-dir", sd.is_dir(), str(sd))
    return ok, settings


# ---------------------------------------------------------------------------
# 检查 3：第一个 AgentScope Agent（真 LLM + 真工具）
# ---------------------------------------------------------------------------
def _word_count(text: str) -> str:
    """统计一段文本的字符数（工具函数本体，不消耗 LLM）。

    **返回类型必须注意**：``FunctionTool`` 只认 ``str`` / ``dict`` /
    ``ToolChunk`` 三种返回值，其余会被 ``json.dumps`` 掉
    （``third_party/agentscope/src/agentscope/tool/_adapters.py:177``
    的 ``_convert_func_result_to_chunk``）。这里刻意返回 ``str``。

    Args:
        text (`str`): 待统计的文本。

    Returns:
        `str`: 形如 ``"字符数：28"`` 的一行文本。
    """
    return f"字符数：{len(text)}"


def build_smoke_agent(settings: Settings) -> Agent:
    """装配一个真正会走 ReAct 循环的 AgentScope ``Agent``。

    这里刻意**只**写装配代码：模型用官方 ``DeepSeekChatModel``，工具用官方
    ``FunctionTool`` 包一个 Python 函数，工具集用官方 ``Toolkit``。没有一行
    属于"自己实现 Agent Loop"。

    Args:
        settings (`Settings`): 已读好 ``.env`` 的设置对象。

    Returns:
        `Agent`: 可以直接 ``await agent.reply(...)`` 的 Agent 实例。

    Raises:
        `ValueError`: ``.env`` 里缺少 LLM key / base_url / model 任一项。
    """
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model_name):
        raise ValueError(
            "缺少 LLM 配置：请在仓库根 .env 里写 OPENAI_API_KEY / "
            "OPENAI_BASE_URL / LLM_MODEL",
        )

    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        ),
        model=settings.llm_model_name,
        stream=False,  # 冒烟阶段关流式，直接拿到完整 Msg，便于断言
        parameters=DeepSeekChatModel.Parameters(max_tokens=512),
    )

    counter = FunctionTool(
        _word_count,
        name="word_count",
        description="统计一段文本的字符数，返回形如「字符数：12」的字符串。",
        is_read_only=True,
        # 铁律：自定义工具的 check_permissions 默认行为是 ASK，会 park 住对话；
        # 冒烟场景显式声明为 ALLOW，避免脚本被 HITL 卡死。
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="冒烟脚本：允许离线安全工具直接执行",
        ),
    )

    toolkit = Toolkit(tools=[counter])

    return Agent(
        name="smoke-agent",
        system_prompt=(
            "你是一个环境自检助手。需要数数时调用 word_count 工具，"
            "然后用一句中文回答，不要复述工具输出以外的内容。"
        ),
        model=model,
        toolkit=toolkit,
    )


async def check_agentscope_agent(settings: Settings) -> bool:
    """跑一次真实 ``reply``，断言回复非空且上下文被写回。

    Args:
        settings (`Settings`): 设置对象。

    Returns:
        `bool`: 是否通过。
    """
    section("检查 3 · 第一个 AgentScope Agent（真 LLM）")

    agent = build_smoke_agent(settings)
    request = "请数一下「Agent Harness 是模型外围的运行管控基础设施」这句话有多少个字符。"
    started = time.perf_counter()
    reply = await agent.reply(UserMsg("user", request))
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    text = reply.get_text_content() or ""
    usage = reply.usage
    prompt_tokens = getattr(usage, "input_tokens", 0) or 0
    completion_tokens = getattr(usage, "output_tokens", 0) or 0

    # 统计这一轮里模型真的发起了几次工具调用（信息性，不作为 PASS 条件——
    # 模型是否调工具取决于模型自己，把它写成硬断言会让冒烟脚本随模型版本变脆）。
    tool_call_count = sum(
        1
        for msg in agent.state.context
        for block in msg.content
        if getattr(block, "type", None) == "tool_call"
    )

    ok = True
    ok &= record(
        "agent-reply-nonempty",
        bool(text.strip()),
        f"len(text)={len(text)} reply=\"{text.strip()[:40]}\"",
    )
    ok &= record(
        "agent-context-written",
        len(agent.state.context) == 2,
        f"len(agent.state.context)={len(agent.state.context)}（1 条 user + 1 条 assistant）",
    )
    ok &= record(
        "agent-usage-counted",
        prompt_tokens > 0 and completion_tokens > 0,
        f"input_tokens={prompt_tokens} output_tokens={completion_tokens}",
    )
    note(f"本轮工具调用次数：{tool_call_count}")
    note(f"单次 reply 耗时：{elapsed_ms:.0f} ms")
    return ok


# ---------------------------------------------------------------------------
# 检查 4：第一次 ReMe 检索（嵌入式，不起服务）
# ---------------------------------------------------------------------------
def build_reme_app(settings: Settings) -> tuple[ReMe, Path]:
    """嵌入式装配一个 ReMe 应用（``reme.ReMe(**config)``），不起任何服务。

    三条硬约束（缺一条就会踩坑）：

    1. ``ReMe(**config)`` **不会**自动读 ``reme/config/default.yaml``，必须
       先显式调 :func:`reme.config.resolve_app_config`，否则 ``jobs`` 是空
       dict，``run_job`` 立刻 ``KeyError: Job 'search' not found``；
    2. ``as_llm`` 组件只读 ``LLM_API_KEY`` / ``LLM_BASE_URL`` /
       ``LLM_MODEL_NAME`` / ``LLM_BACKEND`` 四个环境变量，而本仓库 ``.env``
       写的是 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``LLM_MODEL``，
       所以必须手工映射；
    3. ``default.yaml`` 里 ``service.web_enabled`` / ``mcp_enabled`` 默认是
       ``true``，这里显式关掉，保证脚本不占端口。

    Args:
        settings (`Settings`): 设置对象，提供仓库根与 LLM 三要素。

    Returns:
        `tuple[ReMe, Path]`: 装配好但尚未 ``_start()`` 的应用，以及它的工作区根目录。
    """
    workspace = settings.resolve(Path(".harness") / "reme" / _SMOKE_WS_NAME)
    workspace.mkdir(parents=True, exist_ok=True)

    cfg: dict[str, Any] = resolve_app_config(log_config=False)
    cfg["workspace_dir"] = str(workspace)
    cfg["enable_logo"] = False
    cfg["service"] = {
        **cfg["service"],
        "web_enabled": False,
        "mcp_enabled": False,
    }
    cfg["components"]["as_llm"]["default"].update(
        {
            "model": settings.llm_model_name,
            "credential": {
                "api_key": settings.llm_api_key,
                "base_url": settings.llm_base_url,
            },
        },
    )
    return ReMe(**cfg), workspace


def _seed_daily_note(workspace: Path, daily_dir_name: str) -> Path:
    """往 ``daily/<今天>/`` 写一篇带 front matter 的 md。

    ``index_update_loop``（后台 job）的 watch 目录之一就是 ``daily_dir``，
    所以只要文件落在里面，``_start()`` 之后就会被动建索引。

    Args:
        workspace (`Path`): ReMe 工作区根目录。
        daily_dir_name (`str`): ``ApplicationConfig.daily_dir`` 的值（默认 ``"daily"``）。

    Returns:
        `Path`: 写入的文件路径。
    """
    target_dir = workspace / daily_dir_name / date.today().isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    note = target_dir / "harness_intro.md"
    note.write_text(
        "---\n"
        "name: harness_intro\n"
        "description: Agent Harness 第 1 讲的冒烟笔记\n"
        "memory_tags: [agent, harness]\n"
        "---\n"
        "# Hello Harness\n\n"
        "Agent Harness 是模型外围的运行管控基础设施，负责 Agent Loop 与 Tool Use。\n"
        "它把模型之外的一切运行时控制设施（会话、记忆、工具、沙箱、权限、评测）收拢成一层。\n",
        encoding="utf-8",
    )
    return note


async def check_reme_search(settings: Settings) -> bool:
    """嵌入式跑一次 ReMe ``search`` job，断言检索命中且带出处。

    整个检查**不调用 LLM**：``default.yaml`` 里
    ``file_store.default.embedding_store`` 被显式设成空串，因此索引只有 BM25，
    检索走关键词通道即可命中。

    Args:
        settings (`Settings`): 设置对象。

    Returns:
        `bool`: 是否通过。
    """
    section("检查 4 · 第一次 ReMe 检索（嵌入式，零 LLM 调用）")

    app, workspace = build_reme_app(settings)
    ok = True

    jobs = sorted(app.context.jobs)
    ok &= record(
        "reme-jobs-registered",
        "search" in jobs and "index_update_loop" in jobs,
        f"共 {len(jobs)} 个 job（含 background index_update_loop）",
    )

    note = _seed_daily_note(workspace, app.config.daily_dir)
    print(f"       → 已写入待索引文件：{note}")

    await app._start()
    try:
        # index_update_loop 是 BackgroundJob，第二个 step 是 watch_changes_step
        # 里的 awatch 长驻循环——直接 run_job 它会永久挂住。这里等它跑完一轮。
        await asyncio.sleep(3.0)

        started = time.perf_counter()
        resp = await app.run_job(
            "search",
            query="Agent Harness 是什么",
            limit=3,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        answer = resp.answer if isinstance(resp.answer, str) else str(resp.answer)
        counts = (resp.metadata or {}).get("counts", {})

        ok &= record("reme-search-success", bool(resp.success), f"success={resp.success}")
        ok &= record(
            "reme-search-hit",
            "harness_intro.md" in answer,
            f"命中片段 {len(answer)} 字符，counts={counts}",
        )
        ok &= record(
            "reme-search-metadata",
            {"results", "counts"} <= set(resp.metadata or {}),
            f"metadata keys={sorted((resp.metadata or {}).keys())}",
        )
        print(f"       → 检索耗时：{elapsed_ms:.0f} ms")
        print("       → answer 首 200 字符：")
        print("         " + answer.strip()[:200].replace("\n", "\n         "))
    finally:
        await app._close()

    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main() -> int:
    """串起四项检查，打印汇总表。

    Returns:
        `int`: 全部通过返回 ``0``，否则 ``1``。
    """
    print("harness_kit smoke · 第 1 讲环境自检")
    print(
        f"python={sys.version.split()[0]}  repo={_REPO_ROOT}  "
        f"cwd={Path.cwd()}",
    )

    ok = check_versions()
    ok_settings, settings = check_settings()
    ok &= ok_settings

    if ok_settings and settings.has_llm():
        try:
            ok &= await check_agentscope_agent(settings)
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本要吞掉一切并如实报告
            ok &= record("agent-reply", False, f"{type(exc).__name__}: {exc}")
    else:
        ok &= record("agent-reply", False, "跳过：缺少 LLM 配置")

    try:
        ok &= await check_reme_search(settings)
    except Exception as exc:  # noqa: BLE001
        ok &= record("reme-search", False, f"{type(exc).__name__}: {exc}")

    section("汇总")
    passed = sum(1 for _, item_ok, _ in _RESULTS if item_ok)
    total = len(_RESULTS)
    for name, item_ok, detail in _RESULTS:
        mark = "PASS" if item_ok else "FAIL"
        print(f"  [{mark}] {name:<24} {detail}")
    print(f"\n通过 {passed}/{total}")

    if ok:
        print("\n=== SMOKE PASS ===")
        return 0
    print("\n=== SMOKE FAIL ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
