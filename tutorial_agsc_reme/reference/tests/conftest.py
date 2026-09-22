# -*- coding: utf-8 -*-
"""pytest 的公共夹具（第 1 讲交付物之一）。

它只做三件事，顺序不能反：

1. **把仓库内的 ``third_party/ReMe`` 塞进 ``sys.path`` 最前面**，压住
   ``site-packages`` 里的 ``reme`` 0.3.1.10。这一步必须在任何
   ``import reme`` 之前完成，所以它写在 ``conftest.py`` 顶层而不是某个 fixture 里
   —— pytest 会在收集测试文件**之前**先导入 conftest。
2. **把 ``reference/`` 塞进 ``sys.path``**，让 ``import harness_kit`` 在
   "没 pip install、只跑 pytest" 的情况下也能工作。
3. **加载仓库根的 ``.env``**（``override=False``）。``Settings.from_env()``
   自己也会 load 一次，这里提前 load 是为了让**不经过 Settings** 的测试
   （例如直接构造 ``DeepSeekChatModel``）也能读到 ``os.environ``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

#: ``.../tutorial_agsc_reme/reference``
REFERENCE_ROOT: Path = Path(__file__).resolve().parents[1]
#: ``reference`` → ``tutorial_agsc_reme`` → 仓库根
REPO_ROOT: Path = REFERENCE_ROOT.parents[1]
#: 本地 ReMe 克隆的源码根，必须排在最前面。
REME_SRC: Path = REPO_ROOT / "third_party" / "ReMe"

for _candidate in (str(REME_SRC), str(REFERENCE_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

load_dotenv(REPO_ROOT / ".env", override=False)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录。

    Returns:
        `Path`: ``.../Agentscope-Learning``。
    """
    return REPO_ROOT


@pytest.fixture(scope="session")
def llm_env() -> dict[str, str]:
    """LLM 三要素，缺失时**跳过**而不是失败。

    离线 CI 里没有 key 是正常情况；用 ``skip`` 而不是 ``fail``，可以让
    "没有 LLM 也能跑的那部分测试"继续验证。

    Returns:
        `dict[str, str]`: 至少包含 ``api_key`` / ``base_url`` / ``model`` 三个键。
    """
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL") or os.environ.get("LLM_MODEL_NAME")
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY/LLM_API_KEY", api_key),
            ("OPENAI_BASE_URL/LLM_BASE_URL", base_url),
            ("LLM_MODEL/LLM_MODEL_NAME", model),
        )
        if not value
    ]
    if missing:
        pytest.skip(f"缺少 LLM 环境变量：{missing}（离线模式跳过）")
    return {"api_key": api_key, "base_url": base_url, "model": model}


@pytest.fixture()
def settings() -> Any:
    """一个指向真实仓库的 :class:`harness_kit.settings.Settings`。

    注意：这里**不改** ``repo_root``，所以 ``ensure_dirs()`` 会在仓库根下创建
    ``.harness/workspace`` 与 ``.harness/sessions`` —— 这正是
    ``Settings.from_env()`` 的语义，测试不应当绕开它。

    Returns:
        `Any`: ``Settings`` 实例。
    """
    from harness_kit.settings import Settings

    return Settings.from_env()
