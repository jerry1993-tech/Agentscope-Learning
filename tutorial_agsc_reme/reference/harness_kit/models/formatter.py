# -*- coding: utf-8 -*-
"""``HarnessOpenAICompatFormatter`` —— 吸收「OpenAI 协议之外」的 provider 差异。

**它存在的理由**：AgentScope 的 ``OpenAIChatFormatter``
（``third_party/agentscope/src/agentscope/formatter/_openai_formatter.py:229``）
产出的消息体是**按 api.openai.com 的口味**调好的。翻它的源码能看到三处
「OpenAI 特化」：

1. 每条消息都带 ``"name": msg.name``（``:293``、``:346``、``:406``）——
   这是 OpenAI 用来区分多 agent 的字段；而 AgentScope 自己的 DeepSeek
   formatter 是**故意不带** ``name`` 的（对比
   ``.../formatter/_deepseek_formatter.py``，它只发 ``role`` 和
   ``content``）。很多兼容端点（vLLM / 自建网关）对 ``name`` 有正则约束
   （``^[a-zA-Z0-9_-]+$``），中文名或带空格的 agent 名会直接 400。
2. ``"content": content_blocks or None``（``:295``）—— 纯工具调用的 assistant
   消息会得到 ``content: null``。OpenAI 接受，部分兼容实现不接受。
3. ``ThinkingBlock`` **被静默丢弃**（``:391-394`` 的注释写着
   "skip thinking blocks silently"）。丢得对（DeepSeek 明确禁止把
   ``reasoning_content`` 回灌进历史），但**一句日志都不打**，排查
   「模型为什么忘了自己的思考」时会很痛苦。

本 formatter 不改结构、只做**后处理**：先让父类按 OpenAI 规则排出消息体，
再按其配置把上面三处差一点一点抹平，并**把每一次改动记进计数器**
（:attr:`NormalizeStats`），让「formatter 到底改了什么」可见、可断言。
这正是「适配器/格式化器」该有的样子 —— 差异显式化，而不是靠注释。

用法：

.. code-block:: python

    formatter = HarnessOpenAICompatFormatter(
        drop_message_name=True,          # 自建端点多半需要
        null_content_as_empty=True,
        thinking_as_text=False,
    )
    # 注意：formatter 要挂在**模型**上，不是挂在 Agent 上 ——
    # AgentScope 的 Agent.__init__ 没有 formatter 参数，它读的是
    # self.model.formatter（third_party/agentscope/src/agentscope/
    # agent/_agent.py:2066）。
    model = OpenAICompatChatModel(..., formatter=formatter)
    agent = Agent(name="a", model=model, ...)
    ...
    print(formatter.stats.snapshot())    # 看看它到底改了几处
"""

from __future__ import annotations

from typing import Any, ClassVar

from loguru import logger
from pydantic import ConfigDict, Field, PrivateAttr

from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg

__all__ = ["HarnessOpenAICompatFormatter", "NormalizeStats"]

_TEXTUAL_ROLES: frozenset[str] = frozenset({"user", "system", "tool"})
"""这些角色的 ``content`` 在 OpenAI 协议里**必须**是字符串或 parts 数组，
不允许为 ``null``。assistant 因为有 ``tool_calls`` 兜底，可以留 ``null``。"""


