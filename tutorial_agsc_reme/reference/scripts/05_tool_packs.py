# -*- coding: utf-8 -*-
"""第 5 讲验证脚本：工具包契约、schema 生成、装配与真实执行。

默认**完全离线**（0 次 LLM 调用）；加 ``--live`` 才会打一次 DeepSeek
（用于证明「我们自己写的工具包能被官方 Agent 正常调用」）。

用法::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/05_tool_packs.py
    # 再加一个 --live 跑端到端
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
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionContext
from agentscope.state import AgentState
from agentscope.tool import ToolResponse, Toolkit

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

VERIFY_ROOT: Path = Path(harness_kit.__file__).resolve().parent.parent
"""本脚本所在的 ``reference/``（或验证目录）根。"""


# ======================================================================
# 小工具
# ======================================================================
def section(title: str) -> None:
    """打印一个小节标题。

    Args:
        title (`str`): 标题文本。
    """
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def make_demo_repo(root: Path) -> Path:
    """造一个最小 git 仓库（真跑 ``git``，保证 ``RepoTree`` 有输出）。

    Args:
        root (`Path`): 目标目录。

    Returns:
        `Path`: 仓库根。
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (root / "test_calc.py").write_text(
        "from calc import add, mul\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "\n"
        "\n"
        "def test_mul():\n"
        "    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="l5",
        GIT_AUTHOR_EMAIL="l5@example.com",
        GIT_COMMITTER_NAME="l5",
        GIT_COMMITTER_EMAIL="l5@example.com",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=False, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=False, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root,
                   check=False, env=env)
    return root


async def call_tool(
    toolkit: Toolkit,
    name: str,
    payload: dict,
    activated: list[str] | None = None,
) -> tuple[str, str]:
    """通过 ``Toolkit.call_tool`` 调一个工具，返回 ``(text, state)``。

    Args:
        toolkit (`Toolkit`): 已装配的工具集。
        name (`str`): 工具名。
        payload (`dict`): 工具入参。
        activated (`list[str] | None`): 预先激活的工具组，写进
            ``AgentState.tool_context.activated_groups``。

    Returns:
        `tuple[str, str]`: 结果文本与最终状态。
    """
    state = AgentState()
    state.tool_context.activated_groups = list(activated or [])
    tool_call = ToolCallBlock(id=name, name=name, input=json.dumps(payload))
    text = ""
    final_state = "?"
    async for chunk in toolkit.call_tool(tool_call, state):
        if isinstance(chunk, ToolResponse):
            final_state = str(chunk.state)
            break
        text += chunk.content[0].text
        final_state = str(chunk.state)
    return text, final_state


def one_line(text: str, limit: int) -> str:
    """把多行 JSON 压成一行便于阅读。

    Args:
        text (`str`): 原始文本。
        limit (`int`): 截断长度。

    Returns:
        `str`: 压平并截断后的文本。
    """
    return " ".join(text.split())[:limit]


# ======================================================================
# 各小节
# ======================================================================
async def part_a_manifest(builtin: BuiltinToolPack, repo_pack: RepoToolPack) -> None:
    """A. 清单与 ``describe()``。

    Args:
        builtin (`BuiltinToolPack`): 基础包。
        repo_pack (`RepoToolPack`): 仓库包。
    """
    section("A. 清单：ToolPackManifest 与 describe()")
    for pack in (builtin, repo_pack):
        print(f"  {pack.manifest.name:8s} {pack.describe()}")
        print(f"           groups    = {pack.manifest.groups}")
        print(f"           requires  = {pack.manifest.requires}")
        print(f"           dangerous = {pack.manifest.dangerous_tools}")
    print("  BASIC_GROUP =", BASIC_GROUP)
    print("  builtin 清单里声明的工具名 =", builtin.tool_names())


