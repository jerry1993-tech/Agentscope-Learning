# -*- coding: utf-8 -*-
"""主管-工人团队编排（契约 §3.13，第 13 讲）。

**先说清楚 AgentScope 里到底有什么、没有什么**（这是本模块存在的全部理由）：

- **有**：``Agent``（``third_party/agentscope/src/agentscope/agent/_agent.py:117``）
  本身就是一个完整的 ReAct 状态机，自带工具调用、权限、上下文压缩；
- **有（但在 app 服务层）**：派生/邀请/建队四个工具
  ``AgentCreate``（``.../app/_tool/_agent_create.py:145``）、``AgentInvite``、
  ``TeamCreate``（``.../app/_tool/_team_create.py:30``）、``TeamSay``，
  以及 ``app/middleware/_team_member_middleware.py`` 和 ``app/message_bus/``；
- **没有（在 SDK 层）**：任何「本地多 Agent 编排」的原语。
  ``_recon/02_agentscope_agent_loop.md:1234`` 的结论是「核心 ``Agent`` 本身
  **没有** ``spawn_subagent`` 之类的能力」；``A2AAgent``
  （``.../agent/_a2a_agent.py:189``）是**跨进程**的 A2A 协议客户端，它连
  ``Agent`` 都不继承，且本环境没装 ``a2a`` extra（``_recon/09:1278``）。

所以本模块补的就是 SDK 层缺的那一块：**一个进程内的、可测试的、带预算闸门的
成员注册表 + 派活 + 收活**。它不实现任何推理循环 —— 成员就是 ``Agent``，
``dispatch`` 只是 ``await member.reply(...)``。

**三条硬语义**（教程正文要断言的）：

1. **不共享消息历史**。每个成员是一个**独立 ``Agent`` 实例**，各自持有独立
   ``AgentState``（``.../state/_state.py:209``）。``dispatch`` 只把任务文本
   送进去、把最终文本取出来，**两台 Agent 的 ``state.context`` 永不相通**。
   这既是隔离（工人之间不会互相污染），也是并发的**前提**。
2. **串行还是并行**。``dispatch`` 是**串行安全**的：单个 Agent 的 ``context``
   是一条共享列表，同一个 Agent 并发 reply 会把上下文交错写乱。``broadcast``
   之所以敢用 ``asyncio.gather``，是因为它按**成员**分派，一个成员一次只跑
   一个任务 —— 这由 :class:`~harness_kit.multiagent.limits.SpawnLimiter` 的
   ``max_concurrent`` 与「广播前先去重成员」两件事共同保证。
3. **派活要先过闸门**。每次派活都从 ``SpawnLimiter`` 领一张票据，``finally``
   归还。撞上限额时**不抛到调用方**，而是把它变成一条 ``ok=False`` 的
   ``TeamResult``：编排层要能区分「这个工人干不了」和「闸门不放行」，
   前者可以换人重试，后者换人也没用。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg

from harness_kit.multiagent.limits import SpawnLimitExceeded, SpawnLimiter
from harness_kit.multiagent.router import CapabilityRouter, NoRouteError

__all__ = [
    "DEFAULT_HANDOFF_TOOL_NAME",
    "AgentTeam",
    "TeamResult",
]

DEFAULT_HANDOFF_TOOL_NAME: str = "handoff_to_teammate"
"""交接工具的默认名字（:mod:`harness_kit.multiagent.handoff` 里定义实现）。"""

_TASK_PROMPT = """\
<system-reminder>You are "{member}", one member of a team. You are given \
exactly one task. Do it and answer with the result.

## Your task

{task}

