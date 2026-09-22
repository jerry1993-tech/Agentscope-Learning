# -*- coding: utf-8 -*-
"""权限规则与规则集（契约 §3.11 / §5.5，第 11 讲）。

**AgentScope 缺的到底是什么**

AgentScope 的权限系统本身是完整的：:class:`~agentscope.permission.PermissionEngine`
（``third_party/agentscope/src/agentscope/permission/_engine.py:17``）按 5 种模式分派，
:class:`~agentscope.permission.PermissionRule`（``.../_rule.py:8``）描述单条规则，
:class:`~agentscope.permission.PermissionDecision`（``.../_decision.py:11``）是决策结果。
**规则只能通过 Python 代码 ``engine.add_rule(...)`` 加进去** —— 没有任何"从磁盘读规则文件"
的入口（``.../_engine.py:49`` 的 ``add_rule`` 是唯一入口）。于是"换一套权限策略"
就等于"改代码"，这在企业里不可接受。

本模块补的就是这一层：**YAML 规则文件 → ``RuleSet`` → 原生 ``PermissionRule`` 列表**。

**为什么不自定义规则类型**

:class:`~agentscope.permission.PermissionRule` 的匹配语义**由工具自己决定**
（``.../_engine.py:21`` 的类 docstring，实现见 ``_rule_matches``，``.../_engine.py:775``）：
``Bash`` 工具把 ``rule_content`` 当成命令的**子串**匹配，``Write``/``Read`` 当成路径的
**glob**。自定义一个字段更多的规则类，就得让每个工具认识它 —— 那是重写内核。
所以 :class:`RuleSet` 里装的**就是原生规则对象**，只是"怎么写出来"由我们负责。

**YAML 的紧凑形态**

原生一条 ``PermissionRule`` 只能表达**一个** ``rule_content``。而人写规则时想表达的
往往是"这一组模式都拒绝"：

.. code-block:: yaml

    order: 10
    default_behavior: ask
    rules:
      - tool: "Bash"
        behavior: deny
        deny_patterns: ["rm -rf /", "git push --force", "curl * | sh"]
      - tool: "Read"
        behavior: allow
        allow_paths: ["src/**", "docs/**"]
      - tool: "Write"
        behavior: ask
        rule_content: "**/*.pem"

:meth:`RuleSet.from_yaml` 会把上面第一条**展开成 3 条**原生规则
（``rule_content`` 分别是三个模式，``source`` 记下文件来源）。
展开而不是新增语义 —— 引擎侧完全无感。

**``default_behavior`` 怎么落地**

引擎在"没有任何规则命中"时会返回一个**模式兜底决策**，其特征是
``decision_reason == f"Mode: {mode.value}"``（``.../_engine.py:206`` 与 ``:387``，
两处逐字相同）。这是 AgentScope 里唯一一个**稳定可识别的"没规则命中"标记**，
:meth:`RuleSet.resolve_fallback` 就靠它把行为改写成 ``default_behavior``。
为什么不直接比对 ``behavior == ASK``：因为 ASK 也可能来自规则或工具的安全检查
（``.../_engine.py:186`` 的 bypass-immune 分支），那些**必须**保持 ASK。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from agentscope.permission import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "DEFAULT_ORDER",
    "MODE_FALLBACK_PREFIX",
    "RuleSet",
    "RuleSetError",
    "build_rulesets",
    "is_mode_fallback",
]


DEFAULT_ORDER: int = 100
"""``RuleSet.order`` 的默认值（契约 §3.11）。越小越先匹配。"""

MODE_FALLBACK_PREFIX: str = "Mode: "
"""引擎"无规则命中"兜底决策的 ``decision_reason`` 前缀。

