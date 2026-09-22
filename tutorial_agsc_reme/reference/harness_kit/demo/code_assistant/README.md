# 代码助手 Demo（第 20 讲）

一个**能跑**的代码库问答助手：把一小段代码库灌进 ReMe 索引，然后提问，
回答里带出处，且出处能与"本次真正检索到的来源"对上。

这个 Demo 的存在意义不是"又一个 RAG 例子"，而是把第 20 讲的四层拼在一起，
证明它们**确实能装成一个产品形态**：

| 这件事 | 用的是谁 | 本 Demo 里的落点 |
| --- | --- | --- |
| Agent Loop / Tool Use / context | AgentScope 原生 `Agent`（`third_party/agentscope/src/agentscope/agent/_agent.py:117`） | `agent.py` 的 `build_code_assistant` |
| 索引 / 检索 / 写回 | ReMe 嵌入式（`reme.ReMe(**config)`）经第 15 讲的 `harness_kit/memory/*` | `ingest_repo.py`（写）、`agent.py` 的 `recall`（读） |
| 索引产出的可读性 | 本 Demo 的渲染层（代码 → markdown，见下） | `ingest_repo.py` 的 `render_source_document` |
| 治理：Profile / 权限 / 中间件 / 预算 | 第 2 讲的 `HarnessBuilder` + 第 8 讲的中间件 + 第 11 讲的权限引擎 | `harness_kit/profiles/researcher_with_memory.yaml` |

**没有一行是"重写的内核"**：Agent Loop 是 AgentScope 的
`Agent.reply_stream`，检索是 ReMe 的 job，装配是 harness_kit 的 Profile 机制。

---

## 目录

```text
harness_kit/demo/code_assistant/
├── __init__.py          惰性导出（不 import 就不拉 agentscope）
├── agent.py             Profile → HarnessBuilder → 装好的 CodeAssistant
├── ingest_repo.py       渲染 + 入库（写入侧），也是 CLI
├── main.py              端到端四步：索引 → 装配 → 提问 → 核对引用
├── README.md            本文件
├── Dockerfile           容器化（构建上下文 = 仓库根）
└── deployment.yaml      部署清单（Deployment + Service + ConfigMap）
```

---

## 快速开始

```bash
# 两个 PYTHONPATH 缺一不可：ReMe 的 0.4.1.13 克隆必须在 site-packages 之前，
# 否则 import reme 会拿到装好的旧版（实测 0.3.1.10）
export PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:\
/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
export HARNESS_REPO_ROOT=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference

cd $HARNESS_REPO_ROOT

# 端到端四步（会真的调用模型）
python -m harness_kit.demo.code_assistant.main --limit 6

# 复用已有索引再问一个别的问题（换的问题必须落在已索引的语料里，见下）
python -m harness_kit.demo.code_assistant.main --skip-ingest \
  --question "Agent.observe 方法是做什么的？它和 reply 有什么不同？给出出处"

# 只做写入侧（幂等：同一条命令重跑应该全部「未变」）
python -m harness_kit.demo.code_assistant.ingest_repo --limit 6 --json
```

模型凭据从仓库根的 `.env` 读（`OPENAI_API_KEY` / `OPENAI_BASE_URL` /
`LLM_MODEL`，由 `harness_kit/settings.py:168` 的 `load_dotenv` 显式加载，
**与当前工作目录无关**）。

> **第二条命令的问题为什么必须"换在语料里"**：默认索引语料是
> `DEFAULT_TARGET = "third_party/agentscope/src/agentscope/agent"`（12 个文件，
> `agent.py:100`），只有这个目录下的文件进了 ReMe 索引。而第 4 步「引用核对」
> 比对的是**本次检索命中**与回答里写出的路径。
>
> 所以问一个答案不在语料里的问题 —— 比如 `Toolkit.add_tool`，它在
> `agentscope/tool/` 下 —— 模型只能用自己的 `Grep` / `Read` 去磁盘上找，
> 并引用**真实源码路径**（`third_party/.../_toolkit.py:660`）。回答是对的、
> 行号也是真的，但检索命中里没有这个文件，第 4 步会报
> 「未通过：有引用编号 [1, 2, 3, 4, 5]，但回答里没写出任何检索命中的来源路径」。
>
> 反过来也要说清：**「未通过」不等于"回答错了"**，它只说明"回答没有引用
> 本次检索命中的工作区路径"。这一层的语义边界见下文「第 4 步」的"已知上限"
> 与「出问题时按这个顺序看」的表。
>
> 想问你自己的代码，就换 `--target` 重新索引，并且**让问题与语料同源**；
> 注意 `--limit` 是按路径字母序截断的 —— 实测
> `--target third_party/agentscope/src/agentscope/tool --limit 6` 取到的是
> `__init__ / _adapters / _base / _builtin/*` 这 6 个文件，**`_toolkit.py` 并不在内**，
> 所以换语料时 `--limit` 要给够。（顺带：`tool/__init__.py` 会渲染成与
> `agent/__init__.py` 同名的 `resource/agentscope/__init__.py.md` 并覆盖它 ——
> 这正是上文"已知上限"第 1 条说的同名冒充。）

