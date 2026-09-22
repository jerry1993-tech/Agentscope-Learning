# Agentscope-Learning

学习工作区。成品是 [`tutorial_agsc_reme/`](tutorial_agsc_reme/) 里的
**《Agent Harness 全栈 20 天》**——一套基于 **AgentScope + ReMe** 拆解教学的企业级
Agent Harness 教程，外加一份与正文逐字对应、可直接跑通的参考实现。

## 从这里开始

1. [`tutorial_agsc_reme/README.md`](tutorial_agsc_reme/README.md) —— **教程总入口**
   （目录结构、每讲体量、运行方式、`.env` 变量清单、全局核对结果）
2. [`harness_00_教程总览与学习路线.md`](tutorial_agsc_reme/harness_00_教程总览与学习路线.md)
   —— 路线图 + 数据流 11 步（先读这一份，约 20 分钟）
3. [`harness_01_学习路线与环境准备.md`](tutorial_agsc_reme/harness_01_学习路线与环境准备.md)
   —— 从这一讲开始动手

## 内容一览

| 路径 | 说明 |
| --- | --- |
| `tutorial_agsc_reme/harness_00…20_*.md` | 21 讲正文，合计约 129,000 行 |
| `tutorial_agsc_reme/_recon/` | 16 份源码侦察报告（18,670 行），正文里每个 `路径:行号` 的原始证据 |
| `tutorial_agsc_reme/reference/` | **参考实现** `harness_kit/`：114 个 Python 模块 + 9 个 YAML、21 套 pytest、25 个逐讲验证脚本 |

## 环境（三步）

```bash
# 1) 两个库：third_party/ 不入库，自行克隆（版本契约：agentscope 2.0.8 / reme 0.4.1.13）
git clone https://github.com/agentscope-ai/agentscope.git third_party/agentscope
git clone https://github.com/agentscope-ai/ReMe.git       third_party/ReMe
pip install -e third_party/agentscope

# 2) 凭据：模板在 reference/，但要放到**仓库根**的 .env 里
cp tutorial_agsc_reme/reference/.env.example .env    # 然后填自己的 KEY

# 3) PYTHONPATH 必须让 third_party/ReMe 排在 site-packages 之前（否则会 import 到旧版 ReMe）
export PYTHONPATH="$PWD/third_party/ReMe:$PWD/tutorial_agsc_reme/reference"
python tutorial_agsc_reme/reference/scripts/00_smoke.py
```

<details>
<summary>为什么 PYTHONPATH 这么写（最容易翻车的一处）</summary>

`pip` 里那个 `reme 0.3.1.10` 是坏的；教程针对的是 `third_party/ReMe/` 里克隆的
`0.4.1.13`。不设 `PYTHONPATH` 时 `import reme` 会静默拿到旧的那个。
详见 [`tutorial_agsc_reme/README.md`](tutorial_agsc_reme/README.md) §6.2。
</details>

## 关于 `.env`

仓库里**只有模板** `tutorial_agsc_reme/reference/.env.example`（内容全是占位符）。
真实的 `.env` 已被 [`.gitignore`](.gitignore) 排除，任何情况下都不要提交它。

## 逐讲验证

正文里的每段输出都由 `reference/scripts/NN_*.py` 真实跑出来，每讲配一套离线 pytest：

```bash
# 逐讲验证脚本
python tutorial_agsc_reme/reference/scripts/11_permission.py

# 单侧离线单元测试（不调 LLM）
pytest tutorial_agsc_reme/reference/tests/
```
