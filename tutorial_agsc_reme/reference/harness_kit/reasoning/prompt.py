# -*- coding: utf-8 -*-
"""system prompt 装配：为 KV Cache 而设计（契约 §3.14，第 14 讲）。

**这一模块存在的唯一理由，是一个很多人不知道的成本事实**：绝大多数 LLM API
的 prompt cache 是**前缀匹配**的 —— 只要前缀字节完全一致，那一段就按缓存价
计费（DeepSeek 的 cache hit 与 cache miss 差一个数量级；Anthropic 的
cache write/read 也是两套价）。反过来，**改动前缀里的任何一个字节，从那一字节
往后（包括所有工具 schema）全部按未命中重算**。

于是「往 system prompt 里塞点实时状态」这种看起来人畜无害的操作，代价是
**每一轮都把整个上下文按全价重算一遍**。本模块把这件事变成结构约束：

1. **固定段落顺序**：``角色 → 约束 → 工具 → 技能 → 记忆 → 动态``
   （:data:`SECTION_ORDER`）。顺序固定 = 前缀稳定；调用方给的顺序不影响渲染
   结果，只影响同一序号内的先后。
2. **动态内容一律不进 system prompt**，改走 :meth:`PromptAssembler.hint` 造的
   ``HintBlock``（``third_party/agentscope/src/agentscope/message/_block.py:101``）
   经 :meth:`PromptAssembler.inject` 落到消息流**末尾** —— 它被转换成一条
   **user 消息**，所以前缀一个字节都不变。这是 AgentScope 给的「注入动态内容的
   正确姿势」（它自己注入运行时状态就是这么做的，``.../agent/_agent.py:1636``）。
   注意**不能**用 ``agent.observe``：那条路会校验入参并拒绝 ``HintBlock``。
3. **跨 reply 字节级稳定，而且是断言出来的**：:meth:`PromptAssembler.render`
   默认 ``strict=True``，会拿每个稳定段落的正文与首次渲染时的基线比对，
   一旦不一致就抛 :class:`VolatileSectionError` —— **把「cache 失效」这个
   静默的性能问题变成一次响亮的报错**。:meth:`PromptAssembler.compute_fingerprint`
   给外部一个短哈希，用来在自己的日志里断言「这一轮的 system prompt 没变」。

**为什么指纹用 ``hashlib`` 而不是内置 ``hash()``**：``hash()`` 对 str 是按进程
加盐的（``PYTHONHASHSEED``），跨进程 / 跨重启结果不同，拿它当缓存键会在
「重启后所有缓存突然全失效」这种地方坑人。``sha256`` 的 16 位十六进制是稳定的。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Sequence

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentscope.message import HintBlock

__all__ = [
    "PROMPT_VERSION",
    "SECTION_ORDER",
    "PromptAssembler",
    "PromptSection",
    "VolatileSectionError",
    "render_tool_list",
]

PROMPT_VERSION: str = "harness-kit-prompt/1"
"""前缀格式版本。**改渲染格式时必须改它** —— 否则指纹不变而实际字节变了，
外部基于指纹的缓存判断会失效。它参与指纹计算。"""

SECTION_ORDER: tuple[str, ...] = (
    "role",
    "constraints",
    "tools",
    "skills",
    "memory",
    "dynamic",
)
"""段落渲染顺序（契约 §3.14 指定）。

**顺序本身就是设计**，不是随便排的：

- ``role`` / ``constraints`` 放最前：它们最稳定，且最该被模型优先读到；
- ``tools`` / ``skills`` 紧随其后：变化频率低（工具集在一轮任务里通常不变），
  但**一旦变就必须在稳定区之后**，免得把前面的缓存全冲掉；
- ``memory`` 排在稳定区**最后**：长期记忆是最常变的一块（每轮可能注入新条目），
  放在稳定区末尾意味着它的变化只影响它自己之后的内容；
- ``dynamic`` 垫底：**只有在「整个会话内不会变」时它才配待在 system prompt 里**
  （例如本会话的工作目录、用户 ID）。每次 reply 都变的东西（当前时间、
  剩余预算、最新检索结果）一律走 :meth:`PromptAssembler.hint` ——
  那才是它们该待的地方。
