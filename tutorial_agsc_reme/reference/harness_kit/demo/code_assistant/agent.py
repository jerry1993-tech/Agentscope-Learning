# -*- coding: utf-8 -*-
"""代码助手 Demo 的装配层：Profile → HarnessBuilder → 一个能答代码问题的 Agent。

**这个 Demo 想证明的一件事**：一个"能查代码库并给出出处"的助手，不需要
新写任何 Agent Loop / 检索算法 —— 它由三个已有件拼成：

=========================================== ================================================
这一层用到的                              出处
=========================================== ================================================
``Agent``（Agent Loop / Tool Use / context） AgentScope 原生，``third_party/agentscope/src/agentscope/agent/_agent.py:117``
ReMe 嵌入式记忆（索引 / 检索 / 写回）        ``harness_kit/memory/client.py``（第 15 讲）包着 ``reme.ReMe``
治理（Profile / 权限 / 中间件 / 预算）       ``harness_kit/config/builder.py``（第 2 讲）
=========================================== ================================================

**为什么要在这里改 Profile 的两个字段**

Demo 复用 ``harness_kit/profiles/researcher_with_memory.yaml``（它已经有
``reme_memory`` 中间件、强制引用的 sys_prompt、``explore`` 只读权限）。但两处
必须按 Demo 的场景改写，否则会踩到真实的坑：

1. **``memory.workspace_root`` 与 ``middleware[reme_memory].params.workspace_dir``
   必须指向 Demo 自己的索引目录**。默认值 ``./.harness/reme/research`` 是研究
   助手的语料；如果 Demo 也往那里写，两个场景的记忆会混在一起 —— 检索出别人的
   资料是"看起来能用、结果不可信"的典型。
   这两个值必须**一致**：写入侧（:mod:`harness_kit.memory.ingest`）用
   ``memory.workspace_root``，检索侧（``LongTermMemoryMiddleware``）用
   ``workspace_dir``。不一致时检索会静默返回 0 条。

2. **工具白名单收窄成只读的 ``Read`` / ``Grep`` / ``Glob``**。Profile 里
   ``tools.packs: [builtin]`` 会把 ``Bash`` / ``Edit`` / ``Write`` 也带上；
   Demo 是"读代码回答问题"，带上写工具只会让模型在无人值守时尝试改文件
   （``explore`` 权限会拒绝它，但那一轮就白跑了）。收窄是**行为约束**，
   权限是**兜底**，两层都要有。

**一个必须知道的真实约束：``Grep`` / ``Glob`` / ``Read`` 的"根"是进程 cwd，不是 Profile 的 workspace。**

AgentScope 的这三个工具都只收一个 ``backend``，没有 ``cwd`` 参数；相对路径由
``await self._backend.getcwd()`` 补全（``third_party/agentscope/src/agentscope/tool/_builtin/_grep.py:222``、
``:242``），而 ``LocalBackend.getcwd()`` 就是 ``os.getcwd()``
（``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:902``）。
``harness_kit/tools/builtin_pack.py:375`` 只把 ``Bash`` 钉在 ``workdir`` 上，
其余五个工具拿的是同一个 backend —— 所以换 Profile 的 ``workspace.root``
**并不会**改变搜索根。

结论：**Demo 的入口（:mod:`harness_kit.demo.code_assistant.main`）在跑之前会
``os.chdir(代码库根)``**，理由与做法都写在那个函数里。程序化调用
:func:`build_code_assistant` 时如果希望模型的工具能看到真实代码，请自行把
进程 cwd 切到代码库根（库函数里不做 chdir —— 那会污染调用方的进程状态）。

**验证方式的诚实说明**：Demo 回答里的引用（形如 ``[1]``）是模型按 sys_prompt
写出来的，不是我们拼接的。所以 :meth:`CodeAssistant.verify_citations` 做的是
**交叉核对**：把回答里的 ``[n]`` 与"本次检索真正命中的文件"比对，报告是否
"引用了检索到的来源"。它证明的是"回答有据可查"，不是"每个字都正确"。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from harness_kit.config.builder import BuiltHarness, HarnessBuilder
    from harness_kit.config.schema import ResolvedProfile
    from harness_kit.settings import Settings

__all__ = [
    "AGENT_NAME",
    "CODE_SYS_PROMPT",
    "DEFAULT_ALIAS",
    "DEFAULT_INDEX_DIR",
    "DEFAULT_QUESTION",
    "DEFAULT_TAGS",
    "DEFAULT_TARGET",
    "MEMORY_MIDDLEWARE",
    "CitationCheck",
    "CodeAssistant",
    "build_code_assistant",
    "citation_markers",
    "demo_profile",
    "reference_root",
    "source_candidates",
]

#: Demo 使用的 Profile（相对 ``harness_kit/profiles``）。
BASE_PROFILE: str = "researcher_with_memory"

#: 记忆中间件的注册名（``harness_kit/registry.py`` 的 ``_register_middlewares``）。
MEMORY_MIDDLEWARE: str = "reme_memory"

#: Demo 的索引目录，相对 reference 目录。
DEFAULT_INDEX_DIR: str = "./.harness/reme/code_assistant"

#: 默认要索引的代码库（相对仓库根，也就是 ``third_party/agentscope`` 的一个子树）。
DEFAULT_TARGET: str = "third_party/agentscope/src/agentscope/agent"

#: 写进工作区 ``resource/<alias>/`` 的那层目录名。
#:
#: **``main.py`` 与 ``ingest_repo`` CLI 必须共用这一个值**：``alias`` 决定
#: 渲染产物落在工作区的哪个命名空间里，而检索命中的是**整个工作区**。两边用了
#: 不同的 alias，同一个工作区里就会堆出两套 ``resource/`` 命名空间，检索时
#: 两批一起返回 —— 实测现象是命中里一半是 ``resource/agent/...``、一半是
#: ``resource/agentscope/...``，同名文件互相冒充（见 README 的"已知上限"）。
#: 所以 CLI 的 ``--alias`` 默认值也钉在这里，不再回退到 target 的目录名。
DEFAULT_ALIAS: str = "agentscope"

#: 写进渲染产物 front matter 的默认标签。
#:
#: 同样必须**两边共用**：标签是渲染进 markdown 正文的，改标签 = 改内容 sha256
#: = 入库判定为「新增」。实测踩过：`main.py` 默认带 `["code","agentscope"]`，
#: 而 CLI 默认不带标签，两者交替跑会让同一批文件永远显示"新增 6"，
#: 幂等性看起来失效（其实只是内容变了）。
DEFAULT_TAGS: tuple[str, ...] = ("code", "agentscope")

#: Demo 的默认问题：答案确实写在 ``agent/_agent.py`` 的 docstring 里。
DEFAULT_QUESTION: str = (
    "AgentScope 的 Agent.reply_stream 里有个 yield_final_msg 参数，"
    "它控制什么行为？默认值是什么？请给出出处（文件路径与行号）。"
)

#: Demo Agent 的名字（覆盖 Profile 里的 ``research-agent``）。
AGENT_NAME: str = "code-assistant"

#: Demo 的 sys_prompt：在 Profile 那份"强制引用"的基础上，补上"只读代码"的边界。
CODE_SYS_PROMPT: str = (
    "你是代码助手，负责回答关于这个代码库的问题。"
    "回答必须基于检索到的资料或你实际读到的文件："
    "每个结论后用 [1] [2] 这样的编号标注来源，并在末尾用「来源」列表写出"
    "编号对应的 文件路径:行号。"
    "找不到出处就写「无来源」，不要凭记忆编造 API 名字或行号。"
    "你没有写权限：只读代码，不要尝试修改任何文件。"
)

#: Demo 只暴露的只读工具（``harness_kit/tools/builtin_pack.py`` 里的工具名）。
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

#: 正文里的引用标记，与 ``harness_kit/eval/metrics.py`` 的 ``CitationMarkerPattern`` 同形。
_CITATION_RE: re.Pattern[str] = re.compile(r"\[(\d+)\]")


def reference_root() -> Path:
    """定位 ``tutorial_agsc_reme/reference``（Profile 相对路径的锚点）。

    查找顺序：``HARNESS_REPO_ROOT`` 环境变量 → 从本文件向上找第一个含
    ``harness_kit/profiles`` 的目录。**不写死 ``parents[3]``**：Demo 会被复制
    到别处跑（见 ``Dockerfile``），层级一变硬编码就失效。

    Returns:
        `Path`: reference 目录的绝对路径。

    Raises:
        `RuntimeError`: 向上找不到 ``harness_kit/profiles``。
    """
    override = os.environ.get("HARNESS_REPO_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "harness_kit" / "profiles").is_dir():
            return parent
    raise RuntimeError(
        "找不到 harness_kit/profiles：请设置 HARNESS_REPO_ROOT 指向 "
        "tutorial_agsc_reme/reference",
    )


def repo_root() -> Path:
    """定位仓库根（``third_party/agentscope`` 所在的那一层）。

    从 reference 目录向上找含 ``third_party`` 的目录。

    Returns:
        `Path`: 仓库根绝对路径。

    Raises:
        `RuntimeError`: 找不到 ``third_party``。
    """
    for parent in (reference_root(), *reference_root().parents):
        if (parent / "third_party").is_dir():
            return parent
    raise RuntimeError(f"在 {reference_root()} 及其上层找不到 third_party 目录")


def default_settings(**overrides: Any) -> "Settings":
    """构造锚定到 reference 目录的 :class:`Settings`。

    Args:
        **overrides (`Any`): 透传给 ``Settings.from_env`` 的覆盖项。

    Returns:
        `Settings`: 设置对象。
    """
    from harness_kit.settings import Settings

    extra = dict(overrides)
    extra.setdefault("repo_root", reference_root())
    return Settings.from_env(**extra)


def demo_profile(
    *,
    index_root: str | Path,
    settings: "Settings | None",
    profile_name: str = BASE_PROFILE,
    question_prompt: str = CODE_SYS_PROMPT,
) -> "ResolvedProfile":
    """在 ``researcher_with_memory`` 之上改出 Demo 的 Profile。

    **只改四处**，其余（模型、温度、max_iters、权限模式、规则文件、中间件顺序）
    原样继承 —— 这正是 Profile 该有的用法：Demo 不是"另一套配置"，而是
    "同一套治理下的一次场景化覆盖"。

    Args:
        index_root (`str | Path`): Demo 的记忆工作区（检索与写入必须同一个）。
        settings (`Settings | None`): 设置对象（用于解析 Profile 目录）。
        profile_name (`str`): 基础 Profile 名。
        question_prompt (`str`): 覆盖 sys_prompt。

    Returns:
        `ResolvedProfile`: 冻结后的 Profile（``model_copy`` 产出，原对象不动）。

    Raises:
        `ValueError`: 基础 Profile 里没有 ``reme_memory`` 中间件 —— 那意味着
            Demo 检索不到任何东西，必须在装配前就报错，而不是"回答时才发现"。
    """
    from harness_kit.config.loader import load_resolved_profile
    from harness_kit.config.schema import MiddlewareSpec

    settings = settings or default_settings()
    search_dir = Path(settings.resolve(settings.profile_dir))
    profile = load_resolved_profile(profile_name, search_dir=search_dir)

    root = Path(index_root)
    if not root.is_absolute():
        root = settings.resolve(root)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    memory = profile.memory.model_copy(
        update={"enabled": True, "workspace_root": str(root), "catalog": "code"},
    )

    found = False
    middleware: list[Any] = []
    for item in profile.middleware:
        if item.name != MEMORY_MIDDLEWARE:
            middleware.append(item)
            continue
        found = True
        params = dict(item.params)
        params["workspace_dir"] = str(root)
        middleware.append(MiddlewareSpec(name=item.name, params=params))
    if not found:
        raise ValueError(
            f"Profile {profile_name!r} 的 middleware 里没有 {MEMORY_MIDDLEWARE}；"
            "没有它，Agent 不会检索记忆，Demo 永远答不出带出处的答案。"
            f"请在 harness_kit/profiles/{profile_name}.yaml 里补上该中间件。",
        )

    agent = profile.agent.model_copy(
        update={"name": AGENT_NAME, "sys_prompt": question_prompt},
    )
    return profile.model_copy(
        update={"memory": memory, "middleware": middleware, "agent": agent},
    )


def citation_markers(text: str) -> list[int]:
    """抽出正文里的引用编号（``[1] [2]`` → ``[1, 2]``，去重且保序）。

    Args:
        text (`str`): 待扫描文本。

    Returns:
        `list[int]`: 出现过的编号。
    """
    seen: dict[int, None] = {}
    for match in _CITATION_RE.finditer(text or ""):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)


def source_candidates(source: str) -> list[str]:
    """列出一个命中路径"可能被回答点名的几种写法"。

    为什么要这一步：ReMe 命中的是**工作区里的渲染产物**，形如
    ``resource/agentscope/_agent.py.md``（见 :mod:`ingest_repo` 的命名约定），
    而模型回答里写出来的出处**多半是真实源码路径**
    （``third_party/agentscope/src/agentscope/agent/_agent.py:324``）——
    它可能刚用 ``Read`` 读过原文件，也可能只是照着记忆里的路径写。
    只比对完整工作区路径会把这种**正确**的回答判成"没引用"。

    Args:
        source (`str`): 命中来源（ReMe 工作区相对路径）。

    Returns:
        `list[str]`: 由强到弱的候选写法：完整路径 → 文件名 → 去掉渲染
        后缀（``.md``）的文件名。
    """
    candidates = [source]
    name = Path(source).name
    if name not in candidates:
        candidates.append(name)
    stem = Path(name)
    while stem.suffix == ".md":  # `x.py.md` → `x.py`；`README.md` → `README`
        stem = stem.with_suffix("")
    if stem.name and stem.name not in candidates:
        candidates.append(stem.name)
    return candidates


class CitationCheck(BaseModel):
    """一次"引用是否有据"的交叉核对结果。"""

    model_config = ConfigDict(extra="forbid")

    markers: list[int] = Field(default_factory=list)
    """回答里出现的引用编号。"""

    sources: list[str] = Field(default_factory=list)
    """**本次检索真正命中**的来源路径（工作区相对路径）。"""

    grounded: list[str] = Field(default_factory=list)
    """回答里被点名、且在 :attr:`sources` 里能找到的来源。"""

    orphan_markers: list[int] = Field(default_factory=list)
    """写了编号但回答里没有出现任何来源路径的编号（可能是空引用）。"""

    @property
    def ok(self) -> bool:
        """是否有引用、且至少一条能与检索命中对上。

        Returns:
            `bool`: 是否通过核对。
        """
        return bool(self.markers) and bool(self.grounded)

    def summary(self) -> str:
        """一行结论（CLI / 日志用）。

        Returns:
            `str`: 人类可读的核对结论。
        """
        if not self.markers:
            return "未通过：回答里没有任何 [n] 引用标记"
        if not self.grounded:
            return (
                f"未通过：有引用编号 {self.markers}，但回答里没写出任何"
                f"检索命中的来源路径（命中 {len(self.sources)} 条）"
            )
        extra = f"，另有 {len(self.orphan_markers)} 个编号未落到来源路径" if self.orphan_markers else ""
        return f"通过：{len(self.grounded)} 条来源与检索命中一致{extra}"


@dataclass
class CodeAssistant:
    """装好的代码助手（装配产物 + 生命周期 + 两个便利方法）。

    Attributes:
        settings (`Settings`): 设置对象。
        profile (`ResolvedProfile`): 实际生效的 Profile。
        builder (`HarnessBuilder`): 装配器（``aclose`` 时要关它）。
        built (`BuiltHarness`): 装配产物（``agent`` / ``memory`` / ``middlewares``）。
        index_root (`Path`): 记忆工作区（写入侧与检索侧共用）。
    """

    settings: "Settings"
    profile: "ResolvedProfile"
    builder: "HarnessBuilder"
    built: "BuiltHarness"
    index_root: Path
    last_sources: list[str] = field(default_factory=list)

    async def ask(self, question: str, *, stream: bool = False) -> str:
        """问一个问题，返回回答文本。

        Args:
            question (`str`): 用户问题。
            stream (`bool`): ``True`` 时逐字打印到 stdout。

        Returns:
            `str`: 回答文本。
        """
        from agentscope.message import Msg, TextBlock

        message = Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text=question)],
        )
        if stream:
            chunks: list[str] = []
            async for chunk in self.built.agent.reply_stream(message):
                delta = getattr(chunk, "delta", None)
                if delta:
                    chunks.append(str(delta))
                    print(str(delta), end="", flush=True)
            print()
            return "".join(chunks)

        reply = await self.built.agent.reply(message)
        getter = getattr(reply, "get_text_content", None)
        text = getter() if callable(getter) else None
        return str(text) if text else str(reply)

    async def recall(self, query: str, *, limit: int = 5) -> list[str]:
        """直接调检索（不经过模型），用于把"检索到什么"与"模型写了什么"分开看。

        这是排查"模型没引用"时**第一步该跑的东西**：如果这里就 0 条命中，
        问题在索引/写入侧（文件没 ingest、工作区不对），不在模型。

        Args:
            query (`str`): 查询串。
            limit (`int`): 条数上限。

        Returns:
            `list[str]`: 命中的来源路径（工作区相对路径），按分数降序。
        """
        from harness_kit.memory.client import MemoryClient

        builder = getattr(self.built, "memory", None)
        if builder is None or not isinstance(builder, MemoryClient):
            logger.warning("Demo 没有装出 MemoryClient，跳过 recall")
            return []

        from harness_kit.memory.search import MemorySearch
        from harness_kit.memory.workspace import ReMeWorkspace

        search = MemorySearch(
            builder,
            workspace=ReMeWorkspace(root=self.index_root),
        )
        result = await search.search(query, limit=limit, min_score=0.0)
        self.last_sources = [hit.path for hit in result.hits]
        for hit in result.hits:
            logger.debug("recall: score={:.4f} {}:{}", hit.score, hit.path, hit.start_line)
        return list(self.last_sources)

    def tool_calls(self) -> list[str]:
        """列出本次会话里模型实际调用过的工具名（按出现顺序，含重复）。

        **为什么要看这个**：Demo 里"回答有据"可以有两条证据来源 ——
        一条是 ReMe 中间件把检索结果注入 context，另一条是模型自己用
        ``Read`` / ``Grep`` 去读原文件。只看最终回答分不出是哪条，
        把工具调用列出来就一目了然（例如 ``['Grep', 'Read']``，
        或记忆中间件挂出来的 ``['memory_search', 'Read']``）。

        读的是 AgentScope ``AgentState.context``
        （``third_party/agentscope/src/agentscope/state/_state.py:220``）
        里各条消息的 ``ToolCallBlock``
        （``third_party/agentscope/src/agentscope/message/_block.py:138``）
        —— 只读，不改动任何状态。

        注意：记忆中间件挂出来的 ``memory_search`` **不在 Toolkit 里**，
        它是每次 reply 时由中间件的 ``list_tools`` hook 现挂的
        （``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_middleware.py:443``），
        所以只能从这个调用记录（或 ``middleware.list_tools()``）里看到。

        Returns:
            `list[str]`: 工具名列表。
        """
        from agentscope.message import ToolCallBlock

        state = getattr(self.built.agent, "state", None)
        names: list[str] = []
        for message in getattr(state, "context", None) or []:
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, ToolCallBlock):
                    names.append(str(block.name))
        return names

    def verify_citations(self, answer: str) -> CitationCheck:
        """把回答里的引用与本次检索命中交叉核对（见模块 docstring 的说明）。

        **核对的是"文件级"证据**：回答里点名的文件，是否是本次检索命中的
        某个来源对应的文件（比对用的是 :func:`source_candidates` 给出的
        完整路径 / 文件名 / 去掉 ``.md`` 的文件名）。

        **已知上限（不许当成"每个字都对"）**：同名文件会互相冒充 ——
        命中 ``plan/_agent.py`` 而回答引用 ``agent/_agent.py`` 时，这一层
        判不出来。要更严就得把 chunk 的行号区间也带进来，那是"逐句核对"，
        不是 Demo 该做的事；回答内容是否真的对得上原文，得靠人看或换评测集。

        Args:
            answer (`str`): 模型回答。

        Returns:
            `CitationCheck`: 核对结果。
        """
        markers = citation_markers(answer)
        sources = list(dict.fromkeys(self.last_sources))
        grounded = [
            source
            for source in sources
            if any(candidate in answer for candidate in source_candidates(source))
        ]
        # "孤儿编号"的定义：回答里写了 [n]，但**一条检索命中都没被点名**。
        # 不逐条核对编号与来源的一一对应 —— 模型写 [1] 时未必对应
        # hits[0]，强行对齐会造出假的"引用错位"结论。
        return CitationCheck(
            markers=markers,
            sources=sources,
            grounded=grounded,
            orphan_markers=[] if grounded else list(markers),
        )

    async def aclose(self) -> None:
        """关闭装配器（连带关掉嵌入的 ReMe 客户端）。"""
        closer = getattr(self.builder, "aclose", None) or getattr(self.builder, "close", None)
        if closer is None:
            return
        outcome = closer()
        if hasattr(outcome, "__await__"):
            await outcome


async def build_code_assistant(
    *,
    settings: "Settings | None" = None,
    index_root: str | Path = DEFAULT_INDEX_DIR,
    profile_name: str = BASE_PROFILE,
    session_id: str | None = None,
    sys_prompt: str = CODE_SYS_PROMPT,
    read_only_tools: bool = True,
) -> CodeAssistant:
    """一行装出 Demo 的代码助手。

    Args:
        settings (`Settings | None`): 设置对象；``None`` 时用 :func:`default_settings`。
        index_root (`str | Path`): 记忆工作区（相对路径按 ``settings.repo_root`` 解析）。
        profile_name (`str`): 基础 Profile 名。
        session_id (`str | None`): 会话 id；``None`` 时由 builder 生成。
        sys_prompt (`str`): 覆盖 sys_prompt。
        read_only_tools (`bool`): 是否把工具收窄成 ``Read`` / ``Grep`` / ``Glob``。

    Returns:
        `CodeAssistant`: 装好的助手。

    Raises:
        `ValueError`: 基础 Profile 缺少记忆中间件（见 :func:`demo_profile`）。
    """
    from harness_kit.config.builder import HarnessBuilder

    settings = settings or default_settings()
    profile = demo_profile(
        index_root=index_root,
        settings=settings,
        profile_name=profile_name,
        question_prompt=sys_prompt,
    )
    builder = HarnessBuilder(profile, settings=settings, session_id=session_id)
    built = await builder.build_all()

    if read_only_tools:
        removed = await _restrict_tools(built.agent)
        if removed:
            logger.info("Demo 已收窄工具集，移除写工具: {}", removed)

    root = Path(str(profile.memory.workspace_root))
    logger.bind(
        profile=profile.name,
        agent=profile.agent.name,
        index_root=str(root),
        permission=profile.permission.mode,
        middlewares=[type(m).__name__ for m in built.middlewares],
    ).info("代码助手已装配")
    return CodeAssistant(
        settings=settings,
        profile=profile,
        builder=builder,
        built=built,
        index_root=root,
    )


async def _restrict_tools(agent: Any) -> list[str]:
    """把 Agent 工具集里除 :data:`READ_ONLY_TOOLS` 之外的写工具摘掉。

    用的是 AgentScope ``Toolkit`` 自己的接口（**不重写工具管理**）：

    - 枚举：``toolkit.tool_groups[i].tools[j].name``
      （``third_party/agentscope/src/agentscope/tool/_tool_group.py:32`` 的 ``tools``）；
    - 摘除：``await toolkit.remove_tool(name)``
      （``third_party/agentscope/src/agentscope/tool/_toolkit.py:682``，**是 async**）。

    摘不掉时**只打 warning 不抛异常** —— 权限层的 ``explore`` 仍是兜底，
    Demo 不该因为一个工具名对不上就整个起不来。

    Args:
        agent (`Any`): AgentScope ``Agent``。

    Returns:
        `list[str]`: 实际被移除的工具名。
    """
    toolkit = getattr(agent, "toolkit", None)
    remover = getattr(toolkit, "remove_tool", None)
    if toolkit is None or not callable(remover):
        logger.warning(
            "Toolkit 不可用或没有 remove_tool（tool/_toolkit.py:682），"
            "跳过工具收窄；权限层仍会拦截写操作",
        )
        return []

    names: list[str] = []
    for group in getattr(toolkit, "tool_groups", []) or []:
        for tool in getattr(group, "tools", []) or []:
            names.append(str(getattr(tool, "name", tool)))

    removed: list[str] = []
    for name in names:
        if name in READ_ONLY_TOOLS:
            continue
        try:
            await remover(name)
            removed.append(name)
        except Exception as exc:  # noqa: BLE001 - 摘不掉不影响只读语义
            logger.warning("移除工具 {} 失败: {}", name, exc)
    return removed
