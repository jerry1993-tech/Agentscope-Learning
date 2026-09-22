# -*- coding: utf-8 -*-
"""第 6 讲的 pytest（交付物之一）：把技能层的四条契约钉成可回归的断言。

四条契约：

1. **不合法就报错，不静默跳过** —— ``LocalSkillLoader`` 遇到缺
   ``description`` 的 ``SKILL.md`` 只打一条 warning 然后返回 ``None``
   （``third_party/agentscope/src/agentscope/skill/_local_loader.py:71-77``），
   结果是 Agent 少了一个技能而没有任何人知道。``HarnessSkillLoader`` 必须抛。
2. **过滤必须落在 ``list_skills()`` 上** —— ``Toolkit._get_available_skills``
   （``third_party/agentscope/src/agentscope/tool/_toolkit.py:419``）只认
   ``ToolGroup.list_skills()`` → ``SkillLoaderBase.list_skills()`` 这条链，
   ``enabled`` / ``tenants`` 只有在这里生效才算生效。
3. **两层渐进披露** —— 索引（name + description + dir）常驻提示词，正文由
   ``Skill`` 工具按需读取；``disclosure="full"`` 是显式选择的另一种权衡。
4. **``requires`` 是可执行的依赖** —— 加载期就报缺失/成环，而不是运行时才发现。

用法（``tests/conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/``
塞进 ``sys.path``，所以不设 ``PYTHONPATH`` 也能跑；这里显式写出来是为了与
另外几个脚本一致）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest tests/test_lesson06_skills.py -v

LLM 调用预算：**0 次**（全部离线，纯磁盘 + 进程内断言）。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import harness_kit
import pytest
from agentscope.skill import LocalSkillLoader
from agentscope.state import AgentState
from agentscope.tool import ToolGroup, Toolkit
from harness_kit.config.schema import SkillsSpec
from harness_kit.settings import Settings
from harness_kit.skills import (
    HarnessSkillLoader,
    SkillDependencyError,
    SkillManifestError,
    UnknownSkillError,
    build_skill_instruction_template,
    load_manifest,
    loaders_from_spec,
    parse_manifest_text,
    parse_tags,
)

REF = Path(harness_kit.__file__).resolve().parent.parent
BUILTIN = REF / "harness_kit" / "skills" / "builtin"


# ----------------------------------------------------------------------
# 造技能目录的小工具
# ----------------------------------------------------------------------
def write_skill(
    root: Path,
    dirname: str,
    *,
    name: str | None = None,
    description: str = "一句话说明何时该用这个技能",
    body: str = "## 步骤\n1. 做点什么\n",
    extra: str = "",
    scripts: list[str] | None = None,
) -> Path:
    """在 ``root/dirname`` 下写一个 ``SKILL.md``。

    ``name`` 默认为 ``dirname``；``extra`` 是原样拼进 front matter 的
    额外字段（用于 ``version`` / ``tenants`` 等）。

    Args:
        root (`Path`): 技能根目录（不存在会被创建）。
        dirname (`str`): 技能子目录名。
        name (`str | None`): front matter 里的 ``name``。
        description (`str`): front matter 里的 ``description``。
        body (`str`): front matter 之后的正文。
        extra (`str`): 额外 front matter 行（含换行）。
        scripts (`list[str] | None`): 写进 ``scripts:`` 字段的相对路径，
            同时真的在磁盘上落一个空脚本文件。

    Returns:
        `Path`: 技能目录。
    """
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    script_field = ""
    if scripts:
        for script in scripts:
            path = skill_dir / script
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        script_field = f"scripts: [{', '.join(scripts)}]\n"

    front = (
        f"name: {name or dirname}\ndescription: {description}\n"
        f"{script_field}{extra}"
    )
    (skill_dir / "SKILL.md").write_text(
        f"---\n{front}---\n{body}",
        encoding="utf-8",
    )
    return skill_dir


# ----------------------------------------------------------------------
# 契约 1：manifest 的字段与校验
# ----------------------------------------------------------------------
def test_parse_tags_accepts_four_shapes() -> None:
    assert parse_tags(["a", "b"]) == ["a", "b"]
    assert parse_tags("a, b") == ["a", "b"]
    assert parse_tags("a b") == ["a", "b"]
    assert parse_tags(None) == []


def test_parse_tags_dedupes_keeping_order() -> None:
    assert parse_tags("b, a, b") == ["b", "a"]


def test_parse_tags_rejects_non_string_element() -> None:
    with pytest.raises(SkillManifestError):
        parse_tags(["a", 1])


def test_parse_manifest_text_requires_front_matter(tmp_path: Path) -> None:
    with pytest.raises(SkillManifestError, match="front matter"):
        parse_manifest_text("# 只有正文\n", path=tmp_path / "SKILL.md")


def test_parse_manifest_text_requires_description(tmp_path: Path) -> None:
    text = "---\nname: code_review\n---\n正文\n"
    with pytest.raises(SkillManifestError, match="必填字段"):
        parse_manifest_text(text, path=tmp_path / "SKILL.md")


@pytest.mark.parametrize(
    "bad_name",
    ["Code Review", "code_review!", "_leading_underscore", "code--review"],
)
def test_manifest_rejects_bad_name(tmp_path: Path, bad_name: str) -> None:
    text = f"---\nname: {bad_name}\ndescription: x\n---\n正文\n"
    with pytest.raises(SkillManifestError, match="不合法"):
        parse_manifest_text(text, path=tmp_path / "SKILL.md")


@pytest.mark.parametrize("bad_version", ["v1", "1.0.0.0", "1.0.0-"])
def test_manifest_rejects_bad_version(tmp_path: Path, bad_version: str) -> None:
    text = f'---\nname: ok\ndescription: x\nversion: "{bad_version}"\n---\n正文\n'
    with pytest.raises(SkillManifestError, match="版本"):
        parse_manifest_text(text, path=tmp_path / "SKILL.md")


def test_manifest_rejects_unquoted_float_version(tmp_path: Path) -> None:
    """``version: 1.0`` 这种写法 YAML 会解析成 float，报的是类型错。

    这是真实踩过的坑：版本号一律加引号写 ``version: "1.0.0"``，
    否则 pydantic 的 ``string_type`` 报错信息里根本看不到"版本"两个字。
    """
    text = "---\nname: ok\ndescription: x\nversion: 1.0\n---\n正文\n"
    with pytest.raises(SkillManifestError, match="校验失败"):
        parse_manifest_text(text, path=tmp_path / "SKILL.md")


def test_manifest_defaults_and_derived_properties(tmp_path: Path) -> None:
    text = "---\nname: alpha\ndescription: 演示\n---\n正文\n"
    manifest = parse_manifest_text(text, path=tmp_path / "alpha" / "SKILL.md")

    assert manifest.version == "0.1.0"
    assert manifest.enabled is True
    assert manifest.tags == []
    assert manifest.summary == "alpha: 演示"
    assert manifest.dir == tmp_path / "alpha"
    # 空 tenants 归一成 ["*"]：所有租户可见
    assert manifest.tenant_scope == ["*"]
    assert manifest.applies_to(None) is True
    assert manifest.applies_to("acme") is True
    assert manifest.render_full().startswith("# skill: alpha (v0.1.0)")


def test_manifest_matches_tags_and_query(tmp_path: Path) -> None:
    text = (
        "---\nname: code_review\ndescription: 结构化代码审查\n"
        "tags: [review, diff]\n---\n正文\n"
    )
    manifest = parse_manifest_text(text, path=tmp_path / "S" / "SKILL.md")
    assert manifest.matches(tags=["review"]) is True
    assert manifest.matches(tags=["review", "nope"]) is False
    assert manifest.matches(query="REVIEW") is True
    assert manifest.matches(query="nonexistent") is False


def test_manifest_tenant_scope_excludes_anonymous(tmp_path: Path) -> None:
    text = (
        "---\nname: acme_only\ndescription: 只给 acme\n"
        "tenants: [acme]\n---\n正文\n"
    )
    manifest = parse_manifest_text(text, path=tmp_path / "S" / "SKILL.md")
    assert manifest.applies_to(None) is False
    assert manifest.applies_to("acme") is True
    assert manifest.applies_to("globex") is False


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="SKILL.md"):
        load_manifest(tmp_path / "nope")


# ----------------------------------------------------------------------
# 契约 2：原生加载器 vs HarnessSkillLoader
# ----------------------------------------------------------------------
def test_native_loader_skips_bad_skill_silently(tmp_path: Path) -> None:
    """缺 description 的 SKILL.md 被原生加载器**静默丢弃**。"""
    write_skill(tmp_path, "no_description", name="no_description")
    (tmp_path / "no_description" / "SKILL.md").write_text(
        "---\nname: only_name\n---\n正文\n",
        encoding="utf-8",
    )
    loader = LocalSkillLoader(directory=str(tmp_path))
    assert asyncio.run(loader.list_skills()) == []


def test_harness_loader_raises_on_bad_skill(tmp_path: Path) -> None:
    write_skill(tmp_path, "ok", name="ok")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text(
        "---\nname: broken\n---\n正文\n",
        encoding="utf-8",
    )
    loader = HarnessSkillLoader(tmp_path)
    with pytest.raises(SkillManifestError):
        loader.load_manifests()


def test_harness_loader_strict_false_degrades_to_warning(tmp_path: Path) -> None:
    write_skill(tmp_path, "ok", name="ok")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("no front matter\n", "utf-8")
    loader = HarnessSkillLoader(tmp_path, strict=False)
    assert [m.name for m in loader.load_manifests()] == ["ok"]


def test_harness_loader_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        HarnessSkillLoader(tmp_path / "ghost")
    # strict=False 时容忍空目录，只返回空列表
    assert HarnessSkillLoader(tmp_path / "ghost", strict=False).load_manifests() == []


def test_native_scan_subdir_defaults_to_false(tmp_path: Path) -> None:
    """原生默认 ``scan_subdir=False``：两级目录结构一个技能都扫不到。"""
    write_skill(tmp_path, "nested/nested_skill", name="nested_skill")
    assert asyncio.run(LocalSkillLoader(directory=str(tmp_path)).list_skills()) == []
    scanned = LocalSkillLoader(directory=str(tmp_path), scan_subdir=True)
    assert [s.name for s in asyncio.run(scanned.list_skills())] == ["nested_skill"]


def test_harness_loader_scans_subdir_by_default(tmp_path: Path) -> None:
    write_skill(tmp_path, "nested/nested_skill", name="nested_skill")
    assert [
        m.name for m in HarnessSkillLoader(tmp_path).load_manifests()
    ] == ["nested_skill"]


# ----------------------------------------------------------------------
# 契约 2b：真实内置技能
# ----------------------------------------------------------------------
def test_builtin_manifests_are_real() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    names = [m.name for m in loader.load_manifests()]
    assert names == ["code_review", "commit_convention"]

    review = loader.get("code_review")
    assert review.version == "1.0.0"
    assert review.tags == ["review", "diff", "quality"]
    assert review.requires == ["commit_convention"]
    assert review.missing_scripts() == []
    assert review.script_paths[0].name == "review_check.py"

    commit = loader.get("commit_convention")
    assert commit.tags == ["git", "commit", "convention"]
    assert commit.missing_scripts() == []


def test_get_unknown_skill_raises() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    with pytest.raises(UnknownSkillError):
        loader.get("no_such_skill")


def test_get_body_is_second_layer() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    body = loader.get_body("code_review")
    assert body.startswith("# skill: code_review (v1.0.0)")
    assert "scripts/review_check.py" in body


def test_missing_scripts_reported(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "ghost_script",
        name="ghost_script",
        scripts=["scripts/review_check.py"],
    )
    (tmp_path / "ghost_script" / "scripts" / "review_check.py").unlink()

    loader = HarnessSkillLoader(tmp_path, strict=False)
    # strict=True（默认）时连 get() 都会抛：脚本缺失就是无效技能
    with pytest.raises(FileNotFoundError, match="ghost_script"):
        HarnessSkillLoader(tmp_path).get("ghost_script")
    manifest = loader.get("ghost_script")
    assert [p.name for p in manifest.missing_scripts()] == ["review_check.py"]


# ----------------------------------------------------------------------
# 契约 2c：enabled / tenants 过滤
# ----------------------------------------------------------------------
def make_three_tenant_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", name="alpha")
    write_skill(
        tmp_path,
        "beta",
        name="beta",
        extra="enabled: false\n",
    )
    write_skill(
        tmp_path,
        "acme_only",
        name="acme_only",
        extra="tenants: [acme]\n",
    )


def test_enabled_switch_hides_skill(tmp_path: Path) -> None:
    make_three_tenant_skills(tmp_path)
    assert [
        m.name for m in HarnessSkillLoader(tmp_path).load_manifests()
    ] == ["alpha"]


def test_include_disabled_but_tenant_filter_still_applies(tmp_path: Path) -> None:
    """``include_disabled=True`` 只是关掉**开关**过滤，租户过滤照旧。

    这是一个真实的坑：想"诊断时看到全部技能"会以为拿到三份，实际
    ``acme_only`` 仍然被租户挡住 —— 要看它必须同时传 ``tenant="acme"``。
    """
    make_three_tenant_skills(tmp_path)
    loader = HarnessSkillLoader(tmp_path)
    assert [
        m.name for m in loader.load_manifests(include_disabled=True)
    ] == ["alpha", "beta"]
    assert [
        m.name
        for m in loader.load_manifests(include_disabled=True, tenant="acme")
    ] == ["acme_only", "alpha", "beta"]


def test_tenant_whitelist(tmp_path: Path) -> None:
    make_three_tenant_skills(tmp_path)
    loader = HarnessSkillLoader(tmp_path, tenant="acme")
    assert [m.name for m in loader.load_manifests()] == ["acme_only", "alpha"]
    assert [
        m.name for m in HarnessSkillLoader(tmp_path, tenant="globex").load_manifests()
    ] == ["alpha"]


def test_enabled_allowlist(tmp_path: Path) -> None:
    make_three_tenant_skills(tmp_path)
    loader = HarnessSkillLoader(tmp_path, enabled=["alpha"])
    assert [m.name for m in loader.load_manifests()] == ["alpha"]


def test_enabled_allowlist_unknown_name_warns_but_keeps_working(
    tmp_path: Path,
) -> None:
    make_three_tenant_skills(tmp_path)
    loader = HarnessSkillLoader(tmp_path, enabled=["alpha", "alhpa"])
    assert [m.name for m in loader.load_manifests()] == ["alpha"]
    with pytest.raises(UnknownSkillError):
        HarnessSkillLoader(
            tmp_path,
            enabled=["alpha", "alhpa"],
            strict_enabled=True,
        ).load_manifests()


async def test_list_skills_filters_really_work(tmp_path: Path) -> None:
    """过滤落在 ``list_skills()`` 上 —— 也就是 Toolkit 唯一认的入口。

    注意这里比的是**集合**：原生 ``list_skills()`` 的顺序来自 ``os.walk``，
    取决于文件系统，不要对顺序做断言（要稳定顺序请用
    :meth:`HarnessSkillLoader.resolve_order`）。
    """
    make_three_tenant_skills(tmp_path)
    loader = HarnessSkillLoader(tmp_path)
    assert {s.name for s in await loader.list_skills()} == {"alpha"}
    acme = HarnessSkillLoader(tmp_path, tenant="acme")
    assert {s.name for s in await acme.list_skills()} == {"acme_only", "alpha"}


# ----------------------------------------------------------------------
# 契约 3：两层渐进披露
# ----------------------------------------------------------------------
def test_index_text_shape() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    text = loader.index_text()
    assert text.startswith("<skill-index>")
    assert text.rstrip().endswith("</skill-index>")
    assert "- code_review:" in text
    assert "dir=" in text
    # 索引里**没有**正文：这是第一层与第二层的分界线
    assert "## 何时使用" not in text
    assert len(text) < len(loader.get_body("code_review"))


def test_index_text_empty_when_no_skill(tmp_path: Path) -> None:
    assert HarnessSkillLoader(tmp_path).index_text() == ""


def test_select_by_tag_and_query() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    assert [m.name for m in loader.select(tags=["review"])] == ["code_review"]
    assert [
        m.name for m in loader.select(query="commit")
    ] == ["commit_convention"]
    assert loader.select(tags=["nope"]) == []


def test_instruction_templates_differ_by_disclosure() -> None:
    index_tpl = build_skill_instruction_template("index")
    full_tpl = build_skill_instruction_template("full")
    assert "{{ skill_viewer }}" in index_tpl
    assert "<content>" in full_tpl
    assert "<content>" not in index_tpl


# ----------------------------------------------------------------------
# 契约 4：requires 的拓扑与报错
# ----------------------------------------------------------------------
def test_resolve_order_topological() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    assert loader.resolve_order() == ["commit_convention", "code_review"]
    assert loader.enabled_names() == ["commit_convention", "code_review"]


def test_resolve_order_missing_dependency(tmp_path: Path) -> None:
    write_skill(tmp_path, "solo", name="solo", extra="requires: [ghost]\n")
    with pytest.raises(SkillDependencyError, match="ghost"):
        HarnessSkillLoader(tmp_path).resolve_order()


def test_resolve_order_detects_cycle(tmp_path: Path) -> None:
    write_skill(tmp_path, "ping", name="ping", extra="requires: [pong]\n")
    write_skill(tmp_path, "pong", name="pong", extra="requires: [ping]\n")
    with pytest.raises(SkillDependencyError, match="成环"):
        HarnessSkillLoader(tmp_path).resolve_order()


def test_enabled_filter_can_break_dependency() -> None:
    """只留 ``code_review`` 会让它的依赖凭空消失 —— 加载期就报错。"""
    loader = HarnessSkillLoader(BUILTIN, enabled=["code_review"])
    with pytest.raises(SkillDependencyError, match="commit_convention"):
        loader.resolve_order()


# ----------------------------------------------------------------------
# 接入 Toolkit / Profile
# ----------------------------------------------------------------------
async def test_toolkit_sees_index_and_skill_tool() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    toolkit = Toolkit(
        tools=[],
        skills_or_loaders=[loader],
        skill_instruction_template=build_skill_instruction_template("index"),
    )
    instructions = await toolkit.get_skill_instructions()
    assert instructions is not None and "<agent-skills>" in instructions
    assert "code_review" in instructions

    names = {s["function"]["name"] for s in await toolkit.get_tool_schemas()}
    assert "Skill" in names


async def test_skill_viewer_reads_body_on_demand() -> None:
    loader = HarnessSkillLoader(BUILTIN)
    toolkit = Toolkit(tools=[], skills_or_loaders=[loader])
    viewer = toolkit.builtin_skill_viewer.tool
    chunk = await viewer.call(skill="code_review", _agent_state=AgentState())
    text = "".join(getattr(block, "text", "") for block in chunk.content)
    assert chunk.state == "running"
    assert "scripts/review_check.py" in text
    # SkillViewer 给的是去掉 front matter 的正文，没有 harness 的版本头
    assert not text.startswith("# skill:")

    missing = await viewer.call(skill="no_such", _agent_state=AgentState())
    assert missing.state == "error"
    assert "SkillNotFoundError" in missing.content[0].text


async def test_skill_group_is_opt_in() -> None:
    grouped = Toolkit(
        tools=[],
        tool_groups=[
            ToolGroup(
                name="skillgroup",
                description="代码审查与提交规范技能。",
                skills_or_loaders=[HarnessSkillLoader(BUILTIN)],
            ),
        ],
    )
    idle = {s["function"]["name"] for s in await grouped.get_tool_schemas(groups=[])}
    assert "Skill" not in idle
    active = {
        s["function"]["name"]
        for s in await grouped.get_tool_schemas(groups=["skillgroup"])
    }
    assert "Skill" in active
    assert await grouped.get_skill_instructions([]) is None
    assert await grouped.get_skill_instructions(["skillgroup"]) is not None


def test_loaders_from_spec_resolves_relative_dir() -> None:
    settings = Settings.from_env(repo_root=REF, profile_dir=REF / "harness_kit" / "profiles")
    spec = SkillsSpec(
        directories=["./harness_kit/skills/builtin"],
        enabled=["code_review", "commit_convention"],
        scan_subdir=True,
        disclosure="index",
    )
    loaders = loaders_from_spec(spec, settings=settings)
    assert [type(loader).__name__ for loader in loaders] == ["HarnessSkillLoader"]
    assert loaders[0].directory_path == BUILTIN
    assert [m.name for m in loaders[0].load_manifests()] == [
        "code_review",
        "commit_convention",
    ]
    assert loaders_from_spec(SkillsSpec(), settings=settings) == []


# ----------------------------------------------------------------------
# 配套脚本：真的能跑，退出码语义清晰
# ----------------------------------------------------------------------
def run_script(path: Path, *args: str) -> tuple[int, str]:
    """同步跑一个配套脚本。

    Args:
        path (`Path`): 脚本路径。
        *args (`str`): 传给脚本的参数。

    Returns:
        `tuple[int, str]`: 退出码与 stdout。
    """
    completed = subprocess.run(
        [sys.executable, str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout


def test_commit_msg_script_accepts_conventional_commit() -> None:
    script = BUILTIN / "commit_convention" / "scripts" / "check_commit_msg.py"
    code, out = run_script(script, "--string", "feat(mcp): add stdio transport")
    assert code == 0, out
    assert "type=feat" in out


def test_commit_msg_script_rejects_bad_message() -> None:
    script = BUILTIN / "commit_convention" / "scripts" / "check_commit_msg.py"
    code, out = run_script(script, "--string", "Fixed the Bug.")
    assert code == 1
    assert "bad-header" in out


def test_review_script_flags_smelly_code(tmp_path: Path) -> None:
    script = BUILTIN / "code_review" / "scripts" / "review_check.py"
    target = tmp_path / "bad_sample.py"
    target.write_text(
        "def handler(data=[]):\n"
        "    try:\n"
        "        return eval(data)\n"
        "    except:\n"
        "        pass\n",
        encoding="utf-8",
    )
    code, out = run_script(script, str(target), "--json")
    import json

    payload = json.loads(out)
    rules = {finding["rule"] for finding in payload["findings"]}
    assert code == 1
    assert {"bare-except", "dangerous-builtin", "mutable-default-arg"} <= rules

    soft, _ = run_script(script, str(target), "--fail-on", "none")
    assert soft == 0
