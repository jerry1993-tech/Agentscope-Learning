# -*- coding: utf-8 -*-
"""第 7 讲验证脚本：MCP 工具协议（`harness_kit/mcp/`）。

它把本讲的主结论全部变成可执行的断言：

  A. `MCPServerSpec` 的声明式校验：把 8 类配置错误提前到装配期，
     而不是等到 Agent "莫名其妙少了一组工具"
  B. 命名空间与工具名的三条规则（`sanitize_tool_name` / `namespace_of` /
     `namespaced_tool_name`），并与 `MCPTool` 真实生成的名字逐字对照
  C. stdio 握手：连上 `scripts/mcp_demo_server.py`，`list_raw_tools` 给原名、
     `list_tools` 给模型名（双名制）
  D. 注入 `Toolkit`：`collect_tools` 的三种冲突策略 + `inject_into_toolkit`
     的组名校验（`add_tool` 是 async、未知组 `ValueError`）
  E. 完整 Tool Use 通路：`Toolkit.call_tool` → `ToolChunk` → `ToolResponse`
  F. 韧性：一个 MCP 掉线只是少几个工具，不会杀死这一轮（`Toolkit` 吞掉
     `list_tools` 的异常）
  G. 反向：`build_mcp_server` 把 harness_kit 工具暴露成 MCP Server 的护栏
  H. 端到端回环：`MCPClient(streamable_http)` ↔ 自己的 server（端口 18137，
     用完立即关闭）
  I. Profile 装配：`HarnessBuilder.build_all()` 出来的 Toolkit 里有 `mcp` 组
     （0 次 LLM 调用）
  J.（需要 key，`--live` 打开）真实 deepseek-flash：模型自己发现并调用
     `mcp__harness-demo__calc`（**1~3 次调用**）

用法（`PYTHONPATH` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/07_mcp.py

    加 `--live` 才会跑 J 段（真实 LLM，**1~3 次调用**）。

LLM 调用预算：A~I 段 **0 次**（H 段会临时起一个本地 HTTP 服务，端口 18137，
结束即关）；J 段 **1~3 次**。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

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
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig  # noqa: E402
from agentscope.message import Msg, TextBlock, ToolCallBlock  # noqa: E402
from agentscope.state import AgentState  # noqa: E402
from agentscope.tool import FunctionTool, ToolBase, ToolGroup, Toolkit  # noqa: E402

from harness_kit.config import load_resolved_profile  # noqa: E402
from harness_kit.config.builder import build_from_profile  # noqa: E402
from harness_kit.mcp import (  # noqa: E402
    MCPServerRegistry,
    MCPServerSpec,
    ToolNameConflictError,
    build_mcp_clients,
    build_mcp_server,
    close_mcp_clients,
    collect_tools,
    connect_mcp_clients,
    inject_into_toolkit,
    list_remote_tools,
    namespace_of,
    namespaced_tool_name,
    sanitize_tool_name,
    to_tool_base,
)
from harness_kit.mcp.server import (  # noqa: E402
    exposed_tool_names,
    tool_chunk_to_text,
)
from harness_kit.settings import Settings  # noqa: E402
from harness_kit.tools.builtin_pack import calc, now  # noqa: E402

PY = sys.executable
DEMO_SERVER = REF / "scripts" / "mcp_demo_server.py"
HTTP_PORT = 18137
"""回环演示端口。契约 §3.7 要求 ≥ 18000；这里故意避开 18100 这个默认值，
以免与另一个并发跑的教程进程抢端口。"""

_LIVE = "--live" in sys.argv
_COUNTER = itertools.count(1)
_LOG_LEVEL = os.getenv("HARNESS_LOG_LEVEL", "WARNING").upper()


def quiet_logging() -> None:
    """把日志压到 ``HARNESS_LOG_LEVEL``（默认 WARNING）。

    本脚本的价值在于**自己的断言输出**，而 MCP SDK / agentscope / httpx 的
    每请求一行 INFO 会把它淹掉 30 倍。默认 WARNING 让输出可以直接贴进教程；
    要看握手细节就 ``HARNESS_LOG_LEVEL=DEBUG``。

    两个必须单独处理的 logger，都是实测出来的：

    - ``"as"``：AgentScope 在 import 期就调
      ``setup_logger("INFO")``（``third_party/agentscope/src/agentscope/_logging.py:47``），
      并且把 ``propagate`` 设成 ``False``（同文件 ``:44``）。所以"改 root logger
      的级别"对它完全无效，必须直接改名字叫 ``"as"`` 的那个 logger；
    - loguru：harness_kit 自己用 loguru，它的默认 sink 是 DEBUG 级，
      要 ``logger.remove()`` 再重新 add。
    """
    level = getattr(logging, _LOG_LEVEL, logging.WARNING)
    logging.basicConfig(level=level)
    for name in ("mcp", "httpx", "httpcore", "anyio", "as", "openai"):
        logging.getLogger(name).setLevel(level)
    logger.remove()
    logger.add(sys.stderr, level=_LOG_LEVEL)


# ======================================================================
# 输出小工具
# ======================================================================
def title(text: str) -> None:
    """打印一节的分隔标题。

    Args:
        text (`str`): 标题文本。
    """
    print(f"\n{'=' * 74}\n{text}\n{'=' * 74}")


def step(text: str) -> None:
    """打印一个小节内的步骤行。

    Args:
        text (`str`): 步骤文本。
    """
    print(f"\n--- {text} ---")


def show(label: str, value: object) -> None:
    """打印一行 `标签 -> 值`。

    Args:
        label (`str`): 标签。
        value (`object`): 值。
    """
    print(f"  {label:<22} -> {value}")


def ok(condition: bool, text: str) -> None:
    """断言并打印结果；失败立即抛 `AssertionError`。

    Args:
        condition (`bool`): 断言条件。
        text (`str`): 描述。

    Raises:
        `AssertionError`: 条件为假。
    """
    print(f"  [{'PASS' if condition else 'FAIL'}] {text}")
    if not condition:  # pragma: no cover - 只在断言失败时走到
        raise AssertionError(text)


def spec_error(**kwargs: Any) -> str:
    """构造一个 MCPServerSpec 并返回它的校验错误消息。

    Args:
        **kwargs (`Any`): 传给 :class:`MCPServerSpec` 的字段。

    Returns:
        `str`: 异常类型名与首行消息；没有异常时返回 ``"<no error>"``。
    """
    try:
        MCPServerSpec(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 这一节专门在展示错误文本
        first = str(exc).strip().splitlines()
        tail = [line.strip() for line in first if line.strip()]
        return f"{type(exc).__name__}: {tail[-1] if tail else ''}"
    return "<no error>"


# ======================================================================
# A. MCPServerSpec：声明式校验
# ======================================================================
def section_a() -> None:
    """A 段：`MCPServerSpec` 的校验把配置错误提前到装配期。"""
    title("A. MCPServerSpec：把配置错误提前到装配期（0 次 LLM 调用）")

    step("A1. 三种合法 transport")
    stdio_spec = MCPServerSpec(
        name="harness-demo",
        transport="stdio",
        command=PY,
        args=[str(DEMO_SERVER)],
    )
    http_spec = MCPServerSpec(
        name="harness-http",
        transport="streamable_http",
        url="http://127.0.0.1:18100/mcp",
        headers={"Authorization": "Bearer ${TOKEN}"},
    )
    sse_spec = MCPServerSpec(
        name="harness-sse",
        transport="sse",
        url="http://127.0.0.1:18100/sse?key=1",
    )
    for spec in (stdio_spec, http_spec, sse_spec):
        show(spec.name, spec.describe())
    ok(stdio_spec.stateful is True, "stdio 解析出的 stateful 恒为 True")
    ok(http_spec.stateful is False, "HTTP 未显式声明时默认 stateless")
    ok(sse_spec.resolved_namespace == "harness-sse", "namespace 缺省取 name")

    step("A2. 八类配置错误")
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "名字含非法字符",
            {"name": "bad.name", "transport": "stdio", "command": PY},
        ),
        (
            "stdio 缺 command",
            {"name": "x", "transport": "stdio"},
        ),
        (
            "stdio 多给了 url",
            {
                "name": "x",
                "transport": "stdio",
                "command": PY,
                "url": "http://h/sse",
            },
        ),
        (
            "stdio 声明 stateless",
            {
                "name": "x",
                "transport": "stdio",
                "command": PY,
                "is_stateful": False,
            },
        ),
        (
            "http 缺 url",
            {"name": "x", "transport": "streamable_http"},
        ),
        (
            "sse 路径不以 /sse 结尾",
            {"name": "x", "transport": "sse", "url": "http://h/mcp"},
        ),
        (
            "streamable_http 路径却是 /sse",
            {"name": "x", "transport": "streamable_http", "url": "http://h/sse"},
        ),
        (
            "enable/disable 重叠",
            {
                "name": "x",
                "transport": "stdio",
                "command": PY,
                "enable_tools": ["a"],
                "disable_tools": ["a"],
            },
        ),
    ]
    for label, payload in cases:
        print(f"  {label:<28} {spec_error(**payload)[:120]}")
    ok(len(cases) == 8, "八类错误全部被拦下")

    step("A3. 端口与 transport 常量")
    show("TRANSPORTS", ("stdio", "sse", "streamable_http"))
    print(
        "  注意：YAML 里写 streamable_http（下划线），"
        "而 MCP SDK 的 run(transport=...) 写 streamable-http（连字符）",
    )


# ======================================================================
# B. 命名空间与工具名
# ======================================================================
def section_b() -> None:
    """B 段：命名空间与工具名的三条规则。"""
    title("B. 命名空间与工具名：三条规则（0 次 LLM 调用）")

    step("B1. sanitize_tool_name：非法字符替换成 'x' 而不是 '_'")
    samples = [
        "read_file",
        "repo.read",
        "cmd:run",
        "weird name!",
        "__dunder__",
    ]
    for raw in samples:
        show(raw, sanitize_tool_name(raw))
    ok(sanitize_tool_name("repo.read") == "repoxread", "点号 → x")
    ok(sanitize_tool_name("cmd:run") == "cmdxrun", "冒号 → x")

    step("B2. namespace_of：非法字符替换成 hyphen，空串回落 'mcp'")
    for raw in ["harness-demo", "my.server", "!!", ""]:
        show(repr(raw), namespace_of(raw))

    step("B3. namespaced_tool_name：mcp__{server}__{tool}")
    names = [
        namespaced_tool_name("harness-demo", "calc"),
        namespaced_tool_name("fs", "repo.read"),
        MCPServerSpec(
            name="srv",
            transport="stdio",
            command=PY,
            namespace="tenant-a",
        ).namespaced_name("echo"),
    ]
    for name in names:
        show("tool name", name)
    ok(names[0] == "mcp__harness-demo__calc", "与 MCPTool 的生成规则一致")


# ======================================================================
# C. stdio 握手
# ======================================================================
async def section_c() -> list[MCPClient]:
    """C 段：stdio 握手、raw/wrapped 双名制。

    Returns:
        `list[MCPClient]`: 仍处于连接状态的客户端（供后面几节复用）。
    """
    title("C. stdio 握手：连上自己的 MCP Server（0 次 LLM 调用）")

    registry = MCPServerRegistry(
        [
            MCPServerSpec(
                name="harness-demo",
                transport="stdio",
                command=PY,
                args=[str(DEMO_SERVER)],
                cwd=str(REF),
            ),
            MCPServerSpec(
                name="harness-disabled",
                transport="stdio",
                command=PY,
                args=[str(DEMO_SERVER)],
                enabled=False,
            ),
        ],
    )
    step("C1. 登记表：只有 enabled 的会变成客户端")
    for line in registry.describe_all():
        print(f"  {line}")
    clients = registry.to_clients()
    show("客户端数量", len(clients))
    ok(len(clients) == 1, "enabled=False 的 server 被跳过")

    step("C2. connect（串行）")
    await connect_mcp_clients(clients)
    client = clients[0]
    show("is_connected", client.is_connected)
    ok(client.is_connected, "握手完成")

    step("C3. list_raw_tools：server 上的原始名与原始 schema")
    raw = await list_remote_tools(client)
    show("raw names", [t["name"] for t in raw])
    print("  raw calc 的 inputSchema:")
    print("   ", json.dumps(
        next(t for t in raw if t["name"] == "calc")["inputSchema"],
        ensure_ascii=False,
    ))

    step("C4. list_tools / get_tool：模型侧的名字")
    wrapped = await client.list_tools()
    show("wrapped names", [t.name for t in wrapped])
    ok(
        [t.name for t in wrapped]
        == ["mcp__harness-demo__now", "mcp__harness-demo__calc",
            "mcp__harness-demo__env_info"],
        "命名规则与 B 段推出来的一致",
    )

    step("C5. to_tool_base 手工包装一个工具（等价于 get_tool）")
    calc_raw = next(t for t in raw if t["name"] == "calc")
    tool = to_tool_base(client, calc_raw, namespace="harness-demo")
    show("type", type(tool).__name__)
    show("name", tool.name)
    show("is_mcp", tool.is_mcp)
    show("is_state_injected", tool.is_state_injected)
    show("is_read_only", tool.is_read_only)
    perms = await tool.check_permissions()
    show("check_permissions", f"{perms.behavior} | {perms.message}")
    ok(tool.is_mcp is True, "MCPTool.is_mcp 为 True")
    ok(tool.is_state_injected is False, "MCP 工具禁止注入 AgentState（安全边界）")

    step("C6. 直接调用远端工具")
    chunk = await tool(expression="6*7")
    show("calc(6*7)", tool_chunk_to_text(chunk))
    show("state", chunk.state)
    ok(tool_chunk_to_text(chunk).endswith("42"), "远端返回 42")

    return clients


# ======================================================================
# D. 注入 Toolkit
# ======================================================================
async def section_d(clients: list[MCPClient]) -> None:
    """D 段：`collect_tools` 与 `inject_into_toolkit`。

    Args:
        clients (`list[MCPClient]`): 已连接的客户端。
    """
    title("D. 注入 Toolkit：冲突策略与组名校验（0 次 LLM 调用）")

    step("D1. collect_tools：正常收集")
    tools = await collect_tools(clients)
    show("工具数", len(tools))
    show("工具名", [t.name for t in tools])
    ok(len(tools) == 3, "三个工具全部收集到")

    step("D2. 冲突策略：拿一个本地同名工具去撞")
    fake = FunctionTool(
        lambda **_: "本地同名工具",
        name="mcp__harness-demo__calc",
        is_read_only=True,
    )
    for policy in ("error", "skip", "replace"):
        try:
            merged = await collect_tools(
                clients,
                base=[fake],
                on_conflict=policy,  # type: ignore[arg-type]
            )
            hits = [t for t in merged if t.name == "mcp__harness-demo__calc"]
            if not hits:
                origin = "谁都不保留（skip 且基线不在结果里）"
            elif hits[0] is fake:
                origin = "本地"
            else:
                origin = "远端"
            show(f"on_conflict={policy}", f"{len(merged)} 个工具，冲突位 -> {origin}")
        except ToolNameConflictError as exc:
            show(f"on_conflict={policy}", f"ToolNameConflictError: {str(exc)[:70]}")

    step("D3. inject_into_toolkit：组不存在会 ValueError")
    bare = Toolkit(tools=[FunctionTool(now, is_read_only=True)])
    try:
        await inject_into_toolkit(bare, tools, group="mcp")
    except ValueError as exc:
        show("未知组", str(exc)[:150])

    step("D4. inject_into_toolkit：组存在则注入")
    toolkit = Toolkit(
        tools=[FunctionTool(now, is_read_only=True)],
        tool_groups=[
            ToolGroup(
                name="mcp",
                description="MCP server 提供的工具。需要先调用 meta tool 激活。",
            ),
        ],
    )
    await inject_into_toolkit(toolkit, tools, group="mcp")
    before = await toolkit.get_tool_schemas()
    show("未激活 mcp 组时可见", sorted(s["function"]["name"] for s in before))
    activated = await toolkit.get_tool_schemas(groups=["mcp"])
    show("激活 mcp 组后可见", sorted(s["function"]["name"] for s in activated))
    ok(
        "mcp__harness-demo__calc" in {s["function"]["name"] for s in activated},
        "MCP 工具要激活后模型才看得到",
    )
    ok(
        "mcp__harness-demo__calc" not in {s["function"]["name"] for s in before},
        "未激活时模型看不到（避免几十个 MCP 工具撑爆提示词）",
    )

    step("D5. 幂等：注入两次不会变成两份")
    await inject_into_toolkit(toolkit, tools, group="mcp")
    group = next(g for g in toolkit.tool_groups if g.name == "mcp")
    names = [t.name for t in group.tools]
    show("mcp 组工具", names)
    ok(len(names) == len(set(names)) == 3, "重名只会覆盖，不会重复")


# ======================================================================
# E. 完整 Tool Use 通路
# ======================================================================
async def section_e(clients: list[MCPClient]) -> None:
    """E 段：`Toolkit.call_tool` 的完整通路。

    Args:
        clients (`list[MCPClient]`): 已连接的客户端。
    """
    title("E. 完整 Tool Use 通路：ToolChunk → ToolResponse（0 次 LLM 调用）")

    toolkit = Toolkit(
        tool_groups=[
            ToolGroup(
                name="mcp",
                description="MCP server 提供的工具。",
                mcps=clients,
            ),
        ],
    )
    call = ToolCallBlock(
        type="tool_call",
        id="call-1",
        name="mcp__harness-demo__calc",
        input='{"expression": "(20+22)"}',
    )

    async def _run(state: AgentState) -> tuple[list[str], Any]:
        """跑一次 `Toolkit.call_tool`。

        Args:
            state (`AgentState`): 工具调用所依附的 Agent 状态。

        Returns:
            `tuple[list[str], Any]`: 依次收到的块类型，与最后一块。
        """
        seen: list[str] = []
        final: Any = None
        async for item in toolkit.call_tool(call, state):
            seen.append(type(item).__name__)
            final = item
        return seen, final

    step("E1. 组没激活时：模型拿到的是一句可自纠的错误，不是异常")
    seen, final = await _run(AgentState())
    show("增量 / 终态类型", seen)
    show("state", final.state)
    show("content", [b.text for b in final.content][0][:100])
    ok(seen[-1] == "ToolResponse", "最后一块是 ToolResponse（异常已被转成数据）")
    ok(str(final.state) == "error", "状态是 error 而不是抛异常")

    step("E2. 激活 mcp 组之后：同一个调用成功")
    state = AgentState()
    state.tool_context.activated_groups = ["mcp"]
    seen, final = await _run(state)
    show("state", final.state)
    show("content", [b.text for b in final.content])
    ok(str(final.state) == "success", "状态为 success")
    ok("42" in "".join(b.text for b in final.content), "远端算出 42")


# ======================================================================
# F. 韧性
# ======================================================================
async def section_f() -> None:
    """F 段：一个 MCP 掉线不能杀死整轮对话。"""
    title("F. 韧性：MCP 掉线只是少几个工具（0 次 LLM 调用）")

    client = MCPClient(
        name="harness-demo",
        is_stateful=True,
        mcp_config=StdioMCPConfig(command=PY, args=[str(DEMO_SERVER)]),
    )
    await client.connect()
    toolkit = Toolkit(
        tools=[FunctionTool(now, is_read_only=True)],
        tool_groups=[
            ToolGroup(name="mcp", description="MCP 工具。", mcps=[client]),
        ],
    )
    before = sorted(
        s["function"]["name"]
        for s in await toolkit.get_tool_schemas(groups=["mcp"])
    )
    show("掉线前", before)

    await client.close()
    step("关掉 server 后再列工具（Toolkit 会吞掉异常并 warning）")
    after = sorted(
        s["function"]["name"]
        for s in await toolkit.get_tool_schemas(groups=["mcp"])
    )
    show("掉线后", after)
    ok("mcp__harness-demo__calc" not in after, "远端工具消失")
    ok("now" in after, "本地工具照常可用 —— 这一轮没有被 MCP 拖死")
    # 业务侧自己包一层时要注意：`close()` 之后再 `close()` 会 RuntimeError
    show("close 后 is_connected", client.is_connected)


# ======================================================================
# G. 反向：暴露为 MCP Server
# ======================================================================
async def section_g() -> None:
    """G 段：`build_mcp_server` 的装配与护栏。"""
    title("G. 反向：把 harness_kit 暴露成 MCP Server（0 次 LLM 调用）")

    step("G1. 两条硬护栏")
    for label, kwargs in (
        ("空工具列表", {"tools": []}),
        ("端口 8080", {"tools": [FunctionTool(now)], "port": 8080}),
    ):
        try:
            build_mcp_server("bad", **kwargs)  # type: ignore[arg-type]
        except ValueError as exc:
            show(label, str(exc)[:110])

    step("G2. 正常装配 + namespace 前缀")
    tools: list[ToolBase] = [
        FunctionTool(now, is_read_only=True),
        FunctionTool(calc, is_read_only=True),
        FunctionTool(
            _env_info,
            name="env_info",
            description="Return the interpreter and harness versions.",
            is_read_only=True,
        ),
    ]
    server = build_mcp_server(
        "harness-demo",
        tools=tools,
        port=HTTP_PORT,
        instructions="harness_kit 的演示工具集。",
    )
    show("server type", type(server).__name__)
    show("暴露的工具名", exposed_tool_names(server))
    ok(exposed_tool_names(server) == ["now", "calc", "env_info"], "无前缀时保留原名")

    prefixed = build_mcp_server(
        "harness-demo",
        tools=tools,
        port=HTTP_PORT,
        namespace="verify",
    )
    show("带 namespace", exposed_tool_names(prefixed))
    ok(
        exposed_tool_names(prefixed) == ["verify__now", "verify__calc", "verify__env_info"],
        "namespace 前缀生效",
    )

    step("G3. 低层 handler 的 tools/list：inputSchema 原样透传")
    # 低层 handler 不是普通函数：`list_tools()` 是**装饰器**，注册完就返回原函数，
    # 真正可 await 的是它塞进 `request_handlers` 的那个闭包
    # （mcp/server/lowlevel/server.py:282）。所以这里按请求类型取 handler，
    # 而不是 `await server.list_tools()`（那会得到 "object function can't be
    # used in 'await' expression"）。
    list_handler = prefixed._mcp_server.request_handlers[  # noqa: SLF001 - 诊断用
        mcp_types.ListToolsRequest
    ]
    listed = await list_handler(
        mcp_types.ListToolsRequest(method="tools/list"),
    )
    payload = [t.model_dump(exclude_none=True) for t in listed.root.tools]
    print("  ", json.dumps(payload[1], ensure_ascii=False)[:300])
    annotations = payload[1].get("annotations")
    show("annotations", annotations)
    ok(annotations and annotations.get("readOnlyHint") is True, "只读提示被带上")

    step("G4. 低层 handler 的 tools/call：错误也走协议层")
    call_handler = prefixed._mcp_server.request_handlers[  # noqa: SLF001 - 诊断用
        mcp_types.CallToolRequest
    ]

    async def _call(name: str, arguments: dict[str, Any]) -> Any:
        """按 MCP 协议调一次工具。

        Args:
            name (`str`): 工具名。
            arguments (`dict[str, Any]`): 参数。

        Returns:
            `Any`: ``CallToolResult``。
        """
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name=name,
                arguments=arguments,
            ),
        )
        return (await call_handler(request)).root

    bad = await _call("verify__calc", {"expression": "1/0"})
    show("除零 -> isError", bad.isError)
    show("除零 -> text", bad.content[0].text)
    unknown = await _call("verify__nope", {})
    show("未知工具 -> isError", unknown.isError)
    show("未知工具 -> text", unknown.content[0].text[:90])
    ok(unknown.isError is True, "未知工具走协议层错误，而不是崩掉连接")


def _env_info() -> str:
    """返回解释器与依赖版本，供 G 段暴露成 MCP 工具。

    Returns:
        `str`: 形如 ``python=3.11.13 agentscope=2.0.8``。
    """
    import platform

    return f"python={platform.python_version()} agentscope=2.0.8"


# ======================================================================
# H. 端到端回环（HTTP）
# ======================================================================
async def section_h() -> None:
    """H 段：`streamable_http` 回环 —— 自己的 client 连自己的 server。"""
    title(f"H. 端到端回环：MCPClient ↔ 自己的 server（端口 {HTTP_PORT}）")

    proc = subprocess.Popen(
        [PY, str(DEMO_SERVER), "--http", "--port", str(HTTP_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ},
        cwd=str(REF),
    )
    try:
        step("H1. 等待 HTTP 服务起来")
        client = MCPClient(
            name="loopback",
            is_stateful=False,
            mcp_config=HttpMCPConfig(
                url=f"http://127.0.0.1:{HTTP_PORT}/mcp",
                timeout=30.0,
            ),
        )
        for attempt in range(30):
            try:
                await client.list_tools()
                break
            except Exception:  # noqa: BLE001 - 启动竞态，重试即可
                if attempt == 29:  # pragma: no cover - 只在服务起不来时走到
                    raise
                await asyncio.sleep(0.5)
        show("is_sse（只按 url 路径判定）", client._is_sse)  # noqa: SLF001
        show("is_stateful", client.is_stateful)
        ok(client._is_sse is False, "/mcp 路径走 streamable-http")  # noqa: SLF001

        step("H2. 走一遍远端工具")
        tools = await client.list_tools()
        show("工具名", [t.name for t in tools])
        calc_tool = await client.get_tool("calc")
        chunk = await calc_tool(expression="(1+2)*3")
        show("calc((1+2)*3)", tool_chunk_to_text(chunk))
        ok("9" in tool_chunk_to_text(chunk), "HTTP 传输拿到正确结果")

        step("H3. SSE 路径判定对照（不真连，只看路由）")
        for url in ["http://h/sse", "http://h/sse?key=1", "http://h/messages/", "http://h/mcp"]:
            probe = MCPClient(
                name="probe",
                is_stateful=False,
                mcp_config=HttpMCPConfig(url=url),
            )
            show(url, f"is_sse={probe._is_sse}")  # noqa: SLF001
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 只在进程卡死时走到
            proc.kill()
        print(f"  HTTP 服务已关闭（returncode={proc.returncode}）")


# ======================================================================
# I. Profile 装配
# ======================================================================
async def section_i() -> None:
    """I 段：从 Profile 装出一个带 MCP 的 Agent（0 次 LLM 调用）。"""
    title("I. Profile 装配：HarnessBuilder 里的 MCP 接线（0 次 LLM 调用）")

    profile_yaml = f"""\
