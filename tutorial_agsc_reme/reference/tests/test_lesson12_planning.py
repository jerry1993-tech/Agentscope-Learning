# -*- coding: utf-8 -*-
"""第 12 讲的 pytest：任务图状态机 / 计划持久化 / SOP 状态机 / 拆解与调度。

四条纪律：

1. **0 次 LLM 调用**。本讲的全部被测组件都在"编排"这一层，而编排层的正确性
   与"模型答得好不好"无关 —— 所以这里的每一张图、每一次 SOP 尝试都是**构造出来
   的**，不是模型生成的。需要真模型的断言只有一条："``decompose`` 的产出能被
   ``TaskGraph.from_specs`` 吃下"，那一条用 stub（一个实现了 ``reply`` /
   ``reply_stream`` 的假 Agent）来测协议，真实模型部分留给
   ``scripts/12_planning.py --live``。
2. **每个"能过"都要配一个"该拦"。** 编排组件的失效模式是**静默跑错**：
   环没查出来、``blocked`` 没级联、``running`` 没归一化、尝试预算被绕过 ——
   这些都不会抛异常，只会让一趟长任务在半夜卡死。所以下面每条正向断言旁边
   都有一条反向断言（``pytest.raises``）。
3. **``blocked`` 是本讲补出来的第六个状态，必须单独测传递性。** 它同时承担
   "不许再调度"与"上游救活后要能回来"两个相反的要求；这两条任何一条失效，
   断点续跑给出的图就与内存里的图不再等价。
4. **``AgentLike`` / ``PipelineProtocol`` 是鸭子类型，测试里就直接用鸭子。**
   :class:`StubAgent` 不是 ``Agent`` 的子类，它只满足协议；如果哪天
   ``HarnessSOP`` / ``HarnessPlanner`` 偷偷依赖了 ``Agent`` 的具体属性，
   这些测试会立刻变红。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson12_planning.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.sop import SOPRunState
from agentscope.types import ReplyFinishedReason

from harness_kit.events import EventKind
from harness_kit.planning import (
    HarnessPlanner,
    HarnessSOP,
    PlanDraft,
    PlanNotFoundError,
    PlanStore,
    SOPDefinition,
    SOPError,
    SubTaskDraft,
    TaskGraph,
    TaskGraphError,
    TaskNode,
    TaskStatus,
    build_graph,
    plan_digest,
)
from harness_kit.session import JsonlSessionStore

# ======================================================================
# 测试替身
# ======================================================================


class StubAgent:
    """满足 ``AgentLike`` Protocol 的假执行者 / 假验证者 / 假拆解者。

    ``HarnessSOP`` 只要求 executor / verifier 有 ``reply_stream``
    （``third_party/agentscope/src/agentscope/sop/_schema.py:35`` 的
    ``AgentLike``），``HarnessPlanner`` 只要求 agent 有 ``reply``
    与 ``reply_stream``。本类刻意**不继承** ``Agent``：如果实现里偷偷用到了
    ``Agent`` 的某个具体属性，只有"鸭子"才会把它暴露出来。

    ``payloads`` 是**跨两个方法共享的一条队列**，按调用先后依次弹出 —— 这样
    self-verify（同一个对象既当 executor 又当 verifier）也能用一份 payload
    序列说清楚："这次给 ``{"handover": ...}``，下次给 ``{"passed": True}``"。

    Args:
        name (`str`): 角色名。
        payloads (`list[dict[str, Any]]`): 依次弹出的结构化输出。
        texts (`list[str]`): ``reply_stream`` 依次吐出的正文。
    """

    def __init__(
        self,
        name: str = "stub",
        payloads: list[dict[str, Any]] | None = None,
        texts: list[str] | None = None,
    ) -> None:
        """初始化。"""
        self.name = name
        self.payloads = list(payloads or [])
        self.texts = list(texts or [])
        self.calls: list[str] = []
        """每次调用记一笔 ``"reply"`` / ``"reply_stream"``，用于断言调用次数。"""
        self.schemas: list[str] = []
        """每次调用请求的 schema 名（``_Handover`` / ``_Verdict`` / ``PlanDraft``）。"""

    def _next_payload(self) -> dict[str, Any]:
        """弹出一个 payload；弹空时返回空 dict（模拟"这轮没有结构化输出"）。

        Returns:
            `dict[str, Any]`: 本次调用的结构化输出。
        """
        return self.payloads.pop(0) if self.payloads else {}

    async def reply(
        self,
        inputs: Any = None,
        structured_schema: Any = None,
        **kw: Any,
    ) -> Msg:
        """返回一条带 ``structured_output`` 的终态 ``Msg``（拆解用）。"""
        self.calls.append("reply")
        self.schemas.append(getattr(structured_schema, "__name__", "?"))
        return Msg(
            name=self.name,
            role="assistant",
            content=[TextBlock(text="(stub reply)")],
            finished_reason=ReplyFinishedReason.COMPLETED,
            structured_output=self._next_payload(),
        )

    async def reply_stream(
        self,
        inputs: Any = None,
        structured_schema: Any = None,
        yield_final_msg: bool = False,
        **kw: Any,
    ) -> AsyncGenerator[Msg, None]:
        """吐一条终态 ``Msg``（执行 / 验证用）。"""
        self.calls.append("reply_stream")
        self.schemas.append(getattr(structured_schema, "__name__", "?"))
        text = self.texts.pop(0) if self.texts else "(stub stream)"
        yield Msg(
            name=self.name,
            role="assistant",
            content=[TextBlock(text=text)],
            finished_reason=ReplyFinishedReason.COMPLETED,
            structured_output=self._next_payload(),
        )


def handover(text: str) -> dict[str, str]:
    """造一个 ``_Handover`` payload。

    Args:
        text (`str`): 交给下一步的内容。

    Returns:
        `dict[str, str]`: ``{"handover": text}``。
    """
    return {"handover": text}


# ======================================================================
# 夹具
# ======================================================================


@pytest.fixture()
def diamond() -> TaskGraph:
    """一张 5 节点的菱形图：``a → b → d``、``a → c → d``、``d → e``。

    Returns:
        `TaskGraph`: 已校验通过的任务图。
    """
    graph = TaskGraph(goal="菱形")
    graph.add(TaskNode(id="a", goal="A"))
    graph.add(TaskNode(id="b", goal="B", depends_on=["a"]))
    graph.add(TaskNode(id="c", goal="C", depends_on=["a"]))
    graph.add(TaskNode(id="d", goal="D", depends_on=["b", "c"]))
    graph.add(TaskNode(id="e", goal="E", depends_on=["d"]))
    graph.validate()
    return graph


@pytest.fixture()
def sop_yaml(tmp_path: Path) -> Path:
    """一份两步骤的临时 SOP 定义（第二步 ``verify: false``）。

    Returns:
        `Path`: 写好的 YAML 文件。
    """
    target = tmp_path / "two_step.yaml"
    target.write_text(
        "name: two_step\n"
        "description: '测试用两步 SOP'\n"
        "steps:\n"
        "  - subject: draft\n"
        "    description: 写一版草稿\n"
        "    max_attempts: 2\n"
        "    verify: true\n"
        "  - subject: publish\n"
        "    description: 发布草稿\n"
        "    max_attempts: 1\n"
        "    verify: false\n",
        encoding="utf-8",
    )
    return target


# ======================================================================
# 一 · TaskStatus 与 TaskNode
# ======================================================================
class TestTaskStatus:
    """六个状态的语义。"""

    def test_six_statuses_and_partition(self) -> None:
        """六个状态一个不多一个不少，且 ``is_terminal`` 与 ``is_dead`` 是两个问题。"""
        values = {_.value for _ in TaskStatus}
        assert values == {
            "pending",
            "running",
            "blocked",
            "done",
            "failed",
            "skipped",
        }

        def classify(status: TaskStatus) -> tuple[bool, bool]:
            node = TaskNode(id="x", goal="g", status=status)
            return node.is_terminal, node.is_dead

        # done 是终态但不是"死"——它不阻塞下游；failed/skipped 既终态又死。
        assert classify(TaskStatus.DONE) == (True, False)
        assert classify(TaskStatus.FAILED) == (True, True)
        assert classify(TaskStatus.SKIPPED) == (True, True)
        # blocked / pending / running 三个都不是终态，也都不是"死"。
        assert classify(TaskStatus.BLOCKED) == (False, False)
        assert classify(TaskStatus.PENDING) == (False, False)
        assert classify(TaskStatus.RUNNING) == (False, False)

    def test_str_enum_json_friendly(self) -> None:
        """``StrEnum`` 直接当字符串用，落盘后读回来仍相等。"""
        assert json.dumps({"s": TaskStatus.BLOCKED.value}) == '{"s": "blocked"}'
        assert TaskStatus("blocked") is TaskStatus.BLOCKED

    def test_node_defaults(self) -> None:
        """``TaskNode`` 的默认值：pending / 0 次尝试 / 空依赖。"""
        node = TaskNode(id="x", goal="做点事")
        assert node.status is TaskStatus.PENDING
        assert node.attempts == 0
        assert node.depends_on == []
        assert node.artifacts == []
        assert node.error is None
        assert "pending" in node.summary() and "x" in node.summary()

    def test_node_extra_field_rejected(self) -> None:
        """拼错字段名要报错，不能静默丢掉（``extra="forbid"``）。"""
        with pytest.raises(Exception):
            TaskNode(id="x", goal="y", statuss="done")  # type: ignore[call-arg]


# ======================================================================
# 二 · TaskGraph：调度语义
# ======================================================================
class TestTaskGraphScheduling:
    """``ready`` / ``mark`` / ``blocked`` 级联与自愈。"""

    def test_ready_is_empty_before_upstream_done(self, diamond: TaskGraph) -> None:
        """只有入度为 0 的 ``a`` 就绪；``b``/``c`` 在等它。"""
        assert [_.id for _ in diamond.ready()] == ["a"]

    def test_ready_fans_out_after_done(self, diamond: TaskGraph) -> None:
        """``a`` 完成后 ``b``/``c`` **同时**就绪 —— 这就是可并行的形状。"""
        diamond.mark("a", TaskStatus.RUNNING)
        diamond.mark("a", TaskStatus.DONE, artifact="A 的产物")
        assert [_.id for _ in diamond.ready()] == ["b", "c"]
        assert diamond.node("a").artifacts == ["A 的产物"]
        assert diamond.node("a").attempts == 1

    def test_running_does_not_satisfy_dependencies(self, diamond: TaskGraph) -> None:
        """``running`` **不算** done：下游必须等终态，不能抢跑。"""
        diamond.mark("a", TaskStatus.RUNNING)
        assert diamond.ready() == []
        assert diamond.node("b").status is TaskStatus.PENDING

    def test_failure_cascades_blocked_and_leaves_sibling_alone(
        self,
        diamond: TaskGraph,
    ) -> None:
        """``b`` 挂了：``d``/``e`` 冻死，但**兄弟** ``c`` 不受影响。"""
        diamond.mark("a", TaskStatus.DONE, artifact="ok")
        diamond.mark("b", TaskStatus.RUNNING)
        diamond.mark("b", TaskStatus.FAILED, error="模型给了空产物")

        assert diamond.node("b").error == "模型给了空产物"
        assert diamond.node("d").status is TaskStatus.BLOCKED
        assert diamond.node("e").status is TaskStatus.BLOCKED
        assert diamond.node("c").status is TaskStatus.PENDING
        # blocked 不在 ready 里 —— 调度器不该再碰它们；只有兄弟 c 还能跑。
        assert [_.id for _ in diamond.ready()] == ["c"]

    def test_blocked_self_heals_when_upstream_revived(self, diamond: TaskGraph) -> None:
        """重规划把 ``b`` 救回 done 后，``d``/``e`` 必须自动回到 pending。

        这条是整个断点续跑设计的地基：如果 ``blocked`` 是不可逆的，
        一次失败就把计划永久钉死，重规划变得毫无意义。

        **注意"救活上游"这一步是调用方的动作**：``failed`` 是终态，
        ``mark`` 会拒绝把它改回去（见下一条测试），所以重规划只能直接改
        ``node.status``。这**恰恰是设计意图** —— 「这活我们决定重做」是业务判断，
        不该被一个通用的状态机悄悄替你做掉；状态机只保证「你救活之后，
        下游能自愈」。
        """
        diamond.mark("a", TaskStatus.DONE, artifact="ok")
        diamond.mark("b", TaskStatus.FAILED, error="先失败")
        assert diamond.node("d").status is TaskStatus.BLOCKED

        # 重规划：把 b 打回 pending，再重跑一次
        diamond.node("b").status = TaskStatus.PENDING
        diamond.node("b").error = None
        diamond.mark("b", TaskStatus.RUNNING)
        diamond.mark("b", TaskStatus.DONE, artifact="补做了")
        assert diamond.node("b").error is None
        assert diamond.node("d").status is TaskStatus.PENDING
        assert diamond.node("e").status is TaskStatus.PENDING
        # d 的另一个依赖 c 还没做，所以这一刻只有 c 就绪 —— pending ≠ 就绪。
        assert [_.id for _ in diamond.ready()] == ["c"]

    def test_skipped_also_kills_downstream(self, diamond: TaskGraph) -> None:
        """``skipped`` 与 ``failed`` 同等看待：下游一律 blocked。"""
        diamond.mark("a", TaskStatus.SKIPPED, error="本次不需要")
        assert diamond.node("b").status is TaskStatus.BLOCKED
        assert diamond.node("c").status is TaskStatus.BLOCKED

    def test_terminal_is_irreversible(self, diamond: TaskGraph) -> None:
        """终态再 mark 抛 ``TaskGraphError``，不静默吞掉重复转移。"""
        diamond.mark("a", TaskStatus.DONE, artifact="ok")
        with pytest.raises(TaskGraphError, match="终态不可逆"):
            diamond.mark("a", TaskStatus.RUNNING)

    def test_unknown_status_rejected(self, diamond: TaskGraph) -> None:
        """拼错的状态名要在入口拦下，不能写进节点。"""
        with pytest.raises(TaskGraphError, match="未知状态"):
            diamond.mark("a", "finish")

    def test_unknown_node_rejected(self, diamond: TaskGraph) -> None:
        """不存在的节点要报错并给出可选值。"""
        with pytest.raises(TaskGraphError, match="节点不存在"):
            diamond.mark("nope", TaskStatus.DONE)

    def test_attempts_counts_running_not_done(self, diamond: TaskGraph) -> None:
        """``attempts`` 由 ``running`` 累加 —— 反复重试同一步是要能被数出来的。"""
        diamond.mark("a", TaskStatus.RUNNING)
        diamond.mark("a", TaskStatus.FAILED, error="第一次")
        assert diamond.node("a").attempts == 1
        # 重规划允许把 failed 打回 pending 再试（这一步由调用方决定）
        diamond.node("a").status = TaskStatus.PENDING
        diamond.mark("a", TaskStatus.RUNNING)
        diamond.mark("a", TaskStatus.DONE, artifact="第二次成了")
        assert diamond.node("a").attempts == 2

    def test_artifact_deduplicated(self, diamond: TaskGraph) -> None:
        """同一个产物重复写入不产生重复条目（重规划后重跑会撞上这条）。"""
        for _ in range(2):
            diamond.node("a").status = TaskStatus.RUNNING
            diamond.mark("a", TaskStatus.DONE, artifact="同样的字")
            diamond.node("a").status = TaskStatus.PENDING
        assert diamond.node("a").artifacts == ["同样的字"]

    def test_progress_and_completion(self, diamond: TaskGraph) -> None:
        """``progress`` 是状态直方图（六个键永远都在），``is_complete`` 看终态。"""
        assert diamond.progress() == {
            "pending": 5,
            "running": 0,
            "blocked": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
        }
        assert not diamond.is_complete()
        for node_id in ("a", "b", "c", "d", "e"):
            diamond.mark(node_id, TaskStatus.RUNNING)
            diamond.mark(node_id, TaskStatus.DONE, artifact=f"{node_id} 的产物")
        assert diamond.progress()["done"] == 5
        assert diamond.is_complete()
        assert diamond.is_successful()

    def test_all_blocked_means_no_ready_but_not_complete(
        self,
        diamond: TaskGraph,
    ) -> None:
        """全 blocked：没有可推进的节点，但**不是**完成，更不是成功。

        三个判断必须分开，长任务半夜停下时运维才能一眼分清
        「做完了」「没得做了」「卡死了」：

        - ``ready() == []``：调度器无事可做；
        - ``is_complete()``：还有非终态节点（``blocked`` 不是终态），所以是 False；
        - ``is_successful()``：False。
        """
        diamond.mark("a", TaskStatus.FAILED, error="死了")
        assert diamond.ready() == []
        assert not diamond.is_complete()
        assert not diamond.is_successful()
        assert diamond.progress() == {
            "pending": 0,
            "running": 0,
            "blocked": 4,
            "done": 0,
            "failed": 1,
            "skipped": 0,
        }

    def test_blocked_is_transitive(self) -> None:
        """阻塞要沿依赖边传递到底，不能只冻住第一层。

        只冻第一层的后果是静默卡死：``c`` 永远停在 ``pending``，它也不会被
        ``ready()`` 选中，于是 ``progress()`` 长期报着一个永远不动的
        ``pending``，运维以为"还有一个任务"，实际是"这一个永远不会跑"。
        """
        graph = TaskGraph(goal="长链")
        graph.add(TaskNode(id="a", goal="A"))
        graph.add(TaskNode(id="b", goal="B", depends_on=["a"]))
        graph.add(TaskNode(id="c", goal="C", depends_on=["b"]))
        graph.add(TaskNode(id="d", goal="D", depends_on=["c"]))
        graph.mark("a", TaskStatus.FAILED, error="第一环就挂了")

        assert [_.status for _ in graph.nodes.values()] == [
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.BLOCKED,
            TaskStatus.BLOCKED,
        ]
        # blocked 集合恰好等于「从失败节点出发沿依赖边可达」的集合
        assert [_.id for _ in graph.by_status(TaskStatus.BLOCKED)] == ["b", "c", "d"]
        assert graph.by_status(TaskStatus.PENDING) == []

    def test_transitive_blocked_self_heals_too(self) -> None:
        """传递闭包不能把自愈堵死：上游救活后整条链一起回到 pending。"""
        graph = TaskGraph(goal="长链")
        graph.add(TaskNode(id="a", goal="A"))
        graph.add(TaskNode(id="b", goal="B", depends_on=["a"]))
        graph.add(TaskNode(id="c", goal="C", depends_on=["b"]))
        graph.mark("a", TaskStatus.FAILED, error="挂了")
        assert graph.node("c").status is TaskStatus.BLOCKED

        graph.node("a").status = TaskStatus.PENDING
        graph.mark("a", TaskStatus.RUNNING)
        graph.mark("a", TaskStatus.DONE, artifact="补做了")
        assert graph.node("b").status is TaskStatus.PENDING
        assert graph.node("c").status is TaskStatus.PENDING
        assert [_.id for _ in graph.ready()] == ["b"]

    def test_mark_many(self, diamond: TaskGraph) -> None:
        """批量转移：重规划时把一批节点一起置 pending 很常用。"""
        diamond.mark("a", TaskStatus.FAILED, error="死了")
        assert diamond.node("b").status is TaskStatus.BLOCKED
        diamond.node("a").status = TaskStatus.PENDING
        diamond.mark_many(["b", "c", "d", "e"], TaskStatus.PENDING)
        assert diamond.node("b").status is TaskStatus.PENDING
        assert diamond.node("d").status is TaskStatus.PENDING


# ======================================================================
# 三 · TaskGraph：图论约束
# ======================================================================
class TestTaskGraphStructure:
    """环 / 悬空依赖 / 自依赖 / 重复 id / 拓扑序确定性。"""

    def test_cycle_rejected(self) -> None:
        """成环必须被 Kahn 查出来，并报出环上节点。"""
        graph = TaskGraph(goal="环")
        graph.add(TaskNode(id="a", goal="A", depends_on=["c"]))
        graph.add(TaskNode(id="b", goal="B", depends_on=["a"]))
        graph.add(TaskNode(id="c", goal="C", depends_on=["b"]))
        with pytest.raises(TaskGraphError, match="成环"):
            graph.validate()

    def test_two_node_cycle_rejected(self) -> None:
        """最短的环也要拦住 —— 它在 DAG 里最容易被手抖写出来。"""
        graph = TaskGraph(goal="互赖")
        graph.add(TaskNode(id="a", goal="A", depends_on=["b"]))
        graph.add(TaskNode(id="b", goal="B", depends_on=["a"]))
        with pytest.raises(TaskGraphError, match="成环"):
            graph.validate()

    def test_dangling_dependency_rejected(self) -> None:
        """依赖了不存在的节点要报错，并把现有节点列出来。"""
        graph = TaskGraph(goal="悬空")
        graph.add(TaskNode(id="a", goal="A", depends_on=["ghost"]))
        with pytest.raises(TaskGraphError, match="悬空依赖"):
            graph.validate()

    def test_self_dependency_rejected(self) -> None:
        """自依赖单独给一条消息，而不是让它伪装成"环"。"""
        graph = TaskGraph(goal="自依赖")
        graph.add(TaskNode(id="a", goal="A", depends_on=["a"]))
        with pytest.raises(TaskGraphError, match="依赖自己"):
            graph.validate()

    def test_duplicate_id_rejected(self, diamond: TaskGraph) -> None:
        """重复 id 会让"哪个 b"变成歧义，必须在 add 时就拒绝。"""
        with pytest.raises(TaskGraphError, match="已存在"):
            diamond.add(TaskNode(id="b", goal="又一个 B"))

    def test_add_convenience_requires_both_args(self) -> None:
        """便捷构造要么给节点、要么同时给 id 与 goal。"""
        graph = TaskGraph(goal="x")
        with pytest.raises(TaskGraphError, match="要么给一个 TaskNode"):
            graph.add(node_id="only_id")
        assert graph.add(node_id="ok", goal="有了").id == "ok"

    def test_topological_order_is_deterministic(self, diamond: TaskGraph) -> None:
        """同一张图两次调用逐字相同 —— 这是"可复现"在编排层的落点。"""
        first = [_.id for _ in diamond.topological_order()]
        second = [_.id for _ in diamond.topological_order()]
        assert first == second
        # Kahn + 插入顺序：a 出队后 b、c 同时入队，按插入顺序 b 在前。
        assert first == ["a", "b", "c", "d", "e"]

    def test_topological_order_respects_edges(self) -> None:
        """插入顺序与依赖方向相反时，拓扑序仍然要服从依赖。"""
        graph = TaskGraph(goal="逆序插入")
        graph.add(TaskNode(id="last", goal="最后", depends_on=["first"]))
        graph.add(TaskNode(id="first", goal="最先"))
        assert [_.id for _ in graph.topological_order()] == ["first", "last"]

    def test_from_specs(self) -> None:
        """``from_specs`` 直接吃 LLM 的结构化输出格式并顺手校验。"""
        graph = TaskGraph.from_specs(
            "写 README",
            [
                {"id": "outline", "goal": "列提纲", "depends_on": []},
                {
                    "id": "draft",
                    "goal": "写正文",
                    "depends_on": ["outline"],
                    "metadata": {"capability": "writing"},
                },
            ],
        )
        assert sorted(graph.nodes) == ["draft", "outline"]
        assert graph.node("draft").metadata["capability"] == "writing"
        assert [_.id for _ in graph.ready()] == ["outline"]

    def test_from_specs_rejects_cycle(self) -> None:
        """模型给出的环不会一路带到调度期才炸。"""
        with pytest.raises(TaskGraphError, match="成环"):
            TaskGraph.from_specs(
                "环",
                [
                    {"id": "a", "goal": "A", "depends_on": ["b"]},
                    {"id": "b", "goal": "B", "depends_on": ["a"]},
                ],
            )

    def test_downstream_transitive(self, diamond: TaskGraph) -> None:
        """``downstream`` 是传递闭包 —— 判影响面时不用再手写遍历。"""
        assert diamond.downstream("a") == ["b", "c", "d", "e"]
        assert diamond.downstream("d") == ["e"]
        assert diamond.downstream("e") == []

    def test_to_mermaid_is_wellformed(self, diamond: TaskGraph) -> None:
        """mermaid 里节点 id 只用 ASCII，标签放双引号里。"""
        lines = diamond.to_mermaid().splitlines()
        assert lines[0] == "graph TD"
        assert "    a --> b" in lines
        assert '    a["a<br/>A"]' in lines

    def test_to_mermaid_quotes_chinese_labels(self) -> None:
        """中文标签必须被双引号包住 —— mermaid 遇到裸中文会解析失败。"""
        graph = build_graph("写文档", subtasks=["列提纲", "写正文"])
        text = graph.to_mermaid()
        assert '"t1<br/>列提纲"' in text
        assert "-->" in text
        # 节点 id 在箭头两侧，务必是 ASCII（t1 / t2）
        assert "t1 --> t2" in text

    def test_describe_lists_every_node(self, diamond: TaskGraph) -> None:
        """``describe`` 是给人看的：每个节点都得在。"""
        text = diamond.describe()
        for node_id in ("a", "b", "c", "d", "e"):
            assert node_id in text


class TestBuildGraph:
    """``build_graph`` 的兜底语义。"""

    def test_serial_chain_by_default(self) -> None:
        """不传 ``depends_on`` 时串行链 —— 串行一定能跑。"""
        graph = build_graph("三步走", subtasks=["一", "二", "三"])
        assert [_.id for _ in graph.topological_order()] == ["t1", "t2", "t3"]
        assert graph.node("t2").depends_on == ["t1"]
        assert graph.node("t1").depends_on == []

    def test_explicit_dependencies_win(self) -> None:
        """``depends_on`` 是**与 subtasks 等长**的列表，不是字典。"""
        graph = build_graph(
            "并行",
            subtasks=["收集", "写作"],
            depends_on=[[], []],  # 两个都无依赖 = 可并行
        )
        assert [_.id for _ in graph.ready()] == ["t1", "t2"]

    def test_mismatched_depends_on_length_rejected(self) -> None:
        """长度对不上要报错，不能默默错位（错位比报错难查十倍）。"""
        with pytest.raises(TaskGraphError, match="长度必须一致"):
            build_graph("x", subtasks=["一", "二"], depends_on=[[]])

    def test_empty_subtasks_still_valid(self) -> None:
        """空任务清单不该炸 —— 它是一张合法（虽然没用）的空图。"""
        graph = build_graph("无事可做", subtasks=[])
        assert graph.nodes == {}
        assert graph.is_complete()


# ======================================================================
# 四 · plan_digest
# ======================================================================
class TestPlanDigest:
    """摘要必须稳定、且对"结构变化"敏感。"""

    def test_stable_across_calls(self, diamond: TaskGraph) -> None:
        """同一张图反复算，结果一致且长度固定。"""
        assert plan_digest(diamond) == plan_digest(diamond)
        assert len(plan_digest(diamond)) == 16

    def test_independent_of_object_identity(self) -> None:
        """两张内容相同的图（不同对象）摘要相同。"""
        assert plan_digest(build_graph("目标", subtasks=["一", "二"])) == plan_digest(
            build_graph("目标", subtasks=["一", "二"]),
        )

    def test_independent_of_timestamps(self, diamond: TaskGraph) -> None:
        """摘要里不含 ``created_at`` —— 否则"没变"会被时间戳判成"变了"。"""
        before = plan_digest(diamond)
        diamond.created_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert plan_digest(diamond) == before

    def test_sensitive_to_status(self, diamond: TaskGraph) -> None:
        """状态变了摘要必须变 —— 否则它无法回答"计划有没有真的动"。"""
        before = plan_digest(diamond)
        diamond.mark("a", TaskStatus.RUNNING)
        assert plan_digest(diamond) != before

    def test_sensitive_to_attempts(self) -> None:
        """尝试次数变了摘要也要变。"""
        graph = build_graph("目标", subtasks=["一"])
        before = plan_digest(graph)
        graph.mark("t1", TaskStatus.RUNNING)
        graph.mark("t1", TaskStatus.FAILED, error="挂了")
        assert plan_digest(graph) != before

    def test_sensitive_to_dependency(self) -> None:
        """依赖关系变了摘要必须变 —— 结构才是这张图的关键。"""
        first = build_graph("目标", subtasks=["一", "二"])
        second = build_graph("目标", subtasks=["一", "二"])
        second.node("t2").depends_on = []
        assert plan_digest(first) != plan_digest(second)


# ======================================================================
# 五 · PlanStore：落盘与断点续跑
# ======================================================================
class TestPlanStoreFiles:
    """文件层的性质：原子写、修订号、防穿越、损坏容错。"""

    async def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        """存进去再读出来，节点与状态逐字相等。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一", "二"])
        graph.mark("t1", TaskStatus.RUNNING)
        graph.mark("t1", TaskStatus.DONE, artifact="产物")
        await store.save("p1", graph)

        back = await store.load("p1")
        assert sorted(back.nodes) == ["t1", "t2"]
        assert back.node("t1").status is TaskStatus.DONE
        assert back.node("t1").artifacts == ["产物"]
        assert back.node("t1").attempts == 1

    async def test_loaded_graph_is_a_copy(self, tmp_path: Path) -> None:
        """读回来的是**新对象**，改它不会污染磁盘上的那份。"""
        store = PlanStore(tmp_path / "plans")
        await store.save("p1", build_graph("目标", subtasks=["一"]))
        first = await store.load("p1")
        first.mark("t1", TaskStatus.DONE, artifact="改了内存")
        assert (await store.load("p1")).node("t1").status is TaskStatus.PENDING

    async def test_no_tmp_file_left_behind(self, tmp_path: Path) -> None:
        """原子替换之后不该留下 ``.tmp``；留下就说明写盘路径被改坏了。"""
        plan_dir = tmp_path / "plans"
        store = PlanStore(plan_dir)
        await store.save("p1", build_graph("目标", subtasks=["一"]))
        assert list(plan_dir.glob("*.tmp")) == []
        assert [_.name for _ in plan_dir.glob("*.json")] == ["p1.json"]

    async def test_revisions_increment(self, tmp_path: Path) -> None:
        """反复保存同一个 ``plan_id``：``revisions`` 递增，这是"改过几次"。

        第一次保存就是 ``1``（``PlanRecord.revisions`` 的默认值是 1，因为
        "写下去过一次"本身已经是一次修订，0 会让"从没保存过"和"保存过"混淆）。
        """
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一"])
        await store.save("p1", graph)
        assert (await store.load_record("p1")).revisions == 1
        await store.save("p1", graph)
        await store.save("p1", graph)
        assert (await store.load_record("p1")).revisions == 3

    async def test_digest_matches_record(self, tmp_path: Path) -> None:
        """记录里的 ``digest`` 与现算的一致 —— 它必须能被外部独立复算。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一", "二"])
        await store.save("p1", graph)
        record = await store.load_record("p1")
        assert record.digest == plan_digest(graph)
        assert record.plan_id == "p1"
        assert record.updated_at

    async def test_plan_path_rejects_traversal(self, tmp_path: Path) -> None:
        """``plan_id`` 不许带路径分隔符 —— 否则 ``..`` 能写到目录外面。"""
        store = PlanStore(tmp_path / "plans")
        for bad in ("../etc/passwd", "a/b", "..", ".", "a\\b"):
            with pytest.raises(ValueError, match="路径分隔符"):
                store.plan_path(bad)
        assert store.plan_path("ok-1").name == "ok-1.json"

    async def test_missing_plan_raises_plan_not_found(self, tmp_path: Path) -> None:
        """不存在的计划给专门的异常，而不是 ``FileNotFoundError``。"""
        store = PlanStore(tmp_path / "plans")
        with pytest.raises(PlanNotFoundError):
            await store.load("ghost")
        with pytest.raises(PlanNotFoundError):
            await store.load_record("ghost")

    async def test_corrupt_file_is_not_fatal(self, tmp_path: Path) -> None:
        """半个 JSON（断电现场）视为"不存在"，而不是让整个列表操作炸掉。"""
        plan_dir = tmp_path / "plans"
        store = PlanStore(plan_dir)
        await store.save("good", build_graph("目标", subtasks=["一"]))
        (plan_dir / "broken.json").write_text("{ 这不是 json", encoding="utf-8")
        assert await store.list_plans() == ["broken", "good"]
        with pytest.raises(PlanNotFoundError):
            await store.load("broken")
        # 坏文件被跳过，好的那个仍然能当"最近的计划"
        assert await store.latest_plan_id() == "good"

    async def test_list_latest_delete(self, tmp_path: Path) -> None:
        """列 / 取最近 / 删除三个管理动作。"""
        store = PlanStore(tmp_path / "plans")
        assert await store.list_plans() == []
        assert await store.latest_plan_id() is None
        await store.save("a-plan", build_graph("A", subtasks=["一"]))
        await store.save("b-plan", build_graph("B", subtasks=["一"]))
        assert await store.list_plans() == ["a-plan", "b-plan"]
        assert (await store.latest_plan_id()) in {"a-plan", "b-plan"}
        assert await store.delete("a-plan") is True
        assert await store.delete("a-plan") is False
        assert await store.list_plans() == ["b-plan"]

    async def test_store_without_session_id_rejected(self, tmp_path: Path) -> None:
        """给了事件存储却不给 ``session_id`` 直接拒绝：seq 无处分配。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        try:
            with pytest.raises(ValueError, match="session_id"):
                PlanStore(tmp_path / "plans", store=session_store)
        finally:
            await session_store.aclose()

    async def test_plan_record_survives_json(self, tmp_path: Path) -> None:
        """``PlanRecord`` 的 JSON 里带 gen 版本号与修订号，便于将来演进格式。"""
        store = PlanStore(tmp_path / "plans")
        await store.save("p1", build_graph("目标", subtasks=["一"]))
        raw = json.loads((tmp_path / "plans" / "p1.json").read_text(encoding="utf-8"))
        assert raw["plan_id"] == "p1"
        assert "v" in raw and "graph" in raw and "digest" in raw


