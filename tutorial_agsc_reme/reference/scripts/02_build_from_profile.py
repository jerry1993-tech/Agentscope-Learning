# -*- coding: utf-8 -*-
"""第 2 讲验证脚本之二：从**真实** Profile 装配出一个**真实** Agent 并让它回答。

与 `02_config_and_registry.py` 的分工：那份完全离线（0 次 LLM 调用），
这一份会真的打两次 DeepSeek，用来看 `_reply_impl` 在真实模型下的
事件序列、`cur_iter` 与 `usage`。

它证明的是本讲最容易被忽略的一件事：
**`HarnessBuilder` 装出来的 Agent 不是"我们自己的 Agent"，而是原封不动的
`agentscope.agent.Agent`** —— 它的 `reply` / `reply_stream` / `_next_action` /
中间件分桶 / 权限引擎全都是官方实现，我们只是把构造参数算对了。

用法（`PYTHONPATH` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/02_build_from_profile.py

LLM 调用预算：**2 次**（一次 `reply_stream`、一次 `reply`），低于单脚本 6 次上限。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import harness_kit

REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent
PROFILES = REF / "harness_kit" / "profiles"

from harness_kit.config import load_resolved_profile  # noqa: E402
from harness_kit.config.builder import (  # noqa: E402
    HarnessBuilder,
    summarize_agent,
)
from harness_kit.settings import Settings  # noqa: E402


async def main() -> None:
    settings = Settings.from_env(repo_root=REPO, profile_dir=PROFILES)
    print("settings.has_llm() =", settings.has_llm())
    print("repo_root          =", settings.repo_root)
    print("profile_dir        =", settings.resolve(str(settings.profile_dir)))

    resolved = load_resolved_profile("default", search_dir=PROFILES)
    print("\n--- ResolvedProfile ---")
    print("name          =", resolved.name)
    print("source_chain  =", resolved.source_chain)
    print("model         =", resolved.model.provider, "/", resolved.model.model_name)
    print("tools.packs   =", resolved.tools.packs)
    print("middleware    =", [m.name for m in resolved.middleware])
    print("agent         =", resolved.agent.name, "| max_iters =", resolved.agent.max_iters)

    async with HarnessBuilder(resolved, settings=settings) as builder:
        harness = await builder.build_all()
        print("\n--- summarize_agent() ---")
        for key, value in summarize_agent(harness.agent).items():
            print(f"  {key:15s} = {value}")

        from agentscope.message import UserMsg

        # 第 1 次 LLM 调用：完整走一遍 _reply_impl 的 Reasoning 分支
        print("\n--- reply_stream() 事件序列（连续同类事件已折叠计数）---")
        collapsed: list[tuple[str, int]] = []
        async for event in harness.agent.reply_stream(
            UserMsg("user", "用一句话说明什么是 ReAct 循环，不要调用任何工具。"),
        ):
            name = type(event).__name__
            if collapsed and collapsed[-1][0] == name:
                collapsed[-1] = (name, collapsed[-1][1] + 1)
            else:
                collapsed.append((name, 1))
        for index, (name, times) in enumerate(collapsed, start=1):
            suffix = f" × {times}" if times > 1 else ""
            print(f"  [{index:02d}] {name}{suffix}")

        # 第 2 次 LLM 调用：reply() 与 reply_stream() 的差别
        final = await harness.agent.reply(
            UserMsg("user", "只回答一个词：你好。"),
        )
        print("\nreply() 最终消息:", final.get_text_content())
        print("context 长度   =", len(harness.state.context))
        print("cur_iter       =", harness.state.cur_iter)
        print("usage          =", final.usage.model_dump() if final.usage else None)

    print("\nDONE（本脚本共 2 次 LLM 调用）")


if __name__ == "__main__":
    asyncio.run(main())
