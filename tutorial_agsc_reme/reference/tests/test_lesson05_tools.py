# -*- coding: utf-8 -*-
"""第 5 讲的 pytest（交付物之一）：把工具层的七条契约钉成可回归的断言。

为什么工具层尤其需要单测：这一层的错误**几乎全是沉默的**——

- ``ToolPackManifest.groups`` 里写错一个工具名（``"Write"`` 写成 ``"write"``），
  装配不会报错，只是那个工具**悄悄留在了 basic 组**，模型一上来就能写文件；
- JSON schema 里多一个 ``title`` 字段，provider 侧不会报错，只是 token 白烧；
- ``repair_arguments`` 少一个兜底分支，只在模型偶尔输出坏 JSON 时才炸一次；
- 组没激活时调组内工具，错误**不会抛异常**，而是变成一条 ``state=error`` 的
  工具结果回灌给模型 —— 模型看到 "inactive" 就会去激活，于是这个 bug
  在生产里表现为「agent 多花一轮」，而不是一条 traceback。

本文件把这些沉默的错变成红的。

用法（``tests/conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/``
塞进 ``sys.path``，所以不设 ``PYTHONPATH`` 也能跑；这里显式写出来是为了与
验证脚本一致）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson05_tools.py -v

LLM 调用预算：**0 次**。全部测试都打在本地对象与临时 git 仓库上，
不联网、不需要 API key。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolBase, ToolResponse

from harness_kit.config.schema import ToolsSpec
from harness_kit.tools import (
    BASIC_GROUP,
    ArgumentRepairError,
    BuiltinToolPack,
    DuplicateToolNameError,
    PackResolutionError,
    RepoToolPack,
    ToolPackBase,
    ToolPackManifest,
    assert_unique_tool_names,
    build_toolkit,
    ensure_strict_schema,
    repair_arguments,
    resolve_pack_order,
    summarize_tool_result,
)
from harness_kit.tools.builtin_pack import READ_ONLY_ALLOW, calc, now

# ===========================================================================
# 夹具与辅助
# ===========================================================================
DEMO_CALC = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def mul(a, b):\n"
    "    return a * b\n"
)
DEMO_TEST = (
    "from calc import add, mul\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n"
    "\n"
    "\n"
    "def test_mul():\n"
    "    assert mul(2, 3) == 6\n"
)


@pytest.fixture()
def demo_repo(tmp_path: Path) -> Path:
    """一个真的 ``git init`` 过的最小仓库（``RepoTree`` 走 ``git ls-files``）。

    Returns:
        `Path`: 仓库根。
    """
    (tmp_path / "calc.py").write_text(DEMO_CALC, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(DEMO_TEST, encoding="utf-8")
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="l5",
        GIT_AUTHOR_EMAIL="l5@example.com",
        GIT_COMMITTER_NAME="l5",
        GIT_COMMITTER_EMAIL="l5@example.com",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False, env=env)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=False, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=tmp_path,
        check=False,
        env=env,
    )
    return tmp_path


@pytest.fixture()
def repo_packs(demo_repo: Path) -> list[ToolPackBase]:
    """``builtin`` + ``repo`` 两个包（都指向临时仓库）。

    Returns:
        `list[ToolPackBase]`: 未装配的包实例。
    """
    return [
        BuiltinToolPack(workdir=str(demo_repo)),
        RepoToolPack(
            workdir=str(demo_repo),
            test_command=[
                sys.executable,
                "-m",
                "pytest",
                str(demo_repo / "test_calc.py"),
                "-p",
                "no:cacheprovider",
                "-q",
            ],
        ),
    ]


async def call_tool(
    toolkit,
    name: str,
    payload: dict,
    activated: list[str] | None = None,
) -> tuple[str, str]:
    """走 ``Toolkit.call_tool`` 调一个工具，返回 ``(文本, 最终状态)``。

    Args:
        toolkit: 已装配的 ``Toolkit``。
        name (`str`): 工具名。
        payload (`dict`): 入参。
        activated (`list[str] | None`): 预先激活的工具组。

    Returns:
        `tuple[str, str]`: 结果文本与 ``ToolResponse.state`` 的字符串值。
    """
    tool_call = ToolCallBlock(id=name, name=name, input=json.dumps(payload))
    state = AgentState(name="tester")
    state.tool_context.activated_groups = list(activated or [])
    chunks: list[str] = []
    response: ToolResponse | None = None
    async for item in toolkit.call_tool(tool_call, state=state):
        if isinstance(item, ToolResponse):
            response = item
        else:
            chunks.append(
                "".join(block.text for block in item.content if block.type == "text"),
            )
    assert response is not None, "Toolkit.call_tool 没有产出 ToolResponse"
    return "".join(chunks), str(response.state)


def make_pack(
    name: str,
    tool_names: list[str],
    *,
    requires: list[str] | None = None,
    groups: dict[str, list[str]] | None = None,
) -> ToolPackBase:
    """造一个只有名字、没有真实现的包（专供装配顺序 / 重名 / 成环测试）。

    Args:
        name (`str`): 包名。
        tool_names (`list[str]`): 这三个工具名会被做成 ``FunctionTool``。
        requires (`list[str] | None`): 依赖的包名。
        groups (`dict[str, list[str]] | None`): 默认分组。

    Returns:
        `ToolPackBase`: 包实例。
    """
    def _body(**_: object) -> str:
        return "ok"

    _body.__doc__ = "A stub tool.\n\n    Args:\n        **kwargs: ignored.\n"

    class _StubPack(ToolPackBase):
        manifest = ToolPackManifest(
            name=name,
            description=f"stub pack {name}",
            requires=list(requires or []),
            groups=dict(groups or {}),
        )

        async def build_tools(self, spec: ToolsSpec) -> list[ToolBase]:
            return [
                FunctionTool(
                    _body,
                    name=tool_name,
                    is_read_only=True,
                    is_concurrency_safe=True,
                )
                for tool_name in tool_names
            ]

    return _StubPack()


# ===========================================================================
# 1. 清单与依赖排序
# ===========================================================================
def test_manifest_summary_and_group_of() -> None:
    """清单要能回答「这个工具属于哪个组」，且摘要里带版本与规模。"""
    pack = BuiltinToolPack()
    assert pack.manifest.name == "builtin"
    assert pack.manifest.summary() == "builtin@0.1.0 (3 tools, 2 groups)"
    assert pack.manifest.group_of("Bash") == "shell"
    assert pack.manifest.group_of("Write") == "fs_write"
    # 只读工具不入组 → 归 basic
    assert pack.manifest.group_of("Read") is None
    assert pack.manifest.group_of("Now") is None
    assert "Bash" in pack.manifest.dangerous_tools


def test_manifest_forbids_extra_fields() -> None:
    """``extra="forbid"``：YAML 里写错一个键必须当场炸，不能静默丢字段。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ToolPackManifest(name="x", group={"a": ["b"]})  # type: ignore[call-arg]


