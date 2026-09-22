# -*- coding: utf-8 -*-
"""第 2 讲的 pytest（交付物之一）：把契约 §6.2 的合并规则钉成可回归的断言。

为什么单测要单独写一遍：`merge_dicts` 是**纯函数**，而 Profile 合并是本讲唯一
"错了不会立刻报错、只会在两周后以诡异行为出现"的地方 —— 例如把 `!append`
退化成整体替换，`coding.yaml` 的 `tools.packs` 就会从 `[builtin, repo]`
悄悄变成 `[repo]`，工具少了一个，模型会反复尝试再放弃。

用法（`tests/conftest.py` 已经把 `third_party/ReMe` 与 `reference/` 塞进
`sys.path`，所以不设 `PYTHONPATH` 也能跑；这里显式写出来是为了与另外两个脚本一致）::

    cd /Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/tutorial_agsc_reme/reference
    PYTHONPATH=/Users/a/PycharmProjects/VsCode_Projects/Agentscope-Learning/third_party/ReMe:. \\
      /Users/a/miniconda3/envs/agentscope_reme_pip_env/bin/python -m pytest tests/test_lesson02_config.py -v

LLM 调用预算：**0 次**。
"""

from __future__ import annotations

from pathlib import Path

import harness_kit
import pytest
from harness_kit.config import (
    AppendList,
    ConfigCycleError,
    ConfigInterpolationError,
    ConfigNotFoundError,
    Profile,
    ResolvedProfile,
    merge_dicts,
    resolve_profile,
)
from harness_kit.config.loader import interpolate_env, load_profile

REF = Path(harness_kit.__file__).resolve().parent.parent
PROFILES = REF / "harness_kit" / "profiles"


# ----------------------------------------------------------------------
# 合并规则
# ----------------------------------------------------------------------
def test_merge_maps_recursively() -> None:
    assert merge_dicts({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {
        "a": {"b": 9, "c": 2},
    }


def test_merge_leaf_override_wins() -> None:
    assert merge_dicts({"x": 1}, {"x": 2}) == {"x": 2}


def test_merge_list_replaces_by_default() -> None:
    assert merge_dicts({"p": ["a", "b"]}, {"p": ["c"]}) == {"p": ["c"]}


def test_merge_append_list_appends() -> None:
    assert merge_dicts({"p": ["a", "b"]}, {"p": AppendList(["c"])}) == {
        "p": ["a", "b", "c"],
    }


def test_merge_null_deletes_key() -> None:
    assert merge_dicts({"m": {"enabled": True}, "k": 1}, {"m": None}) == {"k": 1}


def test_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"b": 1}, "p": ["x"]}
    override = {"a": {"b": 2}, "p": AppendList(["y"])}
    merge_dicts(base, override)
    assert base == {"a": {"b": 1}, "p": ["x"]}
    assert override == {"a": {"b": 2}, "p": ["y"]}


# ----------------------------------------------------------------------
# 插值
# ----------------------------------------------------------------------
def test_interpolate_uses_env() -> None:
    assert interpolate_env("${A}", {"A": "1"}) == 1


def test_interpolate_default_value() -> None:
    assert interpolate_env("${MISSING:-fallback}", {}) == "fallback"


def test_interpolate_undefined_raises() -> None:
    with pytest.raises(ConfigInterpolationError):
        interpolate_env("${MISSING}", {})


def test_interpolate_recurses_into_containers() -> None:
    assert interpolate_env({"a": ["${A}"]}, {"A": "7"}) == {"a": [7]}


# ----------------------------------------------------------------------
# 真实 Profile
# ----------------------------------------------------------------------
def test_default_profile_resolves() -> None:
    resolved = resolve_profile(load_profile("default", search_dir=PROFILES), search_dir=PROFILES)
    assert isinstance(resolved, ResolvedProfile)
    assert resolved.model.provider == "deepseek"
    assert resolved.tools.packs == ["builtin"]
    assert resolved.agent.max_iters == 20
    assert resolved.source_chain == ["profile:default"]
    assert resolved.source_map["agent.max_iters"] == "profile:default"


def test_coding_profile_appends_repo_pack() -> None:
    resolved = resolve_profile(load_profile("coding", search_dir=PROFILES), search_dir=PROFILES)
    assert resolved.source_chain == ["profile:default", "profile:coding"]
    assert resolved.tools.packs == ["builtin", "repo"]
    # middleware 是整体替换：coding 的 [budget, guards] 顶掉了 default 的 [logging]
    assert [m.name for m in resolved.middleware] == ["budget", "guards"]
    assert resolved.memory.enabled is True
    assert resolved.memory.min_score == 0.2
    assert resolved.agent.max_iters == 30


def test_explain_mentions_leaf_source() -> None:
    resolved = resolve_profile(load_profile("coding", search_dir=PROFILES), search_dir=PROFILES)
    text = resolved.explain()
    assert "[agent]" in text
    assert "profile:coding" in text


def test_cycle_detected(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: a\nextends: b\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("name: b\nextends: a\n", encoding="utf-8")
    with pytest.raises(ConfigCycleError):
        resolve_profile(load_profile("a", search_dir=tmp_path), search_dir=tmp_path)


def test_missing_bundle_raises_config_not_found(tmp_path: Path) -> None:
    (tmp_path / "p.yaml").write_text("name: p\nbundles: [nope]\n", encoding="utf-8")
    with pytest.raises(ConfigNotFoundError):
        resolve_profile(load_profile("p", search_dir=tmp_path), search_dir=tmp_path)


def test_unknown_field_forbidden() -> None:
    with pytest.raises(Exception):
        Profile.model_validate({"name": "x", "nope": 1})
