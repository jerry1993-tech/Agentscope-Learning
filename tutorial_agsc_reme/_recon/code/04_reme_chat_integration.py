"""ReMe <-> AgentScope 联调：用 run_stream_job("chat") 让 ReMe 内部的 AgentScope Agent
读取 workspace 记忆来回答问题。这是「ReMe 当记忆层 + AgentScope 当 Agent」的最小闭环。
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

load_dotenv(REPO / ".env")

from reme import ReMe  # noqa: E402
from reme.config import resolve_app_config  # noqa: E402

WORKSPACE = Path("/tmp/harness_smoke_reme_chat_ws")
if WORKSPACE.exists():
    shutil.rmtree(WORKSPACE)
WORKSPACE.mkdir(parents=True)


async def main() -> None:
    cfg = resolve_app_config(log_config=False)
    cfg["workspace_dir"] = str(WORKSPACE)
    cfg["enable_logo"] = False
    cfg["service"] = {**cfg["service"], "web_enabled": False, "mcp_enabled": False}
    cfg["components"]["as_llm"]["default"].update(
        {
            "model": os.environ["LLM_MODEL"],
            "credential": {
                "api_key": os.environ["OPENAI_API_KEY"],
                "base_url": os.environ["OPENAI_BASE_URL"],
            },
        }
    )
    app = ReMe(**cfg)

    today = date.today().isoformat()
    d = WORKSPACE / app.config.daily_dir / today
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.md").write_text(
        "---\nname: project\ndescription: 我的 Harness 项目代号\nmemory_tags: [project]\n---\n"
        "# 项目代号\n\n我的 Agent Harness 项目代号是「Bluewhale」。\n",
        encoding="utf-8",
    )

    await app._start()
    await asyncio.sleep(2)

    print("--- chat 流式输出 ---")
    async for chunk in app.run_stream_job("chat", query="我的项目代号是什么？"):
        print(chunk, end="", flush=True)
    print("\n--- end ---")
    await app._close()


asyncio.run(main())
