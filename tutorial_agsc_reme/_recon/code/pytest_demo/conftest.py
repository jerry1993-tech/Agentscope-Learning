"""把仓库根 .env 注入到每个 pytest 会话（pytest 11+ 也可以用 pytest-dotenv 插件）。"""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[4]  # pytest_demo -> code -> _recon -> tutorial_agsc_reme -> repo
load_dotenv(REPO / ".env")


@pytest.fixture(scope="session")
def llm_env() -> dict[str, str]:
    """给测试用的 LLM 连接信息（缺一个就 skip）。"""
    keys = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "LLM_MODEL")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")
    return {k: os.environ[k] for k in keys}