### 真实输出（2026-09-22 实跑，`--limit 6`，长行有截断）

```text
[1/4] 索引代码库
  新增/更新 6 个、未变 0 个、失败 0 个，共 34 个 chunk
  工作区：.../reference/.harness/reme/code_assistant

[2/4] 装配 Agent（Profile → HarnessBuilder）
  Profile      : researcher_with_memory
  Agent        : code-assistant  model=deepseek-flash
  中间件链     : ['LoggingMiddleware', 'GuardsMiddleware', 'LongTermMemoryMiddleware']
  权限模式     : explore（只读；写操作会被拒绝）
  记忆工作区   : .../reference/.harness/reme/code_assistant
  工具搜索根   : /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning（进程 cwd）
  Toolkit 工具 : ['Glob', 'Grep', 'Read']
  中间件工具   : ['memory_search']（每次 reply 现挂）

[3/4] 提问
  -- 直接检索（不经过模型）--
     [1] resource/agentscope/_agent.py.md
     ...
  -- 完整回答 --
  A: `yield_final_msg` 控制 `reply_stream` 是否把内部的最终回复消息（`Msg`）
     也作为流的一项 yield 出来……默认值 `False` [1]。
     来源 1. .../agent/_agent.py:297 2. .../_agent.py:310-313 3. .../_agent.py:324-330
  本次回答模型调用的工具: ['Grep', 'Grep', 'Glob', 'Glob', 'Grep', 'Read']（每个 run 不同）

[4/4] 引用核对（回答里的 [n] × 检索命中）
  引用编号     : [1, 2, 3, 4]
  检索命中     : 5 条（ReMe 工作区路径）
  对上的来源   : [...]
  结论         : 通过：1 条来源与检索命中一致
```

---

## 四步分别在证明什么

### 第 1 步：索引 —— "代码怎么变成可检索的语料"

ReMe 的分块器按后缀挑：`.md` 走 markdown 分块器（按标题切），其余走通用
文本切分。**代码文件没有标题**，直接丢进去只会被按长度硬切，检索命中时模型
看到的是一段没有出处的裸代码。

所以写入前先渲染（`ingest_repo.render_source_document`）：

```markdown
# third_party/agentscope/src/agentscope/agent/_agent.py
> 来源：`...`，共 1235 行。

## third_party/.../agent/_agent.py 行 1-120

（围栏代码块：源码第 1-120 行）

## third_party/.../agent/_agent.py 行 121-240

（依此类推，每 120 行一节 —— 见 `ingest_repo.SECTION_LINES`）
```

于是**引用行里的行号与源文件行号是渲染时就固定下来的**，不靠模型猜。

渲染产物先写进 `<workspace>/resource/<alias>/<相对路径>.md` 再入库，绕开
`MemoryIngestor` 对工作区外文件的"复制 + 同名去重"逻辑
（`harness_kit/memory/ingest.py:604` 的 `_stage_source`）：否则
`agent/_agent.py` 与 `plan/_agent.py` 会撞名成 `_agent-1.md`，引用里看不出是哪棵树。

入库按内容 sha256 幂等，**重跑同一条命令必须全部「未变」**；这是排查
"索引到底更新了没有"最快的一刀。

那层目录名（`alias`）由 `agent.DEFAULT_ALIAS` 一处定义，`main.py` 和
`ingest_repo` CLI 的 `--alias` 默认值都取自它。**这不是洁癖**：index 是幂等的，
但**工作区不是**——`resource/` 下按 alias 分目录，换了 alias 再灌，旧命名空间
不会被清掉，检索时两批一起返回。实测踩过：`resource/agent/` 与
`resource/agentscope/` 并存，同一个文件出现两份、互相冒充（"索引是幂等的"
这句话只在 alias 不变时成立）。

