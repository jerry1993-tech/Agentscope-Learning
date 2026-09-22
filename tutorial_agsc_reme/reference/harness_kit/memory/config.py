# -*- coding: utf-8 -*-
"""由 harness_kit 的 Profile / Settings 生成 ReMe 的 app config。

**三条硬约束（全部来自真实读码 + 真实运行）**

1. **必须显式调用** ``resolve_app_config()``。``ReMe(**config)`` 不会自动去读
   ``reme/config/default.yaml``；只有 ``resolve_app_config()`` 才会把
   ``default.yaml`` 载进来再和你的 kwargs 深合并
   （``third_party/ReMe/reme/config/config_parser.py:262`` 的
   ``resolve_app_config(*, log_config=True, **kwargs)``，内部
   ``_load_config("default")`` 在 ``:290``）。不调用它，``jobs=`` / ``components=``
   会是空的，``run_job`` 直接 ``KeyError``。已实测：见本文件 ``build()`` 的
   ``_ensure_resolved``。
2. **``as_llm`` 只读 ``LLM_*`` 四个环境变量**。``default.yaml:844-849`` 写的是
   ``${LLM_BACKEND:-openai}`` / ``${LLM_MODEL_NAME:-qwen3.7-plus}`` /
   ``${LLM_API_KEY:-}`` / ``${LLM_BASE_URL:-}``。本仓库的 ``.env`` 用的却是
   ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``LLM_MODEL``，四个变量名对不上，
   于是 ``api_key`` 展开成空串，``Application.start()`` 里
   ``BaseAsLLM._start`` 造 ``openai.AsyncClient`` 时报
   ``openai.OpenAIError: Missing credentials``（已实测，见 ``_probes``）。
   所以 :class:`HarnessMemoryConfig` 一律把 :class:`~harness_kit.settings.Settings`
   里的凭据显式写进 ``components.as_llm.default.credential``。
3. **embedding 组件只有在 ``embedding_dimensions is not None`` 时才加入配置**。
   逐字照抄 ``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_config.py:271-360``
   的做法：不加 ``as_embedding`` / ``embedding_store``，``file_store.embedding_store``
   保持空串，检索退化成**纯关键词**。这时的合法表现是
   ``counts == {"vector": 0, "keyword": N, ...}`` 且 ``success=True`` ——
   **不是错误**（契约 §5.3 明确要求正文不得把它当错误讲）。

**service 为什么要关掉**

ReMe 的 ``service`` 组件在 ``Application.__init__`` 时就会被实例化
（``third_party/ReMe/reme/application.py:88`` ``_init_service``），
但只有 ``run_app()`` 才会 ``build_service()`` + ``start_service()`` 去真的监听端口。
harness_kit 一律用嵌入式装配，**永不**调用 ``run_app()``，所以端口本来就不会被占用；
但我们仍然把 ``service.backend`` 显式设成 ``"cli"``，因为
``third_party/ReMe/reme/components/service/cli_service.py:62-65`` 的
``CliService`` 是官方给出的 "Execute a single job through the normal application
lifecycle **without serving a port**" 后端 —— 用它当默认值，可以让
"这个配置不会开端口" 这件事在配置里就看得见，而不是靠调用方的自律。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

from .workspace import ReMeWorkspace

__all__ = [
    "EMBEDDED_JOB_BACKENDS",
    "HarnessMemoryConfig",
    "MemoryConfigError",
    "RESCAN_REINDEX_JOB",
]


#: 嵌入式场景下允许保留的 job 后端。
#:
#: ``Application._start`` 会按 ``components → base → stream → background → cron``
#: 逐个 ``start()``（``third_party/ReMe/reme/application.py:196-205``）。
#: 这里把 ``background`` / ``cron`` 丢掉，**不是因为 start() 会挂**——本讲实测
#: ``await app.start()`` 在含常驻 job 时照常返回（只带 ``index_update_loop`` 时
#: 2.29s，把 40 个 job 全打开时 0.04s），而是因为它们的**调用语义与嵌入式不符**：
#:
#: 1. ``background`` job 的 step 是长驻循环。``WatchChangesStep.execute()`` 里是
#:    ``async for ... in awatch(...)``（``third_party/ReMe/reme/steps/index/watch_changes.py:93``），
#:    永不返回；实测 ``run_job("index_update_loop")`` 在 6s 超时前一直不返回，
#:    而 ``run_job`` 的语义是"跑一次并等结果"（``_recon/00_environment_and_smoke.md:95``）。
#:    嵌入式 harness 的 job 全部由调用方驱动，一个永不返回的 job 只会把调用方吊死。
#: 2. ``cron`` job 自己按时钟触发，无人调用也在烧资源，且需要 ``croniter``
#:    和时区配置（``third_party/ReMe/reme/components/job/cron_job.py:23-31``）。
#: 3. 它们的生命周期横跨整个 app：``BackgroundJob._start`` 起常驻 task
#:    （``.../job/background_job.py:55-75``），``close()`` 要等 ``close_timeout``（默认 5s）
#:    再强杀（``.../job/background_job.py:84-95``）。实测 ``aclose()`` 只带一个
#:    background job 要 1.01s，40 个全开要 3.03s —— 这些时间不该付在请求路径上。
#:
#: 结论：harness_kit 只保留请求-响应式的 ``base`` / ``stream`` job，周期性动作交给
#: ``harness_kit/memory/jobs.py`` 自己的 asyncio 调度器（可控、可超时、可优雅停止）。
EMBEDDED_JOB_BACKENDS: frozenset[str] = frozenset({"base", "stream"})


#: harness 侧对 ``reindex`` job 的**覆盖定义**。
#:
#: 为什么必须覆盖：ReMe ``default.yaml:429-441`` 的 ``reindex`` 只有一步
#: ``reindex_step``，而 ``ReindexStep``（``third_party/ReMe/reme/steps/index/reindex.py:11``）
#: 的注释写得很清楚 —— "Rebuild BM25, embeddings, and/or tags **without scanning
#: workspace files**"。也就是说它只对**已经进了 file_store 的 chunk** 重建索引。
#: 实测（probe p3）：写完文件直接 ``reindex``，``answer`` 是
#: ``{'bm25': {'indexed': 0}, 'embedding': {'indexed': 0}, 'tag': {'indexed': 0}}``，
#: 因为文件从未被 ingest。AgentScope 官方的 ``_config.py:120-136`` 已经踩过这个坑，
#: 它把 ``reindex`` 改写成 ``clear_store_step`` + ``init_changes_step`` +
#: ``update_index_step`` 的**重新扫描**版本。harness_kit 采用同一套写法，
#: 并把它抽成常量，好让教程能指着它讲 "配置是可以被覆盖的"。
#:
#: **``watch_dirs`` / ``watch_suffixes`` 不是可选项，漏了会毁索引。**
#:
#: 重新扫描版的第一步是 ``clear_store_step``（把 file_store 清空），
#: 第二步 ``init_changes_step`` 靠 ``build_context_watch_rules``
#: （``third_party/ReMe/reme/steps/index/_watch_rules.py:46-56``）从**运行时 context**
#: 读 ``watch_dirs`` / ``watch_suffixes``，而这两个键来自 job 自己的配置
#: （``BaseJob.__call__``：``merged = {**self.kwargs, **kwargs}``，
#: ``third_party/ReMe/reme/components/job/base_job.py:86-88``）。
#: 所以 job 配置里没有 ``watch_dirs`` → 规则集为空 → ``collect_existing`` 返回空
#: → diff 认为"没有任何文件" → **索引被清空后再也装不回来**。
#: 这是实测踩到的：``reindex`` 返回 ``success=True`` 且
#: ``metadata["counts"] == {"added": 0, "modified": 0, "deleted": 0}``，
#: 随后 ``search`` 命中 0 条 —— 全程没有任何报错。
#:
#: 目录集合 = AgentScope 官方的 ``["daily_dir", "digest_dir"]`` 再加 ``resource_dir``：
#: harness 的 :class:`~harness_kit.memory.ingest.MemoryIngestor` 会把外部资料
#: 落到 ``resource/``（它自己直接 ``upsert`` 进 file_store，不依赖 reindex），
#: 但调用方也可能手工往 ``resource/`` 放文件，重扫时应当一并纳入。
RESCAN_REINDEX_JOB: dict[str, Any] = {
    "backend": "base",
    "description": "Rebuild indexes by rescanning the watched workspace directories.",
    "watch_dirs": ["daily_dir", "digest_dir", "resource_dir"],
    "watch_suffixes": ["md"],
    "parameters": {"type": "object", "properties": {}},
    "steps": [
        {"backend": "clear_store_step"},
        {
            "backend": "init_changes_step",
            "monitor_type": "file_store",
            "monitor_name": "default",
            "dispatch_steps": ["update_index_step"],
        },
    ],
}


class MemoryConfigError(RuntimeError):
    """ReMe app config 构建失败（版本缺失 / 结构非法）。"""


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """深合并两个 dict，``overlay`` 胜出（与 ReMe 的 ``deep_merge_config`` 同语义）。

    Args:
        base (`dict[str, Any]`): 底稿。
        overlay (`dict[str, Any]`): 覆盖层。

    Returns:
        `dict[str, Any]`: 新的合并结果；两个入参都不被修改。
    """
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class HarnessMemoryConfig:
    """ReMe app config 的声明式构建器（契约 §3.15）。

    它**不持有** app，只持有 "怎么造 app" 的意图；:meth:`build` 每次都返回一份
    全新的深拷贝，所以同一个 builder 可以安全地造多个互相隔离的 app
    （多租户、测试并发都用得到）。

    Example::

        from harness_kit.memory import HarnessMemoryConfig, ReMeWorkspace

        ws = ReMeWorkspace(root="./.harness/reme")
        builder = HarnessMemoryConfig(workspace=ws).with_jobs("search", "write", "reindex")
        cfg = builder.build()
        app = reme.ReMe(**cfg)          # cfg 已经是 resolve 过的全量配置
    """

    workspace: ReMeWorkspace
    """ReMe 工作区（路径 + 六个子目录名）。"""

    embedding_dimensions: int | None = None
    """``None`` = 不装配向量检索（退化关键词检索，合法状态）。"""

    llm_model: str | None = None
    """覆盖 ``as_llm.model``；``None`` 时取 Settings 的模型名。"""

    settings: Any | None = None
    """可选的 :class:`harness_kit.settings.Settings`；``None`` 时 :meth:`build` 里惰性构造。"""

    job_whitelist: tuple[str, ...] | None = None
    """显式 job 白名单（``with_jobs`` 设置）；``None`` = 只按后端过滤。"""

    keep_background_jobs: bool = False
    """是否保留 ``background`` / ``cron`` job。默认 ``False``，原因见 :data:`EMBEDDED_JOB_BACKENDS`。"""

    component_overrides: dict[str, Any] = field(default_factory=dict)
    """``with_components`` 累积的组件覆盖（深合并到 ``components``）。"""

    extra_overrides: dict[str, Any] = field(default_factory=dict)
    """任意其它顶层覆盖（深合并到 config 根）。"""

    _base_cache: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    """``resolve_app_config()`` 结果的缓存（它有 IO + 日志，不该被 ``describe()`` 反复触发）。"""

    # ------------------------------------------------------------------
    # 构造入口
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(
        cls,
        *,
        workspace_root: str | None = None,
        embedding_dimensions: int | None = None,
        llm_model: str | None = None,
        **kwargs: Any,
    ) -> "HarnessMemoryConfig":
        """从 :class:`~harness_kit.settings.Settings` 构造。

        Args:
            workspace_root (`str | None`): 工作区根；``None`` 时用
                ``<Settings.workspace_dir>/reme``。
            embedding_dimensions (`int | None`): 向量维度；``None`` 走关键词检索。
            llm_model (`str | None`): 覆盖模型名。
            **kwargs (`Any`): 透传给 :class:`HarnessMemoryConfig` 的其余字段。

        Returns:
            `HarnessMemoryConfig`: 构建器。

        Raises:
            `MemoryConfigError`: 拿不到 ``Settings``（缺依赖）。
        """
        try:
            from ..settings import Settings
        except ImportError as exc:  # pragma: no cover - 理论上不会发生
            raise MemoryConfigError(f"无法导入 harness_kit.settings: {exc}") from exc

        settings = Settings.from_env()
        root = workspace_root or str(settings.resolve(settings.workspace_dir) / "reme")
        return cls(
            workspace=ReMeWorkspace(root=root),
            embedding_dimensions=embedding_dimensions,
            llm_model=llm_model,
            settings=settings,
            **kwargs,
        )

    @classmethod
    def from_spec(cls, spec: Any, *, settings: Any | None = None, ctx: Any | None = None) -> "HarnessMemoryConfig":
        """从 Profile 的 :class:`~harness_kit.config.schema.MemorySpec` 构造。

        这是 ``HarnessRegistry`` 里 ``memory:reme`` 这条登记项期望的语义
        （``tutorial_agsc_reme/reference/harness_kit/registry.py:1026-1030``）。

        Args:
            spec (`Any`): ``MemorySpec``（含 ``workspace_root`` /
                ``embedding_dimensions`` / ``jobs`` 等字段）。
            settings (`Any | None`): 可选 Settings；``None`` 时从 ``ctx`` 取或新建。
            ctx (`Any | None`): 可选的 ``BuildContext``，取其 ``settings``。

        Returns:
            `HarnessMemoryConfig`: 构建器。

        Raises:
            `MemoryConfigError`: ``spec`` 不是 MemorySpec 形状。
        """
        if spec is None or not hasattr(spec, "workspace_root"):
            raise MemoryConfigError(f"from_spec 需要 MemorySpec，收到 {type(spec).__name__}")

        if settings is None and ctx is not None:
            settings = getattr(ctx, "settings", None)
        if settings is None:
            from ..settings import Settings

            settings = Settings.from_env()

        root = spec.workspace_root
        if not str(root).startswith("/"):
            root = str(settings.resolve(root))

        builder = cls(
            workspace=ReMeWorkspace(root=root),
            embedding_dimensions=spec.embedding_dimensions,
            settings=settings,
        )
        jobs = list(getattr(spec, "jobs", []) or [])
        if jobs:
            builder = builder.with_jobs(*jobs)
        return builder

    # ------------------------------------------------------------------
    # 链式配置
    # ------------------------------------------------------------------
    def with_jobs(self, *job_names: str) -> "HarnessMemoryConfig":
        """设置 job 白名单（只保留这些 job）。

        这一步在 :meth:`build` 里生效：先按后端过滤掉 background / cron，
        再和白名单取交集。白名单**两种失效方式都会抛** :class:`MemoryConfigError`：

        1. 名字在 ``default.yaml`` 里根本不存在（拼错了）；
        2. 名字存在，但它的后端是 ``background`` / ``cron``，已经被嵌入式过滤掉
           （这类 job 的长驻 step 永不返回，见 :data:`EMBEDDED_JOB_BACKENDS`）。

        第二种是最阴的：``with_jobs("search", "dream_cron")`` 如果只做交集，
        会安静地退化成只保留 ``search``，调用方以为"周期性记忆已经开了"，
        实际上什么都没开。静默吞掉一个 job 名是最难查的一类 bug。

        Args:
            *job_names (`str`): job 名，如 ``"search"`` / ``"auto_memory"``。

        Returns:
            `HarnessMemoryConfig`: ``self``（便于链式调用）。
        """
        self.job_whitelist = tuple(job_names)
        return self

    def with_components(self, **overrides: Any) -> "HarnessMemoryConfig":
        """深合并组件覆盖。

        Args:
            **overrides (`Any`): 形如
                ``as_llm={"default": {"model": "deepseek-flash"}}`` 的点号结构。

        Returns:
            `HarnessMemoryConfig`: ``self``。
        """
        self.component_overrides = _deep_merge(self.component_overrides, overrides)
        return self

    def with_overrides(self, **overrides: Any) -> "HarnessMemoryConfig":
        """深合并任意顶层配置覆盖（``workspace_dir`` / ``timezone`` / ``jobs`` ...）。

        Args:
            **overrides (`Any`): 顶层键。

        Returns:
            `HarnessMemoryConfig`: ``self``。
        """
        self.extra_overrides = _deep_merge(self.extra_overrides, overrides)
        return self

    def with_embedding_dimensions(self, dimensions: int | None) -> "HarnessMemoryConfig":
        """设置 / 取消向量维度。

        Args:
            dimensions (`int | None`): ``None`` 表示不装配 embedding。

        Returns:
            `HarnessMemoryConfig`: ``self``。
        """
        if dimensions is not None and dimensions <= 0:
            raise MemoryConfigError(f"embedding_dimensions 必须为正整数或 None，收到 {dimensions}")
        self.embedding_dimensions = dimensions
        return self

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> dict[str, Any]:
        """产出一份可直接喂给 ``reme.ReMe(**config)`` 的全量配置。

        Returns:
            `dict[str, Any]`: ``resolve_app_config`` 结果叠加 harness 覆盖后的深拷贝。

        Raises:
            `MemoryConfigError`: ``reme`` 不可导入，或白名单里有不存在的 job 名。
        """
        raw = self._resolve_base()
        cfg = self._apply_llm_credentials(raw)
        cfg = self._apply_embedding(cfg)
        cfg = self._apply_embedded_service(cfg)
        cfg = self._apply_jobs(cfg)
        cfg = _deep_merge(cfg, {"components": self.component_overrides}) if self.component_overrides else cfg
        cfg = _deep_merge(cfg, self.extra_overrides) if self.extra_overrides else cfg

        # 工作区永远是最后一道，防止 overrides 把 workspace_dir 改到别处，
        # 导致"以为在 A 写、其实写到 B"这类最难查的问题。
        cfg["workspace_dir"] = str(self.workspace.root)
        cfg.update(self.workspace.dir_overrides())
        return copy.deepcopy(cfg)

    def describe(self) -> str:
        """返回配置摘要（不解释凭据，便于日志与教程输出）。

        Returns:
            `str`: 多行摘要。
        """
        cfg = self.build()
        jobs = sorted(cfg.get("jobs", {}))
        components = {k: sorted(v) for k, v in sorted(cfg.get("components", {}).items())}
        lines = [
            f"workspace_dir = {cfg.get('workspace_dir')}",
            f"service       = {cfg.get('service', {}).get('backend')}",
            f"embedding     = {self.embedding_dimensions if self.embedding_dimensions is not None else 'disabled'}",
            f"jobs({len(jobs)})    = {', '.join(jobs)}",
            "components:",
        ]
        for kind, names in components.items():
            lines.append(f"  {kind:<16} {', '.join(names)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _resolve_base(self) -> dict[str, Any]:
        """调用官方 ``resolve_app_config`` 拿到 default.yaml 的全量配置。

        Returns:
            `dict[str, Any]`: 合并了 ``default`` 配置的字典。

        Raises:
            `MemoryConfigError`: ``reme`` 不可导入。
        """
        try:
            from reme.config import resolve_app_config
        except ImportError as exc:
            raise MemoryConfigError(
                "无法导入 reme（需要 reme-ai==0.4.1.13 且 PYTHONPATH 指向 "
                "third_party/ReMe，否则会静默拿到 site-packages 里的旧版）",
            ) from exc

        if self._base_cache is not None:
            return copy.deepcopy(self._base_cache)

        cfg = resolve_app_config(
            workspace_dir=str(self.workspace.root),
            enable_logo=False,
            log_to_console=False,
            log_to_file=False,
        )
        if "jobs" not in cfg or not cfg["jobs"]:
            # 保险丝：这条断言就是"必须显式 resolve"这条约束的可执行形式。
            raise MemoryConfigError("resolve_app_config 返回的配置里没有 jobs，说明配置解析失败")
        self._base_cache = copy.deepcopy(cfg)
        return cfg

    def _apply_llm_credentials(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """把 Settings 里的 LLM 凭据显式写进 ``as_llm``。

        这是硬约束 2 的落地点。``default.yaml`` 只认 ``LLM_API_KEY`` /
        ``LLM_BASE_URL`` / ``LLM_MODEL_NAME`` / ``LLM_BACKEND``，
        而本仓库 ``.env`` 用的是 ``OPENAI_*`` / ``LLM_MODEL``；不显式覆盖就会
        在 ``Application.start()`` 里炸 ``Missing credentials``。

        Args:
            cfg (`dict[str, Any]`): 待覆盖的配置。

        Returns:
            `dict[str, Any]`: 覆盖后的新配置。
        """
        settings = self._settings()
        model = self.llm_model or getattr(settings, "llm_model_name", None)
        api_key = getattr(settings, "llm_api_key", None)
        base_url = getattr(settings, "llm_base_url", None)

        credential: dict[str, Any] = {}
        if api_key:
            credential["api_key"] = api_key
        if base_url:
            credential["base_url"] = base_url

        as_llm: dict[str, Any] = {}
        if model:
            as_llm["model"] = model
        if credential:
            as_llm["credential"] = credential

        if not as_llm:
            logger.warning(
                "Settings 里没有任何 LLM 凭据；as_llm 将沿用 default.yaml 的 "
                "${LLM_*}，若无环境变量则 Application.start() 会因缺少 api_key 失败",
            )
            return cfg
        return _deep_merge(cfg, {"components": {"as_llm": {"default": as_llm}}})

    def _apply_embedding(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """按 ``embedding_dimensions`` 决定是否装配向量组件（硬约束 3）。

        Args:
            cfg (`dict[str, Any]`): 待覆盖的配置。

        Returns:
            `dict[str, Any]`: 覆盖后的新配置。
        """
        if self.embedding_dimensions is None:
            return cfg

        overlay = {
            "components": {
                "as_embedding": {
                    "default": {
                        "backend": "openai",
                        "model": "harness-injected",
                        "dimensions": int(self.embedding_dimensions),
                        "credential": {"api_key": "", "base_url": ""},
                        "parameters": {},
                    },
                },
                "embedding_store": {
                    "default": {
                        "backend": "local",
                        "as_embedding": "default",
                        "enable_cache": True,
                        "max_cache_size": 3000,
                        "max_input_length": 8192,
                        "max_batch_size": 10,
                    },
                },
                "file_store": {"default": {"embedding_store": "default"}},
            },
        }
        merged = _deep_merge(cfg, overlay)
        logger.info(
            "已启用向量检索：embedding_dimensions={}（仍需在 start 前 "
            "update_component 注入真实 embedding model，否则向量路答空）",
            self.embedding_dimensions,
        )
        return merged

    @staticmethod
    def _apply_embedded_service(cfg: dict[str, Any]) -> dict[str, Any]:
        """把 service 换成不开端口的 ``cli`` 后端（见模块 docstring）。

        Args:
            cfg (`dict[str, Any]`): 待覆盖的配置。

        Returns:
            `dict[str, Any]`: 覆盖后的新配置。
        """
        service = dict(cfg.get("service") or {})
        service.update(
            {
                "backend": "cli",
                # cli 后端没有 host/port；显式抹掉 default.yaml 里的 http 参数，
                # 免得将来有人手滑改成 http 时继承到 8000 这种默认端口。
                "host": None,
                "port": None,
                "web_enabled": False,
                "mcp_enabled": False,
            },
        )
        return _deep_merge(cfg, {"service": service})

    def _apply_jobs(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """过滤 job、覆盖 ``reindex``、校验白名单。

        Args:
            cfg (`dict[str, Any]`): 待覆盖的配置。

        Returns:
            `dict[str, Any]`: 覆盖后的新配置。

        Raises:
            `MemoryConfigError`: 白名单里有 default.yaml 里不存在的 job。
        """
        jobs: dict[str, Any] = dict(cfg.get("jobs") or {})
        all_names = set(jobs)

        if not self.keep_background_jobs:
            dropped = sorted(name for name, spec in jobs.items() if spec.get("backend") not in EMBEDDED_JOB_BACKENDS)
            for name in dropped:
                jobs.pop(name, None)
            if dropped:
                logger.debug("嵌入式装配丢弃 {} 个常驻 job: {}", len(dropped), dropped)
            # 覆盖 reindex 为"重新扫描"版本（见 RESCAN_REINDEX_JOB 的说明）。
            jobs["reindex"] = copy.deepcopy(RESCAN_REINDEX_JOB)

        if self.job_whitelist is not None:
            unknown = sorted(set(self.job_whitelist) - all_names)
            if unknown:
                raise MemoryConfigError(
                    f"job 白名单里的名字在 ReMe 配置里不存在: {unknown}；"
                    f"可用: {sorted(all_names)}",
                )
            allowed = set(jobs)
            # 第二种"白名单失效"：名字本身拼对了，但因为后端是 background/cron
            # 刚被过滤掉。**必须单独报错**，否则 with_jobs("search", "dream_cron")
            # 会安静地退化成只保留 search —— 调用方以为自己开了 cron 记忆，
            # 其实什么都没开。这正是"静默吞掉一个 job 名"的另一半。
            filtered_out = sorted(name for name in self.job_whitelist if name not in allowed)
            if filtered_out:
                raise MemoryConfigError(
                    f"job 白名单里的名字因为后端是 background/cron 被过滤掉了: {filtered_out}；"
                    f"嵌入式装配只能保留 {sorted(EMBEDDED_JOB_BACKENDS)} 后端"
                    "（见 EMBEDDED_JOB_BACKENDS 的说明：这类 job 的 step 是长驻循环、"
                    "永不返回，前台 run_job 不给超时就会一直等下去）。"
                    "要保留请设 keep_background_jobs=True，"
                    "或把周期性动作交给 harness_kit.memory.jobs 的 asyncio 调度器。",
                )
            jobs = {name: jobs[name] for name in self.job_whitelist if name in allowed}

        # 注意：这里必须**整体替换** jobs，不能用 _deep_merge ——
        # 深合并会把我刚删掉的 background/cron job 从底稿里又留下来
        # （这正是初版实现的真实 bug，被 MemoryDoctor 的 config.jobs 检查项抓到）。
        result = dict(cfg)
        result["jobs"] = jobs
        return result

    def _settings(self) -> Any:
        """惰性拿到 Settings（涉及 dotenv 读取，所以不在 ``__init__`` 做）。

        Returns:
            `Any`: :class:`harness_kit.settings.Settings` 实例。
        """
        if self.settings is None:
            from ..settings import Settings

            self.settings = Settings.from_env()
        return self.settings


#: :class:`HarnessMemoryConfig.build` 支持的 job 后端语义（供教程引用）。
JobBackend = Literal["base", "stream", "background", "cron"]
