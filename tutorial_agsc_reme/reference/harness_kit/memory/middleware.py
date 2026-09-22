# -*- coding: utf-8 -*-
"""长期记忆中间件：在官方 ``ReMeMiddleware`` 之上补三件事（契约 §3.19）。

**先说清楚"这一层不是重写"**

:class:`LongTermMemoryMiddleware` **继承** AgentScope 官方的
``ReMeMiddleware``（``third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_middleware.py:88``），
并逐字保留它的全部语义：

- 写回发生在 ``on_reply`` 的 ``finally`` 里 —— 回复中途抛异常也要落记忆；
- 写回只取"本轮增量"（用消息 id 差集算出来），不是整段 ``state.context``；
- 注入的消息名固定 ``"memory"``，且写回时按这个名字把它滤掉；
- 单次 reply 内只注入**一条** ``AssistantMsg(name="memory", content=[HintBlock(...)])``
  （user 消息不能携带 HintBlock，所以必须是 assistant 角色）；
- 检索在后台任务里跑，``on_reasoning`` 只"顺手"取一次结果，取不到就跳过本轮
  （单步回复可能在检索完成前就结束了）。

**补的三件事，每一件都对应官方实现里一个具体缺口**

1. **token 预算裁剪（:class:`~harness_kit.memory.budget.MemoryBudget`）**：
   官方把检索到的 chunk 原文用 ``- {text}`` 逐条塞进 prompt，
   **没有任何长度控制**。ReMe 的默认分块器是 10 000 字节窗口
   （``default_file_chunker.py:19-38``），命中 5 条就可能注入几万字符。
2. **显式传 ``min_score``**：官方的 ``_search``
   （``_middleware.py:475-487``）只传 ``query`` 与 ``limit``，
   于是 ReMe 的过滤阈值永远是默认的 0.0（不过滤）。
   注意量纲陷阱，见 :mod:`harness_kit.memory.hybrid`。
3. **显式传 ``tool_context_id``**：官方从不传，于是 ReMe 的**同轮去重**
   （``search.py:108-137`` 的 ``_dedupe_tool_context``）永远不会生效 ——
   同一轮里被 agent 调用多次 ``memory_search`` 时会重复拿到同样的 chunk。
   harness 用 ``session_id`` 当桶，实现"同一会话内不重复给同一条记忆"。

**与官方的两处不可避免的差异（都写在代码注释里）**

- 官方的 ``_search`` 返回 ``list[str]``（纯文本），注入用
  ``_build_memory_message`` 拼成 ``- bullet``。harness 的检索要经过
  门控/预算，处理的是 :class:`~harness_kit.memory.citations.MemoryHit`，
  所以 :meth:`LongTermMemoryMiddleware.on_reasoning` 与 ``on_reply``
  是官方 hook 的**逐行等价体**，只替换"检索"与"注入"两处。
  其余（``pre_ids`` 增量算法、``finally`` 写回、任务清理、消息名过滤）
  逐字保留 —— 改动它们会让"官方语义"这句话变成空话。
- 开了预算时，注入的文本是 :func:`~harness_kit.memory.budget.render_memory_block`
  的产物（带 ``### path:start-end`` 引用行），而不是 ``- bullet``：
  预算的计量对象必须**就是**注入对象，否则"预算里算 300 token、
  实际注入 900 token"这种漂移会永远存在（``budget.py:133-160`` 的
  "全流程唯一的渲染入口"说的就是这件事）。

**``ensure_reme_compat``：一个真实的版本兼容缺口**

AgentScope 官方的 ReMe 配置（``_reme/_config.py:54-75`` 的 ``_dream_steps``）
里有一步 ``backend: "dream_topics_step"``，而 ReMe 0.4.1.13 **没有注册**
这个 backend。``BaseJob._start``（``third_party/ReMe/reme/components/job/base_job.py:59``）
在 **app 启动时**就解析全部 step，于是 ``await app.start()`` 直接抛：

.. code-block:: text

    ValueError: Unregistered backend 'dream_topics_step' of type 'ComponentEnum.STEP'

:func:`ensure_reme_compat` 打**两处**补丁，都**不改 ``third_party/`` 下的文件**：

1. **主路径：换掉配置生成函数。** 运行时把 ``agentscope..._reme._config`` 模块里的
   ``_dream_steps`` 换成剔掉那一步的版本，于是 ``_build_reme_app_config()``
   生成的配置里根本不含未注册 backend。必须在构造 app **之前**打
   （见 :meth:`LongTermMemoryMiddleware._build_app` 的调用顺序）。
2. **兜底：补 app 的 registry。** 如果 app 是别处（比如直接用官方
   ``ReMeMiddleware``）已经构造好的，配置里还带着那一步，就往
   ``app.context.registry`` 注册一个 no-op 替身类。必须在
   ``Application.start()`` 之前。

为什么不能改全局注册表：全局模板 ``reme.components.R`` 在 import 时就被
``freeze()`` 了，写入抛 ``RuntimeError: Component registry is frozen``（实测）。
而 ``Application`` 用的是 ``create_application_registry()`` 产出的**可变副本**
（``component_registry.py:154``），所以补丁只能逐 app 打。

no-op 的代价必须说清楚：替身把 ``success=True`` 写进响应（看起来"跑过了"），
而 dream 的主题抽取其实被跳过 —— 于是 ``dream_extract_step`` 期望的主题源
不会由这一步产生。

**这条兼容缺口只影响 AgentScope 自己的那份 minimal config。**
harness 的装配走的是 ReMe 的 ``default.yaml``，它的 ``auto_dream`` 里
**没有** ``dream_topics_step``（实测：``jobs.auto_dream.steps`` 是
``dream_extract_step`` / ``dream_integrate_step`` / ``dream_finish_step`` /
``auto_tag_step``），所以整条 dream 流水线在本环境是**完整跑通**的：
给两张 daily 笔记跑一轮 ``MemoryMaintainer.auto_dream`` 得到
``Extracted: 3 unit(s) / Integrated: 3 ok``，并在
``digest/personal/`` 与 ``digest/procedure/`` 下落了 3 个节点。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.middleware import ReMeMiddleware
from agentscope.middleware._longterm_memory._reme._utils import _extract_query_text
from agentscope.message import AssistantMsg, HintBlock, Msg

from .budget import MemoryBudget, estimate_tokens_heuristic
from .citations import MemoryHit, to_memory_hits
from .distill import AUTO_MEMORY_JOB as _AUTO_MEMORY_JOB
from .gating import DEFAULT_MIN_SCORE, MemoryGate, MemoryWriteGate, WriteDecision
from .metrics import MemoryMetrics

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from agentscope.agent import Agent

__all__ = [
    "LongTermMemoryMiddleware",
    "build_memory_middleware",
    "ensure_reme_compat",
]

#: 官方注入记忆时用的保留消息名（``_middleware.py:71``）。**不可改**：
#: 写回与注入两侧都靠它互相识别。
_MEMORY_MSG_NAME: str = "memory"

#: AgentScope 配置里引用、但 ReMe 0.4.1.13 未注册的 step。
_COMPAT_STEP_NAME: str = "dream_topics_step"

#: ``_compat_step_class()`` 的缓存，保证同一个进程里类对象稳定 ——
#: ``ComponentRegistry._do_register`` 用 ``existing is cls`` 判幂等
#: （``component_registry.py:48``），每次新建一个类会让第二次注册抛
#: "provided by both ... and ..."。
_COMPAT_STEP_CACHE: dict[str, type] = {}


def _compat_step_class() -> type:
    """构造（并缓存）``dream_topics_step`` 的 no-op 替身类。

    惰性构造的理由：``reme`` 是可选依赖，而 ``raise`` 只能发生在调用时
    （import 时构造会让 ``import harness_kit.memory.middleware`` 在没有 reme 的
    机器上直接失败）。

    Returns:
        `type`: ``BaseStep`` 的子类。
    """
    cached = _COMPAT_STEP_CACHE.get(_COMPAT_STEP_NAME)
    if cached is not None:
        return cached

    from reme.steps.base_step import BaseStep

    class _DreamTopicsCompatStep(BaseStep):
        """``dream_topics_step`` 的 no-op 替身（只让 job 能装配起来）。

        ``BaseStep.__call__``（``third_party/ReMe/reme/steps/base_step.py:150-162``）
        会先把 kwargs 合进 ``RuntimeContext`` 再调 ``execute``，
        所以这里只需要把响应标成成功并把"这是替身"写进 ``metadata``，
        让事后排查能一眼看出 dream 的这一步没真的跑。
        """

        async def execute(self) -> Any:
            """标记成功并说明自己是替身。

            Returns:
                `Any`: ``self.context.response``。
            """
            assert self.context is not None
            response = self.context.response
            response.success = True
            response.answer = (
                "dream_topics_step: harness 兼容替身（no-op）——"
                "ReMe 0.4.1.13 未注册该 backend，dream 的主题抽取被跳过。"
            )
            response.metadata.update(
                {
                    "compat_shim": _COMPAT_STEP_NAME,
                    "noop": True,
                },
            )
            self.logger.info(f"[{self.name}] no-op compat shim ran")
            return response

    _COMPAT_STEP_CACHE[_COMPAT_STEP_NAME] = _DreamTopicsCompatStep
    return _DreamTopicsCompatStep


def ensure_reme_compat(app: Any | None = None) -> list[str]:
    """补齐 ReMe 0.4.1.13 缺失的 step backend（幂等，两处补丁）。

    做两件事，**第一件是主路径，第二件是兜底**：

    1. **改配置生成**：把 ``agentscope..._reme._config`` 模块里的
       ``_dream_steps`` 换成一个"剔掉 ``dream_topics_step``"的版本。
       AgentScope 的 app config 是这个函数生成的（``_config.py:54-76``），
       所以补丁必须在 **``_build_reme_app_config()`` 被调用之前**打上 ——
       这样生成的配置里根本不会出现那个未注册的 backend。
       这是**运行时替换模块里的一个函数对象**，没有改动 ``third_party/`` 里的任何文件。
    2. **补 registry**（兜底）：如果拿到的 ``app`` 是**已经构造好**的
       （配置里已经带着那个 step），就往它的 registry 里注册一个 no-op 替身类，
       让 ``BaseJob._start``（``base_job.py:59``）能解析出 step 类。
       必须在 ``Application.start()`` **之前** —— 启动时 step_specs 就被缓存了。

    两件事都做了的理由：只做 1，则"用官方 ``ReMeMiddleware`` 自己造的 app"
    （不经过 harness 的 ``_build_app``）仍然会在 start 时报错；
    只做 2，则 dream 流水线里会多出一个撒谎的 no-op 步骤（它把
    ``success=True`` 写进响应，看起来"跑过了"）。

    全局模板 ``reme.components.R`` 也会尝试一下，但那一条路在 ReMe 0.4.1.13 上
    **必然失败**（模板在 import 时就被 ``freeze()`` 了，实测抛
    ``RuntimeError: Component registry is frozen``），日志里只会留一行 debug。

    Args:
        app (`Any | None`): 已构造但**尚未 start** 的 ``reme.ReMe`` 实例；
            ``None`` 时只打配置补丁。

    Returns:
        `list[str]`: 本次真正打上的补丁，形如
        ``["agentscope._dream_steps", "app.context.registry:dream_topics_step"]``；
        补不上时为空列表（不会抛异常 —— 兼容补丁失败应该降级为可诊断的警告，
        而不是让中间件构造不出来）。
    """
    patched: list[str] = []
    patch_name = f"agentscope._dream_steps(-{_COMPAT_STEP_NAME})"
    if _patch_agentscope_dream_steps():
        patched.append(patch_name)

    try:
        from reme.enumeration import ComponentEnum
    except ImportError as exc:  # pragma: no cover - 无 reme 的环境
        logger.debug("ensure_reme_compat: reme 不可导入（{}），跳过 registry 补丁", exc)
        return patched

    targets: list[tuple[str, Any]] = []
    registry = getattr(getattr(app, "context", None), "registry", None)
    if registry is not None:
        targets.append(("app.context.registry", registry))
    try:
        from reme.components import R

        targets.append(("reme.components.R", R))
    except ImportError:  # pragma: no cover - 拿不到模板不影响主路径
        pass

    step_cls = _compat_step_class()
    for label, target in targets:
        try:
            existing = target.get(ComponentEnum.STEP, _COMPAT_STEP_NAME)
        except Exception as exc:  # noqa: BLE001 - 读不了就跳过这个目标
            logger.debug("ensure_reme_compat: 读 {} 失败: {}", label, exc)
            continue
        if existing is not None:
            logger.debug("ensure_reme_compat: {} 已有 {}，无需补丁", label, _COMPAT_STEP_NAME)
            continue
        try:
            target.register(_COMPAT_STEP_NAME)(step_cls)
        except Exception as exc:  # noqa: BLE001 - 冻结模板走这里
            logger.debug(
                "ensure_reme_compat: 在 {} 注册 {} 失败（{}）——"
                "若这是被 freeze() 的全局模板，属预期行为",
                label,
                _COMPAT_STEP_NAME,
                exc,
            )
            continue
        logger.warning(
            "ensure_reme_compat: 已在 {} 注册 {} 的 no-op 替身（dream 的主题抽取会被跳过）",
            label,
            _COMPAT_STEP_NAME,
        )
        patched.append(f"{label}:{_COMPAT_STEP_NAME}")
    return patched


def _patch_agentscope_dream_steps() -> bool:
    """把 AgentScope 的 ``_dream_steps()`` 换成剔掉 ``dream_topics_step`` 的版本。

    幂等：补丁函数上留一个 ``_harness_compat`` 标记，第二次调用直接返回。

    Returns:
        `bool`: 本次是否**新打了**补丁（已经打过或打不上返回 ``False``）。
    """
    try:
        from agentscope.middleware._longterm_memory._reme import _config as as_config
    except ImportError as exc:  # pragma: no cover - 没有 agentscope 的环境
        logger.debug("ensure_reme_compat: 拿不到 agentscope 的 ReMe 配置模块: {}", exc)
        return False

    original = getattr(as_config, "_dream_steps", None)
    if original is None:  # pragma: no cover - 官方改了结构
        logger.warning("ensure_reme_compat: agentscope 里找不到 _dream_steps，跳过配置补丁")
        return False
    if getattr(original, "_harness_compat", False):
        return False

    def _dream_steps_without_topics() -> list[dict[str, Any]]:
        """``_dream_steps()`` 的兼容版：剔掉 reme 未注册的那一步。

        Returns:
            `list[dict[str, Any]]`: 剩下的 step 配置。
        """
        steps = list(original())
        kept = [step for step in steps if str(step.get("backend", "")) != _COMPAT_STEP_NAME]
        dropped = len(steps) - len(kept)
        if dropped:
            logger.info(
                "ensure_reme_compat: 已从 agentscope 的 dream 步骤里移除 {} 个未注册 backend（{}）",
                dropped,
                _COMPAT_STEP_NAME,
            )
        return kept

    _dream_steps_without_topics._harness_compat = True  # type: ignore[attr-defined]
    _dream_steps_without_topics._harness_original = original  # type: ignore[attr-defined]
    as_config._dream_steps = _dream_steps_without_topics
    logger.warning(
        "ensure_reme_compat: 已替换 agentscope 的 _dream_steps（去掉未注册的 {}）；"
        "未修改 third_party 下的任何文件",
        _COMPAT_STEP_NAME,
    )
    return True


class LongTermMemoryMiddleware(ReMeMiddleware):
    """带预算与门控的长期记忆中间件（契约 §3.19）。

    Example::

        middleware = LongTermMemoryMiddleware(
            workspace_dir=".harness/reme",
            parameters=LongTermMemoryMiddleware.Parameters(
                chat_model=model,
                mode="both",
                top_k=5,
                budget=MemoryBudget(max_tokens=800),
                min_score=0.0,
            ),
            metrics=MemoryMetrics(),
        )
        agent = Agent(..., middlewares=[middleware])
        await agent(Msg("user", "上次我们定的是哪个部署方案？", "user"))
        await middleware.close()

    也可以直接给扁平参数（本类的 ``__init__`` 是 ``**params``）：

    .. code-block:: python

        LongTermMemoryMiddleware(workspace_dir=".harness/reme", mode="both", top_k=5)
    """

    class Parameters(ReMeMiddleware.Parameters):
        """官方 ``Parameters`` 的超集（契约 §3.19）。

        自带 10 个字段，其中 ``mode`` 是**覆盖默认值**（官方默认 ``"both"``，
        契约要求 harness 默认 ``"static_control"``），其余 9 个是新增：

        - 注入侧：``budget``（token 预算）、``min_score``（透传 ReMe）、
          ``tool_context_id``（同轮去重桶）、``gate``（召回门控）、
          ``metrics``（观测）；
        - 写回侧：``write_gate``（写入门控）、``write_async``（异步化）、
          ``write_timeout_s``（单次超时）、``session_tags``（隔离标签）。
        """

        model_config = ConfigDict(arbitrary_types_allowed=True)

        mode: Literal["static_control", "agent_control", "both"] = Field(
            default="static_control",
            title="Retrieval Mode",
            description=(
                "与官方同名同义，仅默认值不同：官方默认 'both'，"
                "harness 默认 'static_control'（契约 §3.19 如此要求）。"
                "写回在三种模式下都自动发生，mode 只控制检索。"
            ),
        )

        budget: MemoryBudget | None = Field(
            default=None,
            title="Token Budget",
            description=(
                "注入记忆的 token 预算。None = 不做裁剪（与官方行为一致）。"
                "给了预算时注入文本由 render_memory_block 产出，带 path:start-end 引用行。"
            ),
        )

        min_score: float = Field(
            default=0.0,
            title="Min Score",
            description=(
                "透传给 ReMe search 的 min_score。**量纲警告**：RRF 融合分在 0.016 量级，"
                "用 0.2 之类的绝对值会把融合结果全滤掉；0.0 = 不过滤（ReMe 的默认行为）。"
            ),
        )

        tool_context_id: str | None = Field(
            default=None,
            title="Tool Context Id",
            description=(
                "ReMe 的**同轮去重**桶 id。None 时 harness 用 session_id（"
                "同一会话内不重复给同一条记忆）。官方从不传它，所以官方路径下这个能力是关闭的。"
            ),
        )

        metrics: MemoryMetrics | None = Field(
            default=None,
            title="Metrics",
            description=(
                "harness 扩展字段（契约 §3.19 只列了四个额外参数，这是第五个）。"
                "用来记录检索命中率与注入 token —— 没有它，"
                "'检索退化成了纯 BM25' 与 '门控把记忆全拦了' 都不会留下痕迹。"
            ),
        )

        gate: MemoryGate | None = Field(
            default=None,
            title="Recall Gate",
            description=(
                "召回门控。给定时，检索结果先过 MemoryGate（敏感会话 / 归一化分数 / "
                "预算），被拒就不注入并记 gated=True。None = 不门控（只做预算裁剪）。"
            ),
        )

        write_gate: MemoryWriteGate | None = Field(
            default=None,
            title="Write Gate",
            description=(
                "写入门控。给定时，本轮增量先过 MemoryWriteGate，被拒就不调用 auto_memory "
                "（省掉一次 LLM 抽取）。None = 每轮都写（官方行为）。"
            ),
        )

        write_async: bool = Field(
            default=True,
            title="Async Write-Back",
            description=(
                "写回是否异步。True 时在后台任务里跑 auto_memory，回复不必等它结束"
                "（close() 会排空未完成的写入）。False = 与官方一致，在 on_reply 的 "
                "finally 里 await 写完再返回。"
            ),
        )

        write_timeout_s: float = Field(
            default=30.0,
            title="Write Timeout",
            description=(
                "单次写回的超时（秒）。超时按失败记账并取消任务 —— "
                "官方没有超时，一个卡住的 auto_memory 会永久挂住回复。"
            ),
        )

        session_tags: tuple[str, ...] = Field(
            default=(),
            title="Session Tags",
            description=(
                "本实例服务的会话标签，供召回门控的敏感判定使用。"
                "需要按会话动态判定时覆写 session_tags_for(agent)。"
            ),
        )

    def __init__(self, **params: Any) -> None:
        """构造中间件（参数与官方同形，外加四个扩展项）。

        Args:
            **params (`Any`): 两种写法都支持 ——

            1. 官方写法：``workspace_dir=".reme", parameters=Parameters(...)``；
            2. 扁平写法：直接把 ``Parameters`` 的字段当关键字参数传
               （``mode="both", top_k=5, budget=...``）。

            混用（既给 ``parameters`` 又给扁平字段）会抛 ``TypeError``，
            因为那一定是调用方写错了，静默忽略其中一半是最难查的 bug。

        Raises:
            `TypeError`: ``parameters`` 与扁平参数混用，或扁平参数里有未知字段。
            `pydantic.ValidationError`: 参数值不合法（类型/取值范围）。
        """
        nested = params.pop("parameters", None)
        workspace_dir = params.pop("workspace_dir", ".reme")
        if nested is not None:
            if params:
                raise TypeError(
                    "parameters 与扁平参数不能混用；多余的键: " + ", ".join(sorted(params)),
                )
            parameters = (
                nested
                if isinstance(nested, LongTermMemoryMiddleware.Parameters)
                else LongTermMemoryMiddleware.Parameters(**dict(nested))
            )
        else:
            # 未知字段在这里**手工**挡掉，而不是靠 pydantic 的 extra="forbid"：
            # 官方 ``ReMeMiddleware.Parameters`` 的 model_config 是
            # ``{"arbitrary_types_allowed": True}``，extra 语义是默认的 "ignore"，
            # 而 AgentScope 的 Agent 服务会按这个 schema 渲染配置表单 ——
            # 把 extra 改成 forbid 会改变官方那个类的对外契约（实测：
            # ``LongTermMemoryMiddleware(完全不存在的字段=1)`` 被静默接受）。
            # 所以在**扁平入口**这一侧做检查：这里是我们自己的 API，
            # 打错的键必须报错；``parameters=Parameters(...)`` 那条官方路径保持原样。
            unknown = sorted(set(params) - set(LongTermMemoryMiddleware.Parameters.model_fields))
            if unknown:
                raise TypeError(
                    "LongTermMemoryMiddleware 收到未知参数: "
                    + ", ".join(unknown)
                    + "；可用: "
                    + ", ".join(sorted(LongTermMemoryMiddleware.Parameters.model_fields)),
                )
            parameters = LongTermMemoryMiddleware.Parameters(**params)

        super().__init__(workspace_dir=workspace_dir, parameters=parameters)
        self._compat_patched: list[str] = []
        #: 官方 ``_parameters`` 的类型是官方 ``Parameters``，这里收窄成我们自己的，
        #: 好让类型检查器能看到 ``budget`` / ``min_score`` / ``metrics``。
        self._parameters: LongTermMemoryMiddleware.Parameters = parameters
        #: 在飞的写回任务。异步化的代价是"进程退出时可能丢写入"，
        #: 所以必须持有引用并在 :meth:`close` 里排空。
        #: 用 ``id(task)`` 做键不需要，集合本身就按对象身份去重。
        self._write_tasks: set[asyncio.Task] = set()

    # ==================================================================
    # 生命周期
    # ==================================================================
    def _build_app(self) -> Any:
        """构造嵌入式 app，并在构造前/start 前补上 ReMe 兼容补丁。

        顺序是有讲究的：**先**打配置补丁（影响 ``_build_reme_app_config`` 的产物），
        **再**构造 app，**最后**兜底补 registry。
        反过来写的话，构造出来的配置里仍然带着那个未注册的 step。

        Returns:
            `Any`: ``reme.ReMe`` 实例（**尚未** start）。

        Raises:
            `ImportError`: ``reme`` 没装（由父类抛出）。
        """
        self._compat_patched = ensure_reme_compat(None)
        app = super()._build_app()
        self._compat_patched += [
            item for item in ensure_reme_compat(app) if item not in self._compat_patched
        ]
        return app

    @property
    def compat_patched(self) -> list[str]:
        """本实例实际打上的兼容补丁（诊断用）。

        Returns:
            `list[str]`: :func:`ensure_reme_compat` 的返回值。
        """
        return list(self._compat_patched)

    async def close(self) -> None:
        """排空在飞的写回任务，再关掉嵌入式 app（**顺序不能反**）。

        为什么必须先排空：``auto_memory`` 是嵌在 app 里的 job，
        app 一关，在飞的写回就只能在半途失败 —— 而且失败是静默的
        （``_do_write_back`` 只打 warning）。先排空能保证
        "回复已完成 ⇒ 它的记忆已经写完"这条不变式。

        排空用 ``asyncio.wait(timeout=...)``：写回超时（``write_timeout_s``）
        是**单次**上限，这里有 N 个在飞任务，所以总的等待上限取
        ``write_timeout_s * max(1, len(pending))``，避免 10 个任务把关闭
        拖成 10 倍时长。

        Raises:
            `asyncio.CancelledError`: 调用方被取消（照常传播，不吞）。
        """
        pending = {task for task in self._write_tasks if not task.done()}
        if pending:
            budget = float(self._parameters.write_timeout_s) * max(1, len(pending))
            logger.info("memory middleware: 关闭前排空 {} 个在飞写回（上限 {:.1f}s）", len(pending), budget)
            _done, still_pending = await asyncio.wait(pending, timeout=budget)
            for task in still_pending:
                task.cancel()
            if still_pending:
                logger.warning(
                    "memory middleware: 关闭时仍有 {} 个写回未完成，已取消（这些记忆会丢）",
                    len(still_pending),
                )
        self._write_tasks.clear()
        await super().close()

    # ==================================================================
    # 召回门控
    # ==================================================================
    def session_tags_for(self, agent: "Agent") -> tuple[str, ...]:
        """返回该 agent 的会话标签（供召回门控的敏感判定）。

        默认返回构造时给的 ``session_tags``。需要按会话动态判定
        （例如多租户下用 agent 名当租户标签）时覆写本方法 ——
        它是本类**唯一**为多租户/敏感场景预留的扩展点。

        Args:
            agent (`Agent`): 正在回复的 agent（默认实现不读它）。

        Returns:
            `tuple[str, ...]`: 会话标签。
        """
        return tuple(self._parameters.session_tags)

    def _gate_hits(
        self,
        hits: list[MemoryHit],
        *,
        session_id: str | None,
        agent: "Agent",
    ) -> tuple[list[MemoryHit], bool]:
        """把检索结果过一遍召回门控。

        没配 ``gate`` 时原样返回（``gated=False``），与官方行为一致；
        配了就把 :meth:`MemoryGate.apply` 的结论用起来：
        被拒时返回空列表 + ``gated=True``，让调用方记一笔
        ``record_injection(gated=True)``。

        Args:
            hits (`list[MemoryHit]`): 检索命中。
            session_id (`str | None`): 会话 id。
            agent (`Agent`): 用于取 :meth:`session_tags_for`。

        Returns:
            `tuple[list[MemoryHit], bool]`: ``(放行的命中, 是否被门控拒绝)``。
        """
        gate = self._parameters.gate
        if gate is None:
            return hits, False
        decision, kept = gate.apply(hits, session_tags=self.session_tags_for(agent))
        if not decision.allow:
            logger.info(
                "memory middleware: 召回被门控拒绝（{}），session={}，候选 {} 条",
                decision.reason,
                session_id or "<unknown>",
                len(hits),
            )
            return [], True
        logger.debug(
            "memory middleware: 门控放行 {}/{} 条（dropped_low_score={}）",
            len(kept),
            len(hits),
            decision.dropped_low_score,
        )
        return list(kept), False

    # ==================================================================
    # 检索
    # ==================================================================
    async def _retrieve(self, query: str, *, session_id: str | None) -> list[MemoryHit]:
        """检索记忆并转成 :class:`MemoryHit`（官方 ``_search`` 的富版本）。

        Args:
            query (`str`): 查询串。
            session_id (`str | None`): 会话 id；用于去重桶。

        Returns:
            `list[MemoryHit]`: 命中列表（顺序 = ReMe 的融合顺序）。

        Raises:
            `RuntimeError`: ReMe 报 ``success=False``（由 ``_run_job`` 抛）。
            `MemoryUnavailableError`: 配了 ``tool_context_id`` 或去重桶
                却解析不出工作区（只影响路径归一，不影响检索本身，
                此时路径保持 ReMe 给的原样）。
        """
        parameters = self._parameters
        bucket = parameters.tool_context_id or session_id or None
        payload: dict[str, Any] = {
            "query": query,
            "limit": int(parameters.top_k),
            "min_score": float(parameters.min_score),
        }
        if bucket:
            payload["tool_context_id"] = str(bucket)

        started = time.monotonic()
        response = await self._run_job("search", **payload)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        metadata = dict(getattr(response, "metadata", None) or {})
        hits = to_memory_hits(metadata.get("results"), workspace=self._workspace())
        self._record_search(session_id, len(hits), elapsed_ms)
        logger.debug(
            "memory middleware: 检索 {!r} → {} 条（bucket={}，{:.0f} ms）",
            query[:60],
            len(hits),
            bucket,
            elapsed_ms,
        )
        return hits

    def _record_search(self, session_id: str | None, hits: int, elapsed_ms: float) -> None:
        """把一次检索记进 metrics（没配 metrics 就什么都不做）。

        官方实现没有指标，所以"检索退化成纯 BM25"或"每次检索都 0 命中"
        在官方路径下**不留痕迹**。这里补上，:data:`Parameters.metrics`
        的 docstring 承诺的"命中率"才真的可算。

        Args:
            session_id (`str | None`): 会话 id。
            hits (`int`): 命中条数。
            elapsed_ms (`float`): 检索耗时（毫秒）。
        """
        metrics = self._parameters.metrics
        if metrics is None:
            return
        try:
            metrics.record_search(
                session_id=session_id or "<unknown>",
                hits=int(hits),
                elapsed_ms=float(elapsed_ms),
            )
        except Exception as exc:  # noqa: BLE001 - 指标不该让检索失败
            logger.debug("memory middleware: record_search 失败: {}", exc)

    def _render_hits(self, hits: list[MemoryHit]) -> tuple[list[str] | str, int]:
        """把命中渲染成注入内容，并返回它的估计 token 数。

        没配预算 → 返回 ``list[str]``（每条一段正文），交给官方的
        ``_build_memory_message`` 拼 ``- bullet``，与官方行为逐字一致。
        配了预算 → 返回**整段块文本**，因为预算的计量对象必须是注入对象本身
        （``budget.py:133-160``）。

        **返回的 token 数与注入内容严格对应**：无预算时是对每条正文估值的和，
        有预算时是 ``MemoryBudgetResult.estimated_tokens``（即被保留内容渲染后的估计）。
        这样 ``metrics.record_injection`` 记下来的数才有意义 —— 记一个
        "全量命中"的数、注入的却是裁剪后的块，会导致指标永远高估。

        Args:
            hits (`list[MemoryHit]`): 命中列表。

        Returns:
            `tuple[list[str] | str, int]`: ``(注入内容, 估计 token 数)``。
        """
        budget = self._parameters.budget
        if budget is None:
            texts = [hit.text for hit in hits if str(hit.text or "").strip()]
            return texts, sum(estimate_tokens_heuristic(text) for text in texts)

        fitted = budget.fit(hits)
        logger.info(
            "memory middleware: 预算 {} token，保留 {}/{} 条，估计 {} token，truncated={}",
            budget.max_tokens,
            len(fitted.kept),
            len(hits),
            fitted.estimated_tokens,
            fitted.truncated,
        )
        return fitted.render(), int(fitted.estimated_tokens)

    # ==================================================================
    # Hook: on_reply（官方 hook 的逐行等价体，仅替换检索）
    # ==================================================================
    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        """回复前起一个后台检索任务，回复后无条件写回本轮增量。

        与官方实现（``_middleware.py:312-380``）的唯一差别：后台任务跑的是
        :meth:`_retrieve`（拿 ``MemoryHit``，带 ``min_score`` / ``tool_context_id``），
        而不是官方的 ``_search``（拿 ``list[str]``，不传这两个参数）。

        Args:
            agent (`Agent`): AgentScope 的 agent。
            input_kwargs (`dict`): hook 入参（含 ``inputs``）。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一个处理者。

        Yields:
            `Any`: 下游产生的事件，原样透传。
        """
        session_id = self._session_id_of(agent)
        inputs = input_kwargs.get("inputs")
        query_text = _extract_query_text(inputs)

        stale = self._retrieval_tasks.pop(session_id, None)
        if stale is not None and not stale.done():
            stale.cancel()
        if self._parameters.mode != "agent_control" and query_text:
            # 与官方逐字一致：后台任务，on_reasoning 里"顺手"取一次结果。
            self._retrieval_tasks[session_id] = asyncio.create_task(
                self._retrieve(query_text, session_id=session_id),
            )

        pre_ids = {m.id for m in agent.state.context if isinstance(m, Msg)}

        try:
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            task = self._retrieval_tasks.pop(session_id, None)
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 官方同款兜底
                    pass
            increment = [
                m
                for m in agent.state.context
                if isinstance(m, Msg)
                and m.id not in pre_ids
                and getattr(m, "name", None) != _MEMORY_MSG_NAME
            ]
            if query_text and any(
                m.role == "assistant" and m.get_text_content() for m in increment
            ):
                await self._write_back(increment, session_id)

    # ==================================================================
    # Hook: on_reasoning（官方 hook 的逐行等价体，仅替换注入）
    # ==================================================================
    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        """在推理步之前，把已就绪的检索结果注入上下文。

        与官方实现（``_middleware.py:385-416``）的唯一差别：注入内容走
        :meth:`_render_hits`（预算裁剪 / 引用行），并通过
        :meth:`_build_injection` 保持"一条 ``AssistantMsg(name="memory")``"的形状。
        另外多了一步 :meth:`_gate_hits`：配了 ``gate`` 时，命中先过召回门控，
        被拒就**不注入**（而不是注入空内容 —— 空 hint 会白占一段 context）。

        Args:
            agent (`Agent`): AgentScope 的 agent。
            input_kwargs (`dict`): hook 入参。
            next_handler (`Callable[..., AsyncGenerator]`): 链上的下一个处理者。

        Yields:
            `Any`: 下游产生的事件，原样透传。
        """
        session_id = self._session_id_of(agent)
        task = self._retrieval_tasks.get(session_id)
        if task is not None and task.done():
            self._retrieval_tasks.pop(session_id, None)
            try:
                hits = task.result()
            except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
                hits = []
                logger.warning("memory middleware: ReMe 检索失败: {}", e)
            if hits:
                hits, gated = self._gate_hits(hits, session_id=session_id, agent=agent)
            else:
                gated = False
            if hits:
                rendered, tokens = self._render_hits(hits)
                agent.state.context.append(self._build_injection(rendered))
                self._record_injection(session_id, hits, tokens)
            elif gated:
                # 被门控拒绝也要记一笔：否则"命中率高但注入率为 0"
                # 会被误读成检索坏了，实际是门控在按策略拒。
                self._record_injection(session_id, [], 0, gated=True)

        async for event in next_handler(**input_kwargs):
            yield event

    # ==================================================================
    # 内部
    # ==================================================================
    def _build_injection(self, rendered: list[str] | str) -> Msg:
        """构造注入消息（固定一条 ``AssistantMsg(name="memory")``）。

        Args:
            rendered (`list[str] | str`): :meth:`_render_hits` 的产物。

        Returns:
            `Msg`: ``AssistantMsg(name="memory", content=[HintBlock(...)])``。
        """
        if isinstance(rendered, str):
            return AssistantMsg(name=_MEMORY_MSG_NAME, content=[HintBlock(hint=rendered)])
        return self._build_memory_message(rendered)

    def _record_injection(
        self,
        session_id: str | None,
        hits: list[MemoryHit],
        tokens: int,
        *,
        gated: bool = False,
    ) -> None:
        """把注入量记进 metrics（没配 metrics 就什么都不做）。

        Args:
            session_id (`str | None`): 会话 id。
            hits (`list[MemoryHit]`): 本次注入的命中（用于取条数）。
            tokens (`int`): :meth:`_render_hits` 返回的**实际注入量**估计。
            gated (`bool`): 本次是否因门控被拒。被拒时 ``tokens=0``，
                指标里表现为"注入数 +1 但注入 token +0" —— 这是
                ``hit_rate`` 与"实际省下的 context"之间唯一能区分的信息。
        """
        metrics = self._parameters.metrics
        if metrics is None:
            return
        try:
            metrics.record_injection(
                session_id=session_id or "<unknown>",
                tokens=int(tokens),
                gated=bool(gated),
            )
        except Exception as exc:  # noqa: BLE001 - 指标不该让注入失败
            logger.debug("memory middleware: record_injection 失败: {}", exc)
        logger.debug("memory middleware: 注入 {} 条 / 约 {} token / gated={}", len(hits), tokens, gated)

    # ==================================================================
    # 写回：门控 + 异步化
    # ==================================================================
    async def _write_back(
        self,
        messages: list[Msg],
        session_id: str | None,
    ) -> None:
        """把本轮增量写回 ReMe —— 官方 ``_write_back``（``_middleware.py:489``）的增强版。

        官方版本做三件事：没有 ``session_id`` 就 warning 并 return；
        调 ``auto_memory``；异常只 warning。本实现保留这三件事的语义，
        前后各加一层：

        - **前**：``write_gate`` 判定本轮增量值不值得抽取。不配门控时行为
          与官方**逐字一致**（每轮都写）。配了门控且被拒时直接返回，
          省掉一次 LLM 抽取调用。
        - **后**：``write_async=True`` 时把真正的写入丢进后台任务
          （:meth:`_do_write_back`），本方法立即返回 —— 回复不必等写入。
          代价是引入"进程退出丢写入"的窗口，所以任务被记进
          ``self._write_tasks`` 并由 :meth:`close` 排空。
          ``write_async=False`` 时退化成官方的同步等待。

        注意 ``success=True`` **不等于**记忆落盘：``auto_memory`` 在
        "这轮没什么可记的"时会返回 ``success=True`` + ``metadata.created=false,
        modified=false``（实测，见 ``gating.py`` 模块 docstring）。
        所以成功与否由 :meth:`_do_write_back` 里的 ``created or modified`` 判定。

        Args:
            messages (`list[Msg]`): 本轮追加到 context 的增量（含 user 输入、
                每步 assistant、每个 tool call / tool result）。
            session_id (`str | None`): 会话 id，逐次从 agent 现读，永不缓存。
        """
        if not session_id:
            logger.warning("ReMe write skipped: no session_id captured from the agent.")
            return

        gate = self._parameters.write_gate
        if gate is not None:
            decision = gate.decide(messages, session_id=session_id)
            if not decision.allow:
                logger.info(
                    "memory middleware: 写回被门控拒绝（{}），session={}，messages={} chars={}",
                    decision.reason,
                    session_id,
                    decision.messages,
                    decision.chars,
                )
                return
            logger.debug(
                "memory middleware: 写回门控放行（{}），session={}，messages={} chars={}",
                decision.reason,
                session_id,
                decision.messages,
                decision.chars,
            )

        if not self._parameters.write_async:
            await self._do_write_back(messages, session_id)
            return

        task = asyncio.create_task(self._do_write_back(messages, session_id))
        self._write_tasks.add(task)
        # 任务自己负责把自己从集合里摘掉：回调比 close() 里的清理更及时，
        # 长会话下不会攒一堆已完成的 task 对象。
        task.add_done_callback(self._write_tasks.discard)

    async def _do_write_back(self, messages: list[Msg], session_id: str) -> bool:
        """真正跑 ``auto_memory``，并按"有没有落盘"记指标。

        判成功用 ``response.metadata`` 里的 ``created`` / ``modified``，
        而不是 ``response.success`` —— 理由见 :meth:`_write_back`。
        指标写不进去也**不影响**返回值（``record_writeback`` 包在 try 里）。

        Args:
            messages (`list[Msg]`): 本条写入的增量。
            session_id (`str`): 会话 id（已保证非空）。

        Returns:
            `bool`: 记忆是否真的新增或修改（即 ``auto_memory`` 是否落盘）。
        """
        try:
            response = await asyncio.wait_for(
                self._run_job(
                    _AUTO_MEMORY_JOB,
                    messages=[m.model_dump(mode="json") for m in messages],
                    session_id=session_id,
                ),
                timeout=float(self._parameters.write_timeout_s),
            )
        except asyncio.TimeoutError:
            # 官方没有超时：一个卡住的 auto_memory 会永久挂住回复。
            logger.warning(
                "ReMe auto_memory timeout ({}s) for session_id={}: 本轮记忆已丢弃",
                self._parameters.write_timeout_s,
                session_id,
            )
            self._record_writeback(session_id, ok=False)
            return False
        except Exception as e:  # noqa: BLE001 - 写回失败绝不能影响回复
            logger.warning("ReMe auto_memory failed for session_id=%s: %s", session_id, e)
            self._record_writeback(session_id, ok=False)
            return False

        metadata = getattr(response, "metadata", None) or {}
        landed = bool(metadata.get("created") or metadata.get("modified"))
        if not landed:
            # 这是最容易被漏掉的一条：success=True 但什么都没写。
            logger.info(
                "memory middleware: auto_memory 未落盘（success={}，created={}，modified={}，n_messages={}）",
                getattr(response, "success", None),
                metadata.get("created"),
                metadata.get("modified"),
                metadata.get("n_messages"),
            )
        self._record_writeback(session_id, ok=landed)
        return landed

    def _record_writeback(self, session_id: str, *, ok: bool) -> None:
        """写回指标（与 :meth:`_record_search` 同款：指标异常不许冒泡）。

        Args:
            session_id (`str`): 会话 id。
            ok (`bool`): 记忆是否真的落盘。
        """
        metrics = self._parameters.metrics
        if metrics is None:
            return
        try:
            metrics.record_writeback(session_id=session_id, ok=bool(ok))
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory middleware: record_writeback 失败: {}", exc)

    def _workspace(self) -> Any | None:
        """尽力构造一个工作区（用于路径归一）。

        ``ReMeWorkspace`` 是纯路径对象（``workspace.py:85``），构造它不需要 app，
        但需要 ``workspace_dir`` 能解析成一个目录名 —— 解析不了时返回 ``None``，
        让路径保持 ReMe 给的原样（不归一）。**不抛异常**：
        路径长什么样不该决定"这一轮要不要注入记忆"。

        Returns:
            `Any | None`: ``ReMeWorkspace`` 或 ``None``。
        """
        try:
            from .workspace import ReMeWorkspace

            return ReMeWorkspace(root=self._workspace_dir)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            logger.debug("memory middleware: 构造工作区失败（路径将不归一）: {}", exc)
            return None


# ======================================================================
# Profile 装配入口
# ======================================================================
#: ``MiddlewareSpec.params`` 允许出现的键。写错的键会抛 ``ValueError`` ——
#: Profile 里的拼写错误静默生效（"配了但没接线"）是本项目最难查的一类 bug。
_MEMORY_MIDDLEWARE_PARAMS: tuple[str, ...] = (
    "workspace_dir",
    "mode",
    "top_k",
    "min_score",
    "budget_tokens",
    "tool_context_id",
    "chat_model",
    # -- 召回门控（Profile 只能给标量，对象由下面的 builder 构造）--
    "gate_min_score",
    "sensitive_tags",
    "session_tags",
    # -- 写入门控与异步化 --
    "write_min_messages",
    "write_min_chars",
    "write_async",
    "write_timeout_s",
)

#: AgentScope 的 ReMe minimal config 在**构造 app 时**从进程环境读这几个变量
#: （``.../middleware/_longterm_memory/_reme/_config.py:288`` 的
#: ``os.getenv("LLM_API_KEY", "")`` / ``:289`` 的 ``LLM_BASE_URL``）。当调用方
#: 没有把 ``chat_model`` 注进来时，这是唯一的凭据来源，必须显式补齐。
_REME_LLM_ENV_KEYS: tuple[str, ...] = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL_NAME",
    "LLM_BACKEND",
)


def _export_llm_env(environ: dict[str, str] | None) -> list[str]:
    """把 ``Settings.environ_overlay()`` 里的 LLM 变量补进 ``os.environ``。

    为什么必须做：``.env`` 里通常只有 ``OPENAI_API_KEY`` /
    ``OPENAI_BASE_URL``，而 AgentScope 的 ReMe minimal config 只认
    ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL_NAME``。不补的话，
    中间件能起、能检索，但 ``auto_memory`` 写回会在第一次调用时打一条
    ``Missing credentials`` 的 warning 然后静默跳过 —— 记忆永远写不进去。

    **只覆盖 ReMe 真正读的那四个键**，而不是把整个 ``os.environ`` 复制一遍：
    全局环境是共享状态，动得越少越好。

    Args:
        environ (`dict[str, str] | None`): 来自 ``BuildContext.environ``。

    Returns:
        `list[str]`: 本次真正写入的键名（给日志用）。
    """
    import os

    written: list[str] = []
    for key in _REME_LLM_ENV_KEYS:
        value = (environ or {}).get(key)
        if value and os.environ.get(key) != value:
            os.environ[key] = value
            written.append(key)
    return written


def build_memory_middleware(spec: Any, *, ctx: Any = None) -> LongTermMemoryMiddleware:
    """按 Profile 的 ``memory`` 块装配 :class:`LongTermMemoryMiddleware`。

    这是 ``HarnessRegistry`` 里 ``middleware:reme_memory`` 这条登记项的实现
    （``harness_kit/registry.py`` 的 ``_register_middlewares``）。没有它，
    ``profile.memory`` 只是一份"声明"：``HarnessBuilder.build_memory`` 会把
    ReMe 客户端建出来放进 :class:`~harness_kit.config.builder.BuiltHarness`，
    但 **Agent 的中间件链里没有任何东西去检索/注入** —— 典型的"配了但没接线"。

    **参数的取值优先级**（高 → 低）：

    1. ``MiddlewareSpec.params``（Profile 里那一段 ``params:``）；
    2. Profile 的 ``memory:`` 块（``MemorySpec``）；
    3. 代码里的默认值。

    **``workspace_dir`` 必须与写入侧一致**。检索读的是 ReMe 工作区里的
    索引文件，而写入侧（``MemoryIngestor`` / ``MemoryMaintainer``）用的是
    ``HarnessMemoryConfig.from_spec(profile.memory)`` 解析出的
    ``MemorySpec.workspace_root``。两者指向同一个目录，索引才是互通的 ——
    这也是这里默认直接沿用 ``memory.workspace_root`` 的原因。

    Args:
        spec (`Any`): ``MiddlewareSpec``（取 ``params``）。
        ctx (`Any | None`): ``BuildContext``；取 ``profile`` / ``settings`` /
            ``environ``。为 ``None`` 时只能用 ``params`` 里的显式值。

    Returns:
        `LongTermMemoryMiddleware`: 已配置但**尚未启动**的中间件
            （ReMe app 在第一次 hook 调用时惰性构造）。

    Raises:
        `ValueError`: ``params`` 里有未知键，或 ``mode`` / ``top_k`` 非法。
    """
    params = dict(getattr(spec, "params", None) or {})
    unknown = sorted(set(params) - set(_MEMORY_MIDDLEWARE_PARAMS))
    if unknown:
        raise ValueError(
            "middleware:reme_memory 收到未知参数: "
            + ", ".join(unknown)
            + "；可用: "
            + ", ".join(_MEMORY_MIDDLEWARE_PARAMS),
        )

    settings = getattr(ctx, "settings", None)
    memory_spec = getattr(getattr(ctx, "profile", None), "memory", None)

    # -- workspace_dir -------------------------------------------------
    raw_dir = params.get("workspace_dir")
    if raw_dir is None and memory_spec is not None:
        raw_dir = getattr(memory_spec, "workspace_root", None)
    raw_dir = raw_dir or "./.harness/reme"
    if settings is not None and not str(raw_dir).startswith("/"):
        workspace_dir = str(settings.resolve(raw_dir))
    else:
        workspace_dir = str(raw_dir)

    # -- 其余参数 -------------------------------------------------------
    mode = params.get("mode", getattr(memory_spec, "mode", "static_control"))
    if mode not in ("static_control", "agent_control", "both"):
        raise ValueError(
            f"memory mode 只能是 static_control / agent_control / both，收到 {mode!r}"
            "（语义见 third_party/agentscope/src/agentscope/middleware/"
            "_longterm_memory/_reme/_middleware.py:19-27）",
        )

    top_k = int(params.get("top_k", getattr(memory_spec, "top_k", 5)))
    if top_k <= 0:
        raise ValueError(f"top_k 必须为正，收到 {top_k}")

    min_score = float(params.get("min_score", getattr(memory_spec, "min_score", 0.0)))

    budget_tokens = params.get("budget_tokens", getattr(memory_spec, "inject_budget_tokens", 0))
    budget = MemoryBudget(max_tokens=int(budget_tokens)) if budget_tokens else None

    # -- 召回门控（可选）-----------------------------------------------
    # 为什么要一个"0 = 不启用"的哨兵值，而不是"没写这个键就不启用"：
    # Profile 是给人读的配置，`gate_min_score: 0` 这种写法必须**明确地**
    # 表示"我关掉了门控"，而不是因为拼错键名而静默不生效。
    # （键名拼错会在上面的 unknown 检查里直接报错，不会走到这。）
    gate: MemoryGate | None = None
    gate_min_score = float(params.get("gate_min_score", 0.0) or 0.0)
    sensitive_tags = list(params.get("sensitive_tags") or [])
    if gate_min_score > 0 or sensitive_tags:
        gate = MemoryGate(
            # MemoryGate 的 budget 是必填（契约 §3.19）：门控要在预算里
            # 决定"保留哪几条"，所以它自己也会裁一次。没配 budget_tokens 时
            # 给一个默认预算，而不是 None —— 官方实现里"没有长度控制"
            # 正是本讲要修的问题（见模块 docstring 缺口 1）。
            budget=budget if budget is not None else MemoryBudget(),
            min_score=gate_min_score or DEFAULT_MIN_SCORE,
            sensitive_tags=sensitive_tags,
        )

    # -- 写入门控（可选，同样 0 = 不启用）------------------------------
    write_gate: MemoryWriteGate | None = None
    write_min_messages = int(params.get("write_min_messages", 0) or 0)
    write_min_chars = int(params.get("write_min_chars", 12) or 0)
    if write_min_messages > 0:
        write_gate = MemoryWriteGate(
            min_messages=write_min_messages,
            min_chars=write_min_chars,
        )

    kwargs: dict[str, Any] = {
        "workspace_dir": workspace_dir,
        "mode": mode,
        "top_k": top_k,
        "min_score": min_score,
        "budget": budget,
        "gate": gate,
        "write_gate": write_gate,
        "write_async": bool(params.get("write_async", True)),
        "write_timeout_s": float(params.get("write_timeout_s", 30.0) or 30.0),
        "session_tags": tuple(params.get("session_tags") or ()),
    }
    if params.get("tool_context_id") is not None:
        kwargs["tool_context_id"] = params["tool_context_id"]
    if params.get("chat_model") is not None:
        kwargs["chat_model"] = params["chat_model"]

    written = _export_llm_env(getattr(ctx, "environ", None))
    middleware = LongTermMemoryMiddleware(**kwargs)
    logger.bind(
        workspace_dir=workspace_dir,
        mode=mode,
        top_k=top_k,
        min_score=min_score,
        budget_tokens=budget_tokens,
        gate=None if gate is None else gate.min_score,
        sensitive_tags=sensitive_tags,
        write_gate=write_min_messages,
        write_async=kwargs["write_async"],
        llm_env=written,
    ).info("memory middleware 已装配（检索/注入/写回由它负责）")
    return middleware


class CompatReport(BaseModel):
    """``ensure_reme_compat`` 的结果快照（给 doctor / 教程展示用）。

    Attributes:
        patched (`list[str]`): 打上的补丁。
        step (`str`): 被补的 step 名。
    """

    model_config = ConfigDict(extra="forbid")

    patched: list[str] = Field(default_factory=list)
    step: str = _COMPAT_STEP_NAME

    @property
    def ok(self) -> bool:
        """是否打上了补丁。

        Returns:
            `bool`: ``patched`` 非空。
        """
        return bool(self.patched)