同理，front matter 的标签也由 `agent.DEFAULT_TAGS` 一处定义，CLI 的 `--tags`
默认值取自它。**标签是渲染进正文的**，改标签就是改内容 sha256，入库判定会从
「未变」翻成「新增」。实测踩过：`main.py` 带 `["code","agentscope"]`、CLI 不带，
两者交替跑，同一批文件永远显示"新增 6"，看着像幂等失效，其实只是内容变了。
修法就是共用同一个常量 —— 现在 `main.py` 灌完再用 CLI 灌，报的是
`未变 6`。

### 第 2 步：装配 —— 治理看得见的地方

一行 `build_code_assistant()`，打印出来的是 Profile 的裁决结果：中间件链、
权限模式、工具集。Demo 在 `researcher_with_memory.yaml` 之上**只改四处**
（见下），其余（模型、温度、`max_iters`、权限模式、规则文件）原样继承 ——
这正是 Profile 该有的用法：不是"另一套配置"，而是"同一套治理下的一次场景化覆盖"。

### 第 3 步：提问 —— 把"检索"和"回答"分开

先 `recall`（直接调检索，不经过模型），再 `ask`（完整回答）。
**这一步是排查"模型没给出处"的关键分岔**：

- `recall` 命中 0 条 → 问题在写入侧（文件没入库 / 工作区不一致），改 prompt 是白费力气；
- `recall` 有命中但回答不引用 → 问题在模型或 sys_prompt 侧。

顺带打印本次回答里模型实际调用的工具（`CodeAssistant.tool_calls`）。这不是装饰：
"回答有据"有两条来源 —— ReMe 中间件把检索结果注入 context，和模型自己用
`Read` / `Grep` 读原文件。**只看最终回答分不出是哪条**，把工具调用列出来才看得清。

### 第 4 步：核对 —— "有据可查"，不是"每个字都对"

`verify_citations` 做的是**文件级交叉核对**：回答里点名的文件，是否是本次
检索命中的某个来源对应的文件。比对用 `source_candidates`，容忍三种写法：

| 命中来源（ReMe 工作区路径） | 回答里可能写成 |
| --- | --- |
| `resource/agentscope/_agent.py.md` | 完整路径 / `_agent.py.md` / `_agent.py` |

**已知上限（不许当成"内容正确"的证明）**：

1. **同名文件会互相冒充** —— 命中 `plan/_agent.py` 而回答引用 `agent/_agent.py`
   时，这一层判不出来；
2. **不校验编号与来源的对应关系** —— 模型写 `[1]` 时未必对应 hits[0]，
   强行对齐会造出假的"引用错位"结论；
3. 它证明的是"回答提到了检索到的文件"，**不证明**引用的行号、结论的准确性。

要更强的保证，走第 20 讲的评测层（`harness_kit/eval/*`，见下）。

---

## 两个必须改的 Profile 字段（以及为什么）

Demo 复用 `researcher_with_memory.yaml`，但两处必须按场景改写
（`agent.py:demo_profile`），否则会踩坑：

1. **`memory.workspace_root` 与 `middleware[reme_memory].params.workspace_dir`
   必须同时指向 Demo 自己的索引目录，且两者必须一致。**
   默认值是研究助手的语料（`./.harness/reme/research`）；Demo 也往那儿写，
   两个场景的记忆就会混在一起 —— "检索出别人的资料"是"看起来能用、结果不可信"
   的典型。而这两个键分别被**写入侧**（`MemoryIngestor` 读 `memory.workspace_root`）
   和**检索侧**（`LongTermMemoryMiddleware` 读 `workspace_dir`）使用，
   不一致时检索会**静默返回 0 条**，没有任何报错。

2. **工具白名单收窄成只读的 `Read` / `Grep` / `Glob`。**
   Profile 的 `tools.packs: [builtin]` 会把 `Bash` / `Edit` / `Write` 一起带上。
   Demo 是"读代码回答问题"，带写工具只会让模型在无人值守时尝试改文件 ——
   `explore` 权限会拒绝它，但那一轮就白跑了。**收窄是行为约束，权限是兜底**，
   两层都要有。摘除走的是 AgentScope 自己的 async `Toolkit.remove_tool`
   （`third_party/agentscope/src/agentscope/tool/_toolkit.py:682`），不重写工具管理。