async def test_tool_names_is_manifest_side_view() -> None:
    """``tool_names()`` 只读清单（组内工具 ∪ 危险工具），**不建工具**。

    所以它**看不到**留在 basic 的只读工具 —— 这是刻意的：这个方法给上层
    做策略筛选用，不该有副作用。
    """
    pack = BuiltinToolPack()
    assert pack.tool_names() == ["Bash", "Edit", "Write"]
    assert pack.describe() == (
        "builtin@0.1.0 (3 tools, 2 groups) [fs, shell, core]"
    )


def test_resolve_pack_order_topological() -> None:
    """``requires`` 被拓扑排序：被依赖的包一定排在前面。"""
    a = make_pack("a", ["A"])
    b = make_pack("b", ["B"], requires=["a"])
    c = make_pack("c", ["C"], requires=["b"])
    order = [p.manifest.name for p in resolve_pack_order([c, a, b])]
    assert order == ["a", "b", "c"]


def test_resolve_pack_order_missing_dependency() -> None:
    """依赖没给出来 → ``PackResolutionError``，且错误信息里要有链条。"""
    a = make_pack("a", ["A"], requires=["nope"])
    with pytest.raises(PackResolutionError) as exc:
        resolve_pack_order([a])
    assert "nope" in str(exc.value)
    assert "a" in str(exc.value)


