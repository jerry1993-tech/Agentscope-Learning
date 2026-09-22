# -*- coding: utf-8 -*-
"""记忆子系统体检：版本、配置可解析性、backend 是否全部登记、工作区可写。

**为什么需要它**

AgentScope 与 ReMe 的装配失败方式**全都不是异常，而是静默降级**：

=============================== ================================================
看起来正常的现象               真正的病因
=============================== ================================================
``success=True, answer=""``    漏了 ``await app.start()``（job 的 step_specs 为空）
``search`` 返回 0 条但没报错    文件从没被 ingest（``reindex`` 只重建已入库的 chunk）
``import reme`` 拿到旧版        ``PYTHONPATH`` 没设，site-packages 里的 0.3.1.10 抢先
``ValueError: Unregistered backend`` AgentScope 的 ``_dream_steps()`` 引用了比 0.4.1.13 更新的 step
``ImportError`` 起不来          ``Missing credentials``：``LLM_*`` 四个环境变量名字对不上
=============================== ================================================

:class:`MemoryDoctor` 把这些"沉默的失败"变成一张**逐项带结论的检查表**，
而且每一项都能回答 "怎么修"（:attr:`CheckResult.hint`）。

**检查项一览**（全部只读，不会创建端口、不会改工作区）

1. ``reme.version`` —— ``import reme`` 解析到的版本与路径是不是 ``third_party/ReMe``。
2. ``reme.path`` —— ``import reme`` 解析到的 ``__file__`` 是不是 ``third_party/ReMe``
   （这是 ``PYTHONPATH`` 有没有生效的直接证据）。
3. ``config.parse`` —— 配置能不能被 ``reme.schema.ApplicationConfig`` 校验通过。
4. ``config.service`` —— service 后端是不是不开端口的 ``cli``（嵌入式硬约束）。
5. ``config.jobs`` —— 是否混进了 ``background`` / ``cron``（它们的 step 是长驻循环，
   前台 ``run_job`` 不给超时就会一直等下去；``close()`` 还要等 ``close_timeout``）。
6. ``registry.jobs`` —— 每个 job 的 backend 都在 ``ComponentRegistry`` 里登记了吗。
7. ``registry.steps`` —— 每个 job 的每一步 backend 都登记了吗（``dream_topics_step`` 就死在这）。
8. ``registry.components`` —— 每个组件 backend 都登记了吗。
9. ``workspace.dirs`` —— 六个子目录是否齐全（缺了会照实报，不自动创建）。
10. ``workspace.writable`` —— 能不能真的写进去。
11. ``agentscope.version`` —— AgentScope 版本（第 19 讲的中间件依赖 2.0.8）。
12. ``agentscope.reme_middleware`` —— 官方 ``ReMeMiddleware`` 是否可导入，
    并**预先**报告 ``dream_topics_step`` 兼容缺口。

对齐的真实 API：

- ``third_party/ReMe/reme/components/component_registry.py:14`` ``ComponentRegistry``
  / ``:151`` ``create_application_registry``
- ``third_party/ReMe/reme/schema/application_config.py:29`` ``ApplicationConfig``
- ``third_party/ReMe/reme/enumeration/`` 的 ``ComponentEnum``
  （``enumeration/component_enum.py:6``）与 ``component_type_name``
  （``enumeration/component_type.py:12``）—— 注意 ``enumeration`` 是**包**不是单文件
- ``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_config.py:54``
  ``_dream_steps``（兼容缺口的源头）
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from .client import REME_PYTHONPATH_HINT, REME_REQUIRED_VERSION, _parse_version
from .config import EMBEDDED_JOB_BACKENDS

__all__ = ["CheckResult", "MemoryDoctor"]


class CheckResult(BaseModel):
    """单项体检结果（契约 §3.15）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """检查项名，形如 ``"registry.jobs"``。"""

    ok: bool
    """是否通过。"""

    detail: str = ""
    """结论说明（正常时说清"正常在哪"，失败时说清"错在哪"）。"""

    hint: str = ""
    """修复建议；无建议时为空串。"""


