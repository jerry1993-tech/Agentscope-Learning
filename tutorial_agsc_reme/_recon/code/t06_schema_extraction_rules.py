# -*- coding: utf-8 -*-
"""06 侦察验证脚本 I：从函数签名 + docstring 生成 JSON Schema 的真实规则
运行：
  /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python \
      /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/_recon/code/t06_schema_extraction_rules.py
"""
import json
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field

from agentscope.tool._utils import (
    _extract_func_description,
    _extract_input_schema,
)


def show(title: str, fn, **kw) -> None:
    print("=" * 70)
    print(title)
    print(json.dumps(_extract_input_schema(fn, **kw),
                     ensure_ascii=False, indent=2))


# 1) Google-style docstring + 各种注解
def demo_annotations(
    name: str,
    age: int = 18,
    score: float = 0.5,
    flag: bool = True,
    tags: list[str] | None = None,
    mode: Literal["a", "b"] = "a",
    bounded: Annotated[int, Field(ge=0, le=10)] = 5,
    anything=None,
    nested: Optional[dict[str, int]] = None,
) -> str:
    """Do a demonstration of annotation mapping.

    A longer explanation that becomes the second paragraph of the
    description.

    Args:
        name (str): The person name, a required string.
        age (int): The age, with a default so it is not required.
        score (float): A float score.
        flag (bool): A boolean switch.
        tags (list[str] | None): Optional tags.
        mode (Literal["a", "b"]): Becomes a JSON-Schema enum.
        bounded (int): Constraints come from Annotated[Field].
        anything: No annotation at all -> Any.
        nested (dict | None): Nested values.
    """
    return "ok"


show("[1] annotation -> JSON-Schema mapping", demo_annotations)
print("description =", repr(_extract_func_description(demo_annotations.__doc__)))


# 2) numpy / sphinx 风格的 docstring
def numpy_style(a: int, b: int) -> int:
    """Add two numbers.

    Parameters
    ----------
    a : int
        The first operand.
    b : int
        The second operand.

    Returns
    -------
    int
        The sum.
    """
    return a + b


show("[2] numpy-style docstring is also parsed by docstring_parser",
     numpy_style)


def sphinx_style(a: int) -> int:
    """Add one.

    :param a: The operand.
    :type a: int
    :returns: a + 1
    """
    return a + 1


show("[3] sphinx-style docstring", sphinx_style)


# 3) **kwargs / *args
def var_args(a: int, *args: int, **kwargs: str) -> str:
    """Use variable arguments.

    Args:
        a (int): The fixed one.
        *args (int): Extra positional values.
        **kwargs (str): Extra keyword values.
    """
    return "ok"


show("[4] *args/**kwargs are SKIPPED by default", var_args)
show("[5] ... unless include_var_* is True", var_args,
     include_var_positional=True, include_var_keyword=True)


# 4) 无注解 + 无 docstring
def no_annotation_no_doc(a, b):
    return a


show("[6] no annotation, no docstring -> permissive schema",
     no_annotation_no_doc)
print("description =", repr(_extract_func_description("")))


# 5) Enum 类型
class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


def pick(color: Color = Color.RED) -> str:
    """Pick a color.

    Args:
        color (Color): The chosen color.
    """
    return color.value


show("[7] Enum subclass -> $defs + $ref", pick)


# 6) Pydantic 模型参数
class Inner(BaseModel):
    """The inner model."""

    value: int = Field(description="An int.")


def use_model(inner: Inner) -> str:
    """Use a nested model.

    Args:
        inner (Inner): The nested payload.
    """
    return "ok"


show("[8] a Pydantic model parameter -> $defs + $ref", use_model)


# 7) 直接用 Pydantic 模型的 schema
print("=" * 70)
print("[9] FunctionTool(input_schema=<BaseModel>) path calls "
      "_remove_title_field(model_json_schema())")
from agentscope.tool import FunctionTool  # noqa: E402

raw = Inner.model_json_schema()
print("   raw model_json_schema has title:",
      "title" in json.dumps(raw))
print("   FunctionTool(input_schema=Inner).input_schema has title:",
      "title" in json.dumps(FunctionTool(use_model,
                                         input_schema=Inner).input_schema))