> `memory_search` 摘不掉也不用摘：它是**记忆中间件**在每次 `reply` 时通过
> `list_tools` hook 现挂的（`.../middleware/_longterm_memory/_reme/_middleware.py:443`），
> 不在 Toolkit 里，而且它本身是只读的。

---

## 一个必须知道的坑：`Grep` / `Glob` / `Read` 的根是**进程 cwd**

AgentScope 的这三个工具只收一个 `backend`，**没有 `cwd` 参数**；相对路径由
`await self._backend.getcwd()` 补全
（`third_party/agentscope/src/agentscope/tool/_builtin/_grep.py:222`），
而 `LocalBackend.getcwd()` 返回的就是 `os.getcwd()`
（`third_party/agentscope/src/agentscope/tool/_builtin/_backend.py:902`）。
`harness_kit/tools/builtin_pack.py:375` 只把 `Bash` 钉在 `workdir` 上。

后果（实测）：不处理的话，模型手里的 `Grep` 会在"你启动 Demo 的那个目录"里搜，
然后老实回一句"本仓库中不存在 AgentScope 的源码文件"。

所以 **`main.py` 在跑之前会 `os.chdir(<代码库根>)`**（`--code-root` 可改，
默认仓库根）。**只在入口脚本里 chdir，库函数里不 chdir** ——
`build_code_assistant` 是会被复用的装配函数，偷偷改调用方的进程状态是坏味道。
程序化调用时请自行把 cwd 切到代码库根。

---

## 出问题时按这个顺序看

| 现象 | 先看哪里 | 常见原因 |
| --- | --- | --- |
| `recall` 0 条命中 | 工作区路径是否与 `ingest_repo --index` 一致 | 写入侧与检索侧两个键不一致（见上） |
| 回答没有 `[n]` | sys_prompt 是否还带着"强制引用" | Demo 覆盖 sys_prompt 时把它冲掉了 |
| 回答引用真实源码路径而非 `resource/...md` | 正常 | 模型用 `Read` 读了原文件，内容同源 |
| 回答说"找不到文件" | `--code-root` 指向哪里 | 见上一节的 cwd 坑 |
| 重跑索引全是"新增" | 渲染产物是否变了（行号/截断） | 源文件改了，属正常 |
| 一次检索返回两套 `resource/` 命名空间（如 `resource/agent/...` 与 `resource/agentscope/...` 并存） | 这个工作区被两个不同的 `--alias` 灌过 | 清掉工作区重灌：`rm -rf .harness/reme/code_assistant`。`main.py` 与 `ingest_repo` CLI 现在共用 `agent.DEFAULT_ALIAS`，重灌不会再分裂 |
| 本次回答的「模型调用的工具」每次都不一样 | 正常 | 那是模型的决策，不是固定脚本；同一问题可能走纯 Grep，也可能 Grep + Read |

---

## 评测与部署

**评测**：Demo 负责"单次问答有据"，"多次问答是否稳定"交给第 20 讲的评测层：

```bash
# 3 条用例的 jsonl，每条 {"id","input","expected","tags"}
python -m harness_kit.cli --profile default eval --dataset /tmp/cases.jsonl --report-dir /tmp/eval
# 报告落盘 <report-dir>/eval-<UTC 时间戳>.{json,md}
```

**Docker**：`Dockerfile` 的构建上下文是**仓库根**（要带上
`third_party/agentscope` 与 `third_party/ReMe` 两个源码树）：

```bash
docker build -f tutorial_agsc_reme/reference/harness_kit/demo/code_assistant/Dockerfile \
  -t harness-kit-code-assistant:0.1.0 .

# 跑一次端到端 Demo（密钥从仓库根的 .env 读，不写进镜像）
docker run --rm --env-file .env harness-kit-code-assistant:0.1.0 \
  python -m harness_kit.demo.code_assistant.main --limit 4

# 跑常驻服务
docker run --rm -p 18420:18420 --env-file .env \
  -v hk-state:/app/tutorial_agsc_reme/reference/.harness \
  harness-kit-code-assistant:0.1.0 \
  python -m harness_kit.cli --profile researcher_with_memory \
    serve --host 0.0.0.0 --port 18420
```

三个容器相关的注意点：

