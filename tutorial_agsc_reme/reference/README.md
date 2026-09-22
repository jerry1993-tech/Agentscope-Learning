# harness_kit 参考实现

这是《Agent Harness 全栈 20 天教程》（`harness_00` ~ `harness_20`）的**参考实现**，
也是整套教程的**真值来源**：教程正文里的每一段代码都从这里逐字抄过去。

## 它是什么

`harness_kit` 是一个**装配层**，不是一个 Agent 内核。它建立在

- **AgentScope 2.0.8**（`third_party/agentscope/src/agentscope/`，`pip install -e`）
- **ReMe 0.4.1.13**（`third_party/ReMe/reme/`，靠 `PYTHONPATH` 引入）

之上，只做三件事：**装配**（Profile/Bundle → 真实对象）、**补齐**（补上两个库确实缺失的
不可变事件日志、事件总线、评测引擎、token 预算、沙箱配额、声明式 Profile）、
**服务化**（CLI / FastAPI / SSE / MCP Server）。

它**不**重写 Agent Loop、`ChatModelBase`、`Toolkit`、`ReMe` 的 chunker / BM25 / RRF。
详见 `../_contract.md`。

## 环境

| 项 | 值 |
| --- | --- |
| Python | 3.11.13（`/Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python`） |
| AgentScope | 2.0.8（`-e` 安装，直接 `import`） |
| ReMe | 0.4.1.13（**必须** `PYTHONPATH=third_party/ReMe`，否则会静默拿到 site-packages 里的 0.3.1.10） |

## 跑起来

所有命令都在 `tutorial_agsc_reme/reference/` 下执行。

```bash
# 第 1 讲：环境自检 + 首个 Agent + 首次 ReMe 检索
PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python scripts/00_smoke.py

# 单元测试（离线，不调 LLM）
PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest -q
```

`.env` 放在**仓库根**（不是本目录），模板见 `.env.example`。

## 目录

```
reference/
├── pyproject.toml            # 依赖声明 + pytest 配置
├── .env.example              # 环境变量模板（放到仓库根 .env）
├── README.md                 # 本文件
├── scripts/                  # 每讲一个可执行验证脚本
│   ├── 00_smoke.py           # 第 1 讲
│   └── mcp_demo_server.py    # 第 7 讲
├── tests/                    # pytest 用例（离线优先）
└── harness_kit/              # 包本身，按讲次逐模块交付
```

`harness_kit/` 内部各子包与讲次的对应关系见 `../_contract.md` 第二节。

## 纪律

1. `third_party/` **只读**，一个字节都不许改。
2. 所有脚本一律 `PYTHONPATH=.../third_party/ReMe` 运行。
3. 不起常驻端口服务；确需端口一律 `≥ 18000` 且用完即关。
4. 每个验证脚本的 LLM 调用 `≤ 6` 次。
