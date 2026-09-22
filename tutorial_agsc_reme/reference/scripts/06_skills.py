# -*- coding: utf-8 -*-
"""第 6 讲验证脚本：Skills 技能包（`harness_kit/skills/`）。

它把本讲的主结论全部变成可执行的断言：

  A. 两个内置技能的真实 manifest：字段、摘要、配套脚本、缺失脚本检测
  B. **严格校验 vs 静默跳过**：`LocalSkillLoader` 打一条 warning 就当没看见，
     `HarnessSkillLoader` 直接抛 `SkillManifestError`
  C. `scan_subdir` 坑的真实复现：原生默认 `False`，两级目录结构一个技能都扫不到
  D. 两道过滤：`enabled` 开关（per-skill 与 loader 名单）与租户白名单
  E. 第一层渐进披露：`index_text()` 的文本形态，以及 `select()` 的标签 / 关键词过滤
  F. 依赖拓扑：`requires` 的拓扑排序、缺失依赖、成环检测
  G. 接入 AgentScope 的 `Toolkit`：技能索引进系统提示词、`Skill` 工具何时可见、
     `SkillViewer` 真的能把 SKILL.md 正文读出来、`disclosure="full"` 的直接注入
  H. 从 Profile 装配：`HarnessBuilder.build_all()` 出来的 Agent 的
     **真实系统提示词**长什么样（0 次 LLM 调用）
  I. 配套脚本冒烟：两个纯标准库 CLI 的真实退出码
  J.（需要 key，`--live` 打开）真实 deepseek-flash：模型自己决定调用 `Skill` 工具
     读取 `code_review`，然后再按技能正文作答（**2~3 次调用**）

用法（`PYTHONPATH` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/06_skills.py

    加 `--live` 才会跑 J 段（真实 LLM，**2~3 次调用**）。

LLM 调用预算：A~I 段 **0 次**；J 段 **2~3 次**。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import harness_kit

# 从 import 到的包反推路径，这样脚本放在任何地方都能跑：
#   <repo>/tutorial_agsc_reme/reference/harness_kit/__init__.py
#     .parent        -> .../reference/harness_kit
#     .parent.parent -> .../reference
REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(REPO / ".env", override=False)

from agentscope.event import ModelCallStartEvent, ToolCallStartEvent  # noqa: E402
from agentscope.skill import LocalSkillLoader  # noqa: E402
from agentscope.state import AgentState  # noqa: E402
from agentscope.tool import ToolGroup, Toolkit  # noqa: E402

from harness_kit.config import load_resolved_profile, resolve_profile  # noqa: E402
from harness_kit.config.builder import HarnessBuilder  # noqa: E402
from harness_kit.config.schema import (  # noqa: E402
    ModelSpec,
    Profile,
    SkillsSpec,
    ToolsSpec,
)
from harness_kit.settings import Settings  # noqa: E402
from harness_kit.skills import (  # noqa: E402
    HarnessSkillLoader,
    SkillDependencyError,
    SkillManifestError,
    UnknownSkillError,
    build_skill_instruction_template,
    load_manifest,
    parse_manifest_text,
    parse_tags,
)

# 日志：默认只留 WARNING，避免 C 段故意触发的 warning 刷屏。
# 想看全部细节：HARNESS06_LOG_LEVEL=DEBUG
logger.remove()
logger.add(sys.stderr, level=os.getenv("HARNESS06_LOG_LEVEL", "ERROR"))

PROFILES: Path = REF / "harness_kit" / "profiles"
BUILTIN: Path = REF / "harness_kit" / "skills" / "builtin"

TMP: Path = Path(tempfile.mkdtemp(prefix="harness06_"))


def banner(text: str) -> None:
    """打印一个分节标题。

    Args:
        text (`str`): 标题文本。
    """
    print(f"\n===== {text} =====")


def write_skill(root: Path, name: str, body: str) -> Path:
    """在 ``root/name/SKILL.md`` 写一个技能，返回技能目录。

    Args:
        root (`Path`): 技能根目录（会被创建）。
        name (`str`): 技能目录名。
        body (`str`): ``SKILL.md`` 的完整文本（含 front matter）。

    Returns:
        `Path`: 技能目录。
    """
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


# ----------------------------------------------------------------------
# A. 内置技能清单
# ----------------------------------------------------------------------
def section_a() -> None:
    """读两个内置技能的 manifest，逐字段打印。"""
    banner("A. 内置技能的 manifest")
    loader = HarnessSkillLoader(BUILTIN)
    manifests = loader.load_manifests()
    print("技能目录 =", BUILTIN)
    print("技能数   =", len(manifests))
    for manifest in manifests:
        print(f"\n  {manifest.name}  (v{manifest.version})")
        print(f"    description = {manifest.description[:40]}...")
        print(f"    tags        = {manifest.tags}")
        print(f"    requires    = {manifest.requires}")
        print(f"    tenants     = {manifest.tenant_scope}")
        print(f"    enabled     = {manifest.enabled}")
        print(f"    scripts     = {[str(p.relative_to(BUILTIN)) for p in manifest.script_paths]}")
        print(f"    summary     = {manifest.summary[:40]}...")
        print(f"    body 行数   = {len(manifest.body.splitlines())}")

    assert [m.name for m in manifests] == ["code_review", "commit_convention"]
    assert loader.get("code_review").requires == ["commit_convention"]
    assert loader.get("code_review").missing_scripts() == []
    assert loader.get("code_review").version == "1.0.0"
    # body 是**第二层披露**的内容，绝不会出现在 index_text() 里（E 段会断言）
    assert "# 代码审查技能" in loader.get("code_review").body
    assert "code_review" in loader.get("code_review").render_full()
    try:
        loader.get("no_such_skill")
    except UnknownSkillError as exc:
        print(f"\nUnknownSkillError 已抛出: {str(exc)[:60]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("get() 对不存在的技能必须抛 UnknownSkillError")
    print("OK  manifest 6 个契约字段 + 5 个 harness_kit 扩展字段全部落地")


# ----------------------------------------------------------------------
# B. 严格校验 vs 静默跳过
# ----------------------------------------------------------------------
async def section_b() -> None:
    """对比官方 loader 的"静默跳过"与 harness_kit 的"直接报错"。"""
    banner("B. 严格校验 vs 静默跳过")
    root = TMP / "b"
    root.mkdir(parents=True, exist_ok=True)
    # ① 缺 description：官方只记 warning 然后丢掉整个技能
    missing_field = write_skill(
        root,
        "no_description",
        "---\nname: no_description\n---\n正文在这里。\n",
    )
    native = LocalSkillLoader(directory=str(root), scan_subdir=True)
    print("LocalSkillLoader.list_skills() ->", await native.list_skills())
    harness = HarnessSkillLoader(root)
    try:
        harness.load_manifests()
    except SkillManifestError as exc:
        print(f"HarnessSkillLoader 直接报错: {str(exc)[:76]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("缺 description 必须抛 SkillManifestError")

    # ② 完全没有 front matter
    write_skill(root, "no_frontmatter", "# 我是一段普通的 markdown\n")
    try:
        parse_manifest_text(
            "# 我是一段普通的 markdown\n",
            path=Path("/tmp/no_frontmatter/SKILL.md"),
        )
    except SkillManifestError as exc:
        print(f"缺 front matter: {str(exc)[:60]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("缺 front matter 必须抛 SkillManifestError")

    # ③ 技能名非法（会出现在 XML 标签里，所以必须是 [a-z0-9_-]）
    for bad_name in ["Code Review", "code_review!", "_leading_underscore"]:
        try:
            parse_manifest_text(
                f"---\nname: {bad_name}\ndescription: x\n---\n正文\n",
                path=Path("/tmp/x/SKILL.md"),
            )
        except SkillManifestError as exc:
            print(f"非法技能名 {bad_name!r} -> {str(exc)[:48]}...")
        else:  # pragma: no cover - 防御性
            raise AssertionError(f"技能名 {bad_name!r} 应当被拒绝")

    # ④ 版本号非法
    try:
        parse_manifest_text(
            "---\nname: good_name\ndescription: x\nversion: v1\n---\n正文\n",
            path=Path("/tmp/x/SKILL.md"),
        )
    except SkillManifestError as exc:
        print(f"非法版本号 -> {str(exc)[:56]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("version: v1 应当被拒绝")

    # ⑤ tags 的四种写法都容忍（YAML 里写标量是常见手误）
    print("\ntags 归一：")
    for raw in ([["a", "b"], "a, b", "a b", None]):
        print(f"  {raw!r:12s} -> {parse_tags(raw)}")
    assert parse_tags("review, diff") == ["review", "diff"]
    assert parse_tags(None) == []
    try:
        parse_tags(["ok", 3])
    except SkillManifestError:
        print("  非字符串元素 -> SkillManifestError（预期）")
    else:  # pragma: no cover - 防御性
        raise AssertionError("非字符串 tag 应当被拒绝")

    # ⑥ 声明的脚本不存在 -> FileNotFoundError（require_scripts 或 strict）
    ghost = write_skill(
        root / "ghost",
        "ghost_script",
        "---\nname: ghost_script\ndescription: 声明了不存在的脚本\n"
        "scripts:\n  - scripts/nope.py\n---\n正文\n",
    )
    ghost_loader = HarnessSkillLoader(ghost)
    try:
        ghost_loader.load_manifests()
    except FileNotFoundError as exc:
        print(f"\n缺失脚本 -> {str(exc)[:70]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("声明了不存在的脚本必须报错")
    # 注意：strict=True 时连 get() 都会因为缺失脚本而报错（get 内部会 _index()），
    # 所以要在"宽容模式"下才能观察 missing_scripts()
    lenient_ghost = HarnessSkillLoader(ghost, strict=False)
    print("strict=False 时 missing_scripts() =",
          [str(p) for p in lenient_ghost.get("ghost_script").missing_scripts()])
    assert lenient_ghost.get("ghost_script").missing_scripts() != []
    # strict=False 时降级为 warning + 跳过，用于容忍"还没写完的技能目录"
    lenient = HarnessSkillLoader(root, strict=False)
    print("strict=False 时可见技能 =", [m.name for m in lenient.load_manifests(
        include_disabled=True,
    )])
    print("OK  不合法就报错、不静默 —— 这正是「模型突然不会用技能」的根因")


# ----------------------------------------------------------------------
# C. scan_subdir 坑
# ----------------------------------------------------------------------
async def section_c() -> None:
    """复现 LocalSkillLoader 默认不递归导致的"技能消失"。"""
    banner("C. scan_subdir 坑的真实复现")
    root = TMP / "c"
    write_skill(
        root,
        "nested_skill",
        "---\nname: nested_skill\ndescription: 在两级目录里的技能\n---\n正文\n",
    )
    parent = str(root)

    default_loader = LocalSkillLoader(directory=parent)  # scan_subdir 默认 False
    print("LocalSkillLoader(默认 scan_subdir=False)   ->",
          await default_loader.list_skills())
    explicit = LocalSkillLoader(directory=parent, scan_subdir=True)
    print("LocalSkillLoader(scan_subdir=True)         ->",
          [s.name for s in await explicit.list_skills()])
    harness_default = HarnessSkillLoader(root)  # 默认 True
    print("HarnessSkillLoader(默认 scan_subdir=True)  ->",
          [m.name for m in harness_default.load_manifests()])

    assert await default_loader.list_skills() == []
    assert [s.name for s in await explicit.list_skills()] == ["nested_skill"]
    assert [m.name for m in harness_default.load_manifests()] == ["nested_skill"]

    # 原生 loader 的返回顺序来自 os.walk，harness_kit 的 manifest 列表显式排序
    print("原生 list_skills 顺序 =", [s.name for s in await explicit.list_skills()])
    print("harness load_manifests 顺序 =",
          [m.name for m in harness_default.load_manifests()])
    print("OK  子目录里的 SKILL.md 默认扫不到；harness_kit 把它变成默认行为")


# ----------------------------------------------------------------------
# D. 启用开关与租户
# ----------------------------------------------------------------------
async def section_d() -> None:
    """验证 enabled（两级）与 tenants 过滤真的影响 list_skills()。"""
    banner("D. 启用开关与租户白名单")
    root = TMP / "d"
    write_skill(
        root,
        "alpha",
        "---\nname: alpha\ndescription: 默认启用的技能\ntags: [shared]\n---\nA\n",
    )
    write_skill(
        root,
        "beta",
        "---\nname: beta\ndescription: 被 enabled=false 关掉的技能\n"
        "tags: [shared]\nenabled: false\n---\nB\n",
    )
    write_skill(
        root,
        "acme_only",
        "---\nname: acme_only\ndescription: 只对 acme 租户开放\ntags: [shared]\n"
        "tenants: [acme]\n---\nC\n",
    )

    plain = HarnessSkillLoader(root)
    print("默认可视技能（租户 None）        =",
          [m.name for m in plain.load_manifests()])
    print("include_disabled=True（诊断用）  =",
          [m.name for m in plain.load_manifests(include_disabled=True)])
    # 注意：include_disabled 只关掉「enabled 过滤」，租户过滤仍然生效 ——
    # 想看全量必须**同时**把租户一起放开（诊断脚本容易在这里看漏技能）。
    print("include_disabled + tenant=acme   =",
          [m.name for m in plain.load_manifests(
              tenant="acme",
              include_disabled=True,
          )])
    acme = HarnessSkillLoader(root, tenant="acme")
    print("租户 acme 可视技能               =",
          [m.name for m in acme.load_manifests()])
    other = HarnessSkillLoader(root, tenant="globex")
    print("租户 globex 可视技能             =",
          [m.name for m in other.load_manifests()])

    # 显式 enabled 名单：只有名单里的技能可用；空名单 = 不加限制
    listed = HarnessSkillLoader(root, enabled=["alpha"])
    print("enabled=['alpha'] 可视技能       =",
          [m.name for m in listed.load_manifests()])
    # 名单里写了不存在的技能名：默认只 warning（增量式配置的现实）
    typo = HarnessSkillLoader(root, enabled=["alpha", "alhpa"])
    print("enabled 里有拼错的名字           =",
          [m.name for m in typo.load_manifests()])
    strict_typo = HarnessSkillLoader(root, enabled=["alhpa"], strict_enabled=True)
    try:
        strict_typo.load_manifests()
    except UnknownSkillError as exc:
        print(f"strict_enabled=True -> {str(exc)[:64]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("strict_enabled=True 时未知名字必须报错")

    assert [m.name for m in plain.load_manifests()] == ["alpha"]
    assert [m.name for m in plain.load_manifests(include_disabled=True)] == [
        "alpha",
        "beta",
    ]
    assert [
        m.name
        for m in plain.load_manifests(tenant="acme", include_disabled=True)
    ] == ["acme_only", "alpha", "beta"]
    assert [m.name for m in acme.load_manifests()] == ["acme_only", "alpha"]
    assert [m.name for m in other.load_manifests()] == ["alpha"]

    # 过滤必须发生在 list_skills() 里 —— Toolkit 只认这个入口
    native_names = sorted(s.name for s in await plain.list_skills())
    print("原生接口 list_skills() 的顺序    =", native_names)
    assert native_names == ["alpha"], "过滤没生效的话 Toolkit 会把 beta 也塞进提示词"
    print("OK  enabled / tenants 真的会影响 list_skills()，而不是只写在日志里")


# ----------------------------------------------------------------------
# E. 第一层披露与 select
# ----------------------------------------------------------------------
def section_e() -> None:
    """打印技能索引文本，并验证 select() 的过滤逻辑。"""
    banner("E. 第一层渐进披露：技能索引")
    loader = HarnessSkillLoader(BUILTIN)
    index_text = loader.index_text()
    print(index_text)

    print("索引字符数 =", len(index_text))
    bodies = sum(len(m.body) for m in loader.load_manifests())
    print("两份 SKILL.md 正文合计 =", bodies, "字符")
    print(f"索引 / 正文 = {len(index_text) / bodies:.1%} —— 这就是渐进披露省下的上下文")
    assert "<skill-index>" in index_text and "</skill-index>" in index_text
    assert "# 代码审查技能" not in index_text, "正文绝不能出现在第一层披露里"

    print("select(tags=['review'])   ->",
          [m.name for m in loader.select(tags=["review"])])
    print("select(query='commit')    ->",
          [m.name for m in loader.select(query="commit")])
    print("select(query='REVIEW')    ->",
          [m.name for m in loader.select(query="REVIEW")])
    print("select(tags=['nope'])     ->",
          [m.name for m in loader.select(tags=["nope"])])
    assert [m.name for m in loader.select(tags=["review"])] == ["code_review"]
    assert [m.name for m in loader.select(query="REVIEW")] == ["code_review"]
    assert loader.select(tags=["review", "git"]) == []

    # 空目录：index_text() 返回空串（Toolkit 会据此判定"没有技能"）
    empty = HarnessSkillLoader(TMP / "e_empty", strict=False)
    print("空目录 index_text() ->", repr(empty.index_text()))
    assert empty.index_text() == ""
    print("OK  索引只给 name/description/dir；正文与脚本要模型主动读")


# ----------------------------------------------------------------------
# F. 依赖拓扑
# ----------------------------------------------------------------------
def section_f() -> None:
    """验证 requires 的拓扑排序、缺失依赖与成环。"""
    banner("F. 技能依赖的拓扑排序")
    loader = HarnessSkillLoader(BUILTIN)
    print("内置技能的 requires:",
          {m.name: m.requires for m in loader.load_manifests()})
    print("拓扑顺序 =", loader.resolve_order())
    assert loader.resolve_order() == ["commit_convention", "code_review"], (
        "被依赖者必须排在前面：code_review requires commit_convention"
    )

    missing_root = TMP / "f_missing"
    write_skill(
        missing_root,
        "solo",
        "---\nname: solo\ndescription: 依赖了一个不存在的技能\n"
        "requires: [ghost]\n---\nS\n",
    )
    try:
        HarnessSkillLoader(missing_root).resolve_order()
    except SkillDependencyError as exc:
        print(f"\n缺失依赖 -> {str(exc)[:70]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("依赖缺失必须报 SkillDependencyError")

    # 一个真实的"意外缺失"场景：依赖的技能被 enabled 名单挡在外面
    print("内置目录 + enabled=['code_review'] 时（依赖被过滤掉）->")
    try:
        HarnessSkillLoader(BUILTIN, enabled=["code_review"]).resolve_order()
    except SkillDependencyError as exc:
        print(f"  SkillDependencyError: {str(exc)[:72]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("依赖不可见时必须报错，而不是静默降级")

    cycle_root = TMP / "f_cycle"
    write_skill(
        cycle_root,
        "ping",
        "---\nname: ping\ndescription: 环的一端\nrequires: [pong]\n---\nP\n",
    )
    write_skill(
        cycle_root,
        "pong",
        "---\nname: pong\ndescription: 环的另一端\nrequires: [ping]\n---\nQ\n",
    )
    try:
        HarnessSkillLoader(cycle_root).resolve_order()
    except SkillDependencyError as exc:
        print(f"\n成环 -> {str(exc)[:60]}...")
    else:  # pragma: no cover - 防御性
        raise AssertionError("依赖成环必须报 SkillDependencyError")
    print("OK  顺序由 requires 决定，缺失与成环都在加载期就报错")


# ----------------------------------------------------------------------
# G. 接入 Toolkit
# ----------------------------------------------------------------------
async def section_g() -> None:
    """把 loader 交给 Toolkit，验证索引注入、Skill 工具可见性与正文读取。"""
    banner("G. 接入 AgentScope 的 Toolkit")
    loader = HarnessSkillLoader(BUILTIN, enabled=["code_review", "commit_convention"])

    # ① 索引注入的是"我们自己的模板"（契约 §3.6 的 disclosure="index"）
    toolkit = Toolkit(
        tools=[],
        skills_or_loaders=[loader],
        skill_instruction_template=build_skill_instruction_template("index"),
    )
    instructions = await toolkit.get_skill_instructions()
    print("Toolkit.get_skill_instructions() 前 3 行:")
    for line in (instructions or "").splitlines()[:3]:
        print("   ", line)
    assert instructions is not None and "<agent-skills>" in instructions
    assert "code_review" in instructions

    schemas = await toolkit.get_tool_schemas()
    names = sorted(s["function"]["name"] for s in schemas)
    print("basic 组可见工具 =", names)
    assert "Skill" in names, "有技能时 SkillViewer 应当自动可见"

    # ② SkillViewer 真的能把正文读出来（它按需调用 get_skills_method）
    viewer = toolkit.builtin_skill_viewer.tool
    state = AgentState()
    chunk = await viewer.call(skill="code_review", _agent_state=state)
    text = "".join(
        getattr(block, "text", "") for block in chunk.content
    )
    print("SkillViewer.call('code_review') 返回状态 =", chunk.state)
    print("正文前 3 行:", " / ".join(
        [line for line in text.splitlines() if line.strip()][:3],
    ))
    # 注意：SkillViewer 返回的是**去掉 front matter 的正文**，
    # harness_kit 的 render_full() 额外加的 "# skill: <name>" 头不在里面
    assert "# 代码审查技能" in text
    assert "scripts/review_check.py" in text
    missing = await viewer.call(skill="no_such", _agent_state=state)
    print("SkillViewer.call('no_such') 返回状态 =", missing.state)
    assert missing.state == "error", "读不到技能必须返回 ERROR 态而不是抛异常"
    print("  错误文本 =", missing.content[0].text)

    # ③ 技能放进独立工具组：没激活时连 Skill 工具都看不见
    grouped = Toolkit(
        tools=[],
        tool_groups=[
            ToolGroup(
                name="skillgroup",
                description="代码审查与提交规范技能。",
                skills_or_loaders=[loader],
            ),
        ],
    )
    print("\n未激活 skillgroup 时的工具 =",
          await grouped.get_tool_schemas(groups=[]))
    activated = await grouped.get_tool_schemas(groups=["skillgroup"])
    print("激活 skillgroup 后的工具   =",
          sorted(s["function"]["name"] for s in activated))
    print("激活后 Skill 指令是否注入  =",
          (await grouped.get_skill_instructions(["skillgroup"])) is not None)
    print("激活后 basic 组的技能指令  =",
          await grouped.get_skill_instructions([]))
    assert await grouped.get_skill_instructions([]) is None

    # ④ disclosure="full"：正文直接铺进提示词，省一次工具调用但更贵
    full = Toolkit(
        tools=[],
        skills_or_loaders=[loader],
        skill_instruction_template=build_skill_instruction_template("full"),
    )
    full_text = await full.get_skill_instructions()
    print("\nfull 模板字符数 =", len(full_text or ""),
          "| index 模板字符数 =", len(instructions or ""))
    assert "<content>" in (full_text or "")
    print("OK  技能进了 Toolkit：索引进提示词、Skill 工具按需可见、正文按需读取")


# ----------------------------------------------------------------------
# H. 从 Profile 装配
# ----------------------------------------------------------------------
async def section_h() -> None:
    """用 Profile 装配一个真实 Agent，打印它真实的系统提示词。"""
    banner("H. Profile 装配：真实 Agent 的系统提示词")
    settings = Settings.from_env(repo_root=REF, profile_dir=PROFILES)
    base = load_resolved_profile("default", search_dir=PROFILES)
    print("default profile 的 skills =", base.skills.model_dump())

    prompts: dict[str, str] = {}
    for disclosure in ("index", "full"):
        profile = Profile(
            name=f"lesson6_{disclosure}",
            description=f"第 6 讲演示：disclosure={disclosure}",
            extends="default",
            # 本段只关心"系统提示词里放了什么"，与模型无关。用回声模型
            # （第 4 讲交付，registry 里 ``echo`` 的工厂，registry.py:770）
            # 让 A~I 段**完全不依赖 LLM_API_KEY** —— 否则把 reference
            # 拷到 /tmp 里跑时（那里没有 .env）会在这一行炸掉。
            model=ModelSpec(provider="echo", model_name="echo"),
            skills=SkillsSpec(
                directories=["./harness_kit/skills/builtin"],
                enabled=["code_review", "commit_convention"],
                scan_subdir=True,
                disclosure=disclosure,  # type: ignore[arg-type]
            ),
        )
        resolved = resolve_profile(profile, search_dir=PROFILES)
        builder = HarnessBuilder(resolved, settings=settings)
        try:
            harness = await builder.build_all()
            prompt = await harness.agent._get_system_prompt()
            prompts[disclosure] = prompt
            skills_in_toolkit = [
                type(_).__name__
                for group in harness.toolkit.tool_groups
                for _ in (group.skills_or_loaders if group.name == "basic" else [])
            ]
            print(f"\n--- disclosure={disclosure} ---")
            print("basic 组的技能装配物 =", skills_in_toolkit)
            print("系统提示词字符数     =", len(prompt))
            for line in prompt.splitlines():
                if line.startswith("<agent-skills>") or line.startswith("<skill>"):
                    print("   ", line)
                    break
        finally:
            await builder.aclose()

    print("\nindex 字符数 =", len(prompts["index"]),
          "| full 字符数 =", len(prompts["full"]),
          f"| 差值 = {len(prompts['full']) - len(prompts['index'])}")
    assert "<agent-skills>" in prompts["index"]
    assert "# 代码审查技能" not in prompts["index"], "index 披露不该含正文"
    assert "# 代码审查技能" in prompts["full"], "full 披露必须把正文铺进去"
    print("OK  装配层已经把 SkillsSpec.disclosure 翻译成 Toolkit 的模板参数")


# ----------------------------------------------------------------------
# I. 配套脚本冒烟
# ----------------------------------------------------------------------
def section_i() -> None:
    """跑两个技能的配套脚本，记录真实退出码。"""
    banner("I. 配套脚本冒烟（纯标准库 CLI）")
    review = BUILTIN / "code_review" / "scripts" / "review_check.py"
    commit = BUILTIN / "commit_convention" / "scripts" / "check_commit_msg.py"

    bad_py = TMP / "bad_sample.py"
    bad_py.write_text(
        "def handler(data=[]):\n"
        "    try:\n"
        "        return eval(data)\n"
        "    except:\n"
        "        pass\n",
        encoding="utf-8",
    )

    def run(args: list[str]) -> tuple[int, str]:
        """跑一个子进程并返回 (退出码, stdout)。"""
        completed = subprocess.run(
            [sys.executable, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout

    code, out = run([str(review), str(bad_py), "--json"])
    payload = json.loads(out)
    rules = sorted({f["rule"] for f in payload["findings"]})
    print(f"review_check.py 退出码 = {code} | findings = {payload['count']}")
    print("  命中规则 =", rules)
    assert code == 1 and payload["count"] >= 3
    assert "bare-except" in rules and "dangerous-builtin" in rules
    assert "mutable-default-arg" in rules

    code, out = run([str(review), str(bad_py), "--fail-on", "none"])
    print(f"review_check.py --fail-on none 退出码 = {code}")
    assert code == 0

    good_msg = "feat(mcp): add stdio transport"
    code, out = run([str(commit), "--string", good_msg])
    print(f"check_commit_msg.py 合规样本退出码 = {code} | {out.strip()}")
    assert code == 0

    code, out = run([str(commit), "--string", "Fixed the Bug."])
    print(f"check_commit_msg.py 不合规样本退出码 = {code}")
    for line in out.strip().splitlines():
        print("   ", line)
    assert code == 1

    code, out = run([str(commit), "--string", "feat(api)!: drop v1"])
    print(f"check_commit_msg.py 缺破坏性说明退出码 = {code}")
    assert code == 1 and "no-breaking-note" in out
    print("OK  技能的可执行部分能被模型直接调用，退出码语义清晰（0/1/2）")


# ----------------------------------------------------------------------
# J. 真实模型：模型自己决定读技能
# ----------------------------------------------------------------------
async def section_j() -> None:
    """真实 deepseek-flash：模型看到技能索引后主动调 Skill 工具。

    Returns:
        `int`: 本次真实 LLM 调用次数。
    """
    banner("J. 真实模型：让模型自己决定读技能（需要 --live）")
    settings = Settings.from_env(repo_root=REF, profile_dir=PROFILES)
    profile = Profile(
        name="lesson6_live",
        description="第 6 讲演示：真实模型 + code_review 技能",
        extends="default",
        # 把工具清空：本次只演示"模型读技能"这件事。留着 Bash / Write 会触发
        # 权限确认（默认 PermissionMode.DEFAULT 对写操作是 ASK），
        # reply() 会变成"等待用户确认"而不是给出审查结论。
        tools=ToolsSpec(packs=[]),
        skills=SkillsSpec(
            directories=["./harness_kit/skills/builtin"],
            enabled=["code_review", "commit_convention"],
            scan_subdir=True,
            disclosure="index",
        ),
    )
    resolved = resolve_profile(profile, search_dir=PROFILES)
    builder = HarnessBuilder(resolved, settings=settings)
    try:
        harness = await builder.build_all()
        from agentscope.message import Msg, UserMsg

        # 数真实 LLM 调用**不能**数 ``state.context`` 里的 assistant 消息：
        # ``AgentState.append_context``
        # （third_party/agentscope/src/agentscope/state/_state.py:298）会把
        # 同一次 reply 里所有 assistant 内容合并进**同一条** Msg，所以一次
        # ReAct（先调 Skill、再作答）在 context 里只留 1 条 assistant 消息，
        # 而 ``AssistantMsg`` 本身还是工厂函数不是类
        # （third_party/agentscope/src/agentscope/message/_base.py:592），
        # ``isinstance(msg, AssistantMsg)`` 会直接抛 TypeError。
        # 正确口径是**数事件**：每次模型请求都会发一个
        # ``ModelCallStartEvent``（event/_event.py:128）。
        calls = 0
        tool_names: list[str] = []
        answer: Msg | None = None
        async for chunk in harness.agent.reply_stream(
            UserMsg(
                "user",
                "帮我审一下这段代码：\n\n"
                "```python\ndef calc(items=[]):\n"
                "    for i in items:\n"
                "        total += i\n"
                "    return total\n```",
            ),
            yield_final_msg=True,
        ):
            if isinstance(chunk, ModelCallStartEvent):
                calls += 1
            elif isinstance(chunk, ToolCallStartEvent):
                tool_names.append(chunk.tool_call_name)
            elif isinstance(chunk, Msg):
                answer = chunk
        assert answer is not None, "reply_stream(yield_final_msg=True) 应当给出最终 Msg"
        print("Agent 最终返回类型 =", type(answer).__name__)

        # 模型自己决定调用了哪些工具，按 **调用顺序** 记在事件里
        print("本次 reply 里模型调用的工具 =", tool_names)
        print("LLM 调用次数（按 ModelCallStartEvent 计）= ", calls)

        assert "Skill" in tool_names, (
            "模型应当先调用 Skill 工具读取 code_review 技能正文"
        )
        text = answer.get_text_content()
        print("回答前 240 字:\n", text[:240])
    finally:
        await builder.aclose()
    print(f"J 段 LLM 调用 = {calls} 次（预算 3 次）")
    return calls


# ----------------------------------------------------------------------
async def main() -> int:
    """跑完整套验证。

    Returns:
        `int`: 进程退出码；失败时非 0。
    """
    live = "--live" in sys.argv
    section_a()
    await section_b()
    await section_c()
    await section_d()
    section_e()
    section_f()
    await section_g()
    await section_h()
    section_i()
    if live:
        try:
            calls = await section_j()
        except Exception as exc:  # noqa: BLE001 - 验证脚本要给出可读的失败信息
            # 常见原因：把 reference 拷到 /tmp 后跑 --live，那里没有 .env，
            # 模型工厂会抛 "LLM API key 缺失"。给出类型名而不是一串栈。
            print(f"\nFAIL  --live 已指定但 J 段没跑起来：{type(exc).__name__}: {exc}")
            return 2
        if calls == 0:
            print("\nFAIL  --live 已指定但 J 段一次调用都没发生")
            return 2
        if calls > 4:
            print(f"\nFAIL  J 段调用了 {calls} 次，超出预算（<=4）")
            return 2
    else:
        banner("J. 跳过真实模型（要跑请加 --live）")
    print(f"\n临时目录: {TMP}")
    print("\nPASS  第 6 讲验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
