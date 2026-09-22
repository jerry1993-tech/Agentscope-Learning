---
name: commit_convention
description: 按 Conventional Commits 规范生成或校验 git 提交信息，含 type/scope 选择、破坏性变更标注、footer 写法。当用户说“帮我写 commit”“提交信息怎么写”“检查一下这个 commit message”时使用。
version: 1.0.0
tags: [git, commit, convention]
license: internal
scripts:
  - scripts/check_commit_msg.py
---

# 提交信息规范技能

## 何时使用

用户要求写 commit message、审阅 commit message，或需要把一批改动归纳成提交时使用。

## 提交信息格式

```text
<type>(<scope>)!: <subject>

<body>

<footer>
```

- **header**（必填）：`type(scope): subject`，全行不超过 72 字符。
- **body**（可选）：空一行后写"为什么改"，不写"改了什么"（diff 已经说了）。
- **footer**（可选）：`BREAKING CHANGE:` 或 `Closes #123` / `Refs #456`。

### type 取值（只能用这些）

| type | 用在 |
| --- | --- |
| `feat` | 新增用户可见的能力 |
| `fix` | 修复缺陷 |
| `refactor` | 不改行为的重构 |
| `perf` | 性能优化 |
| `docs` | 只改文档 |
| `test` | 只改测试 |
| `build` | 构建脚本 / 依赖 |
| `ci` | CI 配置 |
| `chore` | 杂项，不触达 src 与 test |
| `revert` | 回滚某次提交，body 里写 `This reverts commit <sha>.` |

### scope 取值

用**受影响的模块名**，小写、无空格：`agent`、`memory`、`mcp`、`cli`、`deps`。
改动跨多个模块时省略 scope，**不要**写 `(multiple)` 或 `(*)`。

### subject 写法

- 用祈使句、现在时：`add retry to mcp client`，不是 `added` / `adds`。
- 首字母小写，结尾不加句号。
- 一句话说清"这个提交让系统多了什么能力"。

### 破坏性变更

两种等价的标注方式，任选其一但**必须标注**：

1. header 里加 `!`：`feat(api)!: drop v1 endpoint`
2. footer 里写：`BREAKING CHANGE: /v1 已下线，调用方改用 /v2`

## 工作流程

### 场景 A：用户要你写提交信息

1. 跑 `git diff --stat` 和 `git diff`（已 `git add` 的跑 `git diff --cached`）看真实改动。
2. 判断 type：有新增能力 → `feat`；只修行为 → `fix`；都不沾 → 按上表挑。
3. 判断 scope：改动集中在单个模块才写 scope。
4. 写 header，数一下字符数（≤ 72）。
5. 如果 diff 里删除了公开接口/字段/CLI 参数 → 必须加破坏性变更标注。
6. 最后用脚本自检：

```bash
python <本技能目录>/scripts/check_commit_msg.py --string "feat(mcp): add stdio transport"
```

退出码 0 才把信息交给用户。非 0 时按脚本输出的 `reason` 逐条修，修完再跑一遍。

### 场景 B：用户要你审阅已有提交信息

1. 拿到信息：`git log -1 --format=%B`。
2. 写进临时文件再校验（避免 shell 转义问题）：

```bash
git log -1 --format=%B > /tmp/msg.txt && python <本技能目录>/scripts/check_commit_msg.py /tmp/msg.txt
```

3. 把脚本输出的每条问题翻译成一句"怎么改"，并给出修改后的完整版本。

## 硬性规则

1. **不许编造 diff**。写 message 前必须真的看过 diff；看不到 diff 就直接问用户。
2. 一次提交只做一件事。发现 diff 里混了两类改动（比如同时 `feat` 和 `refactor`），
   明确指出并建议拆成两个 commit。
3. 不要写"update code""fix bug""小改动"这类无信息量的 subject。
4. 不要主动 `git commit` —— 除非用户明确要求，只输出信息文本。

## 配套脚本

- `scripts/check_commit_msg.py` —— Conventional Commits 校验器（纯标准库）。
  支持 `--string`、从文件读、从 stdin 读；`--json` 输出机器可读结果；
  `--max-header-length N` 改表头长度上限（默认 72）。
  退出码：0 = 合规；1 = 不合规；2 = 用法错误。
