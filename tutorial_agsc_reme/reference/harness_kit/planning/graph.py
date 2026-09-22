# -*- coding: utf-8 -*-
"""任务 DAG 与状态机（契约 §3.12，第 12 讲）。

**这一层补的是什么**：AgentScope 提供了三条「流程控制」路线 ——
``pipeline/GoalPipeline``（执行者 + 验证者互相拉扯，
``third_party/agentscope/src/agentscope/pipeline/_goal_pipeline.py:55``）、
``sop/SOPEngine``（固定里程碑序列，``.../sop/_engine.py:24``）、以及
``tool/_task/`` 的 ``TaskCreate`` / ``TaskList`` 四件套。它们回答的是
**「一条流程怎么走」**，但都不回答**「一堆有依赖关系的任务谁先谁后、某个
节点挂了之后它的下游算什么状态」**。后者是 DAG 调度问题，AgentScope 里没有
对应抽象，因此由本模块补上。

**为什么不是「重写 SOP」**：本模块只有数据结构与拓扑计算，**不执行任何
Agent**。执行在 :mod:`harness_kit.planning.planner`（它的 ``execute`` 逐个把
:meth:`TaskGraph.ready` 返回的节点喂给 AgentScope 的 ``Agent``），流程固化在
:mod:`harness_kit.planning.sop`（它把 ``TaskGraph`` 翻译成 ``SOP``／``SOPStep``）。
职责边界：**本模块回答「下一步该谁跑」，不回答「怎么跑」。**

状态机（六个状态，四类转移）：

```
                 ┌──────────── blocked ◄──── 任一依赖 failed/skipped/blocked
                 │                 │                    （传递闭包）
  pending ───────┼─────────────────┘
     ▲           │
     │  mark(RUNNING)            依赖复活（重规划后依赖回到 done）
     │           ▼                        │
     └──── blocked ◄── 依赖未全部 done     │
                 │
                 ▼
              running ──► done / failed / skipped
```

- ``pending``：等待依赖满足；
- ``blocked``：依赖里有 ``failed`` / ``skipped``，或依赖本身 ``blocked``，
  **本节点及它的整条下游都不会再跑**（除非上游被重规划救活）；
- ``running``：已被调度器取走；
- ``done`` / ``failed`` / ``skipped``：终态，不再变化（``mark`` 会把它们钉死）。

**为什么 ``blocked`` 必须是显式状态而不是靠调度器临时判断**：断点续跑
（:mod:`harness_kit.planning.resume`）落盘的是一张 DAG。如果「被上游拖死」只是
调度器里的一个临时判断，那么进程重启后这张 DAG 看起来和「什么都没跑」一模一样 ——
人会以为还有 20 个任务要做，实际上其中 12 个已经注定跑不成。把
``blocked`` 写进节点，落盘之后语义仍然自洽。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Literal, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "TERMINAL_STATUSES",
    "TaskGraph",
    "TaskGraphError",
    "TaskNode",
    "TaskStatus",
    "TaskStatusLiteral",
    "build_graph",
]


class TaskStatus(StrEnum):
    """任务节点的六个状态。

    与契约 §3.12 的 ``Literal["pending", "running", "done", "failed", "skipped"]``
    相比多了一个 :attr:`BLOCKED`，原因是任务书明确要求状态机包含
    ``blocked``（「任务 DAG 与状态机（pending / running / blocked / done /
    failed）」）。``StrEnum`` 的成员值与 ``Literal`` 逐字一致，因此
    ``TaskNode(status="done")`` 这种按契约字面量的构造方式**继续可用**
    （pydantic 会把字符串强制成枚举成员）。
    """

    PENDING = "pending"
    """等待依赖满足。初始状态，也是被重规划救活后的状态。"""

    RUNNING = "running"
    """已被调度器取走，正在执行。"""

    BLOCKED = "blocked"
    """依赖里存在 ``failed`` / ``skipped``，本节点永远不会被调度。"""

    DONE = "done"
    """成功。终态。"""

    FAILED = "failed"
    """失败。终态。"""

    SKIPPED = "skipped"
    """主动跳过（例如重规划后该子任务已无意义）。终态。"""


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED},
)
"""终态集合：进入其中任何一个之后 ``mark`` 不再接受新的状态转移。"""

_DEAD_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.FAILED, TaskStatus.SKIPPED},
)
"""会让下游变成 ``blocked`` 的状态。"""


class TaskGraphError(ValueError):
    """任务图非法：成环、悬空依赖、自依赖、未知状态、未知节点。"""


class TaskNode(BaseModel):
    """一个任务节点。

    字段与契约 §3.12 逐字一致，额外增加了三个**只为断点续跑与可观测服务**的
    字段（``attempts`` / ``error`` / ``metadata``），它们都有默认值，因此不影响
    契约里 ``TaskNode(id=..., goal=...)`` 的构造方式。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    """节点 id，图内唯一。"""

    goal: str
    """这一步要达成什么。**写给执行者看的一句话**，不是写给调度器看的。"""

    status: TaskStatus = TaskStatus.PENDING
    """当前状态。"""

    depends_on: list[str] = Field(default_factory=list)
    """依赖的节点 id。只有全部 ``done`` 时本节点才 ``ready``。"""

    artifacts: list[str] = Field(default_factory=list)
    """本节点产出的可寻址物（文件路径 / 引用句柄 / 摘要）。下游节点按需读取。"""

    attempts: int = Field(default=0, ge=0)
    """被调度的次数。每次 ``mark(RUNNING)`` 自增，重规划时用来判断是否要放弃。"""

    error: str | None = None
    """最近一次失败的摘要（``failed`` 时非空）。"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """自由元数据（路由用的 ``capability``、预算标签等）。"""

    @property
    def is_terminal(self) -> bool:
        """是否处于终态。

        Returns:
            `bool`: ``done`` / ``failed`` / ``skipped`` 为 ``True``。
        """
        return self.status in TERMINAL_STATUSES

    @property
    def is_dead(self) -> bool:
        """是否已失败或被跳过（会拖死下游）。

        Returns:
            `bool`: ``failed`` / ``skipped`` 为 ``True``。
        """
        return self.status in _DEAD_STATUSES

    def summary(self, *, limit: int = 120) -> str:
        """单行摘要，供日志与 ``to_mermaid`` 使用。

        Args:
            limit (`int`, defaults to `120`): ``goal`` 的截断长度。

        Returns:
            `str`: 形如 ``"[done] write_doc :: 写一份 README"``。
        """
        goal = self.goal if len(self.goal) <= limit else self.goal[:limit] + "…"
        return f"[{self.status.value}] {self.id} :: {goal}"


class TaskGraph(BaseModel):
    """一张任务 DAG。

    语义约束（:meth:`validate` 会全部检查）：

    1. ``nodes`` 的键就是节点 id，且 ``node.id == key``；
    2. ``depends_on`` 里的每个 id 都必须存在（否则悬空依赖）；
    3. 不能有自依赖；
    4. 不能成环。
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    """整张图的顶层目标（``HarnessPlanner.decompose`` 的输入）。"""

    nodes: dict[str, TaskNode] = Field(default_factory=dict)
    """节点 id → 节点。**有序字典**，:meth:`topological_order` 用它做稳定平局。"""

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    """创建时间（UTC，tz-aware）。"""

    @field_validator("nodes")
    @classmethod
    def _check_id_consistency(
        cls,
        nodes: dict[str, TaskNode],
    ) -> dict[str, TaskNode]:
        """校验 ``nodes`` 的键与 ``TaskNode.id`` 一致且不重复。

        Args:
            nodes (`dict[str, TaskNode]`): 待校验的节点表。

        Returns:
            `dict[str, TaskNode]`: 原样返回。

        Raises:
            `TaskGraphError`: 键与 ``id`` 不一致，或节点为空 id。
        """
        seen: set[str] = set()
        for key, node in nodes.items():
            if not key:
                raise TaskGraphError("节点 id 不能为空字符串。")
            if node.id != key:
                raise TaskGraphError(
                    f"nodes 的键 {key!r} 与 TaskNode.id {node.id!r} 不一致；"
                    "键必须等于 id，否则 depends_on 指向谁就不确定了。",
                )
            if key in seen:
                raise TaskGraphError(f"节点 id 重复：{key!r}。")
            seen.add(key)
        return nodes

    # ==================================================================
    # 构造
    # ==================================================================
    def add(
        self,
        node: TaskNode | None = None,
        *,
        node_id: str | None = None,
        goal: str | None = None,
        depends_on: Sequence[str] | None = None,
    ) -> TaskNode:
        """往图里加一个节点。

        Args:
            node (`TaskNode | None`, optional): 现成的节点。给定它时其余参数忽略。
            node_id (`str | None`, optional): 便捷构造用的 id。
            goal (`str | None`, optional): 便捷构造用的目标。
            depends_on (`Sequence[str] | None`, optional): 便捷构造用的依赖。

        Returns:
            `TaskNode`: 加进去的节点。

        Raises:
            `TaskGraphError`: id 已存在，或便捷构造参数不齐。
        """
        if node is None:
            if node_id is None or goal is None:
                raise TaskGraphError(
                    "add() 要么给一个 TaskNode，要么同时给 node_id 与 goal。",
                )
            node = TaskNode(
                id=node_id,
                goal=goal,
                depends_on=list(depends_on or []),
            )
        if node.id in self.nodes:
            raise TaskGraphError(f"节点 id 已存在：{node.id!r}。")
        self.nodes[node.id] = node
        return node

    @classmethod
    def from_specs(
        cls,
        goal: str,
        specs: Iterable[dict[str, Any]],
    ) -> "TaskGraph":
        """从「字典列表」建图，供 LLM 的结构化输出直接落地。

        Args:
            goal (`str`): 顶层目标。
            specs (`Iterable[dict[str, Any]]`): 每项含 ``id`` / ``goal`` /
                ``depends_on``（可选 ``metadata``）。

        Returns:
            `TaskGraph`: 校验通过的新图。

        Raises:
            `TaskGraphError`: 任一节点非法，或整图 :meth:`validate` 不通过。
        """
        graph = cls(goal=goal)
        for spec in specs:
            graph.add(
                TaskNode(
                    id=str(spec["id"]),
                    goal=str(spec["goal"]),
                    depends_on=[str(_) for _ in spec.get("depends_on", [])],
                    metadata=dict(spec.get("metadata", {})),
                ),
            )
        graph.validate()
        return graph

    # ==================================================================
    # 查询
    # ==================================================================
    def node(self, node_id: str) -> TaskNode:
        """取一个节点。

        Args:
            node_id (`str`): 节点 id。

        Returns:
            `TaskNode`: 节点对象（**引用**，改它就是改图）。

        Raises:
            `TaskGraphError`: 节点不存在。
        """
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise TaskGraphError(
                f"节点不存在：{node_id!r}；现有节点 {sorted(self.nodes)}。",
            ) from exc

    def by_status(self, status: TaskStatus | str) -> list[TaskNode]:
        """按状态筛选（保持插入顺序，保证可复现）。

        Args:
            status (`TaskStatus | str`): 目标状态。

        Returns:
            `list[TaskNode]`: 匹配的节点。
        """
        target = TaskStatus(status)
        return [_ for _ in self.nodes.values() if _.status is target]

    def ready(self) -> list[TaskNode]:
        """返回「依赖全部 done」的 ``pending`` 节点。

        契约 §3.12 的原文定义。**注意 ``blocked`` 不在返回集合里** ——
        它们依赖已经死了，调度器不该再碰它们，这正是把 ``blocked`` 显式化的收益。

        Returns:
            `list[TaskNode]`: 当前可以跑的节点，按图中的插入顺序。
        """
        out: list[TaskNode] = []
        for node in self.nodes.values():
            if node.status is not TaskStatus.PENDING:
                continue
            deps = [self.nodes[_] for _ in node.depends_on]
            if all(_.status is TaskStatus.DONE for _ in deps):
                out.append(node)
        return out

    def is_complete(self) -> bool:
        """是否所有节点都已进入终态（``done`` / ``failed`` / ``skipped``）。

        **它和「没有可推进的节点」不是一回事**：一张被上游拖死的图里
        ``blocked`` 节点永远不是终态，所以 ``is_complete()`` 会一直是
        ``False``，而 :meth:`ready` 早就空了。三个判断各司其职 ——
        「还跑不跑得动」看 ``ready() == []``，「终态收齐了没」看本方法，
        「是不是全做成了」看 :meth:`is_successful`。

        Returns:
            `bool`: 所有节点都处于终态时为 ``True``。
        """
        return all(_.is_terminal for _ in self.nodes.values())

    def is_successful(self) -> bool:
        """是否全部成功。

        Returns:
            `bool`: 所有节点都是 ``done``（空图算成功）时为 ``True``。
        """
        return all(_.status is TaskStatus.DONE for _ in self.nodes.values())

    def progress(self) -> dict[str, int]:
        """各状态计数，供日志 / 指标使用。

        Returns:
            `dict[str, int]`: 六个状态名 → 数量（含 0）。
        """
        counts = {_.value: 0 for _ in TaskStatus}
        for node in self.nodes.values():
            counts[node.status.value] += 1
        return counts

    # ==================================================================
    # 状态转移
    # ==================================================================
    def mark(
        self,
        node_id: str,
        status: TaskStatus | str,
        *,
        artifact: str | None = None,
        error: str | None = None,
    ) -> TaskNode:
        """把节点推进到新状态，并级联刷新下游的 ``blocked`` 标记。

        语义（契约 §3.12 只给了 ``status`` 与 ``artifact``，另外三个是新增的
        可选关键字，不影响按契约调用）：

        - ``RUNNING``：``attempts += 1``；
        - ``DONE``：``artifact`` 非空时追加进 ``artifacts``，并清空 ``error``；
        - ``FAILED``：``error`` 落到节点上，下游全部转 ``blocked``；
        - ``SKIPPED``：与 ``FAILED`` 同样对待（下游同样 ``blocked``）；
        - 终态再 ``mark``：抛 :class:`TaskGraphError`（不静默吞掉重复转移）。

        Args:
            node_id (`str`): 节点 id。
            status (`TaskStatus | str`): 目标状态。
            artifact (`str | None`, optional): 成功时产出的可寻址物。
            error (`str | None`, optional): 失败摘要。

        Returns:
            `TaskNode`: 被更新的节点。

        Raises:
            `TaskGraphError`: 节点不存在、状态非法、或对终态节点再转移。
        """
        node = self.node(node_id)
        try:
            target = TaskStatus(status)
        except ValueError as exc:
            raise TaskGraphError(
                f"未知状态 {status!r}；合法值 {[_.value for _ in TaskStatus]}。",
            ) from exc

        if node.is_terminal:
            raise TaskGraphError(
                f"节点 {node_id!r} 已是终态 {node.status.value!r}，"
                f"不允许再转移到 {target.value!r}。终态不可逆是断点续跑的前提。",
            )

        node.status = target
        if target is TaskStatus.RUNNING:
            node.attempts += 1
        elif target is TaskStatus.DONE:
            node.error = None
            if artifact is not None and artifact not in node.artifacts:
                node.artifacts.append(artifact)
        elif target in _DEAD_STATUSES:
            node.error = error or node.error

        self._refresh_blocked()
        logger.debug(
            "TaskGraph.mark: {} -> {} (progress={})",
            node_id,
            target.value,
            self.progress(),
        )
        return node

    def mark_many(
        self,
        node_ids: Iterable[str],
        status: TaskStatus | str,
        *,
        error: str | None = None,
    ) -> list[TaskNode]:
        """批量转移（重规划时把一批节点一起置 ``pending`` 很常用）。

        Args:
            node_ids (`Iterable[str]`): 节点 id。
            status (`TaskStatus | str`): 目标状态。
            error (`str | None`, optional): 失败摘要。

        Returns:
            `list[TaskNode]`: 被更新的节点。
        """
        return [self.mark(_, status, error=error) for _ in node_ids]

    def refresh_blocked(self) -> None:
        """公开的阻塞重算入口（:meth:`_refresh_blocked` 的对外门面）。

        断点续跑（:mod:`harness_kit.planning.resume`）在把 ``running`` 打回
        ``pending`` 之后必须重算一次阻塞状态，那条路径在类外，所以这里给一个
        公开名字，避免调用方去碰下划线方法。
        """
        self._refresh_blocked()

    def _refresh_blocked(self) -> None:
        """重算所有 ``pending`` / ``blocked`` 节点的阻塞状态，**扫到不动点**。

        两条规则：

        - 依赖里存在 ``failed`` / ``skipped``（死），**或存在一个 ``blocked``
          的节点** → 自己也 ``blocked``；
        - 依赖全部 ``done`` → 从 ``blocked`` 回到 ``pending``（重规划救活上游后
          必须能自愈，否则一次失败会永久冻死整张图）。

        **为什么第一版只按直接依赖判、后来改成传递闭包**：``a`` 失败 →
        ``b`` blocked → 依赖 ``b`` 的 ``c`` 若只按直接依赖判，会停在
        ``pending``。它永远不会被 :meth:`ready` 选中（那一刻 ``b`` 也不是
        ``done``），于是 ``progress()`` 长期报着一个永远不动的 ``pending``
        —— 这正是本讲反复强调的「静默卡死」：运维看到的是「还有 1 个任务」，
        实际是「这 1 个永远不会跑」。改成不动点扫描后，``blocked`` 集合恰好
        等于「从失败节点出发、沿依赖边可达的节点集」，语义与
        :meth:`downstream` 一致。

        图里可能有环（``_refresh_blocked`` 会在 :meth:`validate` 之前被调用），
        所以不能用拓扑序，只能扫到不动点；外层循环最多
        ``len(self.nodes) + 1`` 次，必然终止。
        """
        for _ in range(len(self.nodes) + 1):
            changed = False
            for node in self.nodes.values():
                if node.status not in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                    continue
                deps = [self.nodes[_] for _ in node.depends_on]
                unreachable = [
                    _.id for _ in deps if _.is_dead or _.status is TaskStatus.BLOCKED
                ]
                if unreachable:
                    if node.status is not TaskStatus.BLOCKED:
                        logger.debug(
                            "TaskGraph: 节点 {} 因依赖 {} 不可达而 blocked。",
                            node.id,
                            unreachable,
                        )
                        node.status = TaskStatus.BLOCKED
                        changed = True
                elif node.status is TaskStatus.BLOCKED:
                    node.status = TaskStatus.PENDING
                    changed = True
            if not changed:
                break

    # ==================================================================
    # 校验与拓扑
    # ==================================================================
    def validate(self) -> None:
        """校验整图：悬空依赖、自依赖、成环。

        Raises:
            `TaskGraphError`: 任一约束不满足。
        """
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep == node.id:
                    raise TaskGraphError(f"节点 {node.id!r} 依赖自己。")
                if dep not in self.nodes:
                    raise TaskGraphError(
                        f"节点 {node.id!r} 依赖了不存在的节点 {dep!r}"
                        f"（悬空依赖）；现有节点 {sorted(self.nodes)}。",
                    )
        # Kahn 算法：能排完 = 无环
        indegree = {
            _: len(node.depends_on) for _, node in self.nodes.items()
        }
        queue = [k for k, v in indegree.items() if v == 0]
        visited = 0
        while queue:
            cur = queue.pop(0)
            visited += 1
            for node in self.nodes.values():
                if cur in node.depends_on:
                    indegree[node.id] -= 1
                    if indegree[node.id] == 0:
                        queue.append(node.id)
        if visited != len(self.nodes):
            cyclic = sorted(k for k, v in indegree.items() if v > 0)
            raise TaskGraphError(
                f"任务图成环，环上（或环的下游）节点：{cyclic}。",
            )

    def topological_order(self) -> list[TaskNode]:
        """返回一个确定性的拓扑序。

        平局时按 **图中的插入顺序** 决胜（Kahn + 就绪队列），因此同一张图
        每次调用返回的顺序完全一致 —— 这是「确定性可复现」要求的落点，
        教程里可以用 ``assert g.topological_order() == g.topological_order()``
        断言它。

        Returns:
            `list[TaskNode]`: 拓扑序节点列表。

        Raises:
            `TaskGraphError`: 成环（等价于 :meth:`validate` 的一部分）。
        """
        order_key = {k: i for i, k in enumerate(self.nodes)}
        indegree = {_: len(n.depends_on) for _, n in self.nodes.items()}
        ready = sorted(
            (k for k, v in indegree.items() if v == 0),
            key=order_key.__getitem__,
        )
        out: list[TaskNode] = []
        while ready:
            cur = ready.pop(0)
            out.append(self.nodes[cur])
            for node in self.nodes.values():
                if cur not in node.depends_on:
                    continue
                indegree[node.id] -= 1
                if indegree[node.id] == 0:
                    ready.append(node.id)
                    ready.sort(key=order_key.__getitem__)
        if len(out) != len(self.nodes):
            raise TaskGraphError("任务图成环，无法给出拓扑序。")
        return out

    def downstream(self, node_id: str) -> list[str]:
        """返回（传递地）依赖 ``node_id`` 的所有节点 id，按拓扑序。

        Args:
            node_id (`str`): 起点。

        Returns:
            `list[str]`: 下游节点 id（不含起点）。
        """
        self.node(node_id)
        out: list[str] = []
        for node in self.topological_order():
            if node.id in out:
                continue
            if node_id in node.depends_on or any(
                _ in out for _ in node.depends_on
            ):
                if node.id != node_id:
                    out.append(node.id)
        return out

    # ==================================================================
    # 展示 / 落盘
    # ==================================================================
    def to_mermaid(self) -> str:
        """渲染成 Mermaid ``graph TD``，便于教程里直接贴图。

        Returns:
            `str`: Mermaid 源码。
        """
        lines = ["graph TD"]
        for node in self.nodes.values():
            label = node.goal.replace('"', "'")
            if len(label) > 40:
                label = label[:40] + "…"
            lines.append(f'    {node.id}["{node.id}<br/>{label}"]')
        for node in self.nodes.values():
            for dep in node.depends_on:
                lines.append(f"    {dep} --> {node.id}")
        return "\n".join(lines)

    def describe(self) -> str:
        """多行人读描述。

        Returns:
            `str`: 每行一个节点，带状态、依赖与产出。
        """
        lines = [
            f"TaskGraph(goal={self.goal!r}, nodes={len(self.nodes)}, "
            f"progress={self.progress()})",
        ]
        for node in self.topological_order():
            deps = f" <- {node.depends_on}" if node.depends_on else ""
            arts = f" artifacts={node.artifacts}" if node.artifacts else ""
            lines.append(f"  {node.summary()}{deps}{arts}")
        return "\n".join(lines)