Do not ask for clarification, do not delegate — nobody else will read your \
answer, only your final text is returned to the team head.</system-reminder>"""


class TeamResult(BaseModel):
    """一次派活的结果（契约 §3.13）。

    字段与契约逐字一致，额外两个带默认值的字段用于观测（不影响按契约构造）。
    """

    model_config = ConfigDict(extra="forbid")

    member: str
    """接活的成员名。"""

    ok: bool
    """是否成功拿到产物。"""

    output: str = ""
    """产物文本（成员最终回复的纯文本）。"""

    elapsed_ms: float = 0.0
    """耗时（毫秒），含闸门等待与模型时间。"""

    error: str | None = None
    """失败原因。三类：``SpawnLimitExceeded:...`` / ``NoRouteError:...`` /
    成员自身抛出的异常串。**区分它们才能决定是重试、换人还是收工。**"""

    task: str = ""
    """原始任务文本（截断到 500 字），便于把结果和任务对上。"""

    depth: int = 0
    """这次派活的深度。"""

    @property
    def gate(self) -> str | None:
        """失败是否由闸门造成，是则返回闸门名。

        Returns:
            `str | None`: ``"max_spawn"`` / ``"max_depth"`` /
            ``"max_concurrent"``；不是闸门失败则为 ``None``。
        """
        if self.error and self.error.startswith("SpawnLimitExceeded"):
            for gate in ("max_depth", "max_spawn", "max_concurrent"):
                if gate in self.error:
                    return gate
        return None

    def summary(self, *, limit: int = 200) -> str:
        """单行摘要。

        Args:
            limit (`int`, defaults to `200`): 产物截断长度。

        Returns:
            `str`: 形如 ``"[ok] researcher (1234ms) :: ..."``。
        """
        body = self.output if len(self.output) <= limit else self.output[:limit] + "…"
        if not self.ok:
            body = f"ERROR {self.error}"
        return f"[{'ok' if self.ok else 'fail'}] {self.member} ({self.elapsed_ms:.0f}ms) :: {body}"


class AgentTeam:
    """主管 + 若干工人的进程内团队。

    Args:
        members (`dict[str, Agent]`): ``{成员名: Agent}``。**每个成员必须是独立
            的 ``Agent`` 实例**；把同一个实例放进两个名字下会让「不共享历史」
            这条语义失效（两名字共用一个 ``AgentState``）。
        router (`CapabilityRouter`): 能力路由表。``members`` 里不在路由表里的
            成员永远选不上，构造时会给一条 warning。
        limits (`SpawnLimiter`): 派生闸门。

    Raises:
        ValueError: ``members`` 为空；或同一个 ``Agent`` 实例被登记在两个名字下。
    """

    def __init__(
        self,
        *,
        members: dict[str, Agent],
        router: CapabilityRouter,
        limits: SpawnLimiter,
    ) -> None:
        """初始化并做一致性检查。"""
        if not members:
            raise ValueError("AgentTeam 至少要有一个成员。")
        seen: dict[int, str] = {}
        for name, agent in members.items():
            key = id(agent)
            if key in seen:
                raise ValueError(
                    f"成员 {name!r} 与 {seen[key]!r} 是**同一个 Agent 实例**。"
                    "团队要求每个成员持有独立 AgentState，否则「不共享消息历史」"
                    "这条语义不成立，并发派活也会把上下文写乱。",
                )
            seen[key] = name
        self._members: dict[str, Agent] = dict(members)
        self.router = router
        self.limits = limits
        self._handoff_installed: set[str] = set()
        self._handoff_tools: dict[tuple[str, str], Any] = {}

        routable = set(router.members)
        unknown = sorted(set(self._members) - routable)
        if unknown:
            logger.warning(
                "AgentTeam: 成员 {} 不在路由表里，永远不会被 route 选中"
                "（broadcast 显式点名除外）。",
                unknown,
            )
        missing_agents = sorted(routable - set(self._members))
        if missing_agents:
            logger.warning(
                "AgentTeam: 路由表里的 {} 没有对应的 Agent，派到它们会失败。",
                missing_agents,
            )

    # ==================================================================
    # 注册表
    # ==================================================================
    @property
    def names(self) -> list[str]:
        """成员名（字典序）。

        Returns:
            `list[str]`: 成员名。
        """
        return sorted(self._members)

    def agent(self, member: str) -> Agent:
        """取成员 Agent。

        Args:
            member (`str`): 成员名。

        Returns:
            `Agent`: AgentScope 的 Agent 实例。

        Raises:
            NoRouteError: 成员不存在。
        """
        try:
            return self._members[member]
        except KeyError as exc:
            raise NoRouteError.for_member(member, self.names) from exc

    def add_member(
        self,
        member: str,
        agent: Agent,
        *,
        capabilities: Sequence[str] | None = None,
    ) -> None:
        """加一个成员（同时登记路由能力）。

        Args:
            member (`str`): 成员名。
            agent (`Agent`): 独立 Agent 实例。
            capabilities (`Sequence[str] | None`, optional): 能力标签。给了就
                顺手写进路由表；``None`` 表示路由表由调用方自己维护。

        Raises:
            ValueError: 重名，或复用了已有成员的 Agent 实例。
        """
        if member in self._members:
            raise ValueError(f"成员 {member!r} 已存在。")
        if any(id(agent) == id(_) for _ in self._members.values()):
            raise ValueError(
                f"成员 {member!r} 用的是已有成员的 Agent 实例，"
                "违反了「每个成员独立 AgentState」的前提。",
            )
        self._members[member] = agent
        if capabilities is not None:
            try:
                self.router.add_member(member, capabilities)
            except ValueError:
                logger.warning("路由表里已有 {}，沿用旧能力标签。", member)

    def remove_member(self, member: str) -> bool:
        """移除成员（路由表里也一并移除，失败只告警）。

        Args:
            member (`str`): 成员名。

        Returns:
            `bool`: 真删掉了为 ``True``。
        """
        if member not in self._members:
            return False
        del self._members[member]
        self._handoff_installed.discard(member)
        for key in [_ for _ in self._handoff_tools if _[0] == member]:
            del self._handoff_tools[key]
        try:
            self.router.remove_member(member)
        except ValueError as exc:
            logger.warning("路由表移除 {} 失败：{}", member, exc)
        return True

    # ==================================================================
    # 派活
    # ==================================================================
    async def dispatch(
        self,
        task: str,
        *,
        required_capability: str | None = None,
        member: str | None = None,
        depth: int = 0,
        timeout_s: float | None = None,
    ) -> TeamResult:
        """把任务交给一个成员并收集结果。

        Args:
            task (`str`): 任务文本。
            required_capability (`str | None`, optional): 必需能力，透传给
                :meth:`CapabilityRouter.route`。
            member (`str | None`, optional): **显式指定**成员，跳过路由。
                给它是为了「主管点名」与「重试时换人」两条路径。
            depth (`int`, defaults to `0`): 本次派活的深度，会被送进闸门。
            timeout_s (`float | None`, optional): 单次派活的超时。``None`` 表示
                不额外加超时（``Agent`` 自己的模型调用有超时与重试）。

        Returns:
            `TeamResult`: 结果。**任何失败都不抛异常**，而是 ``ok=False`` +
            ``error`` —— 编排层需要一个统一的返回值形状，否则每个调用点都要
            写一遍 ``try/except`` 三件套。

        Raises:
            NoRouteError: 路由失败且没有可用成员（这是**配置错误**，静默返回
                ``ok=False`` 会让人以为只是模型不行，所以这里显式抛）。
        """
        started = time.perf_counter()
        if member is None:
            member = self.router.route(task, required=required_capability)

        if member not in self._members:
            return TeamResult(
                member=member,
                ok=False,
                error=(
                    f"NoRouteError: 路由选中的成员 {member!r} 没有对应的 Agent；"
                    f"现有成员 {self.names}。"
                ),
                elapsed_ms=(time.perf_counter() - started) * 1000,
                task=task[:500],
                depth=depth,
            )

        try:
            ticket = self.limits.try_acquire(depth=depth)
        except SpawnLimitExceeded as exc:
            logger.warning("dispatch 被闸门拒绝：{}", exc)
            return TeamResult(
                member=member,
                ok=False,
                error=f"SpawnLimitExceeded({exc.gate}): {exc}",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                task=task[:500],
                depth=depth,
            )

        try:
            output = await self._ask(
                member,
                task,
                timeout_s=timeout_s,
            )
            ok, error = True, None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            output, ok = "", False
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("成员 {} 派活失败：{}", member, error)
        finally:
            ticket.release()

        elapsed = (time.perf_counter() - started) * 1000
        self.router.record(member, ok=ok)
        result = TeamResult(
            member=member,
            ok=ok,
            output=output,
            elapsed_ms=elapsed,
            error=error,
            task=task[:500],
            depth=depth,
        )
        logger.info("dispatch: {}", result.summary(limit=80))
        return result

    async def broadcast(
        self,
        task: str,
        *,
        members: list[str] | None = None,
        depth: int = 0,
        timeout_s: float | None = None,
    ) -> list[TeamResult]:
        """把同一个任务发给多个成员，并发收活。

        Args:
            task (`str`): 任务文本。
            members (`list[str] | None`, optional): 显式指定成员列表。``None``
                时由 :meth:`CapabilityRouter.route_many` 选（去重后取能力互补的
                前几个）。**显式列表会去重**：同一个成员跑两遍既浪费又会让
                它自己的上下文串起来。
            depth (`int`, defaults to `0`): 派活深度。
            timeout_s (`float | None`, optional): 每个成员的超时。

        Returns:
            `list[TeamResult]`: 与去重后的成员列表等长、**顺序一致**的结果。
            并发失败（闸门拒绝）会以 ``ok=False`` 出现在对应位置。
        """
        targets = list(dict.fromkeys(members)) if members else self.router.route_many(
            task,
            k=self.limits.max_concurrent,
            unique_capability=True,
        )
        if not targets:
            return []
        results = await asyncio.gather(
            *(
                self.dispatch(
                    task,
                    member=name,
                    depth=depth,
                    timeout_s=timeout_s,
                )
                for name in targets
            ),
        )
        logger.info(
            "broadcast: {} 个成员，成功 {} 个",
            len(results),
            sum(1 for _ in results if _.ok),
        )
        return list(results)

    async def _ask(
        self,
        member: str,
        task: str,
        *,
        timeout_s: float | None,
    ) -> str:
        """真正调用成员 Agent：``Agent.reply_stream`` + 取最终文本。

        用 ``reply_stream(..., yield_final_msg=True)`` 而不是 ``reply()``：
        前者让我们能**边收边丢**（大团队的中间事件不需要全留在内存里），
        同时拿到最终的 ``Msg``。取文本用 ``Msg.get_text_content()``
        （``.../message/_base.py:156``），它会跳过 ``ThinkingBlock`` /
        ``ToolCallBlock``，只拼 ``TextBlock``。

        Args:
            member (`str`): 成员名。
            task (`str`): 任务文本。
            timeout_s (`float | None`): 超时。

        Returns:
            `str`: 成员的最终文本。

        Raises:
            TimeoutError: 超时。
            RuntimeError: 成员没有产出任何 Msg。
        """
        agent = self.agent(member)
        prompt = _TASK_PROMPT.format(member=member, task=task)

        async def _consume() -> str:
            final: Msg | None = None
            async for item in agent.reply_stream(
                inputs=UserMsg("user", prompt),
                yield_final_msg=True,
            ):
                if isinstance(item, Msg):
                    final = item
            if final is None:
                raise RuntimeError(
                    f"成员 {member!r} 没有产出最终消息（reply_stream 结束但没有 Msg）。",
                )
            return final.get_text_content() or ""

        if timeout_s is None:
            return await _consume()
        return await asyncio.wait_for(_consume(), timeout=timeout_s)

    # ==================================================================
    # 交接工具
    # ==================================================================
    def handoff_tool(
        self,
        self_name: str,
        *,
        tool_name: str = DEFAULT_HANDOFF_TOOL_NAME,
    ) -> Any:
        """造一个交接工具（契约 §3.13 的 ``HandoffTool``）。

        延迟 import：``handoff`` 需要 ``AgentTeam`` 做类型注解，顶层互相
        import 会成环，所以把 import 放进方法里。

        **同一个 ``(成员, 工具名)`` 只造一个实例并缓存**：每次调用都新建的话，
        ``install_handoff_tool`` 会把上一次的 ``HandoffTool`` 连同它记下的
        ``outcomes``（移交历史）一起丢掉，于是「结果回收」在重复安装后就查不到
        任何东西了 —— 验证脚本里真的踩到过（``supervise`` 内部会再装一次，
        外部拿到的那只就成了孤儿）。

        Args:
            self_name (`str`): 持有这个工具的成员名（用它挡住「自己交给自己」）。
            tool_name (`str`, defaults to ``"handoff_to_teammate"``): 工具名。

        Returns:
            `HandoffTool`: 实现 ``ToolBase`` 的交接工具（同一成员重复取是同一实例）。
        """
        from harness_kit.multiagent.handoff import HandoffTool

        if self_name not in self._members:
            raise NoRouteError.for_member(self_name, self.names)
        key = (self_name, tool_name)
        tool = self._handoff_tools.get(key)
        if tool is None:
            tool = HandoffTool(
                team=self,
                self_name=self_name,
                tool_name=tool_name,
            )
            self._handoff_tools[key] = tool
        return tool

    @property
    def handoff_tools(self) -> dict[str, Any]:
        """已造出的交接工具：``{成员名: HandoffTool}``。

        Returns:
            `dict[str, Any]`: 成员名到工具的映射（同一个成员装了多个工具名时
            只保留第一个）。查移交历史用 ``team.handoff_tools["boss"].outcomes``。
        """
        out: dict[str, Any] = {}
        for (member, _), tool in self._handoff_tools.items():
            out.setdefault(member, tool)
        return out

    async def install_handoff_tool(
        self,
        member: str,
        *,
        tool_name: str = DEFAULT_HANDOFF_TOOL_NAME,
        group_name: str = "basic",
    ) -> Any:
        """把交接工具装进某个成员的 Toolkit（幂等）。

        这是「让主管 Agent 自己决定把活交给谁」的唯一姿势：**工具进 Toolkit，
        ReAct 循环自己会调**。我们不写调度循环 —— ``Agent`` 的
        ``_reply`` 就是那个循环（``.../agent/_agent.py:892``）。

        **为什么默认放进 ``"basic"`` 组**：模型能看到的工具 =
        ``"basic"`` 组 + ``state.tool_context.activated_groups`` 里的组
        （``.../agent/_agent.py:3268`` 把 ``activated_groups`` 传给
        ``Toolkit.get_tool_schemas``，而该方法的 docstring 明确写
        「The "basic" group will always be included」，
        ``.../tool/_toolkit.py:171``）。放进一个非 basic 组而不激活它，
        结果是**模型根本看不到这个工具**，表现为「主管从不派活」这种极难查的
        静默失效。所以默认放 basic；要分开管理就显式给 ``group_name``，
        此时本方法会顺手把该组加进 ``activated_groups`` 立刻激活它。

        Args:
            member (`str`): 成员名。
            tool_name (`str`): 工具名。
            group_name (`str`, defaults to ``"basic"``): 工具组名。非
                ``"basic"`` 时会被建出来并立即激活。

        Returns:
            `HandoffTool`: 装进去的工具实例。

        Raises:
            NoRouteError: 成员不存在。
            ValueError: 组名非法（例如库里已有同名工具组的冲突）。
        """
        agent = self.agent(member)
        tool = self.handoff_tool(member, tool_name=tool_name)
        toolkit = agent.toolkit
        if group_name != "basic" and not any(
            _.name == group_name for _ in toolkit.tool_groups
        ):
            from agentscope.tool import ToolGroup

            toolkit.tool_groups.append(
                ToolGroup(
                    name=group_name,
                    description="Team handoff tools.",
                    tools=[],
                ),
            )
        # 幂等：先删后加，重复调用不会堆出两份同名 schema。
        await toolkit.remove_tool(tool_name)
        await toolkit.add_tool(tool, group_name=group_name)
        if group_name != "basic":
            activated = agent.state.tool_context.activated_groups
            if group_name not in activated:
                activated.append(group_name)
        self._handoff_installed.add(member)
        logger.info(
            "install_handoff_tool: {} <- {} (group={})",
            member,
            tool_name,
            group_name,
        )
        return tool

    async def supervise(
        self,
        task: str,
        *,
        supervisor: str,
        depth: int = 0,
        timeout_s: float | None = None,
    ) -> TeamResult:
        """让一个「主管」成员自己决定把活派给谁。

        与 :meth:`dispatch` 的区别：``dispatch`` 由**我们的代码**选人，
        ``supervise`` 由**主管 Agent 的 ReAct 循环**选人 —— 主管拿到
        ``handoff`` 工具后，模型自己决定调不调、调给谁、调几次，工具结果
        自动回填到它的上下文里。**这里没有我们写的循环**，一行都没有。

        Args:
            task (`str`): 任务文本。
            supervisor (`str`): 主管成员名。
            depth (`int`, defaults to `0`): 主管自身的派活深度。
            timeout_s (`float | None`, optional): 超时。

        Returns:
            `TeamResult`: 主管的最终答复。``member`` 字段是主管名。
        """
        await self.install_handoff_tool(supervisor)
        return await self.dispatch(
            task,
            member=supervisor,
            depth=depth,
            timeout_s=timeout_s,
        )

    # ==================================================================
    # 观测
    # ==================================================================
    def describe(self) -> str:
        """多行人读描述。

        Returns:
            `str`: 成员、能力、闸门与路由历史。
        """
        lines = [
            f"AgentTeam(members={len(self._members)}, "
            f"limits: {self.limits.describe()})",
        ]
        history = self.router.history()
        for name in self.names:
            caps = self.router.capabilities_of(name) if name in set(
                self.router.members,
            ) else []
            stat = history.get(name, {})
            lines.append(
                f"  {name:<16} caps={caps} "
                f"n={int(stat.get('attempts', 0))} "
                f"rate={stat.get('success_rate', 0.0):.2f}",
            )
        return "\n".join(lines)

    async def aclose(self) -> None:
        """释放团队引用（``Agent`` 本身没有 ``aclose``）。

        **刻意不关闭 ``Agent``**：``Agent`` 不持有需要显式释放的资源 —— 工作区
        与模型都是构造时注入的、由 :class:`~harness_kit.config.builder.HarnessBuilder`
        统一管理。团队在这里把它们关掉，会让「builder 故意把同一个
        ``ChatModelBase`` 实例共享给多个 agent」的用法被提前打断。
        因此 ``aclose`` 只断开引用，真正的释放由 builder 做。
        """
        self._members.clear()
        self._handoff_installed.clear()
        self._handoff_tools.clear()