def test_resolve_pack_order_detects_cycle() -> None:
    """自依赖成环 → 报错而不是无限递归。"""
    a = make_pack("a", ["A"], requires=["a"])
    with pytest.raises(PackResolutionError) as exc:
        resolve_pack_order([a])
    assert "成环" in str(exc.value)


def test_assert_unique_tool_names() -> None:
    """同名工具必须报错：两份同名 schema 会让 provider 行为未定义。"""
    tool = FunctionTool(calc, name="Calc")
    assert_unique_tool_names([tool])  # 单个不报

    other = FunctionTool(now, name="Calc")
    with pytest.raises(DuplicateToolNameError) as exc:
        assert_unique_tool_names([tool, other])
    assert "Calc" in str(exc.value)


# ===========================================================================
# 2. JSON schema 生成（AgentScope 的 FunctionTool）
# ===========================================================================
async def test_function_tool_schema_has_docstring_description() -> None:
    """描述来自 docstring 的**函数段**，参数描述来自 Args 段。"""
    pack = BuiltinToolPack(include_utility=True)
    tools = {tool.name: tool for tool in await pack.build_tools(ToolsSpec())}
    schema = tools["Calc"].input_schema
    assert "arithmetic" in tools["Calc"].description
    assert schema["properties"]["expression"]["description"].startswith("The expression")
    assert schema["required"] == ["expression"]
    assert schema["properties"]["expression"]["type"] == "string"


async def test_function_tool_schema_has_no_title() -> None:
    """``title`` 被剥掉：它是纯 token 噪音，对模型选参数毫无帮助。"""

    def _walk(node: object) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            if "title" in node:
                found.append(str(node["title"]))
            for value in node.values():
                found.extend(_walk(value))
        elif isinstance(node, list):
            for value in node:
                found.extend(_walk(value))
        return found

    pack = BuiltinToolPack()
    tools = {tool.name: tool for tool in await pack.build_tools(ToolsSpec())}
    assert _walk(tools["Calc"].input_schema) == []
    assert _walk(tools["Now"].input_schema) == []


async def test_default_parameters_survive_into_schema() -> None:
    """有默认值的参数不会进 ``required``，但默认值要如实写进 schema。

    ``required`` 全空时**整个键都不出现**（pydantic 的行为），所以断言写成
    ``schema.get("required", [])``，否则会 KeyError。
    """
    pack = BuiltinToolPack()
    tools = {tool.name: tool for tool in await pack.build_tools(ToolsSpec())}
    schema = tools["Now"].input_schema
    assert schema.get("required", []) == []
    assert schema["properties"]["timezone_offset_hours"]["default"] == 0.0


async def test_utility_tools_are_read_only_and_allow() -> None:
    """``Now`` / ``Calc`` 必须显式 ALLOW，否则每次调用都弹权限框。"""
    pack = BuiltinToolPack()
    tools = {tool.name: tool for tool in await pack.build_tools(ToolsSpec())}
    for name in ("Now", "Calc"):
        assert tools[name].is_read_only is True
        assert tools[name].is_concurrency_safe is True
    decision = await tools["Calc"].check_permissions({}, PermissionContext())
    assert decision.behavior is PermissionBehavior.ALLOW
    assert decision is READ_ONLY_ALLOW


async def test_file_tools_pass_permission_through() -> None:
    """文件工具是 PASSTHROUGH：把决策权交给上层权限引擎。"""
    pack = BuiltinToolPack()
    tools = {tool.name: tool for tool in await pack.build_tools(ToolsSpec())}
    decision = await tools["Write"].check_permissions(
        {"file_path": "/tmp/x.py"},
        PermissionContext(),
    )
    assert decision.behavior is PermissionBehavior.PASSTHROUGH


