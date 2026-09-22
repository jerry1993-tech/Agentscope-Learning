# 《Agent Harness 全栈 20 天》—— 基于 AgentScope + ReMe 打造企业级 Agent Harness

> 本目录是这套教程的**唯一入口**。20 讲正文 + 一份可运行参考实现 + 16 份源码侦察报告，
> 全部在本目录内，可直接照抄、逐文件复刻、每步验证。

---

## 一、这套教程是什么

一套**面向生产、可逐文件复刻、每步可验证**的 Agent Harness 构建教程。

你要在一个空白目录里，完全照着教程一步步写代码，最终搭出一套**建立在
[AgentScope](https://github.com/modelscope/agentscope) 2.0.8 与
[ReMe](https://github.com/modelscope/ReMe) 0.4.1.13 之上**的企业级 Agent Harness ——
包名 `harness_kit`，并且**不修改 `third_party/` 下任何一行代码**。

### 三条不能动摇的立场

| # | 立场 | 具体含义 |
| --- | --- | --- |
| 1 | **不重造轮子** | 严禁手写 Agent Loop / `ChatModelBase` / `Toolkit` / ReMe 的 chunker·BM25·RRF。没有 `mini_harness`，不做原理重写 |
| 2 | **三步法** | 全部动手内容统一为：**精读两个库的真实源码 → 找到它们提供的扩展点 → 用扩展点写出生产级组件** |
| 3 | **`harness_kit` 是装配层，不是内核** | 它只做三件事：**装配**（Profile/Bundle → 真实对象）、**补齐**（两个库确实缺的不可变事件日志、事件总线、评测引擎、token 预算、沙箱配额）、**服务化**（CLI / FastAPI / SSE / MCP Server） |

### 用到的真实扩展点（每一条都在 `_recon/` 里有 `路径:行号` 佐证）

- **agentscope**：`Agent` 装配参数、`FormatterBase` 子类、`ChatModelBase` 子类、
  `ToolBase` / `FunctionTool` / `Toolkit`、`SkillBase` 与技能加载、`MiddlewareBase` 中间件链、
  `WorkspaceBase` 与各沙箱后端（local / docker / e2b / k8s）、`PermissionEngine` 与 `PermissionRule`、
  pipeline 与 sop 引擎、MCP 客户端、rag 与 embedding、`app/` 服务层（FastAPI / storage / message_bus）、
  console 与 tui。
- **reme**：`Application` 装配与 `component_registry`、自定义 `Component`、自定义 `Step`、
  Job（base / stream / background / cron）、插件（plugin manifest）、`file_store` / `keyword_index` /
  `file_graph` / `tag_index` / `embedding_store` 等组件、HTTP 与 MCP 服务、CLI。

### 学员的最终交付形态

1. **一个通用企业级 Harness 脚手架**（可复用的生产底座）——`harness_kit`；
2. **一个跑得通的业务 demo**——`harness_kit/demo/code_assistant/`：一个代码库问答助手，
   用 ReMe 做记忆与文档索引、用 AgentScope 做 Agent 运行时，端到端跑通，回答带可核对的引用。

`harness_kit` 只写这两个库**没有**的东西：配置化组装（Profile / Bundle）、会话事件溯源与断点续跑、
生产工具包、治理中间件、权限策略包、长任务编排、记忆门面、评测、可观测、服务化、命令行。

---

## 二、20 天能学到什么

每天一讲，每讲 90~240 分钟。路线是「地基 → 执行引擎 → 持久记忆 → 收口」四段。

| 阶段 | 讲次 | 学完你应该能独立做到 | 时长合计 |
| --- | --- | --- | --- |
| **地基** | 01~04 | 在任何新项目里 30 分钟内起一个可跑的 Agent，并且知道它每一步走的是框架的哪段代码；能自己写 Profile/Bundle 装配、事件总线、自定义模型适配器 | 570 min |
| **执行引擎** | 05~14 | 给 Agent 装上工具、技能、MCP、沙箱、权限、规划、子智能体、结构化输出，并且每一样都能说清「官方给了什么、我加了什么」 | 1380 min |
| **持久记忆** | 15~19 | 让 Agent 拥有可检索、可演化、可隔离的长期记忆，并能控制注入成本 | 720 min |
| **收口** | 20 | 把整套东西评测、观测、服务化，让线上失败回流成回归集并交给闸门判定，最后用一个真实 demo 收尾 | 240 min |

一句话数据流（第 20 讲会拆成 11 步逐步展开）：

> 用户输入经过 Profile 装配出的 Agent，在中间件链内调用模型、按需调用工具与检索记忆，
> 全过程落成事件日志，最后输出答案与引用。

---

## 三、目录结构

真实清单（`ls -la` / `wc -l` 实测，2026-09-22）：

```text
tutorial_agsc_reme/
├── README.md                                  # 本文件：教程入口
├── harness_00_教程总览与学习路线.md              # 总览：路线图 + 完整目录事实 + 数据流 11 步
├── harness_01 … harness_20_*.md               # 20 讲正文（见下一节表格）
├── _contract.md                               # 作者内部工程契约（2372 行，非正文）
├── _recon/                                    # 16 份源码侦察报告（18670 行，全部结论的出处）
│   ├── 00_environment_and_smoke.md            # 环境修复与最小可运行样例
│   ├── 01…10_agentscope_*.md                  # AgentScope 各子系统侦察
│   ├── 11…14_reme_*.md                        # ReMe 各子系统侦察
│   └── 15_integration_agentscope_reme.md      # 两库集成侦察
└── reference/                                 # ★ 参考实现（教程的真值来源）
    ├── README.md                              # 参考实现速览
    ├── pyproject.toml                         # 依赖契约 + pytest 配置
    ├── .env.example                           # 环境变量模板（复制到仓库根 .env）
    ├── harness_kit/                           # 包本身（114 个 Python 文件 + 9 个 YAML）
    ├── scripts/                               # 每讲一个可执行验证脚本（25 个）
    ├── tests/                                 # 每讲一套 pytest（21 个文件）
    ├── logs/                                  # 运行日志（运行期产物，不入库）
    ├── .harness/                              # 运行期工作区（会话日志、ReMe 索引、workspace；不入库）
    └── .pytest_cache/                         # 运行期缓存（不入库）
```

> 注：早期用作「写作范式参照」的两份 `rag_modules_00_*.md` / `rag_modules_07_*.md` 已于 2026-09-21 清理时移除，
> 它们从未进入 git，仓库内没有副本；讲次正文与 `_recon/` 都不依赖它们。

`reference/harness_kit/` 的 19 个子包与讲次对应：

| 讲次 | 子包 / 文件 | 讲次 | 子包 / 文件 |
| --- | --- | --- | --- |
| 01 | `__init__.py`、`settings.py` | 11 | `permission/`（rules / policy / hitl / audit + rules/*.yaml） |
| 02 | `config/`、`registry.py` | 12 | `planning/`（graph / planner / sop / resume + sops/*.yaml） |
| 03 | `events/`、`session/models.py` | 13 | `multiagent/`（team / router / handoff / limits） |
| 04 | `models/`（factory / formatter / pricing / ratelimit / adapters） | 14 | `reasoning/`（structured / critique / prompt） |
| 05 | `tools/`（pack / builtin_pack / repo_pack / utils） | 15~19 | `memory/`（21 个文件，见 §4.1 参考实现展开） |
| 06 | `skills/`（loader / manifest / builtin） | 20 | `eval/`、`observe/`、`service/`、`profiles/`、`cli.py`、`demo/` |
| 07 | `mcp/`（registry / adapter / server） | — | — |
| 08 | `middleware/`（base / logging / budget / redact / tracing / guards / compact） | — | — |
| 09 | `session/`（store / jsonl_store / sqlite_store / snapshot / replay / resume） | — | — |
| 10 | `sandbox/`（policy / local / docker / offload / guard） | — | — |

---

## 四、每一讲的文件与真实体量

全部 21 个正文文件合计 **129,194 行 / 5.91 MB**（`wc -l` + `stat -f%z` 实测）：

| 讲次 | 文件名 | 行数 | 大小 | 主题关键词 |
| --- | --- | ---: | ---: | --- |
| 00 | `harness_00_教程总览与学习路线.md` | 539 | 35.8 KB | 路线图、目录事实、数据流 11 步 |
| 01 | `harness_01_学习路线与环境准备.md` | 3,241 | 170.9 KB | PYTHONPATH 隔离、`.env`、首个 Agent、首次 ReMe 检索 |
| 02 | `harness_02_AgentScope核心解剖_Agent与主循环.md` | 5,420 | 232.8 KB | `Agent`、`_reply_impl`、`_next_action`、Profile/Bundle、Registry |
| 03 | `harness_03_消息块事件与状态.md` | 5,137 | 235.2 KB | `Msg` 与块类型、事件类型、`AgentState`、事件总线与翻译 |
| 04 | `harness_04_模型适配层与自定义适配器.md` | 7,027 | 293.4 KB | `ChatModelBase`、流式累积、formatter、token 计数、prompt cache |
| 05 | `harness_05_工具系统与生产工具包.md` | 3,620 | 174.8 KB | `ToolBase`、JSON Schema 生成、`Toolkit`、参数修复、内置工具 |
| 06 | `harness_06_Skills技能包.md` | 5,517 | 236.3 KB | SKILL.md 规范、技能加载器、渐进披露 |
| 07 | `harness_07_MCP工具协议.md` | 5,705 | 256.0 KB | MCP client、三种 transport、命名空间、harness_kit 暴露为 MCP Server |
| 08 | `harness_08_中间件与Hook链.md` | 9,119 | 403.8 KB | `MiddlewareBase` onion hook、预算 / 追踪 / 脱敏 / 守卫、上下文压缩 |
| 09 | `harness_09_会话事件溯源与回放.md` | 5,841 | 268.5 KB | 不可变日志、SQL / Redis 表结构、快照、回放、恢复 |
| 10 | `harness_10_Workspace与安全沙箱.md` | 6,221 | 298.8 KB | `WorkspaceBase`、Docker / E2B / K8s、offload、配额 |
| 11 | `harness_11_权限引擎与危险操作拦截.md` | 7,059 | 349.6 KB | 权限规则、bash 解析、危险命令检测、HITL、审计 |
| 12 | `harness_12_Planning与SOP长任务.md` | 7,642 | 392.4 KB | pipeline 与 SOP 引擎、目标分解、状态机持久化与续跑 |
| 13 | `harness_13_Subagent与多智能体.md` | 6,040 | 304.5 KB | sub-agent、A2A、派生与隔离、路由、失败传播 |
| 14 | `harness_14_Reasoning与结构化输出.md` | 5,175 | 258.4 KB | thinking 块、结构化输出工具、system prompt 装配、指纹 |
| 15 | `harness_15_ReMe架构总览与配置.md` | 5,672 | 305.1 KB | CLI→Client→Service→Application→Job→Step→Component→Workspace、体检 |
| 16 | `harness_16_ReMe记忆写入与文件原生存储.md` | 6,242 | 306.5 KB | `FileNode`/`FileChunk`/`FileLink`、Markdown 分块、wikilink、tag_index |
| 17 | `harness_17_ReMe混合检索.md` | 5,471 | 282.9 KB | BM25、向量、tag、图扩展、RRF、passage 抽取与引用 |
| 18 | `harness_18_ReMe自演化.md` | 5,958 | 277.1 KB | `auto_memory`、`auto_dream`、cron / 后台 job、agent_wrapper |
| 19 | `harness_19_长期记忆中间件集成.md` | 6,792 | 306.5 KB | 官方 `middleware/_longterm_memory/_reme`、Recorder / Retriever、门控与预算 |
| 20 | `harness_20_评测可观测服务化与代码助手demo.md` | 15,756 | 659.3 KB | 评测引擎、反馈闭环、OTel、FastAPI + SSE + Web UI、端到端 demo、容器化 |

> 第 20 讲体量约等于两讲半，建议单独留一个完整的半天。
> 讲次与文件名的对应关系由 `harness_00` §4.1 与 §5 两张表维护，已与磁盘逐一核对一致。

---

## 五、推荐阅读方式

1. **先读 `harness_00` 通读一遍**（约 20 分钟），把「六缺口」「数据流 11 步」「职责边界三问」刻进脑子。
   之后每一讲遇到「这个要不要我自己写」，都用那三问回答：
   官方有现成基类/hook → 继承；官方有但缺声明式外层 → 外面包一层；官方完全没有 → 才允许新写。
2. **每讲的代码自己敲一遍。** 正文代码与 `reference/harness_kit/` 逐字一致，
   但「复制粘贴」和「手敲一遍」的收获差一个量级。参考实现是用来**对照纠错**的，不是用来抄的。
3. **每讲先跑 `reference/scripts/NN_*.py`**，看到真实输出，再回头读正文——顺序反过来容易读成散文。
4. **`_recon/` 当字典查，不要通读。** 正文里每一个 `路径:行号` 的原始证据都在那里；
   当你不信正文某句话时，去 `_recon/` 找那一行源码。
5. **每讲末尾有 5~10 道自测题**，答不出来就别往下走（契约 §9 的硬性要求）。
6. 想快速判断自己学完没有：跑 `reference/tests/` 里对应那一讲的 pytest，全绿即达标。

---

## 六、运行环境要求

### 6.1 Python 与两个库

| 项 | 值 |
| --- | --- |
| 解释器 | `/Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python`，**Python 3.11.13** |
| conda 环境名 | `agentscope_reme_pip_env` |
| AgentScope | **2.0.8**，`pip install -e third_party/agentscope`（源码在 `third_party/agentscope/src/agentscope/`），直接 `import` |
| ReMe | **0.4.1.13**，克隆在 `third_party/ReMe/`，**不安装**，靠 `PYTHONPATH` 引入 |

> `pyproject.toml` 里的 `requires-python = "==3.11.*"` 与
> `agentscope==2.0.8` / `reme==0.4.1.13` 是**版本契约**，不是给 pip 解析用的：
> 教程里每一个 `路径:行号` 都是针对这两个确切版本核实过的。

### 6.2 PYTHONPATH 隔离旧版 ReMe（最容易翻车的一处）

环境里同时存在两个 ReMe：

- `third_party/ReMe/` 的 **0.4.1.13**（教程要用的）
- site-packages 里 PyPI 装的 `reme_ai==0.3.1.10`，它一口气占了 `reme/`、`reme4/`、`reme_ai/` 三个顶层包名

**不处理的话 `import reme` 会静默拿到 0.3.1.10**，而它内部
`from agentscope.token import HuggingFaceTokenCounter` 在 AgentScope 2.0.8 里根本不存在，
直接 `ImportError`。所以：

```bash
# 必须让 third_party/ReMe 排在 site-packages 之前
export PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe
```

**所有脚本一律带上这条 `PYTHONPATH`**，没有例外。自检「导对了没」的唯一依据：

```bash
PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe \
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
  -c "import reme; print(reme.__version__)"     # 必须打印 0.4.1.13
```

### 6.3 `.env` 变量清单

`.env` 放在**仓库根**（`/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/.env`），
**不是**本目录。模板见 `reference/.env.example`。

原因：`harness_kit.settings.Settings` 里 `_REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[3]`
从 `reference/harness_kit/settings.py` 上溯三层正好是仓库根，
`Settings.from_env()` 只读那一个 `.env`。

变量名的坑在于**存在两套约定**：

| 约定 | 变量名 | 谁认它 |
| --- | --- | --- |
| A（ReMe `as_llm` 组件） | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME` / `LLM_BACKEND` | ReMe 的 `default.yaml` |
| B（OpenAI 兼容工具通用） | `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL` | 本仓库既有 `.env`、多数工具链 |

`Settings` 用 pydantic 的 `AliasChoices` **同时接住两套名字**，字段统一叫
`llm_api_key` / `llm_base_url` / `llm_model_name`，所以两种写法二选一即可。当前仓库根
`.env` 用的是写法 B（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL`，实测可用
`deepseek-flash @ https://api.deepseek.com`）。

环境变量总览：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` / `LLM_API_KEY` | 是 | LLM 密钥（二选一） |
| `OPENAI_BASE_URL` / `LLM_BASE_URL` | 是 | OpenAI 兼容端点，如 `https://api.deepseek.com` |
| `LLM_MODEL` / `LLM_MODEL_NAME` | 是 | 模型名，如 `deepseek-flash` / `deepseek-chat` |
| `LLM_BACKEND` | 否 | 仅写法 A 需要，取值 `openai` |
| `HARNESS_REPO_ROOT` | 否 | 覆盖 `Settings.repo_root`；默认自动上溯到仓库根。把参考实现拷到别处时必须显式指定 |
| `HARNESS_*` | 否 | `Settings` 的其余字段都支持 `HARNESS_` 前缀环境变量覆盖 |

`.env` 由 `settings.py` 用 `load_dotenv` **显式加载**，与当前工作目录无关——
在任何目录 `cd` 过去跑都不会读错密钥。

### 6.4 工程纪律（违反会导致教程代码跑不起来）

1. `third_party/` **只读**，一个字节都不许改。
2. 所有脚本一律 `PYTHONPATH=.../third_party/ReMe` 运行。
3. **不起常驻端口服务**：ReMe 一律用嵌入式装配（`reme.ReMe(**config)` 或
   `reme.application.Application`）跑 `run_job`，不要起 HTTP 服务。
   确需端口一律 `≥ 18000` 且用完即关（`harness-kit serve` 默认 `127.0.0.1:18420`）。
4. 每个验证脚本的 LLM 调用 **≤ 6 次**。
5. 其它依赖均为本环境已装好的：`pydantic` v2、`pydantic-settings`、`loguru`、`numpy`、
   `fastapi`、`uvicorn`、`mcp`、`httpx`、`pyyaml`、`zstandard`、`python-dotenv`，
   测试用 `pytest` + `pytest-asyncio`。

---

## 七、`reference/harness_kit` 参考实现的用法

参考实现根：`tutorial_agsc_reme/reference/`。下面所有命令都在该目录下执行。

```bash
export REPO=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning
export REF=$REPO/tutorial_agsc_reme/reference
export PY=/Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python
cd $REF
```

### 7.1 怎么跑它

```bash
# 第 1 讲：环境自检 + 首个 Agent + 首次 ReMe 检索
PYTHONPATH=$REPO/third_party/ReMe $PY scripts/00_smoke.py

# 逐讲验证脚本（每讲一个，跑出与教程正文一致的真实输出）
PYTHONPATH=$REPO/third_party/ReMe $PY scripts/03_events_and_state.py
PYTHONPATH=$REPO/third_party/ReMe $PY scripts/09_session_replay_resume.py
# …scripts/00 ~ scripts/20，另有 08_context_compaction.py / 20_feedback_loop.py 两个补遗

# 单侧离线单元测试（不调 LLM；已验证可收集 1041 个用例）
PYTHONPATH=$REPO/third_party/ReMe:$REF $PY -m pytest -q
PYTHONPATH=$REPO/third_party/ReMe:$REF $PY -m pytest -q tests/test_lesson11_permission.py

# 命令行总闸门（8 个子命令：run / doctor / eval / feedback / serve / replay / memory / profile）
PYTHONPATH=$REPO/third_party/ReMe:$REF HARNESS_REPO_ROOT=$REF $PY -m harness_kit.cli doctor
PYTHONPATH=$REPO/third_party/ReMe:$REF HARNESS_REPO_ROOT=$REF $PY -m harness_kit.cli profile list
```

`HARNESS_REPO_ROOT` 默认会钉到 `reference/`（Profile 里写的
`./harness_kit/profiles`、`./.harness/...` 都是相对 `reference` 的），只有把
参考实现整体搬走时才需要改它。

### 7.2 怎么用它对照学习

参考实现是**真值来源**，正文代码逐字抄自它。推荐两种对照姿势：

```bash
# 姿势一：抄完一节，diff 自己的文件与参考实现
diff -u /path/to/my_project/harness_kit/session/jsonl_store.py $REF/harness_kit/session/jsonl_store.py

# 姿势二：只对照接口签名，不看实现（避免直接抄）
grep -nE "^(class|    def|async def)" $REF/harness_kit/permission/policy.py
```

配套的 `reference/tests/` 是**验收标准**：你写的模块能让自己那一讲的 pytest 全绿，
说明这一讲过关了。`scripts/NN_*.py` 则是**可复现的输出样本**——
正文里贴的真实运行输出就是运行它们得到的。

### 7.3 业务 demo 怎么跑

Demo 位置：`$REF/harness_kit/demo/code_assistant/`（另有 `Dockerfile` 与 `deployment.yaml`）。
它是一个代码库问答助手：把一小段代码库灌进 ReMe 索引 → 提问 → 回答带出处，
且出处能与「本次真正检索到的来源」对上。它证明了第 20 讲的四层
（AgentScope Agent Loop / ReMe 索引检索 / harness_kit 装配 / 治理与引用核对）**确实能装成一个产品形态**。

```bash
export HARNESS_REPO_ROOT=$REF

# 端到端四步：索引 → 装配 → 提问 → 核对引用（会真的调用模型）
PYTHONPATH=$REPO/third_party/ReMe:$REF $PY -m harness_kit.demo.code_assistant.main --limit 6

# 复用已有索引，换一个「答案在默认语料里」的问题
PYTHONPATH=$REPO/third_party/ReMe:$REF $PY -m harness_kit.demo.code_assistant.main \
  --skip-ingest --question "Agent.observe 方法是做什么的？它和 reply 有什么不同？给出出处"

# 只跑写入侧（幂等：同一条命令重跑应全部显示「未变」）
PYTHONPATH=$REPO/third_party/ReMe:$REF $PY -m harness_kit.demo.code_assistant.ingest_repo --limit 6 --json
```

模型凭据从仓库根 `.env` 读，与当前工作目录无关。demo 的装配用的是
`harness_kit/profiles/researcher_with_memory.yaml`（只读权限模式 `explore` +
`LongTermMemoryMiddleware`），索引落在 `reference/.harness/reme/code_assistant/`。
更详细的四步讲解与真实输出见 `reference/harness_kit/demo/code_assistant/README.md`。

> **换问题时注意语料边界**：默认索引语料是
> `DEFAULT_TARGET = third_party/agentscope/src/agentscope/agent`（12 个文件），
> 而第 4 步「引用核对」比对的是**本次检索命中**与回答里写出的路径。
> 问一个答案不在语料里的问题（例如 `Toolkit.add_tool`，它在 `agentscope/tool/` 下）
> 时，模型只能用自己的 `Grep`/`Read` 去磁盘上找并引用真实源码路径 ——
> 回答是对的，第 4 步却会报「未通过」。要问别的子树就换 `--target` 重新索引，
> 并让问题与语料同源、`--limit` 给够。

---

## 八、本次全局核对结果（2026-09-22）

| 核对项 | 结论 |
| --- | --- |
| `harness_00` §4.1 / §5 路线表 vs 磁盘文件 | **21/21 一致**（00~20 全部存在，无缺无多） |
| `harness_00` 引用的全部 `harness_kit/` `scripts/` `tests/` 路径 | **全部存在**，无悬空引用 |
| `scripts/` 清单（25 个）vs 正文声明 | 一致 |
| `memory/` 文件数 | 正文称 21 个，实测 20 个模块 + `__init__.py` = 21，一致 |
| `tests/` 第 20 讲用例数 | 正文称 68，实测 `def test_` 68，一致 |
| `cli.py` 子命令数 | 正文称 8，实测 `run/doctor/eval/feedback/serve/replay/memory/profile` = 8，一致 |
| `harness_00` 目录树中的 `session/`、`models/` 模块名 | **发现 2 处简写与真实文件名不符，已修正**（见下） |
| 参考实现整体 import | **通过**，见下 |

### 已修正的不一致

1. `harness_00` §4 目录树 `session/` 一行原写 `jsonl / sqlite`，实际文件名是
   `jsonl_store.py` / `sqlite_store.py` —— 已改为真实文件名并补上 `__init__`。
2. `harness_00` §4 目录树 `models/` 一行漏了 `formatter.py` —— 已补。
3. `harness_00` §4.1 第 09 讲交付模块原写 `session/{store,jsonl,sqlite,snapshot,replay,resume}.py`，
   已改为 `session/{models,store,jsonl_store,sqlite_store,snapshot,replay,resume}.py`。

### 参考实现 import 校验

```bash
PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:\
/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference \
/Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
-c "import harness_kit, pathlib; print(harness_kit.__file__)"
```

真实输出（一次通过，无需修复）：

```text
/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference/harness_kit/__init__.py
```

环境侧同时确认：`Python 3.11.13` / `agentscope 2.0.8` /
`reme 0.4.1.13`（来自 `third_party/ReMe/reme/`，PYTHONPATH 隔离生效）。

---

> **下一步**：进入 [`harness_00_教程总览与学习路线.md`](./harness_00_教程总览与学习路线.md)，
> 读完总览后从 [`harness_01_学习路线与环境准备.md`](./harness_01_学习路线与环境准备.md) 开始动手。
