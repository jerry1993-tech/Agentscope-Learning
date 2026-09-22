# -*- coding: utf-8 -*-
"""嵌入式 ReMe 客户端：把 ``reme.ReMe`` 的生命周期与 ``run_job`` 收成一个小门面。

**为什么不写 HTTP 客户端**

ReMe 官方提供三种部署形态：``reme start`` 起 HTTP/MCP 服务、CLI、以及**进程内嵌入**。
harness_kit 一律选第三种，理由是可运维性而不是偏好：

- 没有端口就没有端口冲突（多个 agent 并发跑教程时尤其重要）；
- 没有跨进程边界，就不需要处理认证、重试、序列化；
- ``ReMe(**config)`` 只是个普通对象，生命周期跟着 Agent 进程走，好理解也好测试。

代价是 ReMe 的启动开销（BM25 载入、组件拓扑排序）落在调用方进程里，
所以本模块把 ``start()`` 做成**显式且幂等**的，并给它加超时。

**必须踩准的四个真实约束（全部实测过）**

1. ``run_job`` 的 ``name`` 是 **positional-only**
   （``third_party/ReMe/reme/application.py:370``：
   ``async def run_job(self, name: str, /, **kwargs) -> Response``）。
   写 ``run_job(name="search")`` 会 ``TypeError: got some positional-only arguments
   passed as keyword arguments``。所以 :meth:`MemoryClient.run_job` 的签名也把
   ``name`` 标成 positional-only —— 让类型检查器和调用方都在**编译期**就知道这件事。
2. **必须先 ``start()``**。跳过它，job 的 ``step_specs`` 是空的，
   ``BaseJob.__call__`` 遍历空列表后返回 ``success=True, answer="", metadata={}``
   —— 一个非常像成功的假成功（``third_party/ReMe/reme/components/job/base_job.py:36``
   在 ``_start`` 里才 ``self.step_specs = [...]``）。
   :class:`MemoryClient` 因此在 :meth:`run_job` 里硬性检查 :attr:`started`。
3. ``success=False`` **必须抛**。ReMe 把错误编码在 ``Response.success``
   （``third_party/ReMe/reme/schema/response.py:8``）里而不是抛异常，
   静默返回空 answer 会让检索失败伪装成"没搜到"。见 :class:`MemoryJobError`。
4. **``background`` / ``cron`` job 不能进嵌入式装配**。它们不会被 ``start()``
   挂住（本讲实测：只带 ``index_update_loop`` 时 ``start()`` 用 2.29s 正常返回，
   40 个 job 全开也只 0.04s），但它们的长驻 step 永不返回，``run_job`` 会把调用方
   吊死（实测 ``run_job("index_update_loop")`` 6s 超时仍不返回），
   而且 ``close()`` 要等 ``close_timeout``（默认 5s）才强杀。
   本模块把 ``start()`` 包上 ``asyncio.wait_for``，让任何挂起都变成
   **可诊断的超时**；真正的规避在 ``harness_kit/memory/config.py`` 的 job 过滤里。
"""

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any, Awaitable, Callable

from loguru import logger

__all__ = [
    "MemoryClient",
    "MemoryJobError",
    "MemoryUnavailableError",
    "REME_MIN_VERSION",
    "REME_PYTHONPATH_HINT",
    "REME_REQUIRED_VERSION",
    "build_memory_client",
    "reme_available",
    "reme_version",
]


#: harness_kit 对齐的 ReMe 版本。
REME_REQUIRED_VERSION: str = "0.4.1.13"

#: 低于这个版本一律视为"装错了"（site-packages 里的 0.3.1.10 就是这个坑）。
REME_MIN_VERSION: tuple[int, ...] = (0, 4, 1)