async def test_build_tools_is_idempotent() -> None:
    """同一个包实例重复 ``build_tools`` 返回同一批工具对象。

    否则 ``build_tools`` 与 ``build_toolkit`` 先后调用会造出两套 backend、
    两套 ``Read`` 缓存 —— 见 ``builtin_pack.py`` 模块 docstring。
    """
    pack = BuiltinToolPack()
    spec = ToolsSpec()
    first = await pack.build_tools(spec)
    second = await pack.build_tools(spec)
    assert [id(t) for t in first] == [id(t) for t in second]


async def test_build_tools_rejects_bad_max_result_chars() -> None:
    """``max_result_chars <= 0`` 会让工具结果无上限灌进上下文，必须拒绝。"""
    pack = BuiltinToolPack()
    with pytest.raises(ValueError):
        await pack.build_tools(ToolsSpec(max_result_chars=0))
    with pytest.raises(ValueError):
        await pack.build_tools(ToolsSpec(max_result_chars=-1))


# ===========================================================================
# 3. 装配：basic 常驻、组按需、保留组名、跨包重名
# ===========================================================================
BASIC_EXPECTED = {
    "Glob", "Grep", "Read", "Now", "Calc", "RepoTree", "RepoSearch",
}
GROUP_EXPECTED = {"Write", "Edit", "Bash", "RepoReplace", "RunTests"}


async def visible_names(toolkit, groups: list[str] | None = None) -> set[str]:
    """取「当前对模型可见」的工具名集合。

    ``Toolkit`` 没有 ``get_tool_names``，可见性只能从 ``get_tool_schemas``
    反推 —— 而 schema 数组正是发给 provider 的东西，所以这个反推**就是**
    模型真实看到的内容。

    Args:
        toolkit: 已装配的 ``Toolkit``。
        groups (`list[str] | None`): 要激活的组。

    Returns:
        `set[str]`: 工具名集合。
    """
    schemas = await toolkit.get_tool_schemas(groups=groups)
    return {schema["function"]["name"] for schema in schemas}


async def test_basic_group_holds_only_read_only_tools(repo_packs) -> None:
    """只读的常驻 basic，有副作用的入组。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    basic = await visible_names(toolkit)
    assert basic == BASIC_EXPECTED | {"reset_tools"}


async def test_group_tools_hidden_until_activated(repo_packs) -> None:
    """未激活的组，其工具不出现在可见列表里（模型也就看不到 schema）。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    visible = await visible_names(toolkit, groups=["fs_write", "shell", "repo_write"])
    assert visible == BASIC_EXPECTED | GROUP_EXPECTED | {"reset_tools"}

    tool_set = {
        tool.name
        for group in toolkit.tool_groups
        for tool in group.tools
    }
    assert tool_set == BASIC_EXPECTED | GROUP_EXPECTED


async def test_inactive_group_call_returns_error_state(repo_packs) -> None:
    """组没激活就调组内工具 → 不是抛异常，而是 ``state=error`` 的工具结果。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    text, state = await call_tool(
        toolkit,
        "RepoReplace",
        {"path": "calc.py", "old_text": "def add", "new_text": "def add_"},
    )
    assert state == "error"
    assert "inactive" in text
    assert (repo_packs[0].workdir and
            Path(repo_packs[0].workdir, "calc.py").read_text(encoding="utf-8")
            == DEMO_CALC), "被拒绝的调用不该改到文件"


async def test_activated_group_call_succeeds(repo_packs) -> None:
    """激活之后同样的调用就能落地。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    text, state = await call_tool(
        toolkit,
        "RepoReplace",
        {
            "path": "calc.py",
            "old_text": "return a * b",
            "new_text": "return a * b  # ok",
            "expected_count": 1,
        },
        activated=["repo_write"],
    )
    assert state == "success"
    assert json.loads(text)["replaced"] == 1


