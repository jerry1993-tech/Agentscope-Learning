# -*- coding: utf-8 -*-
"""YAML 装载：``${VAR}`` 环境插值 + ``!append`` 标签 + ``extends`` / ``bundles`` 按名查找。

环境插值的实现刻意对齐 ReMe 的 ``expand_env_vars``
（``third_party/ReMe/reme/config/config_parser.py:53``），包括：

- 正则 ``\\$\\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\\}``（``.../config_parser.py:22``）；
- 替换后按 ReMe 的 ``_convert_value``（``.../config_parser.py:98``）做类型还原：
  ``"true"``/``"false"`` → bool、``"null"``/``"none"`` → None、前导零字符串保持字符串、
  其余依次尝试 int / float / JSON。

唯一的差异：ReMe 在变量未定义且没有 ``:-default`` 时抛 ``ValueError``，
这里抛 :class:`ConfigInterpolationError` 以便调用方按类型捕获。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from loguru import logger

from harness_kit.config.schema import (
    AppendList,
    Bundle,
    Profile,
    ResolvedProfile,
    resolve_profile,
)

__all__ = [
    "ConfigCycleError",
    "ConfigError",
    "ConfigInterpolationError",
    "ConfigNotFoundError",
    "ConfigParseError",
    "interpolate_env",
    "load_bundle",
    "load_profile",
    "load_resolved_profile",
    "load_yaml",
]

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
"""与 ``third_party/ReMe/reme/config/config_parser.py:22`` 完全相同的占位符正则。"""

_LEADING_ZERO_RE = re.compile(r"^-?0\d")
"""前导零检测（``.../config_parser.py:23``），避免把 ``"007"`` 变成 ``7``。"""

_SUPPORTED_SUFFIXES: tuple[str, ...] = (".yaml", ".yml", ".json")
"""按名查找时尝试的扩展名，优先级从高到低。"""


# ======================================================================
# 异常
# ======================================================================
class ConfigError(Exception):
    """harness_kit 配置层全部异常的基类。"""


class ConfigNotFoundError(ConfigError):
    """按名查找 Profile / Bundle 时找不到文件（契约 §6.2）。"""


class ConfigCycleError(ConfigError):
    """``extends`` 链成环（契约 §3.2 / §6.2）。"""


class ConfigInterpolationError(ConfigError):
    """``${VAR}`` 无法解析（环境变量未定义且没有 ``:-default``）。"""


class ConfigParseError(ConfigError):
    """YAML 语法错误或顶层不是映射。"""


# ======================================================================
# YAML 读取
# ======================================================================
class _HarnessYamlLoader(yaml.SafeLoader):
    """带 ``!append`` 标签的 SafeLoader。

    独立子类，避免污染全局的 ``yaml.SafeLoader``。
    """


def _construct_append(
    loader: _HarnessYamlLoader,
    node: yaml.Node,
) -> AppendList:
    """把 ``!append [a, b]`` 构造成 :class:`~harness_kit.config.schema.AppendList`。

    Args:
        loader (`_HarnessYamlLoader`): YAML 加载器。
        node (`yaml.Node`): 序列节点。

    Returns:
        `AppendList`: 带追加语义的列表。
    """
    return AppendList(loader.construct_sequence(node, deep=True))


_HarnessYamlLoader.add_constructor("!append", _construct_append)


def _convert_value(value_str: str) -> Any:
    """把插值后的字符串还原成合适的 Python 类型。

    逐字对齐 ReMe 的 ``_convert_value``（``third_party/ReMe/reme/config/config_parser.py:98``）。

    Args:
        value_str (`str`): 待还原的字符串。

    Returns:
        `Any`: ``bool`` / ``None`` / ``int`` / ``float`` / ``list`` / ``dict`` / ``str``。
    """
    text = value_str.strip()
    lowered = text.lower()

    if lowered in ("none", "null"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    if not _LEADING_ZERO_RE.match(text):
        for converter in (int, float):
            try:
                return converter(text)
            except ValueError:
                continue

    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        pass

    return text


def interpolate_env(raw: Any, environ: Mapping[str, str]) -> Any:
    """递归地把 ``${VAR}`` / ``${VAR:-default}`` 替换成环境变量的值。

    只处理**字符串值**（字典的键不动、列表递归、``AppendList`` 保持类型）。

    Args:
        raw (`Any`): 任意 YAML 结构。
        environ (`Mapping[str, str]`): 环境映射。

    Returns:
        `Any`: 插值后的新结构。

    Raises:
        ConfigInterpolationError: 某处引用了未定义且没有默认值的变量。

    Example:
        >>> interpolate_env("${A}", {"A": "1"})
        1
        >>> interpolate_env("${MISSING:-fallback}", {})
        'fallback'
    """

    def _substitute(text: str) -> Any:
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            value = environ.get(name)
            if value is None:
                if default is not None:
                    return default
                raise ConfigInterpolationError(
                    f"配置引用了未定义的环境变量: ${{{name}}}"
                    "（写 ${"
                    f"{name}:-默认值}} 可提供回退值）",
                )
            return value

        expanded = _ENV_VAR_RE.sub(repl, text)
        return _convert_value(expanded) if expanded != text else text

    if isinstance(raw, AppendList):
        return AppendList(interpolate_env(item, environ) for item in raw)
    if isinstance(raw, str):
        return _substitute(raw)
    if isinstance(raw, Mapping):
        return {key: interpolate_env(value, environ) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [interpolate_env(item, environ) for item in raw]
    return raw


def load_yaml(path: Path) -> dict[str, Any]:
    """读取一个 YAML 文件，做 ``${VAR}`` 插值，返回字典。

    插值使用的环境映射是 ``os.environ``；若调用方需要额外覆盖
    （例如把 :class:`~harness_kit.settings.Settings` 的字段也算进去），
    请用 :func:`interpolate_env` 自行二次处理。

    Args:
        path (`Path`): YAML 文件路径。

    Returns:
        `dict[str, Any]`: 插值后的顶层映射。

    Raises:
        FileNotFoundError: 文件不存在。
        ConfigParseError: YAML 语法错误，或顶层不是映射。
    """
    import os

    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.load(text, Loader=_HarnessYamlLoader)  # noqa: S506 - 用的是 SafeLoader 子类
    except yaml.YAMLError as exc:
        raise ConfigParseError(f"YAML 解析失败: {path}: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigParseError(
            f"YAML 顶层必须是映射（mapping），实际是 {type(raw).__name__}: {path}",
        )
    interpolated = interpolate_env(raw, dict(os.environ))
    if not isinstance(interpolated, dict):  # pragma: no cover - 类型收窄
        raise ConfigParseError(f"插值后顶层不是映射: {path}")
    return interpolated


# ======================================================================
# 按名查找
# ======================================================================
def _candidate_paths(name: str, search_dir: Path) -> list[Path]:
    """列出按名查找时的候选文件路径。

    Args:
        name (`str`): Profile / Bundle 名或路径。
        search_dir (`Path`): 搜索目录。

    Returns:
        `list[Path]`: 按优先级排列的候选路径。
    """
    raw = Path(name)
    looks_like_path = (
        raw.is_absolute()
        or "/" in name
        or "\\" in name
        or raw.suffix.lower() in _SUPPORTED_SUFFIXES
    )
    candidates: list[Path] = []
    if looks_like_path:
        candidates.append(raw if raw.is_absolute() else search_dir / raw)
    else:
        for suffix in _SUPPORTED_SUFFIXES:
            candidates.append(search_dir / f"{name}{suffix}")
    return candidates


def _locate(name: str, search_dir: Path) -> Path:
    """定位 Profile / Bundle 文件。

    Args:
        name (`str`): 名字或路径。
        search_dir (`Path`): 搜索目录。

    Returns:
        `Path`: 命中的文件路径。

    Raises:
        FileNotFoundError: 所有候选路径都不存在。
    """
    candidates = _candidate_paths(name, search_dir)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    existing = sorted(
        p.name
        for p in search_dir.glob("*")
        if p.suffix.lower() in _SUPPORTED_SUFFIXES
    ) if search_dir.is_dir() else []
    raise FileNotFoundError(
        f"找不到配置 '{name}'；已尝试 {[str(p) for p in candidates]}；"
        f"搜索目录 {search_dir} 下现有: {existing}",
    )


def load_profile(name_or_path: str | Path, *, search_dir: Path) -> Profile:
    """按名或按路径装载一个 Profile（**不解析** ``extends``）。

    Args:
        name_or_path (`str | Path`): Profile 名（如 ``"coding"``）或 YAML 路径。
        search_dir (`Path`): 按名查找时的搜索目录。

    Returns:
        `Profile`: 校验通过的 Profile；YAML 里没写 ``name`` 时用文件主名回填。

    Raises:
        FileNotFoundError: 文件不存在。
        ConfigParseError: YAML 非法。
        pydantic.ValidationError: 字段不符合契约。
    """
    path = _locate(str(name_or_path), Path(search_dir))
    payload = load_yaml(path)
    payload.setdefault("name", path.stem)
    payload.setdefault("description", "")
    # 用 from_payload 而不是 model_validate：它会把**校验前的原样片段**留给
    # Profile，`packs: !append [repo]` 的追加标记因此不会被 pydantic 的
    # list 校验吃掉（详见 harness_kit/config/schema.py 的 _RawOverlayMixin）。
    profile = Profile.from_payload(payload)
    logger.bind(profile=profile.name, path=str(path)).debug("已装载 Profile")
    return profile


def load_bundle(name: str, *, search_dir: Path) -> Bundle:
    """按名装载一个 Bundle。

    Args:
        name (`str`): Bundle 名或 YAML 路径。
        search_dir (`Path`): 按名查找时的搜索目录。

    Returns:
        `Bundle`: 校验通过的 Bundle。

    Raises:
        FileNotFoundError: 文件不存在（:func:`resolve_profile` 会把它转成
            :class:`ConfigNotFoundError`）。
        ConfigParseError: YAML 非法。
    """
    path = _locate(str(name), Path(search_dir))
    payload = load_yaml(path)
    payload.setdefault("name", path.stem)
    payload.setdefault("description", "")
    # 同 load_profile：保留原样片段，让 Bundle 里的 `!append` 生效。
    bundle = Bundle.from_payload(payload)
    logger.bind(bundle=bundle.name, path=str(path)).debug("已装载 Bundle")
    return bundle


def load_resolved_profile(
    name_or_path: str | Path,
    *,
    search_dir: Path,
) -> ResolvedProfile:
    """装载并解析，一步拿到 :class:`ResolvedProfile`。

    Args:
        name_or_path (`str | Path`): Profile 名或 YAML 路径。
        search_dir (`Path`): 按名查找时的搜索目录。

    Returns:
        `ResolvedProfile`: 合并冻结后的结果。
    """
    profile = load_profile(name_or_path, search_dir=Path(search_dir))
    return resolve_profile(profile, search_dir=Path(search_dir))


def discover(search_dir: Path) -> dict[str, Sequence[str]]:
    """列出搜索目录下可用的 Profile / Bundle 名字（CLI 的 ``profile list`` 用）。

    Args:
        search_dir (`Path`): 搜索目录。

    Returns:
        `dict[str, Sequence[str]]`: ``{"profiles": [...], "bundles": [...]}``。
    """
    profiles: list[str] = []
    bundles: list[str] = []
    if not search_dir.is_dir():
        return {"profiles": [], "bundles": []}
    for path in sorted(search_dir.iterdir()):
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        try:
            payload = load_yaml(path)
        except ConfigError:
            continue
        if "extends" in payload or "bundles" in payload or "agent" in payload:
            profiles.append(path.stem)
        else:
            bundles.append(path.stem)
    return {"profiles": profiles, "bundles": bundles}
