# -*- coding: utf-8 -*-
"""harness_kit 的技能层（契约 §3.6，第 6 讲）。

建立在 AgentScope 的 skill 体系之上：

- ``agentscope.skill.SkillLoaderBase``（``third_party/agentscope/src/agentscope/skill/_base.py:23``）
  —— 抽象基类，只需要 ``async def list_skills()``；
- ``agentscope.skill.LocalSkillLoader``（``.../skill/_local_loader.py:16``）
  —— 目录扫描 + front matter 解析 + mtime 缓存的真实实现；
- ``agentscope.tool.Toolkit``（``.../tool/_toolkit.py:66``）
  —— 通过 ``skills_or_loaders=[...]`` 消费 loader，并把技能索引渲染进系统提示词。

harness_kit 只补两个缺口：

1. :mod:`harness_kit.skills.manifest` —— 把被 ``LocalSkillLoader`` 丢弃的
   ``version`` / ``tags`` / ``tenants`` / ``requires`` / ``scripts`` 捡回来，
   并且**缺字段时报错而不是静默跳过**；
2. :mod:`harness_kit.skills.loader` —— ``HarnessSkillLoader`` 继承
   ``LocalSkillLoader``，把「启用开关 / 租户白名单 / 依赖拓扑」做成真的会
   影响 ``list_skills()`` 结果的过滤。

内置技能（``builtin/``）两个：

- ``code_review`` —— 结构化代码审查，配套 ``scripts/review_check.py``；
- ``commit_convention`` —— Conventional Commits，配套 ``scripts/check_commit_msg.py``。
"""

from harness_kit.skills.loader import (
    HarnessSkillLoader,
    SkillDependencyError,
    UnknownSkillError,
    build_skill_instruction_template,
    loaders_from_spec,
)
from harness_kit.skills.manifest import (
    FRONTMATTER_FILENAME,
    SkillManifest,
    SkillManifestError,
    load_manifest,
    parse_manifest_text,
    parse_tags,
)

__all__ = [
    "FRONTMATTER_FILENAME",
    "HarnessSkillLoader",
    "SkillDependencyError",
    "SkillManifest",
    "SkillManifestError",
    "UnknownSkillError",
    "build_skill_instruction_template",
    "load_manifest",
    "loaders_from_spec",
    "parse_manifest_text",
    "parse_tags",
]
