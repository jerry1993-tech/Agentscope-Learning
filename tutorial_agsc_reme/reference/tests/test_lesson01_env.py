# -*- coding: utf-8 -*-
"""第 1 讲的单元测试：环境隔离、Settings、官方扩展点的形状。

**这些测试全部离线可跑**（不消耗任何 LLM 调用），只有最后一个
``test_llm_roundtrip_optional`` 需要 key，且缺 key 时会 ``skip`` 而不是失败。

测试的选材原则是"**把第 1 讲反复强调的坑，变成一条会红的断言**"：

- ``reme`` 必须解析到 ``third_party/ReMe``（否则是 site-packages 的 0.3.1.10）；
- ``Settings`` 必须能从 ``.env`` 的两套变量名里都读出 key；
- 自定义工具的默认权限必须是 ``ASK``（这是 AgentScope 的设计，不是 bug）；
- ``"basic"`` 是保留工具组名，撞名必须 ``ValueError``；
- ReMe 嵌入式装配**不需要** ``resolve_app_config`` 之外的额外启动参数，
  但**必须**显式 ``await app._start()`` 才能检索到东西。

运行::

    cd tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest tests/test_lesson01_env.py -v
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import agentscope
import reme
import harness_kit

from harness_kit.settings import Settings


# ---------------------------------------------------------------------------
# 1. 版本与路径隔离
# ---------------------------------------------------------------------------
def test_agentscope_is_the_local_2_0_8_source() -> None:
    """AgentScope 必须是我们 ``-e`` 安装的 2.0.8 本地源码。

    真值：``third_party/agentscope/src/agentscope/_version.py:4``。
    """
    assert agentscope.__version__ == "2.0.8"
    assert "third_party/agentscope/src/agentscope" in agentscope.__file__


def test_reme_is_the_local_0_4_1_13_clone() -> None:
    """ReMe 必须解析到仓库内的 0.4.1.13，而不是 site-packages 的 0.3.1.10。

    真值：``third_party/ReMe/reme/__init__.py:3``。

    这是整本教程第一个、也是最重要的断言：导错版本时 Python **不会报错**，
    只会在某个很深的 import 里抛 ``ModuleNotFoundError: No module named
    'agentscope.token'``。
    """
    assert reme.__version__ == "0.4.1.13", (
        f"导到了 {reme.__version__}（{reme.__file__}），"
        "请确认运行时带了 PYTHONPATH=.../third_party/ReMe"
    )
    assert "third_party/ReMe/reme" in reme.__file__


def test_harness_kit_version_contract() -> None:
    """``harness_kit`` 的版本契约：``__version__`` 与 ``get_version()`` 一致。"""
    assert harness_kit.__version__ == "0.1.0"
    assert harness_kit.get_version() == harness_kit.__version__


# ---------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------
def test_settings_reads_env_and_resolves_relative_paths(settings: Settings) -> None:
    """``Settings.from_env()`` 能把 ``.env`` 读进字段，并把相对路径锚到 repo_root。

    真值：``harness_kit/settings.py`` 的 ``repo_root`` / ``resolve()`` /
    ``_REPO_ROOT_FALLBACK``。
    """
    repo_root = settings.repo_root
    assert repo_root.is_dir()
    assert (repo_root / "third_party" / "ReMe").is_dir()

    # 相对路径一律以 repo_root 为锚点解析成绝对路径。
    resolved = settings.resolve("./.harness/workspace")
    assert resolved.is_absolute()
    assert resolved == (repo_root / ".harness" / "workspace").resolve()

    # 绝对路径原样返回（只做 resolve）。
    assert settings.resolve(repo_root) == repo_root.resolve()


def test_settings_masks_api_key_in_redacted_snapshot(settings: Settings) -> None:
    """``redacted()`` 必须把 key 换成掩码，且路径字段绝对化。"""
    snapshot = settings.redacted()
    assert snapshot["llm_api_key"] == "sk-***" or snapshot["llm_api_key"] is None
    assert "sk-" not in str(snapshot["llm_api_key"]).replace("sk-***", "")
    assert Path(str(snapshot["workspace_dir"])).is_absolute()
    assert Path(str(snapshot["session_dir"])).is_absolute()


def test_settings_has_llm_flag_follows_api_key(settings: Settings) -> None:
    """``has_llm()`` 只看 ``llm_api_key`` 是否非空。"""
    assert settings.has_llm() == bool(settings.llm_api_key)


def test_ensure_dirs_is_idempotent(settings: Settings) -> None:
    """``ensure_dirs()`` 幂等：连调两次不报错，目录都在。"""
    settings.ensure_dirs()
    settings.ensure_dirs()
    assert settings.resolve(settings.workspace_dir).is_dir()
    assert settings.resolve(settings.session_dir).is_dir()


def test_settings_alias_choices_accept_both_naming_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两套环境变量名（``LLM_*`` 与 ``OPENAI_*``）都要能被接住。

    这是本仓库最容易踩的坑：ReMe 的 ``as_llm`` 只认 ``LLM_API_KEY`` 那四个，
    而仓库既有的 ``.env`` 写的是 ``OPENAI_API_KEY``。``Settings`` 用
    ``AliasChoices`` 同时接住两套，测试把它钉死。
    """
    for name in (
        "HARNESS_LLM_API_KEY",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "HARNESS_LLM_BASE_URL",
        "LLM_BASE_URL",
        "OPENAI_BASE_URL",
        "HARNESS_LLM_MODEL_NAME",
        "LLM_MODEL_NAME",
        "LLM_MODEL",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-openai-name")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "model-from-openai-name")

    built = Settings.from_env(env_file=tmp_path / "no-such.env")
    assert built.llm_api_key == "sk-from-openai-name"
    assert built.llm_base_url == "https://example.invalid/v1"
    assert built.llm_model_name == "model-from-openai-name"

    # LLM_* 优先于 OPENAI_*：显式写 LLM_API_KEY 时应当胜出。
    monkeypatch.setenv("LLM_API_KEY", "sk-from-llm-name")
    rebuilt = Settings.from_env(env_file=tmp_path / "no-such.env")
    assert rebuilt.llm_api_key == "sk-from-llm-name"


# ---------------------------------------------------------------------------
# 3. AgentScope 扩展点的形状（不调用 LLM）
# ---------------------------------------------------------------------------
async def test_function_tool_default_permission_is_ask() -> None:
    """``FunctionTool`` 的默认权限是 ``ASK``——这是设计，不是 bug。

    真值：``third_party/agentscope/src/agentscope/tool/_adapters.py:116`` 起
    的 ``check_permissions`` 实现，以及 ``:132`` 的
    ``behavior=PermissionBehavior.ASK``。

    这就是为什么直接 ``Toolkit(tools=[FunctionTool(my_func)])`` 跑第一次一定
    "卡住"——它在等一个 ``UserConfirmResultEvent``。
    """
    from agentscope.permission import PermissionBehavior
    from agentscope.tool import FunctionTool

    def ping(text: str) -> str:
        """回显文本。

        Args:
            text (`str`): 任意文本。

        Returns:
            `str`: 原样返回。
        """
        return text

    tool = FunctionTool(ping)
    decision = await tool.check_permissions()
    assert decision.behavior is PermissionBehavior.ASK


async def test_function_tool_explicit_allow_overrides_default() -> None:
    """显式传 ``permission=PermissionDecision(ALLOW)`` 后必须直接放行。"""
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool

    def ping(text: str) -> str:
        """回显文本。

        Args:
            text (`str`): 任意文本。

        Returns:
            `str`: 原样返回。
        """
        return text

    tool = FunctionTool(
        ping,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="单测：直接放行",
        ),
    )
    decision = await tool.check_permissions()
    assert decision.behavior is PermissionBehavior.ALLOW