逐字取自 ``third_party/agentscope/src/agentscope/permission/_engine.py:206``
与 ``:387``：``decision_reason=f"Mode: {self.context.mode.value}"``。
"""


class RuleSetError(ValueError):
    """规则文件本身写错了（字段缺失、行为自相矛盾、YAML 结构不对）。"""


def is_mode_fallback(decision: PermissionDecision, mode: PermissionMode) -> bool:
    """这条决策是不是引擎的"没有规则命中"兜底？

    Args:
        decision (`PermissionDecision`): 引擎返回的决策。
        mode (`PermissionMode`): 当前权限模式（拼出预期的 ``decision_reason``）。

    Returns:
        `bool`: ``True`` 表示这是兜底决策，``default_behavior`` 可以安全改写它。

    Example:
        >>> from agentscope.permission import PermissionDecision, PermissionBehavior
        >>> d = PermissionDecision(behavior=PermissionBehavior.ASK, message="x",
        ...                        decision_reason="Mode: default")
        >>> is_mode_fallback(d, PermissionMode.DEFAULT)
        True
    """
    return decision.decision_reason == f"{MODE_FALLBACK_PREFIX}{mode.value}"


class _YamlRule(BaseModel):
    """YAML 里**一条**规则条目（展开前的形态）。

    字段刻意保持少而正交：``tool`` + ``behavior`` 是骨架，
    ``rule_content`` / ``*_patterns`` / ``*_paths`` 是"匹配什么"的三条写法。

    Attributes:
        tool (`str`): 工具名（``Bash`` / ``Read`` / ``Write`` ...）。
        behavior (`Literal["allow", "deny", "ask"]`): 命中后的行为。
        rule_content (`str | None`): 单个匹配模式（工具自己解释其语义）。
        allow_paths / deny_paths / ask_paths (`list[str]`): 按行为前缀写的路径模式，
            前缀必须与 ``behavior`` 一致。
        allow_patterns / deny_patterns / ask_patterns (`list[str]`): 与上一行的
            ``*_paths`` 形状相同、前缀约束也相同，区别只在语义上偏「非路径」的
            命令模式（例如 ``git push:*``）。
        note (`str`): 人类可读的说明，只用于日志，不参与匹配。
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    behavior: Literal["allow", "deny", "ask"]
    rule_content: str | None = None

    allow_paths: list[str] = Field(default_factory=list)
    deny_paths: list[str] = Field(default_factory=list)
    ask_paths: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)
    ask_patterns: list[str] = Field(default_factory=list)

    note: str = ""

    @model_validator(mode="after")
    def _check_behavior_prefix(self) -> "_YamlRule":
        """校验 ``allow_*`` / ``deny_*`` / ``ask_*`` 的前缀与 ``behavior`` 一致。

        写了 ``behavior: deny`` 却配 ``allow_paths``，只可能是笔误；
        默默按其中一个执行，会让"我以为放行了、其实拒绝了"这种问题在
        生产上极其难查。这里直接拒绝加载。

        Returns:
            `_YamlRule`: 校验通过的自身。

        Raises:
            ValueError: 前缀与 ``behavior`` 矛盾。
        """
        for prefix, lists in (
            ("allow", (self.allow_paths, self.allow_patterns)),
            ("deny", (self.deny_paths, self.deny_patterns)),
            ("ask", (self.ask_paths, self.ask_patterns)),
        ):
            if prefix == self.behavior:
                continue
            if any(lst for lst in lists):
                raise ValueError(
                    f"规则里 behavior={self.behavior!r} 却配了 {prefix}_* 模式；"
                    "前缀必须与 behavior 一致（写错前缀会让策略与意图相反）",
                )
        return self

    def iter_contents(self) -> list[str]:
        """把本条目的所有匹配模式**按书写顺序**摊平。

        Returns:
            `list[str]`: 各模式的字符串；``rule_content`` 排在最前。
        """
        contents: list[str] = []
        if self.rule_content is not None:
            contents.append(self.rule_content)
        ordered = (
            self.allow_paths
            if self.behavior == "allow"
            else self.deny_paths
            if self.behavior == "deny"
            else self.ask_paths
        )
        ordered = ordered + (
            self.allow_patterns
            if self.behavior == "allow"
            else self.deny_patterns
            if self.behavior == "deny"
            else self.ask_patterns
        )
        contents.extend(ordered)
        return contents

    def to_rules(self, *, source: str) -> list[PermissionRule]:
        """展开成原生 :class:`~agentscope.permission.PermissionRule` 列表。

        Args:
            source (`str`): 来源标签（通常是规则文件的路径），写进
                ``PermissionRule.source``，便于 :meth:`RuleSet.merge` 后回溯。

        Returns:
            `list[PermissionRule]`: 一条条目 → 零到多条原生规则。
        """
        behavior = PermissionBehavior(self.behavior)
        contents = self.iter_contents()
        if not contents:
            # 没写任何匹配模式 == "这个工具的**所有**调用都按 behavior 处理"。
            # 引擎的 _rule_matches 第一句就是
            # `if not rule.rule_content: return True`（"Empty rule_content matches
            # everything"，third_party/agentscope/src/agentscope/permission/_engine.py:798）。
            return [
                PermissionRule(
                    tool_name=self.tool,
                    rule_content=None,
                    behavior=behavior,
                    source=source,
                ),
            ]
        return [
            PermissionRule(
                tool_name=self.tool,
                rule_content=content,
                behavior=behavior,
                source=source,
            )
            for content in contents
        ]


