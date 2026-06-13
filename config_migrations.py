"""fetch_url 插件配置版本迁移。

Runner 在 ``plugin.config_version`` 低于模型默认值时会调用 ``rebuild_plugin_config_data``，
但只会补齐新增字段、保留用户已有键值——**不会**自动把「仍是旧默认值的字段」更新为新默认。

本模块在版本升级时补做这类迁移：仅当字段值仍等于已知旧默认时才写入新默认，
用户主动改过的值不会被覆盖。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 与 PluginSectionConfig.config_version 默认值保持同步
CURRENT_CONFIG_VERSION = "1.5.0"

# 历史上各版本发布时的默认值快照（仅用于「是否仍为旧默认」判定）
_LEGACY_DEFAULTS: dict[str, dict[str, Any]] = {
    "fetch.max_download_size": {
        "<1.5.0": 16 * 1024 * 1024,
    },
    "image.max_image_size": {
        "<1.5.0": 2 * 1024 * 1024,
    },
    "image.max_dimension": {
        "<1.5.0": 2048,
    },
    "alt_text.image.max_image_size": {
        "1.3.0": 512 * 1024,
    },
    "alt_text.image.max_dimension": {
        "1.3.0": 1024,
        "1.4.0": 1024,
    },
    "alt_text.image.animated_policy": {
        "1.3.0": "first_frame",
        "1.4.0": "first_frame",
    },
}

# 当前版本目标默认（与 FetchUrlConfig 模型一致）
_CURRENT_DEFAULTS: dict[str, Any] = {
    "fetch.max_download_size": 64 * 1024 * 1024,
    "image.max_image_size": 16 * 1024 * 1024,
    "image.max_dimension": 4096,
    "alt_text.image.target_image_size": 1024 * 1024,
    "alt_text.image.max_dimension": 2048,
    "alt_text.image.max_quality": 80,
    "alt_text.image.min_quality": 10,
    "alt_text.image.animated_policy": "keep_animated",
    "alt_text.image.max_animation_frames": 512,
    "alt_text.image.convert_format": "webp",
}


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version or "0").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def version_less_than(left: str, right: str) -> bool:
    """判断 ``left`` 是否严格早于 ``right``。"""
    return _version_tuple(left) < _version_tuple(right)


def _get_nested(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _set_nested(config: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = config
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


def _del_nested(config: dict[str, Any], dotted: str) -> None:
    keys = dotted.split(".")
    node: Any = config
    for key in keys[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
    if isinstance(node, dict):
        node.pop(keys[-1], None)


class _Missing:
    pass


_MISSING = _Missing()


def _reset_if_legacy_default(
    config: dict[str, Any],
    dotted: str,
    *,
    before_version: str,
    new_value: Any,
    notes: list[str],
) -> None:
    """字段值仍等于 ``before_version`` 之前的已知旧默认时，写入 ``new_value``。"""
    current = _get_nested(config, dotted)
    if current is _MISSING:
        return
    legacy_map = _LEGACY_DEFAULTS.get(dotted, {})
    old_default = legacy_map.get(before_version) or legacy_map.get(f"<{before_version}")
    if old_default is None:
        return
    if current != old_default:
        return
    _set_nested(config, dotted, new_value)
    notes.append(f"{dotted}: {old_default!r} -> {new_value!r}")


def _rename_field_if_present(
    config: dict[str, Any],
    section: str,
    old_key: str,
    new_key: str,
    notes: list[str],
) -> None:
    section_data = config.get(section)
    if not isinstance(section_data, dict):
        return
    nested = section_data.get("image")
    if not isinstance(nested, dict):
        return
    if old_key not in nested or new_key in nested:
        return
    nested[new_key] = nested.pop(old_key)
    notes.append(f"{section}.image.{old_key} 重命名为 {new_key}")


def migrate_plugin_config_data(config: dict[str, Any], from_version: str) -> tuple[dict[str, Any], list[str]]:
    """按 ``from_version`` 应用增量迁移，返回迁移后的配置与变更说明。"""
    notes: list[str] = []
    if not version_less_than(from_version, CURRENT_CONFIG_VERSION):
        return config, notes

    # --- 1.3.x / 1.4.x：VLM 图片节字段重命名 ---
    if version_less_than(from_version, "1.4.0"):
        _rename_field_if_present(config, "alt_text", "max_image_size", "target_image_size", notes)
        _rename_field_if_present(config, "alt_text", "quality_start", "max_quality", notes)
        _rename_field_if_present(config, "alt_text", "quality_floor", "min_quality", notes)

    # --- 1.5.0：入站 / 抓取默认调整；移除误加到 [image] 的动图字段 ---
    if version_less_than(from_version, "1.5.0"):
        _reset_if_legacy_default(
            config,
            "fetch.max_download_size",
            before_version="1.5.0",
            new_value=_CURRENT_DEFAULTS["fetch.max_download_size"],
            notes=notes,
        )
        _reset_if_legacy_default(
            config,
            "image.max_image_size",
            before_version="1.5.0",
            new_value=_CURRENT_DEFAULTS["image.max_image_size"],
            notes=notes,
        )
        _reset_if_legacy_default(
            config,
            "image.max_dimension",
            before_version="1.5.0",
            new_value=_CURRENT_DEFAULTS["image.max_dimension"],
            notes=notes,
        )
        image_section = config.get("image")
        if isinstance(image_section, dict):
            for obsolete in ("animated_policy", "max_animation_frames"):
                if obsolete in image_section:
                    image_section.pop(obsolete, None)
                    notes.append(f"image.{obsolete}: 已移除（动图策略仅保留在 alt_text.image）")

        alt_section = config.get("alt_text")
        if isinstance(alt_section, dict):
            nested = alt_section.get("image")
            if not isinstance(nested, dict):
                nested = {}
                alt_section["image"] = nested
            if "max_image_size" in nested and "target_image_size" not in nested:
                nested["target_image_size"] = nested.pop("max_image_size")
                notes.append("alt_text.image.max_image_size 重命名为 target_image_size")
            if "quality_start" in nested and "max_quality" not in nested:
                nested["max_quality"] = nested.pop("quality_start")
                notes.append("alt_text.image.quality_start 重命名为 max_quality")
            if "quality_floor" in nested and "min_quality" not in nested:
                nested["min_quality"] = nested.pop("quality_floor")
                notes.append("alt_text.image.quality_floor 重命名为 min_quality")
            if nested.get("target_image_size") == 512 * 1024:
                nested["target_image_size"] = _CURRENT_DEFAULTS["alt_text.image.target_image_size"]
                notes.append("alt_text.image.target_image_size: 512KB -> 1MB（仍为旧默认）")
            if nested.get("animated_policy") == "first_frame":
                nested["animated_policy"] = _CURRENT_DEFAULTS["alt_text.image.animated_policy"]
                notes.append("alt_text.image.animated_policy: first_frame -> keep_animated")
            if nested.get("max_dimension") == 1024:
                nested["max_dimension"] = _CURRENT_DEFAULTS["alt_text.image.max_dimension"]
                notes.append("alt_text.image.max_dimension: 1024 -> 2048（仍为旧默认）")

    plugin_section = config.get("plugin")
    if isinstance(plugin_section, dict):
        plugin_section["config_version"] = CURRENT_CONFIG_VERSION

    return config, notes


def normalize_fetch_url_config(
    raw_config: Mapping[str, Any] | None,
    default_config: Mapping[str, Any],
) -> tuple[dict[str, Any], bool, list[str]]:
    """合并默认配置并执行版本迁移（供 ``FetchUrlPlugin.normalize_plugin_config`` 调用）。"""
    from maibot_sdk.config import (
        extract_plugin_config_version,
        merge_plugin_config_data,
        rebuild_plugin_config_data,
    )

    raw: dict[str, Any] = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    if not raw:
        merged = rebuild_plugin_config_data(default_config, {})
        return merged, True, ["空配置，已填充全部默认值"]

    from_version = extract_plugin_config_version(raw)
    latest_version = extract_plugin_config_version(default_config)

    if version_less_than(from_version, latest_version):
        working = rebuild_plugin_config_data(default_config, raw)
        working, notes = migrate_plugin_config_data(working, from_version)
        return working, True, notes

    working, changed = merge_plugin_config_data(default_config, raw)
    return working, changed, []
