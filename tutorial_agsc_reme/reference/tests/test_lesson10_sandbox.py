# -*- coding: utf-8 -*-
"""第 10 讲的 pytest：路径护栏 / 策略 / 本地与容器后端 / offload / 装配。

三条纪律：

1. **0 次 LLM 调用**。需要"真 Agent Loop"的那条用
   :class:`~harness_kit.models.adapters.echo.EchoChatModel`（脚本驱动、
   确定性、离线）。真实模型那部分在 ``scripts/10_sandbox.py --live``。
2. **每条约束都要有"它真的拦住了"的反例**。``deny_paths`` / 越界 /
   前缀伪装 / 超时 / 截断，各自都有一个**必须失败**的断言 ——
   安全组件的失效模式是"静默放行"，只测正例等于没测。
3. **策略层与执行层分开测**。``SandboxPolicy`` / ``PathGuard`` /
   ``QuotaMixin.quotas_as_host_config`` 全是纯函数，不碰 IO、不连 daemon，
   所以它们各自有独立的单元测试；``PolicyLocalWorkspace`` /
   ``QuotaDockerWorkspace`` 才需要真的读写 / 真的起容器。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson10_sandbox.py -v

需要 Docker 的那条（``test_quota_docker_workspace_real_container``）在
Docker 不可用时**跳过**而不是失败 —— ``docker_available()`` 是环境事实，
不是本讲的契约。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.event import (
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.tool import Toolkit

from harness_kit.models.adapters.echo import EchoChatModel
from harness_kit.sandbox import (
    DEFAULT_DENY_PATHS,
    DockerUnavailableError,
    HarnessOffloader,
    PathEscapeError,
    PathGuard,
    PolicyLocalWorkspace,
    QuotaDockerWorkspace,
    SandboxPolicy,
    choose_workspace_kind,
    docker_available,
)

FAKE_SECRET = "sk-not-a-real-key-0123456789\n"


# ======================================================================
# 公共构造器
# ======================================================================
def make_policy(root: Path, **overrides: Any) -> SandboxPolicy:
    """造一份指向 ``root`` 的策略，其余字段可覆盖。

    Args:
        root (`Path`): 工作区根目录。
        **overrides (`Any`): 覆盖字段。

    Returns:
        `SandboxPolicy`: 策略实例。
    """
    return SandboxPolicy(workspace_root=root, **overrides)


async def make_workspace(root: Path, **overrides: Any) -> PolicyLocalWorkspace:
    """造一个已 ``initialize()`` 的策略化本地工作区。

    Args:
        root (`Path`): 工作区根目录。
        **overrides (`Any`): 透传给 :func:`make_policy`。

    Returns:
        `PolicyLocalWorkspace`: 可用工作区。
    """
    ws = PolicyLocalWorkspace(policy=make_policy(root, **overrides))
    await ws.initialize()
    return ws


def chunk_text(chunk: Any) -> str:
    """把 ``ToolChunk`` 里的文本块拼起来。

    Args:
        chunk (`Any`): ``ToolChunk``。

    Returns:
        `str`: 文本。
    """
    return "".join(b.text for b in chunk.content if isinstance(b, TextBlock))


async def auto_confirm_reply(agent: Agent, message: Any, *, max_rounds: int = 6) -> list[Any]:
    """跑 ``reply_stream`` 并在每次 ``RequireUserConfirmEvent`` 时自动确认。

    离线测试里也要走 HITL，是因为"策略拒绝"与"用户拒绝"走的是**同一条**
    工具结果通道（都变成 ``state=error`` 的 :class:`ToolResultBlock`）——
    跳过确认就等于跳过了这条通道。

    Args:
        agent (`Agent`): Agent。
        message (`Any`): 首次输入。
        max_rounds (`int`): 最多 park 几轮。

    Returns:
        `list[Any]`: 全部事件（按到达顺序）。
    """
    events: list[Any] = []
    pending_msg = message
    for _ in range(max_rounds):
        parked: RequireUserConfirmEvent | None = None
        async for item in agent.reply_stream(pending_msg):
            events.append(item)
            if isinstance(item, RequireUserConfirmEvent):
                parked = item
        if parked is None:
            return events
        pending_msg = UserConfirmResultEvent(
            reply_id=parked.reply_id,
            confirm_results=[
                ConfirmResult(confirmed=True, tool_call=tc) for tc in parked.tool_calls
            ],
        )
    return events


def block_output_text(block: ToolResultBlock) -> str:
    """取 :class:`ToolResultBlock` 的纯文本。

    ``output`` 的类型是 ``str | list[TextBlock | DataBlock]``：自己调工具时
    通常是 ``str``，经 Agent 上下文回灌后是块列表 —— 两种都要吃得下。

    Args:
        block (`ToolResultBlock`): 工具结果块。

    Returns:
        `str`: 纯文本。
    """
    if isinstance(block.output, str):
        return block.output
    return "".join(b.text for b in block.output if isinstance(b, TextBlock))


def tool_result_blocks(agent: Agent) -> list[ToolResultBlock]:
    """从 ``agent.state.context`` 里捞出全部工具结果块。

    Args:
        agent (`Agent`): Agent。

    Returns:
        `list[ToolResultBlock]`: 工具结果块。
    """
    out: list[ToolResultBlock] = []
    for msg in agent.state.context:
        for block in msg.content if not isinstance(msg.content, str) else []:
            if isinstance(block, ToolResultBlock):
                out.append(block)
    return out


# ======================================================================
# 1 · PathGuard：路径护栏（纯函数）
# ======================================================================
def test_guard_resolves_symlinks_before_comparing(tmp_path: Path) -> None:
    """``PathGuard.root`` 构造即 ``realpath``：符号链接指向的也算"在内"。"""
    root = tmp_path / "ws"
    root.mkdir()
    guard = PathGuard(root)
    assert guard.root == Path(os.path.realpath(str(root)))

    link = tmp_path / "alias"
    link.symlink_to(root)
    assert guard.is_within(link / "a.py") is True
    assert guard.is_within(root / "a.py") is True
    assert guard.is_within(root) is True


def test_guard_rejects_symlink_escape_that_startswith_would_allow(tmp_path: Path) -> None:
    """工作区里一个指向 ``/etc`` 的软链接，字符串前缀实现会放行、本研究必须拦住。"""
    root = tmp_path / "ws"
    root.mkdir()
    escape = root / "escape"
    escape.symlink_to("/etc")
    guard = PathGuard(root)

    assert str(escape / "passwd").startswith(str(root)) is True  # 朴素实现的判断结果
    assert guard.is_within(escape / "passwd") is False
    with pytest.raises(PathEscapeError):
        guard.resolve_within(escape / "passwd")


def test_guard_rejects_prefix_spoofing_and_parent_traversal(tmp_path: Path) -> None:
    """``<ws>-evil`` 与 ``../..`` 都不在 ``<ws>`` 之内。"""
    root = tmp_path / "ws"
    root.mkdir()
    guard = PathGuard(root)

    assert guard.is_within(Path(str(root) + "-evil") / "a.py") is False
    assert guard.is_within(root / "../../etc/passwd") is False
    assert guard.is_within("/etc/passwd") is False
    assert guard.resolve_within(root / "notes" / ".." / "a.txt") == guard.root / "a.txt"
    assert guard.relative_to_root(root / "src" / "a.py") == "src/a.py"


@pytest.mark.parametrize(
    ("pattern", "rel", "should_hit"),
    [
        ("src/*.py", "src/a.py", True),
        ("*.pem", "a/b/k.pem", True),
        (".git/", "src/.git/config", True),
        ("**/*.pem", "k.pem", True),
        ("secrets/*", "a/secrets/x.txt", False),
    ],
)
def test_guard_matches_patterns_four_rules(
    tmp_path: Path,
    pattern: str,
    rel: str,
    should_hit: bool,
) -> None:
    """四条匹配规则各自命中，反例不命中。"""
    root = tmp_path / "ws"
    root.mkdir()
    guard = PathGuard(root)
    hit = guard.matches_patterns(root / rel, [pattern])
    if should_hit:
        assert hit == pattern
    else:
        assert hit is None


def test_guard_skips_empty_pattern(tmp_path: Path) -> None:
    """空模式不会被当成"匹配一切"。"""
    root = tmp_path / "ws"
    root.mkdir()
    guard = PathGuard(root)
    assert guard.matches_patterns(root / "a.txt", ["", "   "]) is None


# ======================================================================
# 2 · SandboxPolicy：策略模型（纯函数）
# ======================================================================
def test_policy_default_deny_paths_and_deny_beats_allow(tmp_path: Path) -> None:
    """默认黑名单就是契约那三条；黑名单优先于"工作区内默认可写"。"""
    root = tmp_path / "ws"
    root.mkdir()
    policy = make_policy(root)

    assert policy.deny_paths == DEFAULT_DENY_PATHS
    assert policy.is_writable(root / "notes" / "a.md") is True
    assert policy.is_writable(root / ".env") is False
    assert policy.is_writable(root / ".git" / "config") is False
    assert policy.is_readable(root / "k.pem") is False
    assert policy.deny_hit(root / ".env") == ".env"
    assert policy.is_readable("/etc/passwd") is False
    assert policy.containing_root("/etc/passwd", "read") is None


def test_policy_extra_roots_resolve_relative_to_workspace_root(tmp_path: Path) -> None:
    """``read_paths`` 里的 ``./shared`` 指工作区里的 ``shared``，不是进程 cwd。"""
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shared.txt").write_text("x\n", encoding="utf-8")

    policy = make_policy(root, read_paths=[str(outside), "./shared"], write_paths=["./out"])
    assert policy.extra_roots("read") == [outside, root / "shared"]
    assert policy.extra_roots("write") == [root / "out"]
    assert policy.is_readable(outside / "shared.txt") is True
    assert policy.containing_root(outside / "shared.txt", "read") == Path(
        os.path.realpath(str(outside)),
    )
    # 白名单之外仍然拒
    assert policy.is_readable(tmp_path / "nope.txt") is False


def test_policy_fail_closed_validations(tmp_path: Path) -> None:
    """三条加载期校验：空 allowlist / cpu<=0 / 拼错字段名。"""
    root = tmp_path / "ws"
    root.mkdir()

    with pytest.raises(ValueError):
        make_policy(root, network="allowlist")
    with pytest.raises(ValueError):
        make_policy(root, cpu=0)
    with pytest.raises(ValueError):
        make_policy(root, memmory_mb=512)

    # 给了白名单就通过
    ok = make_policy(root, network="allowlist", network_allowlist=["pypi.org"])
    assert ok.network_allowlist == ["pypi.org"]


def test_policy_network_enforceable_only_for_full(tmp_path: Path) -> None:
    """本地后端只对 ``full`` 说"可执行"；``none`` / ``allowlist`` 都会告警。"""
    root = tmp_path / "ws"
    root.mkdir()
    assert make_policy(root, network="full").is_network_enforceable() is True
    assert make_policy(root, network="none").is_network_enforceable() is False
    assert (
        make_policy(root, network="allowlist", network_allowlist=["a.com"]).is_network_enforceable()
        is False
    )


# ======================================================================
# 3 · PolicyLocalWorkspace：本地执行
# ======================================================================
async def test_local_workspace_reads_writes_inside_and_rejects_outside(tmp_path: Path) -> None:
    """工作区内读写正常；越界 / 黑名单 / 相对穿越全部 ``PathEscapeError``。"""
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")

    ws = await make_workspace(root)
    try:
        await ws.write_file("notes/hello.txt", "第一行\n第二行\n")
        assert await ws.read_file("notes/hello.txt") == "第一行\n第二行\n"

        with pytest.raises(PathEscapeError):
            await ws.read_file("/etc/passwd")
        with pytest.raises(PathEscapeError):
            await ws.read_file("../../etc/passwd")
        with pytest.raises(PathEscapeError):
            await ws.read_file(str(outside / "secret.txt"))
        with pytest.raises(PathEscapeError):
            await ws.write_file(".env", FAKE_SECRET)
        with pytest.raises(PathEscapeError):
            await ws.write_file("k.pem", "PEM\n")
        with pytest.raises(PathEscapeError):
            await ws.write_file(".git/config", "x\n")
        with pytest.raises(PathEscapeError):
            await ws.run_command("pwd", cwd="/etc")

        assert not Path("/etc/harness10_probe").exists()
        assert ws.check_readable("notes/hello.txt") == root / "notes" / "hello.txt"
        with pytest.raises(PathEscapeError):
            ws.check_writable("/etc/harness10_probe")
    finally:
        await ws.aclose()
    assert ws.is_alive is False


async def test_local_workspace_run_command_defaults_cwd_to_workdir(tmp_path: Path) -> None:
    """``run_command`` 不给 ``cwd`` 时跑在 ``workdir`` 里，相对路径落在工作区内。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root)
    try:
        await ws.write_file("notes/hello.txt", "x\n")
        # 命令输出自带换行，比较前先 strip
        assert Path((await ws.run_command("pwd")).strip()).resolve() == Path(ws.workdir).resolve()
        assert (await ws.run_command("ls notes")).strip() == "hello.txt"
    finally:
        await ws.aclose()


