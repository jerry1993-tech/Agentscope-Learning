#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第 15 讲《ReMe 架构总览：从 CLI 到 Step 的完整运行时》验证脚本。

跑法（仓库根或任意目录，用绝对路径即可）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/01_reme_doctor.py

加 ``--live`` 会多跑 G 段：真实调一次 deepseek-flash，用来证明
``HarnessMemoryConfig`` 把凭据注进 ``components.as_llm.default.credential`` 这件事
真的生效了（**1 次计费调用**）。

七段的结构与「消耗几次模型调用」：

===  ==============================================================  ============
段   内容                                                            模型调用
===  ==============================================================  ============
A    ``import reme`` 的版本与路径自检（防 0.3.1.10 污染）              0
B    ``ReMeWorkspace``：六个子目录、路径换算、清理、销毁               0
C    ``HarnessMemoryConfig``：从 ``resolve_app_config`` 构建全量配置    0
D    ``MemoryDoctor``：12 项体检（版本 / 配置 / 注册表 / 工作区）       0
E    ``MemoryClient``：嵌入式装配 + 真实 ``run_job``（含 write→索引→检索）0
F    Profile 纳管：``load_profile("coding")`` → ``MemorySpec`` → 配置    0
G    （``--live``）真实 deepseek-flash 走 ``as_llm.default``            1
===  ==============================================================  ============

**A~F 段完全离线、可重复、可 CI**：它们只调用 ReMe 里不碰 LLM 的 job
（``version`` / ``health_check`` / ``write`` / ``reindex`` / ``search``）。
唯一会真实出网的是 G 段，而且它**不是**本讲的验证依赖 —— 本讲要证明的东西
（工作区语义、配置装配、job 生命周期、注册表体检）全部在 A~F 段就被钉死了。

输出里的路径统一做了归一化（工作区根打成 ``$WS``、reference 目录打成 ``$REF``），
所以同一份脚本在任何机器上跑出来的文本是一样的。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

# ----------------------------------------------------------------------
# 路径与 .env（必须早于任何 reme / agentscope / harness_kit 的 import）
# ----------------------------------------------------------------------
#: ``<repo>/tutorial_agsc_reme/reference``
REF: Path = Path(__file__).resolve().parents[1]
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根
REPO: Path = REF.parents[1]
#: 本地 ReMe 克隆必须排在 ``sys.path`` 最前（压住 site-packages 里的 0.3.1.10）。
REME_SRC: Path = REPO / "third_party" / "ReMe"

