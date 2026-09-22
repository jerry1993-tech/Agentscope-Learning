# -*- coding: utf-8 -*-
"""把远端 MCP 工具注入 AgentScope ``Toolkit``（契约 §3.7，第 7 讲）。

**这一层几乎不需要新代码，因为 AgentScope 已经做完了**：

- ``MCPClient.get_tool(name)``（``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:483``）
  会把 ``mcp.types.Tool`` 包成 ``MCPTool``（``.../tool/_adapters.py:195``）；
- ``MCPTool`` 自己生成模型侧工具名 ``mcp__<server>__<tool>``
  （``.../tool/_adapters.py:246``）、原样透传 ``inputSchema``（含 ``$defs``，
  同文件 ``:261``）、把 MCP 结果转成 ``ToolChunk``（同文件 ``:351``）；
- ``Toolkit(mcps=[...])`` 可以直接吞 ``MCPClient``（``.../tool/_toolkit.py:66``）。

harness_kit 补的是三件 AgentScope 没做、而生产一定会撞上的事：

1. **命名空间与去重**：两个 MCP server 都提供 ``read_file`` 时，
   ``mcp__a__read_file`` 与 ``mcp__b__read_file`` 是两个不同的工具，
   要在注入前把冲突说清楚；
2. **按 server 分组**：默认所有 MCP 工具进 ``mcp`` 工具组（而不是常驻的
   ``basic`` 组），这样 Agent 要靠 meta tool ``ResetTools`` 显式激活，
   避免几十个 MCP 工具把提示词撑爆；
3. **注入的幂等与冲突策略**：``Toolkit.add_tool`` 是 ``async`` 的，遇到重名
   只会打一条 warning 然后覆盖（同文件 ``:660-670``）。我们把它变成显式的
   ``on_conflict`` 三选一。

**工具名格式的重要偏离（见返回值 unresolved）**：契约 §3.7 写的是
``name = f"{namespace}__{tool_name}"``，而真实实现是
``mcp__{namespace}__{tool_name}`` —— ``MCPTool`` 是名字的唯一权威
（``.../tool/_adapters.py:246``）。硬造一个不带 ``mcp__`` 前缀的名字，
会让 ``is_mcp`` 标记、调试时的 raw/wrapped 名字对应关系全部失真。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, Mapping

import mcp.types
from loguru import logger

from agentscope.mcp import MCPClient
from agentscope.tool import MCPTool, ToolBase, Toolkit

from harness_kit.mcp.registry import (
    MCPServerRegistry,
    MCPServerSpec,
    namespaced_tool_name,
    namespace_of,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    pass

__all__ = [
    "ToolNameConflictError",
    "collect_tools",
    "inject_into_toolkit",
    "list_remote_tools",
    "to_tool_base",
]

ConflictPolicy = Literal["error", "skip", "replace"]
"""重名处理策略：``error`` 抛异常、``skip`` 保留已有、``replace`` 覆盖。"""


class ToolNameConflictError(ValueError):
    """注入 ``Toolkit`` 时工具重名，且策略为 ``error``。"""


def _as_mcp_tool(tool_desc: Mapping[str, Any] | mcp.types.Tool) -> mcp.types.Tool:
    """把"工具描述"规整成 ``mcp.types.Tool``。

    ``MCPClient.list_raw_tools()`` 给的是 ``mcp.types.Tool`` 对象；配置或
    缓存里存的可能是它的 ``model_dump()`` 结果（``dict``）。两种都接受，
    这样调用方不必关心数据从哪来。

    Args:
        tool_desc (`Mapping[str, Any] | mcp.types.Tool`): 工具描述。

    Returns:
        `mcp.types.Tool`: 规范化后的对象。

    Raises:
        ValueError: dict 里缺少 ``name``，或 pydantic 校验失败。
    """
    if isinstance(tool_desc, mcp.types.Tool):
        return tool_desc

    payload = dict(tool_desc)
    if "name" not in payload:
        raise ValueError(
            f"MCP 工具描述缺少 name 字段: {sorted(payload)}",
        )
    # MCP 的字段名是 camelCase（inputSchema / outputSchema），dict 里两种都容忍
    if "inputSchema" not in payload and "input_schema" in payload:
        payload["inputSchema"] = payload.pop("input_schema")
    if "outputSchema" not in payload and "output_schema" in payload:
        payload["outputSchema"] = payload.pop("output_schema")
    return mcp.types.Tool.model_validate(payload)


def _connection_kwargs(client: MCPClient) -> dict[str, Any]:
    """取出构造 ``MCPTool`` 所需的连接参数。

    ``MCPTool`` 要求 ``session``（有状态）与 ``client_gen``（无状态）二选一，
    两者都通过 ``MCPClient`` 的私有属性暴露 —— 没有公开访问器，
    而 ``MCPClient.get_tool`` 内部用的正是这两个字段
    （``third_party/agentscope/src/agentscope/mcp/_mcp_client.py:483-540``）。

    Args:
        client (`MCPClient`): 已连接（或无状态）的客户端。

    Returns:
        `dict[str, Any]`: 可直接展开给 ``MCPTool(...)`` 的关键字参数。

    Raises:
        RuntimeError: 有状态客户端尚未 ``connect()``。
    """
    if client.is_stateful:
        session = client._session  # noqa: SLF001 - 见 docstring
        if session is None:
            raise RuntimeError(
                f"MCP {client.name!r} 是有状态连接但尚未 connect()；"
                "请先 await client.connect()",
            )
        return {"session": session}
    return {"client_gen": client._get_client_gen}  # noqa: SLF001


def to_tool_base(
    client: MCPClient,
    tool_desc: Mapping[str, Any] | mcp.types.Tool,
    *,
    namespace: str,
) -> ToolBase:
    """把 MCP 工具描述包装成 ``ToolBase``（契约 §3.7）。

    返回的就是 AgentScope 原生的 ``MCPTool``，不是自研包装类 —— 语义完全一致：

    - ``name`` = ``mcp__{namespace}__{tool_name}``（非法字符替换为 ``x``）；
    - ``input_schema`` 原样透传（``$defs`` / ``anyOf`` 都不会被压扁）；
    - ``is_mcp = True``、``is_state_injected = False``
      （``third_party/agentscope/src/agentscope/tool/_adapters.py:203-205``，
      MCP 工具**禁止**注入 AgentState，这是安全边界）；
    - ``check_permissions`` 默认：只读工具 ``ALLOW``，其余 ``ASK``
      （同文件 ``:294-313``）。

    Args:
        client (`MCPClient`): 该工具所属的客户端（用于建立调用通道）。
        tool_desc (`Mapping[str, Any] | mcp.types.Tool`): 工具描述。
        namespace (`str`): 命名空间；空串时退回 ``client.name``。

    Returns:
        `ToolBase`: ``MCPTool`` 实例。

    Raises:
        ValueError: 工具描述不合法。
        RuntimeError: 有状态客户端未连接。
    """
    raw = _as_mcp_tool(tool_desc)
    resolved_ns = namespace_of(namespace or client.name)
    tool = MCPTool(
        mcp_name=resolved_ns,
        tool=raw,
        timeout=client.execution_timeout,
        **_connection_kwargs(client),
    )
    logger.debug(
        "MCP 工具已包装: {} -> {} (read_only={})",
        raw.name,
        tool.name,
        tool.is_read_only,
    )
    return tool


async def list_remote_tools(client: MCPClient) -> list[dict[str, Any]]:
    """列出远端工具（原始描述，dict 形式）。契约 §3.7。

    用 ``list_raw_tools()`` 而不是 ``list_tools()``：前者给的是 server 上的
    原始工具名（``add`` / ``echo``），后者给的是模型侧名字
    （``mcp__demo__add``）。调试与冲突检测需要原始名。

    Args:
        client (`MCPClient`): 已连接（或无状态）的客户端。

    Returns:
        `list[dict[str, Any]]`: 每个工具的 ``model_dump()`` 结果。

    Raises:
        RuntimeError: 有状态客户端未连接。
    """
    raw_tools = await client.list_raw_tools()
    return [_.model_dump(exclude_none=True) for _ in raw_tools]


async def collect_tools(
    clients: list[MCPClient],
    *,
    registry: MCPServerRegistry | None = None,
    base: list[ToolBase] | None = None,
    on_conflict: ConflictPolicy = "replace",
) -> list[ToolBase]:
    """把一批客户端的全部远端工具收集成 ``ToolBase`` 列表。

    重名检测发生在**注入之前**：两个 server 都提供同名工具是合法且常见的，
    只要命名空间不同就不会撞；撞了才按 ``on_conflict`` 处理。

    Args:
        clients (`list[MCPClient]`): ``MCPServerRegistry.to_clients()`` 的产物。
        registry (`MCPServerRegistry | None`): 已知声明表；提供时用它的
            ``namespace`` 覆盖客户端自己的名字。
        base (`list[ToolBase] | None`): 已有工具（通常是本地工具包），
            参与重名检测但不会被修改。
        on_conflict (`ConflictPolicy`): 重名策略。

    Returns:
        `list[ToolBase]`: 可注入的工具列表。

    Raises:
        ToolNameConflictError: ``on_conflict="error"`` 且出现重名。
    """
    taken: dict[str, str] = {tool.name: "本地工具" for tool in base or []}
    collected: list[ToolBase] = []
    index: dict[str, int] = {}

    for client in clients:
        spec: MCPServerSpec | None = None
        if registry is not None and client.name in registry:
            spec = registry.get(client.name)
        namespace = spec.resolved_namespace if spec is not None else client.name

        raw_tools = await client.list_raw_tools()
        for raw in raw_tools:
            tool = to_tool_base(client, raw, namespace=namespace)
            owner = f"MCP server {namespace!r}/{raw.name}"

            if tool.name in taken:
                previous = taken[tool.name]
                if on_conflict == "error":
                    raise ToolNameConflictError(
                        f"工具名冲突: {tool.name!r} 同时来自 {previous} 和 {owner}；"
                        "请给其中一个 server 配不同的 namespace",
                    )
                if on_conflict == "skip":
                    logger.warning(
                        "工具名冲突，保留 {} 并跳过 {}: {}",
                        previous,
                        owner,
                        tool.name,
                    )
                    continue
                logger.warning(
                    "工具名冲突，用 {} 覆盖 {}: {}",
                    owner,
                    previous,
                    tool.name,
                )
                # 冲突来源有两种：``base``（本地工具）与前面某个 MCP server。
                # 只有后者在 ``index`` 里有条目；前者必须**追加**而不是原地
                # 替换，否则 ``index[tool.name]`` 会 KeyError。
                if tool.name in index:
                    collected[index[tool.name]] = tool
                else:
                    index[tool.name] = len(collected)
                    collected.append(tool)
                taken[tool.name] = owner
                continue

            taken[tool.name] = owner
            index[tool.name] = len(collected)
            collected.append(tool)

    logger.bind(tools=[t.name for t in collected]).info(
        "从 {} 个 MCP server 收集到 {} 个工具",
        len(clients),
        len(collected),
    )
    return collected


async def inject_into_toolkit(
    toolkit: Toolkit,
    tools: list[ToolBase],
    *,
    group: str = "mcp",
    on_conflict: ConflictPolicy = "replace",
) -> None:
    """把工具注入 ``Toolkit`` 的指定工具组（契约 §3.7）。

    **必须是 async**：``Toolkit.add_tool`` 是协程
    （``third_party/agentscope/src/agentscope/tool/_toolkit.py:640``）。
    ``Toolkit`` 没有同步的批量注册入口，也没有"只注册不分组"的路径，
    所以这里直接 ``await toolkit.add_tool(tools, group_name=group)``。

    ``group`` 不在 ``toolkit.tool_groups`` 里时 ``add_tool`` 会 ``ValueError``
    （同文件 ``:677``）；本函数先探测一次，把错误换成一条带可用组名的提示。

    Args:
        toolkit (`Toolkit`): 目标工具集。
        tools (`list[ToolBase]`): 待注入工具。
        group (`str`): 目标工具组名，默认 ``"mcp"``（非 ``basic``，
            因此需要 Agent 调 meta tool 激活）。
        on_conflict (`ConflictPolicy`): 与**组内已有工具**重名时的策略。

    Raises:
        ValueError: ``group`` 不存在于 ``toolkit.tool_groups``。
        ToolNameConflictError: ``on_conflict="error"`` 且组内已存在同名工具。
    """
    available = [_.name for _ in toolkit.tool_groups]
    if group not in available:
        raise ValueError(
            f"Toolkit 里没有工具组 {group!r}；可用组: {available}。"
            "请把它加进 Toolkit(tool_groups=[...]) —— 注意 'basic' 是保留组名，"
            "构造时不能再传一个同名的 ToolGroup"
            "（third_party/agentscope/src/agentscope/tool/_toolkit.py:117-125）",
        )
    if not tools:
        logger.debug("没有 MCP 工具需要注入到组 {}（列表为空）", group)
        return

    target = next(_ for _ in toolkit.tool_groups if _.name == group)
    existing = {_.name for _ in target.tools}
    payload: list[ToolBase] = []
    for tool in tools:
        if tool.name in existing:
            if on_conflict == "error":
                raise ToolNameConflictError(
                    f"工具组 {group!r} 里已有同名工具 {tool.name!r}；"
                    "请改用 on_conflict='replace' 或 'skip'",
                )
            if on_conflict == "skip":
                logger.warning("工具组 {} 已有 {}，跳过注入", group, tool.name)
                continue
            logger.warning("工具组 {} 已有 {}，将被覆盖", group, tool.name)
        existing.add(tool.name)
        payload.append(tool)

    if payload:
        await toolkit.add_tool(payload, group_name=group)
    logger.bind(
        group=group,
        injected=[_.name for _ in payload],
        skipped=len(tools) - len(payload),
    ).info("MCP 工具已注入工具组 {}：{} 个", group, len(payload))


def tool_digest(tools: list[ToolBase]) -> str:
    """生成工具清单的稳定摘要，供审计与快照对比。

    Args:
        tools (`list[ToolBase]`): 工具列表。

    Returns:
        `str`: JSON 字符串，按工具名排序。
    """
    return json.dumps(
        [
            {
                "name": tool.name,
                "is_mcp": bool(getattr(tool, "is_mcp", False)),
                "is_read_only": bool(tool.is_read_only),
            }
            for tool in sorted(tools, key=lambda t: t.name)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def namespaced(namespace: str, tool_name: str) -> str:
    """便捷函数：``namespaced_tool_name`` 的短别名。

    Args:
        namespace (`str`): 命名空间。
        tool_name (`str`): 远端工具名。

    Returns:
        `str`: 模型侧完整工具名。
    """
    return namespaced_tool_name(namespace, tool_name)
