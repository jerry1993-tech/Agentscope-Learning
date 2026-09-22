# -*- coding: utf-8 -*-
"""harness_kit 的全局设置。

来源优先级（从高到低）：显式入参 > 进程环境变量 > ``.env`` > 字段默认值。

设计要点：

1. **不引入重依赖**：本模块只依赖 ``pydantic`` / ``pydantic-settings`` / ``dotenv``，
   不 import agentscope / reme，因此可以在任何脚本的第一步安全地构造。
2. **路径钉死**：``workspace_dir`` / ``session_dir`` / ``profile_dir`` 允许写相对路径，
   但 :meth:`Settings.resolve` 一律以 :attr:`Settings.repo_root` 为锚点解析成绝对路径，
   这样无论从哪个 cwd 启动 CLI，落盘位置都一致。
3. **变量名兼容**：契约里字段叫 ``llm_api_key``（读 ``LLM_API_KEY``，复用 ReMe 的变量名），
   但本仓库的 ``.env`` 实际写的是 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``LLM_MODEL``
   —— 这两套名字都用 :class:`~pydantic.AliasChoices` 接住，避免"契约与 .env 打架"。

实测环境（已验证）：

.. code-block:: text

    /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env
    LLM_MODEL=deepseek-flash
    OPENAI_API_KEY=sk-***
    OPENAI_BASE_URL=https://api.deepseek.com

注意：``.env`` 里**没有** ``LLM_MODEL_NAME``，所以契约 §6.3 的
``model_name: ${LLM_MODEL_NAME:-deepseek-chat}`` 会回落到 ``deepseek-chat``，
而不是本环境实测可用的 ``deepseek-flash``。Profile 里应写 ``${LLM_MODEL}``。
"""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from loguru import logger
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT_FALLBACK: Path = Path(__file__).resolve().parents[3]
"""``<repo>/tutorial_agsc_reme/reference/harness_kit/settings.py`` → 上溯 3 层即仓库根。

- ``parents[0]`` = ``.../reference/harness_kit``
- ``parents[1]`` = ``.../reference``
- ``parents[2]`` = ``.../tutorial_agsc_reme``
- ``parents[3]`` = 仓库根
"""

_DEFAULT_ENV_FILE: Path = _REPO_ROOT_FALLBACK / ".env"
"""仓库根下的 ``.env``，由 :func:`Settings.from_env` 显式 ``load_dotenv``。"""

_MASK: str = "sk-***"
"""``redacted()`` 里用于替换密钥的掩码。"""