#: 排查提示。``import reme`` 会静默拿到 site-packages 里的旧版，
#: 所以这条提示要在**每一个**导入失败/版本不符的地方出现。
REME_PYTHONPATH_HINT: str = (
    "请设置 PYTHONPATH=third_party/ReMe 再用同一个解释器运行，例如：\n"
    "  PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \\\n"
    "    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python your_script.py\n"
    "（site-packages 里有一个旧的 reme 0.3.1.10 会抢先被 import）"
)


class MemoryUnavailableError(RuntimeError):
    """``reme`` 不可用（未安装 / 版本过旧）。优雅降级的统一出口。"""


class MemoryJobError(RuntimeError):
    """ReMe 的某个 job 返回了 ``success=False``（契约 §3.15）。"""

    def __init__(self, job: str, answer: str, metadata: dict[str, Any] | None = None) -> None:
        """记录失败的 job 名与 ReMe 给出的原因。

        Args:
            job (`str`): job 名。
            answer (`str`): ``Response.answer``，即 ReMe 的错误说明。
            metadata (`dict[str, Any] | None`): ``Response.metadata``。
        """
        super().__init__(f"ReMe job {job!r} failed: {answer}")
        self.job: str = job
        self.answer: str = answer
        self.metadata: dict[str, Any] = dict(metadata or {})


def reme_available() -> bool:
    """检测 ``reme`` 是否可导入且版本足够新。

    不抛异常，专供"优雅降级"分支使用（```doctor.py`` / CI 探测）。

    Returns:
        `bool`: 可导入且版本 ``>= REME_MIN_VERSION`` 时为 ``True``。
    """
    try:
        module = importlib.import_module("reme")
    except ImportError:
        return False
    return _parse_version(getattr(module, "__version__", "0")) >= REME_MIN_VERSION


def reme_version() -> str | None:
    """返回当前 ``import reme`` 解析到的版本号。

    Returns:
        `str | None`: 版本字符串；导入失败时 ``None``。
    """
    try:
        module = importlib.import_module("reme")
    except ImportError:
        return None
    return str(getattr(module, "__version__", ""))


def _parse_version(value: str) -> tuple[int, ...]:
    """把 ``"0.4.1.13"`` 解析成可比较的元组。

    Args:
        value (`str`): 版本字符串。

    Returns:
        `tuple[int, ...]`: 数字段元组；非数字段被忽略。
    """
    parts: list[int] = []
    for chunk in str(value).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def _require_reme() -> Any:
    """导入并校验 ``reme`` 模块本体与 ``ReMe`` 类。

    Returns:
        `Any`: 已导入的 ``reme`` 模块。

    Raises:
        `MemoryUnavailableError`: 未安装，或版本低于 :data:`REME_MIN_VERSION`。
    """
    try:
        module = importlib.import_module("reme")
    except ImportError as exc:
        raise MemoryUnavailableError(f"未安装 reme（reme-ai）。{REME_PYTHONPATH_HINT}") from exc

    version = _parse_version(getattr(module, "__version__", "0"))
    if version < REME_MIN_VERSION:
        raise MemoryUnavailableError(
            f"import reme 解析到的是 {getattr(module, '__version__', '?')} @ {getattr(module, '__file__', '?')}，"
            f"低于要求的 {REME_REQUIRED_VERSION}。{REME_PYTHONPATH_HINT}",
        )
    if not hasattr(module, "ReMe"):
        raise MemoryUnavailableError(
            f"reme {getattr(module, '__version__', '?')} 里没有 ReMe 类；"
            f"预期 third_party/ReMe/reme/reme.py 的 `class ReMe(Application)`",
        )
    return module


