# -*- coding: utf-8 -*-
"""第 9 讲验证脚本：会话事件溯源 / 快照 / 回放 / 断点续跑。

五段实验，从"最朴素的追加日志"一路走到"跨进程恢复 + 失败回放"：

  A. **JSONL 追加日志**：seq 不变式（跳号必炸）、崩溃残留容忍、跨进程 flock 冲突
  B. **SQLite 后端**：与 JSONL 同构 + blob 外置 + ``vacuum`` 回收
  C. **真实 Agent → 事件总线 → 会话存储 → 快照**（离线，``EchoChatModel``）
  D. **跨进程恢复**：**真的 fork 一个子进程**去读同一个会话目录并接着聊
  E. **失败回放**：一次"park 在用户确认上"的会话，回放能说出它错在哪、
     恢复能给出正确的下一步动作
  F. **真实模型**（可选）：deepseek-flash 跑一轮 → 快照 → 新进程恢复 → 接着聊

用法（``PYTHONPATH`` 必须带，理由见第 1 讲）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/09_session_replay_resume.py

    # 只做"恢复"这一步（给上面的子进程调用，手工调试时也可以直接用）
    PYTHONPATH=... python scripts/09_session_replay_resume.py --resume <session_dir> <session_id>

LLM 调用预算：**2 次**（只在 F 段用真实模型，且 .env 里没有 key 时自动跳过）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import harness_kit
from loguru import logger

REF = Path(harness_kit.__file__).resolve().parent.parent
REPO = REF.parent.parent

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=False)

from agentscope.agent import Agent  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.state import AgentState  # noqa: E402
from agentscope.tool import FunctionTool, Toolkit  # noqa: E402

from harness_kit.config.schema import AgentSpec, PermissionSpec  # noqa: E402
from harness_kit.events import EventBus, EventKind, EventRecord  # noqa: E402
from harness_kit.events.translate import StreamTranslator  # noqa: E402
from harness_kit.models.adapters.echo import EchoChatModel  # noqa: E402
from harness_kit.session import (  # noqa: E402
    JsonlSessionStore,
    SessionEvent,
    SessionInvariantError,
    SessionLockedError,
    SessionReplayer,
    SessionResumer,
    SessionSnapshot,
    Snapshotter,
    SqliteSessionStore,
    check_seq_invariants,
)

# ======================================================================
# 公共件（与 tests/test_lesson09_session.py 同一套构造器）
# ======================================================================
def get_time(city: str = "北京") -> str:
    """查询某个城市的当前时间（只读工具，权限自动放行）。

    Args:
        city (`str`): 城市名。

    Returns:
        `str`: 固定时间字符串。
    """
    return f"{city} 现在是 2026-09-22 10:00:00"


def write_note(text: str) -> str:
    """写一条笔记（**非只读** → 默认权限是 ASK → 会话会 park 在确认上）。

    Args:
        text (`str`): 笔记内容。

    Returns:
        `str`: 确认串。
    """
    return f"已写入：{text}"


class Profile:
    """能喂饱 :class:`~harness_kit.session.resume.SessionResumer` 的最小 Profile。

    ``resume`` 只用三处：``profile.name``、``profile.agent.name``、``profile.permission``。
    这里用第 2 讲真实的 :class:`~harness_kit.config.schema.PermissionSpec`，
    这样"恢复时按当前 Profile 重建权限上下文"走的是**真代码路径**而不是假的。

    Attributes:
        name (`str`): Profile 名。
        agent (`AgentSpec`): Agent 声明。
        permission (`PermissionSpec`): 权限声明。
    """

    def __init__(self, name: str = "default") -> None:
        """初始化。

        Args:
            name (`str`): Profile 名。
        """
        self.name = name
        self.agent = AgentSpec(name="Friday")
        self.permission = PermissionSpec(mode="default", rule_files=[])


class StoreSink:
    """订阅者：把总线上的 :class:`EventRecord` 顺着写进 :class:`SessionStoreBase`。"""

    def __init__(self, store: JsonlSessionStore) -> None:
        """初始化。

        Args:
            store (`JsonlSessionStore`): 落盘目标。
        """
        self.store = store
        self.records: list[EventRecord] = []

    async def __call__(self, record: EventRecord) -> None:
        """落盘一条记录。

        Args:
            record (`EventRecord`): 事件记录。
        """
        await self.store.append(record)
        self.records.append(record)


def build_echo_agent(script: list[dict], *, name: str = "Friday") -> Agent:
    """造一个离线 Agent（回声模型 + 一个只读工具 + 一个非只读工具）。

    Args:
        script (`list[dict]`): 回声模型脚本。
        name (`str`): Agent 名。

    Returns:
        `Agent`: 离线 Agent。
    """
    return Agent(
        name=name,
        system_prompt="你是一个中文助手，回答保持一句话。",
        model=EchoChatModel(stream=False, script=script),
        toolkit=Toolkit(
            tools=[
                FunctionTool(get_time, is_read_only=True),
                FunctionTool(write_note),
            ],
        ),
    )


async def open_session(store: JsonlSessionStore, *, session_id: str, profile: str) -> EventRecord:
    """写一条 ``SESSION_START``（**这一步必须由调用方做**）。

    ``StreamTranslator`` 只翻译 ``Agent.reply_stream`` 吐出来的事件
    （``harness_kit/events/translate.py:395`` 起），它**不会**产 ``SESSION_START``；
    而存储层要靠这条事件读出 ``profile``（见
    ``harness_kit/session/jsonl_store.py:_rebuild_meta``）。
    所以"开一个会话"是应用层的第一件事。

    Args:
        store (`JsonlSessionStore`): 会话存储。
        session_id (`str`): 会话 id。
        profile (`str`): Profile 名。

    Returns:
        `EventRecord`: 写入的事件。
    """
    record = EventRecord(
        session_id=session_id,
        seq=0,
        kind=EventKind.SESSION_START,
        payload={"profile": profile, "agent_name": "Friday", "cwd": os.getcwd()},
    )
    await store.append(record)
    return record


def section(title: str) -> None:
    """打印小节标题。

    Args:
        title (`str`): 标题。
    """
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ======================================================================
# A · JSONL 追加日志
# ======================================================================
async def part_a(root: Path) -> None:
    """A 段：不变式 / 崩溃容忍 / 跨进程写锁。

    Args:
        root (`Path`): 临时根目录。
    """
    section("A · JSONL 追加日志：seq 不变式、崩溃残留、跨进程写锁")
    store = JsonlSessionStore(root / "sessions")
    await open_session(store, session_id="a1", profile="default")
    await store.append(EventRecord(session_id="a1", seq=1, kind=EventKind.REPLY_START,
                                   payload={"reply_id": "r1", "input_preview": "几点了"}))
    print("落盘两行后 latest_seq =", await store.latest_seq("a1"))

    try:
        await store.append(EventRecord(session_id="a1", seq=9, kind=EventKind.REPLY_END,
                                       payload={"reply_id": "r9", "iterations": 1, "tool_calls": []}))
    except SessionInvariantError as exc:
        print("跳号被拒 ->", f"{type(exc).__name__}: {str(exc)[:88]}…")

    # 模拟"崩溃时最后一个 zstd frame 没写完"
    path = root / "sessions" / "a1.jsonl.zst"
    with path.open("ab") as handle:
        handle.write(b"\x28\xb5\x2f\xfd\x00\x00\x00\x00half-written-frame")
    reopened = JsonlSessionStore(root / "sessions")
    events = await reopened.read("a1")
    print("文件尾被写坏后重开：仍能读出", len(events), "条；latest_seq =",
          await reopened.latest_seq("a1"))

    # 跨进程写锁：本进程自己拿住 flock，再让 store 去追加
    import fcntl

    with (root / "sessions" / "a1.lock").open("ab") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            await reopened.append(EventRecord(session_id="a1", seq=2, kind=EventKind.REPLY_END,
                                              payload={"reply_id": "r1", "iterations": 1,
                                                       "tool_calls": []}))
        except SessionLockedError as exc:
            print("另一个进程持锁时追加被拒 ->", f"{type(exc).__name__}: {str(exc)[:60]}…")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    await store.aclose()
    await reopened.aclose()
    print("落盘文件:", sorted(p.name for p in (root / "sessions").iterdir()))


# ======================================================================
# B · SQLite 后端
# ======================================================================
async def part_b(root: Path) -> None:
    """B 段：SQLite 与 JSONL 同构 + blob 外置 + vacuum。

    Args:
        root (`Path`): 临时根目录。
    """
    section("B · SQLite 后端：与 JSONL 同构、blob 外置、vacuum 回收")
    jsonl = JsonlSessionStore(root / "sessions")
    sqlite = SqliteSessionStore(root / "session.db", blob_threshold=512)

    # 两个后端吃**同一批记录对象**：``event_id`` / ``ts`` 都是构造期生成的，
    # 分两次造会拿到不同的随机 id 与不同的时间戳，"等价"就无从谈起。
    big = "grep 出来的日志行\n" * 400
    batch = [
        EventRecord(session_id="b1", seq=0, kind=EventKind.SESSION_START,
                    payload={"profile": "coding", "agent_name": "Friday", "cwd": "/tmp"}),
        EventRecord(session_id="b1", seq=1, kind=EventKind.TOOL_RESULT,
                    payload={"call_id": "c1", "state": "success", "chars": len(big),
                             "error": None, "output": big}),
        EventRecord(session_id="b1", seq=2, kind=EventKind.CUSTOM,
                    payload={"name": "temp", "data": "q" * 8192}),
    ]
    for store in (jsonl, sqlite):
        for record in batch:
            await store.append(record)

    a = await jsonl.read("b1")
    b = await sqlite.read("b1")
    print("两个后端读出来的 SessionEvent 完全相等:", a == b)
    print("SQLite 表行数:", await sqlite.counts(), "| blob_threshold =", sqlite.blob_threshold)
    print("大 payload 原样读回:", b[1].record.payload["output"] == big)
    print("JSONL 侧：文件 =", (root / "sessions" / "b1.jsonl.zst").stat().st_size,
          "字节（zstd 压过的正文全在这一个文件里）")

    # 删掉一条事件再 vacuum：blob 是**派生数据**，没有事件引用它就该被回收。
    # 注意存储层**故意不提供删除 API**（追加日志的语义就是不可变），所以这里
    # 走的是"运维直接开库"的路子 —— 用标准库 sqlite3 连上去删一行。
    import sqlite3

    conn = sqlite3.connect(root / "session.db")
    conn.execute("DELETE FROM events WHERE session_id='b1' AND seq=2")
    conn.commit()
    conn.close()
    print("vacuum:", await sqlite.vacuum())
    print("索引卡片:", (await sqlite.meta("b1")).model_dump(mode="json"))
    await jsonl.aclose()
    await sqlite.aclose()


# ======================================================================
# C/D · 真实 Agent → 总线 → 存储 → 快照 → 跨进程恢复
# ======================================================================
async def part_c(root: Path) -> tuple[Path, str]:
    """C 段：跑一个真 Agent，把事件落盘并收口快照。

    Args:
        root (`Path`): 临时根目录。

    Returns:
        `tuple[Path, str]`: ``(会话目录, 会话 id)``，供 D 段复用。
    """
    section("C · 真实 Agent → EventBus → JsonlSessionStore → Snapshotter")
    session_dir = root / "sessions"
    session_id = "sess-cross-process"
    store = JsonlSessionStore(session_dir)
    await open_session(store, session_id=session_id, profile="default")

    agent = build_echo_agent([
        {"text": "我先查一下时间。", "tool_calls": [
            {"id": "call-1", "name": "get_time", "input": {"city": "北京"}},
        ], "usage": {"input_tokens": 120, "output_tokens": 18}},
        {"text": "北京现在是 2026-09-22 10:00:00。", "usage": {"input_tokens": 180,
                                                              "output_tokens": 12}},
    ])
    agent.state.session_id = session_id

    bus = EventBus()
    sink = StoreSink(store)
    bus.subscribe("*", sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=session_id)
    # 翻译器内部从 0 开始取号（``_seq = 0``），而 0 已经被 SESSION_START 占了。
    # 这就是 ``StreamTranslator.seek()`` 存在的理由：**seq 的权威在存储侧**，
    # 翻译器必须被显式对齐过去，否则第一条记录会因为重号被存储层拒掉。
    translator.seek(await store.latest_seq(session_id) + 1)
    translator.note_input("现在北京几点？")
    count = await translator.consume(agent.reply_stream(UserMsg("user", "现在北京几点？")))
    await bus.drain()
    await bus.aclose()

    print("翻译器产出记录:", count, "| 读到的 AgentEvent 条数:", translator.records_seen,
          "| 跳过:", translator.skipped)
    print("翻译器分类计数:", translator.counts)
    print("落盘事件:", [(r.seq, r.kind.value) for r in sink.records])
    events = await store.read(session_id)
    check_seq_invariants(events, expected_session_id=session_id)
    print("seq 不变式通过：0..", await store.latest_seq(session_id), " 无洞")

    snapshotter = Snapshotter(store, every_n_events=3)
    snapshot = await snapshotter.force_snapshot(agent.state)
    print("快照锚点 seq =", snapshot.seq, "| agent_state 字节数 =",
          len(snapshot.model_dump_json().encode("utf-8")))
    print("AgentState.context 条数 =", len(agent.state.context))
    await store.aclose()
    return session_dir, session_id


async def part_d(session_dir: Path, session_id: str) -> None:
    """D 段：**真的起一个子进程**去恢复同一个会话并接着聊。

    Args:
        session_dir (`Path`): 会话目录。
        session_id (`str`): 会话 id。
    """
    section("D · 跨进程恢复：子进程读同一个会话目录并接着聊")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "third_party" / "ReMe"), str(REF)],
    )
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--resume", str(session_dir), session_id],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    print("$ python scripts/09_session_replay_resume.py --resume", session_dir, session_id)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print("子进程 stderr:", proc.stderr.strip()[-2000:])
        raise SystemExit(f"跨进程恢复失败，退出码 {proc.returncode}")


async def resume_in_process(session_dir: Path, session_id: str) -> int:
    """``--resume`` 模式的实现：恢复 + 回放 + 接着聊一遍。

    Args:
        session_dir (`Path`): 会话目录。
        session_id (`str`): 会话 id。

    Returns:
        `int`: 退出码。
    """
    store = JsonlSessionStore(session_dir)
    snapshotter = Snapshotter(store)
    resumer = SessionResumer(store, snapshotter)
    profile = Profile("default")

    result = await resumer.resume_detailed(session_id, profile=profile)
    print(f"  [pid={os.getpid()}] 快照锚点 seq = {result.snapshot_seq}，"
          f"尾部事件 = {result.tail_events} 条")
    print(f"  [pid={os.getpid()}] interrupted = {result.interrupted}，"
          f"parked_on_confirm = {result.parked_on_confirm}")
    print(f"  [pid={os.getpid()}] 下一步提示：{result.resume_hint}")
    print(f"  [pid={os.getpid()}] 恢复出的历史：")
    for msg in result.state.context:
        print(f"      - {msg.role:9s} {msg.get_text_content()}")

    replay = SessionReplayer(store)
    summary = await replay.summarize(session_id)
    print(f"  [pid={os.getpid()}] 回放摘要：{json.dumps(summary, ensure_ascii=False)}")

    # 接着聊：把恢复出的 state 装进一个**全新的 Agent**
    # 注意先量长度：``agent.state = result.state`` 之后两者**是同一个对象**，
    # reply 会就地改 ``context``，事后再读 result 只会读到"改完之后"的它。
    restored_len = len(result.state.context)
    agent = build_echo_agent([{"text": "北京 10 点，适合出门。"}])
    agent.state = result.state
    msg = await agent.reply(UserMsg("user", "那适合出门吗？"))
    print(f"  [pid={os.getpid()}] 续聊回答：{msg.get_text_content()}")
    print(f"  [pid={os.getpid()}] 续聊后 context 条数 = {len(agent.state.context)}"
          f"（恢复时是 {restored_len}）")
    await store.aclose()
    return 0


# ======================================================================
# E · 失败回放
# ======================================================================
async def part_e(root: Path) -> None:
    """E 段：park 在用户确认上的会话 —— 回放指出问题，恢复给出下一步。

    Args:
        root (`Path`): 临时根目录。
    """
    section("E · 失败回放：park 在用户确认上的会话")
    session_dir = root / "sessions_parked"
    session_id = "sess-parked"
    store = JsonlSessionStore(session_dir)
    await open_session(store, session_id=session_id, profile="default")

    agent = build_echo_agent([
        {"text": "我把这条记下来。", "tool_calls": [
            {"id": "note-1", "name": "write_note", "input": {"text": "记得买牛奶"}},
        ], "usage": {"input_tokens": 90, "output_tokens": 14}},
    ])
    agent.state.session_id = session_id

    bus = EventBus()
    sink = StoreSink(store)
    bus.subscribe("*", sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=session_id)
    translator.seek(await store.latest_seq(session_id) + 1)
    translator.note_input("记一下：记得买牛奶")
    await translator.consume(agent.reply_stream(UserMsg("user", "记一下：记得买牛奶")))
    await bus.drain()
    await bus.aclose()

    print("落盘事件:", [(r.seq, r.kind.value) for r in sink.records])
    print("Agent 停在待确认上：", agent.state.has_awaiting_tool_calls(agent.name))
    print("待确认的工具调用：",
          [(b.name, str(b.state)) for b in agent.state.get_awaiting_tool_calls(agent.name)])
    await Snapshotter(store).force_snapshot(agent.state)
    await store.aclose()

    reopened = JsonlSessionStore(session_dir)
    folded = await SessionReplayer(reopened).fold(session_id)
    print("回放发现的错误：")
    for item in folded.errors:
        print("   -", item)
    print("回放折出的工具调用：", folded.tool_calls)
    print("回放折出的 token：", folded.token_usage.model_dump())

    resumer = SessionResumer(reopened, Snapshotter(reopened))
    result = await resumer.resume_detailed(session_id, profile=Profile("default"))
    print("恢复结论：parked_on_confirm =", result.parked_on_confirm,
          "| awaiting =", [item["name"] for item in result.awaiting_tool_calls])
    print("下一步提示：", result.resume_hint)
    event = resumer.interrupt_event(result.state, profile=Profile("default"))
    print("收口事件（AgentScope 原生）：", type(event).__name__,
          "| reply_id =", None if event is None else event.reply_id)
    await reopened.aclose()


# ======================================================================
# F · 真实模型（可选）
# ======================================================================
async def part_f(root: Path) -> int:
    """F 段：用 deepseek-flash 真跑一轮，再跨 store 实例恢复并接着聊。

    Args:
        root (`Path`): 临时根目录。

    Returns:
        `int`: 实际发生的 LLM 调用次数（没有 key 时为 0）。
    """
    section("F · 真实模型：deepseek-flash 落盘 → 恢复 → 接着聊")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("跳过：.env 里没有 OPENAI_API_KEY")
        return 0

    from agentscope.credential import DeepSeekCredential
    from agentscope.model import DeepSeekChatModel

    base_url = os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com"
    model_name = os.getenv("LLM_MODEL") or "deepseek-chat"
    print("模型:", model_name, "| 端点:", base_url)

    def build(script_ignored: None = None) -> Agent:
        model = DeepSeekChatModel(
            credential=DeepSeekCredential(api_key=api_key, base_url=base_url),
            model=model_name,
            stream=True,
            client_kwargs={"timeout": 60.0},
        )
        return Agent(
            name="Friday",
            system_prompt="你是一个中文助手。回答保持一句话。",
            model=model,
            toolkit=Toolkit(tools=[FunctionTool(get_time, is_read_only=True)]),
        )

    session_dir = root / "sessions_live"
    session_id = "sess-live"
    store = JsonlSessionStore(session_dir)
    await open_session(store, session_id=session_id, profile="default")

    agent = build()
    agent.state.session_id = session_id
    bus = EventBus()
    sink = StoreSink(store)
    bus.subscribe("*", sink)
    await bus.start()
    translator = StreamTranslator(bus, session_id=session_id)
    translator.seek(await store.latest_seq(session_id) + 1)
    question = "用一句话说明「事件溯源」是什么。"
    translator.note_input(question)
    await translator.consume(agent.reply_stream(UserMsg("user", question)))
    await bus.drain()
    await bus.aclose()
    print("第一次回答:", agent.state.context[-1].get_text_content())
    print("落盘事件:", [(r.seq, r.kind.value) for r in sink.records])
    await Snapshotter(store).force_snapshot(agent.state)
    await store.aclose()

    # ---- 新进程语义：新 store + 新 agent -------------------------------------
    store2 = JsonlSessionStore(session_dir)
    resumer = SessionResumer(store2, Snapshotter(store2))
    result = await resumer.resume_detailed(session_id, profile=Profile("default"))
    print("恢复：快照锚点 =", result.snapshot_seq, "| 历史条数 =", len(result.state.context),
          "| 提示 =", result.resume_hint)

    agent2 = build()
    agent2.state = result.state
    follow_up = "把它翻译成英文。"
    msg = await agent2.reply(UserMsg("user", follow_up))
    print("续聊回答:", msg.get_text_content())
    print("续聊后历史条数 =", len(agent2.state.context))
    await Snapshotter(store2).force_snapshot(agent2.state)
    await store2.aclose()

    store3 = JsonlSessionStore(session_dir)
    final = await SessionReplayer(store3).summarize(session_id)
    print("最终回放摘要:", json.dumps(final, ensure_ascii=False))
    print("最终快照数:", len(await store3.list_snapshots(session_id)))
    await store3.aclose()
    return 2


# ======================================================================
# 入口
# ======================================================================
async def main() -> int:
    """跑完全部实验。

    Returns:
        `int`: 退出码。
    """
    logger.remove()
    # 只压掉"价格表里没有 echo"这条噪音（离线回声模型本来就不该记账）；
    # 其余 WARNING 一律保留 —— A 段的"尾部 frame 不完整"正是要看的那条。
    logger.add(
        sys.stderr,
        level="WARNING",
        filter=lambda record: "价格表里没有" not in record["message"],
    )

    print("harness_kit", harness_kit.__version__, "| Python", sys.version.split()[0])
    with tempfile.TemporaryDirectory(prefix="lesson09_") as tmp:
        root = Path(tmp)
        await part_a(root)
        await part_b(root)
        session_dir, session_id = await part_c(root)
        await part_d(session_dir, session_id)
        await part_e(root)
        calls = await part_f(root)

    print("\n" + "=" * 72)
    print(f"全部通过：JSONL / SQLite 事件溯源 + 快照 + 回放 + 跨进程恢复（LLM 调用 {calls} 次）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--resume":
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
        raise SystemExit(asyncio.run(resume_in_process(Path(sys.argv[2]), sys.argv[3])))
    raise SystemExit(asyncio.run(main()))
