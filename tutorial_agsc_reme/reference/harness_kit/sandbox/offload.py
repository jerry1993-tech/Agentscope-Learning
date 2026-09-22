# -*- coding: utf-8 -*-
"""大文件 / 大输出的 offload 封装（契约 §3.10，第 10 讲）。

**协议本体**（``third_party/agentscope/src/agentscope/workspace/_offload_protocol.py:8``）：

.. code-block:: python

    class Offloader(Protocol):
        async def offload_data_block(self, block: DataBlock) -> DataBlock: ...
        async def offload_context(self, session_id: str, msgs: list[Msg]) -> str: ...
        async def offload_tool_result(self, session_id: str,
                                      tool_result: ToolResultBlock) -> str: ...

**注意契约 §3.10 漏了 ``async``**：它把三个方法写成了同步签名。
真实 API 是 ``async def``（上面就是原文），而 agent 侧是真的在 ``await``
（``third_party/agentscope/src/agentscope/agent/_agent.py:711`` 的
``await self.offloader.offload_context(self.state.session_id, msgs=...)``、
``:858`` / ``:2080`` 的 ``await ...offload_data_block(block)``、
``:2662`` 的 ``await ...offload_tool_result(...)``）。
按契约第 ⓬ 条"以真实 API 为准"，这里实现成异步方法，
偏离记在交付说明的 ``unresolved`` 里。

**为什么返回值不能"省"**

``offload_context`` / ``offload_tool_result`` 返回的字符串会被原样塞进
system-reminder 交给模型（``_agent.py:715-719`` 与 ``:2670-2676``）：

.. code-block:: text

    <system-reminder>The compressed context is offloaded to '<path>',
    you can refer to it when needed.</system-reminder>

所以**绝不允许**在小体积时"假装 offload 了"再返回一个不存在的路径 ——
模型会去读一个空文件。本模块因此**永远真的落盘**，
只在大到不合理时"拒绝落盘"并明确回报（见 :attr:`max_offload_bytes`）。

**这个类到底比工作区自带的实现多做什么**

工作区自己就实现了这三件事（``workspace/_base.py:1004 / :1060 / :1119``），
而且已经很完整：``context.jsonl`` 追加、``tool_result-<id>.txt`` 带 ``(1)`` 去重、
``data/<sha256>.<ext>`` 按内容哈希短路、写出去的是可移植的 ``workspace:///`` URL。
所以本类**一个字都不重写**，只做四件工作区不该管的事：

1. **空工作区降级**：``workspace=None`` 时不假装成功，明确回报"未 offload"；
2. **磁盘护栏**：单次 offload 超过 :attr:`max_offload_bytes`（默认 64 MiB）
   直接拒绝 —— 一次上下文压缩就往工作区灌 2 GB 是真实的部署事故，
   而工作区自身没有任何上限；
3. **可观测量**：``stats()`` 给出"压缩了几次、省了多少字节、拒绝了几次"；
4. **策略透传说明**：写盘走工作区的 backend，所以
   :class:`~harness_kit.sandbox.local.PolicyBackend` 的路径策略**自动生效**
   （``PolicyLocalWorkspace`` / ``QuotaDockerWorkspace`` 的 backend 都是策略化的），
   本类不需要、也不应该再判一次路径。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentscope.message import DataBlock, Msg, ToolResultBlock
from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from agentscope.workspace import WorkspaceBase

__all__ = [
    "DEFAULT_MAX_OFFLOAD_BYTES",
    "HarnessOffloader",
]

DEFAULT_MAX_OFFLOAD_BYTES = 64 * 1024 * 1024
"""单次 offload 的字节上限（64 MiB）。