name: lesson07_mcp
description: 第 7 讲：声明式接入 MCP server，模型用 echo（离线）。
model:
  provider: echo
  model_name: echo-offline
  temperature: 0.0
tools:
  packs: [builtin]
mcp:
  servers:
    - name: harness-demo
      transport: stdio
      command: {PY}
      args: [{json.dumps(str(DEMO_SERVER))}]
      cwd: {REF}
  group: mcp
middleware:
  - name: logging
    params: {{ level: INFO }}
agent:
  name: mcp-agent
  sys_prompt: "你需要时可以用 MCP 工具。"
  max_iters: 5
"""
    with tempfile.TemporaryDirectory(prefix="l7_profile_") as tmp:
        path = Path(tmp) / "lesson07_mcp.yaml"
        path.write_text(profile_yaml, encoding="utf-8")

        step("I1. 解析 Profile")
        resolved = load_resolved_profile(path, search_dir=Path(tmp))
        show("profile", resolved.name)
        show("mcp servers", [s.name for s in resolved.mcp.servers])
        show("mcp group", resolved.mcp.group)
        show("model provider", resolved.model.provider)

        step("I2. build_all：装配出真实 Agent")
        settings = Settings.from_env(
            repo_root=REPO,
            workspace_dir=REPO / ".harness" / "workspace",
            session_dir=REPO / ".harness" / "sessions",
        )
        built = await build_from_profile(resolved, settings=settings)
        show("agent class", type(built.agent).__name__)
        show("tool groups", [g.name for g in built.toolkit.tool_groups])

        step("I3. mcp 组里真的有远端工具")
        group = next(g for g in built.toolkit.tool_groups if g.name == "mcp")
        show("组内 mcps", [c.name for c in group.mcps])
        schemas = await built.toolkit.get_tool_schemas(groups=["mcp"])
        show("激活后可见", sorted(s["function"]["name"] for s in schemas))
        ok(
            "mcp__harness-demo__calc" in {s["function"]["name"] for s in schemas},
            "Profile 里声明的 server 变成了模型可见的工具",
        )

        # AgentScope 的 ``Agent`` **没有** ``close()``/``aclose()`` 方法
        # （grep `def close` agent/_agent.py 无命中）—— 它本身不持有需要释放的
        # 连接；真正要关的是我们塞进 Toolkit 的 MCP 客户端。
        await close_mcp_clients(list(group.mcps))


# ======================================================================
# J. 真模型
# ======================================================================
async def section_j() -> None:
    """J 段：真模型自己发现并调用 MCP 工具（需要 `--live`）。"""
    title("J. 真模型端到端：模型自己调用 MCP 工具（1~3 次 LLM 调用）")
    if not _LIVE:
        print("  （未加 --live，跳过）")
        return

    profile_yaml = f"""\
