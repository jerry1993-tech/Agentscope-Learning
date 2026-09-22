# -*- coding: utf-8 -*-
"""第 8 讲补遗：短期上下文压缩的完整验证脚本（默认 **0 次模型调用**）。

跑法（仓库根）::

    PYTHONPATH=third_party/ReMe:tutorial_agsc_reme/reference \\
        /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \\
        tutorial_agsc_reme/reference/scripts/08_context_compaction.py

    加 ``--live`` 才跑 J 段（真实 deepseek-flash，**最多 2 次调用**）。

十段，全部对应 ``harness_kit/middleware/compact.py`` 里的一个真实结论：

- A：``ContextBudgetSpec`` 的默认值与官方 ``ContextConfig`` 默认值**逐字段相等**
  （证明"投影"没有偷偷改语义），以及哪 3 个字段被刻意排除；
- B：``to_context_config()`` / ``build_context_config()`` 的覆盖语义；
- C：``HarnessBuilder(context_budget=...)`` 把 override 送进 ``Agent``
  （``agent.context_config`` 真的变了）—— 这是修好"9 个字段一个都接不出来"的地方；
- D：``registry`` 里新增的 ``compact`` 名字能经 Profile 变成真实中间件实例；
- E：``count_pending_tool_calls`` 的三种情形（无调用 / 悬空 / 配对完成）；
- F：护栏真的短路了 ``next_handler``（spy 计数），并且**不挂中间件时行为不变**；
- G：**离线跑通一次真实压缩**（EchoChatModel 走官方
  ``generate_structured_output`` 那条路），观测到 ``summary`` 与消息条数变化；
- H：``min_messages`` 的省调用逻辑；
- I：观测回调：异步回调被 ``await``；回调抛错被吞掉、不影响主流程；
- J（``--live``）：真实模型压缩一次。

LLM 调用预算：A~I 段 **0 次**；J 段 **1~2 次**。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# 路径：让脚本从任何位置都能 import 到 harness_kit 与 third_party
# ----------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REFERENCE = _HERE.parent.parent
if str(_REFERENCE) not in sys.path:
    sys.path.insert(0, str(_REFERENCE))
for _parent in _REFERENCE.parents:
    _third = _parent / "third_party" / "ReMe"
    if _third.is_dir() and str(_third) not in sys.path:
        sys.path.insert(0, str(_third))
    if (_parent / "third_party" / "agentscope").is_dir():
        break

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(_REFERENCE.parent.parent / ".env", override=False)

from agentscope.agent import Agent, ContextConfig, ReActConfig  # noqa: E402
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, UserMsg  # noqa: E402
from agentscope.tool import Toolkit  # noqa: E402

from harness_kit.config.builder import HarnessBuilder  # noqa: E402
from harness_kit.config.loader import load_resolved_profile  # noqa: E402
from harness_kit.config.schema import ModelSpec  # noqa: E402
from harness_kit.middleware.compact import (  # noqa: E402
    CompactionRecord,
    ContextBudgetSpec,
    ContextCompactionMiddleware,
    build_context_config,
    count_pending_tool_calls,
)
from harness_kit.models.adapters.echo import EchoChatModel  # noqa: E402
from harness_kit.models.factory import build_chat_model  # noqa: E402
from harness_kit.registry import HarnessRegistry  # noqa: E402
from harness_kit.settings import Settings  # noqa: E402

LIVE: bool = "--live" in sys.argv

_PASS: list[str] = []
_FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记一条断言。

    Args:
        name (`str`): 断言名。
        condition (`bool`): 是否为真。
        detail (`str`): 附带信息。
    """
    mark = "[PASS]" if condition else "[FAIL]"
    (_PASS if condition else _FAIL).append(name)
    print(f"  {mark} {name}" + (f"  {detail}" if detail else ""))


def banner(text: str) -> None:
    """打一条分隔标题。

    Args:
        text (`str`): 标题。
    """
    print("=" * 72)
    print(text)


