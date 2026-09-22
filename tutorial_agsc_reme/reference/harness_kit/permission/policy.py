# -*- coding: utf-8 -*-
"""权限引擎与预设策略（契约 §3.11，第 11 讲）。

本模块在 AgentScope 原生 :class:`~agentscope.permission.PermissionEngine`
之上补三件事，每一件都对应原生确实没有的能力：

1. **规则文件**。:class:`HarnessPermissionEngine.from_profile` 把
   :class:`~harness_kit.config.schema.PermissionSpec` 里的 ``rule_files``
   装载成原生 :class:`~agentscope.permission.PermissionRule` 并注入 context。
2. **``default_behavior``**。原生引擎"无规则命中"时只能兜底成 ASK
   （``third_party/agentscope/src/agentscope/permission/_engine.py:203``）；
   只读助手需要兜底成 DENY。改写逻辑在
   :meth:`~harness_kit.permission.rules.RuleSet.resolve_fallback`。
3. **审计**。每次决策落一条 :class:`~harness_kit.permission.audit.AuditEntry`。

**一个必须说清楚的接线事实（决定本模块为什么有两个类）**

:class:`~agentscope.agent.Agent` 在 ``__init__`` 里**自己**造引擎：
``self._engine = PermissionEngine(self.state.permission_context)``
（``third_party/agentscope/src/agentscope/agent/_agent.py:193``），
用的就是 ``state.permission_context`` 里那把规则。因此：

- 想让 **Profile 的规则**生效 → 把配置好的 ``PermissionContext`` 放进
  ``AgentState`` 即可（:class:`~harness_kit.config.builder.HarnessBuilder` 已经这么做）；
- 想让 **``default_behavior`` 与审计**生效 → 光有 context 不够，
  因为原生引擎不知道这两个概念。

所以本模块给出**两条互补的接线路径**：

=================================== ==================================================
:class:`HarnessPermissionEngine`    AgentScope 引擎的**子类**。给"引擎外面"的调用方用：
                                    CLI 的 ``harness check``、评测里的预检、单测。
                                    直接 ``await engine.check_permission(tool, input)``
                                    就能拿到"带 default_behavior、已审计"的决策。
:class:`HarnessPermissionMiddleware` 走 AgentScope 的中间件扩展点
                                    （``on_check_permission``，
                                    ``.../middleware/_base.py:170``）。**Agent 循环里**
                                    的每一次工具调用都会经过它，因此它是让
                                    ``default_behavior`` 与审计在真实运行中生效的
                                    唯一位置 —— 而且它拿得到 ``agent``，
                                    于是拿得到 ``agent.state.session_id``，
                                    这正好补上 ``check_permission`` 签名里没有会话 id 的缺口。
=================================== ==================================================

两者共用同一份 :class:`~harness_kit.permission.rules.RuleSet` 与同一个
:class:`~harness_kit.permission.audit.AuditLog`，因此无论从哪条路径触发，
判定结果与审计口径都一致。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

from agentscope.middleware import MiddlewareBase
from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)
from loguru import logger

from harness_kit.permission.audit import AuditLog
from harness_kit.permission.rules import RuleSet, RuleSetError, build_rulesets

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.agent import Agent

    from harness_kit.config.schema import PermissionSpec
    from harness_kit.settings import Settings

__all__ = [
    "PRESETS",
    "HarnessPermissionEngine",
    "HarnessPermissionMiddleware",
    "build_permission_engine",
    "preset_engine",
    "resolve_mode",
]


# ----------------------------------------------------------------------
# 模式解析
# ----------------------------------------------------------------------
_MODE_BY_VALUE: dict[str, PermissionMode] = {
    mode.value: mode for mode in PermissionMode
}


def resolve_mode(value: "str | PermissionMode") -> PermissionMode:
    """把 Profile 里的字符串模式解析成 :class:`~agentscope.permission.PermissionMode`。

    Args:
        value (`str | PermissionMode`): ``"default"`` / ``"accept_edits"`` /
            ``"explore"`` / ``"bypass"`` / ``"dont_ask"``，或已经是枚举。

    Returns:
        `PermissionMode`: 解析结果。

    Raises:
        ValueError: 名字不在 5 个模式里。

    Example:
        >>> resolve_mode("dont_ask") is PermissionMode.DONT_ASK
        True
    """
    if isinstance(value, PermissionMode):
        return value
    try:
        return _MODE_BY_VALUE[value]
    except KeyError as exc:
        raise ValueError(
            f"未知的权限模式 {value!r}；可用值: {sorted(_MODE_BY_VALUE)}"
            "（third_party/agentscope/src/agentscope/permission/_types.py:18）",
        ) from exc


# ----------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------
class HarnessPermissionEngine(PermissionEngine):
    """带规则文件、``default_behavior`` 与审计的权限引擎（契约 §3.11）。

    **与父类的关系**：本类是 :class:`~agentscope.permission.PermissionEngine`
    的子类，因此 ``isinstance(engine, PermissionEngine)`` 为真，
    :class:`~harness_kit.config.builder.HarnessBuilder` 可以按契约把它当原生引擎用。
    但 :meth:`check_permission` 被**完全覆写**：真正的判定交给内部的原生引擎
    （``self._inner``），本类只做两件父类没有的事 —— 兜底行为改写 + 审计。

    **为什么不直接改父类的 ``_check_*``**：那 5 个方法是 AgentScope 的模式策略实现，
    改它们等于把模式语义复制一份到自己代码里；将来上游改一处，两边就漂移了。
    用"站在外面看结果"的方式（:meth:`~harness_kit.permission.rules.RuleSet.resolve_fallback`
    比对 ``decision_reason`` 哨兵）则天然跟随上游。

    Attributes:
        rulesets (`list[RuleSet]`): 装载的规则集（按 ``order`` 升序）。
        audit (`AuditLog | None`): 审计日志；``None`` 表示不审计。
        session_id (`str`): 审计用的会话 id；``""`` 表示尚未绑定。
    """

    def __init__(
        self,
        *,
        rulesets: list[RuleSet],
        mode: PermissionMode,
        audit: AuditLog | None = None,
        inner: PermissionEngine | None = None,
        session_id: str = "",
    ) -> None:
        """构造引擎（契约 §3.11 的签名 + 一个可选的 ``session_id`` 扩展）。

        Args:
            rulesets (`list[RuleSet]`): 规则集。``order`` 小的先被注入，
                因此同桶内更重要的规则排前面。
            mode (`PermissionMode`): 权限模式。**仅当 ``inner`` 为 ``None`` 时生效**
                （给了 ``inner`` 就以 ``inner.context.mode`` 为准，避免"两个模式"的歧义）。
            audit (`AuditLog | None`): 审计日志。
            inner (`PermissionEngine | None`): 复用的原生引擎；``None`` 时新建。
            session_id (`str`): 审计用的会话 id。

                .. note::
                   契约 §3.11 的 ``__init__`` 没有这个参数。加上它是因为
                   :meth:`~agentscope.permission.PermissionEngine.check_permission`
                   的签名里**没有会话 id**，而 ``AuditLog.record`` 需要它。
                   默认值让契约写法（不传）依然成立，需要审计会话归属的调用方
                   可以传参或事后调 :meth:`bind_session`。
        """
        ordered = sorted(rulesets, key=lambda item: item.order)

        if inner is None:
            context = PermissionContext(mode=mode)
            inner = PermissionEngine(context)
            for ruleset in ordered:
                for rule in ruleset.rules:
                    inner.add_rule(rule)
        else:
            # 复用调用方的引擎：把 rulesets 里的规则**追加**进去（add_rule 幂等性
            # 由调用方保证；重复注入只会让同一条规则在桶里出现两次，不影响语义）。
            if ordered:
                logger.bind(rules=sum(len(rs.rules) for rs in ordered)).debug(
                    "向既有 PermissionEngine 追加规则",
                )
            for ruleset in ordered:
                for rule in ruleset.rules:
                    inner.add_rule(rule)

        super().__init__(inner.context)

        self._inner: PermissionEngine = inner
        self.rulesets: list[RuleSet] = ordered
        self.audit: AuditLog | None = audit
        self.session_id: str = session_id
        self._audit_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 构造入口
    # ------------------------------------------------------------------
    @classmethod
    def from_profile(
        cls,
        spec: "PermissionSpec",
        *,
        audit: AuditLog | None = None,
        settings: "Settings | None" = None,
    ) -> "HarnessPermissionEngine":
        """从 :class:`~harness_kit.config.schema.PermissionSpec` 构造（契约 §3.11）。

        ``spec.rule_files`` 里的相对路径按 :class:`~harness_kit.settings.Settings`
        的 ``repo_root`` 解析（与 :meth:`HarnessBuilder._load_rules` 同一口径，
        ``harness_kit/config/builder.py:666``）；``spec.audit_path`` 同理。
        不给 ``audit`` 时**不会**自动建审计日志 —— 契约里 ``audit`` 是可选的，
        而"悄悄往磁盘写文件"不是装配层该做的事，需要审计的调用方显式传入
        :class:`~harness_kit.permission.audit.AuditLog`。

        **``settings`` 参数为什么必须存在**：这里的 ``settings`` 是路径锚点，
        不是全局单例。早期实现里写的是 ``settings = Settings()``，于是一旦调用方
        用 ``Settings.from_env(repo_root=<项目根>)`` 把 ``repo_root`` 换过
        （教程里所有入口都这么做，见 ``harness_kit/cli.py``），规则文件依然会去
        **默认仓库根** 下面找，报 ``FileNotFoundError: 权限规则文件不存在:
        <默认仓库根>/harness_kit/permission/rules/research.yaml``（已实测）。
        现在缺省值仍是 ``Settings()``（兼容既有调用），但装配链上会把
        ``BuildContext.settings`` 一路传进来。

        Args:
            spec (`PermissionSpec`): Profile 里的权限声明。
            audit (`AuditLog | None`): 审计日志。
            settings (`Settings | None`): 路径锚点；``None`` 时用 ``Settings()``。

        Returns:
            `HarnessPermissionEngine`: 装配好的引擎。

        Raises:
            ValueError: 模式名非法。
            FileNotFoundError: 规则文件不存在。
            RuleSetError: 规则文件写错了。
        """
        from harness_kit.settings import Settings

        resolved_settings = settings or Settings()
        paths = [resolved_settings.resolve(path) for path in spec.rule_files]
        rulesets = build_rulesets(paths)
        mode = resolve_mode(spec.mode)

        engine = cls(rulesets=rulesets, mode=mode, audit=audit)
        logger.bind(
            mode=mode.value,
            rule_files=spec.rule_files,
            rules=sum(len(rs.rules) for rs in rulesets),
            default_behavior=engine.default_behavior.value,
            audit=str(audit.path) if audit is not None else None,
        ).info("权限引擎已按 Profile 装配")
        return engine

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def default_behavior(self) -> PermissionBehavior:
        """当前生效的兜底行为。

        多个规则集时取 ``order`` 最小者的 :attr:`RuleSet.default_behavior`
        （与 :meth:`RuleSet.merge` 的裁决口径一致）。

        Returns:
            `PermissionBehavior`: 兜底行为；没有任何规则集时是原生默认的 ASK。
        """
        if not self.rulesets:
            return PermissionBehavior.ASK
        return self.rulesets[0].default_behavior

    @property
    def inner(self) -> PermissionEngine:
        """内部真正判定的原生引擎。

        Returns:
            `PermissionEngine`: 原生引擎。
        """
        return self._inner

    def add_ruleset(self, ruleset: RuleSet) -> None:
        """追加一个规则集（并保持 ``order`` 升序）。

        Args:
            ruleset (`RuleSet`): 待追加的规则集。
        """
        for rule in ruleset.rules:
            self._inner.add_rule(rule)
        self.rulesets.append(ruleset)
        self.rulesets.sort(key=lambda item: item.order)
        logger.bind(source=ruleset.source, rules=len(ruleset.rules)).debug(
            "规则集已追加到权限引擎",
        )

    def bind_session(self, session_id: str) -> "HarnessPermissionEngine":
        """绑定审计用的会话 id。

        Args:
            session_id (`str`): 会话 id。

        Returns:
            `HarnessPermissionEngine`: ``self``（便于链式调用）。
        """
        self.session_id = session_id
        return self

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    async def check_permission(
        self,
        tool: Any,
        tool_input: dict[str, Any],
    ) -> PermissionDecision:
        """判定一次工具调用（保持父类签名）。

        流程：① 原生引擎判定 → ② 按 ``default_behavior`` 改写兜底决策
        → ③ 落审计。

        Args:
            tool (`ToolBase`): 工具实例。
            tool_input (`dict[str, Any]`): 工具输入。

        Returns:
            `PermissionDecision`: 最终决策。
        """
        decision = await self._inner.check_permission(tool, tool_input)
        decision = self.apply_default_behavior(decision)

        if self.audit is not None:
            await self._audit(tool, tool_input, decision)
        return decision

    def apply_default_behavior(self, decision: PermissionDecision) -> PermissionDecision:
        """把"无规则命中"的兜底决策改写成 :attr:`default_behavior`。

        **只用优先级最高的那个规则集**（``order`` 最小）。理由：多个规则集时
        ``default_behavior`` 只能有一个，而 :attr:`default_behavior` 属性
        已经声明"取 ``order`` 最小者"；如果这里改成"从高到低挨个试"，
        就会出现"最高优先级说 ASK、次高说 DENY，结果兜底成了 DENY"这种
        与 :attr:`default_behavior` 自相矛盾的行为。

        **能改写什么、不能改写什么**（实测得出，必须讲清楚以免读者误判）：只有
        "走到模式兜底那一步"的决策会被改写。原生引擎在四类情况下**提前**返回，
        于是 ``default_behavior`` 在这四类上**不发生作用**：

        1. 有 DENY / ASK / ALLOW 规则命中 —— 规则优先，本来就该如此；
        2. 只读操作走了快路径（``decision_reason="Read-only operations are
           auto-allowed"``，``.../_engine.py:690``）——
           这也是为什么 ``default_behavior: deny`` 不会把 ``Read`` 拦掉，
           那是 :class:`~agentscope.permission.PermissionMode` 的既定语义；
        3. 工具的 ``check_permissions`` 直接给了 ALLOW / DENY；
        4. **模式本身没有"兜底成 ASK"这一步**。全文只有 ``_check_default``
           （``.../_engine.py:206``）与 ``_check_accept_edits``
           （``.../_engine.py:387``）两处会产出
           ``decision_reason="Mode: <mode>"`` 哨兵，因此
           :func:`~harness_kit.permission.rules.is_mode_fallback` 只在这两个
           模式下为真。``EXPLORE``（只读白名单或 DENY）、``BYPASS``（一律放行）、
           ``DONT_ASK``（ASK 转 DENY）各有自己的兜底语义，它们**不是**
           "没有规则命中"的另一种写法 —— 所以 ``default_behavior`` 在这三个
           模式下是**惰性**的（实测：``scripts/11_permission.py`` B 段矩阵）。

        Args:
            decision (`PermissionDecision`): 原生引擎的决策。

        Returns:
            `PermissionDecision`: 改写后的决策；不适用时**原对象**原样返回。
        """
        if not self.rulesets:
            return decision
        return self.rulesets[0].resolve_fallback(decision, mode=self.context.mode)

    async def _audit(
        self,
        tool: Any,
        tool_input: dict[str, Any],
        decision: PermissionDecision,
    ) -> None:
        """写一条审计记录。

        Args:
            tool (`ToolBase`): 工具实例。
            tool_input (`dict[str, Any]`): 工具输入。
            decision (`PermissionDecision`): 最终决策。
        """
        assert self.audit is not None  # noqa: S101 - 调用方已判空，这里收窄类型
        session_id = self.session_id
        if not session_id:
            session_id = "<unbound>"
            logger.bind(tool=getattr(tool, "name", "?")).warning(
                "权限审计未绑定会话 id，将以 '<unbound>' 记录；"
                "请在构造 Agent 后调用 HarnessPermissionEngine.bind_session(...)",
            )
        async with self._audit_lock:
            await self.audit.record(
                tool_name=getattr(tool, "name", "?"),
                tool_input=tool_input,
                decision=decision,
                session_id=session_id,
            )

    def describe(self) -> dict[str, Any]:
        """返回人类可读的自述（CLI ``harness inspect`` / 排障用）。

        Returns:
            `dict[str, Any]`: 模式、兜底行为、各桶规则数、审计路径、绑定会话。
        """
        context = self.context
        return {
            "mode": context.mode.value,
            "default_behavior": self.default_behavior.value,
            "rulesets": [
                {"source": rs.source, "order": rs.order, "rules": len(rs.rules)}
                for rs in self.rulesets
            ],
            "allow_rules": sum(len(v) for v in context.allow_rules.values()),
            "deny_rules": sum(len(v) for v in context.deny_rules.values()),
            "ask_rules": sum(len(v) for v in context.ask_rules.values()),
            "audit_path": str(self.audit.path) if self.audit is not None else None,
            "session_id": self.session_id,
        }


# ----------------------------------------------------------------------
# 中间件：让 default_behavior 与审计在真实 Agent 循环里生效
# ----------------------------------------------------------------------
class HarnessPermissionMiddleware(MiddlewareBase):
    """把 :class:`HarnessPermissionEngine` 的策略接到真实工具调用上的中间件。

    AgentScope 的中间件洋葱里有一个正好卡在"决策即将被消费"位置上的钩子：
    ``on_check_permission``（``third_party/agentscope/src/agentscope/middleware/_base.py:170``）。
    它收到 ``agent`` / ``tool`` / ``tool_input``，并返回最终要用的
    :class:`~agentscope.permission.PermissionDecision`。**本类不改变判定逻辑**，
    只做两件引擎之外的事：

    1. 用 ``default_behavior`` 改写原生引擎的兜底 ASK；
    2. 用 ``agent.state.session_id`` 作为会话 id 落审计。

    .. note::
       中间件拿到的 ``tool_input`` 是**副本**（``_base.py:187`` 的说明），
       因此这里改 ``decision.updated_input`` 不会影响实际调用 ——
       本类**不**做输入改写，那是
        :mod:`harness_kit.middleware.guards` 的职责。

    Example:
        >>> # 中间件是在**构造 Agent 时**传进去的，Toolkit 上没有注册中间件的方法
        >>> agent = Agent(                      # doctest: +SKIP
        ...     name="a", system_prompt="s", model=model,
        ...     toolkit=toolkit,
        ...     middlewares=[HarnessPermissionMiddleware(engine=engine)],
        ...     state=AgentState(session_id="s-1", permission_context=engine.context),
        ... )
        中间件已装配
    """

    def __init__(
        self,
        *,
        engine: HarnessPermissionEngine,
        skip_if_confirmed: bool = True,
    ) -> None:
        """构造中间件。

        Args:
            engine (`HarnessPermissionEngine`): 提供 ``default_behavior``
                与 :class:`~harness_kit.permission.audit.AuditLog`。
            skip_if_confirmed (`bool`): ``True``（默认）时，用户已经确认过的调用
                （``tool_call.state == ALLOWED``）直接透传、不重复审计。

                为什么需要它：AgentScope 对"已确认"的调用会短路成 ALLOW
                （``third_party/agentscope/src/agentscope/agent/_agent.py:2430``），
                如果中间件再改一次或再审计一次，同一次调用会出现两条互相矛盾的记录。
        """
        self.engine: HarnessPermissionEngine = engine
        self.skip_if_confirmed: bool = skip_if_confirmed
        self.checks_seen: int = 0
        """本进程内经过本中间件的决策数（诊断用）。"""

    async def on_check_permission(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[PermissionDecision]],
    ) -> PermissionDecision:
        """权限决策钩子（AgentScope 中间件协议）。

        Args:
            agent (`Agent`): 正在判定的 Agent（会话 id 从这里取）。
            input_kwargs (`dict`): 含 ``tool_call`` / ``tool`` / ``tool_input``。
            next_handler (`Callable[..., Awaitable[PermissionDecision]]`): 洋葱的下一层，
                最终落到原生引擎。

        Returns:
            `PermissionDecision`: 最终决策。
        """
        decision = await next_handler(**input_kwargs)

        tool_call = input_kwargs.get("tool_call")
        if self.skip_if_confirmed and getattr(tool_call, "state", None) == "allowed":
            logger.bind(session_id=self._session_id(agent)).trace(
                "该调用已由用户确认，跳过 default_behavior 与审计",
            )
            return decision

        self.checks_seen += 1
        resolved = self.engine.apply_default_behavior(decision)

        if self.engine.audit is not None:
            tool = input_kwargs.get("tool")
            tool_input = input_kwargs.get("tool_input") or {}
            await self.engine.audit.record(
                tool_name=getattr(tool, "name", "?"),
                tool_input=tool_input,
                decision=resolved,
                session_id=self._session_id(agent),
            )
        return resolved

    @staticmethod
    def _session_id(agent: "Agent") -> str:
        """从 Agent 上读会话 id（读不到时给占位串，绝不编造）。

        Args:
            agent (`Agent`): Agent 实例。

        Returns:
            `str`: 会话 id 或 ``"<unbound>"``。
        """
        state = getattr(agent, "state", None)
        session_id = getattr(state, "session_id", "")
        return session_id if isinstance(session_id, str) and session_id else "<unbound>"


# ----------------------------------------------------------------------
# 预设策略
# ----------------------------------------------------------------------
PRESETS: dict[str, str] = {
    "read_only": "只读助手：EXPLORE 模式 + 兜底拒绝；只能读、只能跑只读命令",
    "workspace_write": "工作区可写：ACCEPT_EDITS 模式 + 兜底询问；工作区内可写、越界要问",
    "production": "生产放行：DONT_ASK 模式 + 兜底拒绝；无人值守，只放行显式允许的调用",
}
"""三套预设策略的名字与一句话说明（第 10/11 讲 demo 用）。"""


def preset_engine(
    name: str,
    *,
    audit: AuditLog | None = None,
    workspace_root: Path | None = None,
    extra_rulesets: Sequence[RuleSet] | None = None,
) -> HarnessPermissionEngine:
    """构造一套预设策略引擎。

    三套预设的取舍（都要能说清"为什么不是别的模式"）：

    ``read_only`` —— **只读助手**
        模式用 ``EXPLORE``：原生引擎在这个模式下**跳过**工具的
        ``check_permissions``、直接按"只读/可写"分流
        （``third_party/agentscope/src/agentscope/permission/_engine.py:214``），
        对修改类工具一律 DENY。
        ``default_behavior`` 取 ``DENY`` 而不是 ASK：只读助手的定位就是"不该问，
        直接不让"。默认再叠一条 ``Write``/``Edit`` 的 DENY，防止上游模式被改后失控。

        .. warning::
           实测：**``default_behavior`` 在 ``EXPLORE`` 下永远不会生效** ——
           :meth:`~agentscope.permission.PermissionEngine._check_explore`
           没有"模式兜底"这一步，它要么按只读白名单 ALLOW、要么 DENY
           （全文只有 ``_check_default``（``.../_engine.py:206``）与
           ``_check_accept_edits``（``.../_engine.py:387``）两处会产生
           ``decision_reason="Mode: ..."`` 的兜底决策）。
           这里保留 ``DENY`` 是为了**模式被改动时仍然失败关闭**，不是因为它在 EXPLORE 下有用。

    ``workspace_write`` —— **工作区可写**
        模式用 ``ACCEPT_EDITS``：工作目录内的读写与文件系统命令自动放行
        （模式表见 ``.../permission/_types.py:18``），其余按规则。
        ``default_behavior`` 保持 ``ASK``：**出了工作区必须问人**，这是它的核心价值。

        ``workspace_root`` 通过 :attr:`PermissionContext.working_directories` 注册，
        **不是**通过加一条 glob 规则。原因（实测）：
        ``fnmatch`` 匹配的是 ``tool_input["file_path"]`` 的**原始字符串**
        （``.../tool/_builtin/_write.py:194``），agent 传相对路径时
        ``"<abs root>/**"`` 这种规则**永远匹配不上**；
        而工作目录判定走的是 ``os.path.realpath`` 对比
        （``.../tool/_base.py:390`` 的 :meth:`ToolBase._path_in_allowed_working_path`），
        相对路径会被 ``realpath`` 解析成绝对路径再比 —— 这才是能工作的机制，
        顺带还解决了 macOS 上 ``/tmp`` → ``/private/tmp`` 的符号链接问题。

    ``production`` —— **生产放行**
        模式用 ``DONT_ASK`` 而**不是** ``BYPASS``。原因在模式表里写得很明白：
        ``BYPASS`` 会**忽略工具的安全 ASK**（包括 ``rm -rf /`` 这类），
        只保护显式 deny 规则；而 ``DONT_ASK`` 把"无人可问"的 ASK **转成 DENY**
        （``.../permission/_engine.py:594`` 的
        :meth:`~agentscope.permission.PermissionEngine._convert_ask_to_deny`），
        是**失败关闭**的。无人值守场景要的正是后者。

    Args:
        name (`str`): ``"read_only"`` / ``"workspace_write"`` / ``"production"``。
        audit (`AuditLog | None`): 审计日志。
        workspace_root (`Path | None`): 工作区根目录（仅 ``workspace_write`` 用）。
        extra_rulesets (`Sequence[RuleSet] | None`): 追加的规则集，``order`` 更小的会
            排在预设规则前面（即优先）。

    Returns:
        `HarnessPermissionEngine`: 装配好的引擎。

    Raises:
        KeyError: 预设名不存在。

    Example:
        >>> engine = preset_engine("read_only")
        >>> engine.default_behavior.value
        'deny'
    """
    if name not in PRESETS:
        raise KeyError(
            f"未知的预设策略 {name!r}；可用: {sorted(PRESETS)}",
        )

    inner: PermissionEngine | None = None

    if name == "read_only":
        rulesets = [
            RuleSet(
                order=10,
                default_behavior=PermissionBehavior.DENY,
                source="<preset:read_only>",
                rules=[
                    PermissionRule(
                        tool_name="Write",
                        rule_content=None,
                        behavior=PermissionBehavior.DENY,
                        source="<preset:read_only>",
                    ),
                    PermissionRule(
                        tool_name="Edit",
                        rule_content=None,
                        behavior=PermissionBehavior.DENY,
                        source="<preset:read_only>",
                    ),
                    # Bash 在 EXPLORE 下已经按"只读命令"白名单过滤，这里再显式拒绝
                    # 几个改写型命令，作为"模式被误改"时的第二道闸。
                    PermissionRule(
                        tool_name="Bash",
                        rule_content="rm ",
                        behavior=PermissionBehavior.DENY,
                        source="<preset:read_only>",
                    ),
                ],
            ),
        ]
        mode = PermissionMode.EXPLORE

    elif name == "workspace_write":
        preset_rules = [
            PermissionRule(
                tool_name="Bash",
                rule_content="git push --force",
                behavior=PermissionBehavior.DENY,
                source="<preset:workspace_write>",
            ),
        ]
        rulesets = [
            RuleSet(
                order=10,
                default_behavior=PermissionBehavior.ASK,
                source="<preset:workspace_write>",
                rules=preset_rules,
            ),
        ]
        mode = PermissionMode.ACCEPT_EDITS
        if workspace_root is not None:
            # 注册工作目录：这是 ACCEPT_EDITS 真正读的字段（见函数 docstring）。
            root = Path(workspace_root).expanduser().resolve()
            context = PermissionContext(mode=mode)
            context.working_directories[str(root)] = AdditionalWorkingDirectory(
                path=str(root),
                source="harness_kit:preset:workspace_write",
            )
            inner = PermissionEngine(context)

    else:  # production
        rulesets = [
            RuleSet(
                order=10,
                default_behavior=PermissionBehavior.DENY,
                source="<preset:production>",
                rules=[
                    PermissionRule(
                        tool_name=tool_name,
                        rule_content=None,
                        behavior=PermissionBehavior.DENY,
                        source="<preset:production>",
                    )
                    for tool_name in ("Write", "Edit")
                ]
                + [
                    PermissionRule(
                        tool_name="Read",
                        rule_content=None,
                        behavior=PermissionBehavior.ALLOW,
                        source="<preset:production>",
                    ),
                    PermissionRule(
                        tool_name="Glob",
                        rule_content=None,
                        behavior=PermissionBehavior.ALLOW,
                        source="<preset:production>",
                    ),
                    PermissionRule(
                        tool_name="Grep",
                        rule_content=None,
                        behavior=PermissionBehavior.ALLOW,
                        source="<preset:production>",
                    ),
                    PermissionRule(
                        tool_name="Bash",
                        rule_content="rm -rf /",
                        behavior=PermissionBehavior.DENY,
                        source="<preset:production>",
                    ),
                    PermissionRule(
                        tool_name="Bash",
                        rule_content="curl ",
                        behavior=PermissionBehavior.ASK,
                        source="<preset:production>",
                    ),
                ],
            ),
        ]
        mode = PermissionMode.DONT_ASK

    if extra_rulesets:
        rulesets = [*extra_rulesets, *rulesets]

    engine = HarnessPermissionEngine(
        rulesets=rulesets,
        mode=mode,
        audit=audit,
        inner=inner,
    )
    logger.bind(
        preset=name,
        mode=engine.context.mode.value,
        default_behavior=engine.default_behavior.value,
        rules=sum(len(rs.rules) for rs in rulesets),
        working_dirs=sorted(engine.context.working_directories),
    ).info("预设权限策略已装配：{}", PRESETS[name])
    return engine


# ----------------------------------------------------------------------
# 注册表工厂
# ----------------------------------------------------------------------
async def build_permission_engine(
    spec: "PermissionSpec",
    ctx: Any | None = None,
) -> HarnessPermissionEngine:
    """注册表工厂：``permission`` 类目下的 ``yaml_ruleset``（第 11 讲）。

    函数名与签名由 :meth:`~harness_kit.registry.HarnessRegistry._register_permissions`
    钉死（``harness_kit/registry.py:1042``，``attrs=("build_permission_engine", ...)``），
    :meth:`~harness_kit.config.builder.HarnessBuilder.build_permission_engine`
    以 ``factory(spec, ctx=ctx)`` 调用它（``harness_kit/config/builder.py:702``）。

    与 :meth:`HarnessPermissionEngine.from_profile` 的区别只有一个：**这里会按
    ``spec.audit_path`` 真的建一个 :class:`~harness_kit.permission.audit.AuditLog`**。
    理由：``PermissionSpec.audit_path`` 是 Profile 里的正式字段，如果工厂不消费它，
    这个字段就是死配置 —— 用户写了审计路径却不产生审计文件，是比"没有审计"更糟的沉默失败。

    **会话 id 的归属**：``BuildContext`` 里没有会话 id
    （``harness_kit/registry.py:113`` 的字段是 ``settings`` / ``profile`` /
    ``workspace`` / ``workdir`` / ``environ``），而引擎装配发生在会话创建**之前**。
    因此这里返回的引擎处于"未绑定会话"状态，运行时请用
    :class:`HarnessPermissionMiddleware` 落审计（它从 ``agent.state.session_id`` 取），
    或在拿到会话后显式 :meth:`HarnessPermissionEngine.bind_session`。

    Args:
        spec (`PermissionSpec`): 权限声明。
        ctx (`BuildContext | None`): 装配上下文；给了就用它的 ``settings`` 解析路径。

    Returns:
        `HarnessPermissionEngine`: 装配好的引擎。

    Raises:
        ValueError: 模式名非法。
        FileNotFoundError: 规则文件不存在。
        RuleSetError: 规则文件写错了。
    """
    from harness_kit.settings import Settings

    settings = getattr(ctx, "settings", None) or Settings()
    path = settings.resolve(spec.audit_path)
    audit = AuditLog(path, hash_inputs=True)

    # settings 必须显式传下去：from_profile 的缺省值是裸 ``Settings()``，
    # 那样规则文件会按默认 repo_root 去找，调用方的 repo_root 覆盖会被吞掉。
    engine = HarnessPermissionEngine.from_profile(spec, audit=audit, settings=settings)
    logger.bind(
        mode=engine.context.mode.value,
        rules=sum(len(rs.rules) for rs in engine.rulesets),
        default_behavior=engine.default_behavior.value,
        audit=str(path),
    ).info("注册表工厂已装配权限引擎（含审计）")
    return engine