async def test_local_workspace_run_command_clamps_timeout(tmp_path: Path) -> None:
    """策略的 ``timeout_s`` 真的生效：``sleep 30`` 在 2 秒内返回 ``timed out``。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root, timeout_s=2)
    try:
        import time

        started = time.monotonic()
        text = await ws.run_command("sleep 30")
        elapsed = time.monotonic() - started
        assert "timed out" in text
        assert elapsed < 10, f"超时没生效，耗时 {elapsed:.2f}s"
    finally:
        await ws.aclose()


async def test_local_workspace_truncates_output_with_marker(tmp_path: Path) -> None:
    """``max_output_bytes`` 截断并留下可读标记（不是静默丢数据）。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root, max_output_bytes=200)
    try:
        text = await ws.run_command("seq 1 200")
        assert "harness_kit 沙箱已截断" in text
        assert len(text) < 400
    finally:
        await ws.aclose()


async def test_local_workspace_constructor_rejects_workdir_outside_root(tmp_path: Path) -> None:
    """``workdir ∉ policy.workspace_root`` 在**构造期**就炸。"""
    from agentscope.workspace import LocalWorkspace

    root = tmp_path / "ws"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(PathEscapeError):
        PolicyLocalWorkspace(
            policy=make_policy(root),
            base_workspace=LocalWorkspace(workdir=str(other)),
        )