def build_graph(
    goal: str,
    *,
    subtasks: Sequence[str],
    depends_on: Sequence[Sequence[str]] | None = None,
) -> TaskGraph:
    """给「一条直线的子任务列表」快速建图的便捷函数。

    不传 ``depends_on`` 时构造**串行链**（``t2`` 依赖 ``t1`` …），这是 LLM
    拆解失败时的安全兜底：串行一定能跑，并行优化是后话。

    Args:
        goal (`str`): 顶层目标。
        subtasks (`Sequence[str]`): 每个子任务的一句话目标。
        depends_on (`Sequence[Sequence[str]] | None`, optional): 与
            ``subtasks`` 等长的依赖列表；``None`` 表示串行链。

    Returns:
        `TaskGraph`: 已 :meth:`TaskGraph.validate` 的图。

    Raises:
        `TaskGraphError`: 两个序列长度不一致。
    """
    graph = TaskGraph(goal=goal)
    ids = [f"t{i + 1}" for i in range(len(subtasks))]
    if depends_on is not None and len(depends_on) != len(subtasks):
        raise TaskGraphError(
            f"subtasks({len(subtasks)}) 与 depends_on({len(depends_on)}) "
            "长度必须一致。",
        )
    for index, (node_id, text) in enumerate(zip(ids, subtasks)):
        if depends_on is None:
            deps: list[str] = ids[index - 1 : index] if index else []
        else:
            deps = list(depends_on[index])
        graph.add(TaskNode(id=node_id, goal=text, depends_on=deps))
    graph.validate()
    return graph


TaskStatusLiteral = Literal[
    "pending",
    "running",
    "blocked",
    "done",
    "failed",
    "skipped",
]
"""``TaskStatus`` 的字面量别名（契约 §3.12 的 ``Literal`` 形态）。"""
