#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 7 讲演示入口：把 harness_kit 的工具暴露成一个真实的 MCP stdio server。

被 ``harness_kit.mcp.registry.MCPServerSpec(transport="stdio", command=..., args=[本文件])``
以子进程方式拉起，所以这个文件必须**零参数、直接 run**。

用法（手工调试）：

.. code-block:: bash

    # stdio：会被 AgentScope 的 MCPClient 当子进程拉起，手跑时表现为"没反应"（在等握手）
    PYTHONPATH=<repo>/tutorial_agsc_reme/reference \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \\
      <repo>/tutorial_agsc_reme/reference/scripts/mcp_demo_server.py

    # streamable-http：起在 18100 端口，验证完立刻 Ctrl-C
    ... mcp_demo_server.py --http

暴露的工具：

- ``harness_now`` —— 当前时间（带时区偏移）；
- ``harness_calc`` —— 安全的四则运算求值（ast 白名单，不用 eval）；
- ``harness_env_info`` —— 解释器版本与已安装的 agentscope / reme 版本。

三个都来自 ``harness_kit.tools.builtin_pack``，**没有一行是本文件重写的**。
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from pathlib import Path

# 允许直接 `python scripts/mcp_demo_server.py`（不依赖已安装 harness_kit）
_REFERENCE_ROOT = Path(__file__).resolve().parents[1]
if str(_REFERENCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_REFERENCE_ROOT))

from agentscope.tool import FunctionTool, ToolBase  # noqa: E402

from harness_kit.mcp.server import build_mcp_server  # noqa: E402
from harness_kit.tools.builtin_pack import calc, now  # noqa: E402


def _quiet_logging(level: str = "WARNING") -> None:
    """把子进程的日志压到 ``level``，否则它会污染父进程的标准输出。

    stdio 传输下，这个 server 是父进程（``MCPClient``）拉起来的子进程，
    **stderr 被子进程继承**，于是它每处理一个请求就打的
    ``Processing request of type ListToolsRequest`` 会直接混进父进程的输出。
    父进程跑的是"验证脚本 + 断言"，混进日志就没法读了。

    可控：``HARNESS_LOG_LEVEL=DEBUG`` 时把 SDK 的日志放回来（调试握手时有用）。

    Args:
        level (`str`): 标准库日志级别名。
    """
    logging.basicConfig(level=getattr(logging, level.upper(), logging.WARNING))
    for name in ("mcp", "httpx", "httpcore", "anyio", "asyncio"):
        logging.getLogger(name).setLevel(getattr(logging, level.upper(), 30))
    try:
        from loguru import logger

        logger.remove()
        logger.add(
            sys.stderr,
            level=os.getenv("HARNESS_LOG_LEVEL", level).upper(),
        )
    except ImportError:  # pragma: no cover - loguru 是硬依赖，这里只是兜底
        pass


def env_info() -> str:
    """Return the interpreter and harness dependency versions.

    Returns:
        `str`: 形如 ``python=3.11.13 agentscope=2.0.8 reme=0.4.1.13``。
    """
    from importlib.metadata import PackageNotFoundError, version

    def _v(name: str) -> str:
        """读取一个包的版本。

        Args:
            name (`str`): 包名。

        Returns:
            `str`: 版本号，缺失时为 ``"<missing>"``。
        """
        try:
            return version(name)
        except PackageNotFoundError:
            return "<missing>"

    return (
        f"python={platform.python_version()} "
        f"agentscope={_v('agentscope')} reme={_v('reme')}"
    )


def build_tools() -> list[ToolBase]:
    """构造要暴露的工具列表。

    ``FunctionTool`` 会从 docstring 解析出 JSON schema
    （``third_party/agentscope/src/agentscope/tool/_adapters.py`` 的
    ``FunctionTool``），所以这三个函数必须写完整的 ``Args`` / ``Returns`` 段。

    Returns:
        `list[ToolBase]`: 三个工具。
    """
    return [
        FunctionTool(now, is_read_only=True),
        FunctionTool(calc, is_read_only=True),
        FunctionTool(env_info, is_read_only=True),
    ]


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv (`list[str] | None`): 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        `int`: 进程退出码。
    """
    parser = argparse.ArgumentParser(description="harness_kit MCP demo server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="用 streamable-http 起服务（默认 stdio）；端口固定 18100",
    )
    parser.add_argument("--port", type=int, default=18100)
    parser.add_argument(
        "--log-level",
        default=os.getenv("HARNESS_LOG_LEVEL", "WARNING"),
        help="日志级别（默认 WARNING，设 DEBUG 可看 MCP SDK 的请求日志）",
    )
    args = parser.parse_args(argv)
    _quiet_logging(args.log_level)

    server = build_mcp_server(
        "harness-demo",
        tools=build_tools(),
        port=args.port,
        instructions="harness_kit 的演示工具集：时间、安全计算、环境信息。",
    )
    if args.http:
        server.run("streamable-http")
    else:
        server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