async def part_b_schema(builtin: BuiltinToolPack, spec: ToolsSpec) -> list:
    """B. 从函数签名 + docstring 自动生成 JSON Schema。

    Args:
        builtin (`BuiltinToolPack`): 基础包。
        spec (`ToolsSpec`): 工具声明。

    Returns:
        `list`: 基础包造出的工具。
    """
    section("B. FunctionTool：签名 + docstring → JSON Schema")
    tools = await builtin.build_tools(spec)
    by_name = {tool.name: tool for tool in tools}
    for name in ("Now", "Calc", "Read"):
        tool = by_name[name]
        print(f"  {name:5s} read_only={tool.is_read_only!s:5s} "
              f"concurrency_safe={tool.is_concurrency_safe!s:5s} "
              f"| {tool.description.splitlines()[0][:52]}")
    print("  --- Calc.input_schema ---")
    print(json.dumps(by_name["Calc"].input_schema, ensure_ascii=False, indent=2))
    ctx = PermissionContext()
    allow = await by_name["Calc"].check_permissions({}, ctx)
    write = await by_name["Write"].check_permissions({"file_path": "x.py"}, ctx)
    print("  Calc.check_permissions  ->", allow.behavior,
          "|", allow.decision_reason)
    print("  Write.check_permissions ->", write.behavior)
    return tools


async def part_c_assembly(
    spec: ToolsSpec,
    builtin: BuiltinToolPack,
    repo_pack: RepoToolPack,
) -> Toolkit:
    """C. 多包装配成一个 Toolkit，并看「组可见性」。

    Args:
        spec (`ToolsSpec`): 工具声明。
        builtin (`BuiltinToolPack`): 基础包。
        repo_pack (`RepoToolPack`): 仓库包。

    Returns:
        `Toolkit`: 装配好的工具集。
    """
    section("C. 多包装配：basic 常驻 + 其余组按需激活")
    toolkit = await build_toolkit(spec, [builtin, repo_pack])
    for group in toolkit.tool_groups:
        print(f"  {group.name:12s} -> {[t.name for t in group.tools]}")
        if group.name != BASIC_GROUP:
            print(f"      {group.description}")
    basic_only = await toolkit.get_tool_schemas()
    all_groups = await toolkit.get_tool_schemas(groups=["fs_write", "shell", "repo_write"])
    print("  未激活任何组时可见：", sorted(s["function"]["name"] for s in basic_only))
    print("  激活全部组后可见  ：", sorted(s["function"]["name"] for s in all_groups))
    print("  工具 schema 的形状：", json.dumps(all_groups[0]["function"]["name"]))
    return toolkit


async def part_d_repair(calc_schema: dict) -> None:
    """D. 参数修复。

    Args:
        calc_schema (`dict`): ``Calc`` 的 input schema。
    """
    section("D. 参数修复：模型给的坏 JSON 有几种形状")
    cases = [
        ("合法", '{"expression": "1+1"}'),
        ("包了代码块", '```json\n{"expression": "2*3"}\n```'),
        ("单引号", "{'expression': '3**2'}"),
        ("尾随逗号", '{"expression": "10/4",}'),
        ("被截断", '{"expression": "abs(-7)"'),
        ("空串（无参）", ""),
        ("无可救药", "so sorry, I cannot tell you"),
    ]
    for label, raw in cases:
        try:
            print(f"  {label:12s} -> {repair_arguments(raw, calc_schema)}")
        except ArgumentRepairError as exc:
            print(f"  {label:12s} -> ArgumentRepairError attempts={exc.attempts}")


def part_e_strict() -> None:
    """E. schema 收紧。"""
    section("E. ensure_strict_schema：收紧 schema")
    loose = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
    }
    print("  原始  :", json.dumps(ensure_strict_schema(loose), ensure_ascii=False))
    print("  全必填:", json.dumps(ensure_strict_schema(loose, require_all=True),
                                 ensure_ascii=False))


def part_f_summarize() -> None:
    """F. 结果截断。"""
    section("F. summarize_tool_result：头 60% + 尾 40%")
    text = "\n".join(f"line {i}" for i in range(1, 501))
    out = summarize_tool_result(text, max_chars=200)
    print(f"  原文 {len(text)} 字符 -> {len(out)} 字符")
    print("  头:", repr(out[:36]))
    print("  中:", repr(out[110:150]))
    print("  尾:", repr(out[-36:]))


