# -*- coding: utf-8 -*-
"""06 侦察验证脚本 C：内置工具 + sandbox backend + 权限判定
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_builtin_and_backend.py
"""
import asyncio
import json
import logging
import os
import tempfile

from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionContext
from agentscope.state import AgentState
from agentscope.tool import (
    Bash,
    Edit,
    Glob,
    Grep,
    LocalBackend,
    Read,
    ToolChunk,
    ToolGroup,
    Toolkit,
    Write,
)

logging.disable(logging.INFO)


async def main() -> None:
    workdir = tempfile.mkdtemp(prefix="as_builtin_")
    backend = LocalBackend()

    print("=" * 70)
    print("[1] BackendBase: only 3 abstract primitives exist")
    from agentscope.tool import BackendBase
    print("   BackendBase abstract methods:",
          sorted(BackendBase.__abstractmethods__))
    print("   LocalBackend abstract methods:",
          sorted(LocalBackend.__abstractmethods__))
    print("   _path_module =", backend._path_module.__name__,
          "| os_name =", backend.os_name)

    print("=" * 70)
    print("[2] exec_shell takes an ARGV list, NOT a shell string")
    r = await backend.exec_shell(["echo", "hello", "harness"])
    print("   exit_code =", r.exit_code, "stdout =", r.stdout, "ok =", r.ok())
    r2 = await backend.exec_shell(["definitely-not-a-command-xyz"])
    print("   not-found: exit_code =", r2.exit_code,
          "stderr =", r2.stderr[:60])

    print("=" * 70)
    print("[3] write_file / read_file are raw bytes")
    p = os.path.join(workdir, "a.txt")
    await backend.write_file(p, "第一行\n第二行\n".encode("utf-8"))
    print("   read back:", await backend.read_file(p))

    print("=" * 70)
    print("[4] Bash wraps the argv in a shell; streaming ToolChunk")
    bash = Bash(cwd=workdir, backend=backend)
    chunks = []
    async for c in bash.call(command="echo hi && ls | wc -l",
                             description="demo"):
        chunks.append(c)
        print("   chunk:", repr(c.content[0].text), c.state, "is_last",
              c.is_last)

    print("=" * 70)
    print("[5] Bash timeout -> exit_code -1 -> ERROR chunk")
    async for c in bash.call(command="sleep 5", timeout=500):
        print("   chunk:", repr(c.content[0].text), c.state)

    print("=" * 70)
    print("[6] Bash.check_read_only is per-invocation (tree-sitter)")
    for cmd in ["ls -a", "cat a.txt", "git status", "rm -rf /tmp/x",
                "ls $(rm -rf /)", "python x.py"]:
        ro = await bash.check_read_only({"command": cmd})
        risky = bash._bash_parser.check_injection_risk(cmd)
        print(f"   {cmd!r:26s} read_only={ro!s:5s} injection_risk={risky}")

    ctx = PermissionContext()
    for cmd in ["ls -a", "rm -rf /tmp/x", "cat /etc/passwd"]:
        d = await bash.check_permissions({"command": cmd}, ctx)
        print(f"   check_permissions({cmd!r}) -> {d.behavior}"
              f" bypass_immune={d.bypass_immune}")

    print("=" * 70)
    print("[7] Builtin file tools via Toolkit (state-injected)")
    toolkit = Toolkit(tools=[
        Read(), Write(), Edit(), Glob(), Grep(), Bash(cwd=workdir),
    ])
    toolkit.tool_groups[0].description = "basic"
    state = AgentState()

    async def run(name: str, payload: dict) -> None:
        tc = ToolCallBlock(id=name, name=name,
                           input=json.dumps(payload))
        async for chunk in toolkit.call_tool(tc, state):
            if isinstance(chunk, ToolChunk):
                txt = chunk.content[0].text
                print(f"   {name}: {txt[:110]!r} / {chunk.state}")

    await run("Write", {"file_path": os.path.join(workdir, "b.py"),
                        "content": "import os\nprint(os.getcwd())\n"})
    await run("Read", {"file_path": os.path.join(workdir, "b.py")})
    await run("Edit", {"file_path": os.path.join(workdir, "b.py"),
                       "old_string": "print(os.getcwd())",
                       "new_string": "print('edited')"})
    await run("Read", {"file_path": os.path.join(workdir, "b.py")})
    await run("Glob", {"pattern": "**/*.py", "path": workdir})
    await run("Grep", {"pattern": "edited", "path": workdir})

    print("=" * 70)
    print("[8] the Read cache lives in AgentState.tool_context")
    print("   cached files:",
          [e.file_path.rsplit("/", 1)[-1]
           for e in state.tool_context.read_file_cache])

    print("=" * 70)
    print("[9] Edit rejects a non-unique / missing old_string")
    await run("Edit", {"file_path": os.path.join(workdir, "b.py"),
                       "old_string": "NOPE", "new_string": "X"})

    print("=" * 70)
    print("[10] ToolGroup machinery is what makes the builtin tools visible")
    print("   group names:", [g.name for g in toolkit.tool_groups])
    print("   tools in 'basic':",
          [t.name for t in toolkit.tool_groups[0].tools])


if __name__ == "__main__":
    asyncio.run(main())
