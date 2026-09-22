# -*- coding: utf-8 -*-
"""自演化入口：``auto_memory`` / ``auto_dream`` / ``auto_resource``（契约 §3.18）。

**为什么需要一层，而不是直接 ``run_job("auto_memory", ...)``**

``AutoMemoryStep.execute``（``third_party/ReMe/reme/steps/evolve/auto_memory.py:332-486``）
在**不同分支**里往 ``metadata`` 里放的东西**不一样**：

=================================== ==========================================
分支产生的 ``metadata`` 键
=================================== ==========================================
正常完成（新建或更新）              ``date`` / ``path`` / ``created`` /
                                    ``modified`` / ``n_messages`` /
                                    ``source_conversation`` / ``index``
没有任何消息                        ``date`` / ``modified`` / ``n_messages``
                                    —— **没有 ``path``**
创建后 re-query 没找到笔记          ``date`` / ``path=None`` / ``created=False``
                                    / ``modified`` / ``n_messages``
re-query 或改名抛异常               ``success=False`` + 部分键
=================================== ==========================================

于是"调用方想问的那个问题"（**这一轮记忆到底变了吗？**）在 ``Response`` 上
没有任何一个字段能直接回答：``success=True`` 可能是"更新了"、也可能是
"模型觉得不值得记"。:class:`MaintenanceResult` 就是那个缺失的枚举，
:class:`MemoryMaintainer` 是"从 metadata 的三四种形状里把它读出来"的那段逻辑。

**update 分支的 ``_allowed_paths``：一条被覆盖的约束**

``AutoMemoryStep`` 在 update 分支里会**自己**设一条请求级约束
（``auto_memory.py:414-416``）：

.. code-block:: python

    if not created:
        reply_kwargs["injected_job_kwargs"] = {"_allowed_paths": [note_path]}

而 create 分支**不设**。这意味着：

- update 分支：模型只能改那一条已有笔记，改不动别的文件（ReMe 自己的保护）；
- create 分支：模型用 ``daily_write`` 自己选路径落笔，**没有**约束。

契约的 ``allowed_paths`` 参数因此有两种合理语义，harness 选的是**调用方约束**
那一侧，并且用**事后校验**落地它（见 :meth:`MemoryMaintainer._enforce_allowed`）：
job 跑完之后检查产物路径是否落在 ``allowed_paths`` 里，不在就报 ``failed``。
之所以不做"事前注入"：``_allowed_paths`` 是**请求级**的运行时键，
``BaseJob.__call__``（``base_job.py:86-88``）每次都用 ``RuntimeContext(**merged)``
造一个**新的** context，所以塞进 ``auto_memory`` 这个 job 的 kwargs 里
**不会**流到它内部工具调用（``daily_write`` / ``edit``）的 job 上去；
真正能传过去的只有 ``injected_job_kwargs``，而那是 step 内部构造的。
事后校验至少给出一个**确定的**结论：路径不合约 → ``failed``，调用方必须处理。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from .client import MemoryClient, MemoryJobError

__all__ = [
    "AUTO_DREAM_JOB",
    "AUTO_MEMORY_JOB",
    "AUTO_RESOURCE_JOB",
    "FRONTMATTER_UPDATE_JOB",
    "MaintenanceResult",
    "MemoryMaintainer",
    "MemoryMaintenanceScheduler",
    "NightlyReport",
    "SESSION_END_EVENT_NAME",
]

#: 会话蒸馏 job（``config/default.yaml`` 的 ``jobs.auto_memory``）。
AUTO_MEMORY_JOB: str = "auto_memory"

#: 夜间整合 job（``jobs.auto_dream``，前台版本；``dream_cron`` 是它的常驻版本）。
AUTO_DREAM_JOB: str = "auto_dream"

#: 资源加工 job（``jobs.auto_resource``），``resource_watch_loop`` 的前台等价物。
AUTO_RESOURCE_JOB: str = "auto_resource"

#: front matter 更新 job（``jobs.frontmatter_update``）。
FRONTMATTER_UPDATE_JOB: str = "frontmatter_update"

#: ``auto_memory`` 的 ``metadata`` 里代表"这一轮有没有落盘"的关键键。
_ACTION_CREATED = "created"
_ACTION_UPDATED = "updated"
_ACTION_SKIPPED = "skipped"
_ACTION_FAILED = "failed"


class MaintenanceResult(BaseModel):
    """一次自演化动作的结果（契约 §3.18）。

    Attributes:
        action (`Literal["created","updated","skipped","failed"]`):
            ``"created"`` 新建了记忆文件；
            ``"updated"`` 改动了已有记忆文件；
            ``"skipped"`` **跑完了但什么都没变**（模型判断不值得记、
            或者更新后内容与原来一致）；
            ``"failed"`` 失败（job 报错，或产物路径违反了 ``allowed_paths``）。
        path (`str | None`): 受影响的文件（工作区相对路径）。
            ``"skipped"`` 时**可能是** ``None``（ReMe 在"没有消息"分支里根本不写
            ``path``），也可能是具体路径（更新了但没有实质变化）。
        detail (`str`): 人读的说明。失败时是错误原文；
            成功时是 ReMe 的 ``answer``（内部 agent 的回复文本）；
            ``auto_dream`` 时是整段摘要。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "updated", "skipped", "failed"]
    path: str | None = None
    detail: str = ""

    @property
    def changed(self) -> bool:
        """这次动作是否真的改动了磁盘。

        Returns:
            `bool`: ``action`` 是 ``"created"`` 或 ``"updated"``。
        """
        return self.action in (_ACTION_CREATED, _ACTION_UPDATED)


