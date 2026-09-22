# -*- coding: utf-8 -*-
"""``BuiltinToolPack`` —— 生产环境的「基础工具包」。

**本模块不实现任何文件/命令工具**，它做的是**配置与组装**：

- 把 AgentScope 自带的 6 个工具（``Bash`` / ``Edit`` / ``Glob`` / ``Grep`` /
  ``Read`` / ``Write``）**共用同一个 ``LocalBackend``**；
- 把它们**分组**（``fs_read`` / ``fs_write`` / ``shell`` / ``utility``），
  交给 ``ToolGroup``，让模型可以按需激活；
- 补两个 AgentScope 没有、但生产上每个 Agent 都要用的**只读工具**：
  ``Now``（当前时间）与 ``Calc``（安全算术）—— 用 ``FunctionTool`` 包一层，
  不写新的 ``ToolBase`` 子类；
- 把「哪些工具是危险的」变成 :attr:`ToolPackManifest.dangerous_tools` 清单。

**为什么是「共用 backend」而不是各建一个**：``Read`` 有一个内部的读取缓存
（按 backend 维度隔离），``Bash`` 的 ``cwd`` 也挂在 backend 上。各建一个
``LocalBackend()`` 时它们**语义上仍然是同一台机器，但状态各自独立** ——
``Read`` 缓存了 ``a.py`` 的旧内容、``Write`` 改了它、``Read`` 再读还是旧内容，
这类 bug 极难查。共用一个实例是唯一正确的做法。

**为什么 ``Now`` / ``Calc`` 用 ``FunctionTool`` 而不是写 ``ToolBase`` 子类**：
``FunctionTool``（``third_party/agentscope/src/agentscope/tool/_adapters.py:36``）
会自动从函数签名 + docstring 生成 JSON schema（``_extract_input_schema`` /
``_extract_func_description``，``.../tool/_utils.py``）。自己写 ``ToolBase``
意味着自己维护 schema、自己做类型校验、自己做异常包装 —— 三倍的代码量换零收益。

**真实 API 锚点**：

- ``Bash(cwd=..., backend=...)``：``.../tool/_builtin/_bash.py:137``
- ``Read(max_line_characters=..., backend=...)``：``.../tool/_builtin/_read.py:131``
- ``Write/Edit(dangerous_files=..., dangerous_directories=..., backend=...)``：
  ``.../_write.py:65``、``.../_edit.py:87``
- ``Glob(backend=..., glob_helper_path=...)``：``.../_glob.py:99``
- ``Grep(backend=...)``：``.../_grep.py:159``
- ``LocalBackend``：``.../tool/_builtin/_backend.py``，从 ``agentscope.tool``
  顶层导出（``.../tool/__init__.py:29``）
"""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import (
    BackendBase,
    Bash,
    Edit,
    FunctionTool,
    Glob,
    Grep,
    LocalBackend,
    Read,
    ToolBase,
    Write,
)

from harness_kit.tools.pack import ToolPackBase, ToolPackManifest

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from harness_kit.config.schema import ToolsSpec

__all__ = [
    "BuiltinToolPack",
    "build_builtin_pack",
    "calc",
    "now",
]

READ_ONLY_ALLOW = PermissionDecision(
    behavior=PermissionBehavior.ALLOW,
    message="只读工具，无副作用，直接放行。",
    decision_reason="harness_kit.tools.builtin_pack.READ_ONLY_ALLOW",
)
"""``Now`` / ``Calc`` 的权限决策。

``FunctionTool`` 的默认行为是 ``ASK``
（``third_party/agentscope/src/agentscope/tool/_adapters.py:134``），
意思是「每调一次问一次用户」。对一个「读时钟」和「算 1+1」的工具来说
这是纯粹的骚扰，而且它会**污染权限对话框**：真需要用户确认的危险操作
淹没在「是否允许读取当前时间」里。所以这里显式 ALLOW。

注意 ALLOW 不是「绕过规则」：``PermissionEngine`` 里用户配的 DENY / ASK
规则（``harness_kit/permission/rules.py``）优先级高于工具自报的决策，
所以企业策略仍能把它关掉。
"""