async def test_tools_spec_groups_override_manifest(repo_packs) -> None:
    """``ToolsSpec.groups`` 是**整体替换**，且组之间可以重叠。

    这里 ``shell`` 被 spec 改成 ``[Bash, Write]``，但 ``fs_write`` 没被 spec
    提到 → 保留清单默认的 ``[Write, Edit]``。于是 ``Write`` **同时出现在两个
    组里**：AgentScope 不禁止这件事，激活任一组都能看到它。
    想让 ``Write`` 只剩一处，就得在 spec 里把 ``fs_write`` 也显式写掉。
    """
    spec = ToolsSpec(groups={"shell": ["Bash", "Write"]})
    toolkit = await build_toolkit(spec, repo_packs)
    described = {group.name: [t.name for t in group.tools]
                 for group in toolkit.tool_groups}
    assert described["shell"] == ["Bash", "Write"]
    assert described["fs_write"] == ["Edit", "Write"]
    assert described["repo_write"] == ["RepoReplace", "RunTests"]

    # 显式覆盖 fs_write，才真的能把它关掉
    tightened = await build_toolkit(
        ToolsSpec(groups={"shell": ["Bash", "Write"], "fs_write": ["Edit"]}),
        repo_packs,
    )
    groups = {group.name: [t.name for t in group.tools]
              for group in tightened.tool_groups}
    assert groups["fs_write"] == ["Edit"]


async def test_reserved_basic_group_is_rejected(repo_packs) -> None:
    """``basic`` 是 AgentScope 的保留组名，用户在 spec 里写它必须报错。"""
    spec = ToolsSpec(groups={BASIC_GROUP: ["Bash"]})
    with pytest.raises(ValueError) as exc:
        await build_toolkit(spec, repo_packs)
    assert BASIC_GROUP in str(exc.value)


async def test_disabled_tools_drop_out(repo_packs) -> None:
    """``disabled`` 按名字摘工具，且摘掉的工具不出现在任何组里。"""
    spec = ToolsSpec(disabled=["Bash", "Edit"])
    toolkit = await build_toolkit(spec, repo_packs)
    described = {group.name: [t.name for t in group.tools]
                 for group in toolkit.tool_groups}
    everything = {name for names in described.values() for name in names}
    assert "Bash" not in everything
    assert "Edit" not in everything
    # shell 组里只剩被 disabled 掉的 Bash → 整组消失
    assert "shell" not in described


async def test_duplicate_across_packs_rejected() -> None:
    """跨包重名是装配期错误，不能等到 provider 那边才发现。"""
    a = make_pack("a", ["X"])
    b = make_pack("b", ["X"], requires=["a"])
    with pytest.raises(DuplicateToolNameError):
        await build_toolkit(ToolsSpec(), [a, b])


async def test_build_toolkit_rejects_string_packs(repo_packs) -> None:
    """字符串入参要明确指向 ``HarnessBuilder``，而不是悄悄失败。"""
    with pytest.raises(TypeError) as exc:
        await build_toolkit(ToolsSpec(), ["builtin"])  # type: ignore[list-item]
    assert "HarnessBuilder" in str(exc.value)