# ----------------------------------------------------------------------
# 公共小工具
# ----------------------------------------------------------------------
def filler(i: int, chars: int) -> Msg:
    """造一条足够长的 user 消息（用来把 token 数顶过触发线）。

    Args:
        i (`int`): 序号。
        chars (`int`): 大致字符数。

    Returns:
        `Msg`: user 消息。
    """
    body = "上下文占位内容" * max(1, chars // 7)
    return Msg(name="user", content=[TextBlock(text=f"[{i}] {body}")], role="user")


def make_agent(
    *,
    middlewares: list[Any] | None = None,
    context_size: int = 8192,
    context_config: ContextConfig | None = None,
    name: str = "compact-probe",
) -> Agent:
    """造一个完全离线的 Agent。

    Args:
        middlewares (`list[Any] | None`): 中间件链。
        context_size (`int`): 回声模型的上下文窗口（token）。
        context_config (`ContextConfig | None`): 默认上下文配置。
        name (`str`): Agent 名。

    Returns:
        `Agent`: 可 ``await agent.compress_context()``。
    """
    return Agent(
        name=name,
        system_prompt="你是一个严谨的中文助手。",
        model=EchoChatModel(stream=False, context_size=context_size),
        toolkit=Toolkit(tools=[]),
        middlewares=list(middlewares or []),
        context_config=context_config,
        react_config=ReActConfig(max_iters=2),
    )


# ----------------------------------------------------------------------
# A. 声明式投影的默认值对齐
# ----------------------------------------------------------------------
def section_a() -> None:
    """A 段：默认值逐字段对齐 + 被刻意排除的字段。"""
    banner("A. ContextBudgetSpec 的默认值 vs 官方 ContextConfig")

    official = ContextConfig()
    spec = ContextBudgetSpec()
    projected = spec.to_context_config()

    # spec 上的字段名 -> 官方字段名。唯一一处改名是"加单位后缀"，
    # 因为 `tool_result_limit` 的官方单位本来就是 token，而 spec 里
    # 同时还有 3 个比例字段，不写单位会误读。
    name_map: dict[str, str] = {f: f for f in type(spec).model_fields}
    name_map["tool_result_limit_tokens"] = "tool_result_limit"

    official_fields = set(type(official).model_fields)
    mapped = set(name_map.values())
    print(f"  官方 ContextConfig 字段（{len(official_fields)} 个）：{sorted(official_fields)}")
    print(f"  被投影的字段（{len(mapped)} 个）：{sorted(mapped)}")
    excluded = sorted(official_fields - mapped)
    print(f"  刻意不投影的字段（{len(excluded)} 个）：{excluded}")

    check("A1 官方 ContextConfig 共 10 个字段", len(official_fields) == 10)
    check(
        "A2 投影覆盖 7 个字段，剩 3 个是 prompt/schema 对象",
        mapped == official_fields - {"compression_prompt", "summary_template", "summary_schema"},
        f"excluded={excluded}",
    )

    same: list[str] = []
    diff: list[str] = []
    for spec_field, official_field in sorted(name_map.items()):
        left, right = getattr(projected, official_field), getattr(official, official_field)
        if left == right:
            same.append(official_field)
        else:
            diff.append(f"{official_field}: {left!r} != {right!r}")
    print(f"  默认值相等的字段：{same}")
    check("A3 投影后的默认值与官方逐字段相等", not diff, f"不一致={diff}")
    check(
        "A4 未显式给 tool_result_limit_tokens 时，token 上限沿用官方默认",
        projected.tool_result_limit == official.tool_result_limit,
        f"{projected.tool_result_limit}",
    )

    # 这个中间件只该进一条链 —— 这是 HarnessMiddleware docstring 里点名的坑。
    mw = ContextCompactionMiddleware()
    print(f"  ContextCompactionMiddleware().implemented_hooks() = {mw.implemented_hooks()}")
    check(
        "A5 只实现 on_compress_context 一条链（没被基类误判成实现了全部 7 条）",
        mw.implemented_hooks() == ["on_compress_context"],
    )
    check("A6 is_implemented('on_reply') 为假", not mw.is_implemented("on_reply"))


# ----------------------------------------------------------------------
# B. 覆盖语义
# ----------------------------------------------------------------------
def section_b() -> None:
    """B 段：spec → ContextConfig 的覆盖语义。"""
    banner("B. to_context_config() / build_context_config() 的覆盖语义")

    spec = ContextBudgetSpec(
        trigger_ratio=0.5,
        reserve_ratio=0.05,
        context_buffer_ratio=0.1,
        compression_tool_enabled=True,
        compression_fallback_to_truncation=False,
        max_image_num=2,
        tool_result_limit_tokens=1234,
    )
    cfg = spec.to_context_config()
    print(
        f"  to_context_config(): trigger={cfg.trigger_ratio} reserve={cfg.reserve_ratio} "
        f"buffer={cfg.context_buffer_ratio} tool_enabled={cfg.compression_tool_enabled} "
        f"fallback={cfg.compression_fallback_to_truncation} max_image={cfg.max_image_num} "
        f"tool_result_limit={cfg.tool_result_limit}",
    )
    check("B1 六个标量字段原样翻译", cfg.trigger_ratio == 0.5 and cfg.reserve_ratio == 0.05)
    check("B2 tool_result_limit_tokens 落到 tool_result_limit", cfg.tool_result_limit == 1234)
    check(
        "B3 未投影的 3 个字段仍是官方默认",
        cfg.compression_prompt == ContextConfig().compression_prompt
        and cfg.summary_schema == ContextConfig().summary_schema,
    )

    # build_context_config 的第二个参数是"第 2 讲那条路"进来的覆盖。
    overridden = build_context_config(spec, tool_result_limit_tokens=999)
    check(
        "B4 显式 tool_result_limit_tokens 覆盖 spec 里的值",
        overridden.tool_result_limit == 999,
        f"{overridden.tool_result_limit}",
    )
    plain = build_context_config()
    check(
        "B5 spec=None 且不给 token 上限时，等于官方默认",
        plain.trigger_ratio == ContextConfig().trigger_ratio
        and plain.tool_result_limit == ContextConfig().tool_result_limit,
    )

    # pydantic 的边界：官方给的比例约束在这里也要挡住。
    for bad_kwargs, why in (
        ({"trigger_ratio": 0.95}, "trigger_ratio 上限 0.9"),
        ({"reserve_ratio": 0.95}, "reserve_ratio 上限 <0.9"),
        ({"max_image_num": -1}, "max_image_num 下界 0"),
    ):
        try:
            ContextBudgetSpec(**bad_kwargs)
        except Exception as error:  # pydantic.ValidationError
            check(f"B6 非法值被拒：{why}", True, type(error).__name__)
        else:
            check(f"B6 非法值被拒：{why}", False, "居然通过了")
    try:
        ContextCompactionMiddleware(min_messages=0)
    except ValueError:
        check("B7 min_messages=0 被拒", True)
    else:
        check("B7 min_messages=0 被拒", False, "居然通过了")


# ----------------------------------------------------------------------
# C. HarnessBuilder：override 真的送进了 Agent
# ----------------------------------------------------------------------
PROFILE_YAML: str = """
name: lesson8_compact_demo
description: 第 8 讲补遗验证用 Profile：回声模型 + compact 中间件。
model:
  provider: echo
  model_name: echo
  stream: false
tools:
  packs: [builtin]
  max_result_chars: 8000
middleware:
  - name: compact
    params: { block_when_pending_tools: true, min_messages: 2 }
memory:
  enabled: false
agent:
  name: lesson8-compact-agent
  sys_prompt: "你是一个严谨的中文助手，回答尽量简短。"
  max_iters: 4
"""


async def section_c() -> None:
    """C 段：HarnessBuilder 的 context_budget override。"""
    banner("C. HarnessBuilder(context_budget=...) 送进 Agent.context_config")

    settings = Settings.from_env()
    workdir = Path(tempfile.mkdtemp(prefix="lesson8_compact_"))
    profile_path = workdir / "lesson8_compact.yaml"
    profile_path.write_text(PROFILE_YAML, encoding="utf-8")
    profile = load_resolved_profile(profile_path, search_dir=workdir)

    # C1：不传 override —— 只有 tool_result_limit 被换算出来（第 2 讲的老行为）。
    plain_builder = HarnessBuilder(profile, settings=settings, registry=HarnessRegistry.default())
    plain_cfg = plain_builder._build_context_config()  # pylint: disable=protected-access
    expected_tokens = max(1, profile.tools.max_result_chars // 4)
    print(
        f"  不传 override：tool_result_limit={plain_cfg.tool_result_limit} "
        f"trigger={plain_cfg.trigger_ratio}（8000/4={expected_tokens}）",
    )
    check(
        "C1 不传 override 时只换算 tool_result_limit，其余走官方默认",
        plain_cfg.tool_result_limit == expected_tokens
        and plain_cfg.trigger_ratio == ContextConfig().trigger_ratio,
    )

    # C2：传 override —— 其余字段真的变了。
    spec = ContextBudgetSpec(trigger_ratio=0.6, reserve_ratio=0.08, max_image_num=1)
    builder = HarnessBuilder(
        profile,
        settings=settings,
        registry=HarnessRegistry.default(),
        context_budget=spec,
    )
    cfg = builder._build_context_config()  # pylint: disable=protected-access
    print(
        f"  传 override：tool_result_limit={cfg.tool_result_limit} "
        f"trigger={cfg.trigger_ratio} reserve={cfg.reserve_ratio} max_image={cfg.max_image_num}",
    )
    check(
        "C2 override 的 3 个字段生效，且 tool_result_limit 仍由 max_result_chars 换算",
        cfg.trigger_ratio == 0.6
        and cfg.reserve_ratio == 0.08
        and cfg.max_image_num == 1
        and cfg.tool_result_limit == expected_tokens,
    )

    # C3：端到端 —— Agent 自己读到的 context_config 就是这份。
    agent = await builder.build_agent()
    print(
        f"  agent.context_config: trigger={agent.context_config.trigger_ratio} "
        f"reserve={agent.context_config.reserve_ratio} "
        f"tool_result_limit={agent.context_config.tool_result_limit}",
    )
    check(
        "C3 build_agent() 之后 Agent 读到的就是 override 后的配置",
        agent.context_config.trigger_ratio == 0.6
        and agent.context_config.tool_result_limit == expected_tokens,
    )
    chain = [type(m).__name__ for m in getattr(agent, "_compress_context_middlewares")]
    print(f"  Agent 真实的 on_compress_context 链 = {chain}")
    check("C4 Profile 里的 compact 中途件真的进了压缩链", chain == ["ContextCompactionMiddleware"])

    await builder.aclose()
    await plain_builder.aclose()


# ----------------------------------------------------------------------
# D. registry 与 Profile
# ----------------------------------------------------------------------
async def section_d() -> None:
    """D 段：registry 里的 compact 名字 → 真实实例。"""
    banner("D. registry：Profile 写 compact 就能装配")

    registry = HarnessRegistry.default()
    print(f"  registry 里的中间件名（registry.names('middleware')）=")
    try:
        names = registry.names("middleware")
    except Exception:  # pylint: disable=broad-exception-caught
        names = sorted(registry._factories)  # pylint: disable=protected-access
    print(f"    {names}")
    check("D1 registry 里有 compact", "compact" in names)

    settings = Settings.from_env()
    workdir = Path(tempfile.mkdtemp(prefix="lesson8_compact_d_"))
    profile_path = workdir / "lesson8_compact.yaml"
    profile_path.write_text(PROFILE_YAML, encoding="utf-8")
    profile = load_resolved_profile(profile_path, search_dir=workdir)
    builder = HarnessBuilder(profile, settings=settings, registry=HarnessRegistry.default())
    built = await builder.build_middlewares()
    print(f"  build_middlewares() -> {[type(m).__name__ for m in built]}")
    check("D2 装出的是 ContextCompactionMiddleware", isinstance(built[0], ContextCompactionMiddleware))
    print(
        f"  参数确实从 YAML 进来：block_when_pending_tools="
        f"{built[0].block_when_pending_tools} min_messages={built[0].min_messages}",
    )
    check(
        "D3 YAML 里的 params 生效",
        built[0].block_when_pending_tools is True and built[0].min_messages == 2,
    )
    await builder.aclose()

    # 参数写错时是"构造期就炸"，不是"跑到一半才炸"。
    bad_yaml = PROFILE_YAML.replace(
        "params: { block_when_pending_tools: true, min_messages: 2 }",
        "params: { min_messages: 0 }",
    )
    bad_path = workdir / "bad.yaml"
    bad_path.write_text(bad_yaml, encoding="utf-8")
    bad_profile = load_resolved_profile(bad_path, search_dir=workdir)
    bad_builder = HarnessBuilder(bad_profile, settings=settings, registry=HarnessRegistry.default())
    try:
        await bad_builder.build_middlewares()
    except Exception as error:  # noqa: BLE001
        check("D4 非法参数在装配期就报错", True, f"{type(error).__name__}: {error}")
    else:
        check("D4 非法参数在装配期就报错", False, "居然装配成功了")
    await bad_builder.aclose()


# ----------------------------------------------------------------------
# E. count_pending_tool_calls
# ----------------------------------------------------------------------
async def section_e() -> None:
    """E 段：悬空工具调用的计数。"""
    banner("E. count_pending_tool_calls")

    agent = make_agent()
    await agent.observe([filler(0, 200)])
    check("E1 上下文里没有工具调用时是 0", count_pending_tool_calls(agent) == 0)

    agent.state.context.append(
        Msg(
            name="assistant",
            content=[ToolCallBlock(id="call-1", name="get_time", input="{}")],
            role="assistant",
        ),
    )
    check("E2 发起了但没结果 -> 1", count_pending_tool_calls(agent) == 1)

    # 工具结果是**以 Agent 自己的 assistant 消息**存进 context 的
    # （``Agent._save_to_context`` → ``state.append_context(self.name, blocks)``，
    # ``third_party/agentscope/src/agentscope/agent/_agent.py:3473``）——
    # 不是 OpenAI 意义上的 role="tool"，也不是 role="system"
    # （``Msg`` 的校验器直接拒收带工具块的非文本 system 消息）。
    agent.state.context.append(
        Msg(
            name=agent.name,
            content=[ToolResultBlock(id="call-1", name="get_time", output="12:00")],
            role="assistant",
        ),
    )
    check("E3 id 配对完成后回到 0", count_pending_tool_calls(agent) == 0)

    # 顺序无关：并发执行工具时结果顺序不保证，所以这里只做 id 集合差。
    agent.state.context.append(
        Msg(
            name="assistant",
            content=[
                ToolCallBlock(id="call-a", name="t", input="{}"),
                ToolCallBlock(id="call-b", name="t", input="{}"),
            ],
            role="assistant",
        ),
    )
    agent.state.context.append(
        Msg(
            name=agent.name,
            content=[ToolResultBlock(id="call-b", name="t", output="ok")],
            role="assistant",
        ),
    )
    check("E4 乱序返回也算得准（call-a 仍悬空）", count_pending_tool_calls(agent) == 1)

    # 读不到 context 时返回 0（不拦），而不是抛错。
    class _Blind:
        """没有 state.context 的替身。"""

    check("E5 读不到 context 时返回 0（护栏不变成路障）", count_pending_tool_calls(_Blind()) == 0)

    # 与官方的对应物比一比覆盖面：官方的 ``get_unfinished_tool_calls``
    # 只看 context[-1]，而且要求 ``context[-1].id == state.reply_id``
    # （third_party/agentscope/src/agentscope/state/_state.py:374-403）。
    # 把悬空调用放在**更早**的消息里，官方看不见，我们看得见。
    agent2 = make_agent(name="scope-probe")
    await agent2.observe([filler(0, 200)])
    agent2.state.context.append(
        Msg(
            name="scope-probe",
            content=[ToolCallBlock(id="old-call", name="t", input="{}")],
            role="assistant",
        ),
    )
    await agent2.observe([filler(1, 200)])  # 之后又来了一条 user 消息
    official = agent2.state.get_unfinished_tool_calls(agent2.name)
    ours = count_pending_tool_calls(agent2)
    print(f"  悬空调用在更早的消息里：官方 get_unfinished_tool_calls = {len(official)} 条，我们 = {ours} 条")
    check("E6 官方判据够不着的情形我们数得出来", len(official) == 0 and ours == 1)


# ----------------------------------------------------------------------
# F. 护栏
# ----------------------------------------------------------------------
async def section_f() -> None:
    """F 段：悬空时短路 next_handler；不挂中间件时行为不变。"""
    banner("F. 护栏：悬空工具调用时拒绝压缩")

    calls = {"n": 0}

    async def spy_next_handler(**_kwargs: Any) -> None:
        """替身 next_handler。"""
        calls["n"] += 1

    mw = ContextCompactionMiddleware()
    agent = make_agent(middlewares=[mw])
    await agent.observe([filler(0, 400), filler(1, 400)])
    agent.state.context.append(
        Msg(
            name="assistant",
            content=[ToolCallBlock(id="call-1", name="get_time", input="{}")],
            role="assistant",
        ),
    )
    await mw.on_compress_context(
        agent=agent,
        input_kwargs={"context_config": None, "instructions": None},
        next_handler=spy_next_handler,
    )
    print(f"  next_handler 调用次数 = {calls['n']}，记录 = {[r.to_line() for r in mw.records]}")
    check("F1 悬空时 next_handler 一次都没被调用", calls["n"] == 0)
    check(
        "F2 记录里的原因是 pending_tool_calls",
        mw.records and mw.records[-1].skipped_reason == "pending_tool_calls(1)",
    )

    # 配对完成后再来一次：这次必须放行。
    agent.state.context.append(
        Msg(
            name=agent.name,
            content=[ToolResultBlock(id="call-1", name="get_time", output="12:00")],
            role="assistant",
        ),
    )
    await mw.on_compress_context(
        agent=agent,
        input_kwargs={"context_config": None, "instructions": None},
        next_handler=spy_next_handler,
    )
    print(f"  配对完成后 next_handler 调用次数 = {calls['n']}")
    check("F3 配对完成后放行 next_handler", calls["n"] == 1)

    # 关掉护栏：悬空时也放行（把选择权还给使用者）。
    calls["n"] = 0
    loose = ContextCompactionMiddleware(block_when_pending_tools=False)
    agent2 = make_agent(middlewares=[loose])
    await agent2.observe([filler(0, 400)])
    agent2.state.context.append(
        Msg(
            name="assistant",
            content=[ToolCallBlock(id="call-9", name="get_time", input="{}")],
            role="assistant",
        ),
    )
    await loose.on_compress_context(
        agent=agent2,
        input_kwargs={"context_config": None, "instructions": None},
        next_handler=spy_next_handler,
    )
    check("F4 block_when_pending_tools=False 时放行", calls["n"] == 1)

    # 不挂中间件时，Agent 的链路恒为空 —— 护栏是纯增量，默认什么都不改。
    bare = make_agent()
    check(
        "F5 不挂中间件时压缩链为空（默认行为一字不改）",
        list(getattr(bare, "_compress_context_middlewares")) == [],
    )


# ----------------------------------------------------------------------
# G. 真实压缩（离线）
# ----------------------------------------------------------------------
async def section_g() -> None:
    """G 段：离线跑通一次真实压缩。"""
    banner("G. 离线跑通一次真实压缩（0 次 LLM 调用）")

    records: list[CompactionRecord] = []
    mw = ContextCompactionMiddleware(on_compaction=records.append)
    agent = make_agent(middlewares=[mw], context_size=8192)
    n_input = 5
    await agent.observe([filler(i, 2000) for i in range(n_input)])
    before = len(agent.state.context)
    tokens = await agent.model.count_tokens(
        [m for m in agent.state.context],
        None,
    )
    threshold = 0.05 * 8192
    print(f"  压缩前 msgs={before} 估算 tokens={tokens} 触发线(0.05*8192)={threshold}")

    await agent.compress_context(context_config=ContextConfig(trigger_ratio=0.05, reserve_ratio=0.01))

    after = len(agent.state.context)
    summary = agent.state.summary or ""
    print(f"  压缩后 msgs={after} summary={len(summary)} 字符")
    print(f"  summary 开头：{summary[:100].replace(chr(10), ' ')}")
    print(f"  记录：{[r.to_line() for r in mw.records]}")

    check("G1 真的触发了压缩", bool(mw.records) and mw.records[-1].compressed)
    check("G2 压缩后消息条数变少", after < before, f"{before} -> {after}")
    check("G3 summary 非空（摘要真被生成出来）", len(summary) > 0)
    check(
        "G4 CompactionRecord 的字段自洽",
        mw.records[-1].n_msgs_before == before and mw.records[-1].n_msgs_after == after,
    )
    check("G5 观测回调拿到了同一条记录", len(records) == 1 and records[0].compressed)
    check("G6 回调对象与 records 列表是同一份数据", records[0] is mw.records[0])

    # 没到触发线时不能压 —— 这时候官方实现自己就会提前 return，
    # 中间件如实记成 noop。min_messages 设 1 是为了让这一轮真的走到实现里，
    # 否则会被 H 段那条"消息太少"的省调用逻辑先拦下
    # （默认 min_messages=2 时，1 条消息的上下文会记成 too_few_messages，
    # 两种原因都是"跳过"，但属于不同的判据，别混为一谈）。
    mw2 = ContextCompactionMiddleware(min_messages=1)
    agent2 = make_agent(middlewares=[mw2], context_size=8192)
    await agent2.observe([filler(0, 100)])
    await agent2.compress_context()  # 用 Agent 自己的默认 trigger_ratio=0.8
    print(f"  未到触发线：{[r.to_line() for r in mw2.records]}")
    check(
        "G7 未到触发线时 compressed=False，原因是 noop 而不是护栏",
        bool(mw2.records)
        and not mw2.records[-1].compressed
        and mw2.records[-1].skipped_reason.startswith("noop"),
    )
    check(
        "G8 未达触发线时 state.context 一字未动",
        len(agent2.state.context) == 1 and not agent2.state.summary,
    )


# ----------------------------------------------------------------------
# H. min_messages
# ----------------------------------------------------------------------
async def section_h() -> None:
    """H 段：min_messages 省掉无意义的调用。"""
    banner("H. min_messages：消息太少就别问了")

    mw = ContextCompactionMiddleware(min_messages=10)
    agent = make_agent(middlewares=[mw])
    await agent.observe([filler(0, 300), filler(1, 300)])
    await mw.on_compress_context(
        agent=agent,
        input_kwargs={"context_config": None, "instructions": None},
        next_handler=lambda **_: None,  # type: ignore[arg-type,return-value]
    )
    print(f"  {[r.to_line() for r in mw.records]}")
    check(
        "H1 条数不足时跳过，原因是 too_few_messages",
        mw.records and mw.records[-1].skipped_reason == "too_few_messages(<10)",
    )
    check("H2 跳过时 elapsed_ms 很小（没干活）", mw.records[-1].elapsed_ms < 50)


# ----------------------------------------------------------------------
# I. 观测回调
# ----------------------------------------------------------------------
async def section_i() -> None:
    """I 段：回调的异步与容错。"""
    banner("I. 观测回调：异步被 await、抛错被吞")

    seen: list[str] = []

    async def async_cb(record: CompactionRecord) -> None:
        """异步回调。"""
        await asyncio.sleep(0)
        seen.append(record.skipped_reason or "compressed")

    mw = ContextCompactionMiddleware(min_messages=99, on_compaction=async_cb)
    agent = make_agent(middlewares=[mw])
    await agent.observe([filler(0, 100)])
    await mw.on_compress_context(
        agent=agent,
        input_kwargs={"context_config": None, "instructions": None},
        next_handler=lambda **_: None,  # type: ignore[arg-type,return-value]
    )
    check("I1 异步回调被 await 到了", seen == [mw.records[-1].skipped_reason], f"{seen}")

    def sync_cb(record: CompactionRecord) -> None:  # noqa: ARG001
        """同步回调。"""
        seen.append("sync")

    mw2 = ContextCompactionMiddleware(min_messages=99, on_compaction=sync_cb)
    agent2 = make_agent(middlewares=[mw2])
    await agent2.observe([filler(0, 100)])
    await mw2.on_compress_context(
        agent=agent2,
        input_kwargs={"context_config": None, "instructions": None},
        next_handler=lambda **_: None,  # type: ignore[arg-type,return-value]
    )
    check("I2 同步回调也能用", seen[-1] == "sync")

    async def boom(_record: CompactionRecord) -> None:
        """故意抛错。"""
        raise RuntimeError("观测炸了")

    mw3 = ContextCompactionMiddleware(min_messages=99, on_compaction=boom)
    agent3 = make_agent(middlewares=[mw3])
    await agent3.observe([filler(0, 100)])
    try:
        await mw3.on_compress_context(
            agent=agent3,
            input_kwargs={"context_config": None, "instructions": None},
            next_handler=lambda **_: None,  # type: ignore[arg-type,return-value]
        )
    except RuntimeError:
        check("I3 回调抛错被吞掉（不会炸主流程）", False, "异常逃出来了")
    else:
        check("I3 回调抛错被吞掉（不会炸主流程）", True)
    check("I4 尽管回调炸了，记录还是留下了", len(mw3.records) == 1)


# ----------------------------------------------------------------------
# J. 真模型（--live）
# ----------------------------------------------------------------------
async def section_j() -> None:
    """J 段：真实 deepseek-flash 压缩一次（1~2 次调用）。"""
    banner("J. 真实 deepseek-flash 压缩（--live，1~2 次 LLM 调用）")

    settings = Settings.from_env()
    model = build_chat_model(
        ModelSpec(
            provider="deepseek",
            model_name=settings.llm_model_name or "deepseek-flash",
            api_key_env="OPENAI_API_KEY",
            base_url_env="OPENAI_BASE_URL",
        ),
        settings=settings,
    )
    size = getattr(model, "context_size", None)
    print(f"  真实模型 context_size = {size}")

    mw = ContextCompactionMiddleware()
    agent = Agent(
        name="harness-live-compact",
        system_prompt="你是一个严谨的中文助手。",
        model=model,
        toolkit=Toolkit(tools=[]),
        middlewares=[mw],
        react_config=ReActConfig(max_iters=2),
    )
    await agent.observe(
        [
            UserMsg(
                "user",
                "请记住这条事实：" + "第 8 讲要讲中间件与 Hook 链。" * 200,
            ),
            UserMsg("user", "再记住：压缩要留摘要。" * 200),
        ],
    )
    await agent.compress_context(
        context_config=ContextConfig(trigger_ratio=0.05, reserve_ratio=0.01),
    )
    print(f"  记录：{[r.to_line() for r in mw.records]}")
    summary = agent.state.summary or ""
    print(f"  摘要开头：{summary[:200].replace(chr(10), ' ')}")
    check("J1 真实模型上也真的压缩了", bool(mw.records) and mw.records[-1].compressed)
    check("J2 摘要非空", len(summary) > 0)
    check("J3 summary 是模型写的而不是回声占位", "[echo]" not in summary)


async def main() -> int:
    """跑全部章节。

    Returns:
        `int`: 进程退出码。
    """
    section_a()
    section_b()
    await section_c()
    await section_d()
    await section_e()
    await section_f()
    await section_g()
    await section_h()
    await section_i()
    if LIVE:
        await section_j()
    else:
        banner("J. 被跳过（没有 --live）")
        print("  加上 --live 会真实调用 deepseek-flash 压缩一次")

    banner("汇总")
    print(f"  PASS {len(_PASS)} 项")
    if _FAIL:
        print(f"  FAIL {len(_FAIL)} 项：")
        for name in _FAIL:
            print(f"    - {name}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    logger.remove()
    raise SystemExit(asyncio.run(main()))