def test_toolkit_basic_group_is_reserved() -> None:
    """``"basic"`` 是保留工具组名，在 ``tool_groups`` 里再写一次必须 ``ValueError``。

    真值：``third_party/agentscope/src/agentscope/tool/_toolkit.py:121``
    （``Toolkit.__init__`` 里的 ``raise ValueError("The 'basic' tool group is
    reserved ...")``）。这是第 5 讲 ``harness_kit/tools/pack.py`` 的
    "``tool_groups`` 里不能出现保留组名 basic" 那条约束的出处。
    """
    from agentscope.tool import Toolkit, ToolGroup

    # 正常构造：只用 tools= 参数，工具会落进 "basic" 组。
    toolkit = Toolkit(tools=[])
    assert [group.name for group in toolkit.tool_groups] == ["basic"]

    # 撞名：tool_groups 里出现 "basic" 直接报错。
    with pytest.raises(ValueError, match="basic"):
        Toolkit(
            tools=[],
            tool_groups=[ToolGroup(name="basic", description="撞名的组")],
        )


def test_agentscope_top_level_exports_are_deliberately_minimal() -> None:
    """AgentScope 2.0.8 顶层几乎不导出东西，必须从子包导入。

    真值：``third_party/agentscope/src/agentscope/__init__.py:16`` 的 ``__all__``
    只有 5 个符号。所以 ``agentscope.init()`` / ``agentscope.token`` /
    ``agentscope.memory`` 这些 1.x 写法全部不可用。
    """
    assert agentscope.__all__ == [
        "logger",
        "setup_logger",
        "set_id_factory",
        "set_timestamp_factory",
        "__version__",
    ]
    with pytest.raises(AttributeError):
        _ = agentscope.token  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 4. ReMe 嵌入式装配（不起 HTTP 服务，零 LLM 调用）