#: ``(组件类目, 配置里的键名)``。键名与 ``ApplicationConfig.components`` 的一级键一致，
#: 也和 ``ComponentEnum`` 的值一致（``component_type_name`` 做了一层归一）。
_COMPONENT_SECTIONS: tuple[str, ...] = (
    "tokenizer",
    "as_llm",
    "as_embedding",
    "embedding_store",
    "agent_wrapper",
    "file_graph",
    "file_catalog",
    "file_chunker",
    "keyword_index",
    "tag_index",
    "file_store",
)


class MemoryDoctor:
    """ReMe 记忆子系统体检器（契约 §3.15）。

    体检**不需要**启动 app，因此可以安全地跑在 CI、容器启动探针、
    以及"配置改完先别跑、先 doctor 一下"的工作流里。

    Example::

        doctor = MemoryDoctor(HarnessMemoryConfig(workspace=ws).build())
        for result in doctor.check():
            print(result.ok, result.name, result.detail)
        print(doctor.report())
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """保存待体检的配置。

        Args:
            config (`dict[str, Any]`): ReMe app config（通常是 ``build()`` 的产物）。
        """
        self._config: dict[str, Any] = copy.deepcopy(config)

    @property
    def config(self) -> dict[str, Any]:
        """被测配置的深拷贝（避免体检过程污染调用方）。

        Returns:
            `dict[str, Any]`: 配置副本。
        """
        return copy.deepcopy(self._config)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def check(self) -> list[CheckResult]:
        """跑完所有检查项。

        Returns:
            `list[CheckResult]`: 结果列表；``ok=False`` 的项按严重度靠前。
        """
        results: list[CheckResult] = []
        module, import_result = self._check_reme_import()
        results.append(import_result)

        if module is not None:
            results.append(self._check_reme_path(module))

        results.append(self._check_config_parse())
        results.append(self._check_service())
        results.append(self._check_no_resident_jobs())
        results.extend(self._check_registry())
        results.append(self._check_workspace_dirs())
        results.append(self._check_workspace_writable())
        results.extend(self._check_agentscope())
        return results

    def report(self) -> str:
        """把 :meth:`check` 的结果渲染成人类可读的多行报告。

        Returns:
            `str`: 形如 ``"[OK]   reme.version  ..."`` 的报告；末尾给一行汇总。
        """
        results = self.check()
        lines: list[str] = []
        failed = 0
        for result in results:
            mark = "OK  " if result.ok else "FAIL"
            if not result.ok:
                failed += 1
            lines.append(f"[{mark}] {result.name:<28} {result.detail}")
            if not result.ok and result.hint:
                for hint_line in result.hint.splitlines():
                    lines.append(f"       -> {hint_line}")
        total = len(results)
        lines.append("")
        lines.append(f"合计 {total} 项，通过 {total - failed} 项，失败 {failed} 项")
        return "\n".join(lines)

    def ok(self) -> bool:
        """是否全部通过。

        Returns:
            `bool`: 全过为 ``True``。
        """
        return all(result.ok for result in self.check())

    # ------------------------------------------------------------------
    # 单项检查
    # ------------------------------------------------------------------
    def _check_reme_import(self) -> tuple[Any | None, CheckResult]:
        """检查 ``reme`` 能否导入且版本足够。

        Returns:
            `tuple[Any | None, CheckResult]`: (reme 模块或 None, 结果)。
        """
        try:
            import importlib

            module = importlib.import_module("reme")
        except ImportError as exc:
            return None, CheckResult(
                name="reme.available",
                ok=False,
                detail=f"import reme 失败: {exc}",
                hint=REME_PYTHONPATH_HINT,
            )
        version = str(getattr(module, "__version__", "?"))
        ok = _parse_version(version) >= (0, 4, 1)
        return module, CheckResult(
            name="reme.version",
            ok=ok,
            detail=f"reme {version} @ {getattr(module, '__file__', '?')}（要求 {REME_REQUIRED_VERSION}）",
            hint="" if ok else REME_PYTHONPATH_HINT,
        )

    @staticmethod
    def _check_reme_path(module: Any) -> CheckResult:
        """检查 ``reme`` 是否解析到了 ``third_party/ReMe``（而不是 site-packages）。

        判定方式是看 ``reme.__file__`` 里有没有 ``third_party/ReMe``；
        这是个启发式，但它抓的正是那个真实存在过的坑。

        Args:
            module (`Any`): 已导入的 ``reme`` 模块。

        Returns:
            `CheckResult`: 结果。
        """
        path = str(getattr(module, "__file__", ""))
        in_repo = "third_party/ReMe" in path
        return CheckResult(
            name="reme.path",
            ok=in_repo,
            detail=path,
            hint="" if in_repo else REME_PYTHONPATH_HINT,
        )

    def _check_config_parse(self) -> CheckResult:
        """用 ReMe 自己的 pydantic 模型校验配置。

        Returns:
            `CheckResult`: 结果。
        """
        try:
            from reme.schema import ApplicationConfig
        except ImportError as exc:
            return CheckResult(
                name="config.parse",
                ok=False,
                detail=f"无法导入 reme.schema.ApplicationConfig: {exc}",
                hint=REME_PYTHONPATH_HINT,
            )
        try:
            cfg = ApplicationConfig(**self._config)
        except Exception as exc:
            return CheckResult(
                name="config.parse",
                ok=False,
                detail=f"ApplicationConfig 校验失败: {type(exc).__name__}: {exc}",
                hint="检查 config 里是否有非法字段名或非法值（如 session_dir 写成绝对路径）",
            )
        return CheckResult(
            name="config.parse",
            ok=True,
            detail=f"ApplicationConfig 校验通过；jobs={len(cfg.jobs)} components={len(cfg.components)} 类目",
        )

    def _check_service(self) -> CheckResult:
        """检查 service 后端是不是不开端口的 ``cli``。

        Returns:
            `CheckResult`: 结果。
        """
        backend = str((self._config.get("service") or {}).get("backend", ""))
        ok = backend == "cli"
        return CheckResult(
            name="config.service",
            ok=ok,
            detail=f"service.backend = {backend!r}",
            hint=""
            if ok
            else (
                "嵌入式装配应使用 service.backend='cli'"
                "（third_party/ReMe/reme/components/service/cli_service.py:62）。"
                "用 HarnessMemoryConfig.build() 会自动设置。"
                "注意：只要不调 run_app() 就不会真的占端口，但显式写 cli 能让这件事在配置里可见。"
            ),
        )

    def _check_no_resident_jobs(self) -> CheckResult:
        """检查是否混进了 ``background`` / ``cron`` job。

        Returns:
            `CheckResult`: 结果。
        """
        jobs = self._config.get("jobs") or {}
        resident = sorted(name for name, spec in jobs.items() if spec.get("backend") not in EMBEDDED_JOB_BACKENDS)
        ok = not resident
        return CheckResult(
            name="config.jobs",
            ok=ok,
            detail=f"{len(jobs)} 个 job；常驻 job: {resident or '无'}",
            hint=""
            if ok
            else (
                "background/cron job 的 step 是长驻循环（watch_changes_step 里的 "
                "``async for ... in awatch(...)`` 永不返回），run_job 会把调用方吊死；"
                "它们的 close 还要等 close_timeout。请用 HarnessMemoryConfig.build()"
                "（默认过滤），或把周期性动作交给 "
                "harness_kit.memory.jobs.MemoryJobs 的 asyncio 调度器。"
            ),
        )

    def _check_registry(self) -> list[CheckResult]:
        """检查所有 job / step / component 的 backend 都在注册表里。

        Returns:
            `list[CheckResult]`: 三项结果（registry.jobs / registry.steps / registry.components）。
        """
        try:
            from reme.components.component_registry import create_application_registry
            from reme.enumeration import ComponentEnum
        except ImportError as exc:  # pragma: no cover - reme 不可用
            return [
                CheckResult(
                    name="registry.jobs",
                    ok=False,
                    detail=f"无法导入 ComponentRegistry: {exc}",
                    hint=REME_PYTHONPATH_HINT,
                ),
            ]

        registry = create_application_registry()

        missing_jobs: list[str] = []
        missing_steps: list[str] = []
        for name, spec in (self._config.get("jobs") or {}).items():
            backend = str(spec.get("backend", ""))
            if backend and registry.get(ComponentEnum.JOB, backend) is None:
                missing_jobs.append(f"{name}({backend})")
            for step in spec.get("steps") or []:
                if isinstance(step, dict):
                    step_backend = str(step.get("backend", ""))
                else:
                    step_backend = str(getattr(step, "backend", ""))
                if step_backend and registry.get(ComponentEnum.STEP, step_backend) is None:
                    missing_steps.append(f"{name}->{step_backend}")

        missing_components: list[str] = []
        for section, group in (self._config.get("components") or {}).items():
            if section not in _COMPONENT_SECTIONS:
                continue
            for comp_name, spec in (group or {}).items():
                backend = str((spec or {}).get("backend", ""))
                if backend and registry.get(section, backend) is None:
                    missing_components.append(f"{section}:{comp_name}({backend})")

        results = [
            CheckResult(
                name="registry.jobs",
                ok=not missing_jobs,
                detail=f"未登记: {missing_jobs}" if missing_jobs else "全部登记",
                hint="" if not missing_jobs else "这些 job backend 在 ReMe 注册表里不存在；检查拼写或版本",
            ),
            CheckResult(
                name="registry.steps",
                ok=not missing_steps,
                detail=f"未登记: {missing_steps}" if missing_steps else "全部登记",
                hint=""
                if not missing_steps
                else (
                    "报 'ValueError: Unregistered backend X of type ComponentEnum.STEP' 的就是这一类。"
                    "典型例子：AgentScope 2.0.8 的 _config._dream_steps() 引用了 "
                    "dream_topics_step，而 reme 0.4.1.13 没注册它 —— 需要用 "
                    "harness_kit.memory.middleware.ensure_reme_compat() 打兼容补丁。"
                ),
            ),
            CheckResult(
                name="registry.components",
                ok=not missing_components,
                detail=f"未登记: {missing_components}" if missing_components else "全部登记",
                hint="" if not missing_components else "检查组件 backend 名与 ReMe 版本",
            ),
        ]
        return results

    def _check_workspace_dirs(self) -> CheckResult:
        """检查六个子目录是否齐全（只读，不创建）。

        Returns:
            `CheckResult`: 结果。
        """
        from pathlib import Path

        root = Path(str(self._config.get("workspace_dir", "")))
        if not str(root):
            return CheckResult(name="workspace.dirs", ok=False, detail="配置里没有 workspace_dir")
        if not root.is_dir():
            return CheckResult(
                name="workspace.dirs",
                ok=False,
                detail=f"工作区根不存在: {root}",
                hint="先调用 ReMeWorkspace(root=...).ensure()，或直接 await MemoryClient(config).start()"
                "（Application.__init__ 会自动建目录）",
            )
        missing: list[str] = []
        for key in ("metadata_dir", "session_dir", "mem_session_dir", "resource_dir", "daily_dir", "digest_dir"):
            name = str(self._config.get(key, ""))
            if name and not (root / name).is_dir():
                missing.append(name)
        return CheckResult(
            name="workspace.dirs",
            ok=not missing,
            detail=f"缺少: {missing}" if missing else f"六个子目录齐全 @ {root}",
            hint="" if not missing else "ReMeWorkspace(root=...).ensure() 会一次性补齐",
        )

    def _check_workspace_writable(self) -> CheckResult:
        """检查工作区可写（写一个探针文件再删掉）。

        Returns:
            `CheckResult`: 结果。
        """
        from pathlib import Path

        root = Path(str(self._config.get("workspace_dir", "")))
        if not root.is_dir():
            return CheckResult(
                name="workspace.writable",
                ok=False,
                detail="工作区根不存在，无法判断可写性",
                hint="先 ensure() 工作区",
            )
        probe = root / ".harness_doctor_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            return CheckResult(
                name="workspace.writable",
                ok=False,
                detail=f"写入失败: {exc}",
                hint="检查目录权限；容器里常见原因是挂载了只读卷",
            )
        return CheckResult(name="workspace.writable", ok=True, detail=f"可写 @ {root}")

    def _check_agentscope(self) -> list[CheckResult]:
        """检查 AgentScope 侧：版本、官方 ``ReMeMiddleware`` 可导入性、兼容缺口。

        Returns:
            `list[CheckResult]`: 两项或三项结果。
        """
        results: list[CheckResult] = []
        try:
            import agentscope
        except ImportError as exc:
            return [
                CheckResult(
                    name="agentscope.version",
                    ok=False,
                    detail=f"import agentscope 失败: {exc}",
                    hint="pip install -e third_party/agentscope",
                ),
            ]
        version = str(getattr(agentscope, "__version__", "?"))
        results.append(
            CheckResult(
                name="agentscope.version",
                ok=version.startswith("2."),
                detail=f"agentscope {version}",
                hint="" if version.startswith("2.") else "本契约对齐 2.0.8；1.x 没有 middleware 子包",
            ),
        )

        try:
            from agentscope.middleware._longterm_memory._reme import _config as reme_config
        except ImportError as exc:
            results.append(
                CheckResult(
                    name="agentscope.reme_middleware",
                    ok=False,
                    detail=f"官方 ReMeMiddleware 的 _config 不可导入: {exc}",
                    hint="需要 agentscope 2.0.8（third_party/agentscope）",
                ),
            )
            return results

        missing = self._dream_topics_missing(reme_config)
        results.append(
            CheckResult(
                name="agentscope.reme_middleware",
                ok=not missing,
                detail=(
                    "_dream_steps() 引用了 reme 未注册的 dream_topics_step，"
                    "直接 start() 会 ValueError: Unregistered backend"
                    if missing
                    else "官方 ReMeMiddleware 与本地 reme 的 dream 流水线兼容"
                ),
                hint=""
                if not missing
                else (
                    "调用 harness_kit.memory.middleware.ensure_reme_compat() 即可（幂等，"
                    "只替换 agentscope 模块里的 _dream_steps 函数对象，不改 third_party 文件）"
                ),
            ),
        )
        return results

    @staticmethod
    def _dream_topics_missing(reme_config: Any) -> bool:
        """判断本地 ReMe 是否缺 ``dream_topics_step``。

        Args:
            reme_config (`Any`): ``agentscope..._reme._config`` 模块。

        Returns:
            `bool`: 缺了（= 需要兼容补丁）为 ``True``。
        """
        steps_builder: Callable[[], list[dict[str, Any]]] | None = getattr(reme_config, "_dream_steps", None)
        if steps_builder is None:
            return False
        try:
            steps = steps_builder()
        except Exception:  # pragma: no cover - 结构变了
            return False
        names = {str(step.get("backend", "")) for step in steps if isinstance(step, dict)}
        if "dream_topics_step" not in names:
            return False
        try:
            from reme.components.component_registry import create_application_registry
            from reme.enumeration import ComponentEnum
        except ImportError:  # pragma: no cover
            return True
        registry = create_application_registry()
        return registry.get(ComponentEnum.STEP, "dream_topics_step") is None

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------
    def failures(self) -> list[CheckResult]:
        """只返回失败的项。

        Returns:
            `list[CheckResult]`: 失败项。
        """
        return [result for result in self.check() if not result.ok]

    def log(self) -> None:
        """把报告按 ``WARNING``/``INFO`` 打到 loguru。

        全部通过时用 INFO，有失败项时用 WARNING —— 这样容器日志里
        ``grep WARNING`` 就能捞到配置问题。
        """
        results = self.check()
        failures = [result for result in results if not result.ok]
        if failures:
            logger.warning("记忆子系统体检未通过：\n{}", self.report())
        else:
            logger.info("记忆子系统体检通过（{} 项）", len(results))