# ======================================================================
# 4 · 内置工具共用同一个策略化 backend
# ======================================================================
async def test_builtin_tools_use_the_policy_backend(tmp_path: Path) -> None:
    """``list_tools()`` 造出来的工具持有的是同一个 :class:`PolicyBackend`。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root, timeout_s=5, max_output_bytes=512)
    try:
        tools = {tool.name: tool for tool in await ws.list_tools()}
        assert "Write" in tools and "Read" in tools and "Bash" in tools
        assert tools["Write"]._backend is ws.get_backend()  # noqa: SLF001
        assert tools["Bash"]._backend is ws.get_backend()  # noqa: SLF001
    finally:
        await ws.aclose()


async def test_write_tool_outside_workspace_raises_and_writes_nothing(tmp_path: Path) -> None:
    """内置 ``Write`` 走策略：越界时异常逃出工具，文件根本不会被创建。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root)
    try:
        tools = {tool.name: tool for tool in await ws.list_tools()}
        chunk = await tools["Write"](
            file_path=str(root / "notes" / "ok.txt"),
            content="hi\n",
        )
        assert "has been written successfully" in chunk_text(chunk)

        with pytest.raises(PathEscapeError):
            await tools["Write"](file_path="/etc/harness10_probe.txt", content="nope\n")
        assert not Path("/etc/harness10_probe.txt").exists()

        with pytest.raises(PathEscapeError):
            await tools["Read"](file_path="/etc/passwd")
    finally:
        await ws.aclose()