# ---------------------------------------------------------------------------
def _build_embedded_app(workspace: Path) -> Any:
    """把 ReMe 装成"库"而不是"服务"。

    三个必做动作，缺一个就会以很难定位的方式失败：

    1. ``resolve_app_config()`` —— 否则 ``jobs={}``；
    2. 关掉 ``service.web_enabled`` / ``mcp_enabled`` —— 否则占端口；
    3. **显式往 ``components.as_llm.default`` 里灌凭据**。``default.yaml:846-853``
       写的是 ``backend: ${LLM_BACKEND:-openai}`` / ``model: ${LLM_MODEL_NAME:-qwen3.7-plus}``
       / ``credential.api_key: ${LLM_API_KEY:-}``；本仓库 ``.env`` 里叫
       ``OPENAI_API_KEY``，不映射的话 ``BaseAsLLM._start()``
       （``third_party/ReMe/reme/components/as_llm/__init__.py:34``）会在
       ``OpenAIChatModel.__init__`` 里抛
       ``openai.OpenAIError: Missing credentials``——**堆栈极深，看起来完全不像是
       环境变量名的问题**。

    本测试**不需要真 key**：``assets.llm`` 只是要"能构造出来"，
    而检索走的是 BM25，不会真的发请求。所以缺 key 时用占位串。

    Args:
        workspace (`Path`): 工作区根目录。

    Returns:
        `Any`: 尚未 ``_start()`` 的 ``reme.ReMe`` 实例。

    Raises:
        `ValueError`: 配置里缺 ``service.backend``（说明没走
            ``resolve_app_config``）。
    """
    import os

    from reme import ReMe
    from reme.config import resolve_app_config

    cfg = resolve_app_config(log_config=False)
    cfg["workspace_dir"] = str(workspace)
    cfg["enable_logo"] = False
    cfg["service"] = {**cfg["service"], "web_enabled": False, "mcp_enabled": False}
    cfg["components"]["as_llm"]["default"].update(
        {
            "model": os.environ.get("LLM_MODEL") or "offline-placeholder",
            "credential": {
                "api_key": os.environ.get("OPENAI_API_KEY")
                or os.environ.get("LLM_API_KEY")
                or "sk-offline-placeholder",
                "base_url": os.environ.get("OPENAI_BASE_URL")
                or os.environ.get("LLM_BASE_URL")
                or "https://example.invalid/v1",
            },
        },
    )
    return ReMe(**cfg)


async def test_reme_assemble_without_default_yaml_yields_no_jobs(
    tmp_path: Path,
) -> None:
    """反例：没有 ``default.yaml`` 的 ``jobs`` 段时，应用是空的，``run_job`` 直接 ``KeyError``。

    真值：``third_party/ReMe/reme/application.py:26``（``__init__`` 里第一行走的是
    ``resolve_plugin_runtime``，**不读** ``default.yaml``）与
    ``third_party/ReMe/reme/config/config_parser.py:262``
    （``resolve_app_config`` 才是读 ``default.yaml`` 的那个人）；
    报错本体在 ``application.py:373`` 的
    ``raise KeyError(f"Job '{name}' not found")``。

    这就是"看起来像 job 名字写错了、实际是配置根本没加载"的那个误导性报错。
    """
    from reme import ReMe
    from reme.config import resolve_app_config

    cfg = resolve_app_config(log_config=False)
    cfg["workspace_dir"] = str(tmp_path / "no_yaml")
    cfg["enable_logo"] = False
    cfg["service"] = {**cfg["service"], "web_enabled": False, "mcp_enabled": False}

    # 把 default.yaml 提供的那两段摘掉，模拟"从来没调过 resolve_app_config"。
    cfg.pop("jobs", None)
    cfg.pop("components", None)

    app = ReMe(**cfg)
    try:
        assert app.context.jobs == {}, "没有 jobs 段时应用应当是空的"
        with pytest.raises(KeyError, match="search"):
            await app.run_job("search", query="anything")
    finally:
        await app._close()