"""


class VolatileSectionError(ValueError):
    """稳定段落的内容在两次渲染之间变了，或者段落配置本身自相矛盾。

    它在生产里意味着**prompt cache 正在静默失效**：每次调用的前缀都不同，
    账单按全价走，而输出看不出任何异常。所以本异常刻意做得很难被忽略。
    """


class PromptSection(BaseModel):
    """system prompt 的一个段落（契约 §3.14）。

    Attributes:
        key (`str`): 段落标识，决定渲染顺序（见 :data:`SECTION_ORDER`）。
            会做归一化：小写、空格与连字符转下划线。
        title (`str`): 渲染成 ``## {title}`` 的标题。
        body (`str`): 正文。**渲染时逐字使用，不做 strip / 不补空行** ——
            任何「顺手美化一下」都会让指纹随调用方无心的小改而变。
        volatile (`bool`): 为真时**不进入** :meth:`PromptAssembler.render`，
            改由 :meth:`PromptAssembler.hints` 当 ``HintBlock`` 注入。
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    body: str
    volatile: bool = False

    @field_validator("key")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        """归一化 key。

        Args:
            value (`str`): 原始 key。

        Returns:
            `str`: 归一化后的 key。

        Raises:
            ValueError: key 为空。
        """
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not normalized:
            raise ValueError("PromptSection.key 不能为空。")
        return normalized

    @field_validator("title")
    @classmethod
    def _require_title(cls, value: str) -> str:
        """标题不能为空（没有标题的段落会渲染成裸正文，破坏结构）。

        Args:
            value (`str`): 原始标题。

        Returns:
            `str`: 去空白后的标题。

        Raises:
            ValueError: 标题为空。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "PromptSection.title 不能为空：段落标题是前缀结构的一部分，"
                "空标题会让两个不同的段落渲染出无法区分的文本。",
            )
        return stripped

    def render(self) -> str:
        """渲染成一个段落。

        Returns:
            `str`: ``"## {title}\\n{body}"``（正文为空时省略换行）。
        """
        if not self.body:
            return f"## {self.title}"
        return f"## {self.title}\n{self.body}"

    @property
    def order_index(self) -> int:
        """在 :data:`SECTION_ORDER` 里的位置（未知 key 排最后）。

        Returns:
            `int`: 顺序下标。
        """
        try:
            return SECTION_ORDER.index(self.key)
        except ValueError:
            return len(SECTION_ORDER)


def render_tool_list(schemas: Iterable[dict[str, Any]]) -> str:
    """把工具 schema 列表渲染成稳定文本（``tools`` 段落用）。

    **必须排序**：``toolkit.get_tool_schemas()`` 的返回顺序取决于注册顺序与
    激活的组，而注册顺序在「按配置动态注册」时可能每次进程启动都不同 ——
    不排序的话，前缀会在重启后整体位移，缓存全废。
    （AgentScope 的 ``Toolkit`` 自己也是排序输出的，见
    ``.../tool/_toolkit.py`` 的 ``get_tool_schemas``；这里再排一次是因为
    调用方可能会把多个来源的 schema 拼在一起。）

    Args:
        schemas (`Iterable[dict[str, Any]]`): ``{"type": "function",
            "function": {...}}`` 形态的 schema。

    Returns:
        `str`: 每行一个工具的文本，形如
        ``"- read_file(path: str, limit: int?): Read a file."``；
        空列表返回一句占位说明（**不返回空串**：空字符串会让「没有工具」和
        「忘了渲染」长得一模一样）。
    """
    lines: list[str] = []
    for schema in schemas:
        func = schema.get("function", {}) if isinstance(schema, dict) else {}
        name = func.get("name", "?")
        params = func.get("parameters") or {}
        properties = params.get("properties") or {}
        required = set(params.get("required") or [])
        args = ", ".join(
            f"{key}: {value.get('type', 'any')}{'' if key in required else '?'}"
            for key, value in properties.items()
        )
        description = (func.get("description") or "").strip().splitlines()
        first_line = description[0] if description else ""
        lines.append(f"- {name}({args}): {first_line}".rstrip())
    if not lines:
        return "（本回合没有可用工具。）"
    lines.sort()
    return "\n".join(lines)


class PromptAssembler:
    """system prompt 装配器（契约 §3.14）。

    Args:
        sections (`list[PromptSection]`): 段落列表。顺序随意 —— 渲染时按
            :data:`SECTION_ORDER` 重排。

    Raises:
        ValueError: key 重复（重复会让「哪一段生效」变得不可预测）。
    """

    def __init__(self, *, sections: list[PromptSection]) -> None:
        """初始化并校验段落。"""
        self._sections: dict[str, PromptSection] = {}
        for section in sections:
            if section.key in self._sections:
                raise ValueError(
                    f"段落 key 重复：{section.key!r}。"
                    "重复的段落会让渲染结果取决于「哪一个先被加进来」，"
                    "而不是取决于设计。请先合并它们。",
                )
            self._sections[section.key] = section
        self._baseline: dict[str, str] = {}
        self._renders: int = 0
        self._drift: list[str] = []
        logger.debug(
            "PromptAssembler: {} 个段落（稳定 {} / volatile {}）",
            len(self._sections),
            len(self.stable_keys),
            len(self.volatile_keys),
        )

    # ==================================================================
    # 段落管理
    # ==================================================================
    @property
    def sections(self) -> list[PromptSection]:
        """全部段落，按渲染顺序（稳定在前，volatile 在后）。

        Returns:
            `list[PromptSection]`: 段落列表（是副本，改它不影响装配器）。
        """
        ordered = sorted(
            self._sections.values(),
            key=lambda s: (s.volatile, s.order_index),
        )
        return list(ordered)

    @property
    def stable_keys(self) -> list[str]:
        """稳定段落的 key，按渲染顺序。

        Returns:
            `list[str]`: key 列表。
        """
        return [
            _.key for _ in self.sections if not _.volatile
        ]

    @property
    def volatile_keys(self) -> list[str]:
        """volatile 段落的 key。

        Returns:
            `list[str]`: key 列表。
        """
        return [_.key for _ in self.sections if _.volatile]

    def section(self, key: str) -> PromptSection:
        """取一个段落。

        Args:
            key (`str`): 段落 key。

        Returns:
            `PromptSection`: 段落。

        Raises:
            KeyError: 没有这个 key。
        """
        try:
            return self._sections[key]
        except KeyError as exc:
            raise KeyError(
                f"没有段落 {key!r}；现有 {sorted(self._sections)}。",
            ) from exc

    def add(self, section: PromptSection) -> None:
        """加一个段落。

        Args:
            section (`PromptSection`): 新段落。

        Raises:
            ValueError: key 已存在（**不静默覆盖**：覆盖会悄悄改掉前缀）。
        """
        if section.key in self._sections:
            raise ValueError(
                f"段落 {section.key!r} 已存在；要改内容请先 drop() 再 add()，"
                "或者改 section.body 后调用 rebaseline() 明确接受前缀变化。",
            )
        self._sections[section.key] = section

    def drop(self, key: str) -> bool:
        """删一个段落。

        Args:
            key (`str`): 段落 key。

        Returns:
            `bool`: 真删掉了为 ``True``。
        """
        if key not in self._sections:
            return False
        del self._sections[key]
        self._baseline.pop(key, None)
        return True

    def rebaseline(self) -> None:
        """把当前正文接受为新的基线（明确放弃现有的 prompt cache）。

        用途：**会话生命周期结束、或上下文被压缩之后**（第 12 讲的
        ``compress_context``），前缀变化是预期内的，此时应当明确重新开始，
        而不是让 :meth:`render` 一直报错。
        """
        self._baseline = {
            section.key: section.body for section in self.sections if not section.volatile
        }
        logger.info(
            "PromptAssembler.rebaseline: 已把 {} 个稳定段落接受为新基线"
            "（从这一刻起 prompt cache 需重新建立）。",
            len(self._baseline),
        )

    # ==================================================================
    # 渲染
    # ==================================================================
    def render(self, *, strict: bool = True) -> str:
        """渲染 system prompt（**只有稳定段落**）。

        Args:
            strict (`bool`, defaults to `True`): 为真时，稳定段落的正文若与
                首次渲染的基线不一致就抛 :class:`VolatileSectionError`。

        Returns:
            `str`: 以空行分隔的段落文本。

        Raises:
            VolatileSectionError: 稳定段落的内容漂移了（``strict=True``）。
        """
        self._renders += 1
        stable = [section for section in self.sections if not section.volatile]
        self._check_drift(stable, strict=strict)
        text = self._compose(stable)
        if strict:
            for section in stable:
                self._baseline.setdefault(section.key, section.body)
        return text

    def _compose(
        self,
        stable: Sequence[PromptSection] | None = None,
    ) -> str:
        """把稳定段落拼成文本（**纯函数，不改任何状态**）。

        :meth:`render` 与 :meth:`compute_fingerprint` 共用它，所以指纹一定是
        「此刻 render 会产出的字节」，不会因为多调了一次指纹而漂移。

        Args:
            stable (`Sequence[PromptSection] | None`, optional): 稳定段落；
                ``None`` 时现算。

        Returns:
            `str`: 段落文本。
        """
        if stable is None:
            stable = [
                section for section in self.sections if not section.volatile
            ]
        return "\n\n".join(section.render() for section in stable)

    def _check_drift(
        self,
        stable: Sequence[PromptSection],
        *,
        strict: bool,
    ) -> None:
        """比对稳定段落与基线。

        Args:
            stable (`Sequence[PromptSection]`): 稳定段落。
            strict (`bool`): 是否抛错。

        Raises:
            VolatileSectionError: ``strict`` 且检测到漂移。
        """
        changed: list[str] = []
        for section in stable:
            before = self._baseline.get(section.key)
            if before is not None and before != section.body:
                changed.append(section.key)
        if not changed:
            return

        self._drift.extend(_ for _ in changed if _ not in self._drift)
        message = (
            f"稳定段落 {changed} 的正文与首次渲染时不一致。"
            "system prompt 是 prompt cache 的前缀：它一变，"
            "从该段落往后（含所有工具 schema）的输入 token 全部按未命中重算。"
            "要注入每次都变的内容，请把该段落标 volatile=True，用 hint() 注入"
            "（它会被转成消息流末尾的 user 消息，前缀不受影响）；"
            "确实是会话边界导致的合理变化，请显式调用 rebaseline()。"
        )
        if strict:
            raise VolatileSectionError(message)
        logger.warning("PromptAssembler: {}（已按 strict=False 重新基线化）", message)

    def compute_fingerprint(self) -> str:
        """前缀指纹（跨进程稳定）。

        **它不改变任何状态**：不记录基线、不递增 ``renders``、不记漂移，
        可以在渲染前后随便调用。这一点是刻意的 —— 外部要写
        「这一轮的指纹 == 上一轮的指纹」这种断言，就不该让**断言本身**
        改变被断言的对象（否则断言的副作用会自己把基线建起来，
        严格模式从此永不报警）。

        Returns:
            `str`: ``sha256(PROMPT_VERSION + "\\x00" + 稳定文本)[:16]``。
        """
        payload = f"{PROMPT_VERSION}\x00{self._compose()}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    # ==================================================================
    # 动态注入
    # ==================================================================
    def hint(self, text: str, *, source: str | None = None) -> HintBlock:
        """造一个注入用的 ``HintBlock``。

        ``HintBlock``（``.../message/_block.py:101``）在发给模型时会被转成一条
        **user 消息**，追加在消息流的**末尾**。这就是「动态内容不进 system
        prompt」的落点：末尾追加不改变任何前缀字节，因此缓存照旧命中。
        AgentScope 自己也用这条路注入运行时状态
        （``.../agent/_agent.py:1622`` 造块、``:1636`` 落库），注释原文就是
        「We attach a HintBlock instead of mutating the system prompt, so that
        prompt caching still works」。

        **怎么把它交出去 —— 这是最容易踩空的一步**：
        ``agent.observe([block])`` **不行**。``observe`` 会走
        ``_handle_incoming_messages``（``:2057``）做入参校验，而那里的规则是
        「消息必须是 role 为 user/assistant 的 ``Msg``，且不得包含 tool calls /
        tool results / thinking blocks」—— ``HintBlock`` 直接被拒，报
        ``ValueError: Invalid message in the input: ('type', 'hint')``。
        真实可用的三条路：

        1. :meth:`PromptAssembler.inject`（本模块的封装，内部用第 2 条）；
        2. ``agent.state.append_context(agent.name, [block])`` ——
           AgentScope 内部的注入点（``.../state/_state.py:298``），
           ReMe 的长期记忆中间件也是这么干的
           （``.../middleware/_longterm_memory/_reme/_middleware.py:410``）；
        3. ``await agent.compress_context(instructions=block)`` ——
           只在压缩上下文时用（``.../agent/_agent.py:389``）。

        Args:
            text (`str`): 注入内容。
            source (`str | None`, optional): 来源标记（团队消息 / 系统通知用）。

        Returns:
            `HintBlock`: 交给上面三条路之一的块。

        Raises:
            ValueError: ``text`` 为空（空的 hint 会变成一条空 user 消息，
                白白占 token 还会干扰模型）。
        """
        if not text or not text.strip():
            raise ValueError(
                "hint 的 text 不能为空：它会变成一条空的 user 消息，"
                "既浪费 token 又会让模型困惑。",
            )
        return HintBlock(hint=text, source=source)

    def inject(self, agent: Any, *, text: str | None = None) -> int:
        """把 volatile 段落（或一段临时文本）注入到 Agent 的**消息流末尾**。

        **本方法是 prompt.py 里唯一碰 Agent 的地方，而且用的是鸭子类型**
        （只要求 ``agent`` 有 ``name`` 和 ``state.append_context``）——
        这样本模块不必 import ``agentscope.agent``，单测装配器时就不需要起模型。

        一次调用注入所有 volatile 段落，加一段 ``text``（如果有）。
        **只影响消息流，一个字都不碰 system prompt**，所以
        :meth:`compute_fingerprint` 的返回值在注入前后完全相同 ——
        这正是「缓存友好」的可断言形态。

        Args:
            agent (`Any`): ``Agent`` 实例（鸭子类型）。
            text (`str | None`, optional): 额外注入的一段文本。

        Returns:
            `int`: 实际注入的块数。

        Raises:
            ValueError: ``text`` 给了但是空的。
            AttributeError: ``agent`` 不具备 ``state.append_context``
                （说明拿错了对象 —— 静默跳过才是真正危险的，那会让
                「记忆注入失败了」在生产里完全隐形）。
        """
        blocks = self.hints()
        if text is not None:
            blocks.append(self.hint(text, source="harness_kit.reasoning.prompt"))
        if not blocks:
            return 0
        append_context = getattr(getattr(agent, "state", None), "append_context", None)
        if append_context is None:
            raise AttributeError(
                f"{type(agent).__name__} 没有 state.append_context："
                "本方法只接受 AgentScope 的 Agent（或其等价物）。"
                "HintBlock 必须落到消息流里，塞进 system prompt 会让 "
                "prompt cache 失效，那正是本模块要防的事。",
            )
        append_context(agent.name, blocks)
        logger.debug("PromptAssembler.inject: 向 {} 注入 {} 个 HintBlock", agent.name, len(blocks))
        return len(blocks)

    def hints(self) -> list[HintBlock]:
        """把 volatile 段落全部转成 ``HintBlock``。

        Returns:
            `list[HintBlock]`: 每个 volatile 段落一个块（标题 + 正文）。
            没有 volatile 段落时返回空列表。
        """
        out: list[HintBlock] = []
        for section in self.sections:
            if not section.volatile:
                continue
            out.append(
                self.hint(
                    f"[{section.title}]\n{section.body}" if section.body
                    else f"[{section.title}]",
                    source="harness_kit.reasoning.prompt",
                ),
            )
        return out

    def dynamic_text(self) -> str:
        """volatile 段落的文本形态（日志 / 审计用）。

        Returns:
            `str`: 与 :meth:`hints` 同源，但拼成一段文本。
        """
        return "\n\n".join(
            section.render() for section in self.sections if section.volatile
        )

    # ==================================================================
    # 观测
    # ==================================================================
    @property
    def renders(self) -> int:
        """``render`` 被调用的次数。

        Returns:
            `int`: 次数。
        """
        return self._renders

    @property
    def drift(self) -> list[str]:
        """历史上有过内容漂移的稳定段落 key。

        Returns:
            `list[str]`: key 列表。它非空但 ``renders`` 很大，说明这个
            prompt 的前缀一直在抖 —— 哪怕调用方用 ``strict=False`` 压住了报错，
            成本问题依然存在。
        """
        return list(self._drift)

    def describe(self) -> str:
        """多行人读描述。

        Returns:
            `str`: 段落顺序、volatile 归属、指纹与漂移记录。
        """
        lines = [
            f"PromptAssembler(renders={self._renders}, "
            f"fingerprint={self.compute_fingerprint()})",
            f"  order: {' -> '.join(SECTION_ORDER)}",
        ]
        for section in self.sections:
            flag = "volatile" if section.volatile else "stable"
            lines.append(
                f"  [{flag}] {section.key:<12} title={section.title!r} "
                f"{len(section.body)} chars",
            )
        if self._drift:
            lines.append(f"  drift: {self._drift}")
        return "\n".join(lines)