class TestPlanStoreResume:
    """断点续跑的三条归一化规则。"""

    async def test_running_becomes_pending(self, tmp_path: Path) -> None:
        """``running`` → ``pending``，``attempts`` 保留。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一", "二"])
        graph.mark("t1", TaskStatus.DONE, artifact="完成")
        graph.mark("t2", TaskStatus.RUNNING)  # 进程在这里被杀
        await store.save("p1", graph)

        resumed = await store.resume("p1")
        assert resumed.node("t1").status is TaskStatus.DONE
        assert resumed.node("t2").status is TaskStatus.PENDING
        assert resumed.node("t2").attempts == 1, "attempts 要留给上层做熔断"
        assert [_.id for _ in resumed.ready()] == ["t2"]

    async def test_terminal_statuses_preserved(self, tmp_path: Path) -> None:
        """已做成的活绝不重跑：``done`` / ``failed`` 原样保留。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一", "二", "三"])
        graph.mark("t1", TaskStatus.DONE, artifact="产物一")
        graph.mark("t2", TaskStatus.FAILED, error="挂了")
        await store.save("p1", graph)

        resumed = await store.resume("p1")
        assert resumed.node("t1").status is TaskStatus.DONE
        assert resumed.node("t1").artifacts == ["产物一"]
        assert resumed.node("t2").status is TaskStatus.FAILED
        assert resumed.node("t2").error == "挂了"
        assert resumed.node("t3").status is TaskStatus.BLOCKED

    async def test_blocked_recomputed_after_manual_fix(self, tmp_path: Path) -> None:
        """上游事后被手工改回 done，恢复时必须自愈出可调度的图。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一", "二"])
        graph.mark("t1", TaskStatus.FAILED, error="挂了")
        assert graph.node("t2").status is TaskStatus.BLOCKED
        # 现场有人补做了上游，但**没有**重算阻塞（例如直接改的 JSON）
        graph.node("t1").status = TaskStatus.DONE
        await store.save("p1", graph)

        resumed = await store.resume("p1")
        assert resumed.node("t2").status is TaskStatus.PENDING
        assert [_.id for _ in resumed.ready()] == ["t2"]

    async def test_resume_does_not_mutate_disk(self, tmp_path: Path) -> None:
        """只 ``resume`` 不 ``save`` 时磁盘上仍是原样（恢复本身要显式落盘）。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一"])
        graph.mark("t1", TaskStatus.RUNNING)
        await store.save("p1", graph)
        await store.resume("p1")
        assert (await store.load("p1")).node("t1").status is TaskStatus.RUNNING

    async def test_save_and_resume_persists(self, tmp_path: Path) -> None:
        """``save_and_resume`` 把归一化结果写回，第二次恢复不会重复告警。"""
        store = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一"])
        graph.mark("t1", TaskStatus.RUNNING)
        await store.save("p1", graph)
        await store.save_and_resume("p1")
        assert (await store.load("p1")).node("t1").status is TaskStatus.PENDING

    async def test_resume_missing_plan(self, tmp_path: Path) -> None:
        """恢复一个不存在的计划给 ``PlanNotFoundError``。"""
        store = PlanStore(tmp_path / "plans")
        with pytest.raises(PlanNotFoundError):
            await store.resume("ghost")


