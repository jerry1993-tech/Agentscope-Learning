# -*- coding: utf-8 -*-
"""后台 / cron job 的封装（契约 §3.18）。

**这一层要解决的是一个真实的事故模式：假的成功**

ReMe 的 ``BaseJob._start``（``third_party/ReMe/reme/components/job/base_job.py:59``）
在 app 启动时把每个 step 的类与构造参数解析并缓存在 ``self.step_specs`` 里：

.. code-block:: python

    async def _start(self) -> None:
        if self.app_context is None:
            raise RuntimeError(...)
        self.step_specs = [self._resolve_step(raw) for raw in self.step_configs]

而 ``BaseJob.__call__``（``base_job.py:86-98``）执行的是 ``self._build_steps()``，
它遍历的正是 ``step_specs``：

.. code-block:: python

    for step in self._build_steps():
        await step(context)

所以**如果 job 没有 start 过，``step_specs`` 是空列表，循环体一次都不进，
``Response.success`` 保持默认的 ``True``、``answer`` 是空串、``metadata`` 是空字典** ——
调用方拿到的是一个"成功"的空壳。这就是契约里那条
"跳过 ``await app._start()`` 会得到 ``success=True / answer="" / metadata={}`` 的假成功"。
:class:`~harness_kit.memory.client.MemoryClient.run_job` 因此在入口硬性检查
``started``（``client.py:396-400``），本模块只是在这层保护之上再加两道：

1. **超时必填**：``run_once`` 的 ``timeout_s`` 默认 60 秒且不允许 ``None``。
2. **常驻 job 拒绝执行**：``background`` / ``cron`` 后端的 job 前台 ``run_job``
   会永久等待（它们的 step 是长驻循环 —— 典型如
   ``async for raw_changes in awatch(...)``，
   ``third_party/ReMe/reme/steps/index/watch_changes.py:93``；
   只在 ``stop_event`` 被设置时才退出，而前台调用没有人会去设它）。
   :meth:`MemoryJobs.run_once` 在调用前读 ``job.backend`` 并直接拒绝。

**为什么"拒绝"而不是"帮它加上超时"**

``asyncio.wait_for`` 能取消协程，但取消的落点不在调用方的控制里：
``BackgroundJob`` 被取消时会走它自己的清理路径 ——
``_close``（``third_party/ReMe/reme/components/job/background_job.py:67``）
先 set ``stop_event``（``:69``）唤醒长驻循环，再由 ``_shutdown_task``
（``:73``）等 ``close_timeout``（``:80``）、等不到才 ``cancel``（``:82``）。
而且它背后还挂着一个 ``ThreadPoolExecutor``
（``third_party/ReMe/reme/application.py:192``）。
既然这些 job 的正确用法是"由一个进程专门跑"（``_run_with_supervisor``
本来就会在崩溃后按指数退避重启，
``third_party/ReMe/reme/components/job/background_job.py:105-122``），
那么在嵌入式场景里**拒绝它、并指出替代品**
（``dream_cron`` → ``auto_dream``，``index_update_loop`` → ``reindex``）
比"帮你挂上去再超时"更诚实：超时只是把"等不到结果"变成"等 60 秒才等到
一个假结果"，而拒绝能在调用点就说清该换哪个 job。

**未验证**：取消 ``BackgroundJob`` 之后进程能否干净退出，本讲没有做逐项测量
（只测了 ``close()`` 的耗时：带一个 background job 时 1.01s、40 个 job 全开 3.03s）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from loguru import logger

from .client import MemoryClient, MemoryJobError, MemoryUnavailableError

if TYPE_CHECKING:  # pragma: no cover - 只有类型检查器会进来
    from reme.schema import Response

__all__ = [
    "DEFAULT_JOB_TIMEOUT_S",
    "RESIDENT_BACKENDS",
    "JobTimeout",
    "MemoryJobs",
]

#: ``run_once`` 的默认超时（秒）。契约 §3.18 写的就是 60.0。
DEFAULT_JOB_TIMEOUT_S: float = 60.0

#: 会常驻（不会自然结束）的 job 后端。取值来自
#: ``third_party/ReMe/reme/components/job/`` 下的三个实现类
#: （``background_job.py`` / ``cron_job.py``；``stream_job.py`` 不在其列，
#: 它由 HTTP 服务驱动，嵌入式场景下没有 ``enable_serve`` 的 job 根本不会被拉起）。
RESIDENT_BACKENDS: frozenset[str] = frozenset({"background", "cron"})

#: 常驻 job → 它的前台等价物（用于报错信息里给出可执行的替代方案）。
_RESIDENT_ALTERNATIVES: dict[str, str] = {
    "index_update_loop": "reindex()",
    "resource_watch_loop": "MemoryIngestor.add_directory(...) 然后 reindex()",
    "digest_watch_loop": "reindex()",
    "dream_cron": "MemoryMaintainer.auto_memory(...) 或 auto_dream",
    "optimize_index_cron": "reindex()",
    "proactive_refresh_cron": "proactive_refresh",
}


class JobTimeout(RuntimeError):
    """job 超过超时未返回（契约 §3.18）。

    继承 ``RuntimeError`` 而不是 ``TimeoutError``：``TimeoutError`` 在
    ``asyncio`` 里被当作"取消传播"的一等公民，``except TimeoutError``
    会把 harness 自己的超时和任何底层库的超时混在一起。
    本类额外带上 job 名与超时值，便于日志里直接读出"谁卡了多久"。
    """

    def __init__(self, job: str, timeout_s: float) -> None:
        """记录是哪一次调用超时。

        Args:
            job (`str`): job 名。
            timeout_s (`float`): 超时秒数。
        """
        super().__init__(f"ReMe job {job!r} 超过 {timeout_s}s 未返回")
        self.job: str = job
        self.timeout_s: float = float(timeout_s)


class MemoryJobs:
    """job 门面（契约 §3.18）。

    Example::

        jobs = client.jobs()              # 或 MemoryJobs(client)
        print(jobs.available())
        resp = await jobs.reindex()
        resp = await jobs.run_once("search", query="部署令牌", limit=3)
        results = await jobs.run_all(["daily_list", "list_tags"])

    与 :meth:`MemoryClient.run_job` 的分工：``run_job`` 是**单次**、**抛异常**的
    原语；本类是**带超时、带可运行性检查、成批执行**的策略层。
    """

    def __init__(self, client: MemoryClient) -> None:
        """绑定一个已启动（或即将启动）的客户端。

        Args:
            client (`MemoryClient`): 客户端。本类**不**替它 ``start()`` ——
                生命周期由调用方管，这样同一个 client 上可以并存多个门面。
        """
        self.client = client

    # ------------------------------------------------------------------
    # 单个 job
    # ------------------------------------------------------------------
    async def run_once(
        self,
        job: str,
        *,
        timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
        **kwargs: Any,
    ) -> "Response":
        """前台执行一个 job，带超时与常驻检查（契约 §3.18）。

        Args:
            job (`str`): job 名。
            timeout_s (`float`): 超时秒数，必须为正；**不接受 ``None``**。
                理由：不加超时的嵌入式 job 一旦挂起，整个进程就再也不会前进，
                而"挂起"在本环境里是**预期行为**（background job）。
            **kwargs (`Any`): 透传给 ``Application.run_job`` 的参数。

        Returns:
            `Response`: ReMe 的响应（``answer`` / ``success`` / ``metadata``）。

        Raises:
            `JobTimeout`: 超过 ``timeout_s``。
            `ValueError`: ``timeout_s`` 不是正数。
            `MemoryUnavailableError`: app 未启动，或 job 不存在。
            `MemoryJobError`: ``Response.success`` 为 ``False``。
        """
        if timeout_s is None or float(timeout_s) <= 0:
            raise ValueError(f"timeout_s 必须是正数，收到 {timeout_s!r}")
        self._assert_runnable(job)
        try:
            return await self.client.run_job(job, timeout_s=float(timeout_s), **kwargs)
        except TimeoutError as exc:
            raise JobTimeout(job, float(timeout_s)) from exc

    async def run_all(self, jobs: Sequence[str]) -> dict[str, "Response"]:
        """逐个前台执行，**失败不中断**（契约 §3.18）。

        用 ``MemoryClient.run_job_raw``（``client.py:422``）：它把
        ``MemoryJobError`` / ``TimeoutError`` / ``MemoryUnavailableError``
        统一转成 ``success=False`` 的响应。原因：一批 job 里有一个失败
        （比如 ``daily_list`` 那天没有日记），不代表其余的不该跑完；
        而"跑了一半抛异常"会让调用方既拿不到后面 job 的结果、
        也说不清前面哪些已经产生了副作用。

        Args:
            jobs (`Sequence[str]`): job 名序列。

        Returns:
            `dict[str, Response]`: job 名 → 响应（含失败项，``success=False``）。
        """
        results: dict[str, "Response"] = {}
        for name in jobs:
            try:
                self._assert_runnable(name)
            except MemoryUnavailableError as exc:
                logger.warning("run_all 跳过 {!r}: {}", name, exc)
                results[name] = _failed_response(str(exc))
                continue
            results[name] = await self.client.run_job_raw(
                name,
                timeout_s=DEFAULT_JOB_TIMEOUT_S,
            )
            if not getattr(results[name], "success", True):
                logger.warning("run_all: job {!r} 失败: {}", name, getattr(results[name], "answer", ""))
        return results

    # ------------------------------------------------------------------
    # 常用 job
    # ------------------------------------------------------------------
    async def reindex(
        self,
        *,
        watch_dirs: Sequence[str] | None = None,
        watch_suffixes: Sequence[str] | None = None,
    ) -> "Response":
        """重建索引（契约 §3.18）。

        **为什么不能直接调 ReMe 原生的 ``reindex``**

        ReMe 自带的 ``reindex`` job 是 ``steps: [{"backend": "reindex_step"}]``
        （``third_party/ReMe/reme/steps/index/reindex.py:7``），它假设
        ``file_store`` 里已经有 chunk，只重建派生索引。如果文件从未被 ingest
        （harness 的写入路径是"直接 upsert"，不经监视循环），
        原生 ``reindex`` 会返回"0 个文件被重建" —— 又是一个不报错的空动作。

        harness 的解法与 AgentScope 官方一致：把 ``reindex`` 覆盖成
        **重新扫描**版本（:data:`~harness_kit.memory.config.RESCAN_REINDEX_JOB`，
        ``clear_store_step`` + ``init_changes_step`` + ``update_index_step``）。
        这个覆盖发生在 :meth:`HarnessMemoryConfig.build` 里，
        所以本方法只需要调 ``"reindex"``；如果得到的 job 不是重新扫描版，
        说明 config 不是 harness 造的，此时记一条 warning 而不是假装没事。

        ``watch_dirs`` / ``watch_suffixes`` 默认走 job 配置里的值
        （harness 造的 config 里是 ``daily_dir`` / ``digest_dir`` / ``resource_dir``
        加 ``md``，见 :data:`~harness_kit.memory.config.RESCAN_REINDEX_JOB`）。
        调用方给了就以调用方的为准 —— ``BaseJob.__call__`` 是
        ``{**job.kwargs, **call_kwargs}``（``base_job.py:87``），调用时传的键胜出。
        **不要传空列表**：那等于"没有要扫描的目录"，而重扫的第一步是
        ``clear_store_step``，结果是把索引清空且不重建（实测就是这样，
        见 RESCAN_REINDEX_JOB 的注释）。

        Args:
            watch_dirs (`Sequence[str] | None`): 要扫描的目录（配置字段名或绝对路径）；
                ``None`` = 用 job 配置。
            watch_suffixes (`Sequence[str] | None`): 只看这些后缀；``None`` = 用 job 配置。

        Returns:
            `Response`: ReMe 的响应；``metadata["counts"]`` 里带
            ``added`` / ``modified`` / ``deleted`` 三个计数 —— 重扫完请核对它们，
            而不是只看 ``success=True``。

        Raises:
            `MemoryUnavailableError`: config 里没有 ``reindex`` job
                （``with_jobs`` 白名单没包含它）。
            `JobTimeout`: 超时。
        """
        self._warn_if_not_rescan("reindex")
        payload: dict[str, Any] = {}
        if watch_dirs is not None:
            payload["watch_dirs"] = [str(item) for item in watch_dirs]
        if watch_suffixes is not None:
            payload["watch_suffixes"] = [str(item) for item in watch_suffixes]
        return await self.run_once("reindex", **payload)

    async def daily_list(self, date: str | None = None) -> "Response":
        """列出某一天的记忆卡（契约 §3.18）。

        对应 ``daily_list_step``（``third_party/ReMe/reme/steps/file_io/daily_list.py``）：
        它的 ``metadata["notes"]`` 是 ``[{**front_matter, "path": ...}, ...]``。

        Args:
            date (`str | None`): ``YYYY-MM-DD``；``None`` 时用今天。

        Returns:
            `Response`: ``metadata["notes"]`` / ``metadata["count"]``。

        Raises:
            `MemoryUnavailableError`: 没有 ``daily_list`` job。
            `JobTimeout`: 超时。
        """
        payload: dict[str, Any] = {}
        if date:
            payload["date"] = str(date)
        return await self.run_once("daily_list", **payload)

    # ------------------------------------------------------------------
    # 发现与诊断
    # ------------------------------------------------------------------
    def available(self) -> list[str]:
        """列出可用 job 名（来自已启动的 app，或来自配置）。

        Returns:
            `list[str]`: 已排序的 job 名。
        """
        return list(self.client.job_names())

    def backend_of(self, job: str) -> str:
        """读一个 job 的后端名（``"base"`` / ``"background"`` / ``"cron"`` / ...）。

        Args:
            job (`str`): job 名。

        Returns:
            `str`: 后端名；job 不存在或 app 未启动时返回空串。
        """
        if not self.client.started:
            return ""
        instance = self.client.application.context.jobs.get(job)
        return str(getattr(instance, "backend", "") or "")

    def describe(self) -> str:
        """渲染一张 job 清单（名字、后端、是否可直接前台执行）。

        Returns:
            `str`: 多行文本。
        """
        lines = ["MemoryJobs:"]
        for name in self.available():
            backend = self.backend_of(name) or "?"
            resident = "常驻，禁止前台执行" if backend in RESIDENT_BACKENDS else "可前台执行"
            lines.append(f"  - {name:24s} backend={backend:11s} {resident}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _assert_runnable(self, job: str) -> None:
        """确认 job 存在且不是常驻 job。

        Args:
            job (`str`): job 名。

        Raises:
            `MemoryUnavailableError`: app 未启动、job 不存在、或是常驻 job。
        """
        if not self.client.started:
            raise MemoryUnavailableError(
                f"run job {job!r} 前必须先 await client.start()",
            )
        if job not in self.client.application.context.jobs:
            raise MemoryUnavailableError(
                f"ReMe 里没有 job {job!r}；可用: {self.available()}。"
                "若这是一个被 HarnessMemoryConfig 过滤掉的 job，"
                "请在装配期用 with_jobs(...) 把它加入白名单。",
            )
        backend = self.backend_of(job)
        if backend in RESIDENT_BACKENDS:
            alternative = _RESIDENT_ALTERNATIVES.get(job, "对应的前台 job")
            raise MemoryUnavailableError(
                f"job {job!r} 的后端是 {backend!r}，它是常驻 job："
                "它的 step 是一个长驻循环，前台 run_job 会一直等到超时（等不到结果）。"
                f"替代方案：{alternative}。",
            )

    def _warn_if_not_rescan(self, job: str) -> None:
        """检查 ``reindex`` 是不是"重新扫描"版本。

        Args:
            job (`str`): job 名。
        """
        if not self.client.started:
            return
        instance = self.client.application.context.jobs.get(job)
        if instance is None:
            return
        names = [getattr(spec[0], "__name__", "") for spec in (getattr(instance, "step_specs", []) or [])]
        if "reindex_step" in names and "clear_store_step" not in names:
            logger.warning(
                "reindex job 是 ReMe 原生版本（reindex_step），它只重建派生索引，"
                "不会重新扫描文件；harness 的 RESCAN_REINDEX_JOB 才能扫出新文件。"
                "当前 config 可能不是 HarnessMemoryConfig.build() 造的。",
            )


def _failed_response(answer: str) -> Any:
    """造一个最小的失败响应替身（与 ``MemoryClient`` 内部用的形状一致）。

    Args:
        answer (`str`): 失败说明。

    Returns:
        `Any`: 带 ``answer`` / ``success`` / ``metadata`` 的对象。
    """
    from .client import _FailureResponse

    return _FailureResponse(answer)
