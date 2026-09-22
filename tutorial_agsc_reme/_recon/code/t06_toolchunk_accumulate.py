# -*- coding: utf-8 -*-
"""06 侦察验证脚本 D：ToolChunk 流式累积成 ToolResponse 的规则
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_toolchunk_accumulate.py
"""
import base64
import json

from agentscope.message import Base64Source, DataBlock, TextBlock, ToolResultState
from agentscope.tool import ToolChunk, ToolResponse

print("=" * 70)
print("[1] same block id + TextBlock -> text is concatenated in place")
r = ToolResponse(id="t1")
r.append_chunk(ToolChunk(id="c1",
                         content=[TextBlock(id="b1", text="Hello ")],
                         state=ToolResultState.RUNNING))
r.append_chunk(ToolChunk(id="c2",
                         content=[TextBlock(id="b1", text="world")],
                         state=ToolResultState.RUNNING))
print("   content =", [(b.id, b.text) for b in r.content], "state =", r.state)

print("=" * 70)
print("[2] DIFFERENT TextBlock ids -> merged anyway (post-processing)")
r2 = ToolResponse(id="t2")
r2.append_chunk(ToolChunk(content=[TextBlock(id="x", text="a")]))
r2.append_chunk(ToolChunk(content=[TextBlock(id="y", text="b")]))
print("   content =", [(b.id, b.text) for b in r2.content],
      "(one block, id kept from the first)")

print("=" * 70)
print("[3] DataBlock with the same id + Base64Source -> bytes are decoded,")
print("    concatenated and re-encoded (padding safe)")
r3 = ToolResponse(id="t3")
p1 = base64.b64encode(b"\x89PNG-first-half").decode()
p2 = base64.b64encode(b"-second-half").decode()
src = Base64Source(type="base64", media_type="image/png", data=p1)
r3.append_chunk(ToolChunk(content=[DataBlock(source=src, id="d1")]))
r3.append_chunk(ToolChunk(content=[DataBlock(
    source=Base64Source(type="base64", media_type="image/png", data=p2),
    id="d1")]))
merged = base64.b64decode(r3.content[0].source.data)
print("   merged raw bytes =", merged)

print("=" * 70)
print("[4] state is monotone-worse: ERROR > INTERRUPTED/DENIED > SUCCESS")
for seq in [["running", "success"], ["running", "error"],
            ["error", "success"], ["running", "denied"]]:
    r4 = ToolResponse(id="t4")
    for s in seq:
        r4.append_chunk(ToolChunk(content=[TextBlock(id="z", text=".")],
                                  state=ToolResultState(s)))
    # success is not in ToolResponse's Literal, the default is SUCCESS
    print(f"   {seq} -> {r4.state}")

print("=" * 70)
print("[5] metadata is merged dict-wise")
r5 = ToolResponse(id="t5")
r5.append_chunk(ToolChunk(content=[], metadata={"a": 1}))
r5.append_chunk(ToolChunk(content=[], metadata={"b": 2, "a": 3}))
print("   metadata =", r5.metadata)

print("=" * 70)
print("[6] ToolChunk / ToolResponse JSON (they travel in events & storage)")
print(ToolChunk(content=[TextBlock(text="hi")]).model_dump_json())
print(ToolResponse(content=[TextBlock(text="hi")]).model_dump_json())

print("=" * 70)
print("[7] ToolResultState members")
print("   ", [s.value for s in ToolResultState])