class TestPlanStoreEventSourcing:
    """与第 9 讲会话日志的对接。"""

    async def test_snapshot_written_as_custom_event(self, tmp_path: Path) -> None:
        """每次保存都往会话日志追加一条 ``CUSTOM`` 事件。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        store = PlanStore(tmp_path / "plans", store=session_store, session_id="s1")
        graph = build_graph("目标", subtasks=["一", "二"])
        await store.save("p1", graph)
        graph.mark("t1", TaskStatus.DONE, artifact="产物")
        await store.save("p1", graph)

        history = await store.history("p1")
        assert len(history) == 2
        assert all(_.kind is EventKind.CUSTOM for _ in history)
        assert [_.seq for _ in history] == sorted(_.seq for _ in history)
        first, second = history
        assert first.payload["name"] == "plan_snapshot"
        assert first.payload["data"]["plan_id"] == "p1"
        assert first.payload["data"]["progress"] == {
            "pending": 2,
            "running": 0,
            "blocked": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
        }
        assert first.payload["data"]["nodes"] == ["t1", "t2"]
        assert second.payload["data"]["progress"]["done"] == 1
        await session_store.aclose()

    async def test_payload_has_no_full_graph(self, tmp_path: Path) -> None:
        """payload 里不许塞整张图 —— 日志会被撑爆。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        store = PlanStore(tmp_path / "plans", store=session_store, session_id="s1")
        await store.save("p1", build_graph("目标", subtasks=["一"]))
        history = await store.history("p1")
        data = history[0].payload["data"]
        assert set(data) == {"plan_id", "digest", "progress", "nodes"}
        assert len(data["digest"]) == 16
        await session_store.aclose()

    async def test_history_filters_by_plan_id(self, tmp_path: Path) -> None:
        """两个计划的快照在同一个会话里互不污染。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        store = PlanStore(tmp_path / "plans", store=session_store, session_id="s1")
        await store.save("p1", build_graph("目标一", subtasks=["一"]))
        await store.save("p2", build_graph("目标二", subtasks=["一"]))
        await store.save("p1", build_graph("目标一", subtasks=["一"]))
        assert len(await store.history("p1")) == 2
        assert len(await store.history("p2")) == 1
        assert await store.history("p3") == []
        await session_store.aclose()

    async def test_resume_writes_resumed_flag(self, tmp_path: Path) -> None:
        """恢复动作本身也进日志，且带上 ``resumed`` / ``revived`` 两个标记。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        store = PlanStore(tmp_path / "plans", store=session_store, session_id="s1")
        graph = build_graph("目标", subtasks=["一"])
        graph.mark("t1", TaskStatus.RUNNING)
        await store.save("p1", graph)
        await store.resume("p1")

        history = await store.history("p1")
        assert history[-1].payload["data"]["resumed"] is True
        assert history[-1].payload["data"]["revived"] == ["t1"]
        await session_store.aclose()

    async def test_events_satisfy_session_invariants(self, tmp_path: Path) -> None:
        """写进去的事件不能让第 9 讲的 seq 不变式（无洞、无重复）失效。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        store = PlanStore(tmp_path / "plans", store=session_store, session_id="s1")
        for index in range(3):
            await store.save(f"p{index}", build_graph(f"目标{index}", subtasks=["一"]))
        events = await session_store.verify_invariants("s1")
        assert [_.seq for _ in events] == [0, 1, 2]
        await session_store.aclose()

    async def test_no_store_means_no_events(self, tmp_path: Path) -> None:
        """不配事件存储时退化成纯文件持久化，``history`` 给空列表。"""
        store = PlanStore(tmp_path / "plans")
        await store.save("p1", build_graph("目标", subtasks=["一"]))
        assert await store.history("p1") == []
        assert (await store.load_record("p1")).revisions == 1
        assert (await store.load_record("p1")).digest

    async def test_describe_mentions_event_sourcing(self, tmp_path: Path) -> None:
        """``describe`` 要把"有没有事件溯源"讲清楚。"""
        assert "event_sourcing=off" in PlanStore(tmp_path / "plans").describe()
        session_store = JsonlSessionStore(tmp_path / "sessions")
        wired = PlanStore(tmp_path / "plans2", store=session_store, session_id="s1")
        assert "event_sourcing=on" in wired.describe()
        await session_store.aclose()


# ======================================================================
# 六 · SOPDefinition：YAML → 定义
# ======================================================================
class TestSOPDefinition:
    """定义的解析、校验、环境插值。"""

    def test_from_dict_happy_path(self) -> None:
        """最小可用的定义，默认值要合理。"""
        definition = SOPDefinition.from_dict(
            {
                "name": "demo",
                "steps": [{"subject": "s1", "description": "做点事"}],
            },
        )
        assert definition.name == "demo"
        assert definition.steps[0].max_attempts == 3, "默认 3 次"
        assert definition.steps[0].verify is True, "默认要验证"

    def test_from_dict_missing_description_rejected(self) -> None:
        """``description`` 必填 —— "这一步要达成什么"不能省。"""
        with pytest.raises(SOPError, match="SOP 定义非法"):
            SOPDefinition.from_dict({"name": "bad", "steps": [{"subject": "x"}]})

    def test_from_dict_empty_steps_rejected(self) -> None:
        """空 steps 直接拒绝。"""
        with pytest.raises(SOPError, match="SOP 定义非法"):
            SOPDefinition.from_dict({"name": "bad", "steps": []})

    def test_from_dict_bad_type_rejected(self) -> None:
        """``verify`` 写成无法解释的字符串要报错。

        **注意 pydantic 会替你做真值转换**：``"yes"`` / ``"on"`` / ``"1"``
        都会被转成 ``True``（详见 pydantic 的 bool 解析规则），所以"能不能转"
        才是这条测试的判据 —— 用 ``"maybe"`` 才真的会炸。
        """
        assert SOPDefinition.from_dict(
            {
                "name": "ok",
                "steps": [{"subject": "x", "description": "y", "verify": "yes"}],
            },
        ).steps[0].verify is True
        with pytest.raises(SOPError, match="SOP 定义非法"):
            SOPDefinition.from_dict(
                {
                    "name": "bad",
                    "steps": [{"subject": "x", "description": "y", "verify": "maybe"}],
                },
            )
        with pytest.raises(SOPError, match="SOP 定义非法"):
            SOPDefinition.from_dict(
                {
                    "name": "bad",
                    "steps": [{"subject": "x", "description": "y", "verify": 3}],
                },
            )

    def test_from_dict_extra_field_rejected(self) -> None:
        """拼错的字段名要报错。"""
        with pytest.raises(SOPError, match="SOP 定义非法"):
            SOPDefinition.from_dict(
                {
                    "name": "bad",
                    "steps": [{"subject": "x", "description": "y", "max_attemp": 2}],
                },
            )

    def test_from_dict_zero_attempts_rejected(self) -> None:
        """``max_attempts`` 至少 1，否则这一步永远不可能过。"""
        with pytest.raises(SOPError, match="SOP 定义非法"):
            SOPDefinition.from_dict(
                {
                    "name": "bad",
                    "steps": [{"subject": "x", "description": "y", "max_attempts": 0}],
                },
            )

    def test_from_file_yaml(self, sop_yaml: Path) -> None:
        """从 YAML 文件读，步骤顺序即运行顺序。"""
        definition = SOPDefinition.from_file(sop_yaml)
        assert definition.name == "two_step"
        assert [_.subject for _ in definition.steps] == ["draft", "publish"]
        assert [_.verify for _ in definition.steps] == [True, False]
        assert [_.max_attempts for _ in definition.steps] == [2, 1]

    def test_from_file_missing(self, tmp_path: Path) -> None:
        """文件不存在给 ``FileNotFoundError``（不是 SOPError）。"""
        with pytest.raises(FileNotFoundError):
            SOPDefinition.from_file(tmp_path / "nope.yaml")

    def test_from_file_json_root_not_object(self, tmp_path: Path) -> None:
        """JSON 根节点不是对象时给 ``SOPError``，不是 ``AttributeError``。"""
        target = tmp_path / "list.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(SOPError, match="根节点必须是 map"):
            SOPDefinition.from_file(target)

    def test_from_file_yaml_env_interpolation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``${VAR:-default}`` 插值来自第 2 讲的 loader，同一份 SOP 可指向不同产物。"""
        target = tmp_path / "interp.yaml"
        target.write_text(
            "name: interp\n"
            "description: '目标版本 ${SOP_TARGET_VERSION:-v9.9.9}'\n"
            "steps:\n"
            "  - subject: s\n"
            "    description: d\n",
            encoding="utf-8",
        )
        assert "v9.9.9" in SOPDefinition.from_file(target).description
        monkeypatch.setenv("SOP_TARGET_VERSION", "v1.2.3")
        assert "v1.2.3" in SOPDefinition.from_file(target).description

    def test_real_sops_dir_definitions_load(self) -> None:
        """源码树里真实存在的每一份 SOP 都必须能装配（防止手写 YAML 写坏）。"""
        sops_dir = (
            Path(__file__).resolve().parents[1] / "harness_kit" / "planning" / "sops"
        )
        files = sorted(sops_dir.glob("*.yaml")) + sorted(sops_dir.glob("*.yml"))
        assert files, "planning/sops 下至少要有一份真实定义"
        for path in files:
            definition = SOPDefinition.from_file(path)
            assert definition.steps
            assert all(_.description.strip() for _ in definition.steps)