class RuleSet(BaseModel):
    """从 YAML 加载的权限规则集合（契约 §3.11）。

    语义：多个 RuleSet 按 ``order`` **从小到大**排列，前者的规则先被引擎匹配；
    引擎内部对每个行为桶（allow / deny / ask）的匹配是"首条命中即返回"
    （``third_party/agentscope/src/agentscope/permission/_engine.py:713`` 起）。
    **注意**：引擎的桶间优先级是恒定的 DENY > ASK > ALLOW
    （``_check_default`` 的 Step 1/2/5），``order`` 只在**同桶内**决定先后。

    Attributes:
        order (`int`): 优先级，越小越先匹配。
        default_behavior (`PermissionBehavior`): 无规则命中时改写为什么行为。
        rules (`list[PermissionRule]`): 原生规则对象（展开后）。
        source (`str`): 本规则集的来源标签（文件路径或 ``"<builtin>"``）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int = DEFAULT_ORDER
    default_behavior: PermissionBehavior = PermissionBehavior.ASK
    rules: list[PermissionRule] = Field(default_factory=list)
    source: str = "<inline>"

    @field_validator("default_behavior", mode="before")
    @classmethod
    def _coerce_behavior(cls, value: Any) -> Any:
        """允许 YAML 里直接写 ``"ask"`` / ``"deny"`` / ``"allow"`` 字符串。

        Args:
            value (`Any`): 原始值。

        Returns:
            `Any`: 归一化后的值（``PermissionBehavior`` 或原样交给 pydantic 报错）。
        """
        if isinstance(value, str):
            try:
                return PermissionBehavior(value)
            except ValueError:
                return value
        return value

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: Path) -> "RuleSet":
        """从一个 YAML 文件加载规则集（契约 §3.11）。

        文件形态见模块 docstring。解析复用第 2 讲的
        :func:`~harness_kit.config.loader.load_yaml`（同一套 ``${VAR}`` 插值、
        同一套错误类型），避免规则文件成为配置体系里的"另一个世界"。

        Args:
            path (`Path`): YAML 文件路径。

        Returns:
            `RuleSet`: 加载并展开后的规则集。

        Raises:
            FileNotFoundError: 文件不存在。
            RuleSetError: 顶层不是映射、``rules`` 不是列表、或条目字段写错。
        """
        from harness_kit.config.loader import load_yaml

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"权限规则文件不存在: {path}")

        raw = load_yaml(path)
        return cls.from_mapping(raw, source=str(path))

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, source: str) -> "RuleSet":
        """从一个已解析的映射构造规则集（``from_yaml`` 的实现主体）。

        Args:
            raw (`dict[str, Any]`): 顶层映射，可含 ``order`` /
                ``default_behavior`` / ``rules``。
            source (`str`): 来源标签。

        Returns:
            `RuleSet`: 展开后的规则集。

        Raises:
            RuleSetError: 结构不对或条目字段写错。
        """
        if not isinstance(raw, dict):
            raise RuleSetError(
                f"规则文件 {source} 的顶层必须是映射，实际是 {type(raw).__name__}",
            )

        entries = raw.get("rules", [])
        if not isinstance(entries, list):
            raise RuleSetError(
                f"规则文件 {source} 的 rules 必须是列表，实际是 {type(entries).__name__}",
            )

        expanded: list[PermissionRule] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise RuleSetError(
                    f"规则文件 {source} 的 rules[{index}] 必须是映射，"
                    f"实际是 {type(entry).__name__}",
                )
            try:
                parsed = _YamlRule.model_validate(entry)
            except ValueError as exc:
                raise RuleSetError(
                    f"规则文件 {source} 的 rules[{index}] 不合法: {exc}",
                ) from exc
            expanded.extend(parsed.to_rules(source=f"{source}#rules[{index}]"))

        try:
            ruleset = cls(
                order=int(raw.get("order", DEFAULT_ORDER)),
                default_behavior=raw.get("default_behavior", PermissionBehavior.ASK),
                rules=expanded,
                source=source,
            )
        except ValueError as exc:
            raise RuleSetError(f"规则文件 {source} 的头部字段不合法: {exc}") from exc

        logger.bind(source=source, order=ruleset.order, rules=len(ruleset.rules)).debug(
            "已加载权限规则集",
        )
        return ruleset

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------
    def merge(self, other: "RuleSet") -> "RuleSet":
        """合并另一个规则集（契约 §3.11："``other.order`` 更小则 ``other`` 优先"）。

        合并结果的 ``order`` / ``default_behavior`` 取**优先级更高**（``order`` 更小）
        的那一个；``rules`` 是"高优先级在前"的拼接，因此引擎在同桶内
        仍然先看到更重要的规则。

        Args:
            other (`RuleSet`): 待合并的规则集。

        Returns:
            `RuleSet`: 新的合并结果（两个输入都不被修改）。

        Example:
            >>> base = RuleSet(order=100, rules=[])
            >>> override = RuleSet(order=10, rules=[])
            >>> override.merge(base).order
            10
        """
        winner, loser = (other, self) if other.order < self.order else (self, other)
        merged = RuleSet(
            order=winner.order,
            default_behavior=winner.default_behavior,
            rules=[*winner.rules, *loser.rules],
            source=f"{winner.source}+{loser.source}",
        )
        logger.bind(
            winner=winner.source,
            loser=loser.source,
            rules=len(merged.rules),
        ).debug("权限规则集已合并")
        return merged

    # ------------------------------------------------------------------
    # 兜底行为
    # ------------------------------------------------------------------
    def resolve_fallback(
        self,
        decision: PermissionDecision,
        *,
        mode: PermissionMode,
    ) -> PermissionDecision:
        """把引擎的"无规则命中"兜底决策改写成 ``default_behavior``。

        引擎自己只会兜底成 ``ASK``
        （``third_party/agentscope/src/agentscope/permission/_engine.py:203``），
        而"只读助手"这类预设需要兜底成 ``DENY`` —— 这正是
        :attr:`default_behavior` 存在的意义。

        两条安全护栏（都不可绕过）：

        1. 只有 :func:`is_mode_fallback` 认出来的决策才会被改写 ——
           规则命中的 ASK、工具的安全 ASK 一律保持原样；
        2. ``bypass_immune=True`` 的决策永不降级为 ``ALLOW`` ——
           否则一个"禁止自动放行"的危险操作会被 ``default_behavior: allow``
           悄悄放过去，这是不可接受的。

        Args:
            decision (`PermissionDecision`): 引擎返回的决策。
            mode (`PermissionMode`): 当前权限模式。

        Returns:
            `PermissionDecision`: 改写后的决策（未命中兜底时**原样返回**）。
        """
        from dataclasses import replace

        if not is_mode_fallback(decision, mode):
            return decision
        if decision.behavior is self.default_behavior:
            return decision

        if self.default_behavior is PermissionBehavior.ALLOW and decision.bypass_immune:
            logger.bind(tool_input_immune=True).warning(
                "拒绝把 bypass-immune 决策改写为 ALLOW（default_behavior=allow 被安全护栏拦下）",
            )
            return decision

        resolved = replace(
            decision,
            behavior=self.default_behavior,
            decision_reason=(
                f"RuleSet.default_behavior={self.default_behavior.value}"
                f"（来源 {self.source}）；原始原因: {decision.decision_reason}"
            ),
        )
        logger.bind(
            source=self.source,
            behavior=self.default_behavior.value,
        ).debug("兜底行为已按 RuleSet 改写")
        return resolved


def build_rulesets(paths: "list[str | Path] | None") -> list[RuleSet]:
    """按顺序加载一批规则文件并合并成一个（``order`` 最小者优先）。

    这是 :class:`~harness_kit.permission.policy.HarnessPermissionEngine` 的
    ``rule_files`` 入口：Profile 里写多个文件时，靠 ``order`` 决定覆盖关系。

    Args:
        paths (`list[str | Path] | None`): 规则文件路径；``None`` 或空列表
            返回空列表（表示"没有文件级规则"，引擎只用模式策略）。

    Returns:
        `list[RuleSet]`: 合并后的规则集（**单元素**列表；无文件时为空列表）。

    Raises:
        FileNotFoundError: 某个文件不存在。
        RuleSetError: 某个文件写错了。
    """
    if not paths:
        return []

    merged = RuleSet.from_yaml(Path(paths[0]))
    for raw_path in paths[1:]:
        merged = merged.merge(RuleSet.from_yaml(Path(raw_path)))
    logger.bind(files=len(paths), order=merged.order, rules=len(merged.rules)).info(
        "权限规则文件已合并",
    )
    return [merged]
