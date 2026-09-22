"""环境自检脚本：一条命令验完 AgentScope / ReMe / LLM 三件事。

运行：
  PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python 05_env_check.py
"""
import asyncio
import os
import sys
from pathlib import Path

REPO = Path("/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning")
sys.path.insert(0, str(REPO / "third_party" / "ReMe"))

from dotenv import load_dotenv

load_dotenv(REPO / ".env")

OK, BAD = "✅", "❌"

# 1) AgentScope
import agentscope  # noqa: E402

print(f"{OK} agentscope {agentscope.__version__} -> {agentscope.__file__}")
assert agentscope.__path__[0].startswith(str(REPO)), "agentscope 不是 -e 安装的本地源码"

# 2) ReMe
import reme  # noqa: E402

print(f"{OK} reme {reme.__version__} -> {reme.__file__}")
assert reme.__version__ == "0.4.1.13", f"版本不对：{reme.__version__}，你可能漏了 PYTHONPATH"
assert str(REPO / "third_party/ReMe") in reme.__file__, f"导到的不是本地源码：{reme.__file__}"

# 3) LLM
from agentscope.credential import DeepSeekCredential  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.model import DeepSeekChatModel  # noqa: E402


async def ping() -> str:
    model = DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"]
        ),
        model=os.environ["LLM_MODEL"],
        stream=False,
        parameters=DeepSeekChatModel.Parameters(max_tokens=256),
    )
    res = await model([UserMsg("user", "只回答两个字：收到")])
    return res.content[0].text


text = asyncio.run(ping())
print(f"{OK} LLM {os.environ['LLM_MODEL']} @ {os.environ['OPENAI_BASE_URL']} -> {text!r}")
print("\n环境自检全部通过。")
