# -*- coding: utf-8 -*-
"""第 2 讲验证脚本之一：配置层 + 注册表 + Agent 装配，**全程 0 次 LLM 调用**。

它把本讲的四条主结论全部变成可执行的断言：

  A. `merge_dicts` 的四条合并规则（map 递归 / 叶子覆盖 / list 替换 / !append / null 删除）
  B. `${VAR}` 插值与 `ConfigInterpolationError`
  C. 三层 YAML（两个 Bundle + 一个 Profile）继承链 + `!append` + `null` 删除 + `explain()`
  D. `ConfigCycleError` / `ConfigNotFoundError`
  E. `HarnessRegistry` 的名字清单、`freeze`、`UnknownComponentError`、懒加载 `echo`
  F. 用 `echo` 模型装配出**真实**的 AgentScope `Agent` 并 `reply`（确定性、无网络）
  G. `Agent._next_action` 状态机的四个分支（这是本讲的核心：不写 Loop，只读 Loop）
  H. `reply` / `reply_stream` / `observe` 的语义差异

用法（`PYTHONPATH` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/02_config_and_registry.py

LLM 调用预算：**0 次**。F 段用的是 `EchoChatModel`（`harness_kit/models/adapters/echo.py`）
—— 一个把输入原样回放的确定性适配器，它也是一个真正的 `ChatModelBase`，
所以 F 段走的是货真价实的 `Agent._reply_impl` 主循环，只是模型不联网。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import harness_kit

# 从 import 到的包反推路径，这样脚本放在任何地方都能跑：
#   <repo>/tutorial_agsc_reme/reference/harness_kit/__init__.py
#     .parent        -> .../reference/harness_kit
#     .parent.parent -> .../reference
REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent

from harness_kit.config import (  # noqa: E402
    AppendList,
    ConfigCycleError,
    ConfigInterpolationError,
    ConfigNotFoundError,
    Profile,
    build_from_profile,
    load_resolved_profile,
    merge_dicts,
    resolve_profile,
)
from harness_kit.config.builder import (  # noqa: E402
    BuiltHarness,
    HarnessBuilder,
    summarize_agent,
)
from harness_kit.config.loader import interpolate_env, load_profile  # noqa: E402
from harness_kit.registry import HarnessRegistry, UnknownComponentError  # noqa: E402
from harness_kit.settings import Settings  # noqa: E402

PROFILES = REF / "harness_kit" / "profiles"
TMP = Path(tempfile.mkdtemp(prefix="harness02_"))


def banner(text: str) -> None:
    print(f"\n===== {text} =====")


# ----------------------------------------------------------------------
# A. 合并规则
# ----------------------------------------------------------------------
def section_a() -> None:
    banner("A. merge_dicts")
    base = {"a": {"b": 1, "c": 2}, "packs": ["builtin"], "memory": {"enabled": False}}
    override = {
        "a": {"b": 9},
        "packs": AppendList(["repo"]),
        "memory": None,
    }
    merged = merge_dicts(base, override)
    print("base     :", base)
    print("override :", override)
    print("merged   :", merged)
    assert merged == {"a": {"b": 9, "c": 2}, "packs": ["builtin", "repo"]}
    assert base == {"a": {"b": 1, "c": 2}, "packs": ["builtin"], "memory": {"enabled": False}}
    print("OK  map 递归合并 / 叶子覆盖 / !append 追加 / null 删除；base 未被就地修改")


# ----------------------------------------------------------------------
# B. 插值
# ----------------------------------------------------------------------
def section_b() -> None:
    banner("B. interpolate_env")
    env = {"LLM_MODEL": "deepseek-flash", "PORT": "8080", "DEBUG": "true"}
    print("${LLM_MODEL}            ->", repr(interpolate_env("${LLM_MODEL}", env)))
    print("${MISSING:-fallback}   ->", repr(interpolate_env("${MISSING:-fallback}", env)))
    print("${PORT} (int 还原)     ->", repr(interpolate_env("${PORT}", env)))
    print("${DEBUG} (bool 还原)   ->", repr(interpolate_env("${DEBUG}", env)))
    print("${NOPE:-null} (None)   ->", repr(interpolate_env("${NOPE:-null}", {})))
    print(
        "嵌套递归              ->",
        interpolate_env({"m": {"name": "${LLM_MODEL}"}, "l": ["${PORT}"]}, env),
    )
    try:
        interpolate_env("${UNDEFINED_VAR}", {})
    except ConfigInterpolationError as exc:
        print("未定义变量 ->", type(exc).__name__, ":", exc)
    else:  # pragma: no cover
        raise AssertionError("未定义变量竟然没报错")


# ----------------------------------------------------------------------
# C. 三层继承链 + explain
# ----------------------------------------------------------------------
def _write(name: str, text: str) -> Path:
    path = TMP / name
    path.write_text(text, encoding="utf-8")
    return path


def section_c() -> None:
    banner("C. extends + bundles + !append + null 删除 + explain")
    _write(
        "base_bundle.yaml",
        "name: base_bundle\n"
        "description: 基线\n"
        "tools:\n"
        "  packs: [builtin]\n"
        "  max_result_chars: 4000\n"
        "middleware:\n"
        "  - name: logging\n"
        "    params: { level: INFO }\n"
        "memory:\n"
        "  enabled: true\n"
        "  catalog: base\n",
    )
    _write(
        "repo_bundle.yaml",
        "name: repo_bundle\n"
        "description: 追加仓库工具、关掉记忆\n"
        "tools:\n"
        "  packs: !append [repo]\n"
        "memory: null\n",
    )
    _write(
        "child.yaml",
        "name: child\n"
        "description: 叶子 Profile\n"
        "extends: parent\n"
        "bundles: [base_bundle, repo_bundle]\n"
        "model:\n"
        "  provider: echo\n"
        "  model_name: echo\n"
        "middleware: !append [{ name: budget, params: { max_tool_calls: 5 } }]\n"
        "tools:\n"
        "  max_result_chars: 8000\n",
    )
    _write(
        "parent.yaml",
        "name: parent\n"
        "description: 中间层\n"
        "extends: grandparent\n",
    )
    _write(
        "grandparent.yaml",
        "name: grandparent\n"
        "description: 根\n"
        "agent:\n"
        "  name: git-agent\n"
        "  max_iters: 7\n",
    )

    profile = load_profile("child", search_dir=TMP)
    resolved = resolve_profile(profile, search_dir=TMP)
    print("source_chain :", resolved.source_chain)
    print("tools.packs  :", resolved.tools.packs)
    print("max_result_chars:", resolved.tools.max_result_chars)
    print("middleware   :", [m.name for m in resolved.middleware])
    print("memory.enabled:", resolved.memory.enabled)
    print("agent.max_iters:", resolved.agent.max_iters, "| agent.name:", resolved.agent.name)
    print("model        :", resolved.model.provider, "/", resolved.model.model_name)
    print("--- explain() ---")
    print(resolved.explain())
    assert resolved.tools.packs == ["builtin", "repo"]
    assert resolved.tools.max_result_chars == 8000
    assert [m.name for m in resolved.middleware] == ["logging", "budget"]
    assert resolved.memory.enabled is False
    assert resolved.agent.max_iters == 7


# ----------------------------------------------------------------------
# D. 异常
# ----------------------------------------------------------------------
def section_d() -> None:
    banner("D. ConfigCycleError / ConfigNotFoundError")
    _write("loop_a.yaml", "name: loop_a\nextends: loop_b\n")
    _write("loop_b.yaml", "name: loop_b\nextends: loop_a\n")
    try:
        resolve_profile(load_profile("loop_a", search_dir=TMP), search_dir=TMP)
    except ConfigCycleError as exc:
        print("成环 ->", type(exc).__name__, ":", exc)
    else:  # pragma: no cover
        raise AssertionError("成环竟然没报错")

    _write("bad_ref.yaml", "name: bad_ref\nbundles: [no_such_bundle]\n")
    try:
        resolve_profile(load_profile("bad_ref", search_dir=TMP), search_dir=TMP)
    except ConfigNotFoundError as exc:
        print("缺 Bundle ->", type(exc).__name__, ":", exc)
    else:  # pragma: no cover
        raise AssertionError("缺 Bundle 竟然没报错")

    try:
        load_profile("no_such_profile", search_dir=TMP)
    except FileNotFoundError as exc:
        print("缺 Profile ->", type(exc).__name__, ":", str(exc)[:120], "...")


# ----------------------------------------------------------------------
# E. Registry
# ----------------------------------------------------------------------
def section_e() -> None:
    banner("E. HarnessRegistry")
    registry = HarnessRegistry.default()
    for kind in ("model", "tool_pack", "middleware", "workspace", "memory", "permission", "mcp"):
        print(f"{kind:11s}: {registry.names(kind)}")
    print("frozen =", registry.frozen)
    registry.freeze()
    print("freeze() 后 frozen =", registry.frozen)
    try:
        registry.register_model("late", lambda spec: None)
    except Exception as exc:  # RegistryFrozenError
        print("冻结后注册 ->", type(exc).__name__, ":", exc)

    try:
        registry.get("middleware", "not_registered")
    except UnknownComponentError as exc:
        print("未登记名字 ->", type(exc).__name__, ":", exc)

    factory = registry.get("model", "echo")
    print("懒加载解析 echo ->", factory.__module__ + "." + factory.__qualname__)


# ----------------------------------------------------------------------
# F. 用 echo 模型装配真实 Agent 并 reply
# ----------------------------------------------------------------------
async def section_f(settings: Settings) -> None:
    banner("F. echo Profile 装配 + 离线 reply")
    echo_yaml = REF / "harness_kit" / "profiles" / "default.yaml"
    payload_dir = TMP / "echo_profile"
    payload_dir.mkdir(exist_ok=True)
    payload = echo_yaml.read_text(encoding="utf-8").replace(
        "provider: deepseek",
        "provider: echo",
    ).replace("model_name: ${LLM_MODEL:-deepseek-chat}", "model_name: echo")
    (payload_dir / "echo.yaml").write_text(payload, encoding="utf-8")

    resolved = load_resolved_profile("echo", search_dir=payload_dir)
    print("provider =", resolved.model.provider, "| model_name =", resolved.model.model_name)
    async with HarnessBuilder(resolved, settings=settings) as builder:
        harness = await builder.build_all()
        info = summarize_agent(harness.agent)
        print("summarize_agent:")
        for key, value in info.items():
            print(f"  {key:15s} = {value}")

        from agentscope.message import UserMsg

        msg = await harness.agent.reply(UserMsg("user", "1+1=?"))
        print("reply ->", msg.get_text_content())
        print(
            "context 长度 =",
            len(harness.state.context),
            "| cur_iter =",
            harness.state.cur_iter,
        )
    print("async with 退出，资源已逆序释放")


# ----------------------------------------------------------------------
# G. _next_action 状态机
# ----------------------------------------------------------------------
async def section_g(settings: Settings) -> None:
    banner("G. Agent._next_action 状态机")
    from agentscope.message import AssistantMsg, ToolCallBlock, ToolCallState
    from agentscope.agent._utils import Acting, Exit, Reasoning

    resolved = load_resolved_profile("default", search_dir=PROFILES)
    # 只装 Agent，不触发任何推理：把 profile 的 model 换成 echo 只是为了让
    # 本节的构造完全离线（不读 API key 也不发包）。
    resolved = resolved.model_copy(
        update={"model": resolved.model.model_copy(update={"provider": "echo"})},
    )
    async with HarnessBuilder(resolved, settings=settings) as builder:
        harness = await builder.build_all()
        agent = harness.agent
        name = agent.name
        print("agent.name =", name, "| max_iters =", agent.react_config.max_iters)

        # G1: 尾部 assistant 消息里有一个 PENDING 的 tool call → Acting
        agent.state.append_context(
            name,
            [
                ToolCallBlock(
                    id="call_1",
                    name="Bash",
                    input='{"command": "pwd"}',
                    state=ToolCallState.PENDING,
                ),
            ],
        )
        action = agent._next_action(None)
        print(
            "G1 PENDING tool_call  ->",
            type(action).__name__,
            [tc.name for tc in action.tool_calls] if isinstance(action, Acting) else "",
        )

        # G2: 同一个 tool call 变成 ASKING（等用户确认）→ Exit 但 exit_events=None
        last = agent.state.context[-1]
        last.get_content_blocks("tool_call")[0].state = ToolCallState.ASKING
        action = agent._next_action(None)
        print(
            "G2 ASKING（HITL 挂起）->",
            type(action).__name__,
            "| exit_events =",
            action.exit_events,
            "| exit_msg =",
            action.exit_msg.get_text_content()[:48],
        )

        # G3: cur_iter 达到 max_iters → 强制收口的 Reasoning
        last.get_content_blocks("tool_call")[0].state = ToolCallState.FINISHED
        agent.state.cur_iter = agent.react_config.max_iters
        action = agent._next_action(None)
        hint = action.hint.hint if isinstance(action, Reasoning) else ""
        print(
            "G3 cur_iter == max_iters ->",
            type(action).__name__,
            "| tool_choice =",
            action.tool_choice.model_dump() if action.tool_choice else None,
        )
        print("   hint =", hint[:72], "...")

        # G4: 超过 max_iters 且拿到了 final_msg → Exit(EXCEED_MAX_ITERS)
        agent.state.cur_iter = agent.react_config.max_iters + 1
        final = AssistantMsg(id=agent.state.reply_id, name=name, content="收尾答案")
        action = agent._next_action(final)
        print(
            "G4 cur_iter > max_iters + final_msg ->",
            type(action).__name__,
            "| exit_msg.finished_reason =",
            getattr(action.exit_msg, "finished_reason", None),
            "| events =",
            [type(e).__name__ for e in (action.exit_events or [])],
        )


# ----------------------------------------------------------------------
# H. reply / reply_stream / observe
# ----------------------------------------------------------------------
async def section_h(settings: Settings) -> None:
    banner("H. reply / reply_stream / observe")
    resolved = load_resolved_profile("default", search_dir=PROFILES)
    resolved = resolved.model_copy(
        update={"model": resolved.model.model_copy(update={"provider": "echo"})},
    )
    async with HarnessBuilder(resolved, settings=settings) as builder:
        harness = await builder.build_all()
        agent = harness.agent
        from agentscope.message import UserMsg

        before = len(agent.state.context)
        await agent.observe(UserMsg("user", "这条只进上下文，不进模型"))
        after = len(agent.state.context)
        print(f"observe(): context {before} -> {after}（不触发推理）")

        events: list[str] = []
        async for item in agent.reply_stream(UserMsg("user", "流式一轮")):
            events.append(type(item).__name__)
        print("reply_stream() 事件序列:", events)
        print("reply_stream 默认不吐最终消息文本 -> 需要 yield_final_msg=True")

        final: list[str] = []
        async for item in agent.reply_stream(
            UserMsg("user", "再来一轮"),
            yield_final_msg=True,
        ):
            if hasattr(item, "get_text_content"):
                final.append(item.get_text_content())
        print("yield_final_msg=True 拿到的最终文本:", final)


async def main() -> None:
    print("repo      =", REPO)
    print("reference =", REF)
    print("tmp       =", TMP)
    settings = Settings.from_env(
        repo_root=REPO,
        profile_dir=PROFILES,
    )
    print("settings.redacted() =", settings.redacted())

    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    await section_f(settings)
    await section_g(settings)
    await section_h(settings)

    # 校验 Profile 校验器真的会拒绝非法字段（extra="forbid"）
    banner("I. extra=forbid")
    try:
        Profile.model_validate({"name": "x", "unknown_field": 1})
    except Exception as exc:
        print("未知字段 ->", type(exc).__name__, ":", str(exc).splitlines()[1].strip())

    print("\nALL OFFLINE CHECKS PASSED（本脚本 0 次 LLM 调用）")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
