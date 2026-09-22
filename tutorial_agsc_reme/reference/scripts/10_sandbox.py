# -*- coding: utf-8 -*-
"""第 10 讲验证脚本：Workspace 与安全沙箱（``harness_kit/sandbox/``）。

它把本讲的主结论全部变成可执行的断言 / 可观察的输出：

  A. **``PathGuard``**：先 ``realpath`` 再比较 —— macOS 的 ``/tmp`` →
     ``/private/tmp``、符号链接逃逸（``<ws>/escape -> /etc``）、
     前缀伪装（``ws-evil`` 不以 ``ws`` 为界）、``..`` 穿越、
     ``matches_patterns`` 的四条匹配规则。
  B. **``SandboxPolicy``**：白名单 + 黑名单的判定顺序、``deny_paths`` 默认值、
     fail-closed 校验（``network=allowlist`` 却给空白名单、``cpu=0``、
     ``extra="forbid"``）、``is_network_enforceable`` 与明确降级告警。
  C. **``PolicyLocalWorkspace``**：相对路径按 ``workdir`` 解析、越界 / 黑名单 /
     ``..`` 穿越全部被拒；``workdir ∉ workspace_root`` 在**构造期**就炸；
     ``timeout_s`` 真的压住 ``sleep``；``max_output_bytes`` 真的截断。
  D. **内置工具也过策略**：``await ws.list_tools()`` 拿到的 ``Write`` / ``Read`` /
     ``Bash`` 共用同一个 ``PolicyBackend``，所以越界写、越界读、超时、超大输出
     四种情况在**工具层**就被拦住 —— 这是"策略打在 ``BackendBase`` 边界"的直接证据。
  E. **``HarnessOffloader``**：三个协议方法真的落盘；没有工作区时**明确降级**
     而不是假装成功；超过 ``max_offload_bytes`` 时拒绝落盘并保持内联。
  F. **后端选择与降级**：``choose_workspace_kind`` 的四个分支 + 真实探测
     ``docker_available()``；拼错的后端名直接 ``ValueError``（不静默降级）。
  G. **``QuotaMixin`` 纯函数**：``quotas_as_host_config()`` /
     ``apply_quotas_to_config()``，不连 daemon 也能真跑真断言。
  H. **``QuotaDockerWorkspace``**：真实起容器，实测
     ``/sys/fs/cgroup/memory.max``、``cpu.max``、``pids.max`` 与"网络不可达"。
     Docker 不可用时这一段退化为断言 ``DockerUnavailableError``。
  I. **从 Profile 装配**：``workspace.kind: local`` + ``policy:`` 经
     ``HarnessBuilder.build_workspace()`` 变成真实的 ``PolicyLocalWorkspace``。
  J.（需要 key，``--live`` 打开）真实 deepseek-flash 驱动 Agent 在笼子里干活：
     写文件、跑命令、越界读被拦、HITL 自动确认（**3~6 次 LLM 调用**）。

用法（``PYTHONPATH`` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/10_sandbox.py

    加 ``--live`` 才会跑 J 段（真实 LLM）。

LLM 调用预算：A~I 段 **0 次**；J 段 **3~6 次**。
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

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

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.event import (  # noqa: E402
    ConfirmResult,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from agentscope.message import (  # noqa: E402
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolResultBlock,
    URLSource,
    UserMsg,
)
from agentscope.tool import Toolkit  # noqa: E402

from harness_kit.config import HarnessBuilder, load_resolved_profile  # noqa: E402
from harness_kit.config.schema import ModelSpec  # noqa: E402
from harness_kit.models.factory import build_chat_model  # noqa: E402
from harness_kit.sandbox import (  # noqa: E402
    CONTAINER_WORKDIR,
    DEFAULT_DENY_PATHS,
    DEFAULT_MAX_OFFLOAD_BYTES,
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
from harness_kit.settings import Settings  # noqa: E402

LIVE: bool = "--live" in sys.argv

#: 本脚本所有临时文件的根。**故意钉在 ``/tmp``** —— 在 macOS 上它是
#: ``/private/tmp`` 的符号链接，第 10 讲最想让你亲眼看到的坑就在这里。
#: （不写 ``dir=`` 的话 ``tempfile`` 会听 ``$TMPDIR``，在 macOS 上落到
#: ``/var/folders/...``，那个路径没有符号链接，就看不到这个坑。）
_TMP_DIR: str | None = "/tmp" if os.path.isdir("/tmp") else None
SCRATCH: Path = Path(tempfile.mkdtemp(prefix="lesson10_", dir=_TMP_DIR))

#: 一个形状合法的假凭据（绝不是真 key），用来演示 ``deny_paths`` 拦 ``.env``。
FAKE_SECRET: str = "sk-not-a-real-key-0123456789\n"


# ======================================================================
# 小工具
# ======================================================================
def banner(title: str) -> None:
    """打一条分节横幅。

    Args:
        title (`str`): 节标题。
    """
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def show(label: str, value: Any) -> None:
    """打印一行 ``label = value``。

    Args:
        label (`str`): 标签。
        value (`Any`): 值。
    """
    print(f"  {label:38s} = {value}")


def chunk_text(chunk: Any) -> str:
    """把 :class:`~agentscope.tool.ToolChunk` 里的文本块拼成字符串。

    **注意**：块是 :class:`~agentscope.message.TextBlock`，取文本用 ``.text``；
    只有 :class:`~agentscope.message.Msg` 才有 ``get_text_content()``。

    Args:
        chunk (`Any`): ``ToolChunk``。

    Returns:
        `str`: 拼接后的文本。
    """
    return "".join(block.text for block in chunk.content if isinstance(block, TextBlock))


def first_error_line(exc: BaseException) -> str:
    """从 pydantic 的 ``ValidationError`` 里挑出最有信息量的一行。

    Args:
        exc (`BaseException`): 捕获到的异常。

    Returns:
        `str`: 一行错误摘要。
    """
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return " | ".join(lines[1:3]) if len(lines) > 1 else lines[0]


async def expect_escape(label: str, awaitable: Any) -> None:
    """断言一个协程抛 :class:`PathEscapeError`，并打印原因。

    Args:
        label (`str`): 人类可读的用例名。
        awaitable (`Any`): 待执行的协程。
    """
    try:
        await awaitable
    except PathEscapeError as exc:
        print(f"  拦截 OK  {label}")
        print(f"            {exc}")
    else:
        raise AssertionError(f"{label} 竟然没被拦住 —— 策略有洞")


def new_root(name: str) -> Path:
    """在临时目录下建一个工作区根目录。

    Args:
        name (`str`): 子目录名。

    Returns:
        `Path`: 已创建的目录。
    """
    root = SCRATCH / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# ======================================================================
# A. PathGuard：路径逃逸检测
# ======================================================================
def section_a() -> None:
    """A 段：`PathGuard` 的符号链接解析与模式匹配。"""
    banner("A. PathGuard：先 realpath 再比较（符号链接 / 前缀伪装 / .. 穿越）")
    root = new_root("a_guard")
    guard = PathGuard(root)

    show("构造时传入的 root", root)
    show("guard.root（构造时已 resolve）", guard.root)
    show("os.path.realpath('/tmp')", os.path.realpath("/tmp"))
    show("root 是否符号链接形式？", str(root) != str(guard.root))
    show("guard.describe()", guard.describe())

    print("  --- A1. 两种写法指的是同一个地方 ---")
    show("is_within(root/'a.py')", guard.is_within(root / "a.py"))
    show("is_within(realpath 形式)", guard.is_within(guard.root / "a.py"))
    show("is_within(root 自身)", guard.is_within(guard.root))

    print("  --- A2. 符号链接：不 resolve 就会误判 ---")
    link = SCRATCH / "a_link_to_root"
    if not link.exists():
        link.symlink_to(root)
    show("软链接 -> root，is_within(link/'a.py')", guard.is_within(link / "a.py"))
    escape = root / "escape"
    if not escape.exists():
        escape.symlink_to("/etc")
    show("root/escape -> /etc 时 is_within", guard.is_within(root / "escape" / "passwd"))
    show(
        "（朴素 str.startswith 会判 True，这就是洞）",
        str(root / "escape" / "passwd").startswith(str(root)),
    )

    print("  --- A3. 前缀伪装与 .. 穿越 ---")
    show("is_within('<root>-evil/a.py')", guard.is_within(Path(str(root) + "-evil") / "a.py"))
    show("is_within(root/'../../etc/passwd')", guard.is_within(root / "../../etc/passwd"))
    show("is_within('/etc/passwd')", guard.is_within("/etc/passwd"))

    print("  --- A4. resolve_within：合法路径归一化，越界抛异常 ---")
    show("resolve_within(root/'notes/../a.txt')", guard.resolve_within(root / "notes" / ".." / "a.txt"))
    try:
        guard.resolve_within("/etc/passwd")
    except PathEscapeError as exc:
        show("resolve_within('/etc/passwd')", f"PathEscapeError: {exc}")
    else:
        raise AssertionError("resolve_within 竟然放行了 /etc/passwd")
    show("relative_to_root(root/'src/a.py')", guard.relative_to_root(root / "src" / "a.py"))

    print("  --- A5. matches_patterns 的四条规则 ---")
    cases = [
        ("src/*.py", "src/a.py", "规则 1：相对 glob 直接 fnmatch"),
        ("*.pem", "a/b/k.pem", "规则 2：与文件名 fnmatch"),
        (".git/", "src/.git/config", "规则 3：与任一段 fnmatch"),
        ("**/*.pem", "k.pem", "规则 4：去掉 **/ 再试"),
        ("secrets/*", "a/secrets/x.txt", "反例：不该命中"),
    ]
    for pattern, rel, why in cases:
        hit = guard.matches_patterns(root / rel, [pattern])
        show(f"matches_patterns({rel!r}, [{pattern!r}])", f"{hit!r}   # {why}")
        if pattern == "secrets/*":
            assert hit is None, "反例不该命中"
        else:
            assert hit == pattern, f"{pattern!r} 应当命中 {rel!r}"


# ======================================================================
# B. SandboxPolicy：策略模型
# ======================================================================
def section_b() -> None:
    """B 段：`SandboxPolicy` 的判定、默认值与 fail-closed 校验。"""
    banner("B. SandboxPolicy：黑名单优先于白名单，全部先归一化再比较")
    root = new_root("b_policy")
    outside = new_root("b_outside")
    (outside / "shared.txt").write_text("shared\n", encoding="utf-8")
    (root / ".env").write_text(FAKE_SECRET, encoding="utf-8")

    policy = SandboxPolicy(workspace_root=root)
    show("DEFAULT_DENY_PATHS", DEFAULT_DENY_PATHS)
    show("政策根（已 realpath）", policy.workspace_root)

    print("  --- 判定 ---")
    show("is_readable(root/'a.txt')", policy.is_readable(root / "a.txt"))
    show("is_readable('/etc/passwd')", policy.is_readable("/etc/passwd"))
    show("is_writable(root/'notes/a.md')", policy.is_writable(root / "notes" / "a.md"))
    show("is_writable(root/'.env')", policy.is_writable(root / ".env"))
    show("is_writable(root/'.git/config')", policy.is_writable(root / ".git" / "config"))
    show("is_readable(root/'k.pem')", policy.is_readable(root / "k.pem"))
    show("deny_hit(root/'.env')", policy.deny_hit(root / ".env"))
    show("containing_root('/etc/passwd','read')", policy.containing_root("/etc/passwd", "read"))

    print("  --- 额外白名单：相对路径按 workspace_root 解析，不是进程 cwd ---")
    policy2 = SandboxPolicy(
        workspace_root=root,
        read_paths=[str(outside), "./shared"],
        write_paths=["./out"],
    )
    show("extra_roots('read')", [str(p) for p in policy2.extra_roots("read")])
    show("extra_roots('write')", [str(p) for p in policy2.extra_roots("write")])
    show("is_readable(outside/'shared.txt')", policy2.is_readable(outside / "shared.txt"))
    show("containing_root(outside/'shared.txt')", policy2.containing_root(outside / "shared.txt", "read"))
    show("deny_hit(outside/'shared.txt')", policy2.deny_hit(outside / "shared.txt"))

    print("  --- 网络档位与降级（下一行 WARNING 是刻意打出来的）---")
    show("policy(none).is_network_enforceable()", policy.is_network_enforceable())
    full = SandboxPolicy(workspace_root=root, network="full")
    show("policy(full).is_network_enforceable()", full.is_network_enforceable())
    policy.warn_if_unenforceable("local")

    print("  --- fail-closed 校验：加载期就报错，不等到运行时 ---")
    for label, kwargs in (
        ("network=allowlist 但白名单为空", {"network": "allowlist"}),
        ("cpu=0", {"cpu": 0}),
        ("拼错的字段 memmory_mb=512", {"memmory_mb": 512}),
    ):
        try:
            SandboxPolicy(workspace_root=root, **kwargs)  # type: ignore[arg-type]
        except ValueError as exc:
            show(label, f"ValidationError: {first_error_line(exc)}")
        else:
            raise AssertionError(f"{label} 竟然通过了校验")

    show("policy.describe()", policy.describe())


# ======================================================================
# C. PolicyLocalWorkspace：本地工作区 + 策略
# ======================================================================
async def _plain_workspace(workdir: Path) -> Any:
    """造一个原生 :class:`~agentscope.workspace.LocalWorkspace` 供复用测试。

    Args:
        workdir (`Path`): 工作目录。

    Returns:
        `Any`: 未 ``initialize()`` 的原生本地工作区。
    """
    from agentscope.workspace import LocalWorkspace

    return LocalWorkspace(workdir=str(workdir))


async def section_c() -> None:
    """C 段：策略化本地工作区的读写、越界拦截、超时与输出截断。"""
    banner("C. PolicyLocalWorkspace：本地工作区 + 策略校验")
    root = new_root("c_ws")
    outside = new_root("c_ws_outside")
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")

    policy = SandboxPolicy(workspace_root=root, timeout_s=30, max_output_bytes=4096)
    ws = PolicyLocalWorkspace(policy=policy)
    await ws.initialize()
    backend = ws.get_backend()
    show("backend", f"{type(backend).__name__}(inner={type(backend._inner).__name__})")  # type: ignore[attr-defined]
    show("workdir", ws.workdir)
    show("workspace_id", ws.workspace_id)
    show("is_alive", ws.is_alive)

    print("  --- 正常路径：相对路径按 workdir 解析 ---")
    await ws.write_file("notes/hello.txt", "第一行\n第二行\n")
    show("read_file('notes/hello.txt')", repr(await ws.read_file("notes/hello.txt")))
    show("run_command('pwd')", repr(await ws.run_command("pwd")))
    show("run_command('ls notes')", repr(await ws.run_command("ls notes")))

    print("  --- 越界与黑名单：全部抛 PathEscapeError ---")
    await expect_escape("read_file('/etc/passwd')", ws.read_file("/etc/passwd"))
    await expect_escape("read_file('../../etc/passwd')", ws.read_file("../../etc/passwd"))
    await expect_escape(
        f"read_file('{outside}/secret.txt')",
        ws.read_file(str(outside / "secret.txt")),
    )
    await expect_escape("write_file('.env')", ws.write_file(".env", FAKE_SECRET))
    await expect_escape("write_file('k.pem')", ws.write_file("k.pem", "PEM\n"))
    await expect_escape("write_file('.git/config')", ws.write_file(".git/config", "x\n"))
    await expect_escape("run_command('pwd', cwd='/etc')", ws.run_command("pwd", cwd="/etc"))
    show("确认越界写没有留下文件", not Path("/etc/harness10_probe").exists())
    show("check_readable('notes/hello.txt')", ws.check_readable("notes/hello.txt"))
    try:
        ws.check_writable("/etc/harness10_probe")
    except PathEscapeError as exc:
        show("check_writable('/etc/...')", f"PathEscapeError: {str(exc)[:50]}...")
    else:
        raise AssertionError("check_writable 竟然放行了 /etc")

    print("  --- 超时：policy.timeout_s 真的压住了 sleep ---")
    tight = SandboxPolicy(workspace_root=root, timeout_s=2)
    ws_tight = PolicyLocalWorkspace(policy=tight)
    await ws_tight.initialize()
    started = time.monotonic()
    text = await ws_tight.run_command("sleep 30")
    elapsed = time.monotonic() - started
    show("run_command('sleep 30')（policy.timeout_s=2）", repr(text))
    show("实际耗时（秒）", f"{elapsed:.2f}")
    assert 1.0 < elapsed < 6.0, f"超时没按 2 秒生效（耗时 {elapsed:.2f}s）"

    print("  --- 同一个超时，换条带子进程的命令就不灵了（AgentScope 的坑）---")
    started = time.monotonic()
    text = await ws_tight.run_command("sleep 6; echo never")
    elapsed = time.monotonic() - started
    show("run_command('sleep 6; echo never')", repr(text))
    show("实际耗时（秒）", f"{elapsed:.2f}")
    print("        2 秒就判了超时，但 shell 只 kill 掉自己，孙进程还攥着 stdout 管道，")
    print("        asyncio 的 communicate() 必须等它退出 —— 于是又白等了 4 秒。")
    assert elapsed > 5.0, "这个坑没复现出来（说明 AgentScope 改了实现，请复核）"

    print("  --- 输出截断：max_output_bytes 真的截断 ---")
    small = SandboxPolicy(workspace_root=root, max_output_bytes=200)
    ws_small = PolicyLocalWorkspace(policy=small)
    await ws_small.initialize()
    text = await ws_small.run_command("seq 1 200")
    show("run_command('seq 1 200') 字符数", len(text))
    show("是否含截断标记", "harness_kit 沙箱已截断" in text)
    show("末尾 46 字符", repr(text[-46:]))

    print("  --- workdir ∉ workspace_root：构造期就炸 ---")
    other = new_root("c_other_ws")
    try:
        PolicyLocalWorkspace(
            policy=SandboxPolicy(workspace_root=root),
            base_workspace=await _plain_workspace(other),
        )
    except PathEscapeError as exc:
        show("base_workspace.workdir 在策略根之外", f"PathEscapeError: {str(exc)[:54]}...")
    else:
        raise AssertionError("workdir 越界竟然构造成功了")

    await ws.aclose()
    await ws_tight.aclose()
    await ws_small.aclose()
    show("aclose() 之后 is_alive", ws.is_alive)
    show("policy.describe()", policy.describe())


# ======================================================================
# D. 内置工具也过策略
# ======================================================================
async def section_d() -> None:
    """D 段：`await ws.list_tools()` 拿到的内置工具共用同一个策略化 backend。"""
    banner("D. 内置工具也过策略：Write / Read / Bash 共用 PolicyBackend")
    root = new_root("d_tools")
    policy = SandboxPolicy(workspace_root=root, timeout_s=5, max_output_bytes=512)
    ws = PolicyLocalWorkspace(policy=policy)
    await ws.initialize()

    tools = {tool.name: tool for tool in await ws.list_tools()}
    show("list_tools() 名字", sorted(tools))
    show("Write 拿到的 backend", type(getattr(tools["Write"], "_backend")).__name__)
    show(
        "Write 与工作区是同一个 backend 对象",
        getattr(tools["Write"], "_backend") is ws.get_backend(),
    )
    show("Bash 的 _cwd", getattr(tools["Bash"], "_cwd", None))

    print("  --- D1. Write 写在策略根之内：成功 ---")
    chunk = await tools["Write"](
        file_path=str(root / "notes" / "from_tool.txt"),
        content="line-1\nline-2\n",
    )
    show("ToolChunk.state（工具只置 running，由 agent 收尾）", chunk.state)
    show("ToolChunk 文本", chunk_text(chunk))

    print("  --- D2. Write 写到策略根之外：PathEscapeError 从工具里逃出来 ---")
    try:
        await tools["Write"](file_path="/etc/harness10_probe.txt", content="nope\n")
    except PathEscapeError as exc:
        show("Write('/etc/harness10_probe.txt')", f"PathEscapeError: {str(exc)[:52]}...")
    else:
        raise AssertionError("Write 竟然写出了工作区")
    show("目标文件是否被创建", Path("/etc/harness10_probe.txt").exists())

    print("  --- D3. Read 读工作区外：同样被拦 ---")
    try:
        await tools["Read"](file_path="/etc/passwd")
    except PathEscapeError as exc:
        show("Read('/etc/passwd')", f"PathEscapeError: {str(exc)[:52]}...")
    else:
        raise AssertionError("Read 竟然读到了 /etc/passwd")

    print("  --- D4. Bash：输出被策略截断（工具自己只截 30000 字符）---")
    chunks = [c async for c in await tools["Bash"](command="pwd && ls")]
    text = "".join(chunk_text(c) for c in chunks)
    show("Bash('pwd && ls') 首行", text.splitlines()[0])
    big = [c async for c in await tools["Bash"](command="seq 1 5000")]
    big_text = "".join(chunk_text(c) for c in big)
    show("Bash('seq 1 5000') 字符数", len(big_text))
    show("是否含 harness_kit 截断标记", "harness_kit 沙箱已截断" in big_text)

    print("  --- D5. Bash：超时由策略压住，报错文案却是工具自己的数字 ---")
    started = time.monotonic()
    slow = [c async for c in await tools["Bash"](command="sleep 30")]
    elapsed = time.monotonic() - started
    slow_text = "".join(chunk_text(c) for c in slow)
    show("Bash('sleep 30') 结果", repr(slow_text))
    show("实际耗时（秒）", f"{elapsed:.2f}")
    print("        工具文案写的是 120000ms，真正生效的是 policy.timeout_s=5")
    assert 4.0 <= elapsed < 12.0, f"策略超时没生效（耗时 {elapsed:.2f}s）"

    await ws.aclose()


# ======================================================================
# E. HarnessOffloader：大内容落盘
# ======================================================================
async def section_e() -> None:
    """E 段：`HarnessOffloader` 的三个协议方法、降级与体积护栏。"""
    banner("E. HarnessOffloader：大块内容落盘，消息里只留引用")
    root = new_root("e_offload")
    policy = SandboxPolicy(workspace_root=root, max_output_bytes=4096)
    ws = PolicyLocalWorkspace(policy=policy)
    await ws.initialize()
    offloader = HarnessOffloader(workspace=ws)
    show("DEFAULT_MAX_OFFLOAD_BYTES", DEFAULT_MAX_OFFLOAD_BYTES)

    print("  --- E1. offload_data_block：base64 大图 -> workspace:// URL ---")
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 20000
    block = DataBlock(
        source=Base64Source(
            type="base64",
            media_type="image/png",
            data=base64.b64encode(payload).decode(),
        ),
    )
    saved = await offloader.offload_data_block(block)
    show("返回块的 source 类型", type(saved.source).__name__)
    show("返回块的 url", getattr(saved.source, "url", None))

    print("  --- E2. 同一个 block 再 offload 一次：按 base64 文本 hash 短路 ---")
    again = await offloader.offload_data_block(block)
    show(
        "两次 URL 相同",
        str(getattr(again.source, "url", None)) == str(getattr(saved.source, "url", None)),
    )
    show("data/ 目录内容", sorted(p.name for p in (root / "data").iterdir()))

    print("  --- E3. 已经是 URLSource 的块：原样返回 ---")
    url_block = DataBlock(
        source=URLSource(type="url", url="workspace:///data/x.png", media_type="image/png"),
    )
    same = await offloader.offload_data_block(url_block)
    show("原样返回（同一个对象）", same is url_block)

    print("  --- E4. offload_context：压缩掉的上下文落进 sessions/<sid>/context.jsonl ---")
    msgs = [
        Msg(name="user", role="user", content=[TextBlock(text=f"历史消息 {i}")])
        for i in range(5)
    ]
    path = await offloader.offload_context("sess-10", msgs)
    show("返回路径", path)
    show("会话目录内容", sorted(p.name for p in (root / "sessions" / "sess-10").iterdir()))

    print("  --- E5. offload_tool_result：同名重写加 (1) 后缀，不覆盖 ---")
    tool_result = ToolResultBlock(id="call_10", name="Bash", output="x" * 3000)
    show("第一次", await offloader.offload_tool_result("sess-10", tool_result))
    show("第二次", await offloader.offload_tool_result("sess-10", tool_result))

    print("  --- E6. 体积护栏：超过 max_offload_bytes 时拒绝落盘，保持内联 ---")
    tiny = HarnessOffloader(workspace=ws, max_offload_bytes=1024)
    refused = await tiny.offload_context("sess-10", msgs * 50)
    show("offload_context 的返回", refused)
    show("拒绝计数", tiny.stats()["refused_too_large"])

    print("  --- E7. 没有 workspace：明确降级，绝不假装成功 ---")
    bare = HarnessOffloader(workspace=None)
    show("offload_context", await bare.offload_context("s", msgs))
    show("offload_tool_result", await bare.offload_tool_result("s", tool_result))
    show("stats", bare.stats())

    show("有工作区时的 stats", offloader.stats())
    try:
        HarnessOffloader(workspace=ws, max_offload_bytes=0)
    except ValueError as exc:
        show("max_offload_bytes=0", f"ValueError: {exc}")
    else:
        raise AssertionError("0 字节上限竟然通过了")
    show("describe()", offloader.describe())
    await ws.aclose()


# ======================================================================
# F. 后端选择与降级
# ======================================================================
async def section_f() -> None:
    """F 段：`choose_workspace_kind` 与真实可用性探测。"""
    banner("F. 后端选择与降级：choose_workspace_kind + docker_available()")
    show("choose_workspace_kind('local')", choose_workspace_kind("local"))
    show("choose_workspace_kind('docker', docker_ok=True)", choose_workspace_kind("docker", docker_ok=True))
    show("choose_workspace_kind('e2b', e2b_ok=True)", choose_workspace_kind("e2b", e2b_ok=True))
    show("choose_workspace_kind('e2b', docker_ok=True)", choose_workspace_kind("e2b", docker_ok=True))
    show("choose_workspace_kind('docker', docker_ok=False)", choose_workspace_kind("docker", docker_ok=False))
    show("choose_workspace_kind('docker', docker_ok=None)", choose_workspace_kind("docker", docker_ok=None))
    show(
        "choose_workspace_kind('e2b', 两者都不可用)",
        choose_workspace_kind("e2b", docker_ok=False, e2b_ok=False),
    )
    try:
        choose_workspace_kind("dokcer")  # 故意拼错
    except ValueError as exc:
        show("choose_workspace_kind('dokcer')", f"ValueError: {exc}")
    else:
        raise AssertionError("拼错的后端名竟然被静默降级了")

    real = await docker_available()
    show("docker_available()（真实探测）", real)
    show("按真实探测选 docker", choose_workspace_kind("docker", docker_ok=real))


# ======================================================================
# G. QuotaMixin：配额翻译（纯函数，不连 daemon）
# ======================================================================
def section_g() -> None:
    """G 段：`QuotaMixin` 把策略翻译成 `HostConfig`。"""
    banner("G. QuotaMixin：策略 -> Docker HostConfig（纯函数，不连 daemon）")
    root = new_root("g_quota")

    class _Fake(QuotaDockerWorkspace):
        """只为调用 mixin 的两个纯函数而存在，**不**触发任何容器生命周期。"""

        def __init__(self, policy: SandboxPolicy) -> None:
            self.policy = policy

    policy = SandboxPolicy(workspace_root=root, cpu=1.5, memory_mb=512, pids=64, network="none")
    fake = _Fake(policy)
    show("policy", repr(policy))
    show("quotas_as_host_config()", fake.quotas_as_host_config())

    config: dict[str, Any] = {"Image": "python:3.11-slim", "HostConfig": {"Binds": ["a:b"]}}
    merged = fake.apply_quotas_to_config(config)
    show("apply_quotas_to_config()", merged)
    show("原有 Binds 是否被保留", merged["HostConfig"]["Binds"])

    print("  --- network=allowlist：HostConfig 表达不了，降级为断网（fail closed）---")
    allow = SandboxPolicy(workspace_root=root, network="allowlist", network_allowlist=["pypi.org"])
    show("quota(network=allowlist)", _Fake(allow).quotas_as_host_config())

    print("  --- network=full：不写 NetworkMode，用 Docker 默认 bridge ---")
    full = SandboxPolicy(workspace_root=root, network="full")
    show("quota(network=full)", _Fake(full).quotas_as_host_config())

    print("  --- 类型护栏 ---")
    try:
        _Fake(policy).apply_quotas_to_config(["not", "a", "dict"])  # type: ignore[arg-type]
    except TypeError as exc:
        show("apply_quotas_to_config(list)", f"TypeError: {exc}")
    else:
        raise AssertionError("非 dict 的 config 竟然通过了")


# ======================================================================
# H. QuotaDockerWorkspace：真实容器 + 真实配额
# ======================================================================
async def section_h(docker_ok: bool) -> None:
    """H 段：真实起容器并核对配额；Docker 不可用时验证降级报错。

    Args:
        docker_ok (`bool`): `docker_available()` 的结果。
    """
    banner("H. QuotaDockerWorkspace：容器 + 真实 cgroup 配额")
    root = new_root("h_docker")
    policy = SandboxPolicy(
        workspace_root=root,
        cpu=0.5,
        memory_mb=384,
        pids=64,
        network="none",
        timeout_s=30,
    )
    ws = QuotaDockerWorkspace(policy=policy)
    show("CONTAINER_WORKDIR", CONTAINER_WORKDIR)

    if not docker_ok:
        try:
            await ws.start()
        except DockerUnavailableError as exc:
            show("docker 不可用时 start()", f"DockerUnavailableError: {str(exc)[:60]}...")
        else:
            raise AssertionError("Docker 不可用却启动成功了")
        return

    started = time.monotonic()
    await ws.start()
    show("启动耗时（秒；镜像已缓存时很快）", f"{time.monotonic() - started:.1f}")
    backend = ws.get_backend()
    show("backend", f"{type(backend).__name__}(inner={type(backend._inner).__name__})")  # type: ignore[attr-defined]
    show("容器内 workdir", ws.workdir)
    show("注入的配额", ws.quotas_as_host_config())

    print("  --- H1. 内存配额：/sys/fs/cgroup/memory.max ---")
    show("memory.max", (await ws.run_command("cat /sys/fs/cgroup/memory.max")).strip())
    print("  --- H2. CPU 配额：/sys/fs/cgroup/cpu.max（50000 100000 即 0.5 核）---")
    show("cpu.max", (await ws.run_command("cat /sys/fs/cgroup/cpu.max")).strip())
    show("（对照）nproc 只反映宿主核数", (await ws.run_command("nproc")).strip())
    print("  --- H3. 进程数配额：/sys/fs/cgroup/pids.max（防 fork 炸弹）---")
    show("pids.max", (await ws.run_command("cat /sys/fs/cgroup/pids.max")).strip())
    print("  --- H4. 网络：NetworkMode=none，容器真的连不出去 ---")
    net = await ws.run_command(
        "python -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\" 2>&1 | tail -1",
    )
    show("对外连接尝试", net.strip().splitlines()[-1] if net.strip() else "(空输出)")

    print("  --- H5. 容器模式的路径策略：/workspace 之内仍查 deny_paths ---")
    await ws.write_file("/workspace/notes/in_container.txt", "container-ok\n")
    show("read_file", repr(await ws.read_file("/workspace/notes/in_container.txt")))
    try:
        await ws.write_file("/workspace/.env", FAKE_SECRET)
    except PathEscapeError as exc:
        show("write_file('/workspace/.env')", f"PathEscapeError: {str(exc)[:52]}...")
    else:
        raise AssertionError("容器里竟然写成了 /workspace/.env")

    print("  --- H6. 容器内工作目录之外放行（容器本身就是边界）---")
    show("read_file('/etc/hostname')", (await ws.read_file("/etc/hostname")).strip())
    show("宿主侧能看到 bind-mount 的产物", sorted(p.name for p in root.iterdir()))
    await ws.aclose()
    show("aclose() 之后 is_alive", ws.is_alive)


# ======================================================================
# I. 从 Profile 装配
# ======================================================================
async def _probe_escape(ws: Any) -> str:
    """对装配出的工作区做一次越界读，返回结论字符串。

    Args:
        ws (`Any`): 工作区。

    Returns:
        `str`: 结论描述。
    """
    try:
        await ws.read_file("/etc/passwd")
    except PathEscapeError:
        return "read_file('/etc/passwd') -> PathEscapeError"
    raise AssertionError("Profile 装配出来的工作区竟然放行了 /etc/passwd")


async def section_i() -> None:
    """I 段：`workspace.policy` 经 `HarnessBuilder` 变成策略化工作区。"""
    banner("I. 从 Profile 装配：workspace.kind + policy -> PolicyLocalWorkspace")
    profile_dir = Path(tempfile.mkdtemp(prefix="lesson10_profiles_"))
    root = new_root("i_profile_ws")
    profile_path = profile_dir / "sandbox_demo.yaml"
    profile_path.write_text(
        "\n".join(
            [
                "name: sandbox_demo",
                "description: 第 10 讲演示：本地工作区 + 沙箱策略",
                "model:",
                "  provider: deepseek",
                "  model_name: deepseek-chat",
                "  api_key_env: OPENAI_API_KEY",
                "  base_url_env: OPENAI_BASE_URL",
                "tools:",
                "  packs: [builtin]",
                "skills:",
                "  directories: []",
                "mcp:",
                "  servers: []",
                "workspace:",
                "  kind: local",
                f"  root: {root}",
                "  policy:",
                "    deny_paths: ['.git/', '.env', '**/*.pem']",
                "    network: none",
                "    cpu: 0.5",
                "    memory_mb: 256",
                "    pids: 64",
                "    timeout_s: 15",
                "    max_output_bytes: 65536",
                "permission:",
                "  mode: default",
                "memory:",
                "  enabled: false",
                "agent:",
                "  name: sandbox-demo-agent",
                '  sys_prompt: "你是一个严谨的助手，只能在工作区里读写文件。"',
                "  max_iters: 6",
                "",
            ],
        ),
        encoding="utf-8",
    )
    settings = Settings.from_env()
    profile = load_resolved_profile(str(profile_path), search_dir=profile_dir)
    show("ResolvedProfile.workspace.kind", profile.workspace.kind)
    show("ResolvedProfile.workspace.root", profile.workspace.root)
    show("policy 类型（YAML 里没写 workspace_root）", type(profile.workspace.policy).__name__)
    show(
        "policy.workspace_root（_fill_policy_root 回填）",
        profile.workspace.policy.workspace_root,  # type: ignore[union-attr]
    )

    builder = HarnessBuilder(profile, settings=settings)
    ws = await builder.build_workspace()
    show("装配出的工作区类型（registry: policy_local）", type(ws).__name__)
    show("策略根", ws.policy.workspace_root)  # type: ignore[attr-defined]
    show("网络档位", ws.policy.network)  # type: ignore[attr-defined]
    show("超时（秒）", ws.policy.timeout_s)  # type: ignore[attr-defined]
    await ws.initialize()  # type: ignore[union-attr]
    await ws.write_file("from_profile.txt", "profile-ok\n")  # type: ignore[union-attr]
    show("read_file('from_profile.txt')", repr(await ws.read_file("from_profile.txt")))  # type: ignore[union-attr]
    show("越界仍被拦", await _probe_escape(ws))
    await builder.aclose()
    shutil.rmtree(profile_dir, ignore_errors=True)


# ======================================================================
# J. 真模型（--live）
# ======================================================================
def _last_assistant_text(context: list[Msg]) -> str:
    """取最后一条 assistant 消息的纯文本。

    ``reply_stream`` 在 ``stream=False`` 时不会把最终答复作为 ``Msg`` 事件吐出来，
    所以要看总结得回到 ``agent.state.context``。

    Args:
        context (`list[Msg]`): agent 的上下文。

    Returns:
        `str`: 文本内容；没有 assistant 消息时是空串。
    """
    for msg in reversed(context):
        if msg.role == "assistant":
            return msg.get_text_content() or ""
    return ""


def _mentions_denial(text: str) -> bool:
    """看模型有没有如实报告"被沙箱拒绝"。

    Args:
        text (`str`): assistant 的最终回复。

    Returns:
        `bool`: 是否提到拒绝 / 越界 / 无权之类。
    """
    return any(
        word in text
        for word in ("拒绝", "denied", "沙箱", "越界", "无权", "不允许", "无法读取", "无法访问")
    )


async def section_j() -> None:
    """J 段：真实 deepseek-flash 把 Agent 关进笼子里（3~6 次 LLM 调用）。"""
    banner("J. 真实 deepseek-flash：Agent 在策略化工作区里干活（3~6 次 LLM 调用）")
    root = new_root("j_live_ws")
    policy = SandboxPolicy(
        workspace_root=root,
        deny_paths=[".git/", ".env", "**/*.pem"],
        network="none",
        timeout_s=20,
        max_output_bytes=65536,
    )
    ws = PolicyLocalWorkspace(policy=policy)
    await ws.initialize()

    settings = Settings.from_env()
    model = build_chat_model(
        ModelSpec(
            provider="deepseek",
            model_name=settings.llm_model_name or "deepseek-flash",
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL",
            stream=False,
        ),
        settings=settings,
    )
    agent = Agent(
        name="sandboxed",
        system_prompt=(
            "你是一个严谨的助手。你只能在工作区里读写文件："
            "写文件用 Write，读文件用 Read，执行命令用 Bash。"
            "如果某个操作被沙箱拒绝，如实报告被拒绝这件事，不要反复重试。"
            "完成任务后用一句话总结。"
        ),
        model=model,
        toolkit=Toolkit(tools=await ws.list_tools()),
        offloader=HarnessOffloader(workspace=ws),
        react_config=ReActConfig(max_iters=6),
    )
    show("工作区 workdir", ws.workdir)

    target = root / "notes" / "demo.txt"
    task = (
        "请严格按顺序做两件事，不要跳步、不要用别的工具代替：\n"
        f"第一步：调用 Write 工具，往 {target} 写入三行：line-1 / line-2 / line-3；\n"
        "第二步：调用 Read 工具读 /etc/passwd，然后把它读到的内容或失败原因告诉我。"
    )

    event_input: Any = UserMsg("user", task)
    denied = 0
    for round_index in range(6):
        pending: RequireUserConfirmEvent | None = None
        print(f"  --- reply_stream 第 {round_index + 1} 次调用 ---")
        async for item in agent.reply_stream(event_input):
            name = type(item).__name__
            if isinstance(item, Msg):
                print(f"  Msg: {item.get_text_content()}")
            elif isinstance(item, RequireUserConfirmEvent):
                pending = item
                print(f"  {name}: {[tc.name for tc in item.tool_calls]}")
            else:
                detail = getattr(item, "state", None)
                if name == "ToolResultEndEvent" and detail == "error":
                    denied += 1
                print(f"  {name}" + (f": state={detail}" if detail else ""))
        if pending is None:
            break
        print(f"  -- 第 {round_index + 1} 轮 park 在 RequireUserConfirmEvent：自动确认后继续 --")
        event_input = UserConfirmResultEvent(
            reply_id=pending.reply_id,
            confirm_results=[
                ConfirmResult(confirmed=True, tool_call=tc) for tc in pending.tool_calls
            ],
        )

    show("Agent 是否真的把文件写进去了", target.exists())
    if target.exists():
        content = target.read_text(encoding="utf-8")
        show("文件内容", repr(content))
        show("行数", len(content.strip().splitlines()))
    show("被沙箱拒绝的工具结果数（state=error）", denied)
    final_text = _last_assistant_text(agent.state.context)
    print(f"  最终回复（节选）: {final_text[:220]}")
    show("最终回复有没有如实报告被拒", _mentions_denial(final_text))
    show("本例的 offloader 统计（未触发压缩）", agent.offloader.stats())  # type: ignore[union-attr]
    await ws.aclose()


# ======================================================================
# main
# ======================================================================
async def main() -> int:
    """跑完全部段落。

    Returns:
        `int`: 退出码。
    """
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    print(f"scratch = {SCRATCH}")
    section_a()
    section_b()
    await section_c()
    await section_d()
    await section_e()
    await section_f()
    section_g()
    docker_ok = await docker_available()
    await section_h(docker_ok)
    await section_i()
    if LIVE:
        await section_j()
    else:
        print()
        print("=" * 74)
        print("J 段被跳过（没有 --live）。加上 --live 会真实调用 deepseek-flash。")
    print("=" * 74)
    print("ALL SECTIONS DONE")
    print(f"scratch 保留在 {SCRATCH}（内含所有工作区产物，可直接 ls 查看）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
