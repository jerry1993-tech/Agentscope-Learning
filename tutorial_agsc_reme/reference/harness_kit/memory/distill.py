# -*- coding: utf-8 -*-
"""把 AgentScope 会话蒸馏成 ReMe 记忆条目（契约 §3.18）。

**这一层只做"输入整形"，语义全部在 ``auto_memory`` job 里**

``AutoMemoryStep``（``third_party/ReMe/reme/steps/evolve/auto_memory.py:332-486``）
已经是一条完整的流水线：查当天笔记 → 决定 create / update 分支 →
用 ``auto_memory.yaml`` 的模板驱动一个内部 AgentScope agent →
**创建分支再查一次**（re-query，确认模型真的把笔记写下来了）→
刷新当天索引 → 写回 ``metadata``。本模块**不复刻其中任何一步**，
只负责把 AgentScope 的消息喂成它要的形状，再把它的 ``metadata`` 翻译成
:class:`DistillResult`。

**三个必须知道的坑（都来自源码，不是猜测）**

1. ``_sanitize_msg_for_save``（``auto_memory.py:24-38``）会**丢弃两类块**：
   ``tool_result`` 与 ``source.type == "base64"`` 的 ``data`` 块。
   源码注释写明了理由：工具结果里常常包含被召回的 memory / search / read 输出，
   留着它们会让"检索到的事实"在下一轮被当成"用户说过的上下文"。
   所以**不要指望工具输出能被写进记忆**；需要落记忆的内容必须出现在
   user / assistant 的文本块里。
2. ``Msg(content="纯文本")`` 会 ``ValidationError``（AgentScope 2.0.8 的 ``content``
   必须是块列表）。:meth:`SessionDistiller.shape_messages` 因此对
   "dict + str content" 这种从 JSON 反序列化来的形状做了兜底包装。
3. ``auto_memory`` **不是**"给什么记什么"：create 分支的提示词要求模型自己判断
   值不值得记。返回 ``path=None`` 且 ``created=False`` 是**正常结果**，
   含义是"这一轮没有值得落盘的长期事实"，不是失败。

**关于 ``catalog`` 参数（契约未定义语义，此处给出 harness 的解释）**

契约的签名里有 ``catalog: str = "mem_session"``，但 ``auto_memory_step`` 的
运行时可配参数只有 ``messages`` / ``session_id`` / ``memory_hint`` /
``include_images`` / ``date`` —— **没有 catalog**。所以 harness 把它解释为
"把蒸馏产物登记进哪个 ``file_catalog``"：写完之后调
:meth:`~harness_kit.memory.catalog.CatalogManager.register` 取一个轻量台账条目。
这样做的实际价值是：``resource_watch_loop`` 那一类"按 catalog 增量扫描"的流程
可以只看这一个 catalog，而不必扫全工作区。默认值 ``"mem_session"`` 与
``ReMeWorkspace.mem_session_dir`` 同名，但**它们不是同一个东西**
（一个是目录名、一个是 catalog 名），catalog 不存在时会被
:meth:`~harness_kit.memory.catalog.CatalogManager.ensure_catalog` 现造一个。
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from loguru import logger
from pydantic import BaseModel, ConfigDict

from .client import MemoryClient

__all__ = [
    "AUTO_MEMORY_JOB",
    "DistillResult",
    "SessionDistiller",
]

#: ReMe 的会话蒸馏 job 名（``third_party/ReMe/reme/config/default.yaml`` 的
#: ``jobs.auto_memory``，两个 step：``auto_memory_step`` + ``auto_tag_step``）。
AUTO_MEMORY_JOB: str = "auto_memory"

#: 官方 ``ReMeMiddleware`` 注入记忆时用的保留消息名
#: （``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_middleware.py:71``）。
#: 整形时必须把它剔掉：那是**检索产物**，不是对话内容。
MEMORY_HINT_NAME: str = "memory"


class DistillResult(BaseModel):
    """一次会话蒸馏的结果（契约 §3.18）。

    Attributes:
        path (`str | None`): 落盘的记忆卡路径（工作区相对路径）。
            ``None`` 表示这一轮没有产生笔记 —— 三种情况都会走到这里：
            消息为空、模型判断不值得记、或者笔记创建后 re-query 没找到。
        created (`bool`): 是否**新建**了笔记（``False`` = 更新已有笔记，
            或者根本没有笔记）。
        content (`str`): 笔记的**当前全文**（从磁盘读的，不是模型的自述文本）。
            路径缺失时退化成 ``auto_memory`` 的 ``answer``。
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    created: bool = False
    content: str = ""