async def test_meta_tool_reset_tools_present(repo_packs) -> None:
    """只要存在非 basic 组，``Toolkit`` 就自带 meta tool ``reset_tools``。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    assert len(toolkit.tool_groups) == 4
    assert toolkit.tool_groups[0].name == BASIC_GROUP
    assert "reset_tools" in await visible_names(toolkit)
    schema = [
        item for item in await toolkit.get_tool_schemas()
        if item["function"]["name"] == "reset_tools"
    ][0]
    # 组的名字 / 描述会**动态**变成 meta tool 的参数：每组一个 boolean，
    # 参数描述就是 ToolGroup.description —— 这就是组描述必须写好的原因。
    props = schema["function"]["parameters"]["properties"]
    assert set(props) == {"fs_write", "shell", "repo_write"}
    assert all(node["type"] == "boolean" for node in props.values())
    assert all(node["default"] is False for node in props.values())
    assert props["repo_write"]["description"] == (
        "repo_write 工具组，包含工具：RepoReplace, RunTests。"
        "当任务需要这些能力时激活本组。"
    )
    assert "final state" in schema["function"]["description"]
    assert "not incremental" in schema["function"]["description"]


async def test_single_pack_build_toolkit_without_groups() -> None:
    """没有组的包 → 全部落 basic（``ToolPackBase.build_toolkit`` 单包直用）。"""
    pack = make_pack("solo", ["Solo"])
    toolkit = await pack.build_toolkit(ToolsSpec())
    assert [tool.name for tool in toolkit.tool_groups[0].tools] == ["Solo"]
    assert len(toolkit.tool_groups) == 1


async def test_single_pack_uses_pack_own_group_description() -> None:
    """单包 ``build_toolkit`` 用的是**包自己**的 ``_group_description``。

    而 :func:`build_multi_pack_toolkit` 用的是模块级的
    :func:`_default_group_description` —— 也就是说多包装配时，``BuiltinToolPack``
    精心写的那句提示词**会被丢掉**。这条差异是真实的（第 5 讲源码侦察里
    有讲到），所以钉成测试而不是当它不存在。
    """
    pack = make_pack("solo", ["Solo"], groups={"g": ["Solo"]})
    single = await pack.build_toolkit(ToolsSpec())
    assert single.tool_groups[1].description == (
        "g 工具组，来自工具包 solo；包含工具：Solo。当任务需要这些能力时激活本组。"
    )

    multi = await build_toolkit(ToolsSpec(), [pack])
    assert multi.tool_groups[1].description == (
        "g 工具组，包含工具：Solo。当任务需要这些能力时激活本组。"
    )


# ===========================================================================
# 4. 参数修复
# ===========================================================================
def test_repair_empty_is_empty_dict() -> None:
    """无参工具的 ``input`` 常常是空串，不该报错。"""
    assert repair_arguments("") == {}
    assert repair_arguments("   \n ") == {}


def test_repair_plain_json() -> None:
    """正常 JSON 原样通过。"""
    assert repair_arguments('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"expression": "1+1"}\n```',
        '{"expression": "1+1",}',
        '{"expression": “1+1”}',
        '{"expression": "1+1"',
    ],
    ids=["code-fence", "trailing-comma", "smart-quotes", "missing-brace"],
)
def test_repair_string_fixups_deterministic(raw: str) -> None:
    """确定性的字符串修补：代码围栏 / 尾逗号 / 弯引号 / 缺右括号。"""
    assert repair_arguments(raw) == {"expression": "1+1"}


def test_repair_accepts_python_literals() -> None:
    """兜底 ``ast.literal_eval``：模型给了单引号的 Python 字面量。"""
    assert repair_arguments("{'expression': '1+1'}") == {"expression": "1+1"}


def test_repair_type_coercion_via_schema() -> None:
    """给了 schema 时，json_repair 会顺手把 ``"42"`` 改成 ``42``。"""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    assert repair_arguments('{"n": "42"}', schema) == {"n": 42}


def test_repair_raises_argument_repair_error() -> None:
    """修不成 **dict** 时抛 ``ArgumentRepairError``（``[1,2,3]`` 合法但没有字段）。

    注意 ``json_repair`` 极其宽容：``'{"a": }'`` 会被修成 ``{'a': ''}``，
    ``'{{{{'`` 才会失败。所以这里用「合法 JSON 但不是 object」来钉住
    「必须返回 dict」这条契约。
    """
    with pytest.raises(ArgumentRepairError) as exc:
        repair_arguments("[1, 2, 3]")
    assert exc.value.raw == "[1, 2, 3]"
    assert exc.value.attempts == ["json_loads_with_repair", "ast_literal_eval"]

    with pytest.raises(ArgumentRepairError):
        repair_arguments("{{{{")


# ===========================================================================
# 5. schema 收紧与结果截断
# ===========================================================================
def test_ensure_strict_schema_recursively() -> None:
    """``additionalProperties: False`` 要递归地补到每个 object 节点上。"""
    schema = {
        "type": "object",
        "properties": {
            "inner": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
        },
    }
    strict = ensure_strict_schema(schema)
    assert strict["additionalProperties"] is False
    assert strict["properties"]["inner"]["additionalProperties"] is False
    assert strict["required"] == []
    # 原对象没被改（深拷贝）
    assert "additionalProperties" not in schema


def test_ensure_strict_schema_require_all() -> None:
    """``require_all=True`` 把 ``required`` 补成全部 properties。"""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    strict = ensure_strict_schema(schema, require_all=True)
    assert sorted(strict["required"]) == ["a", "b"]


def test_summarize_short_text_untouched() -> None:
    """没超限就一个字都不动。"""
    text = "short"
    assert summarize_tool_result(text, max_chars=100) == text
    assert summarize_tool_result("x" * 50, max_chars=0) == "x" * 50


def test_summarize_keeps_head_and_tail() -> None:
    """头尾都留：错误信息几乎总在尾部。"""
    text = "A" * 300 + "B" * 100
    out = summarize_tool_result(text, max_chars=100, head_ratio=0.6)
    assert out.startswith("A")
    assert out.endswith("B" * 40)
    assert "已截断" in out
    assert len(out) <= 100 + 40  # 标记本身要占几十个字符


def test_summarize_marker_reports_dropped() -> None:
    """标记里要写清丢了多少字符，模型才知道「可以分段再读一次」。"""
    text = "x" * 1000
    out = summarize_tool_result(text, max_chars=100)
    assert "{dropped}" not in out
    assert "已截断" in out


# ===========================================================================
# 6. 真实工具执行（``Now`` / ``Calc`` / 仓库四件套）
# ===========================================================================
def test_calc_arithmetic() -> None:
    """``Calc`` 用 AST 白名单求值，不碰 ``eval``。"""
    assert calc("(1+2)*3/7") == "(1+2)*3/7 = 1.2857142857142858"
    assert calc("2**10") == "2**10 = 1024"
    assert calc("round(pi, 3)") == "round(pi, 3) = 3.142"


def test_calc_refuses_dangerous_input() -> None:
    """属性访问 / 导入 / 超大幂次一律拒绝，且**返回错误字符串**而不是抛异常。"""
    assert calc("__import__('os').system('ls')").startswith("错误：")
    assert calc("(1).__class__").startswith("错误：")
    assert calc("2**999").startswith("错误：")
    assert calc("1/0").startswith("错误：")
    assert calc("1 +").startswith("错误：")


def test_now_shape() -> None:
    """``Now`` 的输出是 ISO 时间 + 星期 + 时区，带显式偏移。"""
    text = now(timezone_offset_hours=8)
    assert "tz=UTC+8" in text
    assert "weekday=" in text
    assert now(timezone_offset_hours=8).endswith(tuple("0123456789)."))  # 带秒


async def test_repo_tree_and_search(demo_repo: Path, repo_packs) -> None:
    """``RepoTree`` / ``RepoSearch`` 是只读的，开局就能用。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    tree, state = await call_tool(toolkit, "RepoTree", {"path": "."})
    assert state == "success"
    payload = json.loads(tree)
    assert payload["ok"] is True
    assert payload["total"] == 2

    found, state = await call_tool(toolkit, "RepoSearch", {"pattern": "def mul"})
    assert state == "success"
    hits = json.loads(found)
    assert hits["ok"] is True
    assert hits["source"] == "git grep"
    assert hits["total_matches"] == 1
    assert hits["file_count"] == 1
    assert hits["matches"][0]["file"] == "calc.py"
    assert hits["matches"][0]["hits"] == ["5:def mul(a, b):"]


