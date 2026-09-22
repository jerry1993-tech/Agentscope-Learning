"""02_agentscope_agent_loop 侦察：最小两步工具任务 + 完整事件流打印。

跑法:
    /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
        /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/06_agent_loop_two_step.py
"""
import asyncio
import os
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
from dotenv import load_dotenv

load_dotenv(REPO / ".env")

from agentscope.agent import Agent, ReActConfig  # noqa: E402
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import Msg, UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402
from agentscope.tool import FunctionTool, ToolChunk, Toolkit  # noqa: E402


# ----------------------------------------------------------------------
# 两个自定义工具：第一步取数字，第二步把数字翻倍
# ----------------------------------------------------------------------
async def get_secret_number() -> str:
    """Get the secret number stored in the vault.

    Returns:
        `str`: the secret number as a plain string.
    """
    # 注意：函数可以直接返回 str，FunctionTool 会经
    # _convert_func_result_to_chunk 包成 ToolChunk(content=[TextBlock(...)])。
    # 但如果你自己 new 一个 ToolChunk，content 必须是 list[TextBlock|DataBlock]，
    # 传字符串会 pydantic 校验失败（教学坑点之一）。
    return "7"


async def multiply_by_two(number: int) -> str:
    """Multiply the given integer by two.

    Args:
        number (`int`): the integer to multiply.

    Returns:
        `str`: the doubled value.
    """
    return str(number * 2)


async def main() -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
        ),
        model=os.environ["LLM_MODEL"],
        stream=True,
        parameters=DeepSeekChatModel.Parameters(max_tokens=1024),
    )

    toolkit = Toolkit(
        tools=[
            # is_read_only=True 走权限引擎的 read-only 快速通道，免去用户确认
            FunctionTool(get_secret_number, is_read_only=True),
            FunctionTool(multiply_by_two, is_read_only=True),
        ],
    )

    agent = Agent(
        name="assistant",
        system_prompt=(
            "你是一个会把任务拆成步骤的中文助手。需要用工具时就直接调用工具，"
            "不要在文字里编造工具结果。"
        ),
        model=model,
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=6),
    )

    prompt = (
        "任务分两步：第一步调用 get_secret_number 拿到秘密数字，"
        "第二步把这个数字作为参数调用 multiply_by_two。"
        "最后用一句中文告诉我翻倍后的结果。"
    )

    print("=" * 72)
    print("事件流（reply_stream）")
    print("=" * 72)
    idx = 0
    final: Msg | None = None
    async for item in agent.reply_stream(
        UserMsg("user", prompt),
        yield_final_msg=True,
    ):
        idx += 1
        if isinstance(item, Msg):
            final = item
            print(f"[{idx:03d}] Msg            role={item.role} name={item.name}")
            print(f"        text={item.get_text_content()!r}")
            print(f"        finished_reason={item.finished_reason}")
        else:
            name = type(item).__name__
            extra = ""
            if name == "ToolCallStartEvent":
                extra = f" tool={item.tool_call_name} id={item.tool_call_id}"
            elif name == "ToolCallDeltaEvent":
                extra = f" delta={item.delta!r}"
            elif name == "ToolResultTextDeltaEvent":
                extra = f" delta={item.delta!r}"
            elif name == "ToolResultEndEvent":
                extra = f" state={item.state}"
            elif name == "ModelCallEndEvent":
                extra = (
                    f" in={item.input_tokens} out={item.output_tokens}"
                    f" cache={item.cache_input_tokens}"
                    f" reason={item.finished_reason}"
                )
            elif name == "ReplyEndEvent":
                extra = f" reason={item.finished_reason}"
            elif name == "HintBlockEvent":
                extra = f" hint={item.hint!r}"
            elif name == "TextBlockDeltaEvent":
                extra = f" delta={item.delta!r}"
            print(f"[{idx:03d}] {name}{extra}")

    print()
    print("=" * 72)
    print("收尾状态")
    print("=" * 72)
    print("state.reply_id    =", agent.state.reply_id)
    print("state.cur_iter    =", agent.state.cur_iter)
    print("len(context)      =", len(agent.state.context))
    for i, m in enumerate(agent.state.context):
        kinds = [type(b).__name__ for b in m.content]
        print(f"  context[{i}] role={m.role} name={m.name} blocks={kinds}")
    if final is not None:
        print("final.usage       =", final.usage)


asyncio.run(main())
