"""07_agentscope_middleware_hook 侦察：PermissionEngine 全模式实测（纯单元，不需要 LLM）。

验证点：
1. 5 个 PermissionMode 的判定顺序（deny > ask > read-only > tool.check_permissions
   > allow > fallback）。
2. PermissionDecision 的字段；bypass_immune 在不同模式下行为不同。
3. run 规则的匹配委托给 tool.match_rule。
4. 引擎本身不执行沙箱隔离 —— 它只产出 decision，执行由 Agent 消费。

对应的源码：
  permission/_engine.py:77  check_permission（模式分发）
  permission/_engine.py:117 _check_default
  permission/_engine.py:214 _check_explore
  permission/_engine.py:297 _check_accept_edits
  permission/_engine.py:395 _check_bypass
  permission/_engine.py:491 _check_dont_ask
"""
import asyncio

from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)
from agentscope.tool import ToolBase


class FakeTool(ToolBase):
    """最小可用的 ToolBase 子类，用于单测权限引擎。

    `is_read_only` 决定 check_read_only 的返回值；`check_perm` 决定
    check_permissions 返回什么（用 None 表示退回 PASSTHROUGH）。
    """

    def __init__(
        self,
        name: str = "FakeTool",
        is_read_only: bool = False,
        check_perm: PermissionDecision | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self._read_only = is_read_only
        self._check_perm = check_perm

    async def check_read_only(self, tool_input: dict) -> bool:
        return self._read_only

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        if self._check_perm is None:
            return PermissionDecision(
                behavior=PermissionBehavior.PASSTHROUGH,
                message="defer to engine",
            )
        return self._check_perm

    async def match_rule(self, rule_content: str, tool_input: dict) -> bool:
        # 简单子串匹配；真实工具用 glob / 前缀通配
        return rule_content in str(tool_input.get("command", ""))

    async def generate_suggestions(self, tool_input: dict) -> list:
        return [
            PermissionRule(
                tool_name=self.name,
                rule_content=str(tool_input.get("command", ""))[:10] + ":*",
                behavior=PermissionBehavior.ALLOW,
                source="suggestion",
            ),
        ]

    async def __call__(self, **kwargs):
        raise NotImplementedError


def rule(behavior: PermissionBehavior, content: str | None) -> PermissionRule:
    return PermissionRule(
        tool_name="FakeTool",
        rule_content=content,
        behavior=behavior,
        source="test",
    )


def show(title: str, d: PermissionDecision) -> None:
    print(
        f"  {title:52s} -> {d.behavior.value:12s} "
        f"| reason={d.decision_reason}",
    )
    if d.suggested_rules:
        print(
            f"      suggested_rules = "
            f"{[(r.rule_content, r.behavior.value) for r in d.suggested_rules]}",
        )


async def main() -> None:
    cmd = {"command": "npm install express"}

    print("=" * 78)
    print("1. DEFAULT 模式：6 级判定顺序")
    print("=" * 78)
    ctx = PermissionContext(mode=PermissionMode.DEFAULT)
    eng = PermissionEngine(ctx)

    show("无规则 + 非只读 + tool PASSTHROUGH",
         await eng.check_permission(FakeTool(), cmd))

    ctx.allow_rules["FakeTool"] = [rule(PermissionBehavior.ALLOW, "npm")]
    show("allow 规则命中 'npm'（文本含 npm）",
         await eng.check_permission(FakeTool(), cmd))

    ctx.deny_rules["FakeTool"] = [rule(PermissionBehavior.DENY, "rm -rf")]
    show("deny 规则 'rm -rf' 不命中",
         await eng.check_permission(FakeTool(), cmd))
    show("deny 规则 'rm -rf' 不命中 + allow 命中（同一请求）",
         await eng.check_permission(FakeTool(), cmd))
    print("  >>> deny 优先级最高：把 deny 规则改成能命中的 'npm' 再试")
    ctx.deny_rules["FakeTool"] = [rule(PermissionBehavior.DENY, "npm")]
    show("deny 'npm' 命中（压过 allow 'npm'）",
         await eng.check_permission(FakeTool(), cmd))

    print()
    print("=" * 78)
    print("2. read-only 快路径：所有模式都放行（_check_read_only_fast_path）")
    print("=" * 78)
    for mode in PermissionMode:
        c = PermissionContext(mode=mode)
        e = PermissionEngine(c)
        # 注意：EXPLORE 会先查 deny/ask 规则，这里都不配，所以落到 read-only
        show(f"mode={mode.value:13s} tool=只读",
             await e.check_permission(FakeTool(is_read_only=True), cmd))

    print()
    print("=" * 78)
    print("3. EXPLORE：非只读一律 DENY，且不查 allow 规则")
    print("=" * 78)
    c = PermissionContext(mode=PermissionMode.EXPLORE)
    c.allow_rules["FakeTool"] = [rule(PermissionBehavior.ALLOW, "npm")]
    e = PermissionEngine(c)
    show("EXPLORE + 有 allow 规则 + 非只读",
         await e.check_permission(FakeTool(), cmd))

    print()
    print("=" * 78)
    print("4. DONT_ASK：ASK 一律转 DENY（_convert_ask_to_deny）")
    print("=" * 78)
    c = PermissionContext(mode=PermissionMode.DONT_ASK)
    c.ask_rules["FakeTool"] = [rule(PermissionBehavior.ASK, "npm")]
    e = PermissionEngine(c)
    show("DONT_ASK + ask 规则命中",
         await e.check_permission(FakeTool(), cmd))
    c2 = PermissionContext(mode=PermissionMode.DONT_ASK)
    e2 = PermissionEngine(c2)
    show("DONT_ASK + 无规则 + 非只读（fallback）",
         await e2.check_permission(FakeTool(), cmd))

    print()
    print("=" * 78)
    print("5. bypass_immune：安全 ASK 能否被 allow 规则压过")
    print("=" * 78)
    safety = PermissionDecision(
        behavior=PermissionBehavior.ASK,
        message="rm -rf / is dangerous",
        decision_reason="safety check",
        bypass_immune=True,
    )
    tool_safety = FakeTool(check_perm=safety)

    for mode in (
        PermissionMode.DEFAULT,
        PermissionMode.ACCEPT_EDITS,
        PermissionMode.BYPASS,
        PermissionMode.DONT_ASK,
    ):
        c = PermissionContext(mode=mode)
        c.allow_rules["FakeTool"] = [rule(PermissionBehavior.ALLOW, "npm")]
        e = PermissionEngine(c)
        show(f"mode={mode.value:13s} 安全ASK + allow规则",
             await e.check_permission(tool_safety, cmd))
    print("  >>> DEFAULT/ACCEPT_EDITS: ASK 保留（allow 压不过）")
    print("  >>> BYPASS: PASS（bypass_immune 被故意忽略）")
    print("  >>> DONT_ASK: DENY（ASK 转 DENY）")

    print()
    print("=" * 78)
    print("6. add_rule() 的分发（permission/_engine.py:49）")
    print("=" * 78)
    c = PermissionContext()
    e = PermissionEngine(c)
    e.add_rule(rule(PermissionBehavior.ALLOW, "a"))
    e.add_rule(rule(PermissionBehavior.DENY, "b"))
    e.add_rule(rule(PermissionBehavior.ASK, "c"))
    print("  allow_rules =", list(c.allow_rules))
    print("  deny_rules  =", list(c.deny_rules))
    print("  ask_rules   =", list(c.ask_rules))

    print()
    print("=" * 78)
    print("7. 空 rule_content = 匹配一切（permission/_engine.py:799）")
    print("=" * 78)
    c = PermissionContext(mode=PermissionMode.DEFAULT)
    c.allow_rules["FakeTool"] = [
        PermissionRule(
            tool_name="FakeTool",
            rule_content=None,
            behavior=PermissionBehavior.ALLOW,
            source="test",
        ),
    ]
    e = PermissionEngine(c)
    show("rule_content=None", await e.check_permission(FakeTool(), cmd))


asyncio.run(main())
