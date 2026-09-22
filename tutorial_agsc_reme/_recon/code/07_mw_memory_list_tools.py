"""07_agentscope_middleware_hook 侦察：三个长期记忆中间件的 mode 语义。

必须用 PYTHONPATH 指向本地 ReMe 源码运行（否则 site-packages 里的旧 reme
0.3.1.10 会抢先，报 `No module named 'agentscope.token'`）:

  PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
  tutorial_agsc_reme/_recon/code/07_mw_memory_list_tools.py

验证点：
1. 三个记忆中间件都遵循同一套 mode 三态契约：
   static_control -> 自动检索 + 注入，无工具
   agent_control  -> 暴露检索工具，不自动检索
   both           -> 两者都要
   写回（write-back）在任何模式下都由 on_reply 自动完成，没有 add 工具。
2. on_system_prompt 只用于给模型「打广告」（告诉它有 memory_search 可用）。
3. list_tools() 是中间件给 Agent 注入工具的唯一入口。
"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.middleware import (  # noqa: E402
    AgenticMemoryMiddleware,
    Mem0Middleware,
    ReMeMiddleware,
)

WS = "/tmp/_recon_reme_ws"


async def probe(label, mw) -> None:
    tools = await mw.list_tools()
    key = await mw.get_middleware_key()
    print(
        f"  {label:38s} key={key:32s} "
        f"tools={[(type(t).__name__, getattr(t, 'name', '?')) for t in tools]}",
    )


async def main() -> None:
    print("=" * 96)
    print("ReMeMiddleware：mode 三态")
    print("=" * 96)
    for mode in ("static_control", "agent_control", "both"):
        mw = ReMeMiddleware(
            workspace_dir=WS,
            parameters=ReMeMiddleware.Parameters(mode=mode),
        )
        await probe(f"ReMe(mode={mode})", mw)
    print()
    print("  on_system_prompt 差异：")
    for mode in ("static_control", "both"):
        mw = ReMeMiddleware(
            workspace_dir=WS,
            parameters=ReMeMiddleware.Parameters(mode=mode),
        )
        out = await mw.on_system_prompt(None, "BASE")
        print(f"    mode={mode:15s} -> {out[:70]!r}")
    print()

    print("=" * 96)
    print("Mem0Middleware：mode 三态（未安装 mem0，list_tools 仍可离线调用）")
    print("=" * 96)
    for mode in ("static_control", "agent_control", "both"):
        try:
            mw = Mem0Middleware(user_id="u1", mode=mode)
            await probe(f"Mem0(mode={mode})", mw)
        except Exception as e:  # noqa: BLE001
            print(f"  Mem0(mode={mode}) 构造失败: {type(e).__name__}: {e}")
    print()

    print("=" * 96)
    print("AgenticMemoryMiddleware：文件后端（workdir 下写 MEMORY.md）")
    print("=" * 96)
    try:
        mw = AgenticMemoryMiddleware(workdir="/tmp/_recon_agentic_mem")
        await probe("AgenticMemory(默认)", mw)
        print("  Parameters 字段 =",
              list(AgenticMemoryMiddleware.Parameters.model_fields))
    except Exception as e:  # noqa: BLE001
        print(f"  构造失败: {type(e).__name__}: {e}")


asyncio.run(main())