async def part_g_errors(by_name: dict, spec: ToolsSpec) -> None:
    """G. 装配期错误。

    Args:
        by_name (`dict`): 工具名 → 工具。
        spec (`ToolsSpec`): 工具声明。
    """

    class NeedsMissing(ToolPackBase):
        """一个依赖了不存在的包的包。"""

        manifest = ToolPackManifest(name="needs_x", requires=["x_not_given"])

        async def build_tools(self, spec: ToolsSpec) -> list:
            """不造工具。

            Args:
                spec (`ToolsSpec`): 工具声明。

            Returns:
                `list`: 空列表。
            """
            return []

    class SelfCyclic(ToolPackBase):
        """一个依赖自己的包。"""

        manifest = ToolPackManifest(name="cyc", requires=["cyc"])

        async def build_tools(self, spec: ToolsSpec) -> list:
            """不造工具。

            Args:
                spec (`ToolsSpec`): 工具声明。

            Returns:
                `list`: 空列表。
            """
            return []

    section("G. 装配期错误：依赖缺失 / 成环 / 跨包重名 / 传字符串")
    for label, packs in (("依赖缺失", [NeedsMissing()]), ("自依赖成环", [SelfCyclic()])):
        try:
            resolve_pack_order(packs)
        except PackResolutionError as exc:
            print(f"  {label} -> {exc}")
    try:
        assert_unique_tool_names([by_name["Now"], by_name["Calc"], by_name["Now"]])
    except DuplicateToolNameError as exc:
        print("  重名 ->", exc)
    try:
        await build_toolkit(spec, ["builtin"])  # type: ignore[list-item]
    except TypeError as exc:
        print("  字符串入参 ->", exc)


async def part_h_switches(spec: ToolsSpec, builtin: BuiltinToolPack,
                          repo_pack: RepoToolPack) -> None:
    """H. ``ToolsSpec`` 的三个开关。

    Args:
        spec (`ToolsSpec`): 工具声明。
        builtin (`BuiltinToolPack`): 基础包。
        repo_pack (`RepoToolPack`): 仓库包。
    """
    section("H. ToolsSpec 的三个开关：disabled / groups 覆盖 / 保留组名")
    trimmed = ToolsSpec(
        disabled=["Bash", "Edit"],
        groups={"fs_write": ["Write"]},
        max_result_chars=4000,
    )
    toolkit = await build_toolkit(trimmed, [builtin, repo_pack])
    print("  disabled=['Bash','Edit'] + groups 覆盖后：")
    for group in toolkit.tool_groups:
        print(f"    {group.name:12s} -> {[t.name for t in group.tools]}")
    try:
        await build_toolkit(ToolsSpec(groups={"basic": ["Read"]}), [builtin])
    except ValueError as exc:
        print("  用保留组名 'basic' ->", one_line(str(exc), 70), "...")


async def part_i_real_run() -> None:
    """I. 真跑工具（本地 git 仓库）。"""
    section("I. 真跑：仓库工具组 + 只读工具（临时 git 仓库）")
    demo = make_demo_repo(Path(tempfile.mkdtemp(prefix="l5_repo_")))
    print("  临时仓库 =", demo)
    runner = RepoToolPack(
        workdir=str(demo),
        test_command=[
            sys.executable,
            "-m",
            "pytest",
            str(demo / "test_calc.py"),
            "-p",
            "no:cacheprovider",
            "-q",
        ],
    )
    toolkit = await build_toolkit(
        ToolsSpec(max_result_chars=8000),
        [BuiltinToolPack(workdir=str(demo)), runner],
    )
    activated = ["fs_write", "shell", "repo_write"]

    text, state = await call_tool(toolkit, "RepoTree", {"path": "."})
    print("  RepoTree     ->", one_line(text, 170), "|", state)
    text, state = await call_tool(toolkit, "RepoSearch", {"pattern": "def mul"})
    print("  RepoSearch   ->", one_line(text, 190), "|", state)

    print("  --- 组没激活时调组内工具 ---")
    text, state = await call_tool(
        toolkit, "RepoReplace",
        {"path": "calc.py", "old_text": "return a * b",
         "new_text": "return a * b  # noqa", "expected_count": 1},
    )
    print("  RepoReplace  ->", one_line(text, 130), "|", state)

    print("  --- 激活 fs_write / shell / repo_write 之后 ---")
    text, state = await call_tool(
        toolkit, "RepoReplace",
        {"path": "calc.py", "old_text": "return a * b",
         "new_text": "return a * b  # noqa", "expected_count": 1},
        activated,
    )
    print("  RepoReplace  ->", one_line(text, 170), "|", state)
    text, state = await call_tool(
        toolkit, "RepoReplace",
        {"path": "calc.py", "old_text": "def", "new_text": "def ",
         "expected_count": 1},
        activated,
    )
    print("  RepoReplace(歧义) ->", one_line(text, 200), "|", state)

    text, state = await call_tool(toolkit, "RunTests", {}, activated)
    print("  RunTests(全绿) ->", one_line(text, 190), "|", state)

    text, state = await call_tool(
        toolkit, "RepoReplace",
        {"path": "calc.py", "old_text": "return a * b  # noqa",
         "new_text": "return a * b + 1", "expected_count": 1},
        activated,
    )
    print("  RepoReplace(改坏) ->", one_line(text, 150), "|", state)
    text, state = await call_tool(toolkit, "RunTests", {}, activated)
    print("  RunTests(有失败) ->", one_line(text, 250), "|", state)
    text, state = await call_tool(
        toolkit, "RepoReplace",
        {"path": "calc.py", "old_text": "return a * b + 1",
         "new_text": "return a * b  # noqa", "expected_count": 1},
        activated,
    )
    print("  RepoReplace(改回) ->", one_line(text, 150), "|", state)

    text, state = await call_tool(toolkit, "Calc", {"expression": "(1+2)*3/7"})
    print("  Calc         ->", repr(text), "|", state)
    text, state = await call_tool(toolkit, "Now", {"timezone_offset_hours": 8})
    print("  Now          ->", repr(text), "|", state)