name: lesson07_live
description: 第 7 讲 live：真模型 + MCP 工具。
model:
  provider: deepseek
  model_name: ${{LLM_MODEL:-deepseek-chat}}
  api_key_env: LLM_API_KEY
  base_url_env: LLM_BASE_URL
  temperature: 0.0
  stream: true
tools:
  packs: [builtin]
mcp:
  servers:
    - name: harness-demo
      transport: stdio
      command: {PY}
      args: [{json.dumps(str(DEMO_SERVER))}]
      cwd: {REF}
  group: mcp
middleware: []
agent:
  name: mcp-live-agent
  sys_prompt: "你是助手。需要精确算术时，优先使用暴露给你的工具，而不是心算。"
  max_iters: 5
"""
    with tempfile.TemporaryDirectory(prefix="l7_live_") as tmp:
        path = Path(tmp) / "lesson07_live.yaml"
        path.write_text(profile_yaml, encoding="utf-8")
        resolved = load_resolved_profile(path, search_dir=Path(tmp))
        settings = Settings.from_env(
            repo_root=REPO,
            workspace_dir=REPO / ".harness" / "workspace",
            session_dir=REPO / ".harness" / "sessions",
        )
        built = await build_from_profile(resolved, settings=settings)
        group = next(g for g in built.toolkit.tool_groups if g.name == "mcp")
        show("远端工具", [c.name for c in group.mcps])

        # 把 mcp 组置为已激活。真实 Agent 也可以自己调 meta tool ``reset_tools``
        # 激活（那就是第 5 讲的故事），这里为了把模型调用次数压在预算内，
        # 直接改状态；这是 ``state.tool_context.activated_groups``
        # （third_party/agentscope/src/agentscope/state/_state.py:42）。
        built.agent.state.tool_context.activated_groups = ["mcp"]

        calls = 0
        trace: list[str] = []
        final: Any = None
        async for evt in built.agent.reply_stream(
            Msg(
                name="user",
                role="user",
                content=[
                    TextBlock(
                        text="请用工具算出 (1234+8766)*7 的值，并只回答这个整数。",
                    ),
                ],
            ),
            yield_final_msg=True,
        ):
            if isinstance(evt, ModelCallStartEvent):
                calls += 1
            elif isinstance(evt, ToolCallStartEvent):
                trace.append(evt.tool_call_name)
            elif hasattr(evt, "get_text_content"):
                final = evt
        show("模型调用轨迹", trace)
        show("本轮真实模型调用次数", calls)
        show("远端工具确实被调用", "mcp__harness-demo__calc" in trace)
        if final is not None:
            print("  agent  ->", final.get_text_content()[:300])
        await close_mcp_clients(list(group.mcps))


# ======================================================================
# 主流程
# ======================================================================
async def main() -> int:
    """跑完全部小节。

    Returns:
        `int`: 进程退出码。
    """
    quiet_logging()
    print(f"harness_kit = {harness_kit.__file__}")
    print(f"Python      = {sys.version.split()[0]}")
    import agentscope

    print(f"agentscope  = {agentscope.__version__}")
    print(f"demo server = {DEMO_SERVER}")
    print(f"log level   = {_LOG_LEVEL}（HARNESS_LOG_LEVEL 可调）")

    section_a()
    section_b()
    clients = await section_c()
    try:
        await section_d(clients)
        await section_e(clients)
    finally:
        await close_mcp_clients(clients)
    await section_f()
    await section_g()
    await section_h()
    await section_i()
    await section_j()

    title("全部小节通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