class MemoryClient:
    """嵌入式 ReMe 客户端（契约 §3.15）。

    职责只有四件事：**惰性构造 app**、**显式启动**、**带错误归一的 run_job**、
    **幂等关闭**。所有记忆语义都在 ReMe 的 job 里，这里不重复实现任何一条。

    Example::

        client = MemoryClient(HarnessMemoryConfig(workspace=ws).build())
        await client.start()
        try:
            resp = await client.run_job("search", query="部署令牌", limit=5)
        finally:
            await client.aclose()
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        start_timeout_s: float = 120.0,
        job_timeout_s: float | None = None,
    ) -> None:
        """保存配置，**不**构造 app（构造发生在 :meth:`start`）。

        Args:
            config (`dict[str, Any]`): 已经过 ``resolve_app_config`` 的全量配置，
                通常来自 :meth:`HarnessMemoryConfig.build`。
            start_timeout_s (`float`): ``start()`` 的超时秒数。超过就抛
                ``TimeoutError``。本讲实测正常 ``start()`` 在 0.04s～2.3s 量级
                （即使配置里带着 ``index_update_loop`` 也一样快），所以超时
                几乎总是意味着**初始化**卡死（某个组件的 ``_start`` 在等网络/端口），
                而不是"job 太多"。
            job_timeout_s (`float | None`): ``run_job`` 的默认超时；``None`` 不限。
        """
        self._config: dict[str, Any] = dict(config)
        self._app: Any | None = None
        self._start_lock: asyncio.Lock = asyncio.Lock()
        self._start_timeout_s: float = float(start_timeout_s)
        self._job_timeout_s: float | None = job_timeout_s
        self._jobs_module: Any | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> "MemoryClient":
        """构造并启动嵌入式 ReMe app（幂等，并发安全）。

        用 ``Application.start()``（``third_party/ReMe/reme/components/base_component.py:225``）
        而不是直接调 ``_start()``：``start()`` 会做 "失败则 rollback 已启动组件" 的清理，
        并把 ``is_started`` 置位，这正是我们幂等判断要用的状态。

        Returns:
            `MemoryClient`: ``self``，便于 ``await MemoryClient(cfg).start()``。

        Raises:
            `MemoryUnavailableError`: ``reme`` 不可导入或版本过旧。
            `TimeoutError`: 超过 ``start_timeout_s``。
        """
        if self._app is not None and getattr(self._app, "is_started", False):
            return self

        async with self._start_lock:
            if self._app is not None and getattr(self._app, "is_started", False):
                return self

            module = _require_reme()
            started_at = time.perf_counter()
            app = module.ReMe(**self._config)
            self._app = app
            try:
                await asyncio.wait_for(app.start(), timeout=self._start_timeout_s)
            except TimeoutError as exc:
                self._app = None
                raise TimeoutError(
                    f"ReMe app 启动超过 {self._start_timeout_s:.0f}s 未返回。"
                    "先看配置里是不是还有 background/cron job"
                    "（本讲实测 start() 本身不会因为它们挂住，但它们的 close 要等 "
                    "close_timeout、还可能把事件循环拖住）；"
                    "请用 HarnessMemoryConfig.build()，它默认过滤掉这两类 job。",
                ) from exc
            except Exception:
                # 启动失败时不要留下半启动的对象，否则 aclose() 会二次清理。
                self._app = None
                raise

            logger.info(
                "ReMe 嵌入式 app 已启动: jobs={} workspace={} elapsed={:.2f}s",
                len(getattr(app.context, "jobs", {})),
                self._config.get("workspace_dir"),
                time.perf_counter() - started_at,
            )
            return self

    async def aclose(self) -> None:
        """关闭 app（幂等）。未启动时是 no-op。"""
        app = self._app
        self._app = None
        if app is None:
            return
        try:
            await app.close()
        except Exception as exc:  # pragma: no cover - 关闭失败不该炸主流程
            logger.warning("关闭 ReMe app 时出错（已忽略）: {}", exc)

    async def __aenter__(self) -> "MemoryClient":
        """支持 ``async with MemoryClient(cfg) as client:``。

        Returns:
            `MemoryClient`: 已启动的客户端。
        """
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出上下文时关闭 app。

        Args:
            exc_type (`Any`): 异常类型。
            exc (`Any`): 异常实例。
            tb (`Any`): traceback。
        """
        await self.aclose()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def started(self) -> bool:
        """app 是否已启动（契约 §3.15）。

        Returns:
            `bool`: 已启动为 ``True``。
        """
        return self._app is not None and bool(getattr(self._app, "is_started", False))

    @property
    def application(self) -> Any:
        """底层 ``reme.ReMe`` 对象（只读逃生口，给需要直接摸 ReMe 的高级用法）。

        Returns:
            `Any`: 已构造的 app。

        Raises:
            `MemoryUnavailableError`: app 还没构造（没调 ``start()``）。
        """
        if self._app is None:
            raise MemoryUnavailableError("MemoryClient 尚未 start()，底层 app 还不存在")
        return self._app

    @property
    def config(self) -> dict[str, Any]:
        """构造这个 app 用的配置（只读副本）。

        Returns:
            `dict[str, Any]`: 配置的浅拷贝。
        """
        return dict(self._config)

    @property
    def workspace_dir(self) -> str:
        """工作区根目录。

        Returns:
            `str`: 绝对路径字符串。
        """
        return str(self._config.get("workspace_dir", ""))

    def job_names(self) -> list[str]:
        """当前 app 里注册的 job 名（未启动时返回配置里的名字）。

        这是一个**真实反映**的查询：ReMe 的 job 只在 ``Application.__init__`` 里注册，
        ``start()`` 只是把每个 job 的 ``step_specs`` 建起来，所以两边一致。

        Returns:
            `list[str]`: 排序后的 job 名。
        """
        if self._app is not None:
            return sorted(getattr(self._app.context, "jobs", {}))
        return sorted((self._config.get("jobs") or {}).keys())

    def component(self, component_type: str, name: str = "default") -> Any:
        """取一个已启动的组件（如 ``file_store`` / ``keyword_index`` / ``tag_index``）。

        Args:
            component_type (`str`): 组件类型，如 ``"file_store"``。
            name (`str`): 组件实例名，默认 ``"default"``。

        Returns:
            `Any`: 组件对象。

        Raises:
            `MemoryUnavailableError`: app 未启动。
            `KeyError`: 组件不存在。
        """
        context = self.application.context
        group = getattr(context, "components", {}).get(component_type)
        if not group or name not in group:
            available = {k: sorted(v) for k, v in getattr(context, "components", {}).items()}
            raise KeyError(f"组件 {component_type}:{name} 不存在；可用: {available}")
        return group[name]

    # ------------------------------------------------------------------
    # job 执行
    # ------------------------------------------------------------------
    async def run_job(self, name: str, /, *, timeout_s: float | None = None, **kwargs: Any) -> Any:
        """执行一个 job 并在 ``success=False`` 时抛异常（契约 §3.15）。

        Args:
            name (`str`): job 名。**positional-only**，与 ReMe 的签名逐字一致。
            timeout_s (`float | None`): 本次调用的超时；``None`` 时用
                ``__init__`` 的 ``job_timeout_s``。
            **kwargs (`Any`): job 参数，直接透传给 ``Application.run_job``。

        Returns:
            `Any`: ReMe 的 ``Response``（``answer`` / ``success`` / ``metadata``）。

        Raises:
            `MemoryUnavailableError`: app 未启动，或 job 名不存在。
            `MemoryJobError`: ``Response.success is False``。
            `TimeoutError`: 超过超时。
        """
        if not self.started:
            raise MemoryUnavailableError(
                "run_job 前必须 await client.start()；跳过 start() 会得到 "
                "success=True / answer='' 的假成功（BaseJob 的 step_specs 是空的）",
            )
        app = self.application
        if name not in app.context.jobs:
            raise MemoryUnavailableError(f"ReMe 里没有 job {name!r}；可用: {self.job_names()}")

        effective = timeout_s if timeout_s is not None else self._job_timeout_s
        started_at = time.perf_counter()
        try:
            if effective is None:
                response = await app.run_job(name, **kwargs)
            else:
                response = await asyncio.wait_for(app.run_job(name, **kwargs), timeout=float(effective))
        except TimeoutError as exc:
            raise TimeoutError(f"ReMe job {name!r} 超过 {effective}s 未返回") from exc

        logger.debug(
            "run_job {} success={} elapsed={:.0f}ms",
            name,
            getattr(response, "success", True),
            (time.perf_counter() - started_at) * 1000,
        )
        if getattr(response, "success", True) is False:
            raise MemoryJobError(
                name,
                str(getattr(response, "answer", "")),
                dict(getattr(response, "metadata", {}) or {}),
            )
        return response

    async def run_job_raw(self, name: str, /, *, timeout_s: float | None = None, **kwargs: Any) -> Any:
        """:meth:`run_job` 的**不抛**版本，给"允许失败"的场景用（如 nightly 归档）。

        Args:
            name (`str`): job 名（positional-only）。
            timeout_s (`float | None`): 超时。
            **kwargs (`Any`): job 参数。

        Returns:
            `Any`: ReMe 的 ``Response``；失败也照样返回。
        """
        try:
            return await self.run_job(name, timeout_s=timeout_s, **kwargs)
        except (MemoryJobError, TimeoutError, MemoryUnavailableError) as exc:
            logger.warning("run_job_raw 吞掉了一次失败: {}", exc)
            return _FailureResponse(str(exc))

    def jobs(self) -> Any:
        """返回 :class:`~harness_kit.memory.jobs.MemoryJobs` 门面（惰性导入避免环）。

        Returns:
            `Any`: ``MemoryJobs(self)``。
        """
        if self._jobs_module is None:
            from .jobs import MemoryJobs

            self._jobs_module = MemoryJobs(self)
        return self._jobs_module

    def bind(self, factory: Callable[["MemoryClient"], Awaitable[Any]]) -> Awaitable[Any]:
        """把 ``self`` 交给一个异步工厂（调用方自己 await）。

        Args:
            factory (`Callable[[MemoryClient], Awaitable[Any]]`): 异步工厂。

        Returns:
            `Awaitable[Any]`: 未 await 的协程。
        """
        return factory(self)


