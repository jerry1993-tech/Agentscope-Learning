# -*- coding: utf-8 -*-
"""短期上下文压缩（第 8 讲补遗）：把 ``ContextConfig`` 与 ``on_compress_context``
接进 Harness。

**为什么需要这一层**：参考架构 L1 的「持久记忆」写的是
**"短期上下文压缩 + 长期记忆 / 遗忘策略"** —— 这是两件事，而本系列原稿
只讲了后一半（第 15~19 讲的 ReMe 长记忆 + 遗忘）。前一半（短期压缩）在
AgentScope 里**是有的**，只是 Harness 从来没把它接出来：

- 压缩器本体：``Agent.compress_context()``（``third_party/agentscope/src/agentscope/agent/_agent.py:386``）；
- 真正干活的实现：``Agent._compress_context_impl()``（同文件 ``:490``）；
- 触发点：ReAct 循环每次进入 reasoning 前
  （``Agent._reply_impl`` 里 ``await self.compress_context()``，同文件 ``:1177``）；
- 配置面：``ContextConfig``（``third_party/agentscope/src/agentscope/agent/_config.py:51``），
  共 **10** 个字段：
  ``trigger_ratio`` / ``reserve_ratio`` / ``context_buffer_ratio`` /
  ``compression_prompt`` / ``summary_template`` / ``summary_schema`` /
  ``tool_result_limit`` / ``compression_fallback_to_truncation`` /
  ``compression_tool_enabled`` / ``max_image_num``；
- 扩展点：``MiddlewareBase.on_compress_context``
  （``third_party/agentscope/src/agentscope/middleware/_base.py:241``）——
  本系列第 8 讲实测过：**它是空链**，``Agent`` 构造时的
  ``_compress_context_middlewares``（``_agent.py:238``）在我们交付的
  Profile 里恒为 ``[]``。

所以这一层要补的不是"实现一个压缩算法"（那是重造轮子），而是三件事：

1. **把标量字段暴露成声明式配置**（:class:`ContextBudgetSpec` / :func:`build_context_config`）。
   原稿的 ``config/builder.py:_build_context_config`` 只把
   ``ToolsSpec.max_result_chars`` 翻译成 ``tool_result_limit``，
   另外 9 个字段一个都没接出来 ——
   于是"上下文超了怎么办"这件事在 Profile 里根本没法表达。
   本讲把其中 6 个标量接出来（触发比 / 保留比 / 缓冲带 / 压缩工具开关 /
   失败降级开关 / 图片上限），剩下 3 个（``compression_prompt`` /
   ``summary_template`` / ``summary_schema``）是 prompt 与 schema 对象，
   **刻意留官方默认**，理由见本节末"不做的事"。
2. **实现 ``on_compress_context``**（:class:`ContextCompactionMiddleware`），
   把"压没压、压掉多少、花了多久"变成可观测事实。这是纯增量：
   不改 Agent 一行代码，只是往 ``middlewares=[...]`` 里多传一个对象。
3. **给压缩加一道护栏**（``block_when_pending_tools``）：当 ``state.context``
   里还存在**没有拿到结果**的 ``ToolCallBlock`` 时拒绝压缩。
   这条不变式的方向与官方一致 —— 官方在切分时也不会把一个"已发出、
   结果未回"的调用丢进摘要（``Agent._split_context_for_compression``，
   ``third_party/agentscope/src/agentscope/agent/_agent.py:2927-2941``，
   配合 ``AgentState.get_unfinished_tool_calls``，
   ``third_party/agentscope/src/agentscope/state/_state.py:374-403``）——
   但**判据的覆盖面不同**：官方那条只看 ``state.context[-1]``，而且要求
   ``last_msg.id == state.reply_id``（``_state.py:392-397``），
   也就是"本轮 reply 里刚发出、还没回的那一个"。
   一旦上下文是从事件日志恢复出来的（第 9 讲）、或被 ``observe`` /
   历史消息带进了更早的悬空调用，官方的判据就够不着，
   而"发起端被压进摘要、结果端还在保留区"会造出一份对不上号的对话。
   本类用**全量 id 集合差**（:func:`count_pending_tool_calls`）覆盖这个更宽的情形。

   这是一次**刻意的加严**，代价要写清楚：默认 ``block_when_pending_tools=True``
   时，只要上下文里存在悬空调用就整轮不压 —— 而官方那条路在这种情形下
   其实还能安全地压掉更老的部分。想恢复官方语义就设
   ``block_when_pending_tools=False``（验证脚本 F4 段实测放行）。
   默认选加严，是因为生产上"压缩推迟一轮"的代价是几个 token，
   而"对话结构对不上"的代价是一次 400。

**明确不做的事**：不自己写摘要 prompt（用官方
``ContextConfig.compression_prompt``，``_config.py:82``）、
不自己定义摘要 schema（用官方 ``ContextConfig.summary_schema``，``_config.py:140``）、
不自己调模型（用官方 ``ChatModelBase.generate_structured_output``，
``third_party/agentscope/src/agentscope/model/_base.py:457`` ——
``Agent._compress_context_impl`` 在 ``_agent.py:633`` 就是走这条）、
不重写 Agent 的压缩流程（只挂 hook）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.agent import ContextConfig

from harness_kit.middleware.base import HarnessMiddleware

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.agent import Agent

__all__ = [
    "CompactionRecord",
    "ContextBudgetSpec",
    "ContextCompactionMiddleware",
    "build_context_config",
    "count_pending_tool_calls",
]


# ----------------------------------------------------------------------
# 声明式配置：把官方 9 个字段接出来
# ----------------------------------------------------------------------


class ContextBudgetSpec(BaseModel):
    """``ContextConfig`` 的声明式投影（装配时翻译过去，见 :func:`build_context_config`）。

    字段名与官方 ``ContextConfig`` 逐一对应，单位也一致（比例是 0~1 的
    **占模型上下文窗口的比**，不是 token 数）。带 ``_tokens`` 后缀的
    ``tool_result_limit_tokens`` 用的是 token 数。

    **刻意不投影的 3 个字段**：``compression_prompt`` / ``summary_template`` /
    ``summary_schema`` —— 它们是 prompt 文本与 JSON schema 对象，不是可调标量。
    把它们塞进 Profile 等于让用户在 YAML 里手写摘要 prompt 与 schema，
    属于"重造官方轮子"，本讲不做（要用就直接构造 ``ContextConfig`` 传给
    ``Agent`` 的 ``context_config=``）。

    **为什么不直接把 ``ContextConfig`` 放进 Profile**：``ContextConfig``
    是 AgentScope 的 pydantic 模型，放进 Profile schema 会让"配置格式"
    与"上游类型"耦合 —— 上游改一个字段名，用户的 Profile 就全废。
    这里做一层显式投影，默认值与官方一致，加字段是显式的。
    """

    model_config = ConfigDict(extra="forbid")

    trigger_ratio: float = Field(default=0.8, gt=0, le=0.9)
    """token 超过窗口的这个比例就触发压缩（官方默认 0.8，上限 0.9）。"""

    reserve_ratio: float = Field(default=0.1, gt=0, lt=0.9)
    """压缩后**保留**的近期上下文比例（官方默认 0.1）。"""

    context_buffer_ratio: float = Field(default=0.2, ge=0, le=1)
    """触发线之前的缓冲带；开了 runtime state 注入或压缩工具时才有意义。"""

    compression_tool_enabled: bool = False
    """是否把 ``CompressContext`` 工具暴露给模型，让它自己决定什么时候压。"""

    compression_fallback_to_truncation: bool = True
    """摘要生成失败时是否退化成"砍掉最老的消息"。``False`` 会改为抛错。"""

    max_image_num: int = Field(default=5, ge=0)
    """上下文里最多保留几张图（超出的最老的会被 offload 或丢弃）。"""

    tool_result_limit_tokens: int | None = Field(default=None, gt=0)
    """单条工具结果的上限（token）。``None`` 表示沿用官方默认 50000。"""

    def to_context_config(self) -> ContextConfig:
        """翻译成官方 ``ContextConfig``。

        Returns:
            `ContextConfig`: AgentScope 原生配置对象。
        """
        fields: dict[str, Any] = {
            "trigger_ratio": self.trigger_ratio,
            "reserve_ratio": self.reserve_ratio,
            "context_buffer_ratio": self.context_buffer_ratio,
            "compression_tool_enabled": self.compression_tool_enabled,
            "compression_fallback_to_truncation": self.compression_fallback_to_truncation,
            "max_image_num": self.max_image_num,
        }
        if self.tool_result_limit_tokens is not None:
            fields["tool_result_limit"] = self.tool_result_limit_tokens
        return ContextConfig(**fields)


def build_context_config(
    spec: ContextBudgetSpec | None = None,
    *,
    tool_result_limit_tokens: int | None = None,
) -> ContextConfig:
    """装配入口：``ContextBudgetSpec`` → ``ContextConfig``。

    Args:
        spec (`ContextBudgetSpec | None`): 声明式配置；``None`` 表示全用官方默认。
        tool_result_limit_tokens (`int | None`): 覆盖单条工具结果上限
            （第 2 讲的 ``ToolsSpec.max_result_chars`` 换算结果从这条路进来，
            保持原有行为不变）。

    Returns:
        `ContextConfig`: 官方配置对象。
    """
    config = (spec or ContextBudgetSpec()).to_context_config()
    if tool_result_limit_tokens is not None:
        config.tool_result_limit = tool_result_limit_tokens
    return config


# ----------------------------------------------------------------------
# 可观测事实
# ----------------------------------------------------------------------


class CompactionRecord(BaseModel):
    """一次 ``compress_context`` 调用的可观测事实。

    这个对象就是"压缩层"的产物：调用方把它写进事件日志 / 指标，
    于是"这一轮的 43 秒里有多少花在压缩上"变成了可回答的问题。
    """

    model_config = ConfigDict(extra="forbid")

    compressed: bool = False
    """是否真的发生了压缩（``state.context`` 长度或 ``summary`` 变了）。"""

    skipped_reason: str = ""
    """没压的原因（``pending_tool_calls`` / ``too_few_messages`` / ``noop``）。"""

    n_msgs_before: int = 0
    """调用前 ``state.context`` 的消息条数。"""

    n_msgs_after: int = 0
    """调用后 ``state.context`` 的消息条数。"""

    summary_chars: int = 0
    """调用后 ``state.summary`` 的字符数（压缩成功的直接证据）。"""

    elapsed_ms: float = 0.0
    """整条 ``on_compress_context`` 链的墙钟耗时（毫秒）。"""

    def to_line(self) -> str:
        """一行摘要。

        Returns:
            `str`: 形如 ``"compressed=True 12→2 msgs, summary=1804 chars, 5123ms"``。
        """
        if not self.compressed:
            return (
                f"compressed=False ({self.skipped_reason or 'noop'}) "
                f"msgs={self.n_msgs_before}, {self.elapsed_ms:.1f}ms"
            )
        return (
            f"compressed=True {self.n_msgs_before}→{self.n_msgs_after} msgs, "
            f"summary={self.summary_chars} chars, {self.elapsed_ms:.1f}ms"
        )


# ----------------------------------------------------------------------
# 护栏：数一数"还没拿到结果的工具调用"
# ----------------------------------------------------------------------


def count_pending_tool_calls(agent: "Agent") -> int:
    """数出 ``state.context`` 里**发起了但没拿到结果**的工具调用数。

    判定是纯集合运算：扫全量 context，收集所有 ``ToolCallBlock.id`` 与
    所有 ``ToolResultBlock.id``，差集大小就是"悬空"的调用数。
    **不按消息顺序假设**（官方并发执行工具时结果顺序不保证，
    见第 2 讲 §2.4），所以这里只做 id 集合差。

    与官方的对应物 ``AgentState.get_unfinished_tool_calls(name)``
    （``third_party/agentscope/src/agentscope/state/_state.py:374``）
    的差别就两点，都指向"我们看得更宽"：

    - 官方只看 ``context[-1]``，我们看**全量**；
    - 官方要求 ``context[-1].id == state.reply_id``（``_state.py:392-397``），
      即"本轮的这条回复"，我们不做这个前提。

    Args:
        agent (`Agent`): AgentScope ``Agent``（读 ``agent.state.context``）。

    Returns:
        `int`: 悬空的工具调用数；无法解析 context 时返回 ``0``（不拦）。
    """
    from agentscope.message import ToolCallBlock, ToolResultBlock

    try:
        context = list(agent.state.context)
    except Exception:  # pylint: disable=broad-exception-caught
        return 0

    called: set[str] = set()
    returned: set[str] = set()
    for msg in context:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, ToolCallBlock):
                called.add(block.id)
            elif isinstance(block, ToolResultBlock):
                returned.add(block.id)
    return len(called - returned)


# ----------------------------------------------------------------------
# 中间件本体
# ----------------------------------------------------------------------


class ContextCompactionMiddleware(HarnessMiddleware):
    """把 ``on_compress_context`` 变成可观测、有护栏的一环。

    **它不改压缩算法本身**：摘要 prompt、schema、模型调用全部还是官方的
    ``Agent._compress_context_impl``。它只加两件事：

    1. **观测**：压缩前后各拍一张快照，差值折成 :class:`CompactionRecord`；
    2. **护栏**（``block_when_pending_tools=True``，默认开）：
       悬空工具调用数 > 0 时**不调用** ``next_handler``，即本轮不压缩
       （加严的范围与代价见模块 docstring 第 3 条；设 ``False``
       即恢复"官方怎么压我就怎么放行"）。

    第 2 条是"onion hook 可以短路"这一语义的正当用法：hook 包住了原方法，
    不往下传就等于不执行 —— 与官方 ``ReplyBudgetControlMiddleware``
    在 ``on_reasoning`` 里把 ``tool_choice`` 覆写成 ``none`` 是同一类手法。

    关掉护栏时（``block_when_pending_tools=False``），它对压缩行为**零影响**：
    ``next_handler`` 一定被调用，唯一的额外动作是读两次 ``state.context``
    长度与 ``state.summary`` 长度。
    """

    def __init__(
        self,
        *,
        block_when_pending_tools: bool = True,
        min_messages: int = 2,
        on_compaction: Callable[[CompactionRecord], Awaitable[None] | None] | None = None,
    ) -> None:
        """构造。

        Args:
            block_when_pending_tools (`bool`): 有悬空工具调用时是否拒绝压缩。
            min_messages (`int`): ``state.context`` 少于这个条数就直接跳过
                （省级一次无意义的模型调用）。必须为正。
            on_compaction (`Callable[[CompactionRecord], Awaitable[None] | None] | None`):
                观测回调；同步或异步都行（异步的会被 ``await``）。
                回调抛异常会被吞掉并打 warning —— **观测失败不许炸主流程**。

        Raises:
            ValueError: ``min_messages`` 非正。
        """
        if min_messages <= 0:
            raise ValueError(f"min_messages 必须为正，收到 {min_messages}")
        self.block_when_pending_tools = block_when_pending_tools
        self.min_messages = min_messages
        self._on_compaction = on_compaction
        self.records: list[CompactionRecord] = []
        """本中间件产生的全部记录（内存保留，供测试与调试读取）。"""

    async def on_compress_context(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        """onion hook：观测 + 护栏。

        Args:
            agent (`Agent`): 执行压缩的 Agent。
            input_kwargs (`dict`): 官方约定含 ``context_config`` / ``instructions``。
            next_handler (`Callable[..., Awaitable[None]]`): 链上的下一环。
        """
        started = time.perf_counter()

        try:
            before = list(agent.state.context)
            summary_before = str(getattr(agent.state, "summary", "") or "")
        except Exception:  # pylint: disable=broad-exception-caught
            # 读不到状态就不做任何判断，原样透传 —— 护栏绝不能变成路障。
            await next_handler(**input_kwargs)
            return

        pending = count_pending_tool_calls(agent)
        if self.block_when_pending_tools and pending > 0:
            record = CompactionRecord(
                compressed=False,
                skipped_reason=f"pending_tool_calls({pending})",
                n_msgs_before=len(before),
                n_msgs_after=len(before),
                summary_chars=len(summary_before),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            await self._finish(agent, record)
            return

        if len(before) < self.min_messages:
            record = CompactionRecord(
                compressed=False,
                skipped_reason=f"too_few_messages(<{self.min_messages})",
                n_msgs_before=len(before),
                n_msgs_after=len(before),
                summary_chars=len(summary_before),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            await self._finish(agent, record)
            return

        await next_handler(**input_kwargs)

        try:
            after = list(agent.state.context)
            summary_after = str(getattr(agent.state, "summary", "") or "")
        except Exception:  # pylint: disable=broad-exception-caught
            after, summary_after = before, summary_before

        compressed = len(after) != len(before) or summary_after != summary_before
        record = CompactionRecord(
            compressed=compressed,
            skipped_reason="" if compressed else "noop(未达触发线或无需压缩)",
            n_msgs_before=len(before),
            n_msgs_after=len(after),
            summary_chars=len(summary_after),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        await self._finish(agent, record)

    # ------------------------------------------------------------------
    async def _finish(self, agent: "Agent", record: CompactionRecord) -> None:
        """记录 + 回调 + 日志。

        Args:
            agent (`Agent`): Agent 实例（只为日志里的名字）。
            record (`CompactionRecord`): 本次记录。
        """
        self.records.append(record)
        logger.bind(
            agent=getattr(agent, "name", "?"),
            compressed=record.compressed,
            skipped=record.skipped_reason,
            msgs_before=record.n_msgs_before,
            msgs_after=record.n_msgs_after,
            elapsed_ms=round(record.elapsed_ms, 1),
        ).info("上下文压缩 {}", record.to_line())

        if self._on_compaction is None:
            return
        try:
            outcome = self._on_compaction(record)
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.bind(error=str(error)).warning(
                "ContextCompactionMiddleware 回调失败，已忽略（观测失败不许炸主流程）",
            )
