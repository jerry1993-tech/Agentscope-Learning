"""ReMe 最小可运行样例：装配 Application -> 写 daily md -> 索引 -> 检索。

运行：
  PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python 01_reme_smoke.py
"""
import asyncio
import os
import shutil
import sys
from datetime import date
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
sys.path.insert(0, str(REPO / "third_party" / "ReMe"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")  # 只读仓库根的 .env

import reme  # noqa: E402
from reme import ReMe, __version__  # noqa: E402
from reme.config import resolve_app_config  # noqa: E402

print("reme version:", __version__)
print("reme file   :", reme.__file__)

WORKSPACE = Path("/tmp/harness_smoke_reme_ws")
if WORKSPACE.exists():
    shutil.rmtree(WORKSPACE)
WORKSPACE.mkdir(parents=True)


def build_app() -> ReMe:
    # 关键：Application 自己不会加载 default.yaml，必须显式调 resolve_app_config()
    cfg = resolve_app_config(log_config=False)
    cfg["workspace_dir"] = str(WORKSPACE)
    cfg["enable_logo"] = False
    cfg["service"] = {**cfg["service"], "web_enabled": False, "mcp_enabled": False}
    # 把 .env 里的 OPENAI_* 映射到 ReMe 期望的 LLM_*
    cfg["components"]["as_llm"]["default"].update(
        {
            "model": os.environ["LLM_MODEL"],
            "credential": {
                "api_key": os.environ["OPENAI_API_KEY"],
                "base_url": os.environ["OPENAI_BASE_URL"],
            },
        }
    )
    return ReMe(**cfg)


async def main() -> None:
    app = build_app()
    print("jobs registered:", sorted(app.context.jobs))

    # 1) 启动前先把带 frontmatter 的 md 写进 workspace 的 daily/<date>/
    today = date.today().isoformat()
    daily_dir = WORKSPACE / app.config.daily_dir / today
    daily_dir.mkdir(parents=True, exist_ok=True)
    note = daily_dir / "harness_intro.md"
    note.write_text(
        "---\n"
        "name: harness_intro\n"
        "description: Agent Harness 第一条笔记\n"
        "memory_tags: [agent, harness]\n"
        "---\n"
        "# Hello Harness\n\n"
        "Agent Harness 是模型外围的运行管控基础设施，负责 Agent Loop 与 Tool Use。\n",
        encoding="utf-8",
    )
    print("wrote:", note)

    # 2) 启动：_start() 会拉起 components + 所有 job（含 index_update_loop 后台 watcher）
    await app._start()
    await asyncio.sleep(3)  # 等后台 index_update_loop 跑完一轮

    # 3) 注意：index_update_loop 是 BackgroundJob，第二个 step 是 watch_changes_step，
    #    它内部是 awatch 长驻循环 —— 直接 run_job("index_update_loop") 会永久阻塞！
    #    正确做法是让 _start() 把它当后台任务跑，然后等它把文件吃进索引。
    await asyncio.sleep(2)

    # 4) 检索
    resp = await app.run_job("search", query="Agent Harness 是什么", limit=3)
    print("[search] success =", resp.success)
    print("[search] answer  =\n", resp.answer)
    print("[search] metadata keys =", list(resp.metadata))

    # 5) 列出所有已注册 job 的元信息
    resp = await app.run_job("help")
    print("[help] answer head =", str(resp.answer)[:200])

    await app._close()


asyncio.run(main())
