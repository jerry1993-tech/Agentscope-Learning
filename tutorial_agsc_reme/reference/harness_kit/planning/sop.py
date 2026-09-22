# -*- coding: utf-8 -*-
"""SOP 状态机封装（契约 §3.12，第 12 讲）。

**这个模块的价值全部在于「封装」二字**。AgentScope 的 SOP 引擎已经写好了
最难的部分：

- ``SOPStep``：一步 = 一次尝试，executor 干活 + verifier 判定
  （``third_party/agentscope/src/agentscope/sop/_schema.py:193``）；
- ``SOPStepBase.record``：把判定写进运行态，**不通过的尝试会清空 submission**，
  于是下一次尝试从「干活」重新开始而不是从「被判定」开始
  （``.../sop/_schema.py:127``）；
- ``SOPEngine``：按序走步骤、把答案路由回停摆的那一步、花掉 ``max_attempts``
  预算（``.../sop/_engine.py:24``）；
- ``SOPRunState`` / ``SOPStepRunState`` / ``SOPPhase`` / ``VerificationResult``：
  **可序列化的运行态**，跨进程存活（``.../sop/_state.py:104``）。

AgentScope 缺的只有一件事：**SOP 的定义只能写在 Python 里**（``SOP(name, steps)``
要求 ``steps`` 是 ``SOPStepBase`` 实例列表，``.../sop/_schema.py:415``）。于是
「换一条 SOP 就要改代码 + 重新部署」。本模块补的就是这个：把 SOP 定义变成
**一份 YAML 文件**（``sop_path``），运行时装配成 ``SOPStep`` 列表。

**为什么 ``HarnessSOP`` 不继承 ``SOPEngine``**：``SOPEngine`` 内部持有
``self.state``（可变），且 ``reply_stream`` 会在多个 ``await`` 之间持续写它。
继承只会让「一次 run」和「一个引擎实例」绑死，导致同一个 ``HarnessSOP``
不能跑第二次。这里改成组合：每次 :meth:`HarnessSOP.run` 内部**新建一个
``SOPEngine``**；要接着跑（例如 HITL 回来）就用 :meth:`HarnessSOP.resume`
把上一轮的 ``SOPRunState`` 喂进新引擎 —— 这正是 ``SOPEngine(sop, state)``
第二个参数存在的理由。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg
from agentscope.sop import SOP, SOPEngine, SOPStep
from agentscope.sop import SOPPhase, SOPRunState, SOPStepRunState

from harness_kit.config.loader import load_yaml

__all__ = [
    "HarnessSOP",
    "SOPDefinition",
    "SOPError",
    "SOPResult",
    "SOPStepResult",
    "SOPStepSpec",
]


class SOPError(ValueError):
    """SOP 定义非法（缺字段、类型错、空 steps）。"""


class SOPStepSpec(BaseModel):
    """YAML 里的一步。"""

    model_config = ConfigDict(extra="forbid")

    subject: str
    """短标题，会出现在 ``SOP_STEP_STARTED`` 自定义事件里。"""

    description: str
    """这一步必须达成什么 —— **是目的地，不是路线**
    （``third_party/agentscope/src/agentscope/sop/_schema.py:89``）。"""

    max_attempts: int = Field(default=3, ge=1)
    """被拒几次后放弃这一步。由引擎强制，step 自身不判断。"""

    verify: bool = True
    """是否给这一步配验证者。``False`` → ``SOPStep(verifier=None)``，
    即「做完就算过」（``.../sop/_schema.py:291``）。"""


class SOPDefinition(BaseModel):
    """一份 SOP 定义（YAML 的 pydantic 视图）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """SOP 名。"""

    description: str = ""
    """这份流程是干什么的。"""

    steps: list[SOPStepSpec] = Field(min_length=1)
    """步骤，**按运行顺序**排列。"""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SOPDefinition":
        """从 dict 构造并转成 :class:`SOPError`。

        Args:
            raw (`dict[str, Any]`): YAML 解析结果。

        Returns:
            `SOPDefinition`: 校验过的定义。

        Raises:
            SOPError: pydantic 校验失败。
        """
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise SOPError(f"SOP 定义非法：{exc}") from exc

    @classmethod
    def from_file(cls, path: Path) -> "SOPDefinition":
        """从 ``.yaml`` / ``.yml`` / ``.json`` 读定义。

        YAML 走 :func:`harness_kit.config.loader.load_yaml`，因此支持
        ``${VAR:-default}`` 环境插值 —— 同一份 SOP 可以在不同环境指向不同的
        产物路径。

        Args:
            path (`Path`): 定义文件路径。

        Returns:
            `SOPDefinition`: 校验过的定义。

        Raises:
            FileNotFoundError: 文件不存在。
            SOPError: 文件内容非法（字段缺失/类型错/``steps`` 为空；JSON 的根
                节点不是对象也走这里）。
            ConfigParseError: YAML 语法错，或 **YAML 根节点不是映射** ——
                这一条由 :func:`harness_kit.config.loader.load_yaml` 先拦下
                （``harness_kit/config/loader.py:223``），所以它抛的不是
                ``SOPError``。``ConfigError`` 继承自 ``Exception`` 而不是
                ``ValueError``，捕获时别只写 ``except SOPError``。
        """
        if not path.exists():
            raise FileNotFoundError(f"SOP 定义文件不存在：{path}")
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            # JSON 走标准库，根节点类型得自己查；YAML 那条路 load_yaml 已经查过
            # 并抛 ConfigParseError 了（见 docstring 的 Raises）。
            if not isinstance(raw, dict):
                raise SOPError(
                    f"SOP 定义根节点必须是 map，收到 {type(raw).__name__}。",
                )
        else:
            raw = load_yaml(path)
        return cls.from_dict(raw)


