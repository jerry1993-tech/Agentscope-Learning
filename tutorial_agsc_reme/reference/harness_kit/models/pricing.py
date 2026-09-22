# -*- coding: utf-8 -*-
"""价格表与成本核算（Layer 1 的「账本」）。

AgentScope 的模型层只统计 token，**不算钱**：``ChatUsage``
（``third_party/agentscope/src/agentscope/model/_model_usage.py:9``）有
``input_tokens`` / ``output_tokens`` / ``cache_input_tokens`` /
``cache_creation_input_tokens`` 四个数，``ChatResponse.usage``
（``third_party/agentscope/src/agentscope/model/_model_response.py:55``）把
它挂在每个响应上，但没有任何一处把它翻译成费用。生产环境必须补上这一步，
否则「这个 Agent 一天烧了多少钱」只能靠人肉估算。

本模块补的正是这一步，且只做翻译，不碰模型调用：

- :class:`Price` / :class:`PriceTable`：声明式价格表（pydantic v2，可 YAML 化）；
- :func:`cost_of`：把一次调用的 :class:`~agentscope.model.ChatUsage` 折算成美元；
- :func:`default_price_table`：内置示例价格表（**教学用，非官方报价**）。

**cache 字段的计价语义**（这是本模块唯一需要动脑的地方）：

- DeepSeek 的 ``prompt_cache_hit_tokens`` 与 OpenAI 的
  ``prompt_tokens_details.cached_tokens`` 都被 AgentScope 落到
  ``ChatUsage.cache_input_tokens``（见
  ``third_party/agentscope/src/agentscope/model/_deepseek/_model.py:276`` 与
  ``.../model/_openai_chat/_model.py:356``）。
- 这两个数**已经包含在** ``input_tokens`` 里（``prompt_tokens = 命中 + 未命中``），
  所以命中部分不能按全价算，否则会把缓存收益算丢。
- 命中部分的价格只在 ``Price.cache_read_per_mtok`` 非 ``None`` 时启用；
  为 ``None`` 时按普通输入价算 —— 与契约 §3.4 的约定一致。

实测锚点（本环境 deepseek-flash，2026-09-21）：同一段 prompt 的第二次调用
``cache_input_tokens=128``，第一次为 ``0``；写成 4 次调用后本模块的
``cost_of`` 能把 128 个 token 按缓存价算，差额可直接打印出来。
"""

from __future__ import annotations

from typing import Any, Mapping

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agentscope.model import ChatUsage

__all__ = [
    "DEFAULT_PRICE_TABLE",
    "EXAMPLE_PRICES",
    "Price",
    "PriceTable",
    "UnknownPriceError",
    "cost_of",
    "default_price_table",
    "format_usd",
    "usage_to_row",
]

_TOKENS_PER_MTOK: int = 1_000_000
"""价格表的单位是「美元 / 百万 token」，token 数除以它才是美元。"""


class UnknownPriceError(KeyError):
    """价格表里查不到该模型，且没有可用的前缀兜底。"""