取值理由：一次上下文压缩把整段历史写进 ``context.jsonl``，
一次工具输出截断把剩余内容写进 ``tool_result-*.txt``。
64 MiB 已经远大于任何"值得让模型再去读"的内容，
再大只说明上游该先做真正的分页而不是往磁盘倒。
"""


class HarnessOffloader:
    """``Offloader`` 协议的实现（契约 §3.10）。

    组合而非继承：工作区已经实现了这三件事，本类只做包装
    （契约把它写成 ``class HarnessOffloader(Offloader)``，
    那只是"实现该协议"的意思 —— ``Offloader`` 是 ``Protocol``，
    显式继承它并不会带来任何行为，反而会让人以为这里重写了落盘逻辑）。

    Example:
        >>> offloader = HarnessOffloader(workspace=ws)   # doctest: +SKIP
        >>> path = await offloader.offload_context(      # doctest: +SKIP
        ...     "s-1", msgs=[msg])
    """

    def __init__(
        self,
        *,
        workspace: "WorkspaceBase | None" = None,
        max_offload_bytes: int = DEFAULT_MAX_OFFLOAD_BYTES,
    ) -> None:
        """构造 offloader。

        Args:
            workspace (`WorkspaceBase | None`, optional): 真正干活的落盘目标。
                ``None`` 表示"没有工作区可用"，此时三个方法都会
                明确降级（见各自 docstring），而不是抛异常把 agent 循环打断 ——
                一个没有工作区的 agent 仍然应该能聊天，只是不能 offload。
            max_offload_bytes (`int`, defaults to 64 MiB): 单次 offload 上限。

        Raises:
            ValueError: ``max_offload_bytes`` 不为正。
        """
        if max_offload_bytes <= 0:
            raise ValueError(
                f"max_offload_bytes 必须为正，得到 {max_offload_bytes!r}",
            )
        self.workspace = workspace
        self.max_offload_bytes = int(max_offload_bytes)
        self._stats: dict[str, int] = {
            "data_block_offloaded": 0,
            "context_offloaded": 0,
            "tool_result_offloaded": 0,
            "bytes_written": 0,
            "skipped_no_workspace": 0,
            "refused_too_large": 0,
        }
        if workspace is None:
            logger.warning(
                "HarnessOffloader 没有 workspace：offload 将降级为"
                "“不落盘”，返回的引用句柄会明确标注未 offload，"
                "模型读到该标注后不会去打开一个不存在的文件",
            )

    # ------------------------------------------------------------------
    # 协议方法（签名与 _offload_protocol.py 逐字一致）
    # ------------------------------------------------------------------
    async def offload_data_block(self, block: DataBlock) -> DataBlock:
        """把 base64 数据块落盘，返回带可移植 URL 的数据块。

        体积判定看的是 **base64 文本长度**（``len(block.source.data)``）而不是
        解码后字节数 —— 这正好是内存里实际占的空间，
        而护栏要防的正是"内存 + 磁盘双份"。

        Args:
            block (`DataBlock`): 待 offload 的数据块。
                已经是 :class:`~agentscope.message.URLSource` 的原样返回
                （与协议语义一致）。

        Returns:
            `DataBlock`: 落盘后的数据块；无法 offload 时**原样返回** ——
            把 base64 留在消息里，总比给一个指向空气的 URL 强。
        """
        if self.workspace is None:
            self._stats["skipped_no_workspace"] += 1
            return block
        size = _block_payload_size(block)
        if size is not None and size > self.max_offload_bytes:
            self._stats["refused_too_large"] += 1
            logger.error(
                "拒绝 offload 数据块 {}：base64 载荷 {} 字节 > 上限 {} 字节；"
                "数据块保持内联（不会写成半截文件）",
                block.id,
                size,
                self.max_offload_bytes,
            )
            return block
        saved = await self.workspace.offload_data_block(block)
        self._stats["data_block_offloaded"] += 1
        self._stats["bytes_written"] += size or 0
        logger.debug("数据块 {} 已 offload（{} 字节）", block.id, size)
        return saved

    async def offload_context(self, session_id: str, msgs: list[Msg]) -> str:
        """把压缩后的上下文追加到工作区，返回可寻址的引用。

        Args:
            session_id (`str`): 会话 id（决定落到哪个会话子目录）。
            msgs (`list[Msg]`): 被压缩掉的消息。**参数名必须是** ``msgs`` ——
                agent 侧就是按关键字传的（``_agent.py:711``）。

        Returns:
            `str`: 落盘路径；没有工作区时返回一句**明确标注未 offload** 的说明。
        """
        if self.workspace is None:
            self._stats["skipped_no_workspace"] += 1
            return "<offload-skipped: no workspace available>"
        size = _messages_size(msgs)
        if size > self.max_offload_bytes:
            self._stats["refused_too_large"] += 1
            logger.error(
                "拒绝 offload 上下文（session={}）：{} 条消息共 {} 字节 > 上限 {}；"
                "返回未 offload 的标注，模型不会去读不存在的文件",
                session_id,
                len(msgs),
                size,
                self.max_offload_bytes,
            )
            return f"<offload-refused: {size} bytes exceeds limit>"
        path = await self.workspace.offload_context(session_id, msgs=msgs)
        self._stats["context_offloaded"] += 1
        self._stats["bytes_written"] += size
        logger.debug(
            "上下文已 offload（session={}，{} 条消息，{} 字节）-> {}",
            session_id,
            len(msgs),
            size,
            path,
        )
        return path

    async def offload_tool_result(
        self,
        session_id: str,
        tool_result: ToolResultBlock,
    ) -> str:
        """把被截断的工具输出落盘，返回可寻址的引用。

        Args:
            session_id (`str`): 会话 id。
            tool_result (`ToolResultBlock`): 被截断掉的那部分结果。

        Returns:
            `str`: 落盘路径；没有工作区或超上限时返回明确标注的说明。
        """
        if self.workspace is None:
            self._stats["skipped_no_workspace"] += 1
            return "<offload-skipped: no workspace available>"
        size = _tool_result_size(tool_result)
        if size > self.max_offload_bytes:
            self._stats["refused_too_large"] += 1
            logger.error(
                "拒绝 offload 工具结果 {}（session={}）：{} 字节 > 上限 {}；"
                "返回未 offload 的标注",
                tool_result.id,
                session_id,
                size,
                self.max_offload_bytes,
            )
            return f"<offload-refused: {size} bytes exceeds limit>"
        path = await self.workspace.offload_tool_result(session_id, tool_result)
        self._stats["tool_result_offloaded"] += 1
        self._stats["bytes_written"] += size
        logger.debug(
            "工具结果 {} 已 offload（session={}，{} 字节）-> {}",
            tool_result.id,
            session_id,
            size,
            path,
        )
        return path

    # ------------------------------------------------------------------
    # 可观测量
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        """返回累计计数（只读快照）。

        Returns:
            `dict[str, int]`: 各类计数。``bytes_written`` 是**估算**的
            载荷字节（按序列化前的内容算），不是磁盘实际增量。
        """
        return dict(self._stats)

    def describe(self) -> dict[str, Any]:
        """自述（日志 / doctor 用）。

        Returns:
            `dict[str, Any]`: 工作区类名、上限、计数。
        """
        return {
            "offloader": type(self).__name__,
            "workspace": type(self.workspace).__name__
            if self.workspace is not None
            else None,
            "max_offload_bytes": self.max_offload_bytes,
            "stats": self.stats(),
        }

    def __repr__(self) -> str:
        """调试表示。

        Returns:
            `str`: 形如 ``HarnessOffloader(workspace=PolicyLocalWorkspace)``。
        """
        target = type(self.workspace).__name__ if self.workspace else "None"
        return f"HarnessOffloader(workspace={target})"


def _block_payload_size(block: DataBlock) -> int | None:
    """数据块里 base64 文本的长度；不是 base64 源则返回 ``None``。

    Args:
        block (`DataBlock`): 数据块。

    Returns:
        `int | None`: 载荷长度，或 ``None``（URL 源 / 未知源）。
    """
    source = block.source
    data = getattr(source, "data", None)
    return len(data) if isinstance(data, str) else None


def _messages_size(msgs: list[Msg]) -> int:
    """一组消息的近似序列化体积。

    用 :meth:`pydantic.BaseModel.model_dump_json` 的长度量：
    它正是要写进 ``context.jsonl`` 的内容，比"字段求和"更接近真实磁盘占用。

    Args:
        msgs (`list[Msg]`): 消息列表。

    Returns:
        `int`: 字节数。
    """
    total = 0
    for msg in msgs:
        try:
            total += len(msg.model_dump_json().encode("utf-8"))
        except Exception:  # noqa: BLE001 - 量体积失败不该阻断 offload
            continue
    return total


def _tool_result_size(tool_result: ToolResultBlock) -> int:
    """工具结果的近似序列化体积。

    Args:
        tool_result (`ToolResultBlock`): 工具结果块。

    Returns:
        `int`: 字节数。
    """
    try:
        return len(tool_result.model_dump_json().encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0