class SOPStepResult(BaseModel):
    """一步跑完之后的快照（从 ``SOPStepRunState`` 投影而来）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str
    """步骤标题。"""

    phase: str
    """``SOPPhase`` 的字符串值：``completed`` / ``failed`` / ``awaiting`` …"""

    attempts: int = 0
    """尝试次数 = ``len(state.verifications)``。"""

    passed: bool = False
    """最后一次判定是否通过。"""

    message: str = ""
    """最后一次判定的理由（不通过时是要改什么）。"""

    submission: str = ""
    """这一步交出来的内容（``TextBlock`` 拼接）。"""


class SOPResult(BaseModel):
    """``HarnessSOP.run`` 的返回值。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """SOP 名。"""

    phase: str
    """整轮运行的 ``SOPRunState.phase``。"""

    ok: bool = False
    """是否 ``completed``。"""

    awaiting: bool = False
    """是否停摆等人（``awaiting``）。为真时应该用 :meth:`HarnessSOP.resume` 接上。"""

    steps: list[SOPStepResult] = Field(default_factory=list)
    """每一步的结果，按 SOP 声明顺序。"""

    output: str = ""
    """最后一步的产物文本 —— 也就是整条流程的交付物。"""

    run_state: dict[str, Any] = Field(default_factory=dict)
    """``SOPRunState.model_dump(mode="json")``，可直接落盘。"""

    elapsed_ms: float = 0.0
    """耗时（毫秒）。"""

    @property
    def failed_steps(self) -> list[str]:
        """失败的步骤标题。

        **返回 subject 字符串，不是 ``SOPStepResult`` 对象** —— 这个属性的
        用途是「日志里说清是哪几步挂了」与「断言/告警里比对步骤名」。需要完整
        快照（判定理由、尝试次数、提交内容）请用 :attr:`failed_results`。

        Returns:
            `list[str]`: ``phase == "failed"`` 的步骤 subject，按声明顺序。
        """
        return [_.subject for _ in self.failed_results]

    @property
    def failed_results(self) -> list[SOPStepResult]:
        """失败步骤的完整快照。

        Returns:
            `list[SOPStepResult]`: ``phase == "failed"`` 的步骤结果对象。
        """
        return [_ for _ in self.steps if _.phase == SOPPhase.FAILED.value]


