# syntax=docker/dockerfile:1
#
# 代码助手 Demo 的镜像（第 20 讲）。
#
# **构建上下文必须是仓库根**，不是本目录：
#
#     docker build -f tutorial_agsc_reme/reference/harness_kit/demo/code_assistant/Dockerfile \
#         -t harness-kit-code-assistant:0.1.0 .
#
# 原因：镜像里要放进两个**上游源码树**（agentscope / ReMe），而 Demo 的
# 定位逻辑是靠"从 reference 目录向上找 third_party"（见 agent.py 的
# reference_root / repo_root）。把两个源码树按原样目录结构放进去，这些逻辑
# 一行都不用改；否则就得在镜像里另配一堆环境变量，等于把路径假设复制一份。
#
# 镜像里**不放** .env：密钥只在运行时用 --env / --env-file 注入。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata + ca-certificates：agentscope 的依赖里已有 tzdata（slim 镜像上没有
# IANA 时区库），ca-certificates 是 HTTPS 调模型 API 用的。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) 两个上游源码树（版本由源码树本身决定：agentscope 2.0.8 / reme 0.4.1.13）
COPY third_party/agentscope /app/third_party/agentscope
COPY third_party/ReMe /app/third_party/ReMe

# 2) harness_kit 本体
COPY tutorial_agsc_reme/reference /app/tutorial_agsc_reme/reference

# 3) 装依赖。不用 editable：镜像里源码树是静态的，装成普通包更省事。
#    python-dotenv 是 harness_kit.settings 直接 import 的，必须显式装
#    （agentscope / reme 都不会把它带进来）。
#    --retries / --timeout：这两个包的依赖树不小（实测 ~200MB wheel），
#    网络抖动时默认超时会直接让构建失败（本项目就遇到过一次 Read timed out）。
RUN pip install --retries 10 --timeout 120 \
    /app/third_party/agentscope /app/third_party/ReMe python-dotenv

# 4) 把宿主机上的运行时状态从镜像里删掉。**这一步不能省**：`.harness/` 里是
#    ReMe 索引、会话事件、权限审计日志，上一步的 COPY 会把开发机上那一份原样
#    带进镜像 —— 后果是容器里的检索会命中"别人（其实是本机）的语料"，
#    而且镜像里躺着 audit.jsonl 这类运行痕迹。实测：不清的话，容器内第一次
#    提问会同时检索到命名空间不一致的两批 resource/（旧 alias + 新 alias），
#    引用核对虽然仍能通过，但那是"数据没隔离"，不是"检索做对了"。
#    镜像里只应该有代码；状态由卷（VOLUME / --mount）在运行时提供。
#    放在 pip install **之后**是刻意的：这个 RUN 一改就会让它之后的层全部重建，
#    排在 pip 后面，调这一行就不用重下 200MB 依赖。
RUN rm -rf /app/tutorial_agsc_reme/reference/.harness

# ReMe 放在 PYTHONPATH 最前面：与宿主机上的运行方式保持一致
# （宿主机上 site-packages 里有一个旧的 reme，不靠 PYTHONPATH 会被抢先 import）。
ENV PYTHONPATH=/app/third_party/ReMe:/app/tutorial_agsc_reme/reference \
    HARNESS_REPO_ROOT=/app/tutorial_agsc_reme/reference

# 所有可变状态都落在 reference/.harness 下 —— 这是 harness_kit 的既有约定：
# Settings 的 workspace_dir / session_dir 默认值都是相对 repo_root 的 ./.harness/*，
# Demo 的默认索引目录也是 ./.harness/reme/code_assistant。挂一个卷到这一层，
# 索引、会话、工作区就一起持久化了，不需要再造第二套路径约定。
VOLUME ["/app/tutorial_agsc_reme/reference/.harness"]

# 只有跑 `harness-kit serve` 时才会用到这个端口（默认端口 18420，铁律 ≥ 18000）。
EXPOSE 18420

# 默认跑一次端到端 Demo（索引 → 提问 → 核对引用）。
# --limit 12 是刻意的小值：Demo 要的是"可读的一段代码库"，不是全量索引。
CMD ["python", "-m", "harness_kit.demo.code_assistant.main", "--limit", "12"]

# 想跑常驻服务（SSE 对话 + /sessions + /metrics）时覆盖 CMD：
#
#     docker run --rm -p 18420:18420 \
#         -e OPENAI_API_KEY=... -e OPENAI_BASE_URL=... -e LLM_MODEL=deepseek-flash \
#         -v hk-state:/app/tutorial_agsc_reme/reference/.harness \
#         harness-kit-code-assistant:0.1.0 \
#         python -m harness_kit.cli --profile researcher_with_memory \
#             serve --host 0.0.0.0 --port 18420
#
# 注意 --host 0.0.0.0：CLI 的默认值是 127.0.0.1，容器里那样绑是收不到外部请求的。
# 这里刻意不写 HEALTHCHECK：默认 CMD 是一次性任务，没有可探活的常驻进程；
# 跑 serve 时用 deployment.yaml 里的 httpGet /healthz 探针。