def test_reme_embedded_app_registers_40_jobs_and_disables_services(
    tmp_path: Path,
) -> None:
    """正例：``resolve_app_config()`` + ``ReMe(**cfg)`` 拿到 40 个 job，且不起服务。

    真值：job 清单来自 ``third_party/ReMe/reme/config/default.yaml``；
    ``Application._init_jobs`` 在 ``application.py:92``。
    """
    app = _build_embedded_app(tmp_path / "reme_ws")
    try:
        jobs = sorted(app.context.jobs)
        assert len(jobs) == 40
        assert {"search", "index_update_loop", "chat", "help"} <= set(jobs)
        # 三类长驻 job 必须存在，但都不能用 run_job 直接跑。
        assert "index_update_loop" in jobs  # background
        assert "chat" in jobs  # stream
        assert "dream_cron" in jobs  # cron
        # 服务被显式关掉，所以不会占端口。
        assert app.config.service.web_enabled is False
        assert app.config.service.mcp_enabled is False
    finally:
        # __init__ 之后没有 _start()，但也统一走 _close() 保证语义一致。
        import asyncio as _asyncio

        _asyncio.run(app._close())


async def test_reme_search_job_hits_seeded_note(tmp_path: Path) -> None:
    """端到端：写 md → ``await app._start()`` → 等后台索引 → ``run_job("search")``。

    全链路 **不调用 LLM**：``default.yaml:956`` 把
    ``file_store.default.embedding_store`` 显式设成空串，所以索引只有 BM25，
    关键词通道就能命中。

    三个必踩点都在这一个测试里钉死了：

    1. 必须先 ``await app._start()``（``application.py:187``），否则索引是空的，
       检索会"成功但返回空"；
    2. ``index_update_loop`` 是 ``BackgroundJob``，直接 ``run_job`` 会永久挂住，
       只能让它作为后台 task 跑（``steps/index/watch_changes.py:93`` 的 ``awatch``
       长驻循环）；
    3. ``run_job`` 的 ``name`` 是 positional-only（``application.py:370``）。
    """
    workspace = tmp_path / "reme_ws"
    app = _build_embedded_app(workspace)
    try:
        daily_dir = workspace / app.config.daily_dir / date.today().isoformat()
        daily_dir.mkdir(parents=True, exist_ok=True)
        (daily_dir / "lesson01.md").write_text(
            "---\n"
            "name: lesson01\n"
            "description: 第 1 讲冒烟笔记\n"
            "memory_tags: [agent, harness]\n"
            "---\n"
            "# Hello Harness\n\n"
            "Agent Harness 是模型外围的运行管控基础设施。\n",
            encoding="utf-8",
        )

        await app._start()
        await asyncio.sleep(3.0)

        resp = await app.run_job("search", query="Agent Harness", limit=3)
        assert resp.success is True
        assert "lesson01.md" in str(resp.answer)
        assert {"results", "counts"} <= set(resp.metadata)
        # counts["vector"] == 0 是**合法**状态：没有配 embedding_store。
        assert resp.metadata["counts"]["vector"] == 0
    finally:
        await app._close()


# ---------------------------------------------------------------------------
# 5. 一次真实 LLM 往返（缺 key 自动 skip）
# ---------------------------------------------------------------------------
async def test_llm_roundtrip_optional(llm_env: dict[str, str]) -> None:
    """真实打一次 deepseek-flash，确认"环境真的能出网"。

    **这是本文件唯一消耗 LLM 配额的测试**（1 次 model call）。缺 key 会 skip。

    注意 ``ChatModelBase.__call__`` 收的是 ``list[Msg]``
    （``third_party/agentscope/src/agentscope/model/_base.py:182``），
    传单个 ``Msg`` 会 ``TypeError: Input must be a list of Msg objects.``
    """
    from agentscope.credential import DeepSeekCredential
    from agentscope.message import UserMsg
    from agentscope.model import DeepSeekChatModel

    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=llm_env["api_key"],
            base_url=llm_env["base_url"],
        ),
        model=llm_env["model"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=64),
    )
    response = await model([UserMsg("user", "只回答两个字：收到")])
    text = "".join(
        block.text for block in response.content if getattr(block, "text", None)
    )
    assert text.strip(), f"模型返回了空文本：{response!r}"