async def test_repo_replace_is_atomic_on_ambiguity(demo_repo: Path, repo_packs) -> None:
    """``old_text`` 命中数不等于 ``expected_count`` → 拒绝写入，文件原封不动。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    before = (demo_repo / "calc.py").read_text(encoding="utf-8")
    text, state = await call_tool(
        toolkit,
        "RepoReplace",
        {"path": "calc.py", "old_text": "def", "new_text": "def ", "expected_count": 1},
        activated=["repo_write"],
    )
    assert state == "success", "工具自己把错误转成了结果，不是异常"
    payload = json.loads(text)
    assert payload["ok"] is False
    assert payload["actual_count"] == 2
    assert payload["expected_count"] == 1
    assert (demo_repo / "calc.py").read_text(encoding="utf-8") == before


async def test_run_tests_summarizes_result(demo_repo: Path, repo_packs) -> None:
    """``RunTests`` 把 pytest 的 ``exit_code`` / 通过数 / 失败数揉成 JSON。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    text, state = await call_tool(toolkit, "RunTests", {}, activated=["repo_write"])
    assert state == "success"
    payload = json.loads(text)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["passed"] == 2
    assert payload["failed"] == 0


async def test_run_tests_reports_failure(demo_repo: Path, repo_packs) -> None:
    """把实现改坏 → ``ok=False`` / ``exit_code=1`` / ``failed=1``。"""
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    await call_tool(
        toolkit,
        "RepoReplace",
        {
            "path": "calc.py",
            "old_text": "return a * b",
            "new_text": "return a * b + 1",
            "expected_count": 1,
        },
        activated=["repo_write"],
    )
    text, state = await call_tool(toolkit, "RunTests", {}, activated=["repo_write"])
    assert state == "success"
    payload = json.loads(text)
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert payload["failed"] == 1
    assert payload["passed"] == 1


