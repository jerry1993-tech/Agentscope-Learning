"""Recon 15 / 验证 (1) ReMe 走嵌入式 API 而非 HTTP 服务; (2) search job 的默认 min_score。"""
import asyncio, os, shutil, logging
from dotenv import load_dotenv
load_dotenv("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env")
from agentscope.middleware._longterm_memory._reme import _config
logging.getLogger("reme").setLevel(logging.ERROR)
_config._dream_steps = lambda: [
    {"backend": "dream_extract_step", "file_catalog": "dream", "scan_days": 2, "max_units": 5},
    {"backend": "dream_integrate_step"},
    {"backend": "dream_finish_step", "file_catalog": "dream"}]

WS = "/tmp/recon15/ws_C"; shutil.rmtree(WS, ignore_errors=True)

async def main():
    cfg = _config._build_reme_app_config(workspace_dir=WS)
    print("config.service        =", cfg["service"])
    print("search job parameters =", cfg["jobs"]["search"]["parameters"])
    print("search job step opts  =", {k: v for k, v in cfg["jobs"]["search"]["steps"][0].items() if k != "backend"})
    from reme import ReMe
    app = ReMe(**cfg)
    await app.start()
    print("service started?      =", app.context.service.is_started if app.context.service else None)
    print("service component     =", type(app.context.service).__name__)
    await app.close()
asyncio.run(main())