class SessionDistiller:
    """会话 → 记忆卡的蒸馏器（契约 §3.18）。

    Example::

        distiller = SessionDistiller(client, workspace=ws)
        result = await distiller.distill(agent.state.context, session_id="s-1")
        print(result.created, result.path)
        print(result.content[:200])
    """

    def __init__(self, client: MemoryClient, *, workspace: Any) -> None:
        """绑定客户端与工作区。

        Args:
            client (`MemoryClient`): 已 start 的客户端。
            workspace (`Any`): :class:`~harness_kit.memory.workspace.ReMeWorkspace`；
                用来把 ReMe 给出的工作区相对路径解析成真实文件路径。
        """
        self.client = client
        self.workspace = workspace

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def distill(
        self,
        msgs: list[Any],
        *,
        session_id: str,
        catalog: str = "mem_session",
        memory_hint: str | None = None,
        date: str | None = None,
    ) -> DistillResult:
        """把一段会话蒸馏成记忆（契约 §3.18）。

        Args:
            msgs (`list[Any]`): AgentScope 的 ``Msg`` 列表（也接受
                ``Msg.model_dump()`` 出来的 dict）。顺序必须是时间顺序。
            session_id (`str`): 会话 id。ReMe 用它给记忆卡打
                ``source_conversation`` 链接并决定"新建还是更新"
                （``auto_memory.py:367-370`` 用 ``validate_session_id`` 校验，
                空值直接 ``success=False``）。harness 在这里先挡一道，
                好让错误信息说清是谁的问题。
            catalog (`str`): 产物登记进哪个 ``file_catalog``；
                ``""`` 表示不登记。语义见模块 docstring。
            memory_hint (`str | None`): 透传给 ``auto_memory`` 的额外提示
                （``memory_hint``），用于告诉蒸馏模型"这轮该重点看什么"。
            date (`str | None`): ``YYYY-MM-DD``；``None`` 时 ReMe 从消息时间戳
                推断，推不出来就用今天（``auto_memory.py:371-380``）。

        Returns:
            `DistillResult`: 蒸馏结果。

        Raises:
            `ValueError`: ``session_id`` 为空，或消息形状无法整形。
            `MemoryJobError`: ReMe 报 ``success=False``
                （例如日期格式非法、session_id 非法、内部 agent 抛异常）。
            `MemoryUnavailableError`: app 未启动或没有 ``auto_memory`` job。

        注意：**契约里的参数顺序是 ``(msgs, *, session_id, catalog)``，
        本实现完全一致**；``memory_hint`` 与 ``date`` 是 harness 追加的
        可选关键字参数（超集，不影响契约调用方）。
        """
        caller = str(session_id or "").strip()
        if not caller:
            raise ValueError(
                "distill 需要非空 session_id：ReMe 的 auto_memory 用它命名 "
                "daily/<date>/<session>.md 并写 source_conversation 链接，"
                "空值会被 AutoMemoryStep 直接判成失败（auto_memory.py:367-370）。",
            )

        shaped = self.shape_messages(msgs)
        payload: dict[str, Any] = {"messages": shaped, "session_id": caller}
        if memory_hint:
            payload["memory_hint"] = str(memory_hint)
        if date:
            payload["date"] = str(date)

        logger.info(
            "distill: session={!r} 输入消息 {} 条（整形后 {} 条），catalog={!r}",
            caller,
            len(msgs or []),
            len(shaped),
            catalog,
        )
        response = await self.client.run_job(AUTO_MEMORY_JOB, **payload)
        metadata = dict(getattr(response, "metadata", None) or {})

        path = metadata.get("path")
        relative = str(path) if path else None
        created = bool(metadata.get("created", False))
        content = await self._read_note(relative)
        if not content:
            content = str(getattr(response, "answer", "") or "")

        if relative and catalog:
            await self._register(relative, catalog)

        logger.info(
            "distill: session={!r} created={} path={} content={} 字符",
            caller,
            created,
            relative,
            len(content),
        )
        return DistillResult(path=relative, created=created, content=content)

    # ------------------------------------------------------------------
    # 输入整形
    # ------------------------------------------------------------------
    @classmethod
    def shape_messages(cls, msgs: Iterable[Any] | None) -> list[dict[str, Any]]:
        """把消息整形成 ``auto_memory`` 能吃的 JSON 形状。

        做四件事，每一步都对应一个真实的坑：

        1. **丢弃注入的记忆提示**：``name == "memory"`` 的消息是
           :class:`~harness_kit.memory.middleware.LongTermMemoryMiddleware`
           注入的检索产物。它是**已经存在于记忆库里的内容**，
           写回去只会让同一件事在库里自我复制（并污染 ``source_conversation``）。
        2. **丢弃空消息**：没有文本也没有块的占位消息对蒸馏没有信息量，
           但它们会让 ``n_messages`` 虚高。
        3. **把 dict 的 str content 包成块列表**：``Msg(content="x")`` 在
           AgentScope 2.0.8 会 ``ValidationError``，而从 JSON 反序列化来的
           ``{"content": "x"}`` 恰好长这样。
        4. **``model_dump(mode="json")``**：与官方 ``ReMeMiddleware._write_back``
           （``_middleware.py:515``）逐字一致，保证两个写入路径产出同一种数据。

        Args:
            msgs (`Iterable[Any] | None`): ``Msg`` 或 dict 的可迭代对象。

        Returns:
            `list[dict[str, Any]]`: 可直接作为 ``messages`` 参数传下去的列表。

        Raises:
            `ValueError`: 元素既不是 ``Msg`` 也不是 dict。
        """
        from agentscope.message import Msg

        out: list[dict[str, Any]] = []
        for item in msgs or ():
            msg = item
            if isinstance(item, dict):
                data = dict(item)
                content = data.get("content")
                if isinstance(content, str):
                    data["content"] = [{"type": "text", "text": content}]
                try:
                    msg = Msg.model_validate(data)
                except Exception as exc:  # noqa: BLE001 - 统一成 ValueError 并带上原文
                    raise ValueError(
                        f"消息 dict 无法解析成 Msg: {exc}；原始键={sorted(data)}",
                    ) from exc
            elif not isinstance(item, Msg):
                raise ValueError(
                    f"shape_messages 只接受 Msg 或 dict，收到 {type(item).__name__}。"
                    "从会话存储里读出来的往往是 dict，但至少要能 model_validate 成 Msg。",
                )

            if getattr(msg, "name", None) == MEMORY_HINT_NAME:
                logger.debug("distill: 丢弃注入的记忆提示消息 id={}", getattr(msg, "id", ""))
                continue
            if not _has_payload(msg):
                logger.debug("distill: 丢弃空消息 id={}", getattr(msg, "id", ""))
                continue
            out.append(msg.model_dump(mode="json"))
        return out

    @staticmethod
    def messages_from_agent(agent: Any, *, since_id: str | None = None) -> list[Any]:
        """从 AgentScope ``Agent`` 上取会话消息（省得调用方自己翻 state）。

        Args:
            agent (`Any`): AgentScope 的 ``Agent``（只要有 ``state.context``）。
            since_id (`str | None`): 只取这条消息 id **之后**的消息。
                ``None`` = 全取。用于"每轮只蒸馏增量"的场景。

        Returns:
            `list[Any]`: ``Msg`` 列表（保持原顺序）。

        Raises:
            `ValueError`: ``agent`` 上没有 ``state.context``。
        """
        from agentscope.message import Msg

        context = getattr(getattr(agent, "state", None), "context", None)
        if context is None:
            raise ValueError(
                f"{type(agent).__name__} 上没有 state.context；"
                "messages_from_agent 需要 AgentScope 2.0.8 的 Agent。",
            )
        messages = [msg for msg in context if isinstance(msg, Msg)]
        if since_id is None:
            return messages
        seen = False
        tail: list[Any] = []
        for msg in messages:
            if seen:
                tail.append(msg)
            elif getattr(msg, "id", None) == since_id:
                seen = True
        return tail

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _read_note(self, relative: str | None) -> str:
        """读回笔记全文。

        Args:
            relative (`str | None`): 工作区相对路径。

        Returns:
            `str`: 文件内容；路径为空或读不到时返回空串。
        """
        if not relative:
            return ""
        try:
            target = self.workspace.resolve_relative(relative)
        except Exception as exc:  # noqa: BLE001 - 路径越界等一律退化成读不到
            logger.debug("distill: 无法解析 {}: {}", relative, exc)
            return ""
        try:
            return await asyncio.to_thread(target.read_text, "utf-8")
        except OSError as exc:
            logger.debug("distill: 读取 {} 失败: {}", target, exc)
            return ""

    async def _register(self, relative: str, catalog: str) -> None:
        """把产物登记进 catalog（失败只记 warning）。

        登记失败**不该**让蒸馏结果作废：笔记已经落盘了，
        catalog 只是一份台账。把它降级成 warning 是有意的取舍，
        并且 warning 里给出路径，便于人工补登。

        Args:
            relative (`str`): 工作区相对路径。
            catalog (`str`): catalog 名。
        """
        from .catalog import CatalogManager

        try:
            await CatalogManager(self.client).register(relative, catalog=catalog)
        except Exception as exc:  # noqa: BLE001 - 台账失败不影响蒸馏本身
            logger.warning("distill: 把 {} 登记进 catalog {!r} 失败: {}", relative, catalog, exc)


def _has_payload(msg: Any) -> bool:
    """判断一条消息有没有值得蒸馏的内容。

    判据比"``content`` 列表非空"严一档：**只有空文本块**的消息（``"   "`` 或
    ``""``）也算没有内容。理由与 :meth:`SessionDistiller.shape_messages` 里
    「丢弃空消息」那条相同 —— 它们对蒸馏没有任何信息量，却会让
    ``metadata["n_messages"]`` 虚高。反过来，任何**非文本块**
    （图片 / 思考 / 工具调用）一律算有内容，哪怕它的文本是空的：
    那些块的存在本身就是信息。

    Args:
        msg (`Any`): ``Msg``。

    Returns:
        `bool`: 有非空文本、或至少有一个非文本块时为 ``True``。
    """
    try:
        text = msg.get_text_content()
    except Exception:  # noqa: BLE001 - 形状异常的 Msg 一律当空
        text = None
    if text and str(text).strip():
        return True
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return False
    for block in content:
        if str(getattr(block, "text", "") or "").strip():
            return True
        if getattr(block, "type", None) not in (None, "text"):
            return True
    return False