1. **`LLM_MODEL` 与 `HARNESS_LLM_MODEL_NAME` 都要给**（前者喂 Profile 的
   `${LLM_MODEL:-...}` 插值，`harness_kit/config/loader.py:227` 直接读
   `os.environ`；后者喂 `Settings`）。只给 `HARNESS_` 那个，Profile 会退回
   `deepseek-chat`。
2. **`--host 0.0.0.0` 不能省**：`cli serve` 默认绑 `127.0.0.1`。
3. **`.harness` 是运行状态，不该进镜像**：Dockerfile 里有一行
   `RUN rm -rf .../.harness` 专门清掉 `COPY` 带进来的宿主状态（索引、会话、
   审计日志）。实测不清的话，容器内第一次提问会检索到两批 alias 不一致的
   `resource/`。镜像里只放代码，状态交给卷。

> 顺带一提：`third_party/agentscope/build/lib/...` 会被一起拷进镜像 —— 那是上游
> `.py` 包旁边躺着的构建副本，不影响运行（`PYTHONPATH` 上的是 `src/`），但模型
> 用 `Grep` 搜源码时会同时搜到两份，回答里会多一句"另有一份构建产物副本"。
> 要么在 Dockerfile 里 `rm -rf` 掉，要么接受这句废话；本 Demo 选择记下来，
> 而不是按自己的口味改上游树的形状。
>
> 顺带一提：本仓库根的 `.dockerignore` 目前不存在，构建上下文是 180MB
> （两个源码树）。加一行 `tutorial_agsc_reme/reference/.harness` 之类的排除
> 能显著提速 —— 但根目录的文件不归本模块管，这里只记录现象。

**部署**：`deployment.yaml` 是纯 K8s 清单（Deployment + Service + ConfigMap +
PVC），密钥走 `Secret`，不写进镜像。它引用镜像 `harness-kit-code-assistant:0.1.0`，
与上面的 `docker build -t` 一致。

---

## 验证状态（诚实记录）

全部在 2026-09-22 本机实跑（Python 3.11 / `agentscope_reme_pip_env`）。

| 项 | 状态 | 证据 |
| --- | --- | --- |
| `main.py` 端到端四步 | **已实跑通过** | `--limit 6`：34 chunk、5 条命中、引用核对"通过：1 条来源与检索命中一致" |
| `ingest_repo` 幂等 | **已实跑通过** | 同一条命令重跑 `未变 6`；**跨入口**（`main.py` 先灌、再用 CLI 灌）也是 `未变 6`（两条入口共用 alias / tags） |
| `build_code_assistant` + 只读收窄 | **已实跑通过** | Toolkit 恰为 `['Glob','Grep','Read']`；中间件挂出 `['memory_search']` |
| 渲染 → 入库 → 引用可回溯 | **已实跑通过** | 回答里的 `.../_agent.py:297` 与命中的 `resource/agentscope/_agent.py.md` 同源 |
| 「快速开始」里三条命令 | **已实跑通过** | 默认问题（`yield_final_msg`）与第二条的 `Agent.observe` 问题都跑到第 4 步「引用核对」、结论均为「**通过**」；`ingest_repo --limit 6 --json` 幂等（`added 0 / unchanged 6`）。**"对上的来源"条数每次 run 不同**（实测 1~2 条）：它取决于模型这一次是否把工作区路径写进"来源"列表，不是固定值 |
| Dockerfile | **已实跑通过** | `docker build` 退出码 0（镜像 598MB，`c89088c30e00`）；容器内 `python -c` 确认 reme 0.4.1.13 / agentscope 2.0.8 / 5 个 Profile 可发现；容器内跑 `main.py --limit 4`：4 条命中全在 `resource/agentscope/` 下、引用核对"通过：1 条来源与检索命中一致" |
| deployment.yaml | **仅静态校验** | 6 个文档能被 YAML 解析；**没有可用 K8s 集群，未 `kubectl apply`**，所以探针/卷/权限的运行时行为未经证实 |

同批验证过的相邻组件（脚本在 `/tmp`，见交付说明）：
服务层 `service/app.py` 的 SSE 对话 + 优雅关闭（端口 18426，跑完已释放）、
`cli serve`（18427，SIGTERM 后优雅关闭）、端口铁律拦截（`--port 8080` → 拒绝）、
3 用例评测 + 报告落盘（`pass_rate=1.0`）、`researcher_with_memory.yaml` 直接装配出
的 Agent 一次真实回复。
