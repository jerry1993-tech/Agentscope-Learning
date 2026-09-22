# -*- coding: utf-8 -*-
"""代码助手 Demo 的端到端入口：**索引 → 提问 → 核对引用**。

一次运行做四件事，每件都会打印出来（教程里逐段讲解的就是这四步）：

0. **定根**：``os.chdir(代码库根)`` —— 让模型手里的 ``Grep`` / ``Glob`` /
   ``Read`` 在真实代码上搜（这三个工具没有 ``cwd`` 参数，根就是进程 cwd；
   详见 :func:`run_demo` 里的长注释，以及 :mod:`agent` 的模块 docstring）；
1. **索引**：把 ``third_party/agentscope`` 的一小段子树渲染成 markdown，
   灌进 ReMe 工作区（``--skip-ingest`` 可跳过，复用已有索引）；
2. **装配**：按 ``researcher_with_memory`` Profile 装出 Agent，
   并打印中间件链、权限模式、工具集 —— 这是"治理"看得见的地方；
3. **提问**：先做一次**直接检索**（``recall``，不经过模型），
   再做一次**完整回答**（模型 + 中间件注入 + 可能的记忆检索工具调用）；
4. **核对**：把回答里的 ``[n]`` 引用与第 3 步的检索命中交叉核对。

**为什么第 3 步要分两次**：这是排查"模型没给出处"最快的一刀。
``recall`` 命中 0 条 → 问题在写入侧（文件没入库、工作区不对），
改 prompt 是白费力气；``recall`` 有命中但回答不引用 → 问题在模型/prompt 侧。
把这两件事分开报，"没引用"就不再是一团迷雾。

用法::

    PYTHONPATH=<ReMe>:<reference> python -m harness_kit.demo.code_assistant.main
    PYTHONPATH=... python -m harness_kit.demo.code_assistant.main --skip-ingest \\
        --question "Toolkit.add_tool 在工具重名时会怎样？给出出处"

``PYTHONPATH`` 里的 ``<ReMe>`` 必须放在前面：装好的 ``reme`` 0.3.1.10 是旧的，
``PYTHONPATH=third_party/ReMe`` 才能让 ``import reme`` 拿到 0.4.1.13 那份克隆。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence

from loguru import logger

__all__ = ["build_parser", "main", "run_demo"]


def build_parser() -> argparse.ArgumentParser:
    """构造 Demo 的 argparse 解析器。

    Returns:
        `argparse.ArgumentParser`: 解析器。
    """
    from harness_kit.demo.code_assistant.agent import (
        DEFAULT_ALIAS,
        DEFAULT_INDEX_DIR,
        DEFAULT_QUESTION,
        DEFAULT_TARGET,
    )

    parser = argparse.ArgumentParser(
        prog="code_assistant_demo",
        description="ReMe 索引 + AgentScope 运行时 + harness_kit 治理的端到端 Demo。",
    )
    parser.add_argument("--target", default=None, help=f"要索引的代码目录（默认 <仓库根>/{DEFAULT_TARGET}）")
    parser.add_argument("--index", default=DEFAULT_INDEX_DIR, help="ReMe 工作区（默认 Demo 专用目录）")
    parser.add_argument("--limit", type=int, default=12, help="最多索引几个文件（默认 12）")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="要问的问题")
    parser.add_argument("--profile", default="researcher_with_memory", help="基础 Profile 名")
    parser.add_argument(
        "--code-root",
        default=None,
        help="模型手里 Grep/Glob/Read 的搜索根（默认 <仓库根>；见 run_demo 的说明）",
    )
    parser.add_argument("--skip-ingest", action="store_true", help="跳过索引，直接用已有工作区提问")
    parser.add_argument("--stream", action="store_true", help="流式打印回答")
    parser.add_argument("--top-k", type=int, default=5, help="直接检索取几条")
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    return parser


async def run_demo(options: argparse.Namespace) -> int:
    """跑完整条链路。

    Args:
        options (`argparse.Namespace`): 已解析的参数。

    Returns:
        `int`: ``0`` = 回答通过了引用核对，``1`` = 没通过或链路有错。
    """
    from harness_kit.demo.code_assistant.agent import (
        DEFAULT_ALIAS,
        DEFAULT_TAGS,
        DEFAULT_TARGET,
        build_code_assistant,
        default_settings,
        repo_root,
    )

    settings = default_settings()
    index = Path(options.index)
    if not index.is_absolute():
        index = settings.resolve(index)
    target = Path(options.target) if options.target else repo_root() / Path(DEFAULT_TARGET)

    # ------------------------------------------------------------------
    # 把进程 cwd 切到代码库根：**这是 Demo 里唯一一处"环境准备"**。
    #
    # 为什么必须做：``Grep`` / ``Glob`` / ``Read`` 只收一个 ``backend``，
    # 没有 ``cwd`` 参数，相对路径由 ``await self._backend.getcwd()`` 补全
    # （``third_party/agentscope/src/agentscope/tool/_builtin/_grep.py:222``），
    # 而 ``LocalBackend.getcwd()`` 返回的就是 ``os.getcwd()``
    # （``third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:902``）。
    # 不切 cwd 的话，模型手里的 Grep 会在"你启动 Demo 的那个目录"里搜 ——
    # 实测会直接回一句"本仓库中不存在 AgentScope 的源码文件"，因为搜的是空目录。
    #
    # 为什么放在 Demo 入口而不是库函数里：chdir 会改整个进程的状态，
    # 让 ``build_code_assistant`` 这种可被复用的装配函数偷偷 chdir 是坏味道。
    # 入口脚本才是"决定这次运行在哪儿跑"的地方。
    #
    # 为什么不会破坏路径解析：``Settings.resolve`` 以 ``repo_root`` 为锚
    # （``harness_kit/settings.py:194``），``.env`` 也是绝对路径读入
    # （``harness_kit/settings.py:52``），下面所有路径在 chdir 之前都已算成绝对路径。
    # ------------------------------------------------------------------
    code_root = Path(options.code_root).expanduser().resolve() if options.code_root else repo_root()
    if not code_root.is_dir():
        print(f"错误：--code-root 不是目录: {code_root}", file=sys.stderr)
        return 1
    os.chdir(code_root)

    separator = "=" * 78

    # ---------------------------------------------------------------- 1. 索引
    print(separator)
    print("[1/4] 索引代码库")
    print(separator)
    if options.skip_ingest:
        print(f"  已跳过（--skip-ingest）；复用工作区 {index}")
    else:
        from harness_kit.demo.code_assistant.ingest_repo import ingest_repo

        try:
            report = await ingest_repo(
                target=target,
                workspace=index,
                # 与 ingest_repo CLI 的 --alias 默认值共用同一个常量：两边不一致
                # 会让同一个工作区堆出两套 resource/ 命名空间（见 agent.DEFAULT_ALIAS）。
                alias=DEFAULT_ALIAS,
                limit=options.limit,
                # 与 ingest_repo CLI 的 --tags 默认值共用同一个常量，否则交替跑
                # 两个入口会把对方灌的内容判成"变了"（见 agent.DEFAULT_TAGS）。
                tags=DEFAULT_TAGS,
                settings=settings,
            )
        except (NotADirectoryError, ValueError) as exc:
            print(f"  索引失败：{exc}", file=sys.stderr)
            return 1
        print(f"  目标：{target}")
        for item in report.failed:
            print(f"  [!!] {item}")
        print(f"  {report.summary()}")
        print(f"  工作区：{report.workspace}")

    # ---------------------------------------------------------------- 2. 装配
    print()
    print(separator)
    print("[2/4] 装配 Agent（Profile → HarnessBuilder）")
    print(separator)
    assistant = await build_code_assistant(
        settings=settings,
        index_root=index,
        profile_name=options.profile,
    )
    try:
        profile = assistant.profile
        print(f"  Profile      : {profile.name}")
        print(f"  Agent        : {profile.agent.name}  model={profile.model.model_name}")
        print(f"  中间件链     : {[type(m).__name__ for m in assistant.built.middlewares]}")
        print(f"  权限模式     : {profile.permission.mode}（只读；写操作会被拒绝）")
        print(f"  记忆工作区   : {assistant.index_root}")
        print(f"  工具搜索根   : {code_root}（进程 cwd，见 run_demo 里的说明）")
        tools = _tool_names(assistant.built.agent)
        print(f"  Toolkit 工具 : {tools}")
        # 记忆中间件提供的 memory_search **不在 Toolkit 里**：它是中间件在
        # 每次 reply 时通过 list_tools hook 现挂的
        # （third_party/agentscope/src/agentscope/middleware/_longterm_memory/_reme/_middleware.py:443，
        # mode 为 agent_control / both 时给，static_control 时给空列表）。
        # 所以这里只能列 Toolkit，看不到的那一个要靠第 [3] 步的工具调用记录来确认。
        print(f"  中间件工具   : {await _middleware_tools(assistant)}（每次 reply 现挂）")

        # ------------------------------------------------------------ 3. 提问
        print()
        print(separator)
        print("[3/4] 提问")
        print(separator)
        print(f"  Q: {options.question}")

        print("\n  -- 直接检索（不经过模型）--")
        sources = await assistant.recall(options.question, limit=options.top_k)
        if sources:
            for rank, source in enumerate(sources, start=1):
                print(f"     [{rank}] {source}")
        else:
            print("     （0 条命中：确认上一步真的入库了文件，工作区是否一致）")

        print("\n  -- 完整回答 --")
        answer = await assistant.ask(options.question, stream=options.stream)
        if not options.stream:
            print(f"  A: {answer}")
        calls = assistant.tool_calls()
        print(f"\n  本次回答模型调用的工具: {calls or '（无：纯靠注入的 context 回答）'}")

        # ------------------------------------------------------------ 4. 核对
        print()
        print(separator)
        print("[4/4] 引用核对（回答里的 [n] × 检索命中）")
        print(separator)
        check = assistant.verify_citations(answer)
        print(f"  引用编号     : {check.markers}")
        print(f"  检索命中     : {len(check.sources)} 条（ReMe 工作区路径）")
        for source in check.sources:
            print(f"       - {source}")
        print(f"  对上的来源   : {check.grounded or '（无）'}")
        print(f"  结论         : {check.summary()}")

        await assistant.aclose()
        return 0 if check.ok else 1
    except Exception:
        await assistant.aclose()
        raise


async def _middleware_tools(assistant: object) -> list[str]:
    """列出中间件挂出来的工具名（**不改状态**，只是问一次 ``list_tools``）。

    Args:
        assistant (`object`): :class:`~harness_kit.demo.code_assistant.agent.CodeAssistant`。

    Returns:
        `list[str]`: 工具名列表；中间件没有该 hook 时为空。
    """
    built = getattr(assistant, "built", None)
    names: list[str] = []
    for middleware in getattr(built, "middlewares", []) or []:
        hook = getattr(middleware, "list_tools", None)
        if not callable(hook):
            continue
        for tool in await hook():
            names.append(str(getattr(tool, "name", tool)))
    return names


def _tool_names(agent: object) -> list[str]:
    """列出 Agent 当前可用的工具名（只读枚举，不改动任何东西）。

    Args:
        agent (`object`): AgentScope ``Agent``。

    Returns:
        `list[str]`: 工具名列表。
    """
    toolkit = getattr(agent, "toolkit", None)
    names: list[str] = []
    for group in getattr(toolkit, "tool_groups", []) or []:
        for tool in getattr(group, "tools", []) or []:
            names.append(str(getattr(tool, "name", tool)))
    return names


def main(argv: Sequence[str] | None = None) -> int:
    """Demo 入口。

    Args:
        argv (`Sequence[str] | None`): 参数列表；``None`` 用 ``sys.argv[1:]``。

    Returns:
        `int`: 退出码。
    """
    parser = build_parser()
    options = parser.parse_args(list(argv) if argv is not None else None)
    logger.remove()
    logger.add(sys.stderr, level=options.log_level.upper(), enqueue=False)
    try:
        return asyncio.run(run_demo(options))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - Demo 也要把异常变成退出码 + 一行原因
        logger.error("{}: {}", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
