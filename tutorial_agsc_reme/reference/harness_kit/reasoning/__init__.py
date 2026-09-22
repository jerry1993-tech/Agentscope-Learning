# -*- coding: utf-8 -*-
"""推理层：结构化输出、自我批判、prompt 装配（契约 §3.14，第 14 讲）。

三个模块彼此独立，可以单独用；组合起来的典型姿势是：

```python
assembler = PromptAssembler(sections=[role, constraints, tools])

agent = Agent(
    name="writer",
    system_prompt=assembler.render(),   # 稳定前缀，指纹可断言
    model=model,
)

# 每次 reply 的动态内容走 hint，不进 system prompt
agent.observe(assembler.hint(f"当前剩余预算：{budget}"))

loop = CritiqueLoop(agent=agent, max_rounds=3, acceptance_score=0.8)
result = await loop.run("写一份季度复盘")

# 需要机器可读的结果时，用结构化输出而不是解析文本
plan = await CallStructured(model=model, schema=PlanDraft).run(messages)
```

============================================== ==========================================================
:mod:`~harness_kit.reasoning.structured`        ``CallStructured`` / ``StructuredOutputTool``。走
                                                ``ChatModelBase`` 的 function-calling 与 ``ToolChoice``。
:mod:`~harness_kit.reasoning.critique`          ``CritiqueLoop``：generate → critique → revise，
                                                判据本身是结构化输出。
:mod:`~harness_kit.reasoning.prompt`            ``PromptAssembler``：KV Cache 友好的 system prompt
                                                装配 + ``HintBlock`` 动态注入。**不 import agentscope
                                                的 agent / model**，只依赖 ``HintBlock``。
============================================== ==========================================================

**依赖方向**：``critique`` → ``structured``，``prompt`` 独立。没有反向依赖，
所以单测 ``PromptAssembler`` 不需要起模型。
"""

from harness_kit.reasoning.critique import (
    CRITIQUE_PROMPT,
    REVISE_PROMPT,
    CritiqueLoop,
    CritiqueResult,
    CritiqueVerdict,
)
from harness_kit.reasoning.prompt import (
    PROMPT_VERSION,
    SECTION_ORDER,
    PromptAssembler,
    PromptSection,
    VolatileSectionError,
    render_tool_list,
)
from harness_kit.reasoning.structured import (
    STRUCTURED_TOOL_INSTRUCTION,
    CallStructured,
    StructuredOutputError,
    StructuredOutputTool,
)

__all__ = [
    "CRITIQUE_PROMPT",
    "PROMPT_VERSION",
    "REVISE_PROMPT",
    "SECTION_ORDER",
    "STRUCTURED_TOOL_INSTRUCTION",
    "CallStructured",
    "CritiqueLoop",
    "CritiqueResult",
    "CritiqueVerdict",
    "PromptAssembler",
    "PromptSection",
    "StructuredOutputError",
    "StructuredOutputTool",
    "VolatileSectionError",
    "render_tool_list",
]