async def part_j_live() -> None:
    """J. 端到端：官方 Agent + 我们的工具包（1 次 LLM 调用）。"""
    section("J. 端到端：官方 Agent 驱动我们的工具包（1 次 DeepSeek 调用）")
    from dotenv import load_dotenv

    from agentscope.agent import Agent
    from agentscope.agent._config import ReActConfig
    from agentscope.credential import OpenAICredential
    from agentscope.event import ModelCallStartEvent, ToolCallStartEvent
    from agentscope.message import Msg, TextBlock
    from agentscope.model import OpenAIChatModel

    repo_root = Path(
        "/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning",
    )
    load_dotenv(repo_root / ".env", override=False)

    demo = make_demo_repo(Path(tempfile.mkdtemp(prefix="l5_agent_")))
    print("  临时仓库 =", demo)
    toolkit = await build_toolkit(
        ToolsSpec(max_result_chars=8000),
        [
            BuiltinToolPack(workdir=str(demo)),
            RepoToolPack(workdir=str(demo)),
        ],
    )

    model = OpenAIChatModel(
        credential=OpenAICredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ.get("OPENAI_BASE_URL"),
        ),
        model=os.getenv("LLM_MODEL", "deepseek-flash"),
        stream=False,
    )
    agent = Agent(
        name="l5-agent",
        system_prompt="你是仓库助手。回答前必须用工具查证，不要凭记忆猜。",
        model=model,
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=6),
    )
    trace: list[str] = []
    model_calls = 0
    final = None
    async for chunk in agent.reply_stream(
        Msg(
            name="user",
            role="user",
            content=[
                TextBlock(
                    text="仓库里有哪些文件？哪个文件定义了函数 mul？",
                ),
            ],
        ),
        yield_final_msg=True,
    ):
        if isinstance(chunk, Msg):
            final = chunk
        elif isinstance(chunk, ModelCallStartEvent):
            model_calls += 1
        elif isinstance(chunk, ToolCallStartEvent):
            trace.append(chunk.tool_call_name)

    assert final is not None
    print("  模型调用轨迹 =", trace)
    print("  本轮真实模型调用次数 =", model_calls)
    print("  agent  ->", final.get_text_content())


async def main() -> int:
    """跑完全部小节。

    Returns:
        `int`: 0 表示跑完。
    """
    import agentscope

    print("harness_kit =", harness_kit.__file__)
    print("Python      =", sys.version.split()[0])
    print("agentscope  =", agentscope.__version__)

    workdir = str(VERIFY_ROOT)
    builtin = BuiltinToolPack(workdir=workdir)
    repo_pack = RepoToolPack(workdir=workdir)
    spec = ToolsSpec(max_result_chars=8000)

    await part_a_manifest(builtin, repo_pack)
    tools = await part_b_schema(builtin, spec)
    by_name = {tool.name: tool for tool in tools}
    toolkit = await part_c_assembly(spec, builtin, repo_pack)
    await part_d_repair(by_name["Calc"].input_schema)
    part_e_strict()
    part_f_summarize()
    await part_g_errors(by_name, spec)
    await part_h_switches(spec, BuiltinToolPack(workdir=workdir),
                          RepoToolPack(workdir=workdir))
    await part_i_real_run()

    if "--live" in sys.argv:
        await part_j_live()
    else:
        section("J. 端到端：Agent + 生产工具包（需 --live）")
        print("  （未加 --live，跳过；本节真实输出见教程第五部分 5.3）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