async def test_bash_tool_gets_workdir_and_policy_limits(tmp_path: Path) -> None:
    """``Bash`` 的 ``cwd`` 是工作区；输出截断用的是策略上限而不是工具的 30000 字符。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root, max_output_bytes=512)
    try:
        bash = {tool.name: tool for tool in await ws.list_tools()}["Bash"]
        assert Path(bash._cwd).resolve() == Path(ws.workdir).resolve()  # noqa: SLF001
        chunks = [c async for c in await bash(command="pwd")]
        assert Path(chunk_text(chunks[0]).strip()).resolve() == Path(ws.workdir).resolve()

        big = [c async for c in await bash(command="seq 1 5000")]
        text = "".join(chunk_text(c) for c in big)
        assert "harness_kit 沙箱已截断" in text
        assert len(text) < 1200  # 远小于工具自己的 30000 字符上限
    finally:
        await ws.aclose()


# ======================================================================
# 5 · HarnessOffloader
# ======================================================================
async def test_offloader_data_block_persists_and_dedups(tmp_path: Path) -> None:
    """base64 块落盘成 ``workspace://`` URL；同一个块第二次不重复写。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root)
    try:
        offloader = HarnessOffloader(workspace=ws)
        block = DataBlock(
            source=Base64Source(
                type="base64",
                media_type="image/png",
                data=base64.b64encode(b"\x89PNG" + b"x" * 20000).decode(),
            ),
        )
        saved = await offloader.offload_data_block(block)
        assert str(saved.source.url).startswith("workspace:///data/")
        again = await offloader.offload_data_block(block)
        assert str(again.source.url) == str(saved.source.url)
        assert len(list((root / "data").iterdir())) == 1
        assert offloader.stats()["data_block_offloaded"] == 2
    finally:
        await ws.aclose()


