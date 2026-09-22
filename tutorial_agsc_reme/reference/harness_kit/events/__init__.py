# -*- coding: utf-8 -*-
"""harness_kit 的事件层：不可变事件记录、进程内事件总线、AgentEvent 翻译器。

模块归属（契约 §二 的讲次映射）：

- :mod:`harness_kit.events.types` —— 第 3 讲
- :mod:`harness_kit.events.bus` —— 第 3 讲
- :mod:`harness_kit.events.translate` —— 第 3 讲

**为什么 ``translate`` 不在这里 import**：依赖方向必须是 DAG。
``translate`` 需要 ``harness_kit.session.models`` 的 ``preview`` /
``INPUT_PREVIEW_LIMIT``（契约 §5.2 规定 ``input_preview`` 截断到 500 字符），
而 ``session.models`` 反过来要 ``from harness_kit.events import EventRecord``
—— 如果本文件 import ``translate``，就会变成
``events/__init__ → translate → session.models → events/__init__`` 的环，
在"先 import session"的进程里直接 ``ImportError``。

所以门面只导出**零反向依赖**的两层（``types`` 与 ``bus``），
翻译器一律用完整路径导入::

    from harness_kit.events.translate import StreamTranslator

这不是偷懒，是刻意的分层：``types`` 谁都能依赖，``bus`` 只依赖 ``types``，
``translate`` 在最上游，只被应用层依赖。
"""

from harness_kit.events.bus import (
    BusClosedError,
    EventBus,
    Handler,
    Subscription,
)
from harness_kit.events.types import (
    PAYLOAD_FIELDS,
    WILDCARD_TOPIC,
    EventKind,
    EventRecord,
    topic_matches,
    utc_now,
)

__all__ = [
    "PAYLOAD_FIELDS",
    "WILDCARD_TOPIC",
    "BusClosedError",
    "EventBus",
    "EventKind",
    "EventRecord",
    "Handler",
    "Subscription",
    "topic_matches",
    "utc_now",
]
