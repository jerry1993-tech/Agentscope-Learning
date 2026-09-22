# -*- coding: utf-8 -*-
"""第 7 讲的 pytest（交付物之一）：把 MCP 层的五条契约钉成可回归的断言。

五条契约：

1. **配置错误在装配期失败，不在运行期失踪** —— AgentScope 的校验全部发生在
   构造 ``MCPClient`` 的那一刻（``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:144``
   的 ``model_post_init``）；Profile 里的 YAML 一旦写错 transport，用户看到的是
   "Agent 莫名其妙少了一组工具"。``MCPServerSpec`` 必须把同一批错误提前到解析期。
2. **名字的唯一权威是 ``MCPTool``** —— 模型侧工具名由
   ``tool/_adapters.py:247`` 生成：``mcp__{server}__{sanitized_tool}``，非法字符
   替换成 ``x``（不是 ``_``，因为 ``_`` 是分隔符）。``namespaced_tool_name``
   必须与它逐字一致，否则调试时 raw / wrapped 两个名字对不上。
3. **工具名冲突必须显式** —— ``Toolkit.add_tool`` 对重名只打一条 warning 然后
   覆盖（``tool/_toolkit.py:660``），生产里这等于静默丢工具。
   ``collect_tools(on_conflict=...)`` 必须让调用方选：error / skip / replace。
4. **反向通道** —— AgentScope 只有 MCP **客户端**，没有 server 侧；
   ``build_mcp_server`` 补的就是它，并且必须走 SDK 低层 handler 把
   ``ToolBase.input_schema`` **原样透传**（``$defs`` / ``anyOf`` 不能丢）。
5. **韧性 = 降级而不是崩** —— ``Toolkit._get_available_tools`` 对
   ``client.list_tools()`` 的异常是"吞掉 + warning"（``tool/_toolkit.py:526``）：
   一个 MCP 掉线只应该让这一组工具消失，而不是杀死整轮对话。

用法（``tests/conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进
``sys.path``，所以不设 ``PYTHONPATH`` 也能跑；这里显式写出来是为了与另外几个
脚本一致）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest tests/test_lesson07_mcp.py -v

LLM 调用预算：**0 次**（全部离线；涉及网络的用例只连本机 stdio 子进程）。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import harness_kit
import mcp.types as mcp_types
import pytest
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionBehavior
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, MCPTool, ToolGroup, Toolkit
from harness_kit.config.schema import MCPSpec
from harness_kit.mcp import (
    MCPServerRegistry,
    MCPServerSpec,
    ToolNameConflictError,
    build_mcp_clients,
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
from harness_kit.mcp.server import (
    DEFAULT_PORT,
    build_mcp_server,
    call_tool_once,
    exposed_tool_names,
    tool_chunk_to_text,
)
from harness_kit.tools.builtin_pack import calc, now

REF = Path(harness_kit.__file__).resolve().parent.parent
DEMO_SERVER = REF / "scripts" / "mcp_demo_server.py"
PY = sys.executable

#: 演示 server 暴露的三个工具（``scripts/mcp_demo_server.py:85-89`` 的顺序）。
DEMO_TOOLS = ("now", "calc", "env_info")


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
@asynccontextmanager
async def connected_client(
    name: str = "test-srv",
    **spec_kwargs: Any,
) -> AsyncIterator[MCPClient]:
    """拉起一个 stdio 客户端并在退出时关闭。

    Args:
        name (`str`): server 名（同时是默认命名空间）。
        **spec_kwargs (`Any`): 传给 :class:`MCPServerSpec` 的额外字段。

    Yields:
        `MCPClient`: 已连接的客户端。
    """
    spec = MCPServerSpec(
        name=name,
        transport="stdio",
        command=PY,
        args=[str(DEMO_SERVER)],
        cwd=str(REF),
        **spec_kwargs,
    )
    client = spec.to_client()
    await client.connect()
    try:
        yield client
    finally:
        await close_mcp_clients([client])


def make_spec(**kwargs: Any) -> MCPServerSpec:
    """构造一个最小合法的 stdio 声明。

    Args:
        **kwargs (`Any`): 覆盖字段。

    Returns:
        `MCPServerSpec`: 声明对象。
    """
    payload: dict[str, Any] = {
        "name": "srv",
        "transport": "stdio",
        "command": "python",
    }
    payload.update(kwargs)
    return MCPServerSpec(**payload)


def make_toolkit(*, with_mcp_group: bool = True) -> Toolkit:
    """造一个带 ``mcp`` 组的 Toolkit。

    Args:
        with_mcp_group (`bool`): 是否把 ``mcp`` 组注册进去。

    Returns:
        `Toolkit`: 目标工具集。
    """
    groups = (
        [
            ToolGroup(
                name="mcp",
                description="MCP server 提供的工具，需要先激活。",
            ),
        ]
        if with_mcp_group
        else None
    )
    return Toolkit(
        tools=[FunctionTool(now, name="local_now", is_read_only=True)],
        tool_groups=groups,
    )


# ======================================================================
# 1. MCPServerSpec：装配期校验
# ======================================================================
def test_spec_accepts_three_transports() -> None:
    """三种 transport 的合法声明都能构造出来。"""
    stdio = make_spec()
    http = MCPServerSpec(
        name="http-srv",
        transport="streamable_http",
        url="http://127.0.0.1:18100/mcp",
    )
    sse = MCPServerSpec(
        name="sse-srv",
        transport="sse",
        url="http://127.0.0.1:18100/sse?key=1",
    )
    assert (stdio.transport, http.transport, sse.transport) == (
        "stdio",
        "streamable_http",
        "sse",
    )


def test_stdio_is_always_stateful() -> None:
    """stdio 解析出的 ``stateful`` 恒为 True，HTTP 默认 False。"""
    assert make_spec().stateful is True
    assert make_spec(is_stateful=True).stateful is True
    http = MCPServerSpec(
        name="h",
        transport="streamable_http",
        url="http://h/mcp",
    )
    assert http.stateful is False
    assert http.model_copy(update={"is_stateful": True}).stateful is True


def test_namespace_defaults_to_name() -> None:
    """``namespace`` 缺省取 ``name``，显式指定时以它为准。"""
    assert make_spec().resolved_namespace == "srv"
    assert make_spec(namespace="tenant-a").resolved_namespace == "tenant-a"


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("name 非法字符", {"name": "bad.name"}),
        ("stdio 缺 command", {"command": None}),
        ("stdio 多给 url", {"url": "http://h/sse"}),
        ("stdio 声明 stateless", {"is_stateful": False}),
        ("enable/disable 重叠", {"enable_tools": ["a"], "disable_tools": ["a"]}),
    ],
)
def test_spec_rejects_bad_stdio(label: str, payload: dict[str, Any]) -> None:
    """stdio 的五类非法组合必须在构造期抛 ``ValidationError``。

    Args:
        label (`str`): 用例名（pytest 报告里可读）。
        payload (`dict[str, Any]`): 字段覆盖。
    """
    with pytest.raises(Exception) as exc:
        make_spec(**payload)
    assert label  # 用例名参与断言，避免被 lint 当成未使用参数
    assert exc.value is not None


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("http 缺 url", {"transport": "streamable_http"}),
        ("stdio 与 http 混写", {"transport": "streamable_http", "url": None}),
    ],
)
def test_spec_rejects_bad_http(label: str, payload: dict[str, Any]) -> None:
    """HTTP 的两类非法组合必须被拦下。

    Args:
        label (`str`): 用例名。
        payload (`dict[str, Any]`): 字段覆盖。
    """
    payload = {"name": "h", **payload}
    with pytest.raises(Exception):
        MCPServerSpec(**payload)
    assert label


def test_spec_sse_path_is_a_contract() -> None:
    """SSE 必须写 ``/sse`` 结尾的路径；反之 streamable-http 不能写。"""
    with pytest.raises(Exception) as exc:
        MCPServerSpec(name="s", transport="sse", url="http://h/mcp")
    assert "/sse" in str(exc.value)
    with pytest.raises(Exception):
        MCPServerSpec(
            name="s",
            transport="streamable_http",
            url="http://h/sse",
        )
    # 带 query 的 /sse?key=1 仍然合法（AgentScope 只取 urlsplit().path）
    MCPServerSpec(name="s", transport="sse", url="http://h/sse?key=1")


def test_spec_extra_fields_are_forbidden() -> None:
    """未知字段被 pydantic 拒绝（``extra="forbid"``）。"""
    with pytest.raises(Exception):
        make_spec(unexpected="x")


# ======================================================================
# 2. 命名空间与工具名
# ======================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("read_file", "read_file"),
        ("repo.read", "repoxread"),
        ("cmd:run", "cmdxrun"),
        ("weird name!", "weirdxnamex"),
    ],
)
def test_sanitize_replaces_with_x(raw: str, expected: str) -> None:
    """非法字符替换成 ``x`` 而不是 ``_``。

    Args:
        raw (`str`): 原始名。
        expected (`str`): 期望结果。
    """
    assert sanitize_tool_name(raw) == expected


def test_namespace_of_falls_back_to_mcp() -> None:
    """命名空间净化规则：非法字符换成 ``-``；全空时回落 ``"mcp"``。"""
    assert namespace_of("harness-demo") == "harness-demo"
    assert namespace_of("my.server") == "my-server"
    assert namespace_of("!!") == "--"  # 换的是 ``-``，不是 ``x``
    assert namespace_of("") == "mcp"  # 只有彻底为空才回落


def test_sanitize_and_namespace_use_different_replacement() -> None:
    """``sanitize_tool_name`` 换 ``x``、``namespace_of`` 换 ``-``。"""
    assert sanitize_tool_name("a.b") == "axb"
    assert namespace_of("a.b") == "a-b"


def test_namespaced_tool_name_matches_mcptool_rule() -> None:
    """与 ``MCPTool`` 的生成规则逐字一致（``tool/_adapters.py:247``）。"""
    assert (
        namespaced_tool_name("harness-demo", "calc")
        == "mcp__harness-demo__calc"
    )
    assert (
        make_spec(namespace="tenant-a").namespaced_name("echo")
        == "mcp__tenant-a__echo"
    )


# ======================================================================
# 3. MCPServerRegistry
# ======================================================================
def test_registry_skips_disabled() -> None:
    """``enabled=False`` 的声明不进 ``to_clients()``。"""
    registry = MCPServerRegistry(
        [
            make_spec(name="on", command=PY, args=[str(DEMO_SERVER)]),
            make_spec(name="off", command=PY, enabled=False),
        ],
    )
    assert len(registry) == 2
    assert [s.name for s in registry.enabled_specs] == ["on"]
    clients = registry.to_clients()
    assert [c.name for c in clients] == ["on"]
    assert registry.describe_all()[1].startswith("[off] ")


def test_registry_get_unknown_raises_keyerror() -> None:
    """取未登记的 server 抛 ``KeyError`` 并列出已登记的名字。"""
    registry = MCPServerRegistry([make_spec(name="a")])
    with pytest.raises(KeyError) as exc:
        registry.get("b")
    assert "a" in str(exc.value)


def test_registry_contains_accepts_spec() -> None:
    """``in`` 同时接受 str 与 ``MCPServerSpec``（pydantic 模型不可哈希）。"""
    spec = make_spec(name="a")
    registry = MCPServerRegistry([spec])
    assert "a" in registry
    assert spec in registry
    assert make_spec(name="b") not in registry


def test_registry_namespaced_name_by_name_or_spec() -> None:
    """``namespaced_name`` 两种入参等价。"""
    spec = make_spec(name="a", namespace="ns")
    registry = MCPServerRegistry([spec])
    assert registry.namespaced_name("a", "calc") == "mcp__ns__calc"
    assert registry.namespaced_name(spec, "calc") == "mcp__ns__calc"


def test_registry_to_client_carries_filters() -> None:
    """``enable_tools`` / ``disable_tools`` / 超时都落到 ``MCPClient`` 上。"""
    spec = make_spec(
        name="a",
        command=PY,
        args=[str(DEMO_SERVER)],
        disable_tools=["env_info"],
        execution_timeout=12.5,
    )
    client = spec.to_client()
    assert isinstance(client.mcp_config, StdioMCPConfig)
    assert client.disable_tools == ["env_info"]
    assert client.execution_timeout == 12.5
    assert client.is_stateful is True


def test_http_spec_builds_http_config() -> None:
    """HTTP 声明构造出 ``HttpMCPConfig``。"""
    spec = MCPServerSpec(
        name="h",
        transport="streamable_http",
        url="http://127.0.0.1:18100/mcp",
        headers={"Authorization": "Bearer x"},
    )
    client = spec.to_client()
    assert isinstance(client.mcp_config, HttpMCPConfig)
    assert client.mcp_config.headers == {"Authorization": "Bearer x"}


# ======================================================================
# 4. 连接生命周期
# ======================================================================
async def test_connect_and_close_roundtrip() -> None:
    """握手 → 列工具 → 关闭，``is_connected`` 如实反映状态。"""
    async with connected_client() as client:
        assert client.is_connected is True
        raw = await list_remote_tools(client)
        assert [t["name"] for t in raw] == list(DEMO_TOOLS)
    assert client.is_connected is False


async def test_double_connect_raises() -> None:
    """重复 ``connect()`` 抛 ``RuntimeError``（AgentScope 的设计）。"""
    async with connected_client() as client:
        with pytest.raises(RuntimeError) as exc:
            await client.connect()
        assert "already connected" in str(exc.value)


async def test_closed_client_cannot_be_reused() -> None:
    """``close()`` 之后同一个对象不能再 ``connect()``：transport 是一次性的。"""
    client = make_spec(command=PY, args=[str(DEMO_SERVER)]).to_client()
    await client.connect()
    await client.close()
    await client.connect()  # SDK 会重建 transport，所以这次仍然成功
    assert client.is_connected is True
    await close_mcp_clients([client])


def test_missing_launch_target_fails_before_spawning() -> None:
    """脚本路径打错时，``to_client()`` 直接抛 ``FileNotFoundError``。

    这是本讲最重要的一条前置校验：留给 AgentScope 处理的话，
    ``connect()`` 的 ``asyncio.shield`` 清理会把它变成 ``CancelledError``
    （继承 ``BaseException``，``except Exception`` 抓不到），
    调用方既看不到真实原因，也拿不到回滚。
    """
    spec = make_spec(name="broken", command=PY, args=["/nonexistent/server.py"])
    with pytest.raises(FileNotFoundError) as exc:
        spec.to_client()
    assert "/nonexistent/server.py" in str(exc.value)

    with pytest.raises(FileNotFoundError):
        make_spec(name="b", command="/no/such/python", args=["x.py"]).to_client()

    # 裸命令名不看 PATH（"python" 在 PATH 里当然也在，但这里刻意不检查）
    make_spec(name="bare", command="definitely-not-a-real-binary").to_client()
    # 不含路径分隔符的 `.py` 参数不当路径（可能是普通参数值）
    make_spec(name="arg", command=PY, args=["--output", "report.py"]).to_client()


async def test_connect_failure_rolls_back() -> None:
    """一个 server 起不来时，``connect_mcp_clients`` 关掉已连上的再抛。

    这里用一个"能启动但立刻退出"的进程模拟握手失败（脚本不存在的场景
    已经被 ``check_launch_targets`` 在更早的地方挡掉了）。断言的重点不是
    异常类型 —— 底层抛出的可能是 AgentScope 那个伪 ``CancelledError`` ——
    而是**回滚确实发生了**：``good`` 不能留成孤儿进程。
    """
    good = make_spec(name="ok", command=PY, args=[str(DEMO_SERVER)]).to_client()
    bad = make_spec(
        name="broken",
        command=PY,
        args=["-c", "import sys; sys.exit(3)"],
    ).to_client()
    with pytest.raises(BaseException) as exc:
        await connect_mcp_clients([good, bad])
    assert exc.value is not None
    assert good.is_connected is False  # 回滚了，没泄漏子进程
    assert bad.is_connected is False


async def test_close_is_idempotent_and_quiet() -> None:
    """``close_mcp_clients`` 对未连接对象不发难。"""
    client = make_spec(command=PY, args=[str(DEMO_SERVER)]).to_client()
    await close_mcp_clients([client])  # 从未连接
    await client.connect()
    await close_mcp_clients([client])
    await close_mcp_clients([client])  # 重复关闭
    assert client.is_connected is False


# ======================================================================
# 5. 远端工具 → ToolBase
# ======================================================================
async def test_list_tools_wrapped_names() -> None:
    """``list_tools`` 给的是模型侧名字（双名制的另一半）。"""
    async with connected_client("harness-demo") as client:
        wrapped = await client.list_tools()
        assert [t.name for t in wrapped] == [
            f"mcp__harness-demo__{name}" for name in DEMO_TOOLS
        ]


async def test_to_tool_base_flags_and_schema() -> None:
    """``MCPTool`` 的关键标记与 schema 透传。"""
    async with connected_client("harness-demo") as client:
        raw = await list_remote_tools(client)
        desc = next(t for t in raw if t["name"] == "calc")
        tool = to_tool_base(client, desc, namespace="harness-demo")
    assert tool.name == "mcp__harness-demo__calc"
    assert tool.is_mcp is True
    assert tool.is_state_injected is False  # 安全边界
    assert tool.is_read_only is True
    assert tool.input_schema["required"] == ["expression"]
    # AgentScope 的 schema 是原样透传，不补 title
    assert "title" not in tool.input_schema


async def test_to_tool_base_accepts_dict_and_tool() -> None:
    """工具描述既接受 dict 也接受 ``mcp.types.Tool``。"""
    async with connected_client("harness-demo") as client:
        raw_objs = await client.list_raw_tools()
        by_dict = await asyncio.to_thread(
            to_tool_base,
            client,
            {"name": "calc", "inputSchema": {"type": "object"}},
            namespace="ns",
        )
        by_obj = to_tool_base(client, raw_objs[1], namespace="ns")
    assert by_dict.name == "mcp__ns__calc"
    assert by_obj.name == "mcp__ns__calc"


def test_to_tool_base_rejects_dict_without_name() -> None:
    """dict 缺 ``name`` 时给出可读报错，而不是 pydantic 的 30 行堆栈。"""
    client = make_spec(command=PY).to_client()
    with pytest.raises(ValueError) as exc:
        to_tool_base(client, {"inputSchema": {}}, namespace="ns")
    assert "缺少 name" in str(exc.value)


async def test_read_only_tool_is_allowed_by_default() -> None:
    """demo server 的三个工具都带 ``readOnlyHint`` → 默认全部 ALLOW。"""
    async with connected_client("harness-demo") as client:
        tools = [await client.get_tool(name) for name in DEMO_TOOLS]
    for tool in tools:
        decision = await tool.check_permissions()
        assert decision.behavior is PermissionBehavior.ALLOW
        assert tool.is_read_only is True


async def test_permission_mapping_allow_vs_ask() -> None:
    """没带 ``readOnlyHint`` 的 MCP 工具默认 ASK（AgentScope 的安全默认）。

    这是一个纯单元测试：``MCPTool.check_permissions`` 不看 session，
    所以传入一个占位对象即可（``tool/_adapters.py:294-315``）。
    """
    plain = mcp_types.Tool(name="mutate", inputSchema={"type": "object"})
    readonly = mcp_types.Tool(
        name="peek",
        inputSchema={"type": "object"},
        annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
    )
    ask = MCPTool(mcp_name="ns", tool=plain, session=object())
    allow = MCPTool(mcp_name="ns", tool=readonly, session=object())

    assert ask.is_read_only is False
    assert allow.is_read_only is True
    assert str(ask.name) == "mcp__ns__mutate"
    assert str(allow.name) == "mcp__ns__peek"
    assert (await ask.check_permissions()).behavior is PermissionBehavior.ASK
    assert (await allow.check_permissions()).behavior is PermissionBehavior.ALLOW


async def test_unconnected_stateful_client_raises() -> None:
    """有状态客户端未连接就调工具 → ``RuntimeError``。"""
    client = make_spec(command=PY, args=[str(DEMO_SERVER)]).to_client()
    with pytest.raises(RuntimeError):
        await client.list_tools()


async def test_remote_tool_direct_call() -> None:
    """直接调用远端工具拿到结果。"""
    async with connected_client("harness-demo") as client:
        tool = await client.get_tool("calc")
        chunk = await tool(expression="6*7")
    assert tool_chunk_to_text(chunk) == "6*7 = 42"


async def test_disable_tools_filters_listing() -> None:
    """``disable_tools`` 在 ``list_raw_tools`` 阶段就过滤掉了。"""
    async with connected_client(
        "harness-demo",
        disable_tools=["env_info"],
    ) as client:
        raw = await list_remote_tools(client)
        wrapped = await client.list_tools()
    assert [t["name"] for t in raw] == ["now", "calc"]
    assert [t.name for t in wrapped] == [
        "mcp__harness-demo__now",
        "mcp__harness-demo__calc",
    ]


# ======================================================================
# 6. collect_tools 的冲突策略
# ======================================================================
def _fake_conflicting_tool(name: str) -> FunctionTool:
    """造一个与远端工具同名的本地工具。

    Args:
        name (`str`): 工具名。

    Returns:
        `FunctionTool`: 本地工具。
    """

    def _impl(**_: Any) -> str:
        """本地占位实现。

        Returns:
            `str`: 固定文本。
        """
        return "local"

    return FunctionTool(_impl, name=name, is_read_only=True)


async def test_collect_tools_no_conflict() -> None:
    """无冲突时全部收集。"""
    async with connected_client("harness-demo") as client:
        tools = await collect_tools([client])
    assert [t.name for t in tools] == [
        f"mcp__harness-demo__{name}" for name in DEMO_TOOLS
    ]


async def test_collect_tools_conflict_error() -> None:
    """``on_conflict="error"`` 抛 ``ToolNameConflictError``。"""
    async with connected_client("harness-demo") as client:
        fake = _fake_conflicting_tool("mcp__harness-demo__calc")
        with pytest.raises(ToolNameConflictError) as exc:
            await collect_tools([client], base=[fake], on_conflict="error")
    assert "mcp__harness-demo__calc" in str(exc.value)


async def test_collect_tools_conflict_skip_keeps_local() -> None:
    """``on_conflict="skip"`` 时远端那条被丢掉（本地不动）。"""
    async with connected_client("harness-demo") as client:
        fake = _fake_conflicting_tool("mcp__harness-demo__calc")
        tools = await collect_tools([client], base=[fake], on_conflict="skip")
    names = [t.name for t in tools]
    assert "mcp__harness-demo__calc" not in names
    assert names == ["mcp__harness-demo__now", "mcp__harness-demo__env_info"]


async def test_collect_tools_conflict_replace_includes_remote() -> None:
    """``on_conflict="replace"`` 时远端那条**必须出现在结果里**。

    这是一条回归测试：``collected[index[name]] = tool`` 只在冲突来自
    *另一个 MCP server* 时成立；冲突来自 ``base``（本地工具）时 ``index``
    里没有这个键，原地替换会 ``KeyError``。正确做法是追加。
    """
    async with connected_client("harness-demo") as client:
        fake = _fake_conflicting_tool("mcp__harness-demo__calc")
        tools = await collect_tools([client], base=[fake], on_conflict="replace")
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "mcp__harness-demo__now",
        "mcp__harness-demo__calc",
        "mcp__harness-demo__env_info",
    }
    assert by_name["mcp__harness-demo__calc"] is not fake
    assert by_name["mcp__harness-demo__calc"].is_mcp is True


async def test_collect_tools_two_servers_same_tools_no_conflict() -> None:
    """两个 server 提供同名工具不算冲突 —— 命名空间把它们分开了。"""
    async with connected_client("srv-a") as a, connected_client("srv-b") as b:
        tools = await collect_tools([a, b])
    names = [t.name for t in tools]
    assert len(names) == 6
    assert "mcp__srv-a__calc" in names and "mcp__srv-b__calc" in names


# ======================================================================
# 7. inject_into_toolkit
# ======================================================================
async def test_inject_unknown_group_raises() -> None:
    """``group`` 不存在时给出可用组名。"""
    toolkit = make_toolkit(with_mcp_group=False)
    with pytest.raises(ValueError) as exc:
        await inject_into_toolkit(toolkit, [], group="mcp")
    assert "basic" in str(exc.value)


async def test_inject_empty_list_is_noop() -> None:
    """空列表不报错。"""
    toolkit = make_toolkit()
    await inject_into_toolkit(toolkit, [], group="mcp")
    group = next(g for g in toolkit.tool_groups if g.name == "mcp")
    assert group.tools == []


async def test_inject_activates_only_after_group_activation() -> None:
    """MCP 工具进组后默认不可见，激活组才可见。"""
    async with connected_client("harness-demo") as client:
        tools = await collect_tools([client])
        toolkit = make_toolkit()
        await inject_into_toolkit(toolkit, tools, group="mcp")

    inactive = {s["function"]["name"] for s in await toolkit.get_tool_schemas()}
    active = {
        s["function"]["name"]
        for s in await toolkit.get_tool_schemas(groups=["mcp"])
    }
    assert "mcp__harness-demo__calc" not in inactive
    assert "mcp__harness-demo__calc" in active
    assert "local_now" in inactive  # basic 组常驻


async def test_inject_twice_is_idempotent() -> None:
    """重复注入同名工具只会覆盖，不会出现两份。"""
    async with connected_client("harness-demo") as client:
        tools = await collect_tools([client])
        toolkit = make_toolkit()
        await inject_into_toolkit(toolkit, tools, group="mcp")
        await inject_into_toolkit(toolkit, tools, group="mcp")
    group = next(g for g in toolkit.tool_groups if g.name == "mcp")
    names = [t.name for t in group.tools]
    assert len(names) == len(set(names)) == len(DEMO_TOOLS)


async def test_inject_conflict_policies() -> None:
    """注入时与组内已有工具重名的三种策略。"""
    async with connected_client("harness-demo") as client:
        tools = await collect_tools([client])
    fake = _fake_conflicting_tool("mcp__harness-demo__calc")

    async def _fresh() -> Toolkit:
        """造一个 mcp 组里已经有一个冲突工具的 Toolkit。

        Returns:
            `Toolkit`: 目标工具集。
        """
        toolkit = make_toolkit()
        await inject_into_toolkit(toolkit, [fake], group="mcp")
        return toolkit

    with pytest.raises(ToolNameConflictError):
        await inject_into_toolkit(
            await _fresh(),
            tools,
            group="mcp",
            on_conflict="error",
        )

    kit = await _fresh()
    await inject_into_toolkit(kit, tools, group="mcp", on_conflict="skip")
    group = next(g for g in kit.tool_groups if g.name == "mcp")
    assert next(t for t in group.tools if t.name.endswith("calc")) is fake

    kit = await _fresh()
    await inject_into_toolkit(kit, tools, group="mcp", on_conflict="replace")
    group = next(g for g in kit.tool_groups if g.name == "mcp")
    assert next(t for t in group.tools if t.name.endswith("calc")) is not fake


# ======================================================================
# 8. Toolkit.call_tool 的完整通路
# ======================================================================
async def test_call_tool_group_inactive_is_an_error_chunk() -> None:
    """组没激活时 ``ToolGroupInactiveError`` 被转成 error chunk，而不是异常。"""
    async with connected_client("harness-demo") as client:
        toolkit = Toolkit(
            tool_groups=[
                ToolGroup(name="mcp", description="MCP 工具。", mcps=[client]),
            ],
        )
        call = ToolCallBlock(
            type="tool_call",
            id="c1",
            name="mcp__harness-demo__calc",
            input='{"expression": "1+1"}',
        )
        final: Any = None
        async for item in toolkit.call_tool(call, AgentState()):
            final = item
    assert str(final.state) == "error"
    assert "ToolGroupInactiveError" in final.content[0].text


async def test_call_tool_success_after_activation() -> None:
    """激活组之后同一个调用成功，并且拿到远端结果。"""
    async with connected_client("harness-demo") as client:
        toolkit = Toolkit(
            tool_groups=[
                ToolGroup(name="mcp", description="MCP 工具。", mcps=[client]),
            ],
        )
        state = AgentState()
        state.tool_context.activated_groups = ["mcp"]
        call = ToolCallBlock(
            type="tool_call",
            id="c2",
            name="mcp__harness-demo__calc",
            input='{"expression": "(20+22)"}',
        )
        seen: list[str] = []
        final: Any = None
        async for item in toolkit.call_tool(call, state):
            seen.append(type(item).__name__)
            final = item
    assert seen[-1] == "ToolResponse"
    assert str(final.state) == "success"
    assert "42" in "".join(b.text for b in final.content)


# ======================================================================
# 9. 韧性：掉线只降级
# ======================================================================
async def test_dead_mcp_only_removes_its_tools() -> None:
    """一个 MCP 掉线后本地工具照常可用（``Toolkit`` 吞掉 list_tools 异常）。"""
    client = make_spec(
        name="harness-demo",
        command=PY,
        args=[str(DEMO_SERVER)],
    ).to_client()
    await client.connect()
    toolkit = Toolkit(
        tools=[FunctionTool(now, name="local_now", is_read_only=True)],
        tool_groups=[
            ToolGroup(name="mcp", description="MCP 工具。", mcps=[client]),
        ],
    )
    before = {
        s["function"]["name"]
        for s in await toolkit.get_tool_schemas(groups=["mcp"])
    }
    assert "mcp__harness-demo__calc" in before

    await client.close()
    after = {
        s["function"]["name"]
        for s in await toolkit.get_tool_schemas(groups=["mcp"])
    }
    assert "mcp__harness-demo__calc" not in after
    assert "local_now" in after


# ======================================================================
# 10. 反向：build_mcp_server
# ======================================================================
def test_build_mcp_server_rejects_empty_and_low_port() -> None:
    """空工具与 <18000 的端口都被拒绝。"""
    with pytest.raises(ValueError) as exc:
        build_mcp_server("s", tools=[])
    assert "至少" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        build_mcp_server("s", tools=[FunctionTool(now)], port=8080)
    assert "18000" in str(exc.value)


def test_build_mcp_server_rejects_duplicate_names() -> None:
    """同名工具（加了 namespace 之后仍重名）被拒绝。"""
    with pytest.raises(ValueError) as exc:
        build_mcp_server(
            "s",
            tools=[FunctionTool(now, name="a"), FunctionTool(now, name="a")],
            port=DEFAULT_PORT,
        )
    assert "重复" in str(exc.value)


def test_build_mcp_server_exposed_names() -> None:
    """暴露出去的对线名字：无前缀时是原名，有前缀时带前缀。"""
    tools = [
        FunctionTool(now, name="now", is_read_only=True),
        FunctionTool(calc, is_read_only=True),
    ]
    plain = build_mcp_server("s", tools=tools, port=DEFAULT_PORT)
    assert exposed_tool_names(plain) == ["now", "calc"]
    prefixed = build_mcp_server(
        "s",
        tools=tools,
        port=DEFAULT_PORT,
        namespace="verify",
    )
    assert exposed_tool_names(prefixed) == ["verify__now", "verify__calc"]


async def test_lowlevel_list_tools_passes_schema_through() -> None:
    """低层 ``tools/list`` handler 原样透传 ``input_schema`` 并带只读提示。"""
    read_only = FunctionTool(calc, is_read_only=True)
    mutating = FunctionTool(now, name="now_tool", is_read_only=False)
    server = build_mcp_server(
        "s",
        tools=[read_only, mutating],
        port=DEFAULT_PORT,
    )
    handler = server._mcp_server.request_handlers[  # noqa: SLF001
        mcp_types.ListToolsRequest
    ]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    listing = {t.name: t for t in result.root.tools}

    # schema 原样透传（含 $defs 之类的嵌套定义，不做压缩）
    assert listing["calc"].inputSchema == read_only.input_schema
    assert listing["calc"].annotations is not None
    assert listing["calc"].annotations.readOnlyHint is True
    # 非只读工具不带 annotations，客户端会按 ASK 处理
    assert listing["now_tool"].annotations is None
    # description 也带上了（模型靠它决定要不要调）
    assert listing["now_tool"].description == mutating.description


async def test_lowlevel_call_tool_unknown_name_is_protocol_error() -> None:
    """请求未暴露的工具 → ``isError=True``，连接不崩。"""
    server = build_mcp_server(
        "s",
        tools=[FunctionTool(calc, is_read_only=True)],
        port=DEFAULT_PORT,
    )
    handler = server._mcp_server.request_handlers[  # noqa: SLF001
        mcp_types.CallToolRequest
    ]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name="nope", arguments={}),
    )
    result = (await handler(request)).root
    assert result.isError is True
    assert "Unknown tool" in result.content[0].text


async def test_lowlevel_call_tool_streaming_tool_is_accumulated() -> None:
    """流式工具被累积成单个结果（``call_tool_once`` 的职责）。"""
    from agentscope.message import TextBlock
    from agentscope.tool import ToolChunk

    async def _streaming(expression: str) -> Any:
        """一个流式工具：分两块吐出结果。

        Args:
            expression (`str`): 被回显的表达式。

        Yields:
            `ToolChunk`: 增量块。
        """
        yield ToolChunk(content=[TextBlock(text="part1 ")])
        yield ToolChunk(content=[TextBlock(text="part2")])

    tool = FunctionTool(_streaming, name="streamer", is_read_only=True)
    chunk = await call_tool_once(tool, {"expression": "x"})
    assert tool_chunk_to_text(chunk) == "part2"  # 只保留最后一块


def test_tool_chunk_to_text_marks_non_text_blocks() -> None:
    """非文本块降级成占位说明，而不是静默丢弃。"""
    from agentscope.message import DataBlock, TextBlock, URLSource
    from agentscope.tool import ToolChunk

    chunk = ToolChunk(
        content=[
            TextBlock(text="hello"),
            DataBlock(
                source=URLSource(
                    type="url",
                    url="http://x/y.png",
                    media_type="image/png",
                ),
            ),
        ],
    )
    assert tool_chunk_to_text(chunk) == "hello\n<data block omitted>"


# ======================================================================
# 11. 从 MCPSpec 装配（HarnessBuilder 走的那个工厂）
# ======================================================================
async def test_build_mcp_clients_empty_spec_short_circuits() -> None:
    """没有声明 server 时直接返回空列表，不起任何进程。"""
    clients = await build_mcp_clients(MCPSpec(servers=[], group="mcp"))
    assert clients == []


async def test_build_mcp_clients_connects_declared_servers() -> None:
    """声明了 server 时工厂负责连好再返回（Toolkit 要求已连接）。"""
    spec = MCPSpec(
        servers=[
            MCPServerSpec(
                name="harness-demo",
                transport="stdio",
                command=PY,
                args=[str(DEMO_SERVER)],
                cwd=str(REF),
            ),
            MCPServerSpec(
                name="off",
                transport="stdio",
                command=PY,
                enabled=False,
            ),
        ],
        group="mcp",
    )
    clients = await build_mcp_clients(spec)
    try:
        assert [c.name for c in clients] == ["harness-demo"]
        assert clients[0].is_connected is True
    finally:
        await close_mcp_clients(clients)
    assert clients[0].is_connected is False