async def test_offloader_context_and_tool_result_land_on_disk(tmp_path: Path) -> None:
    """``offload_context`` 写 ``context.jsonl``；``offload_tool_result`` 重名加后缀。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root)
    try:
        offloader = HarnessOffloader(workspace=ws)
        msgs = [
            Msg(name="user", role="user", content=[TextBlock(text=f"历史 {i}")])
            for i in range(3)
        ]
        path = await offloader.offload_context("s1", msgs)
        assert Path(path).name == "context.jsonl"
        assert Path(path).read_text(encoding="utf-8").count("\n") == 3

        tr = ToolResultBlock(id="call_1", name="Bash", output="y" * 100)
        first = await offloader.offload_tool_result("s1", tr)
        second = await offloader.offload_tool_result("s1", tr)
        assert Path(first).name == "tool_result-call_1.txt"
        assert Path(second).name == "tool_result-call_1(1).txt"
        assert Path(first).exists() and Path(second).exists()
    finally:
        await ws.aclose()


async def test_offloader_without_workspace_degrades_explicitly(tmp_path: Path) -> None:
    """没有工作区时**明确**回报未 offload，且计数可查。"""
    offloader = HarnessOffloader(workspace=None)
    msgs = [Msg(name="user", role="user", content=[TextBlock(text="x")])]
    assert await offloader.offload_context("s", msgs) == (
        "<offload-skipped: no workspace available>"
    )
    tr = ToolResultBlock(id="c", name="Bash", output="x")
    assert await offloader.offload_tool_result("s", tr) == (
        "<offload-skipped: no workspace available>"
    )
    assert offloader.stats()["skipped_no_workspace"] == 2
    assert offloader.stats()["context_offloaded"] == 0


async def test_offloader_refuses_oversize_and_keeps_inline(tmp_path: Path) -> None:
    """超过 ``max_offload_bytes`` 时拒绝落盘，返回明确的拒绝标注。"""
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root)
    try:
        offloader = HarnessOffloader(workspace=ws, max_offload_bytes=1024)
        msgs = [Msg(name="user", role="user", content=[TextBlock(text="z" * 500)])] * 20
        result = await offloader.offload_context("s", msgs)
        assert result.startswith("<offload-refused:")
        assert offloader.stats()["refused_too_large"] == 1
        assert not (root / "sessions").exists() or not any(
            (root / "sessions").rglob("context.jsonl"),
        )
        with pytest.raises(ValueError):
            HarnessOffloader(workspace=ws, max_offload_bytes=0)
    finally:
        await ws.aclose()


# ======================================================================
# 6 · 后端选择与降级
# ======================================================================
@pytest.mark.parametrize(
    ("requested", "kwargs", "expected"),
    [
        ("local", {}, "local"),
        ("docker", {"docker_ok": True}, "docker"),
        ("docker", {"docker_ok": False}, "local"),
        ("docker", {"docker_ok": None}, "local"),
        ("e2b", {"e2b_ok": True}, "e2b"),
        ("e2b", {"docker_ok": True}, "docker"),
        ("e2b", {"docker_ok": False, "e2b_ok": False}, "local"),
    ],
)
def test_choose_workspace_kind_matrix(
    requested: str,
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    """降级矩阵逐格对上（``None`` 表示没探测 → 当不可用处理，fail closed）。"""
    assert choose_workspace_kind(requested, **kwargs) == expected


def test_choose_workspace_kind_rejects_unknown_backend() -> None:
    """拼错的后端名必须报错，绝不静默降级。"""
    with pytest.raises(ValueError):
        choose_workspace_kind("dokcer")


async def test_docker_available_returns_bool() -> None:
    """``docker_available()`` 只返回布尔，不抛异常（探测就该吞掉一切）。"""
    assert isinstance(await docker_available(), bool)


# ======================================================================
# 7 · QuotaMixin：策略 -> HostConfig（纯函数，不连 daemon）
# ======================================================================
class _QuotaOnly(QuotaDockerWorkspace):
    """只带 ``policy`` 的壳，用来单测 mixin 的两个纯函数。"""

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy


def test_quota_host_config_mapping(tmp_path: Path) -> None:
    """CPU / 内存 / swap / pids / 网络五项映射逐项对上。"""
    root = tmp_path / "ws"
    root.mkdir()
    policy = make_policy(root, cpu=1.5, memory_mb=512, pids=64, network="none")
    host_config = _QuotaOnly(policy).quotas_as_host_config()

    assert host_config == {
        "NanoCpus": 1_500_000_000,
        "Memory": 512 * 1024 * 1024,
        "MemorySwap": 512 * 1024 * 1024,
        "PidsLimit": 64,
        "NetworkMode": "none",
    }


def test_quota_allowlist_degrades_to_no_network(tmp_path: Path) -> None:
    """``allowlist`` 表达不了 → 降级为 ``none``；``full`` 不写 ``NetworkMode``。"""
    root = tmp_path / "ws"
    root.mkdir()
    allow = make_policy(root, network="allowlist", network_allowlist=["pypi.org"])
    assert _QuotaOnly(allow).quotas_as_host_config()["NetworkMode"] == "none"

    full = make_policy(root, network="full")
    assert "NetworkMode" not in _QuotaOnly(full).quotas_as_host_config()


def test_quota_apply_preserves_existing_config_and_type_guards(tmp_path: Path) -> None:
    """配额并进去、原有键保留；非 dict 输入报 ``TypeError``。"""
    root = tmp_path / "ws"
    root.mkdir()
    shell = _QuotaOnly(make_policy(root, cpu=2.0, network="none"))

    config: dict[str, Any] = {"Image": "python:3.11-slim", "HostConfig": {"Binds": ["a:b"]}}
    merged = shell.apply_quotas_to_config(config)
    assert merged is config
    assert merged["HostConfig"]["Binds"] == ["a:b"]
    assert merged["HostConfig"]["NanoCpus"] == 2_000_000_000

    with pytest.raises(TypeError):
        shell.apply_quotas_to_config(["not", "a", "dict"])  # type: ignore[arg-type]


# ======================================================================
# 8 · QuotaDockerWorkspace：真容器（Docker 不可用时跳过）
# ======================================================================
async def test_quota_docker_workspace_real_container(tmp_path: Path) -> None:
    """真起容器并核对 cgroup 配额；Docker 不可用时验证的是"明确报错"。"""
    root = tmp_path / "ws"
    root.mkdir()
    policy = make_policy(
        root,
        cpu=0.5,
        memory_mb=384,
        pids=64,
        network="none",
        timeout_s=30,
    )
    ws = QuotaDockerWorkspace(policy=policy)

    if not await docker_available():
        with pytest.raises(DockerUnavailableError):
            await ws.start()
        pytest.skip("本机 Docker 不可用：已改为验证 DockerUnavailableError 这条降级路径")

    await ws.start()
    try:
        assert (await ws.run_command("cat /sys/fs/cgroup/memory.max")).strip() == str(
            384 * 1024 * 1024,
        )
        assert (await ws.run_command("cat /sys/fs/cgroup/cpu.max")).strip() == "50000 100000"
        assert (await ws.run_command("cat /sys/fs/cgroup/pids.max")).strip() == "64"

        net = await ws.run_command(
            "python -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\" 2>&1 | tail -1",
        )
        assert "Network is unreachable" in net

        # 容器模式的路径策略：/workspace 之内仍查 deny_paths
        await ws.write_file("/workspace/notes/a.txt", "ok\n")
        assert await ws.read_file("/workspace/notes/a.txt") == "ok\n"
        with pytest.raises(PathEscapeError):
            await ws.write_file("/workspace/.env", FAKE_SECRET)
        # 容器内工作目录之外放行 —— 容器本身就是边界
        assert (await ws.read_file("/etc/hostname")).strip() != ""
    finally:
        await ws.aclose()
    assert ws.is_alive is False


# ======================================================================
# 9 · 装配层：registry + builder
# ======================================================================
def test_registry_has_lesson10_lazy_entries() -> None:
    """``policy_local`` / ``quota_docker`` 是第 10 讲的懒加载条目。"""
    from harness_kit.registry import HarnessRegistry

    registry = HarnessRegistry.default()
    assert registry.try_get("workspace", "policy_local") is not None
    assert registry.try_get("workspace", "quota_docker") is not None
    assert registry.try_get("workspace", "第 10 讲不存在的东西") is None


async def test_builder_wires_policy_local_from_profile(tmp_path: Path) -> None:
    """``workspace.kind: local`` + ``policy:`` → 真的装配出 ``PolicyLocalWorkspace``。"""
    from harness_kit.config import load_resolved_profile
    from harness_kit.config.builder import HarnessBuilder
    from harness_kit.settings import Settings

    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "p.yaml").write_text(
        "\n".join(
            [
                "name: pytest_sandbox",
                "model:",
                "  provider: deepseek",
                "  model_name: deepseek-chat",
                "  api_key_env: OPENAI_API_KEY",
                "  base_url_env: OPENAI_BASE_URL",
                "workspace:",
                "  kind: local",
                f"  root: {root}",
                "  policy:",
                "    network: none",
                "    timeout_s: 15",
                "agent:",
                "  name: pytest-sandbox-agent",
                '  sys_prompt: "x"',
                "",
            ],
        ),
        encoding="utf-8",
    )
    profile = load_resolved_profile(tmp_path / "p.yaml", search_dir=tmp_path)
    # YAML 里没写 workspace_root，由 WorkspaceSpec._fill_policy_root 从 root 回填
    assert profile.workspace.policy is not None
    assert str(profile.workspace.policy.workspace_root) == str(root)

    builder = HarnessBuilder(profile, settings=Settings.from_env())
    try:
        ws = await builder.build_workspace()
        assert isinstance(ws, PolicyLocalWorkspace)
        assert ws.policy.timeout_s == 15
        assert ws.policy.network == "none"

        await ws.initialize()
        await ws.write_file("from_profile.txt", "ok\n")
        assert await ws.read_file("from_profile.txt") == "ok\n"
        with pytest.raises(PathEscapeError):
            await ws.read_file("/etc/passwd")
    finally:
        await builder.aclose()


# ======================================================================
# 10 · 端到端：策略化工作区挂进真 Agent Loop（离线）
# ======================================================================
async def test_policy_workspace_inside_real_agent_loop(tmp_path: Path) -> None:
    """离线 Agent 跑三步：只读 Bash 自动放行、``Write`` 要 HITL 确认、越界 ``Read`` 变 error。

    三个工具走的**不是**同一条路，正好一次看清 AgentScope 的权限分流：

    - ``Bash("pwd && echo marker")`` —— 引擎的 DEFAULT 模式对已知只读命令
      （``_bash.py:204`` 的 ``check_permissions`` 第 1 条）**自动放行**，
      所以这里不会 park；
    - ``Write`` —— 非只读，走到 DEFAULT 的兜底 ASK，于是 park 到
      ``RequireUserConfirmEvent``；
    - ``Read("/etc/passwd")`` —— 只读快路径放行，然后**被第 10 讲的路径策略**
      在 backend 层拒掉，通道是 ``state=error`` 的工具结果，而不是异常上抛。
    """
    root = tmp_path / "ws"
    root.mkdir()
    ws = await make_workspace(root, timeout_s=10)
    try:
        model = EchoChatModel(
            stream=False,
            script=[
                {
                    "text": "先看一眼工作区。",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "name": "Bash",
                            "input": {"command": "pwd && echo marker"},
                        },
                    ],
                },
                {
                    "text": "再写一个文件。",
                    "tool_calls": [
                        {
                            "id": "c2",
                            "name": "Write",
                            "input": {
                                "file_path": str(root / "notes" / "by_agent.txt"),
                                "content": "written-by-agent\n",
                            },
                        },
                    ],
                },
                {
                    "text": "再读一个越界文件。",
                    "tool_calls": [
                        {"id": "c3", "name": "Read", "input": {"file_path": "/etc/passwd"}},
                    ],
                },
                {"text": "到此为止。"},
            ],
        )
        agent = Agent(
            name="pytest-sandboxed",
            system_prompt="你是一个助手。",
            model=model,
            toolkit=Toolkit(tools=await ws.list_tools()),
            offloader=HarnessOffloader(workspace=ws),
            react_config=ReActConfig(max_iters=8),
        )
        events = await auto_confirm_reply(agent, UserMsg("user", "pwd 一下、写个文件、再读 /etc/passwd"))

        parked = [e for e in events if isinstance(e, RequireUserConfirmEvent)]
        # 只读的 Bash 自动放行，只有 Write 需要确认
        assert [tc.name for e in parked for tc in e.tool_calls] == ["Write"]

        blocks = tool_result_blocks(agent)
        assert len(blocks) == 3
        # 1) Bash 的 pwd 落在工作区内
        first_line = block_output_text(blocks[0]).strip().splitlines()[0]
        assert Path(first_line).resolve() == Path(ws.workdir).resolve()
        # 2) Write 真的落盘了
        assert blocks[1].state == "success"
        assert (root / "notes" / "by_agent.txt").read_text(encoding="utf-8") == (
            "written-by-agent\n"
        )
        # 3) 越界读被第 10 讲的策略拒绝：走 state=error，不是异常上抛
        assert blocks[2].state == "error"
        assert "沙箱策略拒绝" in block_output_text(blocks[2])
        assert Path("/etc/passwd").exists()  # 宿主上这文件当然还在，只是 agent 读不到
    finally:
        await ws.aclose()