for _candidate in (str(REME_SRC), str(REF)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:  # python-dotenv 是 pyproject 里声明的依赖
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:  # pragma: no cover - 本环境已装
    pass

from harness_kit.config import load_profile  # noqa: E402
from harness_kit.memory import (  # noqa: E402
    DEFAULT_SUBDIRS,
    EMBEDDED_JOB_BACKENDS,
    HarnessMemoryConfig,
    MemoryClient,
    MemoryConfigError,
    MemoryDoctor,
    MemoryJobError,
    MemoryUnavailableError,
    ReMeWorkspace,
    WorkspaceError,
    reme_available,
    reme_version,
)

#: 是否跑真实模型那一段。
LIVE: bool = "--live" in sys.argv

#: 归一化用的替换表，在 :func:`main` 里填充。
_SHORTEN: list[tuple[str, str]] = []

#: ``MemoryDoctor`` 里**已知**的一处失败：AgentScope 2.0.8 的
#: ``_config._dream_steps()`` 引用了 reme 0.4.1.13 没注册的 ``dream_topics_step``。
#: 它由第 19 讲的 ``ensure_reme_compat()`` 修，本讲只把它**如实报出来** ——
#: 这正是 ``MemoryDoctor`` 的价值：把"将来一定会在 start() 里炸的东西"提前抓出来。
KNOWN_GAP: frozenset[str] = frozenset({"agentscope.reme_middleware"})


def shorten(text: str) -> str:
    """把机器相关的绝对路径换成 ``$WS`` / ``$REF`` / ``$REPO`` / ``$ANCHOR``。

    Args:
        text (`str`): 任意文本。

    Returns:
        `str`: 归一化后的文本。
    """
    out = str(text)
    for raw, alias in _SHORTEN:
        out = out.replace(raw, alias)
    return out


def p(text: str = "") -> None:
    """打印一行，并把路径归一化。

    Args:
        text (`str`): 待打印的文本。
    """
    sys.stdout.write(shorten(text) + "\n")


def head(title: str) -> None:
    """打印段标题。

    Args:
        title (`str`): 段标题。
    """
    p("")
    p("=" * 72)
    p(f"  {title}")
    p("=" * 72)


# ======================================================================
# A · 版本与路径自检
# ======================================================================
def section_a() -> dict[str, Any]:
    """A 段：确认 ``import reme`` 解析到的是本地 0.4.1.13 而不是 site-packages 的旧版。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str], "remote": str}``。
    """
    head("A · 环境自检（这一段的唯一目的是证伪 0.3.1.10）")
    failures: list[str] = []

    import agentscope
    import reme

    p(f"python            : {sys.version.split()[0]} @ {sys.executable}")
    p(f"sys.path[0]       : {_SHORTEN_PATH(sys.path[0])}")
    p(f"reme.__version__  : {reme.__version__}")
    p(f"reme.__file__     : {_SHORTEN_PATH(reme.__file__)}")
    p(f"agentscope.__version__: {agentscope.__version__}")

    if reme.__version__ != "0.4.1.13":
        failures.append(f"reme 版本不是 0.4.1.13 而是 {reme.__version__}（PYTHONPATH 没设？）")
    if "third_party/ReMe" not in reme.__file__:
        failures.append(f"reme 解析到了仓库外的路径: {reme.__file__}")
    if not reme_available():
        failures.append("reme_available() 返回 False")
    if reme_version() != reme.__version__:
        failures.append("reme_version() 与 reme.__version__ 不一致")

    p(f"reme_available()  : {reme_available()}")
    p(f"reme_version()    : {reme_version()}")
    p(f"结论              : {'PASS' if not failures else 'FAIL'}")
    for item in failures:
        p(f"  ! {item}")
    return {"ok": not failures, "failures": failures, "remote": reme.__version__}


def _SHORTEN_PATH(path: str | os.PathLike[str]) -> str:  # noqa: N802 - 故意用全大写做归一化钩子
    """把绝对路径里的仓库前缀换成 ``$REPO`` / ``$REF``。

    Args:
        path (`str | os.PathLike[str]`): 任意路径。

    Returns:
        `str`: 归一化后的字符串。
    """
    text = str(path)
    for raw, alias in _SHORTEN:
        text = text.replace(raw, alias)
    return text


# ======================================================================
# B · 工作区生命周期
# ======================================================================
def section_b(ws: ReMeWorkspace) -> dict[str, Any]:
    """B 段：工作区的创建、校验、路径换算与清理。

    Args:
        ws (`ReMeWorkspace`): 待操作的工作区。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str]}``。
    """
    head("B · ReMeWorkspace：工作区目录语义与路径治理")
    failures: list[str] = []

    p("DEFAULT_SUBDIRS =")
    for field_name, dirname in DEFAULT_SUBDIRS:
        p(f"  {field_name:<16} -> {dirname}")

    # --- 构造前先证伪一个真实存在过的坑：空 root 会被 Path("") 悄悄变成 cwd ---
    try:
        ReMeWorkspace(root="")
        failures.append("ReMeWorkspace(root='') 居然没有抛 WorkspaceError")
        p("空 root 检查      : FAIL（见上）")
    except WorkspaceError as exc:
        p(f"空 root 检查      : PASS -> {type(exc).__name__}")

    ws.ensure()
    p(f"root              : {_SHORTEN_PATH(ws.root)}")
    for name, _ in DEFAULT_SUBDIRS:
        p(f"  {name:<16} {_SHORTEN_PATH(ws.subdir(name))}")
    p(f"validate()        : {ws.validate() or '[] （健康）'}")
    p(f"is_healthy()      : {ws.is_healthy()}")
    p(f"stats()           : {ws.stats()}")
    p(f"fingerprint()     : {ws.fingerprint()}")
    p(f"dialog_path()     : {_SHORTEN_PATH(ws.dialog_path())}")
    p(f"dir_overrides()   : {sorted(ws.dir_overrides())}")

    # --- 路径换算：相对进必须相对出，否则 tag_index 会直接拒绝 ---
    p(f"relative('daily/2026-09-22/a.md')  -> {ws.relative('daily/2026-09-22/a.md')}")
    p(f"relative(<abs 区外文件>)            -> {_SHORTEN_PATH(ws.relative('/etc/hosts'))}")
    p(f"is_inside('daily/x.md')            -> {ws.is_inside('daily/x.md')}")
    p(f"is_inside('../escape.md')          -> {ws.is_inside('../escape.md')}")
    p(f"resolve_relative('daily/x.md')     -> {_SHORTEN_PATH(ws.resolve_relative('daily/x.md'))}")
    try:
        ws.resolve_relative("../escape.md")
        failures.append("resolve_relative('../escape.md') 没有拦住越界")
    except WorkspaceError:
        p("resolve_relative('../escape.md')   -> WorkspaceError（越界已拦下）")

    # --- 清理：keep='index' 只删垃圾，不删记忆正文 ---
    (ws.daily_path() / "2026-09-22").mkdir(parents=True, exist_ok=True)
    (ws.daily_path() / "2026-09-22" / "card.md").write_text("记忆正文", encoding="utf-8")
    (ws.metadata_path() / "junk.tmp").write_text("垃圾", encoding="utf-8")
    report = ws.clean(keep="index")
    p(f"clean(keep=index) : removed={report.removed_files} kept={len(report.kept)} 项")
    if not (ws.daily_path() / "2026-09-22" / "card.md").is_file():
        failures.append("clean(keep='index') 把记忆正文删了")
    if (ws.metadata_path() / "junk.tmp").exists():
        failures.append("clean(keep='index') 没删掉 *.tmp")

    # --- 销毁：必须显式 confirm ---
    try:
        ws.destroy()
        failures.append("destroy() 没有要 confirm=True")
    except WorkspaceError:
        p("destroy() 无 confirm : WorkspaceError（防手滑已生效）")

    p(f"结论              : {'PASS' if not failures else 'FAIL'}")
    for item in failures:
        p(f"  ! {item}")
    return {"ok": not failures, "failures": failures}


# ======================================================================
# C · 配置构建
# ======================================================================
def section_c(builder: HarnessMemoryConfig) -> dict[str, Any]:
    """C 段：从 ReMe 的 ``default.yaml`` 构建一份嵌入式配置。

    Args:
        builder (`HarnessMemoryConfig`): 配置构建器。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str], "config": dict}``。
    """
    head("C · HarnessMemoryConfig：把 default.yaml 变成嵌入式装配配置")
    failures: list[str] = []

    cfg = builder.build()

    p("describe():")
    for line in builder.describe().splitlines():
        p(f"  {line}")

    backends = sorted({str(spec.get("backend", "")) for spec in cfg["jobs"].values()})
    p(f"job 后端集合      : {backends}")
    p(f"嵌入式允许的后端  : {sorted(EMBEDDED_JOB_BACKENDS)}")
    p(f"service.backend   : {cfg['service']['backend']}")
    p(f"workspace_dir     : {_SHORTEN_PATH(cfg['workspace_dir'])}")
    p(f"reindex steps     : {[s['backend'] for s in cfg['jobs']['reindex']['steps']]}")
    p(f"reindex watch_dirs: {cfg['jobs']['reindex']['watch_dirs']}")
    p(f"as_llm.default    : {sorted(cfg['components']['as_llm']['default'])}")

    if set(backends) - set(EMBEDDED_JOB_BACKENDS):
        failures.append(f"混进了常驻 job 后端: {sorted(set(backends) - set(EMBEDDED_JOB_BACKENDS))}")
    if cfg["service"]["backend"] != "cli":
        failures.append(f"service.backend 不是 cli 而是 {cfg['service']['backend']!r}")
    if "clear_store_step" not in [s["backend"] for s in cfg["jobs"]["reindex"]["steps"]]:
        failures.append("reindex 没有被覆盖成 rescan 版本")
    if not cfg["jobs"]["reindex"].get("watch_dirs"):
        failures.append("rescan 版 reindex 缺 watch_dirs（会让索引清空后装不回来）")
    if cfg["workspace_dir"] != str(builder.workspace.root):
        failures.append("workspace_dir 没有钉死到 ReMeWorkspace.root")
    if "background" in cfg["jobs"] or "index_update_loop" in cfg["jobs"]:
        failures.append("index_update_loop 这种 background job 没被过滤掉")

    # --- 白名单的两种失效方式都必须报错，而不是静默吞掉 ---
    # 用**全新的 builder**做这两次试验：with_jobs() 会就地改 job_whitelist，
    # 复用一个 builder 会让后面的检查带上前一次的污染。
    def _fresh() -> HarnessMemoryConfig:
        return HarnessMemoryConfig(workspace=builder.workspace, settings=builder.settings)

    try:
        _fresh().with_jobs("search", "no_such_job").build()
        failures.append("白名单里写错 job 名居然没报错")
    except MemoryConfigError as exc:
        p(f"错名白名单        : MemoryConfigError -> {str(exc)[:60]}...")

    # 第二种失效方式：名字拼对了，但它的后端是 cron/background，会被嵌入式过滤掉。
    # 如果只做"交集"，with_jobs("search", "dream_cron") 会安静地退化成只留 search。
    try:
        _fresh().with_jobs("search", "dream_cron").build()
        failures.append("白名单里的常驻 job 被静默丢掉了（应当报错）")
    except MemoryConfigError as exc:
        p(f"常驻名白名单      : MemoryConfigError -> {str(exc)[:60]}...")

    p(f"结论              : {'PASS' if not failures else 'FAIL'}")
    for item in failures:
        p(f"  ! {item}")
    return {"ok": not failures, "failures": failures, "config": cfg}


# ======================================================================
# D · 体检
# ======================================================================
def section_d(cfg: dict[str, Any]) -> dict[str, Any]:
    """D 段：跑 ``MemoryDoctor`` 的全部检查项。

    ``agentscope.reme_middleware`` 这一项在本环境**预期失败**（见 :data:`KNOWN_GAP`），
    所以它不计入失败，但要如实打印出来。

    Args:
        cfg (`dict[str, Any]`): ReMe app config。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str]}``。
    """
    head("D · MemoryDoctor：把「静默降级」变成一张检查表")
    doctor = MemoryDoctor(cfg)
    for line in doctor.report().splitlines():
        p(line)

    unexpected = [r for r in doctor.check() if not r.ok and r.name not in KNOWN_GAP]
    gaps = [r for r in doctor.check() if not r.ok and r.name in KNOWN_GAP]
    for gap in gaps:
        p(f"已知缺口（不计失败）: {gap.name} —— 由第 19 讲的 ensure_reme_compat() 修")
    p(f"doctor.ok()       : {doctor.ok()}（因为含已知缺口，预期是 False）")
    p(f"非预期失败         : {[r.name for r in unexpected] or '无'}")
    p(f"结论              : {'PASS' if not unexpected else 'FAIL'}")
    return {"ok": not unexpected, "failures": [f"{r.name}: {r.detail}" for r in unexpected]}


# ======================================================================
# E · 嵌入式装配 + 真实 run_job
# ======================================================================
async def section_e(cfg: dict[str, Any]) -> dict[str, Any]:
    """E 段：用 ``MemoryClient`` 起一个嵌入式 ReMe，跑 write → reindex → search。

    Args:
        cfg (`dict[str, Any]`): ReMe app config。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str], "hits": str}``。
    """
    head("E · MemoryClient：嵌入式装配 + 真实 run_job（0 次 LLM 调用）")
    failures: list[str] = []
    hits_text = ""

    client = MemoryClient(cfg, start_timeout_s=120.0)
    started = time.perf_counter()
    try:
        await client.start()
    except TimeoutError as exc:
        failures.append(f"start() 超时: {exc}")
        p(f"结论              : FAIL -> {exc}")
        return {"ok": False, "failures": failures, "hits": hits_text}

    p(f"started           : {client.started}（{time.perf_counter() - started:.2f}s）")
    jobs = client.job_names()
    p(f"job 数             : {len(jobs)}")
    p(f"job 列表           : {', '.join(jobs)}")
    p(f"组件              : { {k: sorted(v) for k, v in sorted(client.application.context.components.items())} }")
    p(f"service 类型       : {type(client.application.context.service).__name__}")

    # --- 未 start 时 run_job 会假成功：这里正面验证那道保险 ---
    fresh = MemoryClient(cfg)
    try:
        await fresh.run_job("version")
        failures.append("未 start() 的客户端居然跑通了 run_job（假成功没有被拦住）")
    except MemoryUnavailableError:
        p("未 start 就 run_job : MemoryUnavailableError（假成功被拦下）")

    # --- 真 job ---
    resp = await client.run_job("version")
    p(f"version           : success={resp.success} answer={resp.answer!r}")

    resp = await client.run_job(
        "write",
        path="daily/2026-09-22/reme-arch.md",
        name="ReMe 架构笔记",
        description="第 15 讲：CLI → Client → Service → Application → Job → Step → Component",
        content=(
            "ReMe 用 Application 做装配：组件按 Kahn 拓扑序启动，"
            "再由 BaseJob 顺序执行 Step，Step 之间用 RuntimeContext 传值。"
        ),
    )
    p(f"write             : success={resp.success} answer={shorten(str(resp.answer))[:72]!r}")

    resp = await client.run_job("reindex")
    p(f"reindex           : success={resp.success} metadata={resp.metadata}")

    resp = await client.run_job("search", query="Application 装配", limit=3)
    hits_text = str(resp.answer)
    p(f"search            : success={resp.success}")
    for line in hits_text.splitlines()[:6]:
        p(f"  {line}")
    if not hits_text.strip():
        failures.append("write -> reindex -> search 链路没有命中任何 chunk")

    try:
        await client.run_job("no_such_job")
        failures.append("run_job('no_such_job') 居然没抛异常")
    except MemoryUnavailableError as exc:
        p(f"不存在的 job       : MemoryUnavailableError -> {str(exc)[:50]}...")

    # --- success=False 必须抛：用 MemoryJobError 的构造直接验证语义 ---
    err = MemoryJobError("search", "boom", {"why": "demo"})
    p(f"MemoryJobError    : {err} / metadata={err.metadata}")
    if "boom" not in str(err):
        failures.append("MemoryJobError 没有把 answer 带进异常消息")

    await client.aclose()
    p(f"aclose() 后 started: {client.started}")
    if client.started:
        failures.append("aclose() 之后 started 仍为 True")

    p(f"结论              : {'PASS' if not failures else 'FAIL'}")
    for item in failures:
        p(f"  ! {item}")
    return {"ok": not failures, "failures": failures, "hits": hits_text}


# ======================================================================
# F · Profile 纳管
# ======================================================================
def section_f() -> dict[str, Any]:
    """F 段：把 ReMe 纳管进 harness_kit 的 Profile 体系。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str]}``。
    """
    head("F · Profile 纳管：coding.yaml 的 memory: 段 -> MemorySpec -> ReMe 配置")
    failures: list[str] = []

    profile = load_profile("coding", search_dir=REF / "harness_kit" / "profiles")
    spec = profile.memory
    p(f"profile.name      : {profile.name}")
    p(f"profile.extends   : {profile.extends}")
    p(f"memory.enabled    : {spec.enabled}")
    p(f"memory.workspace_root: {spec.workspace_root}")
    p(f"memory.jobs       : {spec.jobs}")
    p(f"memory.top_k      : {spec.top_k}  min_score={spec.min_score}")

    builder = HarnessMemoryConfig.from_spec(spec, settings=_settings_for_demo())
    cfg = builder.build()
    p(f"解析后的 workspace : {_SHORTEN_PATH(cfg['workspace_dir'])}")
    p(f"job 白名单生效后   : {sorted(cfg['jobs'])}")
    p(f"service.backend   : {cfg['service']['backend']}")

    if not spec.enabled:
        failures.append("coding.yaml 的 memory.enabled 不是 true")
    if "auto_memory" not in cfg["jobs"]:
        failures.append("Profile 里列的 auto_memory 没有出现在最终配置里")
    if set(spec.jobs) - set(cfg["jobs"]):
        failures.append(f"Profile 白名单里有 job 被丢了: {sorted(set(spec.jobs) - set(cfg['jobs']))}")

    p(f"结论              : {'PASS' if not failures else 'FAIL'}")
    for item in failures:
        p(f"  ! {item}")
    return {"ok": not failures, "failures": failures}


def _settings_for_demo() -> Any:
    """构造一个把相对路径锚到「工作区父目录」的 Settings（F 段专用）。

    ``from_spec`` 会把 ``memory.workspace_root`` 这种相对路径按
    ``settings.resolve()`` 锚到 ``repo_root`` 上；教程里不想让它写进仓库，
    所以这里把 anchor 换成一个临时目录。

    Returns:
        `Any`: ``Settings`` 实例。
    """
    from harness_kit.settings import Settings

    anchor = Path(tempfile.mkdtemp(prefix="lesson15_f_")).resolve()
    _SHORTEN.append((str(anchor), "$ANCHOR"))
    return Settings(repo_root=anchor, workspace_dir=Path("./.harness/workspace"))


# ======================================================================
# G ·（--live）真实模型
# ======================================================================
async def section_g(cfg: dict[str, Any]) -> dict[str, Any]:
    """G 段：真实调一次 deepseek-flash，证明 as_llm 凭据注入生效。

    Args:
        cfg (`dict[str, Any]`): ReMe app config。

    Returns:
        `dict[str, Any]`: ``{"ok": bool, "failures": list[str]}``。
    """
    head("G · （--live）as_llm.default 的真实模型调用（1 次计费）")
    failures: list[str] = []

    from agentscope.message import TextBlock, UserMsg
    from agentscope.model import ChatResponse

    client = MemoryClient(cfg, start_timeout_s=120.0)
    await client.start()
    try:
        component = client.component("as_llm")
        model = component.model
        p(f"as_llm 组件类      : {type(component).__name__}")
        p(f"底层模型类         : {type(model).__name__}")
        p(f"模型名             : {model.model}")
        p(f"stream             : {model.stream}")

        result = await model([UserMsg(name="user", content=[TextBlock(text="用一句话解释 ReMe 是什么。")])])
        text = ""
        usage = None
        if isinstance(result, ChatResponse):
            text = "".join(b.text for b in result.content if isinstance(b, TextBlock))
            usage = result.usage
        else:
            # stream=True 时 ``ChatModelBase.__call__`` 返回的是 async generator
            # （third_party/agentscope/src/agentscope/model/_base.py:255-295）。
            # 累积语义的坑在这里：**``is_last=True`` 的那一条是完整文本**，
            # 前面的若干条是增量 delta。把两者都累加会得到双份文本 —— 所以
            # 「见 last 就整体替换」，而不是继续 +=。
            async for chunk in result:
                piece = "".join(b.text for b in chunk.content if isinstance(b, TextBlock))
                if getattr(chunk, "is_last", False):
                    text = piece
                else:
                    text += piece
                if chunk.usage is not None:
                    usage = chunk.usage
        p(f"回答               : {text.strip()[:120]}")
        p(
            "usage              : "
            f"input={getattr(usage, 'input_tokens', None)} output={getattr(usage, 'output_tokens', None)}",
        )
        if not text.strip():
            failures.append("模型返回了空文本")
    finally:
        await client.aclose()

    p(f"结论              : {'PASS' if not failures else 'FAIL'}")
    for item in failures:
        p(f"  ! {item}")
    return {"ok": not failures, "failures": failures}


# ======================================================================
# main
# ======================================================================
async def main() -> int:
    """按 A~G 顺序跑完七段，最后打印汇总。

    Returns:
        `int`: 进程退出码；全部通过为 0。
    """
    root = Path(tempfile.mkdtemp(prefix="lesson15_ws_")).resolve()
    # 归一化：脚本输出里不出现机器相关的绝对路径。
    _SHORTEN.extend(
        [
            (str(root), "$WS"),
            (str(REF), "$REF"),
            (str(REPO), "$REPO"),
        ],
    )
    # 教程输出必须"只有本脚本写的东西"：关掉 loguru 默认 sink，
    # 并按模块名屏蔽 reme / harness_kit 自己的日志。
    # 注意 ``logger.disable`` 是按 ``record["name"]``（即发生日志的模块名）过滤的，
    # 所以 ReMe 里 ``get_logger(force_init=True)`` 重新装 sink 也绕不过它。
    logger.remove()
    logger.disable("reme")
    logger.disable("harness_kit")

    ws = ReMeWorkspace(root=root / "reme")
    builder = (
        HarnessMemoryConfig(workspace=ws)
        .with_jobs("version", "health_check", "write", "read", "search", "reindex")
        .with_overrides(timezone="Asia/Shanghai")
    )

    results: dict[str, dict[str, Any]] = {}
    results["A"] = section_a()
    results["B"] = section_b(ws)
    c_result = section_c(builder)
    results["C"] = c_result
    results["D"] = section_d(c_result["config"])
    results["E"] = await section_e(c_result["config"])
    results["F"] = section_f()
    if LIVE:
        results["G"] = await section_g(c_result["config"])

    head("汇总")
    for key in sorted(results):
        state = "PASS" if results[key]["ok"] else "FAIL"
        p(f"  {key}  {state}")
        for item in results[key]["failures"]:
            p(f"       ! {item}")
    failed = [k for k, v in results.items() if not v["ok"]]
    p("")
    p(f"总计 {len(results)} 段，通过 {len(results) - len(failed)} 段，失败 {len(failed)} 段")

    # 收尾：销毁临时工作区（演示 destroy(confirm=True) 的正常用法）。
    ws.destroy(confirm=True)
    shutil.rmtree(root, ignore_errors=True)
    p(f"临时工作区已销毁: {_SHORTEN_PATH(root)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
