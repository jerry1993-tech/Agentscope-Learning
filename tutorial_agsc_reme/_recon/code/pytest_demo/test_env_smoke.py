"""最小 pytest：验证 agentscope 可导入 + LLM 可调通。"""
import asyncio

import agentscope
import pytest
from agentscope.credential import DeepSeekCredential
from agentscope.model import DeepSeekChatModel


def test_agentscope_version() -> None:
    assert agentscope.__version__ == "2.0.8"


@pytest.mark.asyncio
async def test_llm_call(llm_env: dict) -> None:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(api_key=llm_env["OPENAI_API_KEY"], base_url=llm_env["OPENAI_BASE_URL"]),
        model=llm_env["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=256),
    )
    from agentscope.message import UserMsg

    # 注意：ChatModelBase.__call__ 的参数是 list[Msg]，传单个 Msg 会 TypeError
    res = await model([UserMsg("user", "只回答两个字：收到")])
    text = res.content[0].text if res.content else ""
    assert text, f"empty reply: {res}"