class _FailureResponse:
    """``run_job_raw`` 失败时返回的最小 Response 替身（只读）。"""

    __slots__ = ("answer", "success", "metadata")

    def __init__(self, answer: str) -> None:
        """构造一个 ``success=False`` 的响应。

        Args:
            answer (`str`): 失败原因。
        """
        self.answer: str = answer
        self.success: bool = False
        self.metadata: dict[str, Any] = {}


async def build_memory_client(spec: Any, *, ctx: Any | None = None, **kwargs: Any) -> MemoryClient:
    """``HarnessRegistry`` 的 ``memory:reme`` 工厂（已启动的客户端）。

    签名对齐 ``registry.py:1026-1030`` 的登记项：
    ``attrs=("build_memory_client", "MemoryClient")``，类目 ``memory`` 期望
    ``async build_xxx(spec: MemorySpec) -> Any``，并允许额外的 ``ctx`` 关键字。

    Args:
        spec (`Any`): ``MemorySpec``。
        ctx (`Any | None`): 可选 ``BuildContext``（取其 ``settings``）。
        **kwargs (`Any`): 透传给 :meth:`MemoryClient.__init__`。

    Returns:
        `MemoryClient`: **已经 start() 过**的客户端（工厂语义 = 拿到的就是可用的）。

    Raises:
        `MemoryConfigError`: 配置构建失败。
        `MemoryUnavailableError`: ``reme`` 不可用。
    """
    from .config import HarnessMemoryConfig

    builder = HarnessMemoryConfig.from_spec(spec, ctx=ctx)
    # Profile 里显式列了 job 就尊重它，否则用内嵌全量（已过滤 background/cron）。
    whitelist = tuple(getattr(spec, "jobs", []) or ())
    if whitelist:
        builder = builder.with_jobs(*whitelist)
    client = MemoryClient(builder.build(), **kwargs)
    await client.start()
    return client
