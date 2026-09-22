# -*- coding: utf-8 -*-
"""任务移交协议与结果回收（契约 §3.13，第 13 讲）。

**为什么交接必须是一个「工具」而不是一次函数调用**：AgentScope 的 ``Agent``
只有一个扩展点能让**模型自己**决定跨 Agent 边界 —— 那就是工具
（``ToolBase``，``third_party/agentscope/src/agentscope/tool/_base.py:100``）。
把交接做成工具之后，「谁把活交给谁」由主管的 ReAct 循环自己决定，工具结果
自动回填进它的上下文（``third_party/agentscope/src/agentscope/agent/_agent.py:2729``
是 ``on_acting`` 的挂钩点，``:2804`` 是它内部真正调 ``toolkit.call_tool`` 的那行），
我们一行调度代码都不用写。做成普通函数调用则等于把「谁交给谁」写死在我们
代码里，模型没有决策权。

**三个必须踩对的坑**（都在教程正文里断言）：

1. **自定义 ``ToolBase`` 的默认权限是 ASK**。``FunctionTool`` 在
   ``permission=None`` 时返回 ASK
   （``third_party/agentscope/src/agentscope/tool/_adapters.py:132``）。交接是
   **纯控制流、无副作用**（不读文件、不跑命令），每次问一遍用户会把权限
   对话框变成噪声，让真正危险的操作被淹没。所以
   :meth:`HandoffTool.check_permissions` **无条件 ALLOW，并显式写出理由**
   —— 契约 §3.13 原文要求「必须显式声明自己的行为，不能依赖默认值」。
   注意 ALLOW 不是「绕过规则」：``PermissionEngine`` 里用户配的 DENY
   优先级更高，企业策略仍然能关掉它。
2. **``await tool(...)`` 之后拿到的可能是 async generator**。``ToolBase.__call__``
   的返回类型是 ``ToolChunk | AsyncGenerator[ToolChunk, None]``
   （``third_party/agentscope/src/agentscope/tool/_base.py:194``）：``call``
   是异步生成器函数时，``__call__`` 直接返回**生成器**（``:220`` 那段
   ``inspect.isasyncgenfunction(self.call)`` 的分支），``await`` 拿到它就
   必须再 ``async for``。本模块的 ``call`` 是协程函数、直接返回 ``ToolChunk``，
   所以 ``await`` 一次即可 —— 但调用方（Agent 的 ``call_tool``）已经统一
   处理了两种形状，所以这两种写法都对。
   教程里要专门演示这两种形状的差别。
3. **交接的深度必须能传播**。A→B→C→… 的链是「深度爆炸」；每次交接都要
   ``depth + 1`` 地传下去，闸门（``max_depth``）才拦得住。
   本工具在构造时记下自己的 ``depth``，调用时派发 ``depth + 1``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与 team.py 成环
    from harness_kit.multiagent.team import AgentTeam, TeamResult

__all__ = [
    "HANDOFF_TOOL_DESCRIPTION",
    "HandoffOutcome",
    "HandoffProtocol",
    "HandoffRequest",
    "HandoffTool",
    "collect_results",
]

HANDOFF_TOOL_DESCRIPTION = """\
Hand a task over to another member of the team and get their result back.

Use this when the task needs a capability you do not have (check the team \
roster in your context), or when a subtask can run independently while you \
keep working on something else.

The teammate sees ONLY the `task` text you write here — not your files, not \
your conversation, not what your other tools returned. Write the task as if \
for someone who has seen none of your work: what to do, what to return, and \
any constraint that matters.