class Settings(BaseSettings):
    """harness_kit 全局设置。

    来源优先级：显式入参 > 环境变量 > ``.env`` > 默认值。
    """

    model_config = SettingsConfigDict(
        env_prefix="HARNESS_",
        env_file=str(_DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    repo_root: Path = Field(
        default=_REPO_ROOT_FALLBACK,
        description="仓库根目录，用于把所有相对路径钉死。",
    )

    workspace_dir: Path = Field(
        default=Path("./.harness/workspace"),
        description="Agent 的工作区根目录（相对路径按 repo_root 解析）。",
    )

    session_dir: Path = Field(
        default=Path("./.harness/sessions"),
        description="会话事件日志与会话快照的落盘根目录。",
    )

    profile_dir: Path = Field(
        default=Path("./harness_kit/profiles"),
        description="Profile / Bundle 的 YAML 搜索目录。",
    )

    log_level: str = Field(
        default="INFO",
        description="日志级别，交给 loguru 消费。",
    )

    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HARNESS_LLM_API_KEY",
            "LLM_API_KEY",
            "OPENAI_API_KEY",
        ),
        description="LLM API key；按优先级从 LLM_API_KEY / OPENAI_API_KEY 读取。",
    )

    llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HARNESS_LLM_BASE_URL",
            "LLM_BASE_URL",
            "OPENAI_BASE_URL",
        ),
        description="LLM base url；从 LLM_BASE_URL / OPENAI_BASE_URL 读取。",
    )

    llm_model_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HARNESS_LLM_MODEL_NAME",
            "LLM_MODEL_NAME",
            "LLM_MODEL",
            "OPENAI_MODEL",
        ),
        description="默认模型名；从 LLM_MODEL_NAME / LLM_MODEL 读取。",
    )

    llm_backend: str | None = Field(
        default=None,
        validation_alias=AliasChoices("HARNESS_LLM_BACKEND", "LLM_BACKEND"),
        description="可选的 LLM 后端标识（ReMe 侧会用到）。",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        """把日志级别统一成大写，容忍 ``info`` / ``Info`` 之类输入。

        Args:
            value (`Any`): 原始入参。

        Returns:
            `Any`: 大写后的字符串；非字符串原样返回。
        """
        return value.upper() if isinstance(value, str) else value

    # ------------------------------------------------------------------
    # 构造入口
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, **overrides: object) -> "Settings":
        """构造 :class:`Settings` 并做一次存在性校验。

        这是**唯一推荐入口**：先把仓库根的 ``.env`` 灌进 ``os.environ``
        （``override=False``，不覆盖真实环境变量），再构造 Settings，
        最后幂等创建 ``workspace_dir`` / ``session_dir``。

        Args:
            **overrides (`object`): 显式覆盖项，优先级最高，字段名即关键字名。

        Returns:
            `Settings`: 完成目录创建与校验的设置对象。

        Raises:
            `FileNotFoundError`: ``repo_root`` 不存在。
            `ValueError`: 目录创建后仍不可写（例如路径被同名文件占用）。
        """
        env_file = Path(overrides.pop("env_file", _DEFAULT_ENV_FILE))  # type: ignore[arg-type]
        if env_file.is_file():
            load_dotenv(env_file, override=False)
        else:  # pragma: no cover - 只在克隆出来的裸环境里出现
            logger.warning(
                ".env 不存在（{}），将只依赖进程环境变量",
                env_file,
            )

        settings = cls(**overrides)  # type: ignore[arg-type]

        repo_root = settings.repo_root.resolve()
        if not repo_root.is_dir():
            raise FileNotFoundError(f"repo_root 不存在: {repo_root}")
        settings.repo_root = repo_root

        settings.ensure_dirs()

        if not settings.llm_api_key:
            logger.warning(
                "未读到任何 LLM API key（LLM_API_KEY / OPENAI_API_KEY 都为空），"
                "离线组件（echo 模型、权限引擎单测）仍可用，真实 LLM 调用会失败",
            )
        return settings

    # ------------------------------------------------------------------
    # 路径与目录
    # ------------------------------------------------------------------
    def resolve(self, path: str | Path) -> Path:
        """把相对路径锚定到 :attr:`repo_root` 上。

        Args:
            path (`str | Path`): 绝对路径原样返回（仅做 ``resolve()``）；
                相对路径以 ``repo_root`` 为基准。

        Returns:
            `Path`: 绝对路径。已知坑：macOS 的 ``/tmp`` 是 ``/private/tmp``
            的符号链接，所以这里一定 ``resolve()``，否则后续前缀比较会误判越界。
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate
        return candidate.resolve()

    def ensure_dirs(self) -> None:
        """幂等创建 :attr:`workspace_dir` 与 :attr:`session_dir`。

        Raises:
            `ValueError`: 目标路径存在但不是目录。
        """
        for field_name in ("workspace_dir", "session_dir"):
            target = self.resolve(getattr(self, field_name))
            if target.exists() and not target.is_dir():
                raise ValueError(f"{field_name} 指向的不是目录: {target}")
            target.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def redacted(self) -> dict[str, object]:
        """返回可安全落日志的设置快照。

        Returns:
            `dict[str, object]`: 与 ``model_dump()`` 同构，但
            ``llm_api_key`` 被替换成 ``"sk-***"``；路径字段一律绝对化。
        """
        data: dict[str, object] = self.model_dump()
        data["repo_root"] = str(self.repo_root.resolve())
        for field_name in ("workspace_dir", "session_dir", "profile_dir"):
            data[field_name] = str(self.resolve(str(data[field_name])))
        if data.get("llm_api_key"):
            data["llm_api_key"] = _MASK
        return data

    # ------------------------------------------------------------------
    # 环境快照
    # ------------------------------------------------------------------
    def has_llm(self) -> bool:
        """是否具备真实 LLM 调用条件。

        Returns:
            `bool`: ``llm_api_key`` 非空即视为可调用（``base_url`` 可缺省，
            缺省时由各家 SDK 自己决定默认端点）。
        """
        return bool(self.llm_api_key)

    def environ_overlay(self) -> dict[str, str]:
        """返回供 :func:`harness_kit.config.loader.interpolate_env` 使用的环境映射。

        ``pydantic-settings`` 已经把 ``.env`` 读进字段，但 Profile YAML 里的
        ``${VAR}`` 插值走的是另一条路（``os.environ``）。这里把「显式入参换算出来的
        LLM_* 值」补进映射，保证 Profile 里写 ``${LLM_API_KEY}`` 也能解析。

        Returns:
            `dict[str, str]`: 以 ``os.environ`` 为底、被 :class:`Settings`
            字段覆盖后的映射。
        """
        overlay: dict[str, str] = dict(os.environ)
        mapping = {
            "LLM_API_KEY": self.llm_api_key,
            "OPENAI_API_KEY": self.llm_api_key,
            "LLM_BASE_URL": self.llm_base_url,
            "OPENAI_BASE_URL": self.llm_base_url,
            "LLM_MODEL_NAME": self.llm_model_name,
            "LLM_MODEL": self.llm_model_name,
            "LLM_BACKEND": self.llm_backend,
        }
        for key, value in mapping.items():
            if value:
                overlay[key] = value
        return overlay