# ======================================================================
# 工具实现（用 FunctionTool 包装）
# ======================================================================
def now(timezone_offset_hours: float = 0.0) -> str:
    """Get the current date and time.

    Use this tool whenever you need to know the current time — you have no
    other way to learn it. Never guess the date from your training data.

    Args:
        timezone_offset_hours (float): Offset from UTC in hours, e.g. 8 for
            Beijing time. Defaults to 0 (UTC).
    """
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=timezone_offset_hours))
    current = datetime.now(tz)
    return (
        f"{current.isoformat(timespec='seconds')} "
        f"(weekday={current.strftime('%A')}, tz=UTC{timezone_offset_hours:+g})"
    )


_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
"""允许的二元运算符白名单。"""

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
"""允许的一元运算符白名单。"""

_SAFE_NAMES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}
"""允许出现的常量名（白名单，不是 ``eval`` 的命名空间）。"""

_SAFE_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
}
"""允许调用的函数白名单。"""


class CalcError(ValueError):
    """表达式不被 :func:`calc` 接受（语法错、含非法节点、除零等）。"""


def _eval_node(node: ast.AST) -> Any:
    """递归求值一个 AST 节点（白名单式，非 ``eval``）。

    Args:
        node (`ast.AST`): 语法树节点。

    Returns:
        `Any`: 求值结果。

    Raises:
        CalcError: 节点不在白名单里。
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
            node.value,
            (int, float),
        ):
            raise CalcError(
                f"只允许数字常量，收到 {type(node.value).__name__}",
            )
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in _SAFE_NAMES:
            raise CalcError(f"未知常量 {node.id!r}；可用：{sorted(_SAFE_NAMES)}")
        return _SAFE_NAMES[node.id]
    if isinstance(node, ast.BinOp):
        handler = _BINARY_OPS.get(type(node.op))
        if handler is None:
            raise CalcError(f"不支持的运算符 {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise CalcError("幂次超过 64，拒绝执行以免算力被拖垮")
        return handler(left, right)
    if isinstance(node, ast.UnaryOp):
        handler = _UNARY_OPS.get(type(node.op))
        if handler is None:
            raise CalcError(f"不支持的一元运算符 {type(node.op).__name__}")
        return handler(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalcError("只允许调用白名单里的具名函数")
        func = _SAFE_FUNCS.get(node.func.id)
        if func is None:
            raise CalcError(
                f"未知函数 {node.func.id!r}；可用：{sorted(_SAFE_FUNCS)}",
            )
        if node.keywords:
            raise CalcError("不支持关键字参数")
        return func(*[_eval_node(arg) for arg in node.args])
    raise CalcError(f"不支持的语法节点 {type(node).__name__}")


def calc(expression: str) -> str:
    """Evaluate a mathematical expression and return the result.

    Use this tool for any arithmetic instead of computing it yourself —
    you make mistakes on multi-step arithmetic. Supports ``+ - * / // % **``,
    parentheses, the constants ``pi`` / ``e`` / ``tau``, and the functions
    ``abs round min max sqrt log log10 exp sin cos tan floor ceil``.

    Args:
        expression (str): The expression to evaluate, e.g. ``"(1+2)*3/7"``.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return f"错误：表达式语法不合法（{exc.msg}）。请只使用数字与运算符。"

    try:
        value = _eval_node(tree)
    except CalcError as exc:
        return f"错误：{exc}"
    except ZeroDivisionError:
        return "错误：除数为零。"
    except OverflowError:
        return "错误：结果超出浮点范围。"
    except (ValueError, TypeError) as exc:
        return f"错误：{type(exc).__name__}: {exc}"

    return f"{expression} = {value}"


