# `harness_kit/permission/rules/` — 规则 YAML 目录

## 为什么这里既有一个 `rules.py` 又有一个 `rules/` 目录

这不是笔误，是契约的两处硬约定叠在了一起：

- 契约 §3.11 把**加载器模块**钉在 `harness_kit/permission/rules.py`；
- 契约 §11 把**配置文件目录**钉在 `harness_kit/permission/rules/`，
  而 §6.3 的 Profile 里写的是 `rule_files: ["./harness_kit/permission/rules/coding.yaml"]`。

Python 的导入系统能正确区分这两者：`import harness_kit.permission.rules`
会先找 `rules/__init__.py`（**不存在** → 不是常规包），再找 `rules.py`（**存在** → 命中）。
因此 `rules.py` 是模块，`rules/` 只是配置数据目录。

> **不要**在这个目录里放 `__init__.py`。一旦放了，`rules/` 就变成常规包，
> 会把 `rules.py` 整个遮蔽掉，`from harness_kit.permission.rules import RuleSet`
> 会直接 ImportError。

## 文件

| 文件 | 用途 | Profile 引用 |
| --- | --- | --- |
| `coding.yaml` | 编码场景：工作区内放行读写，危险命令一律拒绝 | `coding-assistant` |
| `research.yaml` | 研究场景：只读为主，写操作一律要人确认 | `research-analyst` |

## 写法

```yaml
order: 10                 # 越小越先匹配；多个文件时决定覆盖关系
default_behavior: ask     # 无规则命中时改写为什么行为：allow / deny / ask
rules:
  - tool: "Read"          # 工具名，对应 ToolBase.name
    behavior: allow       # allow / deny / ask
    allow_paths: [...]    # 按行为前缀写的模式列表，前缀必须与 behavior 一致
```

- `rule_content` 写单个模式；
- `allow_paths` / `deny_paths` / `ask_paths` 与 `allow_patterns` / `deny_patterns` /
  `ask_patterns` 是**同一件事的两种读法**（`paths` 暗示路径 glob，`patterns` 暗示命令子串），
  都会被摊平成多条原生 `PermissionRule`（因为原生一条规则只能带一个 `rule_content`）。
- 前缀与 `behavior` **必须一致**：`behavior: deny` 配 `allow_paths` 会被直接拒绝加载，
  避免"我以为放行了、其实拒绝了"这种极难排查的沉默失败。

**模式怎么被解释，取决于工具自己**（契约 §5.5 的硬约定，实现见
`third_party/agentscope/src/agentscope/permission/_engine.py:775` 的 `_rule_matches`）：

- `Bash` → 命令的**子串**匹配（`"git push --force"` 命中含该子串的任何命令）；
- `Read` / `Write` / `Edit` → 文件路径的 **fnmatch glob**（注意：匹配的是 `tool_input`
  里的**原始字符串**，`third_party/agentscope/src/agentscope/tool/_builtin/_write.py:194`，
  所以别写绝对路径前缀，除非你确定 agent 传的就是绝对路径）；
- 其它工具 → 工具自定义。

**不写任何模式**（只有 `tool` + `behavior`）表示"这个工具的**所有**调用都按该行为处理"
（引擎侧 `if not rule.rule_content: return True`，
`third_party/agentscope/src/agentscope/permission/_engine.py:798`）。
