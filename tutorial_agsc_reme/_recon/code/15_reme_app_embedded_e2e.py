"""Recon 15 / snippet B: embedded ReMe app driven end-to-end, WITH the
ReMe 0.4.1.13 compatibility shim for AgentScope's dream step list."""
import asyncio, os, shutil
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")

from agentscope.credential import DeepSeekCredential
from agentscope.model import DeepSeekChatModel
from agentscope.middleware._longterm_memory._reme import _config
from agentscope.middleware._longterm_memory._reme._utils import _extract_memory_texts
from reme import ReMe

# ---- COMPAT SHIM ------------------------------------------------------
# AgentScope 2.0.8's _config._dream_steps() emits a `dream_topics_step`
# that does not exist in reme 0.4.1.13 (see reme/config/default.yaml:64-75).
def _dream_steps_compat():
    return [
        {"backend": "dream_extract_step", "file_catalog": "dream",
         "scan_days": 2, "max_units": 5},
        {"backend": "dream_integrate_step"},
        {"backend": "dream_finish_step", "file_catalog": "dream"},
    ]
_config._dream_steps = _dream_steps_compat
# -----------------------------------------------------------------------

WS = "/tmp/recon15/ws_B"
shutil.rmtree(WS, ignore_errors=True)

async def main():
    chat = DeepSeekChatModel(
        credential=DeepSeekCredential(api_key=os.environ["OPENAI_API_KEY"],
                                      base_url=os.environ["OPENAI_BASE_URL"]),
        model=os.environ["LLM_MODEL"], stream=True)
    app = ReMe(**_config._build_reme_app_config(workspace_dir=WS))
    await app.update_component("as_llm", "default", model=chat)
    await app.start()
    print("[1] jobs registered:", len(app.context.jobs))

    r = await app.run_job("auto_memory", messages=[
        {"role": "user", "name": "alice", "content": [{"type": "text", "text":
            "My name is Alice, I am based in Hangzhou. For every chart I always use matplotlib in dark mode."}]},
        {"role": "assistant", "name": "bot", "content": [{"type": "text", "text":
            "Noted Alice - dark-mode matplotlib from now on."}]},
    ], session_id="session-1")
    print("[2] auto_memory success =", r.success, "answer =", (r.answer or "")[:100].replace("\n"," "))
    await app.run_job("reindex")
    r3 = await app.run_job("search", query="chart library and theme preference", limit=5)
    print("[3] search success =", r3.success)
    print("    memories:", _extract_memory_texts(r3.metadata))
    await app.close()

asyncio.run(main())