class NormalizeStats(dict):
    """计数器（``dict`` 子类，便于直接打印 / ``json.dumps``）。

    统计的是**本 formatter 主动做出的修正次数**，不是 token 数：

    ============================ ==========================================
    键                            含义
    ============================ ==========================================
    ``calls``                     ``format()`` 被调用的次数
    ``messages``                  产出的消息总数（累计）
    ``dropped_name``              被摘掉 ``name`` 的消息数
    ``null_content_filled``       ``content: null`` 被补成 ``""`` 的次数
    ``thinking_dropped``          被丢弃的 thinking 文本块数
    ``thinking_inlined``          thinking 被改写进 text 的次数
    ``empty_messages_removed``    归一化后变空、被整条删掉的消息数
    ============================ ==========================================

    Returns:
        `NormalizeStats`: 计数器。
    """

    _KEYS: ClassVar[tuple[str, ...]] = (
        "calls",
        "messages",
        "dropped_name",
        "null_content_filled",
        "thinking_dropped",
        "thinking_inlined",
        "empty_messages_removed",
    )
    """全部计数键。**在 :meth:`__init__` 里先全部置 0**，而不是等第一次
    ``bump`` 才出现 —— 否则 ``self.stats["messages"] += n`` 这种写法会在
    空字典上直接 ``KeyError``（本模块第一版就踩了这个坑，验收脚本抓到的）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """构造时把所有计数键置 0。

        Args:
            *args (`Any`): 透传给 ``dict``。
            **kwargs (`Any`): 透传给 ``dict``。
        """
        super().__init__({key: 0 for key in self._KEYS})
        if args or kwargs:
            self.update(*args, **kwargs)

    def snapshot(self) -> dict[str, int]:
        """返回一份普通 dict 快照。

        Returns:
            `dict[str, int]`: 键值副本。
        """
        return dict(self)

    def bump(self, key: str, amount: int = 1) -> None:
        """加一笔。

        Args:
            key (`str`): 计数键。
            amount (`int`): 增量，默认 ``1``。
        """
        self[key] = int(self.get(key, 0)) + amount


class HarnessOpenAICompatFormatter(OpenAIChatFormatter):
    """把 OpenAI 格式的消息体再按目标 provider 归一化。

    Args:
        drop_message_name (`bool`): 是否摘掉所有 ``name`` 字段。默认 ``True``
            —— 面向自建端点 / DeepSeek 这类「只认 role + content」的服务。
            接官方 OpenAI 且用了多 agent 时请设为 ``False``。
        null_content_as_empty (`bool`): 是否把 **user / system / tool** 角色
            的 ``content: null`` 补成空串。默认 ``True``。assistant 的
            ``null`` 保持不变（它通常紧接着 ``tool_calls``，OpenAI 要求那里
            就是 ``null``）。
        thinking_as_text (`bool`): 是否把 ``ThinkingBlock`` 的内容拼进同一条
            消息的文本里。默认 ``False``（丢弃，与父类一致 —— DeepSeek 禁止
            回灌 ``reasoning_content``）。设为 ``True`` 只适用于你确定目标
            端点接受这种做法的场景。
        keep_empty_assistant (`bool`): 归一化后既无 ``content`` 又无
            ``tool_calls`` 的 assistant 消息是否保留。默认 ``False``（删掉，
            避免某些端点 400）。
        input_types (`list[str] | None`): 见父类；``None`` 用父类默认值。

    Raises:
        ValueError: ``thinking_as_text`` 为真但 ``drop_message_name`` 为假时
            不冲突，因此本类不抛；保留此段仅为说明**没有**额外约束。
    """

    model_config = ConfigDict(extra="forbid")

    drop_message_name: bool = Field(
        default=True,
        description="摘掉 OpenAI 消息体里的 ``name`` 字段。",
    )
    null_content_as_empty: bool = Field(
        default=True,
        description="把非 assistant 角色的 ``content: null`` 补成空串。",
    )
    thinking_as_text: bool = Field(
        default=False,
        description="把 ThinkingBlock 拼进文本而不是丢弃。",
    )
    keep_empty_assistant: bool = Field(
        default=False,
        description="保留归一化后为空的 assistant 消息。",
    )

    _stats: NormalizeStats = PrivateAttr(default_factory=NormalizeStats)
    """归一化计数器。

    用 ``PrivateAttr`` 而不是普通 pydantic 字段是**被逼的**：
    ``FormatterBase`` 是 pydantic 模型，字段类型要能生成 core schema，而
    ``NormalizeStats`` 是个 ``dict`` 子类 —— pydantic v2 会直接拒绝
    （``PydanticSchemaGenerationError: Unable to generate pydantic-core
    schema for <class 'NormalizeStats'>``）。``PrivateAttr`` 不做校验，
    正好够用。
    """

    _STAT_KEYS: ClassVar[tuple[str, ...]] = NormalizeStats._KEYS  # noqa: SLF001
    """全部计数键（与 :class:`NormalizeStats` 共用同一份定义，避免两处漂移）。"""

    @property
    def stats(self) -> NormalizeStats:
        """归一化计数器（只读视图，可 ``.snapshot()`` 成普通 dict）。

        Returns:
            `NormalizeStats`: 计数器。
        """
        return self._stats

    def reset_stats(self) -> None:
        """清零计数器（长期复用的 formatter 实例在测试里会用到）。"""
        self._stats.clear()
        self._stats.update({key: 0 for key in self._STAT_KEYS})

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        """排消息 → 归一化 → 返回。

        Args:
            msgs (`list[Msg]`): 输入消息。

        Returns:
            `list[dict[str, Any]]`: 可直接作为 ``messages`` 发给
            ``/chat/completions`` 的列表。

        Raises:
            TypeError: ``msgs`` 不是 ``list[Msg]``（由父类的断言抛出）。
        """
        self.assert_list_of_msgs(msgs)

        raw = await super().format(msgs)

        thinking_texts = self._collect_thinking(msgs)
        if thinking_texts:
            if self.thinking_as_text:
                self._inline_thinking(raw, thinking_texts)
            else:
                # 父类 `:391-394` 是**静默**丢弃。这里虽然行为一致，但至少
                # 把它**记下来** —— 「模型为什么忘了自己的思考」是排查成本
                # 极高的一类问题，一个计数器就能省下半小时。
                self._stats.bump("thinking_dropped", len(thinking_texts))

        normalized: list[dict[str, Any]] = []
        for message in raw:
            self._normalize_one(message)
            if self._is_empty(message) and not self.keep_empty_assistant:
                self._stats.bump("empty_messages_removed")
                continue
            normalized.append(message)

        self._stats.bump("calls")
        self._stats.bump("messages", len(normalized))
        return normalized

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_thinking(msgs: list[Msg]) -> list[str]:
        """收集所有 ``ThinkingBlock`` 的文本。

        Args:
            msgs (`list[Msg]`): 输入消息。

        Returns:
            `list[str]`: 非空的思考文本，保持原始顺序。
        """
        texts: list[str] = []
        for message in msgs:
            for block in message.get_content_blocks("thinking"):
                text = getattr(block, "thinking", "") or ""
                if text:
                    texts.append(text)
        return texts

    def _inline_thinking(
        self,
        raw: list[dict[str, Any]],
        thinking_texts: list[str],
    ) -> None:
        """把思考文本附到**最后一条** assistant 消息上。

        为什么是「最后一条 assistant」：``format`` 产出的是扁平列表，
        思考块与消息的一一对应关系在父类的转换中已经丢了。把思考挂到最近
        一条 assistant 上，是信息损失最小、且**确定性**的选择
        （同一份历史永远得到同一份输出，prompt cache 不会被搅乱）。

        Args:
            raw (`list[dict[str, Any]]`): 父类产出的消息体（原地修改）。
            thinking_texts (`list[str]`): 思考文本。
        """
        target = None
        for message in reversed(raw):
            if message.get("role") == "assistant":
                target = message
                break
        if target is None:
            return

        joined = "\n".join(thinking_texts)
        content = target.get("content")
        if isinstance(content, str) and content:
            target["content"] = f"{joined}\n{content}"
        elif content is None or content == "":
            target["content"] = joined
        else:
            # content 是 parts 数组：插到最前面，保持「先想后说」的顺序
            target["content"] = [{"type": "text", "text": joined}, *content]
        self._stats.bump("thinking_inlined")

    def _normalize_one(self, message: dict[str, Any]) -> None:
        """对单条消息做归一化（原地修改）。

        Args:
            message (`dict[str, Any]`): 一条 OpenAI 格式消息。
        """
        role = message.get("role")

        if self.drop_message_name and "name" in message:
            message.pop("name", None)
            self._stats.bump("dropped_name")

        if (
            self.null_content_as_empty
            and role in _TEXTUAL_ROLES
            and message.get("content") is None
        ):
            message["content"] = ""
            self._stats.bump("null_content_filled")

        if role == "tool" and not message.get("tool_call_id"):
            # 这是**错误**而不是差异：缺 tool_call_id 的 tool 消息一定会被
            # provider 拒绝，且拒绝原因（400 invalid request）很难定位。
            # 主动打一条显式日志，比让 provider 报错强。
            logger.error(
                "formatter 收到一条没有 tool_call_id 的 tool 消息，"
                "目标端点一定会拒绝：{}",
                {k: v for k, v in message.items() if k != "content"},
            )

    @staticmethod
    def _is_empty(message: dict[str, Any]) -> bool:
        """判断归一化后的消息是否已经没有任何内容。

        Args:
            message (`dict[str, Any]`): 一条 OpenAI 格式消息。

        Returns:
            `bool`: 无 ``content`` 也无 ``tool_calls`` 时为 ``True``。
        """
        content = message.get("content")
        has_content = bool(content) if content is not None else False
        return not has_content and not message.get("tool_calls")

    def describe(self) -> str:
        """一行摘要。

        Returns:
            `str`: 形如 ``harness-openai-compat(drop_name=True, ...)``。
        """
        return (
            "harness-openai-compat("
            f"drop_name={self.drop_message_name}, "
            f"null_to_empty={self.null_content_as_empty}, "
            f"thinking_as_text={self.thinking_as_text}, "
            f"keep_empty_assistant={self.keep_empty_assistant})"
        )