class MemoryMaintainer:
    """自演化入口（契约 §3.18）。

    Example::

        maintainer = MemoryMaintainer(client)
        result = await maintainer.auto_memory(
            session_id="s-1", msgs=agent.state.context,
        )
        print(result.action, result.path)

        await maintainer.update_frontmatter(
            path=result.path, name="token-rotation", description="令牌轮换流程",
        )
    """

    def __init__(self, client: MemoryClient) -> None:
        """绑定客户端。

        Args:
            client (`MemoryClient`): 已 start 的客户端。
        """
        self.client = client

    # ------------------------------------------------------------------
    # 会话蒸馏
    # ------------------------------------------------------------------
    async def auto_memory(
        self,
        *,
        session_id: str,
        msgs: list[Any],
        allowed_paths: list[str] | None = None,
        memory_hint: str | None = None,
        date: str | None = None,
    ) -> MaintenanceResult:
        """跑一轮会话蒸馏，并把结果翻译成 :class:`MaintenanceResult`（契约 §3.18）。

        Args:
            session_id (`str`): 会话 id；空值直接返回 ``failed``
                （不抛异常 —— 契约的方法签名把失败也建模成结果枚举，
                所以这里把"参数错"和"job 报错"统一成 ``failed``，
                错误原文进 ``detail``）。
            msgs (`list[Any]`): 会话消息（``Msg`` 或 dict）。
            allowed_paths (`list[str] | None`): 只允许产物落在这些工作区相对路径里。
                ``None`` = 不限制。语义与实现方式见模块 docstring。
            memory_hint (`str | None`): 透传给 ReMe 的额外提示。
            date (`str | None`): ``YYYY-MM-DD``。

        Returns:
            `MaintenanceResult`: 结果枚举 + 路径 + 说明。
        """
        from .distill import SessionDistiller

        caller = str(session_id or "").strip()
        if not caller:
            return MaintenanceResult(
                action=_ACTION_FAILED,
                path=None,
                detail="session_id 不能为空（AutoMemoryStep 直接判失败，auto_memory.py:367-370）",
            )

        payload: dict[str, Any] = {}
        if memory_hint:
            payload["memory_hint"] = str(memory_hint)
        if date:
            payload["date"] = str(date)

        try:
            shaped = SessionDistiller.shape_messages(msgs)
            response = await self.client.run_job(
                AUTO_MEMORY_JOB,
                messages=shaped,
                session_id=caller,
                **payload,
            )
        except ValueError as exc:  # 整形失败
            return MaintenanceResult(action=_ACTION_FAILED, path=None, detail=str(exc))
        except MemoryJobError as exc:
            return MaintenanceResult(action=_ACTION_FAILED, path=None, detail=str(exc))

        metadata = dict(getattr(response, "metadata", None) or {})
        answer = str(getattr(response, "answer", "") or "")
        path = metadata.get("path")
        relative = str(path) if path else None
        created = bool(metadata.get("created", False))
        modified = bool(metadata.get("modified", False))

        if created:
            action = _ACTION_CREATED
        elif modified:
            action = _ACTION_UPDATED
        else:
            action = _ACTION_SKIPPED

        if allowed_paths is not None:
            violation = self._enforce_allowed(relative, allowed_paths)
            if violation:
                logger.warning("auto_memory: {}", violation)
                return MaintenanceResult(action=_ACTION_FAILED, path=relative, detail=violation)

        if action == _ACTION_SKIPPED and relative is None:
            # 这一支最常见的原因是"模型判断这轮不值得记"。把它说清楚，
            # 否则调用方会把 skipped 误当成"job 没跑"。
            detail = answer or "没有产生记忆变更（模型判断本轮无需落盘，或输入为空）"
        else:
            detail = answer
        logger.info("auto_memory: session={!r} action={} path={}", caller, action, relative)
        return MaintenanceResult(action=action, path=relative, detail=detail)

    # ------------------------------------------------------------------
    # front matter
    # ------------------------------------------------------------------
    async def update_frontmatter(
        self,
        *,
        path: str,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """更新一条记忆的 front matter（契约 §3.18）。

        对应 ``frontmatter_update_step``（``third_party/ReMe/reme/steps/file_io/frontmatter_update.py``）：
        它读运行时的 ``path`` 与 ``metadata`` 两个键，把 ``metadata`` 里的键
        合并进文件的 YAML front matter。失败时它设 ``success=False``，
        于是 ``client.run_job`` 抛 :class:`~harness_kit.memory.client.MemoryJobError`
        并带上 ``metadata["error"]`` 里的原因（路径越界 / 文件不是 markdown /
        文件不存在 / 没有要更新的字段）。

        ``name`` 与 ``description`` 是 ``frontmatter.py:83`` 定义的
        **一等键**（``_FIRST_CLASS = ("name", "description")``）——
        它们决定笔记在索引与后续 rename 里的显示名，所以 harness 把这两个
        单独提成参数，而不是让调用方自己拼 ``metadata`` 字典。

        Args:
            path (`str`): 工作区相对路径。
            name (`str | None`): 设置 ``name`` 键。
            description (`str | None`): 设置 ``description`` 键。

        Raises:
            `ValueError`: ``path`` 为空，或两个字段都没给。
            `MemoryJobError`: ReMe 侧更新失败（原因在 ``metadata["error"]``）。
            `MemoryUnavailableError`: app 未启动或没有该 job。
        """
        target = str(path or "").strip()
        if not target:
            raise ValueError("update_frontmatter 需要非空 path")
        metadata: dict[str, str] = {}
        if name is not None:
            metadata["name"] = str(name)
        if description is not None:
            metadata["description"] = str(description)
        if not metadata:
            raise ValueError("update_frontmatter 至少要给 name 或 description 之一")

        await self.client.run_job(
            FRONTMATTER_UPDATE_JOB,
            path=target,
            metadata=metadata,
        )
        logger.info("update_frontmatter: {} ← {}", target, sorted(metadata))

    # ------------------------------------------------------------------
    # 其它自演化 job
    # ------------------------------------------------------------------
    async def auto_dream(
        self,
        *,
        date: str | None = None,
        hint: str | None = None,
        scan_days: int | None = None,
        max_units: int | None = None,
    ) -> MaintenanceResult:
        """跑一轮夜间整合（``auto_dream`` job，``dream_cron`` 的前台等价物）。

        **与 :meth:`auto_memory` 的差别**：dream 的产物是 ``digest/`` 下的
        摘要节点，ReMe 只在 ``metadata["modified"]`` 里给一个布尔，
        **不**给具体路径。所以本方法返回的 ``path`` 恒为 ``None``，
        整段摘要进 ``detail``（``DreamFinishStep`` 把摘要写进 ``answer``，
        ``third_party/ReMe/reme/steps/evolve/dream/finish.py:56``）。
        假装能给路径是错的；要逐文件核对请读 ``digest/`` 目录或 catalog。

        Args:
            date (`str | None`): ``YYYY-MM-DD`` 扫描终点；``None`` = 今天。
            hint (`str | None`): 透传给 dream 的引导语。
            scan_days (`int | None`): 往前扫几天，默认 2。
            max_units (`int | None`): 最多抽几条记忆单元，默认 5。

        Returns:
            `MaintenanceResult`: ``action`` 为 ``updated`` / ``skipped`` / ``failed``。
        """
        payload: dict[str, Any] = {}
        if date:
            payload["date"] = str(date)
        if hint:
            payload["hint"] = str(hint)
        if scan_days is not None:
            payload["scan_days"] = int(scan_days)
        if max_units is not None:
            payload["max_units"] = int(max_units)

        try:
            response = await self.client.run_job(AUTO_DREAM_JOB, **payload)
        except MemoryJobError as exc:
            return MaintenanceResult(action=_ACTION_FAILED, path=None, detail=str(exc))

        metadata = dict(getattr(response, "metadata", None) or {})
        answer = str(getattr(response, "answer", "") or "")
        modified = bool(metadata.get("modified", False))
        action = _ACTION_UPDATED if modified else _ACTION_SKIPPED
        logger.info("auto_dream: date={!r} action={}", date, action)
        return MaintenanceResult(action=action, path=None, detail=answer)

    async def auto_resource(self, changes: Sequence[dict[str, Any]]) -> MaintenanceResult:
        """把一批资源变更交给 ``auto_resource`` job 处理。

        这是 ``resource_watch_loop``（常驻）的**前台等价物**：
        ``config/default.yaml`` 里那个 background job 做的是
        "监视目录 → 把变更批次交给 ``auto_resource_step``"，
        而嵌入式场景下"监视"由调用方做（比如 ingest 之后调一次本方法）。

        Args:
            changes (`Sequence[dict[str, Any]]`): 变更批次，每项形如
                ``{"path": "resource/a.md", "change": "added"}``；
                ``change`` 取 ``added`` / ``modified`` / ``deleted``。

        Returns:
            `MaintenanceResult`: ``action`` 为 ``updated`` / ``skipped`` / ``failed``。
        """
        batch = [dict(item) for item in (changes or ())]
        if not batch:
            return MaintenanceResult(action=_ACTION_SKIPPED, path=None, detail="空变更批次，未调用 job")
        try:
            response = await self.client.run_job(AUTO_RESOURCE_JOB, changes=batch)
        except MemoryJobError as exc:
            return MaintenanceResult(action=_ACTION_FAILED, path=None, detail=str(exc))
        metadata = dict(getattr(response, "metadata", None) or {})
        answer = str(getattr(response, "answer", "") or "")
        modified = bool(metadata.get("modified", False))
        action = _ACTION_UPDATED if modified else _ACTION_SKIPPED
        logger.info("auto_resource: {} 项变更 → {}", len(batch), action)
        return MaintenanceResult(action=action, path=None, detail=answer)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _enforce_allowed(relative: str | None, allowed_paths: Sequence[str]) -> str | None:
        """事后校验产物路径是否在允许清单内。

        比较用**字符串规范化**（去空白、反斜杠转正斜杠、去掉 ``./`` 前缀），
        不做文件系统解析：产物路径本来就是工作区相对路径，而
        ``Path.resolve()`` 会把"还不存在的文件"也解出结果，
        从而掩盖"路径写错了"这种错误。

        Args:
            relative (`str | None`): 产物路径（``None`` 表示没有产物）。
            allowed_paths (`Sequence[str]`): 允许的路径清单。

        Returns:
            `str | None`: 违约说明；合规时返回 ``None``。
        """
        if relative is None:
            # 没有产物 = 没有越界。是否需要"必须产出"是调用方的策略，
            # 不是 allowed_paths 这个约束能表达的。
            return None
        allowed = {_normalize_path(item) for item in (allowed_paths or ())}
        if not allowed:
            return f"allowed_paths 为空，但 job 产出了 {relative!r}"
        if _normalize_path(relative) not in allowed:
            return (
                f"产物路径 {relative!r} 不在 allowed_paths {sorted(allowed)} 内。"
                "注意 auto_memory 的 update 分支会用 ReMe 自己算出的 note_path 覆盖"
                "请求级 _allowed_paths（auto_memory.py:414-416），"
                "所以越界既可能来自模型选错了路径，也可能来自 ReMe 的改名逻辑。"
            )
        return None


def _normalize_path(value: Any) -> str:
    """把路径规范成可比较的字符串。

    Args:
        value (`Any`): 路径。

    Returns:
        `str`: 规范化的相对路径字符串。
    """
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


#: 触发「会话结束」落记忆的事件名。
#:
#: harness_kit 用 ``EventKind.CUSTOM`` 承载它
#: （``harness_kit/events/types.py`` 的 ``CUSTOM`` 分支约定 ``payload`` 里有
#: ``name`` 与 ``data`` 两个键），**不新造一个 EventKind**：契约 §5.2 的
#: ``EventKind`` 是封闭枚举，多一个值会让所有按枚举穷举的消费者失效。
SESSION_END_EVENT_NAME: str = "session_end"


class NightlyReport(BaseModel):
    """一夜的例行维护结果（本讲新增，契约外）。

    Attributes:
        date (`str | None`): 本次维护覆盖的日期（``YYYY-MM-DD``）；``None`` = 由 ReMe 取今天。
        reindex_ok (`bool`): ``reindex`` job 是否成功返回（失败不中断，见
            :meth:`MemoryMaintenanceScheduler.nightly`）。
        reindex_counts (`dict[str, int]`): ``reindex`` 的
            ``metadata["counts"]``（``added`` / ``modified`` / ``deleted``）。
            **必须看这三个数**：``success=True`` 且三者全 0，在"索引被清空后再也没装回来"
            这个真实事故里就是这么表现的（见 ``HarnessMemoryConfig`` 的
            ``RESCAN_REINDEX_JOB`` 注释）。
        dream (`MaintenanceResult | None`): ``auto_dream`` 的结果；
            ``nightly(dream=False)`` 时为 ``None``。
    """

    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    reindex_ok: bool = False
    reindex_counts: dict[str, int] = Field(default_factory=dict)
    dream: MaintenanceResult | None = None

    def summary(self) -> str:
        """渲染一行摘要（给日志与验证脚本用）。

        Returns:
            `str`: 一句话说明这一夜发生了什么。
        """
        counts = self.reindex_counts or {}
        reindex = (
            f"reindex ok added={counts.get('added', 0)} "
            f"modified={counts.get('modified', 0)} deleted={counts.get('deleted', 0)}"
            if self.reindex_ok
            else "reindex FAILED"
        )
        if self.dream is None:
            dream = "dream skipped"
        else:
            dream = f"dream {self.dream.action}"
        return f"{self.date or '<today>'}: {reindex}; {dream}"


class MemoryMaintenanceScheduler:
    """把「会话结束」事件接成一轮 ``auto_memory``（本讲新增，契约外）。

    **它补的是哪一段**

    第 9 讲把每个会话变成一条不可变事件流（``EventBus`` + ``SessionStore``），
    但那条流只回答"发生过什么"；:class:`MemoryMaintainer` 能把一段对话变成记忆卡，
    却必须有人**在正确的时刻**调它。本类就是那根线：

    .. code-block:: text

        AgentScope Agent.reply()  ──► EventRecord(kind=CUSTOM,
                                                  payload={"name": "session_end"})
                                              │
                                     EventBus.publish(kind, record)
                                              │
                                     on_event(record)          ← 本类
                                              │
                                     flush(session_id)         ← 本类
                                              │
                          msgs_of(session_id)  ──►  list[Msg]（由调用方提供）
                                              │
                                     MemoryMaintainer.auto_memory(...)

    三个刻意的设计：

    1. **消息由 ``msgs_of`` 注入，不由本类去读会话存储**。
       第 9 讲的 ``SessionStore`` 存的是 **事件**（``EventRecord``），不是完整对话：
       ``PAYLOAD_FIELDS`` 里只有 ``REPLY_START.input_preview``（截断到 500 字符）
       带着人话，用它重建对话等于把记忆建在摘要上。真实的消息在
       ``AgentState.context`` 里，取它的正确姿势是
       :meth:`~harness_kit.memory.distill.SessionDistiller.messages_from_agent`。
       所以本类只要求一个 ``async (session_id) -> list[Msg]`` 函数，
       把"消息从哪来"留在调用方 —— 这也让单元测试可以零 LLM 跑通。
    2. **事件只是触发器，不是数据源**。``payload`` 里只读两个东西：
       ``name``（判断是不是本类的目标事件）与可选的 ``data["catalog"]``。
       事件负载**不参与**记忆内容 —— 否则"事件流被裁剪过"会静默改变记忆。
    3. **``min_messages`` 在调用 job 之前拦截**。``auto_memory`` 对空输入是
       一次完整的 LLM 往返（``AutoMemoryStep`` 走完 agent_wrapper），
       而"用户只说了句你好就关窗口"是高频事件。在本地挡住它，省下的不只是钱，
       还避免了当日笔记被一串无意义的"寒暄"污染。

    Example::

        scheduler = MemoryMaintenanceScheduler(
            MemoryMaintainer(client),
            msgs_of=lambda sid: SessionDistiller.messages_from_agent(agent),
        )
        bus = EventBus()
        scheduler.attach(bus)                    # 订阅 CUSTOM
        await bus.start()
        ...                                      # Agent 跑一轮
        await bus.publish(EventKind.CUSTOM, EventRecord(
            session_id="s-1", seq=9, kind=EventKind.CUSTOM,
            payload={"name": "session_end", "data": {}},
        ))
        await bus.drain()
        print(scheduler.stats())                 # {'triggered': 1, 'delivered': 1, ...}
    """

    def __init__(
        self,
        maintainer: MemoryMaintainer,
        *,
        msgs_of: Callable[[str], Awaitable[list[Any]]] | None = None,
        allowed_paths: list[str] | None = None,
        min_messages: int = 1,
        event_name: str = SESSION_END_EVENT_NAME,
        metrics: Any | None = None,
    ) -> None:
        """配置调度器。

        Args:
            maintainer (`MemoryMaintainer`): 真正干活的维护入口。
            msgs_of (`Callable[[str], Awaitable[list[Any]]] | None`):
                ``async (session_id) -> list[Msg | dict]``。``None`` 时
                :meth:`flush` 会在**不调用 job**的前提下返回 ``failed``
                并说明原因（"没有消息来源"是一个配置错误，不是"这个会话没内容"）。
            allowed_paths (`list[str] | None`): 透传给
                :meth:`MemoryMaintainer.auto_memory` 的产物路径白名单。
            min_messages (`int`): 少于这么多条消息就跳过；必须为正。
            event_name (`str`): 触发用的 ``CUSTOM`` 事件名。
            metrics (`Any | None`): 可选的
                :class:`~harness_kit.memory.metrics.MemoryMetrics`；
                给了就记 ``record_writeback``。

        Raises:
            `ValueError`: ``min_messages`` 不是正数，或 ``event_name`` 为空。
        """
        if int(min_messages) <= 0:
            raise ValueError(f"min_messages 必须为正，收到 {min_messages}")
        name = str(event_name or "").strip()
        if not name:
            raise ValueError("event_name 不能为空")
        self.maintainer = maintainer
        self.msgs_of = msgs_of
        self.allowed_paths = list(allowed_paths) if allowed_paths is not None else None
        self.min_messages: int = int(min_messages)
        self.event_name: str = name
        self.metrics = metrics
        self._subscription: Any | None = None
        self._counters: dict[str, int] = {
            "triggered": 0,
            "delegated": 0,
            "delivered": 0,
            "skipped": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------
    # 事件接线
    # ------------------------------------------------------------------
    def attach(self, bus: Any) -> Any:
        """把本调度器接到一个 :class:`~harness_kit.events.bus.EventBus` 上。

        订阅的是 ``EventKind.CUSTOM``（**不是** ``"*"``）：``REPLY_END``
        每轮都发，而一个会话里可能有几十轮 —— 挂在那里等于每轮都试着重写一遍
        同一天的笔记。``CUSTOM`` 只在调用方显式宣告"这个会话结束了"时才来。

        Args:
            bus (`Any`): ``EventBus``。传 ``None`` 会 ``TypeError``。

        Returns:
            `Any`: ``Subscription`` 句柄（调用方想停就 ``unsubscribe()``）。

        Raises:
            `TypeError`: ``bus`` 没有 ``subscribe`` 方法。
        """
        if not hasattr(bus, "subscribe"):
            raise TypeError(f"attach 需要一个 EventBus（有 subscribe 方法），收到 {type(bus).__name__}")
        from ..events.types import EventKind

        self._subscription = bus.subscribe(EventKind.CUSTOM, self.on_event)
        logger.info(
            "memory scheduler 已挂到事件总线: event_name={!r} min_messages={}",
            self.event_name,
            self.min_messages,
        )
        return self._subscription

    def detach(self) -> None:
        """注销订阅（幂等）。"""
        if self._subscription is None:
            return
        self._subscription.unsubscribe()
        self._subscription = None

    async def on_event(self, record: Any) -> MaintenanceResult | None:
        """``EventBus`` 的 handler：只处理本类的目标事件。

        Args:
            record (`Any`): :class:`~harness_kit.events.types.EventRecord`。

        Returns:
            `MaintenanceResult | None`: 触发了就返回维护结果；
            不是目标事件时返回 ``None``（**不是** ``skipped`` —— 那会污染统计，
            让"这条事件跟我无关"看起来像"这个会话没内容可记"）。
        """
        from ..events.types import EventKind

        if getattr(record, "kind", None) != EventKind.CUSTOM:
            return None
        payload = dict(getattr(record, "payload", None) or {})
        if str(payload.get("name", "")) != self.event_name:
            return None

        session_id = str(getattr(record, "session_id", "") or "")
        self._counters["triggered"] += 1
        logger.info("memory scheduler: 收到 {} 事件 session={!r}", self.event_name, session_id)
        result = await self.flush(session_id)
        self._counters["delegated"] += 1
        if result.action == "failed":
            self._counters["failed"] += 1
        elif result.action == "skipped":
            self._counters["skipped"] += 1
        else:
            self._counters["delivered"] += 1
        if self.metrics is not None:
            try:
                self.metrics.record_writeback(session_id=session_id, ok=result.action != "failed")
            except Exception as exc:  # noqa: BLE001 - 指标不该让调度失败
                logger.debug("record_writeback 失败: {}", exc)
        return result

    # ------------------------------------------------------------------
    # 直接执行
    # ------------------------------------------------------------------
    async def flush(self, session_id: str, *, msgs: list[Any] | None = None) -> MaintenanceResult:
        """立刻为某个会话跑一轮 ``auto_memory``（不等事件）。

        Args:
            session_id (`str`): 会话 id。
            msgs (`list[Any] | None`): 消息；``None`` 时用 ``msgs_of`` 拉。

        Returns:
            `MaintenanceResult`: 结果枚举；**任何异常都被翻成 ``failed``**，
            因为本方法的上游是事件总线的 handler —— 在 handler 里抛异常
            会被总线记成 ``errors``（``EventBus.errors``）并**丢掉这一轮的记忆**，
            而返回 ``failed`` 至少能让调用方在统计里看见。
        """
        caller = str(session_id or "").strip()
        if not caller:
            return MaintenanceResult(action="failed", path=None, detail="session_id 为空，无法定位会话")

        if msgs is None:
            if self.msgs_of is None:
                return MaintenanceResult(
                    action="failed",
                    path=None,
                    detail="没有 msgs_of：调度器不知道去哪取会话消息。"
                    "正确姿势是传 SessionDistiller.messages_from_agent(agent) 这类函数。",
                )
            try:
                msgs = await self.msgs_of(caller)
            except Exception as exc:  # noqa: BLE001 - 取消息失败也要变成 failed
                logger.warning("memory scheduler: msgs_of({!r}) 失败: {}", caller, exc)
                return MaintenanceResult(action="failed", path=None, detail=f"取消息失败: {exc}")

        count = len(list(msgs or ()))
        if count < self.min_messages:
            detail = (
                f"只有 {count} 条消息，低于 min_messages={self.min_messages}，"
                "未调用 auto_memory（省一次 LLM 往返）"
            )
            logger.info("memory scheduler: session={!r} {}", caller, detail)
            return MaintenanceResult(action="skipped", path=None, detail=detail)

        return await self.maintainer.auto_memory(
            session_id=caller,
            msgs=list(msgs or ()),
            allowed_paths=self.allowed_paths,
        )

    # ------------------------------------------------------------------
    # 例行维护
    # ------------------------------------------------------------------
    async def nightly(self, *, date: str | None = None, dream: bool = True) -> NightlyReport:
        """一夜的例行维护：先 ``reindex``，再 ``auto_dream``。

        **顺序不能反**：``auto_dream`` 的输入是"今天读过的材料"，
        而它按 ``file_catalog`` 增量扫描；``reindex`` 负责把磁盘上新出现的
        ``daily/`` / ``digest/`` / ``resource/`` 文件收进 ``file_store`` 与索引。
        先 dream 后 reindex，dream 会读到一份还没更新的目录清单，
        结果是"今天没有新材料"—— 一个不报错的空转。

        两个 job 都**失败不中断**：``reindex`` 失败不该让 dream 也跑不成，
        反之亦然。结果一并写进 :class:`NightlyReport` 由调用方判断。

        Args:
            date (`str | None`): ``YYYY-MM-DD``；``None`` = ReMe 取今天。
            dream (`bool`): 是否跑 ``auto_dream``。

        Returns:
            `NightlyReport`: reindex 的计数与 dream 的结果。
        """
        report = NightlyReport(date=date, reindex_counts={}, dream=None)

        try:
            response = await self.maintainer.client.jobs().reindex()
        except Exception as exc:  # noqa: BLE001 - 例行维护失败不中断
            logger.warning("nightly: reindex 失败: {}", exc)
            report.reindex_ok = False
        else:
            report.reindex_ok = bool(getattr(response, "success", False))
            counts = dict((getattr(response, "metadata", None) or {}).get("counts") or {})
            report.reindex_counts = {
                str(key): int(value) for key, value in counts.items() if isinstance(value, (int, float))
            }
            if report.reindex_ok and not any(report.reindex_counts.values()):
                logger.warning(
                    "nightly: reindex 成功但 added/modified/deleted 全为 0 —— "
                    "这在『索引被 clear_store_step 清空后没装回来』时会静默发生，"
                    "请核对 watch_dirs 是否落到了 job 配置里",
                )

        if dream:
            report.dream = await self.maintainer.auto_dream(date=date)
        logger.info("nightly: {}", report.summary())
        return report

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        """返回计数器快照。

        Returns:
            `dict[str, int]`: ``triggered``（收到目标事件数）、
            ``delegated``（真的走进 flush 的次数）、``delivered``（产出了记忆）、
            ``skipped``（消息太少或模型判断不值得记）、``failed``（出错）。
        """
        return dict(self._counters)

    def explain(self) -> str:
        """把当前配置展开成人话。

        Returns:
            `str`: 多行说明。
        """
        source = "注入的 msgs_of" if self.msgs_of is not None else "（未配置，flush 会返回 failed）"
        return "\n".join(
            [
                "MemoryMaintenanceScheduler(",
                f"  event_name={self.event_name!r}  触发事件：EventKind.CUSTOM + payload['name']",
                f"  min_messages={self.min_messages}  低于它不调 job（省一次 LLM 往返）",
                f"  消息来源：{source}",
                f"  allowed_paths={self.allowed_paths}",
                f"  统计：{self.stats()}",
                ")",
            ],
        )