async def test_read_tool_sees_written_content(demo_repo: Path, repo_packs) -> None:
    """``Read`` 与 ``Write`` 共用一个 backend，写完立刻能读到（缓存不打架）。

    这条测试盯着 ``builtin_pack`` 模块 docstring 里讲的坑：各建一个
    ``LocalBackend()`` 时 ``Read`` 的缓存会给出旧内容。参数名是
    ``file_path``（AgentScope 要求绝对路径），不是 ``path``。
    """
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    target = str(demo_repo / "new.txt")
    text, state = await call_tool(
        toolkit,
        "Write",
        {"file_path": target, "content": "hello\n"},
        activated=["fs_write"],
    )
    assert state == "success"
    assert "written successfully" in text

    text, state = await call_tool(toolkit, "Read", {"file_path": target})
    assert state == "success"
    assert "hello" in text


async def test_group_description_is_present_and_factual(repo_packs) -> None:
    """组描述是模型决定「激活哪一组」的唯一依据，必须非空且成员如实。

    ``ToolGroup`` 对非 ``basic`` 组**强制**要求 description
    （``.../tool/_tool_group.py``），所以这里能拿到的一定不是空串；
    但「非空」不等于「有用」，多包装配只会拼一句通用的
    「包含工具：X, Y」—— 想给业务措辞就得自己覆写
    :func:`build_multi_pack_toolkit` 里的那一行。
    """
    toolkit = await build_toolkit(ToolsSpec(), repo_packs)
    described = {group.name: group.description for group in toolkit.tool_groups}
    assert described["fs_write"] == (
        "fs_write 工具组，包含工具：Edit, Write。当任务需要这些能力时激活本组。"
    )
    assert described["repo_write"] == (
        "repo_write 工具组，包含工具：RepoReplace, RunTests。"
        "当任务需要这些能力时激活本组。"
    )
    assert described[BASIC_GROUP] == ""  # basic 组不需要（也不允许）描述
    for name, description in described.items():
        if name != BASIC_GROUP:
            assert description.strip()
