# -*- coding: utf-8 -*-
"""第 11 讲的 pytest：规则装载 / 兜底行为 / 预设策略 / 危险命令 / HITL / 审计。

四条纪律：

1. **0 次 LLM 调用**。需要"真 Agent Loop"的那几条用
   :class:`~harness_kit.models.adapters.echo.EchoChatModel`（脚本驱动、
   确定性、离线）。真实模型那部分在 ``scripts/11_permission.py --live``。
2. **每个"允许"都要配一个"拒绝"的反例**。权限组件的失效模式是
   **静默放行**：规则写错、模式选错、兜底没生效，都不会报错，只会悄悄把
   危险操作放过去。所以下面每一条正向断言旁边都有一条反向断言。
3. **`bypass_immune` 是硬性质，必须单独测**。它是"用户配了 allow-all 也拦得住"
   的唯一机制；它一旦被破坏，前面所有规则都只是装饰。
4. **HITL 只测失败关闭**。同意路径只有一种写法（``prompter`` 返回 ``True``），
   而"没人回答"有很多种（超时、通道异常、非布尔、被取消），每一种都必须落到
   **拒绝** 而不是放行。

跑法（``conftest.py`` 已经把 ``third_party/ReMe`` 与 ``reference/`` 塞进 ``sys.path``）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest \\
      tests/test_lesson11_permission.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from agentscope.event import RequireUserConfirmEvent, UserConfirmResultEvent
from agentscope.message import ToolCallBlock, ToolResultBlock, UserMsg
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit
from agentscope.tool._builtin._bash import Bash
from agentscope.tool._builtin._bash_parser import BashCommandParser

from harness_kit.models.adapters.echo import EchoChatModel
from harness_kit.permission import (
    MODE_FALLBACK_PREFIX,
    PRESETS,
    AuditLog,
    ConfirmationTimeout,
    HITLBridge,
    HarnessPermissionEngine,
    HarnessPermissionMiddleware,
    RuleSet,
    RuleSetError,
    build_rulesets,
    digest_input,
    is_mode_fallback,
    preset_engine,
    resolve_mode,
)

#: 源码树里那份真实规则文件。
RULES_DIR: Path = Path(__file__).resolve().parents[1] / "harness_kit" / "permission" / "rules"

#: 假 token：用来证明审计默认**不落**明文。
FAKE_TOKEN: str = "sk-not-a-real-key-0123456789"


# ======================================================================
# 公共工具
# ======================================================================
class _DeferredTool:
    """最小工具替身：权限引擎只要求这 5 个方法。

    它**不是** :class:`~agentscope.tool.ToolBase` 子类 —— 引擎对工具的调用
    是鸭子类型的（``_engine.py:806`` 直接取 ``tool.match_rule``），因此
    用一个 20 行的假对象就能把"引擎怎么消费工具意见"这件事隔离出来测。
    真实工具（``Bash`` / ``Read`` / ``Write``）在别处用真身测。

    Args:
        name (`str`): 工具名。
        behavior (`PermissionBehavior`): ``check_permissions`` 的意见。
        bypass_immune (`bool`): 该意见是否免疫 bypass。
        read_only (`bool`): ``check_read_only`` 的返回值。
    """

    def __init__(
        self,
        name: str = "FakeTool",
        *,
        behavior: PermissionBehavior = PermissionBehavior.PASSTHROUGH,
        bypass_immune: bool = False,
        read_only: bool = False,
    ) -> None:
        """初始化。"""
        self.name = name
        self._behavior = behavior
        self._bypass_immune = bypass_immune
        self._read_only = read_only

    async def check_read_only(self, tool_input: dict[str, Any]) -> bool:
        """是否是只读调用。"""
        del tool_input
        return self._read_only

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """返回构造时指定的意见。"""
        del tool_input, context
        return PermissionDecision(
            behavior=self._behavior,
            message="fake tool opinion",
            bypass_immune=self._bypass_immune,
        )

    async def match_rule(self, rule_content: str | None, tool_input: dict[str, Any]) -> bool:
        """子串匹配（与 ``Bash`` 的语义一致）。"""
        if rule_content is None:
            return True
        return rule_content in str(tool_input)

    async def generate_suggestions(self, tool_input: dict[str, Any]) -> list[Any]:
        """不产生建议规则。"""
        del tool_input
        return []

    async def call(self, **kwargs: Any) -> str:
        """不会被调用。"""
        raise NotImplementedError


def make_engine(
    *,
    mode: PermissionMode = PermissionMode.DEFAULT,
    default_behavior: PermissionBehavior = PermissionBehavior.ASK,
    rules: list[PermissionRule] | None = None,
    audit: AuditLog | None = None,
    session_id: str = "test-session",
) -> HarnessPermissionEngine:
    """造一个装了单条规则集的 :class:`HarnessPermissionEngine`。

    Args:
        mode (`PermissionMode`): 权限模式。
        default_behavior (`PermissionBehavior`): 兜底行为。
        rules (`list[PermissionRule] | None`): 原生规则。
        audit (`AuditLog | None`): 审计日志。
        session_id (`str`): 会话 id。

    Returns:
        `HarnessPermissionEngine`: 引擎。
    """
    return HarnessPermissionEngine(
        rulesets=[
            RuleSet(
                order=10,
                default_behavior=default_behavior,
                source="<test>",
                rules=rules or [],
            ),
        ],
        mode=mode,
        audit=audit,
        session_id=session_id,
    )


def _read_tool() -> Any:
    """造一个真实的 AgentScope ``Read`` 工具。

    Returns:
        `Any`: ``Read`` 实例。
    """
    from agentscope.tool import Read

    return Read()


def _write_tool() -> Any:
    """造一个真实的 AgentScope ``Write`` 工具。

    Returns:
        `Any`: ``Write`` 实例。
    """
    from agentscope.tool import Write

    return Write()


async def write_note(text: str) -> str:
    """一个有副作用的工具（用来触发权限询问）。

    Args:
        text (`str`): 便签内容。

    Returns:
        `str`: 确认串。
    """
    return f"note saved: {text}"


def build_agent(
    *,
    session_id: str,
    engine: HarnessPermissionEngine | None,
    middlewares: list[Any] | None = None,
) -> Any:
    """造一个必然调用 ``write_note`` 的离线 Agent。

    Args:
        session_id (`str`): 会话 id。
        engine (`HarnessPermissionEngine | None`): 权限引擎；``None`` 用裸引擎。
        middlewares (`list[Any] | None`): 额外中间件。

    Returns:
        `Any`: :class:`~agentscope.agent.Agent`。
    """
    from agentscope.agent import Agent, ReActConfig

    model = EchoChatModel(
        stream=False,
        script=[
            {
                "text": "记下来。",
                "tool_calls": [
                    {"id": "call-note", "name": "write_note", "input": {"text": "hello"}},
                ],
            },
            {"text": "完成。"},
        ],
    )
    return Agent(
        name="perm-test",
        system_prompt="你是助手。",
        model=model,
        toolkit=Toolkit(tools=[FunctionTool(write_note)]),
        middlewares=middlewares or [],
        state=AgentState(
            session_id=session_id,
            permission_context=engine.context if engine else PermissionContext(),
        ),
        react_config=ReActConfig(max_iters=4),
    )


async def park_once(agent: Any, text: str) -> RequireUserConfirmEvent | None:
    """跑一轮 ``reply_stream``，返回（可能出现的）确认事件。

    Args:
        agent (`Any`): Agent。
        text (`str`): 用户消息。

    Returns:
        `RequireUserConfirmEvent | None`: park 的事件，没有 park 时为 ``None``。
    """
    parked: RequireUserConfirmEvent | None = None
    async for item in agent.reply_stream(UserMsg("user", text)):
        if isinstance(item, RequireUserConfirmEvent):
            parked = item
    return parked


async def resume(agent: Any, event: UserConfirmResultEvent) -> None:
    """把确认结果喂回去，跑到结束。

    Args:
        agent (`Any`): Agent。
        event (`UserConfirmResultEvent`): 确认结果。
    """
    async for _ in agent.reply_stream(event):
        pass


def tool_states(agent: Any) -> list[str]:
    """收集 context 里全部 ``ToolResultBlock.state``。

    Args:
        agent (`Any`): Agent。

    Returns:
        `list[str]`: 状态字符串列表。
    """
    out: list[str] = []
    for msg in agent.state.context:
        content = msg.content if not isinstance(msg.content, str) else []
        for block in content:
            if isinstance(block, ToolResultBlock):
                out.append(str(block.state))
    return out


# ======================================================================
# 一、规则装载：YAML → 原生规则
# ======================================================================
def test_ruleset_from_yaml_expands_each_pattern_into_one_native_rule() -> None:
    """一条 ``deny_patterns: [a, b]`` 条目要展平成 2 条原生规则。

    ``PermissionRule`` 一条只带一个 ``rule_content``，所以"一个 YAML 条目
    = 一条原生规则"是**做不到**的；如果展平写错（例如只取第一个 pattern），
    后面的 pattern 会被静默丢弃 —— 这正是权限组件最危险的失效模式。
    """
    ruleset = RuleSet.from_yaml(RULES_DIR / "coding.yaml")

    assert ruleset.order == 10
    assert ruleset.default_behavior is PermissionBehavior.ASK
    assert ruleset.source.endswith("coding.yaml")

    write_deny = [
        rule.rule_content
        for rule in ruleset.rules
        if rule.tool_name == "Write" and rule.behavior is PermissionBehavior.DENY
    ]
    # coding.yaml 里 Write 那一条写了 6 个 deny_paths
    assert len(write_deny) == 6, write_deny
    assert ".env" in write_deny
    # 反例：被拒绝的 pattern 不会消失，也不会串到别的工具名下
    assert "**/id_rsa" in write_deny
    # 同一条 deny_paths 也展开到了 Edit 上（两个工具共用一份机密文件清单）
    edit_deny = [
        rule.rule_content
        for rule in ruleset.rules
        if rule.tool_name == "Edit" and rule.behavior is PermissionBehavior.DENY
    ]
    assert edit_deny == write_deny
    assert all(
        rule.behavior is PermissionBehavior.ALLOW
        for rule in ruleset.rules
        if rule.tool_name in ("Read", "Glob", "Grep")
    )


def test_ruleset_source_label_is_traceable_to_the_yaml_entry() -> None:
    """``source`` 要能回到 YAML 里的第几条 —— 审计靠它讲清"是谁放的"。"""
    ruleset = RuleSet.from_yaml(RULES_DIR / "coding.yaml")
    first = ruleset.rules[0]

    assert first.source.startswith(str(RULES_DIR / "coding.yaml"))
    assert "#rules[" in first.source


def test_ruleset_rejects_behavior_prefix_mismatch() -> None:
    """``behavior: deny`` 配 ``allow_paths`` 必须**拒绝装载**。

    写错前缀是"策略与意图相反"的典型：作者想拒绝，写出来的却是一条
    allow 规则。如果这里不炸，线上就会静默放开一个本要拦住的路径。
    """
    with pytest.raises(RuleSetError) as excinfo:
        RuleSet.from_mapping(
            {
                "order": 10,
                "rules": [{"tool": "Write", "behavior": "deny", "allow_paths": ["src/**"]}],
            },
            source="<inline-bad>",
        )

    assert "前缀必须与 behavior 一致" in str(excinfo.value)


def test_ruleset_merge_takes_the_smaller_order_regardless_of_argument_order() -> None:
    """``merge`` 的裁决依据是 ``order``，不是传入顺序。"""
    low = RuleSet.from_yaml(RULES_DIR / "coding.yaml")  # order=10
    high = RuleSet.from_yaml(RULES_DIR / "research.yaml")  # order=20

    merged_a = high.merge(low).order
    merged_b = low.merge(high).order

    assert (merged_a, merged_b) == (10, 10)
    # 反例：不是"后者胜" —— 若实现写成 `self` 优先，merged_a 会是 20
    assert high.order == 20


def test_build_rulesets_merges_multiple_files_into_one() -> None:
    """``build_rulesets`` 把 N 个文件合成一个规则集，规则数等于展开后的总数。"""
    merged = build_rulesets([RULES_DIR / "research.yaml", RULES_DIR / "coding.yaml"])

    assert len(merged) == 1
    assert merged[0].order == 10
    assert len(merged[0].rules) == 41


def test_resolve_mode_accepts_both_string_and_enum_and_rejects_unknown() -> None:
    """模式名归一化：字符串与枚举都收，未知名必须报错（不静默回退）。"""
    assert resolve_mode("dont_ask") is PermissionMode.DONT_ASK
    assert resolve_mode(PermissionMode.EXPLORE) is PermissionMode.EXPLORE

    with pytest.raises(ValueError):
        resolve_mode("yolo")


def test_rule_matching_is_first_match_wins_within_a_bucket() -> None:
    """同桶内"首条命中即返回"：DENY 桶按写入顺序短路。

    ``"rm -rf"`` 与 ``"/"`` 都命中 ``rm -rf /``，因此返回哪一条取决于
    **写入顺序**；这也解释了为什么 ``RuleSet.order`` 只在同桶内有意义。
    """

    async def run(pattern_first: str, pattern_second: str) -> str:
        engine = PermissionEngine(PermissionContext(mode=PermissionMode.DEFAULT))
        for pattern in (pattern_first, pattern_second):
            engine.add_rule(
                PermissionRule(
                    tool_name="Bash",
                    rule_content=pattern,
                    behavior=PermissionBehavior.DENY,
                    source=f"<test:{pattern}>",
                ),
            )
        decision = await engine.check_permission(
            _DeferredTool("Bash"),
            {"command": "rm -rf /"},
        )
        return decision.decision_reason or ""

    assert asyncio.run(run("rm -rf", "/")) == "Rule: rm -rf"
    # 反例：换个写入顺序，命中的就是另一条 —— 证明"首条命中"而不是"最具体命中"
    assert asyncio.run(run("/", "rm -rf")) == "Rule: /"


# ======================================================================
# 二、default_behavior 的真实生效范围（本讲最硬的结论）
# ======================================================================
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (PermissionMode.DEFAULT, PermissionBehavior.ASK),
        (PermissionMode.ACCEPT_EDITS, PermissionBehavior.ASK),
        (PermissionMode.EXPLORE, PermissionBehavior.DENY),
        (PermissionMode.BYPASS, PermissionBehavior.ALLOW),
        (PermissionMode.DONT_ASK, PermissionBehavior.DENY),
    ],
)
def test_native_engine_fallback_behavior_per_mode(
    mode: PermissionMode,
    expected: PermissionBehavior,
) -> None:
    """5 个模式的"无规则命中"兜底行为各不相同 —— 这是改写的基线。

    ``_DeferredTool`` 一律 PASSTHROUGH，所以这里拿到的就是纯模式兜底。
    """

    async def run() -> PermissionDecision:
        engine = PermissionEngine(PermissionContext(mode=mode))
        return await engine.check_permission(_DeferredTool("FakeTool"), {"x": 1})

    assert asyncio.run(run()).behavior is expected


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS])
def test_default_behavior_rewrites_fallback_in_default_and_accept_edits(
    mode: PermissionMode,
) -> None:
    """``default_behavior=DENY`` 只在 ``DEFAULT`` / ``ACCEPT_EDITS`` 生效。"""

    async def run() -> PermissionDecision:
        return await make_engine(
            mode=mode,
            default_behavior=PermissionBehavior.DENY,
        ).check_permission(_DeferredTool("FakeTool"), {"x": 1})

    decision = asyncio.run(run())
    assert decision.behavior is PermissionBehavior.DENY
    assert "default_behavior=deny" in (decision.decision_reason or "")


@pytest.mark.parametrize(
    "mode",
    [PermissionMode.EXPLORE, PermissionMode.BYPASS, PermissionMode.DONT_ASK],
)
def test_default_behavior_is_inert_in_the_other_three_modes(mode: PermissionMode) -> None:
    """反例：``EXPLORE`` / ``BYPASS`` / ``DONT_ASK`` 的兜底**不**被改写。

    这三个模式的兜底各有自己的语义（"只读模式不许改" / "旁路放行" /
    "没人可问就拒"），它们不是"没有规则命中"这件事的另一种写法。
    如果哪天有人把 ``default_behavior`` 无差别应用，这条测试会红。
    """

    async def run() -> tuple[PermissionDecision, PermissionDecision]:
        native = await PermissionEngine(PermissionContext(mode=mode)).check_permission(
            _DeferredTool("FakeTool"),
            {"x": 1},
        )
        rewritten = await make_engine(
            mode=mode,
            default_behavior=PermissionBehavior.DENY,
        ).check_permission(_DeferredTool("FakeTool"), {"x": 1})
        return native, rewritten

    native, rewritten = asyncio.run(run())
    # 决策与原生引擎逐字段一致 —— 兜底改写在这三个模式下一动没动
    assert rewritten.behavior is native.behavior
    assert rewritten.decision_reason == native.decision_reason
    assert MODE_FALLBACK_PREFIX not in (rewritten.decision_reason or "")


def test_default_allow_never_upgrades_a_bypass_immune_ask() -> None:
    """``default_behavior=ALLOW`` 不能把安全 ASK 放过去（护栏 2）。

    这是 ``resolve_fallback`` 里最要紧的一条：危险命令的 ASK 是
    ``bypass_immune`` 的，任何"看起来像兜底"的改写都不许把它降级成 ALLOW。
    """
    tool = _DeferredTool(
        "Bash",
        behavior=PermissionBehavior.ASK,
        bypass_immune=True,
    )

    async def run() -> PermissionDecision:
        engine = PermissionEngine(PermissionContext(mode=PermissionMode.DEFAULT))
        raw = await engine.check_permission(tool, {"command": "rm -rf /"})
        resolved = RuleSet(
            order=10,
            default_behavior=PermissionBehavior.ALLOW,
            source="<test:allow-fallback>",
        ).resolve_fallback(raw, mode=PermissionMode.DEFAULT)
        return resolved

    decision = asyncio.run(run())
    # 工具的 ASK 不是模式兜底（reason 里没有 "Mode: "），因此原样保留
    assert is_mode_fallback(decision, PermissionMode.DEFAULT) is False
    assert decision.behavior is PermissionBehavior.ASK
    assert decision.behavior is not PermissionBehavior.ALLOW


def test_is_mode_fallback_recognizes_only_the_sentinel_prefix() -> None:
    """``is_mode_fallback`` 只认 ``"Mode: "`` 哨兵。"""
    fallback = PermissionDecision(
        behavior=PermissionBehavior.ASK,
        message="ask the user",
        decision_reason="Mode: default",
    )
    rule_ask = PermissionDecision(
        behavior=PermissionBehavior.ASK,
        message="ask the user",
        decision_reason="Rule: npm install:*",
    )

    assert is_mode_fallback(fallback, PermissionMode.DEFAULT) is True
    assert is_mode_fallback(rule_ask, PermissionMode.DEFAULT) is False
    # 模式对不上也不算兜底
    assert is_mode_fallback(fallback, PermissionMode.EXPLORE) is False
    assert is_mode_fallback(rule_ask, PermissionMode.DEFAULT) is False


# ======================================================================
# 三、三套预设策略
# ======================================================================
def test_preset_names_are_exactly_the_three_documented_ones() -> None:
    """预设名是契约的一部分，改名/加名都会破坏 Profile 的写法。"""
    assert sorted(PRESETS) == ["production", "read_only", "workspace_write"]


def test_preset_read_only_denies_every_write_and_never_asks(
    tmp_path: Path,
) -> None:
    """只读助手：写操作一律 DENY，且**不产生 ASK**（它的定位是"不让"）。"""
    engine = preset_engine("read_only")
    bash = Bash(cwd=str(tmp_path))
    write_tool = _write_tool()

    async def run() -> dict[str, PermissionDecision]:
        return {
            "write": await engine.check_permission(
                write_tool,
                {"file_path": str(tmp_path / "x.md"), "content": "hi"},
            ),
            "rm": await engine.check_permission(bash, {"command": "rm -rf build"}),
            "ls": await engine.check_permission(bash, {"command": "ls -la"}),
        }

    decisions = asyncio.run(run())
    assert engine.context.mode is PermissionMode.EXPLORE
    assert decisions["write"].behavior is PermissionBehavior.DENY
    assert decisions["rm"].behavior is PermissionBehavior.DENY
    # 正例：只读命令仍然可用，否则"只读助手"就什么都干不了了
    assert decisions["ls"].behavior is PermissionBehavior.ALLOW
    assert PermissionBehavior.ASK not in {d.behavior for d in decisions.values()}


def test_preset_workspace_write_allows_inside_and_asks_for_dangerous_output(
    tmp_path: Path,
) -> None:
    """工作区可写：区内放行，区外/危险目标询问。"""
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    engine = preset_engine("workspace_write", workspace_root=root)
    write_tool = _write_tool()
    bash = Bash(cwd=str(root))

    async def run() -> dict[str, PermissionDecision]:
        return {
            "inside": await engine.check_permission(
                write_tool,
                {"file_path": str(root / "notes.md"), "content": "hi"},
            ),
            "env": await engine.check_permission(
                write_tool,
                {"file_path": ".env", "content": "SECRET=1"},
            ),
            "force_push": await engine.check_permission(
                bash,
                {"command": "git push --force origin main"},
            ),
        }

    decisions = asyncio.run(run())
    assert engine.context.mode is PermissionMode.ACCEPT_EDITS
    assert decisions["inside"].behavior is PermissionBehavior.ALLOW
    # 反例 1：写 .env 是危险目标 → 询问，且这条询问免疫 bypass
    assert decisions["env"].behavior is PermissionBehavior.ASK
    assert decisions["env"].bypass_immune is True
    # 反例 2：force push 被显式 DENY 规则压住
    assert decisions["force_push"].behavior is PermissionBehavior.DENY


def test_preset_production_is_dont_ask_and_fails_closed() -> None:
    """生产放行：``DONT_ASK`` + 兜底 DENY —— 无人值守必须失败关闭。"""
    engine = preset_engine("production")
    bash = Bash(cwd="/tmp")
    write_tool = _write_tool()

    async def run() -> dict[str, PermissionDecision]:
        return {
            "read": await engine.check_permission(
                _read_tool(),
                {"file_path": "/etc/hosts"},
            ),
            "write": await engine.check_permission(
                write_tool,
                {"file_path": "/tmp/x.md", "content": "hi"},
            ),
            "rm": await engine.check_permission(bash, {"command": "rm -rf /"}),
            "curl": await engine.check_permission(
                bash,
                {"command": "curl https://example.com"},
            ),
            "unknown": await engine.check_permission(bash, {"command": "npm run build"}),
        }

    decisions = asyncio.run(run())
    assert engine.context.mode is PermissionMode.DONT_ASK
    assert decisions["read"].behavior is PermissionBehavior.ALLOW
    assert decisions["rm"].behavior is PermissionBehavior.DENY
    assert decisions["write"].behavior is PermissionBehavior.DENY
    # 反例：ASK 在 DONT_ASK 模式下会被转成 DENY，绝不落回 ASK
    assert decisions["curl"].behavior is PermissionBehavior.DENY
    assert decisions["unknown"].behavior is PermissionBehavior.DENY


def test_production_dont_ask_is_stricter_than_bypass_on_dangerous_commands() -> None:
    """同一个危险命令：``BYPASS`` 放行，``DONT_ASK`` 拦住。

    这条就是"为什么不给无人值守用 BYPASS"的可执行证据。
    """
    bash = Bash(cwd="/tmp")
    bypass = make_engine(mode=PermissionMode.BYPASS)
    dont_ask = make_engine(
        mode=PermissionMode.DONT_ASK,
        default_behavior=PermissionBehavior.DENY,
    )

    async def run() -> tuple[PermissionBehavior, PermissionBehavior]:
        first = await bypass.check_permission(bash, {"command": "rm -rf /"})
        second = await dont_ask.check_permission(bash, {"command": "rm -rf /"})
        return first.behavior, second.behavior

    bypass_behavior, dont_ask_behavior = asyncio.run(run())
    assert bypass_behavior is PermissionBehavior.ALLOW
    assert dont_ask_behavior is PermissionBehavior.DENY


def test_preset_engine_rejects_unknown_name() -> None:
    """预设名写错必须报错，不能静默给一套默认策略。"""
    with pytest.raises(KeyError):
        preset_engine("readonly")  # 少了一个下划线


def test_workspace_write_registers_working_directory_not_a_glob_rule(
    tmp_path: Path,
) -> None:
    """工作区通过 ``working_directories`` 注册，而不是靠 glob 规则。

    ``fnmatch`` 比的是 ``tool_input["file_path"]`` 的**原始字符串**，agent 传
    相对路径时 ``"<abs root>/**"`` 永远匹配不上；工作目录判定走
    ``os.path.realpath``，相对路径会先被解析成绝对路径再比。
    """
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    engine = preset_engine("workspace_write", workspace_root=root)

    registered = list(engine.context.working_directories)
    assert registered, "workspace_root 必须被注册进 working_directories"
    entry = engine.context.working_directories[registered[0]]
    assert entry.path == str(root)
    assert entry.source == "harness_kit:preset:workspace_write"
    # 反例：规则桶里**没有**凭空多出一条 <root>/** 的 glob 规则
    assert str(root) not in {
        rule.rule_content
        for bucket in (
            engine.context.allow_rules,
            engine.context.deny_rules,
            engine.context.ask_rules,
        )
        for rules in bucket.values()
        for rule in rules
    }


# ======================================================================
# 四、危险命令识别
# ======================================================================
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("rm -rf /", "rm -rf"),
        ("sudo rm -rf /var", "rm -rf"),
        ("chmod 777 secret.key", "chmod 777"),
        ("dd if=/dev/zero of=/dev/disk0", "dd"),
    ],
)
def test_dangerous_command_detection_hits_real_patterns(
    command: str,
    expected: str,
) -> None:
    """危险模式要真的命中，并且回报是**命中的那一条**。"""
    assert BashCommandParser().check_dangerous_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "git add .",  # 含 'dd' 子串
        "mkdir -p build",  # 含 'dd' 子串
        "echo 'hello world'",
        "ls -la",
    ],
)
def test_dangerous_command_detection_does_not_fire_on_substrings(command: str) -> None:
    """反例：短模式走词边界匹配，'dd' 不会命中 'add' / 'mkdir'。

    这是"安全组件过度拦截"的经典场景：误报多了，用户就会去关掉整套检查，
    那时候真正的危险命令也就没人拦了。
    """
    assert BashCommandParser().check_dangerous_command(command) is None


@pytest.mark.parametrize(
    ("command", "expected_node"),
    [
        ("rm $(find . -name '*.tmp')", "command_substitution"),
        ("for f in *.txt; do cat $f; done", "for_statement"),
        ("(cd /tmp && rm -rf x)", "subshell"),
    ],
)
def test_injection_risk_flags_unstatically_analyzable_structures(
    command: str,
    expected_node: str,
) -> None:
    """命令替换 / 循环 / 子 shell 无法静态判定，必须报风险。"""
    risk = BashCommandParser().check_injection_risk(command)
    assert risk is not None
    assert expected_node in risk


def test_injection_risk_is_none_for_plain_commands() -> None:
    """反例：普通命令不能报风险，否则每条命令都要人工确认。"""
    parser = BashCommandParser()
    assert parser.check_injection_risk("ls -la") is None
    assert parser.check_injection_risk("cat file.txt > out.txt") is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", True),
        ("ls -la | grep py", True),
        ("git status && rm -rf build", False),  # 复合命令里有一段可写
        ("echo hi > /tmp/x.txt", False),  # 重定向 = 写
        ("find . -name '*.py' -delete", False),
    ],
)
def test_read_only_classification_splits_compound_commands(
    command: str,
    expected: bool,
) -> None:
    """只读判定必须**逐段**看复合命令：整条链只读才算只读。"""
    assert BashCommandParser().is_read_only_command(command) is expected


def test_bash_safety_ask_survives_an_allow_all_rule() -> None:
    """``bypass_immune`` 的硬性质：配了 ``Bash`` allow-all 也拦得住危险命令。"""
    bash = Bash(cwd="/tmp")
    context = PermissionContext(mode=PermissionMode.DEFAULT)
    context.allow_rules["Bash"] = [
        PermissionRule(
            tool_name="Bash",
            rule_content=None,
            behavior=PermissionBehavior.ALLOW,
            source="<test:allow-all-bash>",
        ),
    ]
    engine = PermissionEngine(context)

    async def run() -> tuple[PermissionDecision, PermissionDecision]:
        tool_opinion = await bash.check_permissions({"command": "rm -rf /"}, context)
        final = await engine.check_permission(bash, {"command": "rm -rf /"})
        return tool_opinion, final

    tool_opinion, final = asyncio.run(run())
    assert tool_opinion.behavior is PermissionBehavior.ASK
    assert tool_opinion.bypass_immune is True
    # 反例：allow-all 规则消解不了它
    assert final.behavior is PermissionBehavior.ASK
    assert final.bypass_immune is True


def test_deny_rule_outranks_a_safety_ask() -> None:
    """一条 DENY 规则可以把"问"升级成"拒" —— DENY 桶先于 ASK 桶被检查。

    无人值守场景要的是这个：``ASK`` 只是"等一个人"，``DENY`` 才是确定性的。
    """

    async def run() -> PermissionDecision:
        engine = make_engine(
            rules=[],
            default_behavior=PermissionBehavior.ASK,
        )
        engine.add_ruleset(
            RuleSet.from_mapping(
                {
                    "order": 5,
                    "default_behavior": "ask",
                    "rules": [
                        {"tool": "Bash", "behavior": "deny", "deny_patterns": ["rm -rf"]},
                    ],
                },
                source="<test:strict>",
            ),
        )
        return await engine.check_permission(Bash(cwd="/tmp"), {"command": "rm -rf /"})

    decision = asyncio.run(run())
    assert decision.behavior is PermissionBehavior.DENY
    assert "rm -rf" in (decision.decision_reason or "")


def test_bash_generate_suggestions_returns_a_prefix_rule() -> None:
    """建议规则是 ``git push:*`` 这种前缀模式，而不是那条具体命令。"""

    async def run() -> list[Any]:
        return await Bash(cwd="/tmp").generate_suggestions({"command": "git push origin main"})

    suggestions = asyncio.run(run())
    assert [(rule.tool_name, rule.rule_content) for rule in suggestions] == [("Bash", "git push:*")]


# ======================================================================
# 五、中间件：真 Agent Loop 里的兜底改写与审计
# ======================================================================
def test_bare_engine_parks_the_agent_on_a_fallback_ask() -> None:
    """基线：裸引擎下这个工具调用会 park 在 ``RequireUserConfirmEvent``。"""

    async def run() -> tuple[bool, list[str]]:
        agent = build_agent(session_id="bare", engine=None)
        parked = await park_once(agent, "记一下 hello")
        return parked is not None, tool_states(agent)

    parked, states = asyncio.run(run())
    assert parked is True
    # 没人确认 → 工具没执行
    assert states == []


def test_middleware_turns_the_fallback_ask_into_a_deny(tmp_path: Path) -> None:
    """挂上中间件 + ``default_behavior=DENY`` → 不再 park，直接拒绝并审计。"""

    audit = AuditLog(tmp_path / "audit.jsonl")
    engine = make_engine(
        default_behavior=PermissionBehavior.DENY,
        audit=audit,
        session_id="mw",
    )

    async def run() -> tuple[bool, list[str], list[Any]]:
        agent = build_agent(
            session_id="mw",
            engine=engine,
            middlewares=[HarnessPermissionMiddleware(engine=engine)],
        )
        parked = await park_once(agent, "记一下 hello")
        return parked is not None, tool_states(agent), await audit.read(session_id="mw")

    parked, states, entries = asyncio.run(run())
    assert parked is False
    assert states == ["denied"]
    assert len(entries) == 1
    assert entries[0].tool_name == "write_note"
    assert entries[0].behavior is PermissionBehavior.DENY
    assert entries[0].session_id == "mw"


def test_middleware_does_not_break_the_happy_path_when_fallback_is_ask(
    tmp_path: Path,
) -> None:
    """反例：兜底是 ASK 时中间件必须**原样透传** ASK，不能顺手改成别的。

    中间件一旦"顺手"把 ASK 改掉，用户就再也看不到该看到的确认框。
    """
    audit = AuditLog(tmp_path / "audit.jsonl")
    engine = make_engine(
        default_behavior=PermissionBehavior.ASK,
        audit=audit,
        session_id="mw-ask",
    )

    async def run() -> bool:
        agent = build_agent(
            session_id="mw-ask",
            engine=engine,
            middlewares=[HarnessPermissionMiddleware(engine=engine)],
        )
        parked = await park_once(agent, "记一下 hello")
        return parked is not None

    assert asyncio.run(run()) is True


def test_middleware_reports_how_many_decisions_it_saw(tmp_path: Path) -> None:
    """``checks_seen`` 是可观测性计数：一次工具调用 = 一次决策。"""
    middleware = HarnessPermissionMiddleware(
        engine=make_engine(audit=AuditLog(tmp_path / "a.jsonl")),
    )
    engine = middleware.engine

    async def run() -> int:
        agent = build_agent(
            session_id="count",
            engine=engine,
            middlewares=[middleware],
        )
        await park_once(agent, "记一下 hello")
        return middleware.checks_seen

    assert asyncio.run(run()) == 1


# ======================================================================
# 六、HITLBridge：失败关闭
# ======================================================================
def test_hitl_bridge_allows_and_carries_suggested_rules(tmp_path: Path) -> None:
    """同意路径：``confirmed=True``，且把建议规则带回去（下一次不用再问）。"""

    async def run() -> tuple[list[bool], list[list[str]]]:
        engine = make_engine(
            audit=AuditLog(tmp_path / "a.jsonl"),
            session_id="hitl-allow",
        )
        agent = build_agent(
            session_id="hitl-allow",
            engine=engine,
            middlewares=[HarnessPermissionMiddleware(engine=engine)],
        )
        parked = await park_once(agent, "记一下 hello")
        assert parked is not None
        call = parked.tool_calls[0]
        call.suggested_rules = [
            PermissionRule(
                tool_name="write_note",
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source="<test:suggested>",
            ),
        ]
        bridge = HITLBridge(
            bus=None,
            timeout_s=5.0,
            prompter=HITLBridge.auto_allow("测试"),
            session_id="hitl-allow",
        )
        result = await bridge.request(parked)
        await resume(agent, result)
        return (
            [item.confirmed for item in result.confirm_results],
            [
                [rule.rule_content or "*" for rule in (item.rules or [])]
                for item in result.confirm_results
            ],
        )

    confirmed, rules = asyncio.run(run())
    assert confirmed == [True]
    assert rules == [["*"]]


def test_hitl_bridge_denied_result_carries_no_rules(tmp_path: Path) -> None:
    """拒绝时**不能**带上建议规则，否则"拒绝"反而扩大了权限。"""

    async def run() -> tuple[list[bool], list[Any], list[str]]:
        engine = make_engine(
            audit=AuditLog(tmp_path / "a.jsonl"),
            session_id="hitl-deny",
        )
        agent = build_agent(
            session_id="hitl-deny",
            engine=engine,
            middlewares=[HarnessPermissionMiddleware(engine=engine)],
        )
        parked = await park_once(agent, "记一下 hello")
        assert parked is not None
        parked.tool_calls[0].suggested_rules = [
            PermissionRule(
                tool_name="write_note",
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source="<test:suggested>",
            ),
        ]
        bridge = HITLBridge(
            bus=None,
            timeout_s=5.0,
            prompter=HITLBridge.auto_deny("测试"),
            session_id="hitl-deny",
        )
        result = await bridge.request(parked)
        await resume(agent, result)
        return (
            [item.confirmed for item in result.confirm_results],
            [item.rules for item in result.confirm_results],
            tool_states(agent),
        )

    confirmed, rules, states = asyncio.run(run())
    assert confirmed == [False]
    assert rules == [None]
    assert states == ["denied"]


def test_hitl_bridge_timeout_fails_closed(tmp_path: Path) -> None:
    """超时 = 拒绝。这是"用户离开工位"的语义，绝不能变成放行。"""

    async def never_answer(question: str) -> bool:
        del question
        await asyncio.sleep(3600)
        return True

    async def run() -> tuple[list[bool], int, list[str]]:
        engine = make_engine(
            audit=AuditLog(tmp_path / "a.jsonl"),
            session_id="hitl-timeout",
        )
        agent = build_agent(
            session_id="hitl-timeout",
            engine=engine,
            middlewares=[HarnessPermissionMiddleware(engine=engine)],
        )
        parked = await park_once(agent, "记一下 hello")
        assert parked is not None
        bridge = HITLBridge(
            bus=None,
            timeout_s=0.05,
            prompter=never_answer,
            session_id="hitl-timeout",
        )
        result = await bridge.request(parked)
        await resume(agent, result)
        return (
            [item.confirmed for item in result.confirm_results],
            bridge.timeouts,
            tool_states(agent),
        )

    confirmed, timeouts, states = asyncio.run(run())
    assert confirmed == [False]
    assert timeouts == 1
    assert states == ["denied"]


def test_hitl_bridge_channel_error_fails_closed(tmp_path: Path) -> None:
    """通道异常 = 拒绝（不是"默认同意"）。"""

    async def broken(question: str) -> bool:
        del question
        raise ConnectionError("WebSocket 断了")

    async def run() -> list[str]:
        engine = make_engine(
            audit=AuditLog(tmp_path / "a.jsonl"),
            session_id="hitl-error",
        )
        agent = build_agent(
            session_id="hitl-error",
            engine=engine,
            middlewares=[HarnessPermissionMiddleware(engine=engine)],
        )
        parked = await park_once(agent, "记一下 hello")
        assert parked is not None
        bridge = HITLBridge(bus=None, timeout_s=5.0, prompter=broken, session_id="hitl-error")
        await resume(agent, await bridge.request(parked))
        return tool_states(agent)

    assert asyncio.run(run()) == ["denied"]


def test_hitl_bridge_non_boolean_answer_fails_closed() -> None:
    """非布尔返回值按 ``bool(...)`` 解释；``None`` / ``""`` 都是拒绝。"""

    async def returns_none(question: str) -> Any:
        del question
        return None

    async def returns_empty(question: str) -> Any:
        del question
        return ""

    event = RequireUserConfirmEvent(
        reply_id="r-1",
        tool_calls=[ToolCallBlock(id="c-1", name="Bash", input='{"command": "ls"}')],
    )

    async def run() -> list[bool]:
        out: list[bool] = []
        for prompter in (returns_none, returns_empty):
            bridge = HITLBridge(bus=None, timeout_s=5.0, prompter=prompter)
            result = await bridge.request(event)
            out.append(result.confirm_results[0].confirmed)
        return out

    assert asyncio.run(run()) == [False, False]


def test_hitl_bridge_emits_one_result_per_tool_call() -> None:
    """N 条待确认调用 → N 条结果。少一条会让会话永久停在 ASKING。

    ``_check_incoming_event`` 只校验"回传的 id 属于等待集合"
    （``third_party/agentscope/src/agentscope/agent/_agent.py:1903``），
    **不要求**每条等待中的调用都有结果 —— 少发一条不会报错，只会卡住。
    """
    event = RequireUserConfirmEvent(
        reply_id="r-2",
        tool_calls=[
            ToolCallBlock(id=f"c-{index}", name="Bash", input=json.dumps({"command": f"ls {index}"}))
            for index in range(3)
        ],
    )

    async def run() -> UserConfirmResultEvent:
        bridge = HITLBridge(bus=None, timeout_s=5.0, prompter=HITLBridge.auto_allow("测试"))
        return await bridge.request(event)

    result = asyncio.run(run())
    assert len(result.confirm_results) == 3
    assert result.reply_id == "r-2"
    assert [item.tool_call.id for item in result.confirm_results] == ["c-0", "c-1", "c-2"]


def test_hitl_bridge_rejects_non_positive_timeout() -> None:
    """``timeout_s`` 非正数会让"超时=拒绝"永远不触发，必须当场报错。"""
    with pytest.raises(ValueError):
        HITLBridge(timeout_s=0.0)
    with pytest.raises(ValueError):
        HITLBridge(timeout_s=-1.0)


def test_confirmation_timeout_is_a_runtime_error() -> None:
    """``ConfirmationTimeout`` 必须是 ``RuntimeError``：它可能被上层统一兜住。"""
    assert issubclass(ConfirmationTimeout, RuntimeError)


def test_hitl_bridge_default_prompter_is_used_when_none_given() -> None:
    """不传 ``prompter`` 时退化为终端提问，而不是"没人问就放行"。"""
    bridge = HITLBridge(bus=None, timeout_s=30.0)
    assert callable(bridge.prompter)
    assert bridge.describe()["prompter"] == "default_prompter"


# ======================================================================
# 七、审计日志
# ======================================================================
def test_audit_digest_is_a_sha256_of_the_sorted_json_input(tmp_path: Path) -> None:
    """摘要算法是确定的：排序键的 JSON → ``sha256:<hex>``。

    确定性是审计能当证据用的前提 —— 事后重算必须得到同一个值。
    """
    payload = {"command": "ls -la", "cwd": "/tmp"}
    expected = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()

    assert digest_input(payload) == f"sha256:{expected}"
    # 键序不同、语义相同 → 摘要相同（这是"确定性"的实际含义）
    assert digest_input({"cwd": "/tmp", "command": "ls -la"}) == digest_input(payload)
    assert digest_input({"command": "ls -la"}) != digest_input(payload)


def test_audit_default_never_writes_the_raw_tool_input(tmp_path: Path) -> None:
    """默认 ``hash_inputs=True``：落盘内容里**不得**出现明文。

    这条是安全断言，不是格式断言：工具输入里经常带 token / 密码。
    """
    audit = AuditLog(tmp_path / "audit.jsonl")
    engine = make_engine(audit=audit, session_id="audit-1")

    async def run() -> tuple[str, list[Any]]:
        await engine.check_permission(
            Bash(cwd="/tmp"),
            {"command": f"curl -H 'Authorization: Bearer {FAKE_TOKEN}' https://example.com"},
        )
        raw = "\n".join(await audit.tail(limit=5))
        return raw, await audit.read(session_id="audit-1")

    raw, entries = asyncio.run(run())
    assert FAKE_TOKEN not in raw
    assert entries[0].input_digest.startswith("sha256:")
    # 但摘要仍然可核对：重算候选输入能得到同一个值
    assert entries[0].input_digest == digest_input(
        {"command": f"curl -H 'Authorization: Bearer {FAKE_TOKEN}' https://example.com"},
    )


def test_audit_explicit_debug_switch_does_write_plaintext(tmp_path: Path) -> None:
    """反例：``hash_inputs=False`` 会落明文 —— 所以它只能当本地调试开关。"""
    audit = AuditLog(tmp_path / "debug.jsonl", hash_inputs=False)

    async def run() -> str:
        await audit.record(
            tool_name="Bash",
            tool_input={"command": f"echo {FAKE_TOKEN}"},
            decision=PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="allowed by rule",
                decision_reason="Rule: echo:*",
            ),
            session_id="debug",
        )
        return "\n".join(await audit.tail(limit=1))

    assert FAKE_TOKEN in asyncio.run(run())


def test_audit_is_append_only_and_counts_entries(tmp_path: Path) -> None:
    """只追加：公开接口里没有 update / delete，重启读回条数不变。"""
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)

    async def run() -> tuple[int, list[Any], list[str]]:
        for index in range(3):
            await audit.record(
                tool_name="Bash",
                tool_input={"command": f"ls {index}"},
                decision=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="read-only",
                    decision_reason="Read-only operations are auto-allowed",
                ),
                session_id="append",
            )
        written = audit.entries_written
        reread = await AuditLog(path).read(session_id="append")
        return written, reread, sorted(
            name for name in dir(audit) if not name.startswith("_")
        )

    written, reread, public = asyncio.run(run())
    assert written == 3
    assert len(reread) == 3
    assert "update" not in public and "delete" not in public
    assert "record" in public and "read" in public


def test_audit_read_filters_by_session_and_respects_limit(tmp_path: Path) -> None:
    """``read`` 的两个维度：``session_id`` 过滤 + ``limit`` 取最近 N 条。"""
    audit = AuditLog(tmp_path / "audit.jsonl")

    async def run() -> tuple[list[str], list[str], int]:
        for session in ("s-1", "s-1", "s-2"):
            await audit.record(
                tool_name="Bash",
                tool_input={"command": "ls"},
                decision=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="allowed by rule",
                    decision_reason="Rule: ls:*",
                ),
                session_id=session,
            )
        only_s1 = await audit.read(session_id="s-1")
        all_sessions = await audit.read(limit=10)
        newest_two = await audit.read(limit=2)
        return (
            [entry.session_id for entry in only_s1],
            [entry.session_id for entry in all_sessions],
            len(newest_two),
        )

    only_s1, all_sessions, newest = asyncio.run(run())
    assert only_s1 == ["s-1", "s-1"]
    assert all_sessions == ["s-1", "s-1", "s-2"]
    assert newest == 2


def test_audit_records_the_mode_parsed_out_of_the_decision_reason(tmp_path: Path) -> None:
    """``mode`` 字段从 ``"Mode: <mode>"`` 哨兵里解析出来，供事后归因。"""
    audit = AuditLog(tmp_path / "audit.jsonl")
    engine = make_engine(
        default_behavior=PermissionBehavior.DENY,
        audit=audit,
        session_id="mode-1",
    )

    async def run() -> Any:
        await engine.check_permission(_DeferredTool("FakeTool"), {"x": 1})
        entries = await audit.read(session_id="mode-1")
        return entries[0]

    entry = asyncio.run(run())
    assert entry.mode == PermissionMode.DEFAULT.value
    assert entry.behavior is PermissionBehavior.DENY


# ======================================================================
# 八、Profile 驱动装配（离线，0 次 LLM）
# ======================================================================
def test_builder_wires_the_profile_rule_files_into_a_real_engine() -> None:
    """Profile 里的 ``rule_files`` 会被真正装载进引擎，而不是死配置。"""
    from harness_kit.config import HarnessBuilder, load_resolved_profile
    from harness_kit.settings import Settings

    reference_root = Path(__file__).resolve().parents[1]
    profiles = reference_root / "harness_kit" / "profiles"
    settings = Settings.from_env(
        repo_root=reference_root,
        profile_dir=profiles,
    )
    resolved = load_resolved_profile("coding", search_dir=profiles)

    async def run() -> tuple[Any, dict[str, PermissionBehavior]]:
        async with HarnessBuilder(resolved, settings=settings) as builder:
            engine = await builder.build_permission_engine()
            assert engine is not None
            bash = Bash(cwd=str(reference_root))
            return engine, {
                "status": (
                    await engine.check_permission(bash, {"command": "git status"})
                ).behavior,
                "push_force": (
                    await engine.check_permission(
                        bash,
                        {"command": "git push --force origin main"},
                    )
                ).behavior,
                "npm": (
                    await engine.check_permission(bash, {"command": "npm install express"})
                ).behavior,
            }

    engine, decisions = asyncio.run(run())
    assert isinstance(engine, HarnessPermissionEngine)
    assert decisions["status"] is PermissionBehavior.ALLOW
    assert decisions["push_force"] is PermissionBehavior.DENY
    # 反例：Profile 里没有写死的 npm 全放行，它落在 ask 规则上
    assert decisions["npm"] is PermissionBehavior.ASK


def test_from_profile_requires_a_settings_anchor(tmp_path: Path) -> None:
    """``from_profile`` 的路径锚点是 ``settings``，不是全局单例。

    传了 ``repo_root`` 就必须按它找规则文件；找不到要报
    ``FileNotFoundError``，绝不能"静默无规则"地继续跑。
    """
    from harness_kit.config.schema import PermissionSpec
    from harness_kit.settings import Settings

    spec = PermissionSpec(
        mode="default",
        rule_files=["./harness_kit/permission/rules/coding.yaml"],
        audit_path="./.harness/audit.jsonl",
    )

    # 正例：锚点指对时能装载
    ok = HarnessPermissionEngine.from_profile(
        spec,
        settings=Settings.from_env(repo_root=Path(__file__).resolve().parents[1]),
    )
    assert ok.rulesets and ok.rulesets[0].order == 10

    # 反例：锚点指到一个空目录时必须报错
    with pytest.raises(FileNotFoundError):
        HarnessPermissionEngine.from_profile(
            spec,
            settings=Settings(repo_root=tmp_path),
        )