# ======================================================================
# 工具包
# ======================================================================
class BuiltinToolPack(ToolPackBase):
    """基础工具包：AgentScope 原生文件/命令工具 + 只读的 ``Now`` / ``Calc``。

    Args:
        workdir (`str | None`): ``Bash`` 的工作目录；``None`` 时用进程 cwd。
            **这是最容易出事故的一个参数** —— 忘了传它，Agent 的 ``Bash``
            会在你启动进程的目录里跑命令。
        backend (`BackendBase | None`): 注入一个 backend（如 Docker backend）。
            ``None`` 时在 :meth:`build_tools` 里新建一个 ``LocalBackend``
            并让 6 个工具共用。
        include_utility (`bool`): 是否附加 ``Now`` / ``Calc``。默认 ``True``。
        max_line_characters (`int`): 透传给 ``Read`` 的单行截断长度，
            默认 ``2000``（与 AgentScope 默认值一致）。
        dangerous_files (`list[str] | None`): 透传给 ``Write`` / ``Edit`` /
            ``Bash``；``None`` 用 AgentScope 的 ``DEFAULT_DANGEROUS_FILES``。
        dangerous_directories (`list[str] | None`): 与 ``dangerous_files``
            同义，只是匹配的是目录。
    """

    manifest: ToolPackManifest = ToolPackManifest(
        name="builtin",
        version="0.1.0",
        description=(
            "基础工具包：AgentScope 原生文件读写 / 搜索 / 命令执行工具，"
            "外加只读的时间与算术工具。"
        ),
        requires=[],
        # 只把**有副作用**的工具放进组，只读工具留在 basic 常驻。
        #
        # 为什么（实测得出的 AgentScope 语义）：``Toolkit`` 里只有
        # ``"basic"`` 组的工具是**始终可见**的，其它组要先由模型调用
        # meta tool ``reset_tools`` 激活才会出现在 ``tools`` 数组里
        # （``third_party/agentscope/src/agentscope/tool/_toolkit.py:505``：
        # ``groups_filter = ["basic"] + (groups or [])``，且 ``:190`` 的
        # docstring 写明「未提供 groups 时只包含 basic 组」）。
        # 如果把 ``Read`` 也塞进组里，Agent 起步时**看不到任何读文件的工具**，
        # 它得先花一轮去激活 ``fs_read`` —— 白白多一次 LLM 调用。
        # 反过来，让 ``Bash`` 常驻则更糟：模型一上来就能跑任意命令。
        # 结论：**只读的常驻，能写能跑的入组**。
        groups={
            "fs_write": ["Write", "Edit"],
            "shell": ["Bash"],
        },
        tags=["fs", "shell", "core"],
        dangerous_tools=["Write", "Edit", "Bash"],
    )

    def __init__(
        self,
        *,
        workdir: str | None = None,
        backend: BackendBase | None = None,
        include_utility: bool = True,
        max_line_characters: int = 2000,
        dangerous_files: list[str] | None = None,
        dangerous_directories: list[str] | None = None,
    ) -> None:
        """记录配置，不建工具（工具在 :meth:`build_tools` 里才建）。"""
        super().__init__()
        self.workdir = workdir
        self._backend = backend
        self.include_utility = include_utility
        self.max_line_characters = max_line_characters
        self._dangerous_files = dangerous_files
        self._dangerous_directories = dangerous_directories
        self._built: list[ToolBase] | None = None

    # ------------------------------------------------------------------
    def _backend_or_new(self) -> BackendBase:
        """取注入的 backend，没有就新建一个 ``LocalBackend``。

        Returns:
            `BackendBase`: 6 个工具共用的那一个。
        """
        if self._backend is None:
            self._backend = LocalBackend()
            logger.debug("builtin 包新建 LocalBackend（workdir={}）", self.workdir)
        return self._backend

    def _dangerous_kwargs(self) -> dict[str, Any]:
        """组装 ``Bash`` / ``Write`` / ``Edit`` 共用的危险路径参数。

        Returns:
            `dict[str, Any]`: 只含被显式指定的键 —— 不指定的键留给
            AgentScope 用它的 ``DEFAULT_DANGEROUS_*`` 默认值，这比我们
            在这里复制一份常量可靠。
        """
        kwargs: dict[str, Any] = {}
        if self._dangerous_files is not None:
            kwargs["dangerous_files"] = list(self._dangerous_files)
        if self._dangerous_directories is not None:
            kwargs["dangerous_directories"] = list(self._dangerous_directories)
        return kwargs

    async def build_tools(self, spec: "ToolsSpec") -> list[ToolBase]:
        """造出 6 个原生工具（+ 可选的 2 个只读工具）。

        同一个实例重复调用返回**同一批**工具对象（幂等），这样
        ``build_tools`` 与 ``build_toolkit`` 先后调用不会造出两套工具、
        两套缓存。

        Args:
            spec (`ToolsSpec`): 工具声明；本包只校验 ``max_result_chars``
                并读 ``disabled``（由 ``build_toolkit`` 负责摘除）。

        Returns:
            `list[ToolBase]`: 工具实例。

        Raises:
            ValueError: ``max_result_chars`` 非正数。
        """
        if spec.max_result_chars <= 0:
            raise ValueError(
                f"ToolsSpec.max_result_chars 必须为正数，收到 "
                f"{spec.max_result_chars}；0 或负数会让工具结果无上限地"
                "灌进上下文。",
            )

        if self._built is not None:
            return list(self._built)

        backend = self._backend_or_new()
        dangerous = self._dangerous_kwargs()

        tools: list[ToolBase] = [
            Bash(cwd=self.workdir, backend=backend, **dangerous),
            Edit(backend=backend, **dangerous),
            Glob(backend=backend),
            Grep(backend=backend),
            Read(
                max_line_characters=self.max_line_characters,
                backend=backend,
            ),
            Write(backend=backend, **dangerous),
        ]

        if self.include_utility:
            tools.append(
                FunctionTool(
                    now,
                    name="Now",
                    is_read_only=True,
                    is_concurrency_safe=True,
                    permission=READ_ONLY_ALLOW,
                ),
            )
            tools.append(
                FunctionTool(
                    calc,
                    name="Calc",
                    is_read_only=True,
                    is_concurrency_safe=True,
                    permission=READ_ONLY_ALLOW,
                ),
            )

        self._built = tools
        logger.bind(tools=[tool.name for tool in tools]).debug(
            "builtin 工具包已装配（backend={}，workdir={}）",
            type(backend).__name__,
            self.workdir,
        )
        return list(tools)

    def build_context(self) -> dict[str, Any]:
        """把 backend / workdir 暴露给上层（``repo_pack`` 会复用）。

        Returns:
            `dict[str, Any]`: ``{"backend": ..., "workdir": ...}``。
        """
        return {
            "backend": self._backend_or_new(),
            "workdir": self.workdir,
            "max_result_chars": None,
        }

    def _group_description(self, name: str, members: list[ToolBase]) -> str:
        """给四组工具各写一句有信息量的描述。

        这段文字会被塞进 meta tool ``ResetTools`` 的结果里给模型看，
        是模型判断「该激活哪一组」的**唯一依据**，所以要写**什么时候用**，
        而不是复述工具名。

        Args:
            name (`str`): 组名。
            members (`list[ToolBase]`): 组内工具。

        Returns:
            `str`: 组描述。
        """
        hints = {
            "fs_write": "新建或修改文件。需要把改动落盘时激活本组。",
            "shell": "执行任意 shell 命令（跑测试、装依赖、查进程）。"
            "专用工具（Read / Glob / Grep / Write / Edit）能做的事不要用本组。",
        }
        names = ", ".join(tool.name for tool in members)
        return (
            f"{name}：{hints.get(name, '本组工具见成员列表。')}"
            f"（包含 {names}）"
        )


async def build_builtin_pack(
    spec: "ToolsSpec",
    ctx: Any = None,
) -> list[ToolBase]:
    """Layer 0 直连工厂：``async (spec, ctx) -> list[ToolBase]``。

    与 :func:`harness_kit.registry._builtin_tool_pack` **签名完全一致**，
    因此可以用它覆盖注册表里的 ``"builtin"`` 条目：

    .. code-block:: python

        registry = HarnessRegistry.default()
        registry.register_tool_pack("builtin", build_builtin_pack)

    取出 ``ctx.workdir`` 的逻辑与 registry 的直连工厂一致：``Bash`` 必须
    在正确的工作目录里跑。

    Args:
        spec (`ToolsSpec`): 工具声明。
        ctx (`BuildContext | None`, optional): 装配上下文；只用 ``workdir``。

    Returns:
        `list[ToolBase]`: 工具实例。
    """
    workdir: str | None = None
    if ctx is not None and getattr(ctx, "workdir", None) is not None:
        workdir = str(ctx.workdir)
    pack = BuiltinToolPack(workdir=workdir)
    return await pack.build_tools(spec)