# ======================================================================
# 七 · HarnessSOP：状态机
# ======================================================================
class TestHarnessSOPRuntime:
    """用 ``AgentLike`` stub 把 SOP 状态机跑到底（0 次 LLM）。"""

    async def test_happy_path_two_steps(self, sop_yaml: Path) -> None:
        """验证者一路放行：两步各 1 次尝试，交付物是最后一步。"""
        executor = StubAgent("exec", payloads=[handover("草稿"), handover("发布稿")])
        verifier = StubAgent("verif", payloads=[{"passed": True, "message": ""}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)

        result = await sop.run(initial_input="写点什么")
        assert result.ok is True
        assert result.phase == "completed"
        assert result.awaiting is False
        assert [_.subject for _ in result.steps] == ["draft", "publish"]
        assert [_.attempts for _ in result.steps] == [1, 1]
        assert result.failed_steps == []
        assert result.steps[1].submission == "发布稿"
        assert result.output == "发布稿"
        # 执行者拿 _Handover、验证者拿 _Verdict，两侧不混
        assert "_Handover" in executor.schemas
        assert "_Verdict" in verifier.schemas
        assert "_Verdict" not in executor.schemas

    async def test_handover_reaches_next_step(self, sop_yaml: Path) -> None:
        """上一步交出的东西会包进 ``<handover>`` 交给下一步（步骤之间不共享上下文）。"""
        seen: list[str] = []

        class Recorder(StubAgent):
            """把 `inputs` 记下来，用于检查跨步传递。"""

            async def reply_stream(self, inputs: Any = None, **kw: Any):  # type: ignore[override]
                seen.append(str(inputs))
                async for item in super().reply_stream(inputs=inputs, **kw):
                    yield item

        executor = Recorder("exec", payloads=[handover("第一步的交付"), handover("最终稿")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        await sop.run(initial_input="初始输入")

        assert "初始输入" in seen[0], "第一步该拿到整轮输入"
        assert "第一步的交付" in seen[-1], "第二步该拿到上一步的 handover"
        assert "<handover" in seen[-1]

    async def test_refusal_clears_submission_and_retries(self, sop_yaml: Path) -> None:
        """被拒一次 → 再来一次；重做时执行者被**再次**调用。"""
        executor = StubAgent(
            "exec",
            payloads=[handover("草稿 v1"), handover("草稿 v2"), handover("发布稿")],
        )
        verifier = StubAgent(
            "verif",
            payloads=[
                {"passed": False, "message": "缺少验证命令"},
                {"passed": True, "message": ""},
            ],
        )
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")

        assert result.ok is True
        assert result.steps[0].attempts == 2
        assert result.steps[0].passed is True
        # 两步各调用一次以上：draft 两次尝试 + publish 一次
        assert len([_ for _ in executor.calls if _ == "reply_stream"]) == 3
        # 被拒的那次 submission 必须被清掉，不能把"被拒的稿子"当产物往下传
        assert result.steps[0].submission == "草稿 v2"
        assert result.steps[0].message == ""

    async def test_refusal_message_goes_back_to_executor(
        self,
        sop_yaml: Path,
    ) -> None:
        """拒绝理由会原样回给执行者 —— 否则它会原封不动再交一遍。"""
        seen: list[str] = []

        class Recorder(StubAgent):
            """记录每次拿到的 `inputs`。"""

            async def reply_stream(self, inputs: Any = None, **kw: Any):  # type: ignore[override]
                seen.append(str(inputs))
                async for item in super().reply_stream(inputs=inputs, **kw):
                    yield item

        executor = Recorder(
            "exec",
            payloads=[handover("草稿 v1"), handover("草稿 v2"), handover("发布稿")],
        )
        verifier = StubAgent(
            "verif",
            payloads=[{"passed": False, "message": "缺少验证命令"}, {"passed": True}],
        )
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        await sop.run(initial_input="go")
        assert "缺少验证命令" in seen[1], "第二次尝试必须带着上一次的拒绝理由"

    async def test_attempt_budget_exhausted_marks_failed(self, sop_yaml: Path) -> None:
        """预算耗尽 → 该步 FAILED，整轮 FAILED，且尝试次数正好等于预算。"""
        executor = StubAgent(
            "exec",
            payloads=[handover(f"draft-{i}") for i in range(10)],
        )
        verifier = StubAgent(
            "verif",
            payloads=[{"passed": False, "message": "不行"} for _ in range(10)],
        )
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")

        assert result.phase == "failed"
        assert result.ok is False
        assert result.failed_steps == ["draft"]
        assert result.steps[0].attempts == 2, "max_attempts=2，不能多试第三次"
        assert result.steps[1].phase == "pending", "第一步没过，第二步不该被启动"
        assert [_.subject for _ in result.failed_results] == ["draft"]

    async def test_verify_false_skips_verifier(self, tmp_path: Path) -> None:
        """``verify: false`` → ``SOPStep(verifier=None)``，做完即过。"""
        target = tmp_path / "noverify.yaml"
        target.write_text(
            "name: nov\nsteps:\n  - subject: only\n    description: d\n"
            "    max_attempts: 1\n    verify: false\n",
            encoding="utf-8",
        )
        executor = StubAgent("exec", payloads=[handover("做完了")])
        verifier = StubAgent("verif", payloads=[{"passed": False}])
        sop = HarnessSOP(sop_path=target, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")

        assert result.ok is True
        assert result.steps[0].attempts == 1
        assert result.steps[0].passed is True
        assert result.steps[0].message == ""
        assert verifier.calls == [], "配了 verifier 但这一步不验证，它一次都不该被叫"

    async def test_self_verify_fallback(self, tmp_path: Path) -> None:
        """不给 verifier 且 ``verify: true`` 时退化为 self-verify（同体判定）。"""
        target = tmp_path / "selfverify.yaml"
        target.write_text(
            "name: sv\nsteps:\n  - subject: only\n    description: d\n"
            "    max_attempts: 1\n    verify: true\n",
            encoding="utf-8",
        )
        agent = StubAgent("solo", payloads=[handover("产物"), {"passed": True}])
        sop = HarnessSOP(sop_path=target, agent=agent)
        result = await sop.run(initial_input="go")
        assert result.ok is True
        assert "_Verdict" in agent.schemas and "_Handover" in agent.schemas

    async def test_sop_step_wiring(self, sop_yaml: Path) -> None:
        """装配结果：``verify=False`` 的那一步 verifier 必须是 ``None``。"""
        agent = StubAgent(
            "solo",
            payloads=[handover("a"), {"passed": True}, handover("b")],
        )
        sop = HarnessSOP(sop_path=sop_yaml, agent=agent)
        assert len(sop.sop.steps) == 2
        assert sop.sop.steps[0].verifier is not None
        assert sop.sop.steps[1].verifier is None
        assert sop.sop.steps[0].max_attempts == 2
        assert sop.sop.name == "two_step"
        result = await sop.run(initial_input="go")
        assert result.ok is True
        assert result.steps[1].submission == "b"

    async def test_current_state_before_run(self, sop_yaml: Path) -> None:
        """没跑过时 ``current_state`` 是 ``pending``。"""
        sop = HarnessSOP(sop_path=sop_yaml, agent=StubAgent("e"))
        assert sop.current_state == "pending"

    async def test_current_state_after_run(self, sop_yaml: Path) -> None:
        """跑完之后 ``current_state`` 反映引擎的真实阶段。"""
        executor = StubAgent("exec", payloads=[handover("x"), handover("y")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        await sop.run(initial_input="go")
        assert sop.current_state == "completed"

    async def test_name_override(self, sop_yaml: Path) -> None:
        """``name`` 覆盖定义里的名字（同一个文件跑出两条不同流程）。"""
        sop = HarnessSOP(sop_path=sop_yaml, agent=StubAgent("e"), name="renamed")
        assert sop.name == "renamed"
        assert sop.sop.name == "renamed"

    async def test_with_step_agents_rebuilds(self, sop_yaml: Path) -> None:
        """``with_step_agents`` 换掉某一步的执行者并重建 ``SOP``。"""
        default = StubAgent("default", payloads=[handover("d2")])
        special = StubAgent("special", payloads=[handover("s1")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=default, verifier=verifier)
        sop.with_step_agents({"draft": special})
        result = await sop.run(initial_input="go")

        assert result.ok is True
        assert special.calls, "被覆盖的那一步必须走 special"
        assert result.steps[0].submission == "s1"
        assert result.steps[1].submission == "d2"

    async def test_run_with_state_of_wrong_length(self, sop_yaml: Path) -> None:
        """流程被改短/改长后旧状态不能硬套，要明确报错。"""
        sop = HarnessSOP(sop_path=sop_yaml, agent=StubAgent("exec"))
        state = SOPRunState.model_validate(
            {
                "steps": [{"subject": "a"}, {"subject": "b"}, {"subject": "c"}],
            },
        )
        with pytest.raises(SOPError, match="3 steps"):
            await sop.run(initial_input="go", state=state)

    async def test_empty_run_state_initializes_steps(self, sop_yaml: Path) -> None:
        """全新的 ``SOPRunState()`` 会被引擎按 SOP 补齐到正确步数。"""
        sop = HarnessSOP(
            sop_path=sop_yaml,
            agent=StubAgent("exec", payloads=[handover("x"), handover("y")]),
            verifier=StubAgent("verif", payloads=[{"passed": True}]),
        )
        result = await sop.run(initial_input="go", state=SOPRunState())
        assert len(result.run_state["steps"]) == 2
        assert result.ok is True


class TestHarnessSOPPersistence:
    """跨进程续跑：``save_state`` / ``load_state``。"""

    async def test_save_and_load_roundtrip(self, tmp_path: Path, sop_yaml: Path) -> None:
        """运行态能落盘、能读回、且内容足以重建引擎。"""
        executor = StubAgent("exec", payloads=[handover("x"), handover("y")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")

        target = sop.save_state(tmp_path / "state.json", result)
        assert target.exists()
        raw = json.loads(target.read_text(encoding="utf-8"))
        # 运行态里**只有 SOP 自己的状态**，没有执行者的上下文
        assert set(raw) == {"id", "inputs", "steps", "created_at", "phase"}
        assert raw["phase"] == "completed"
        assert len(raw["steps"]) == 2

        loaded = sop.load_state(target)
        assert isinstance(loaded, SOPRunState)
        assert loaded.phase.value == "completed"

    async def test_resume_completed_does_not_rerun(
        self,
        tmp_path: Path,
        sop_yaml: Path,
    ) -> None:
        """已完成的步骤在新引擎里被跳过：执行者一次都不该被调用。

        这就是"断点恢复"的最小充分条件 —— 状态里记着"哪几步已经 done"。
        """
        executor = StubAgent("exec", payloads=[handover("x"), handover("y")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")
        state_file = sop.save_state(tmp_path / "state.json", result)

        fresh = StubAgent("fresh")
        resumed = HarnessSOP(sop_path=sop_yaml, agent=fresh, verifier=fresh)
        resumed_result = await resumed.run(
            initial_input="ignored",
            state=resumed.load_state(state_file),
        )
        assert resumed_result.phase == "completed"
        assert resumed_result.ok is True
        assert fresh.calls == [], "已完成的 SOP 不该再调一次执行者"
        assert resumed_result.steps[0].submission == "x"

    async def test_load_state_missing_file(self, tmp_path: Path, sop_yaml: Path) -> None:
        """状态文件不存在给 ``FileNotFoundError``。"""
        sop = HarnessSOP(sop_path=sop_yaml, agent=StubAgent("e"))
        with pytest.raises(FileNotFoundError):
            sop.load_state(tmp_path / "nope.json")

    async def test_load_state_corrupt(self, tmp_path: Path, sop_yaml: Path) -> None:
        """状态文件被写坏给 ``SOPError``，不是 pydantic 的原始异常。"""
        sop = HarnessSOP(sop_path=sop_yaml, agent=StubAgent("e"))
        target = tmp_path / "bad.json"
        target.write_text('{"steps": "not-a-list"}', encoding="utf-8")
        with pytest.raises(SOPError, match="SOP 运行态非法"):
            sop.load_state(target)

    async def test_partial_state_resumes_at_right_step(
        self,
        tmp_path: Path,
        sop_yaml: Path,
    ) -> None:
        """手工造一个"第一步已过、第二步没跑"的状态，引擎应从第二步接上。"""
        sop = HarnessSOP(
            sop_path=sop_yaml,
            agent=StubAgent("probe", payloads=[handover("下半段")]),
        )
        state = SOPRunState.model_validate(
            {
                "steps": [
                    {
                        "phase": "completed",
                        "submission": [{"type": "text", "text": "上半段的稿子"}],
                        "verifications": [{"passed": True, "message": ""}],
                    },
                    {},
                ],
            },
        )
        result = await sop.run(initial_input="继续", state=state)
        assert result.ok is True
        assert result.steps[0].submission == "上半段的稿子"
        assert result.steps[1].submission == "下半段"
        assert result.steps[1].attempts == 1

    async def test_run_state_is_plain_json(self, tmp_path: Path, sop_yaml: Path) -> None:
        """``run_state`` 必须是可直接 ``json.dumps`` 的纯数据（无对象引用）。"""
        executor = StubAgent("exec", payloads=[handover("x"), handover("y")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")
        text = json.dumps(result.run_state, ensure_ascii=False)
        back = json.loads(text)
        assert back["steps"][0]["phase"] == "completed"
        assert back["steps"][0]["submission"][0]["text"] == "x"

    async def test_resume_via_msg(self, tmp_path: Path, sop_yaml: Path) -> None:
        """``resume`` 用一条普通 ``Msg`` 接着跑，不依赖 AgentScope 的确认事件。"""
        executor = StubAgent("exec", payloads=[handover("x"), handover("y")])
        verifier = StubAgent("verif", payloads=[{"passed": True}])
        sop = HarnessSOP(sop_path=sop_yaml, agent=executor, verifier=verifier)
        result = await sop.run(initial_input="go")
        answer = Msg(
            name="user",
            role="user",
            content=[TextBlock(text="这是外部补上的答案")],
        )
        again = await sop.resume(answer=answer, state=result.run_state)
        assert again.phase == "completed"
        assert again.ok is True


# ======================================================================
# 八 · HarnessPlanner：拆解协议与调度（stub 驱动，0 次 LLM）
# ======================================================================
class TestHarnessPlannerDecompose:
    """``decompose`` 的契约：结构化输出 → 校验过的图。"""

    async def test_decompose_happy_path(self) -> None:
        """模型给出的 subtasks 被翻成 ``TaskGraph``，``metadata`` 带上 capability。"""
        agent = StubAgent(
            "planner",
            payloads=[
                {
                    "reasoning": "先收集再写",
                    "subtasks": [
                        {"id": "collect", "goal": "收集素材", "depends_on": []},
                        {
                            "id": "write",
                            "goal": "写正文",
                            "depends_on": ["collect"],
                            "capability": "writing",
                        },
                    ],
                },
            ],
        )
        planner = HarnessPlanner(agent=agent)
        graph = await planner.decompose("写一篇短文")

        assert graph.goal == "写一篇短文"
        assert sorted(graph.nodes) == ["collect", "write"]
        assert graph.node("write").metadata["capability"] == "writing"
        assert [_.id for _ in graph.ready()] == ["collect"]
        assert agent.schemas == ["PlanDraft"], "必须走 structured_schema=PlanDraft"

    async def test_decompose_without_structured_output(self) -> None:
        """模型没给结构化输出 → ``RuntimeError``，不猜、不退化。"""
        planner = HarnessPlanner(agent=StubAgent("planner", payloads=[{}]))
        with pytest.raises(RuntimeError, match="没有产出结构化输出"):
            await planner.decompose("目标")

    async def test_decompose_rejects_cycle(self) -> None:
        """模型拆出的环要在 ``decompose`` 就被拦住。"""
        agent = StubAgent(
            "planner",
            payloads=[
                {
                    "reasoning": "环",
                    "subtasks": [
                        {"id": "a", "goal": "A", "depends_on": ["b"]},
                        {"id": "b", "goal": "B", "depends_on": ["a"]},
                    ],
                },
            ],
        )
        with pytest.raises(TaskGraphError, match="成环"):
            await HarnessPlanner(agent=agent).decompose("目标")

    async def test_decompose_rejects_dangling(self) -> None:
        """悬空依赖同样在 ``decompose`` 拦住。"""
        agent = StubAgent(
            "planner",
            payloads=[
                {
                    "reasoning": "x",
                    "subtasks": [{"id": "a", "goal": "A", "depends_on": ["ghost"]}],
                },
            ],
        )
        with pytest.raises(TaskGraphError, match="悬空依赖"):
            await HarnessPlanner(agent=agent).decompose("目标")

    async def test_decompose_rejects_duplicate_id(self) -> None:
        """模型给出重复 id 也要拦。"""
        agent = StubAgent(
            "planner",
            payloads=[
                {
                    "reasoning": "x",
                    "subtasks": [
                        {"id": "a", "goal": "A", "depends_on": []},
                        {"id": "a", "goal": "A 又一遍", "depends_on": []},
                    ],
                },
            ],
        )
        with pytest.raises(TaskGraphError, match="已存在"):
            await HarnessPlanner(agent=agent).decompose("目标")

    def test_llm_facing_schemas_have_descriptions(self) -> None:
        """LLM 面向的 schema 每个字段都要有 description —— 它们是提示词的一部分。"""
        for name, field in PlanDraft.model_fields.items():
            assert field.description, f"PlanDraft.{name} 缺 description"
        for name, field in SubTaskDraft.model_fields.items():
            assert field.description, f"SubTaskDraft.{name} 缺 description"


class TestHarnessPlannerExecute:
    """``execute`` 的调度与失败隔离（stub 驱动，0 次 LLM）。"""

    async def test_execute_runs_topological_order(self) -> None:
        """``execute`` 按拓扑序把节点跑完，产物写进 ``artifacts``。"""
        agent = StubAgent("exec", texts=["A 的产物", "B 的产物", "C 的产物"])
        planner = HarnessPlanner(agent=agent, max_steps=10)
        graph = build_graph("三步", subtasks=["一", "二", "三"])

        result = await planner.execute(graph)
        assert result.ok is True
        assert result.steps_used == 3
        assert result.failed_nodes == []
        assert all(_.status is TaskStatus.DONE for _ in graph.nodes.values())
        assert graph.node("t1").artifacts == ["A 的产物"]
        # 3 个节点 + 1 次汇总（stub 不花真钱）
        assert len(agent.calls) == 4
        assert agent.calls[-1] == "reply", "汇总走 reply，节点走 reply_stream"

    async def test_execute_with_summarize_off(self) -> None:
        """``summarize=False`` 用本地拼装兜底，少一次调用。"""
        agent = StubAgent("exec", texts=["一", "二"])
        planner = HarnessPlanner(agent=agent, max_steps=10)
        graph = build_graph("两步", subtasks=["一", "二"])

        result = await planner.execute(graph, summarize=False)
        assert result.ok is True
        assert len(agent.calls) == 2
        assert "t1" in result.summary and "done" in result.summary

    async def test_max_steps_budget_stops_scheduling(self) -> None:
        """``max_steps`` 是跨节点预算：花完就停，剩下的留在 pending。"""
        agent = StubAgent("exec", texts=["一", "二", "三"])
        planner = HarnessPlanner(agent=agent, max_steps=2)
        graph = build_graph("四步", subtasks=["一", "二", "三", "四"])

        result = await planner.execute(graph, summarize=False)
        assert result.steps_used == 2
        assert graph.node("t3").status is TaskStatus.PENDING
        assert result.ok is False, "没跑完就不算 ok"

    async def test_node_exception_isolated(self) -> None:
        """单个节点抛异常不能带走整张图：标 FAILED 并让下游 blocked。"""

        class Exploding(StubAgent):
            """第二次 ``reply_stream`` 抛异常。"""

            async def reply_stream(self, **kw: Any):  # type: ignore[override]
                self.calls.append("reply_stream")
                if len(self.calls) > 1:
                    raise RuntimeError("模拟节点崩溃")
                yield Msg(
                    name=self.name,
                    role="assistant",
                    content=[TextBlock(text="第一步成了")],
                    finished_reason=ReplyFinishedReason.COMPLETED,
                )

        planner = HarnessPlanner(agent=Exploding("boom"), max_steps=10)
        graph = build_graph("两步", subtasks=["一", "二"])
        result = await planner.execute(graph, summarize=False)

        assert result.ok is False
        assert result.failed_nodes == ["t2"]
        assert "模拟节点崩溃" in (graph.node("t2").error or "")
        assert graph.node("t1").status is TaskStatus.DONE

    async def test_empty_output_marks_failed(self) -> None:
        """空产物算失败 —— 不能把"什么都没写"当成"做完了"。"""
        planner = HarnessPlanner(agent=StubAgent("exec", texts=["   "]), max_steps=10)
        graph = build_graph("一步", subtasks=["一"])
        result = await planner.execute(graph, summarize=False)
        assert result.ok is False
        assert result.failed_nodes == ["t1"]
        assert "空产物" in (graph.node("t1").error or "")

    async def test_tool_markup_output_marks_failed(self) -> None:
        """模型把工具调用写进正文时要判失败，不能把标记当报告传下去。"""
        planner = HarnessPlanner(
            agent=StubAgent("exec", texts=["<tool_call>{\"name\": \"Bash\"}</tool_call>"]),
            max_steps=10,
        )
        graph = build_graph("一步", subtasks=["一"])
        result = await planner.execute(graph, summarize=False)
        assert result.ok is False
        assert result.failed_nodes == ["t1"]

    async def test_max_steps_zero_still_summarizes(self) -> None:
        """``max_steps=0`` 一步都不跑，但仍要给出一个结果对象。"""
        planner = HarnessPlanner(agent=StubAgent("exec"), max_steps=0)
        graph = build_graph("两步", subtasks=["一", "二"])
        result = await planner.execute(graph, summarize=False)
        assert result.steps_used == 0
        assert result.ok is False
        assert all(_.status is TaskStatus.PENDING for _ in graph.nodes.values())

    async def test_run_composes_decompose_and_execute(self) -> None:
        """``run`` = ``decompose`` + ``execute``，一条调用跑完整件事。"""
        agent = StubAgent(
            "all",
            payloads=[
                {
                    "reasoning": "两步",
                    "subtasks": [
                        {"id": "a", "goal": "第一步", "depends_on": []},
                        {"id": "b", "goal": "第二步", "depends_on": ["a"]},
                    ],
                },
            ],
            texts=["产物一", "产物二"],
        )
        result = await HarnessPlanner(agent=agent, max_steps=10).run("写点东西")
        assert result.ok is True
        assert result.steps_used == 2
        assert sorted(result.graph.nodes) == ["a", "b"]

    async def test_replan_on_failure_requires_budget(self) -> None:
        """``replan_on_failure=True`` + ``max_replans=0`` 是静默失效，直接拒。"""
        with pytest.raises(TypeError, match="静默失效"):
            HarnessPlanner(agent=StubAgent("x"), replan_on_failure=True, max_replans=0)

    async def test_replan_without_pipeline_disabled(self) -> None:
        """默认不开重规划：``pipeline`` / ``recorder`` 都是 ``None``。"""
        planner = HarnessPlanner(agent=StubAgent("x"), max_steps=5)
        assert planner.pipeline is None
        assert planner.recorder is None

    async def test_aclose_is_safe_without_agent_aclose(self) -> None:
        """``aclose`` 面对一个没有 ``aclose`` 的鸭子对象不能炸。"""
        planner = HarnessPlanner(agent=StubAgent("x"))
        await planner.aclose()


# ======================================================================
# 九 · 端到端（全离线）：拆解 → 调度 → 落盘 → 崩 → 恢复 → 续跑
# ======================================================================
class TestEndToEndOffline:
    """把图、调度、持久化串起来跑一遍完整的长任务生命周期。"""

    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        """一次"跑到一半崩掉、重启后接着跑"的完整回放。"""
        session_store = JsonlSessionStore(tmp_path / "sessions")
        store = PlanStore(tmp_path / "plans", store=session_store, session_id="s1")

        # --- 拆解 ---
        decomposer = StubAgent(
            "planner",
            payloads=[
                {
                    "reasoning": "先收集再写再校",
                    "subtasks": [
                        {"id": "collect", "goal": "收集", "depends_on": []},
                        {"id": "write", "goal": "写", "depends_on": ["collect"]},
                        {"id": "review", "goal": "校", "depends_on": ["write"]},
                    ],
                },
            ],
        )
        graph = await HarnessPlanner(agent=decomposer, max_steps=10).decompose(
            "写一篇短文",
        )
        assert [_.id for _ in graph.ready()] == ["collect"]

        # --- 跑到一半：collect 完成，write 刚被取走时进程被杀 ---
        planner2 = HarnessPlanner(agent=StubAgent("exec", texts=["素材"]), max_steps=1)
        await planner2.execute(graph, summarize=False)
        assert graph.node("collect").status is TaskStatus.DONE
        assert graph.node("write").status is TaskStatus.PENDING

        graph.mark("write", TaskStatus.RUNNING)
        await store.save("plan-1", graph)

        # --- 重启：恢复 ---
        resumed = await store.save_and_resume("plan-1")
        assert resumed.node("collect").status is TaskStatus.DONE
        assert resumed.node("write").status is TaskStatus.PENDING
        assert resumed.node("write").attempts == 1
        assert resumed.node("review").status is TaskStatus.PENDING
        assert [_.id for _ in resumed.ready()] == ["write"]

        # --- 续跑到底 ---
        runner = StubAgent("exec", texts=["草稿 v2", "校对通过"])
        result = await HarnessPlanner(agent=runner, max_steps=10).execute(
            resumed,
            summarize=False,
        )
        assert result.ok is True
        assert resumed.is_successful()
        assert resumed.node("collect").artifacts == ["素材"], "已做成的活没重跑"
        await store.save("plan-1", resumed)

        # --- 审计：事件日志记下了整条时间线 ---
        history = await store.history("plan-1")
        assert len(history) == 4  # save / resume / save_and_resume 的 save / 最后一次
        progress = [_.payload["data"]["progress"] for _ in history]
        assert progress[0] == {
            "pending": 1,
            "running": 1,
            "blocked": 0,
            "done": 1,
            "failed": 0,
            "skipped": 0,
        }
        assert progress[-1] == {
            "pending": 0,
            "running": 0,
            "blocked": 0,
            "done": 3,
            "failed": 0,
            "skipped": 0,
        }
        # digest 在时间线上真的变过（否则"历史"是假的）
        digests = {_.payload["data"]["digest"] for _ in history}
        assert len(digests) >= 3
        assert any(_.payload["data"].get("resumed") for _ in history)
        events = await session_store.verify_invariants("s1")
        assert [_.seq for _ in events] == list(range(len(events)))
        await session_store.aclose()

    async def test_failure_then_replan_revives_graph(self, tmp_path: Path) -> None:
        """失败 → blocked → 重规划救活上游 → 下游自愈 → 跑完。"""
        store = PlanStore(tmp_path / "plans")

        broken = build_graph("两步", subtasks=["一", "二"])
        broken.mark("t1", TaskStatus.FAILED, error="环境缺依赖")
        await store.save("p2", broken)
        resumed = await store.resume("p2")
        assert resumed.node("t2").status is TaskStatus.BLOCKED

        # 重规划：把 t1 打回 pending 并重跑
        resumed.node("t1").status = TaskStatus.PENDING
        resumed.node("t1").error = None
        resumed.refresh_blocked()
        assert resumed.node("t2").status is TaskStatus.PENDING

        planner = HarnessPlanner(
            agent=StubAgent("exec", texts=["补做了", "产物二"]),
            max_steps=10,
        )
        result = await planner.execute(resumed, summarize=False)
        assert result.ok is True
        assert resumed.node("t1").attempts == 1, "打回 pending 不重置 attempts"

    async def test_plan_survives_fresh_process(self, tmp_path: Path) -> None:
        """模拟"换个进程读盘"：新建一个 ``PlanStore`` 实例仍能续跑。"""
        first = PlanStore(tmp_path / "plans")
        graph = build_graph("目标", subtasks=["一", "二"])
        graph.mark("t1", TaskStatus.DONE, artifact="已完成")
        graph.mark("t2", TaskStatus.RUNNING)
        await first.save("plan-x", graph)

        second = PlanStore(tmp_path / "plans")
        resumed = await second.resume("plan-x")
        assert resumed.node("t1").artifacts == ["已完成"]
        assert resumed.node("t2").status is TaskStatus.PENDING
        assert await second.list_plans() == ["plan-x"]