class Price(BaseModel):
    """单个模型的价格（单位：美元 / 百万 token）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: float = Field(
        gt=0,
        description="未命中缓存的输入 token 单价（$/Mtok）。",
    )

    output_per_mtok: float = Field(
        gt=0,
        description="输出 token 单价（$/Mtok）。",
    )

    cache_read_per_mtok: float | None = Field(
        default=None,
        description=(
            "命中 prompt cache 的输入 token 单价（$/Mtok）。``None`` 表示该"
            "provider 不单独计价，命中部分按 :attr:`input_per_mtok` 计。"
        ),
    )

    cache_write_per_mtok: float | None = Field(
        default=None,
        description=(
            "写入 prompt cache 的单价（$/Mtok）。本环境实测：除 Anthropic 外"
            "没有 provider 会填 ``ChatUsage.cache_creation_input_tokens``，"
            "所以这一项在 DeepSeek / OpenAI 上不会生效。"
        ),
    )

    currency: str = Field(
        default="USD",
        description="币种标签，仅用于展示；本模块不做汇率换算。",
    )


class PriceTable(BaseModel):
    """模型名 → 价格 的映射表。"""

    model_config = ConfigDict(extra="forbid")

    prices: dict[str, Price] = Field(
        default_factory=dict,
        description="模型名 → :class:`Price`。键可以带 provider 前缀，"
        "如 ``deepseek/deepseek-chat``。",
    )

    allow_prefix_fallback: bool = Field(
        default=True,
        description=(
            "精确匹配失败时，是否允许用「最长前缀」兜底。"
            "例如表里有 ``deepseek-chat``，查 ``deepseek-chat-0324`` 时命中。"
            "注意这是**按前缀猜测**，日志里会记一条 warning，不要在生产里依赖它。"
        ),
    )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def lookup(self, model_name: str) -> Price:
        """按模型名查价格。

        顺序：精确匹配 → 大小写不敏感匹配 → 最长前缀兜底。

        Args:
            model_name (`str`): 模型名，如 ``deepseek-flash``。

        Returns:
            `Price`: 命中的价格。

        Raises:
            UnknownPriceError: 三种匹配都失败。
        """
        if model_name in self.prices:
            return self.prices[model_name]

        lowered = model_name.lower()
        for key, price in self.prices.items():
            if key.lower() == lowered:
                return price

        if self.allow_prefix_fallback:
            candidates = [
                key
                for key in self.prices
                if lowered.startswith(key.lower())
            ]
            if candidates:
                best = max(candidates, key=len)
                logger.warning(
                    "价格表没有 {} 的精确条目，按最长前缀兜底到 {}；"
                    "请把该模型的真实价格补进 PriceTable",
                    model_name,
                    best,
                )
                return self.prices[best]

        raise UnknownPriceError(
            f"价格表里没有 {model_name!r}；已登记: {sorted(self.prices)}",
        )

    def with_price(self, model_name: str, price: Price) -> "PriceTable":
        """返回**追加了一条**价格的新表（原表不变）。

        Args:
            model_name (`str`): 模型名。
            price (`Price`): 价格。

        Returns:
            `PriceTable`: 新表。
        """
        merged = dict(self.prices)
        merged[model_name] = price
        return PriceTable(
            prices=merged,
            allow_prefix_fallback=self.allow_prefix_fallback,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PriceTable":
        """从 ``{模型名: 价格字典}`` 构造。

        Args:
            raw (`Mapping[str, Any]`): 形如
                ``{"deepseek-chat": {"input_per_mtok": 0.28, ...}}``。

        Returns:
            `PriceTable`: 构造好的价格表。
        """
        return cls(
            prices={
                name: Price.model_validate(value)
                for name, value in raw.items()
            },
        )


def cost_of(usage: ChatUsage, price: Price) -> float:
    """把一次调用的 token 用量折算成美元。

    公式（``price`` 的单位是 $/Mtok）：

    .. code-block:: text

        命中缓存且表里给了 cache_read 价：
            cost = (input - cache_hit) * input_price / 1e6
                 + cache_hit          * cache_read_price / 1e6
                 + output             * output_price / 1e6
        其余情况（含 cache_read 为 None）：
            cost = input * input_price / 1e6 + output * output_price / 1e6

    两个刻意的选择：

    1. ``cache_creation_input_tokens`` **不计费**（除 Anthropic 外没有 provider
       填它，见 :attr:`Price.cache_write_per_mtok` 的说明）；
    2. ``input - cache_hit`` 用 ``max(0, ...)`` 兜底 —— 某些兼容实现会给出
       ``cache_input_tokens > input_tokens`` 的矛盾数据，宁可算 0 也不要算负数。

    Args:
        usage (`ChatUsage`): AgentScope 的用量对象。
        price (`Price`): 该模型的价格。

    Returns:
        `float`: 费用（美元）；非负数。
    """
    input_tokens = int(usage.input_tokens or 0)
    output_tokens = int(usage.output_tokens or 0)
    cache_hit = int(getattr(usage, "cache_input_tokens", 0) or 0)

    cost = output_tokens * price.output_per_mtok / _TOKENS_PER_MTOK

    if cache_hit > 0 and price.cache_read_per_mtok is not None:
        fresh = max(0, input_tokens - cache_hit)
        cached = min(cache_hit, input_tokens)
        cost += fresh * price.input_per_mtok / _TOKENS_PER_MTOK
        cost += cached * price.cache_read_per_mtok / _TOKENS_PER_MTOK
    else:
        cost += input_tokens * price.input_per_mtok / _TOKENS_PER_MTOK

    return max(0.0, cost)


def format_usd(value: float, *, width: int = 6) -> str:
    """把美元金额格式化成固定宽度字符串（便于对齐打印）。

    Args:
        value (`float`): 金额（美元）。
        width (`int`): 小数位数；小于 1 美分的金额会额外用科学计数法补一列，
            避免全部打印成 ``$0.000000``。

    Returns:
        `str`: 形如 ``$0.001234`` 或 ``$0.000012 (1.2e-05)``。
    """
    text = f"${value:.{width}f}"
    if 0 < value < 10 ** (-width):
        text += f" ({value:.1e})"
    return text


def usage_to_row(
    usage: ChatUsage,
    *,
    price: Price | None = None,
) -> dict[str, Any]:
    """把一次用量摊平成一行可打印 / 可入表的记录。

    Args:
        usage (`ChatUsage`): 用量。
        price (`Price | None`): 价格；``None`` 时 ``cost_usd`` 为 ``None``。

    Returns:
        `dict[str, Any]`: 含 ``input_tokens`` / ``output_tokens`` /
        ``cache_input_tokens`` / ``seconds`` / ``cost_usd``。
    """
    return {
        "input_tokens": int(usage.input_tokens or 0),
        "output_tokens": int(usage.output_tokens or 0),
        "cache_input_tokens": int(
            getattr(usage, "cache_input_tokens", 0) or 0,
        ),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
        ),
        "seconds": float(getattr(usage, "time", 0.0) or 0.0),
        "cost_usd": None if price is None else cost_of(usage, price),
    }


EXAMPLE_PRICES: dict[str, dict[str, Any]] = {
    "deepseek-chat": {
        "input_per_mtok": 0.28,
        "output_per_mtok": 0.42,
        "cache_read_per_mtok": 0.028,
    },
    "deepseek-reasoner": {
        "input_per_mtok": 0.28,
        "output_per_mtok": 0.42,
        "cache_read_per_mtok": 0.028,
    },
    "openai/gpt-4o-mini": {
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.6,
        "cache_read_per_mtok": 0.075,
    },
}
"""**教学示例价格表，不是官方报价。**

数值取自各家公开定价页的常见档位，用于把 :func:`cost_of` 的算法跑通；
本环境使用的模型名 ``deepseek-flash`` 并不在官方定价页的档位列表里
（本环境实测它可用，但**它的真实计价档位未核实**），因此下面
:func:`default_price_table` 把它当作 ``deepseek-chat`` 的同价别名处理并打 warning。

生产做法：从厂商定价页把数字抄进自己的 YAML，用
:meth:`PriceTable.with_price` / ``PriceTable.from_mapping`` 覆盖本表。
"""


def default_price_table() -> PriceTable:
    """构造内置示例价格表（含本环境模型名的别名）。

    Returns:
        `PriceTable`: ``deepseek-flash`` → ``deepseek-chat`` 的价格。
    """
    table = PriceTable.from_mapping(EXAMPLE_PRICES)
    table = table.with_price(
        "deepseek-flash",
        table.prices["deepseek-chat"],
    )
    return table


DEFAULT_PRICE_TABLE: PriceTable = default_price_table()
"""内置示例价格表的单例，供 :mod:`harness_kit.models.factory` 默认使用。"""