class HarnessSOP:
    """把一份 YAML SOP 定义跑在 AgentScope 的 ``SOPEngine`` 上。

    Args:
        sop_path (`Path`): YAML / JSON 定义文件。
        agent (`Agent`): 默认的 executor。**每个步骤共用一个 Agent 实例时它们
            共享上下文**；要给某一步独立的上下文，就用 :meth:`with_step_agents`
            覆盖（``.../sop/_schema.py:216`` 明确说明这是设计意图）。
        verifier (`Agent | None`, optional): 默认的 verifier。``None`` 且
            ``step.verify=True`` 时退化为 **self-verify**（同一个 Agent 既做
            又验），这条路径能跑但要清楚它比人验/异体验弱。
        name (`str | None`, optional): 覆盖定义里的 ``name``。

    Raises:
        SOPError: 定义非法（空 steps / 字段类型错）。
        FileNotFoundError: ``sop_path`` 不存在。
    """

    def __init__(
        self,
        *,
        sop_path: Path,
        agent: Agent,
        verifier: Agent | None = None,
        name: str | None = None,
    ) -> None:
        """装配 SOP：定义 → ``SOP`` 对象。"""
        self.sop_path = Path(sop_path)
        self.definition = SOPDefinition.from_file(self.sop_path)
        self.agent = agent
        self.verifier = verifier
        self.name = name or self.definition.name
        self._step_agents: dict[str, Agent] = {}
        self._engine: SOPEngine | None = None
        self.sop = self._build_sop()
        logger.debug(
            "HarnessSOP({}) 装配完成：{} 步",
            self.name,
            len(self.sop.steps),
        )

    # ==================================================================
    # 装配
    # ==================================================================
    def _build_sop(self) -> SOP:
        """把定义装配成 ``SOP``。

        Returns:
            `SOP`: AgentScope 的 SOP 对象。

        Raises:
            SOPError: 定义里 steps 为空。
        """
        steps: list[SOPStep] = []
        for spec in self.definition.steps:
            executor = self._step_agents.get(spec.subject, self.agent)
            verifier: Agent | None = None
            if spec.verify:
                verifier = self._step_agents.get(
                    f"{spec.subject}:verifier",
                    self.verifier or self.agent,
                )
            steps.append(
                SOPStep(
                    subject=spec.subject,
                    description=spec.description,
                    executor=executor,
                    verifier=verifier,
                    max_attempts=spec.max_attempts,
                ),
            )
        if not steps:
            raise SOPError(f"SOP {self.name!r} 一个步骤都没有。")
        return SOP(
            name=self.name,
            steps=steps,
            description=self.definition.description,
        )

    def with_step_agents(self, mapping: dict[str, Agent]) -> "HarnessSOP":
        """给指定步骤换 executor / verifier（独立上下文的唯一方式）。

        Args:
            mapping (`dict[str, Agent]`): ``{subject: agent}``。要给某一步换
                verifier，键写成 ``f"{subject}:verifier"``。

        Returns:
            `HarnessSOP`: ``self``，便于链式调用。
        """
        self._step_agents.update(mapping)
        self.sop = self._build_sop()
        return self

    # ==================================================================
    # 运行
    # ==================================================================
    async def run(
        self,
        *,
        initial_input: str,
        state: SOPRunState | None = None,
    ) -> SOPResult:
        """跑一轮 SOP。

        Args:
            initial_input (`str`): 起始输入，会成为第一步的 ``inputs``。
            state (`SOPRunState | None`, optional): 上一轮存下的运行态。
                给了它就是**接着跑**（``SOPEngine(sop, state)``，
                ``.../sop/_engine.py:32``）。

        Returns:
            `SOPResult`: 结果，``run_state`` 可直接落盘。

        Raises:
            SOPError: 状态里的步骤数与 SOP 定义不符（说明流程被改过），
                此时 ``SOPEngine.__init__`` 会抛 ``ValueError``
                （``.../sop/_engine.py:51``），这里翻译成 ``SOPError``。
        """
        try:
            self._engine = SOPEngine(self.sop, state)
        except ValueError as exc:
            raise SOPError(str(exc)) from exc

        started = time.perf_counter()
        last_text = ""
        async for item in self._engine.reply_stream(
            inputs=UserMsg("user", initial_input),
        ):
            if isinstance(item, Msg):
                text = item.get_text_content() or ""
                if text.strip():
                    last_text = text

        state_obj = self._engine.state
        phase = state_obj.phase
        steps = self._project_steps(state_obj)
        output = ""
        for step in reversed(steps):
            if step.submission:
                output = step.submission
                break
        result = SOPResult(
            name=self.name,
            phase=phase.value,
            ok=phase is SOPPhase.COMPLETED,
            awaiting=phase is SOPPhase.AWAITING,
            steps=steps,
            output=output or last_text,
            run_state=state_obj.model_dump(mode="json"),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        logger.info(
            "SOP {} 结束: phase={} 步数={} 用时 {:.0f}ms",
            self.name,
            result.phase,
            len(steps),
            result.elapsed_ms,
        )
        return result

    async def resume(
        self,
        *,
        answer: Msg,
        state: dict[str, Any] | SOPRunState,
    ) -> SOPResult:
        """HITL 回来后接着跑。

        这里的 ``answer`` 是一个普通的 ``Msg``，**不是** AgentScope 的
        ``UserConfirmResultEvent`` —— 后者要求 ``reply_id`` 与停摆方的 reply
        对得上（``.../sop/_engine.py:100``），而 SOP 引擎在返回时并没有把那个
        reply_id 暴露出来。所以「停摆等人」在 harness_kit 这一层的正确做法是
        **不 park 在 SOP 里**，而是把答案当作新输入重新进入该步骤：SOP 引擎
        会把 ``inputs`` 原样交给停摆的那一步（``.../sop/_schema.py:392``）。

        Args:
            answer (`Msg`): 外部给出的答案 / 反馈。
            state (`dict[str, Any] | SOPRunState`): 上一轮的 ``run_state``。

        Returns:
            `SOPResult`: 接续后的结果。
        """
        if isinstance(state, dict):
            state = SOPRunState.model_validate(state)
        # 不通过 `run(initial_input=...)`，因为要给引擎传一个真实的 Msg。
        try:
            self._engine = SOPEngine(self.sop, state)
        except ValueError as exc:
            raise SOPError(str(exc)) from exc
        started = time.perf_counter()
        async for _ in self._engine.reply_stream(inputs=answer):
            pass
        state_obj = self._engine.state
        steps = self._project_steps(state_obj)
        output = ""
        for step in reversed(steps):
            if step.submission:
                output = step.submission
                break
        return SOPResult(
            name=self.name,
            phase=state_obj.phase.value,
            ok=state_obj.phase is SOPPhase.COMPLETED,
            awaiting=state_obj.phase is SOPPhase.AWAITING,
            steps=steps,
            output=output,
            run_state=state_obj.model_dump(mode="json"),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    @property
    def current_state(self) -> str:
        """当前阶段（契约 §3.12 要求的 ``current_state``）。

        Returns:
            `str`: ``SOPPhase`` 的字符串值；还没跑过时是 ``"pending"``。
        """
        if self._engine is None:
            return SOPPhase.PENDING.value
        return self._engine.phase.value

    # ==================================================================
    # 持久化
    # ==================================================================
    def save_state(self, path: Path, result: SOPResult) -> Path:
        """把 ``result.run_state`` 落盘成 JSON。

        跨进程续跑只需要这一个文件：``SOPRunState`` 就是「这轮跑到哪了」的
        全部（``.../sop/_state.py:104`` 的 docstring：*It covers the SOP's own
        state and nothing below it*）。

        Args:
            path (`Path`): 目标文件。
            result (`SOPResult`): :meth:`run` / :meth:`resume` 的返回值。

        Returns:
            `Path`: 写入的文件路径。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.run_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_state(self, path: Path) -> SOPRunState:
        """读回落盘的运行态。

        Args:
            path (`Path`): :meth:`save_state` 写出的文件。

        Returns:
            `SOPRunState`: 可直接喂给 :meth:`run` 的 ``state``。

        Raises:
            FileNotFoundError: 文件不存在。
            SOPError: 内容不是合法的 ``SOPRunState``。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"SOP 运行态文件不存在：{path}")
        try:
            return SOPRunState.model_validate_json(path.read_text("utf-8"))
        except ValidationError as exc:
            raise SOPError(f"SOP 运行态非法：{exc}") from exc

    # ==================================================================
    # 投影
    # ==================================================================
    def _project_steps(self, state: SOPRunState) -> list[SOPStepResult]:
        """把 ``SOPRunState.steps`` 投影成给人看的 ``SOPStepResult`` 列表。

        Args:
            state (`SOPRunState`): 运行态。

        Returns:
            `list[SOPStepResult]`: 每步一条。
        """
        out: list[SOPStepResult] = []
        for index, spec in enumerate(self.sop.steps):
            if index >= len(state.steps):
                out.append(
                    SOPStepResult(subject=spec.subject, phase=SOPPhase.PENDING.value),
                )
                continue
            record: SOPStepRunState = state.steps[index]
            verdict = record.verifications[-1] if record.verifications else None
            submission = "".join(
                getattr(block, "text", "") for block in (record.submission or [])
            )
            out.append(
                SOPStepResult(
                    subject=spec.subject,
                    phase=record.phase.value,
                    attempts=len(record.verifications),
                    passed=bool(verdict and verdict.passed),
                    message=verdict.message if verdict else "",
                    submission=submission,
                ),
            )
        return out