Do not hand a task back to yourself, and do not hand a task to a teammate \
you have already handed this exact task to.\
"""


class HandoffRequest(BaseModel):
    """一次移交请求。"""

    model_config = ConfigDict(extra="forbid")

    from_member: str
    """发起方。"""

    to_member: str
    """接收方（已解析，非空）。"""

    task: str
    """交给对方的任务文本。**对方只能看到这一段。**"""

    reason: str = ""
    """为什么交给它（审计用；不参与路由）。"""


class HandoffOutcome(BaseModel):
    """一次移交的完整结果（请求 + 结果 + 回填文本）。

    「结果回收」在本模块的落点就是这个类：交接不是「发出去就完了」，
    返回给发起方的必须是 :attr:`feedback` 这段文本 —— 它会成为工具结果，
    直接进发起方的上下文。
    """

    model_config = ConfigDict(extra="forbid")

    request: HandoffRequest
    """移交请求。"""

    ok: bool = False
    """对方是否成功交付。"""

    output: str = ""
    """对方的产物文本。"""

    error: str | None = None
    """失败原因。"""

    elapsed_ms: float = 0.0
    """耗时（毫秒）。"""

    @property
    def feedback(self) -> str:
        """回填给发起方（也就是工具结果）的文本。

        Returns:
            `str`: 成功时是产物，失败时是带原因的错误说明。**永远非空** ——
            空的工具结果会让模型以为工具没执行，然后重复调用。
        """
        if self.ok:
            return (
                f"[handoff -> {self.request.to_member} OK, "
                f"{self.elapsed_ms:.0f}ms]\n{self.output}"
            )
        return (
            f"[handoff -> {self.request.to_member} FAILED, "
            f"{self.elapsed_ms:.0f}ms]\n{self.error or '未知原因'}\n"
            "Do not retry the same handoff blindly; either do the work "
            "yourself or hand it to a different teammate."
        )


class HandoffProtocol(BaseModel):
    """交接协议的「文档化」形态：字段即协议。

    它不参与运行，存在的意义是让教程能**指着一个类型**讲清楚「移交协议包含
    什么」：谁交给谁（``to_member``）、交什么（``task``）、为什么（``reason``）、
    深度（``depth``）、以及发起方期望的产出形状（``expect``）。真正的运行时
    载体是 :class:`HandoffRequest` 与 :class:`HandoffOutcome`。
    """

    model_config = ConfigDict(extra="forbid")

    to_member: str = Field(description="接收方成员名。")
    task: str = Field(description="任务文本，接收方唯一能看到的东西。")
    reason: str = Field(default="", description="为什么移交给它。")
    depth: int = Field(default=0, ge=0, description="本次移交的深度。")
    expect: str = Field(
        default="",
        description="期望的产出形状（例如「一段可编译的 Python」）。",
    )


class HandoffTool(ToolBase):
    """交接工具（契约 §3.13）。

    Args:
        team (`AgentTeam`): 所属团队。**鸭子类型**：只要求它有 ``dispatch``
            和 ``names``，不 import ``AgentTeam`` 的类型（否则与 ``team.py``
            互相 import 成环）。
        self_name (`str`): 持有本工具的成员名，用来挡住「自己交给自己」。
        tool_name (`str`, defaults to ``"handoff_to_teammate"``): 工具名。
        depth (`int`, defaults to `0`): 本工具持有者自身的深度。派发时用的是
            ``depth + 1``。
    """

    is_concurrency_safe = False
    """交接会推动团队的记账（``router.record``）并占用闸门票据，
    并发调用会让票据计数与顺序失去意义，所以标为不安全。"""

    is_read_only = False
    """它不是只读的：它会真的消耗一次 LLM 调用。"""

    is_state_injected = False
    """不需要注入 ``AgentState``：交接只看任务文本与团队名册。"""

    def __init__(
        self,
        *,
        team: "AgentTeam",
        self_name: str,
        tool_name: str = "handoff_to_teammate",
        depth: int = 0,
    ) -> None:
        """构造工具：名字、描述、schema 与权限决策都在这里定死。"""
        super().__init__()
        self.name = tool_name
        self.description = HANDOFF_TOOL_DESCRIPTION
        self.team = team
        self.self_name = self_name
        self.depth = int(depth)
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "to_member": {
                    "type": "string",
                    "description": (
                        "The name of the teammate to hand the task to. "
                        f"Candidates: {sorted(team.names)}. "
                        f"Never '{self_name}' (that is you)."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The task text. The teammate sees ONLY this, so it "
                        "must be self-contained."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Why this teammate (one short sentence).",
                },
            },
            "required": ["to_member", "task"],
            "additionalProperties": False,
        }
        self._outcomes: list[HandoffOutcome] = []

    # ==================================================================
    # 权限
    # ==================================================================
    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """无条件 ALLOW，并给出理由。

        契约 §3.13 的铁律：**不能依赖默认值**。``ToolBase.check_permissions``
        是 ``@abstractmethod``（``.../tool/_base.py:265``），必须自己实现；
        而 ``FunctionTool`` 那条路在 ``permission=None`` 时返回 ASK
        （``.../tool/_adapters.py:132``）。交接是控制流、无副作用，每次问用户
        只是噪声。用户在 ``PermissionEngine`` 里配的 DENY 依然优先，所以
        企业策略能关掉它。

        Args:
            tool_input (`dict[str, Any]`): 工具入参（这里不看）。
            context (`PermissionContext`): 权限上下文（这里不看）。

        Returns:
            `PermissionDecision`: ``ALLOW``。
        """
        del tool_input, context  # 交接的权限与参数无关，显式丢弃以免误用
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=(
                "交接是纯控制流：不读写文件、不执行命令，"
                "它的副作用只是「多花一次模型调用」。"
            ),
            decision_reason="harness_kit.multiagent.handoff.HandoffTool",
        )

    # ==================================================================
    # 执行
    # ==================================================================
    async def call(
        self,
        to_member: str = "",
        task: str = "",
        reason: str = "",
    ) -> ToolChunk:
        """把任务交给队友，回收结果。

        返回值是 ``ToolChunk``（**协程**，不是 async generator），因此调用方
        ``await tool(**kwargs)`` 直接得到它。参数名必须与 ``input_schema``
        的 ``properties`` 逐字一致 —— Agent 的 ``call_tool`` 是按 JSON 键
        展开成关键字参数的。

        Args:
            to_member (`str`): 接收方成员名。
            task (`str`): 任务文本。
            reason (`str`, optional): 移交理由。

        Returns:
            `ToolChunk`: 内容为 :attr:`HandoffOutcome.feedback`。失败**不抛
            异常**而是回一段错误文本：抛异常会让 Agent 循环把它记成工具错误，
            模型看不到可操作的下一步该做什么。
        """
        from harness_kit.multiagent.team import TeamResult  # 局部 import，避免成环

        target = (to_member or "").strip()
        body = (task or "").strip()

        if not body:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "[handoff FAILED] `task` is empty. Write the task "
                            "text you want the teammate to do."
                        ),
                    ),
                ],
            )
        if not target:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "[handoff FAILED] `to_member` is empty. "
                            f"Candidates: {sorted(self.team.names)}."
                        ),
                    ),
                ],
            )
        if target == self.self_name:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"[handoff FAILED] You ({self.self_name}) cannot "
                            "hand a task to yourself — that would just be "
                            "recursion. Do the work, or hand it to someone "
                            f"else: {sorted(set(self.team.names) - {self.self_name})}."
                        ),
                    ),
                ],
            )

        request = HandoffRequest(
            from_member=self.self_name,
            to_member=target,
            task=body,
            reason=reason or "",
        )
        result: "TeamResult" = await self.team.dispatch(
            body,
            member=target,
            depth=self.depth + 1,
        )
        outcome = HandoffOutcome(
            request=request,
            ok=result.ok,
            output=result.output,
            error=result.error,
            elapsed_ms=result.elapsed_ms,
        )
        self._outcomes.append(outcome)
        logger.info(
            "handoff: {} -> {} ok={} ({:.0f}ms)",
            request.from_member,
            target,
            outcome.ok,
            outcome.elapsed_ms,
        )
        return ToolChunk(content=[TextBlock(text=outcome.feedback)])

    # ==================================================================
    # 观测
    # ==================================================================
    @property
    def outcomes(self) -> list[HandoffOutcome]:
        """本工具发起过的所有移交（按时间顺序）。

        Returns:
            `list[HandoffOutcome]`: 移交记录副本。
        """
        return list(self._outcomes)

    def stats(self) -> dict[str, Any]:
        """汇总统计，供指标与 ``describe()`` 使用。

        Returns:
            `dict[str, Any]`: ``{"count", "ok", "failed", "to"}``。
        """
        return {
            "count": len(self._outcomes),
            "ok": sum(1 for _ in self._outcomes if _.ok),
            "failed": sum(1 for _ in self._outcomes if not _.ok),
            "to": sorted({_.request.to_member for _ in self._outcomes}),
        }


def collect_results(
    outcomes: Sequence[HandoffOutcome],
    *,
    max_chars: int = 4000,
) -> str:
    """把多次移交的结果拼成一段可回填的文本（结果回收）。

    Args:
        outcomes (`Sequence[HandoffOutcome]`): 移交结果。
        max_chars (`int`, defaults to `4000`): 总长上限；超出时按条截断。
            工具结果进上下文是**要花钱**的，一次广播把 8 个成员的长文全塞回去
            会直接吃掉下一轮的预算。

    Returns:
        `str`: 拼接文本；``outcomes`` 为空时返回一句说明而不是空串。
    """
    if not outcomes:
        return "（没有移交记录）"
    per = max(200, max_chars // len(outcomes))
    parts: list[str] = []
    for index, outcome in enumerate(outcomes, start=1):
        body = outcome.output if outcome.ok else (outcome.error or "")
        if len(body) > per:
            body = body[:per] + "…"
        flag = "ok" if outcome.ok else "fail"
        parts.append(
            f"[{index}] {outcome.request.from_member} -> "
            f"{outcome.request.to_member} ({flag})\n{body}",
        )
    return "\n\n".join(parts)
