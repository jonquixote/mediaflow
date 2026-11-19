"""Preset loader, schema validation and the SPEC section 6 matrix.

[SAF-13] symlinks inside preset folders are ignored and reported
[MP4-5] MP4 presets require mov_text text subtitles
[MP4-10] HDR keep/ten-bit combinations are validated
[MP4-11] interlaced handling is review-only in v1
[MKV-1] MKV presets are remux-only and copy audio/subtitles
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaflow.presets import (  # noqa: E402
    Preset,
    PresetError,
    PresetMap,
    get_preset,
    load_preset_file,
    load_presets,
    parse_window,
    validate_presets,
)

REPO_PRESETS = Path(__file__).resolve().parents[1] / "presets"

BASE_PRESET: dict[str, object] = {
    "id": "test-preset",
    "description": "a valid test preset",
    "container": "mp4",
    "meta": {
        "experimental": False,
        "enabled": True,
    },
    "video": {
        "action": "encode",
        "codec": "hevc",
        "backend_order": ["qsv", "vaapi"],
        "allow_software": False,
        "quality": 24,
        "look_ahead": True,
        "max_width": 1920,
        "max_height": 1080,
        "ten_bit_source": "keep",
        "tag": "hvc1",
        "hdr": "review",
        "interlaced": "review",
    },
    "audio": {
        "languages": ["original", "eng"],
        "max_tracks": 3,
        "copy_codecs": ["aac", "ac3", "eac3"],
        "exclude_commentary": True,
        "unknown_language": "keep_first",
    },
    "subtitles": {
        "languages": ["eng"],
        "include_forced": True,
        "text": "mov_text",
        "bitmap": "mkv_fallback",
        "mkv_policy": "bitmap_only",
    },
    "skip": {
        "already_efficient": True,
        "efficient_kbps_1080p": 4000,
        "min_savings_pct": 15,
    },
    "schedule": {
        "window": "",
    },
}


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot render {value!r} as TOML")


def _render(data: dict[str, object]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{key}]")
            for child_key, child_value in value.items():
                lines.append(f"{child_key} = {_toml_value(child_value)}")
    return "\n".join(lines) + "\n"


def _set_dotted(data: dict[str, object], dotted: str, value: object) -> None:
    parts = dotted.split(".")
    node: dict[str, object] = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _delete_dotted(data: dict[str, object], dotted: str) -> None:
    parts = dotted.split(".")
    node: dict[str, object] = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return
        node = child
    node.pop(parts[-1], None)


def write_preset(
    directory: Path,
    preset_id: str = "test-preset",
    *,
    overrides: dict[str, object] | None = None,
    remove: tuple[str, ...] = (),
    declared_id: str | None = None,
    filename: str | None = None,
    data: dict[str, object] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    document = copy.deepcopy(BASE_PRESET if data is None else data)
    document["id"] = preset_id if declared_id is None else declared_id
    for dotted, value in (overrides or {}).items():
        _set_dotted(document, dotted, value)
    for dotted in remove:
        _delete_dotted(document, dotted)
    path = directory / (filename or f"{preset_id}.toml")
    path.write_text(_render(document), encoding="utf-8")
    return path


def fresh_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "presets"
    directory.mkdir()
    return directory


# ---------------------------------------------------------------------------
# Shipped presets
# ---------------------------------------------------------------------------


def test_shipped_presets_are_exactly_five_enabled() -> None:
    presets = load_presets(REPO_PRESETS)

    assert isinstance(presets, dict)
    assert isinstance(presets, PresetMap)
    assert set(presets) == {
        "keep-remux-mp4",
        "keep-remux-mkv",
        "1080p-balanced-mp4",
        "1080p-small-mp4",
        "720p-small-mp4",
    }
    assert all(preset.enabled for preset in presets.values())
    assert all(not preset.experimental for preset in presets.values())
    assert presets.warnings == []
    assert "1080p-best-x265-mp4" not in presets


def test_shipped_presets_match_their_documented_configuration() -> None:
    presets = load_presets(REPO_PRESETS)

    remux_mp4 = presets["keep-remux-mp4"]
    assert remux_mp4.container == "mp4"
    assert remux_mp4.video.action == "remux"
    assert remux_mp4.video.codec == "copy"
    assert remux_mp4.video.hdr == "review"
    assert remux_mp4.video.ten_bit_source is None
    assert remux_mp4.audio.copy_codecs == ("aac", "ac3", "eac3")
    assert remux_mp4.subtitles.text == "mov_text"
    assert remux_mp4.subtitles.bitmap == "mkv_fallback"
    assert remux_mp4.raw["id"] == "keep-remux-mp4"

    remux_mkv = presets["keep-remux-mkv"]
    assert remux_mkv.container == "mkv"
    assert remux_mkv.video.action == "remux"
    assert remux_mkv.video.codec == "any"
    assert remux_mkv.video.hdr == "keep"
    assert remux_mkv.video.ten_bit_source is None
    assert remux_mkv.audio.copy_codecs == ("*",)
    assert remux_mkv.subtitles.text == "copy"
    # Output is already MKV; the bitmap policy is documented but not consulted.
    assert remux_mkv.subtitles.bitmap == "drop"
    assert remux_mkv.subtitles.mkv_policy == "bitmap_only"

    balanced = presets["1080p-balanced-mp4"]
    assert balanced.video.action == "encode"
    assert balanced.video.backend_order == ("qsv", "vaapi")
    assert balanced.video.quality == 24
    assert balanced.video.max_width == 1920
    assert balanced.video.max_height == 1080
    assert balanced.video.allow_software is False
    assert balanced.video.hdr == "review"
    assert balanced.video.ten_bit_source == "keep"
    assert balanced.schedule.window == ""

    small = presets["1080p-small-mp4"]
    assert small.video.quality == 28
    assert small.video.max_width == 1920
    assert small.video.max_height == 1080
    assert small.video.allow_software is False

    small_720 = presets["720p-small-mp4"]
    assert small_720.video.quality == 28
    assert small_720.video.max_width == 1280
    assert small_720.video.max_height == 720


def test_experimental_preset_is_opt_in_disabled_and_scheduled() -> None:
    base = load_presets(REPO_PRESETS)
    assert "1080p-best-x265-mp4" not in base

    presets = load_presets(REPO_PRESETS, include_experimental=True)
    assert len(presets) == 6
    experimental = presets["1080p-best-x265-mp4"]
    assert experimental.experimental is True
    assert experimental.enabled is False
    assert experimental.container == "mp4"
    assert experimental.video.action == "encode"
    assert experimental.video.backend_order == ("software",)
    assert experimental.video.allow_software is True
    assert experimental.video.quality == 20
    assert experimental.video.ten_bit_source == "keep"
    assert experimental.schedule.window == "01:00-07:00"
    # Disabled experimental presets do not produce the speed-table warning.
    assert presets.warnings == []


def test_every_shipped_preset_validates_without_errors_or_warnings() -> None:
    presets = load_presets(REPO_PRESETS, include_experimental=True)
    result = validate_presets(presets)
    assert result.ok is True
    assert result.errors == ()
    assert result.warnings == ()


def test_load_preset_file_and_path_metadata() -> None:
    preset = load_preset_file(REPO_PRESETS / "1080p-balanced-mp4.toml")
    assert preset.id == "1080p-balanced-mp4"
    assert preset.path is not None
    assert preset.path.name == "1080p-balanced-mp4.toml"
    assert preset.raw["video"]["quality"] == 24

    with pytest.raises(PresetError, match="not found"):
        load_preset_file(REPO_PRESETS / "absent.toml")


# ---------------------------------------------------------------------------
# Section 6 matrix: every row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,remove,needle",
    [
        # mkv_policy=never with bitmap=mkv_fallback (circular).
        (
            {"subtitles.mkv_policy": "never"},
            (),
            "mkv_fallback",
        ),
        # container=mkv with action=encode.
        (
            {
                "container": "mkv",
                "video.action": "encode",
                "subtitles.text": "copy",
                "audio.copy_codecs": ["*"],
            },
            (),
            "remux-only",
        ),
        # container=mp4 with copy_codecs=["*"].
        (
            {"audio.copy_codecs": ["*"]},
            (),
            "copy_codecs",
        ),
        # hdr=keep on an encode without ten_bit_source=keep.
        (
            {"video.hdr": "keep"},
            ("video.ten_bit_source",),
            "ten_bit_source",
        ),
        # ten_bit_source=to8 together with hdr=keep.
        (
            {"video.hdr": "keep", "video.ten_bit_source": "to8"},
            (),
            "to8",
        ),
        # ten_bit_source on a remux preset.
        (
            {"video.action": "remux", "video.backend_order": []},
            (),
            "absent on a remux",
        ),
        # allow_software=true without a non-empty schedule window.
        (
            {"video.allow_software": True},
            (),
            "window",
        ),
        # quality outside 1-50.
        ({"video.quality": 0}, (), "quality"),
        ({"video.quality": 51}, (), "quality"),
        # min_savings_pct outside 0-90.
        ({"skip.min_savings_pct": -1}, (), "min_savings_pct"),
        ({"skip.min_savings_pct": 91}, (), "min_savings_pct"),
    ],
)
def test_section_6_matrix_rejects(
    tmp_path: Path,
    overrides: dict[str, object],
    remove: tuple[str, ...],
    needle: str,
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, overrides=overrides, remove=remove)

    with pytest.raises(PresetError) as excinfo:
        load_presets(directory)
    joined = str(excinfo.value)
    assert needle in joined
    assert "test-preset" in joined


def test_experimental_enabled_without_speed_entry_is_only_a_warning(
    tmp_path: Path,
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(
        directory,
        overrides={
            "meta.experimental": True,
            "meta.enabled": True,
        },
    )

    presets = load_presets(directory)
    assert len(presets) == 1
    assert presets.warnings
    assert "speed-table" in presets.warnings[0]
    assert "test-preset" in presets.warnings[0]

    result = validate_presets(presets)
    assert result.ok is True
    assert result.errors == ()
    assert result.warnings and "speed-table" in result.warnings[0]


@pytest.mark.parametrize(
    "speed_keys",
    [
        [("test-preset", "qsv", 1080)],
        ["test-preset:qsv:1080"],
        {"test-preset": object()},
        [{"preset": "test-preset", "backend": "qsv", "bucket": 1080}],
    ],
)
def test_experimental_enabled_with_speed_entry_has_no_warning(
    tmp_path: Path, speed_keys: object
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(
        directory,
        overrides={
            "meta.experimental": True,
            "meta.enabled": True,
        },
    )
    presets = load_presets(directory, speed_keys=speed_keys)
    assert presets.warnings == []


# ---------------------------------------------------------------------------
# Identity, unknown keys and types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"mystery": 1}, "unknown key 'mystery'"),
        ({"video.quality_hint": 1}, "unknown key 'quality_hint'"),
        ({"audio.bitrate": 640}, "unknown key 'bitrate'"),
        ({"future": {"mode": "x"}}, "unknown key 'future'"),
    ],
)
def test_unknown_keys_fail_loudly(
    tmp_path: Path, overrides: dict[str, object], needle: str
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, overrides=overrides)
    with pytest.raises(PresetError, match="unknown key") as excinfo:
        load_presets(directory)
    assert needle in str(excinfo.value)


def test_folder_name_must_equal_id(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "folder-name", declared_id="different-id")
    with pytest.raises(PresetError, match="must equal") as excinfo:
        load_presets(directory)
    assert "folder-name" in str(excinfo.value)
    assert "different-id" in str(excinfo.value)


def test_unknown_preset_id_is_a_clear_error() -> None:
    presets = load_presets(REPO_PRESETS)
    with pytest.raises(PresetError, match="unknown preset id") as excinfo:
        get_preset(presets, "does-not-exist")
    assert "keep-remux-mp4" in str(excinfo.value)


def test_validation_mapping_key_must_match_preset_id() -> None:
    preset = load_preset_file(REPO_PRESETS / "1080p-balanced-mp4.toml")
    result = validate_presets({"wrong-key": preset})
    assert result.ok is False
    assert any("does not match id" in error for error in result.errors)


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"video.quality": "24"}, "quality"),
        ({"video.quality": True}, "quality"),
        ({"container": "avi"}, "container"),
        ({"video.action": "transcode"}, "video.action"),
        ({"video.codec": "h264"}, "hevc"),
        ({"video.hdr": "preserve"}, "video.hdr"),
        ({"video.interlaced": "keep"}, "interlaced"),
        ({"video.ten_bit_source": "drop"}, "ten_bit_source"),
        ({"audio.unknown_language": "drop_all"}, "unknown_language"),
        ({"audio.max_tracks": 0}, "max_tracks"),
        ({"audio.languages": []}, "languages"),
        ({"video.backend_order": ["nvenc"]}, "backend_order"),
        ({"video.backend_order": "qsv"}, "backend_order"),
        ({"video.max_width": 1921}, "max_width"),
        ({"video.max_height": 0}, "max_height"),
        ({"subtitles.bitmap": "burn"}, "bitmap"),
        ({"subtitles.mkv_policy": "sometimes"}, "mkv_policy"),
        ({"subtitles.text": "ass"}, "text"),
        ({"subtitles.include_forced": "yes"}, "include_forced"),
        ({"schedule.window": "1:00-7:00"}, "window"),
        ({"schedule.window": "25:00-26:00"}, "window"),
        ({"skip.already_efficient": 1}, "already_efficient"),
        ({"skip.efficient_kbps_1080p": -1}, "efficient_kbps_1080p"),
        ({"skip.min_savings_pct": True}, "min_savings_pct"),
        ({"meta.enabled": 1}, "meta.enabled"),
        ({"video.allow_software": "yes"}, "allow_software"),
        ({"video.look_ahead": 1}, "look_ahead"),
        ({"video.tag": "hev1"}, "tag"),
    ],
)
def test_type_and_enum_validation(
    tmp_path: Path, overrides: dict[str, object], needle: str
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, overrides=overrides)
    with pytest.raises(PresetError) as excinfo:
        load_presets(directory)
    assert needle in str(excinfo.value)


# ---------------------------------------------------------------------------
# Valid alternatives, discovery and installation behaviour
# ---------------------------------------------------------------------------


def test_valid_mkv_remux_and_any_subs_are_accepted(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(
        directory,
        "mkv-remux",
        overrides={
            "container": "mkv",
            "video.action": "remux",
            "video.backend_order": [],
            "video.codec": "any",
            "video.hdr": "keep",
            "audio.copy_codecs": ["*"],
            "subtitles.text": "copy",
            "subtitles.bitmap": "drop",
            "subtitles.mkv_policy": "any_subs",
        },
        remove=("video.ten_bit_source",),
    )
    presets = load_presets(directory)
    preset = presets["mkv-remux"]
    assert preset.container == "mkv"
    assert preset.subtitles.mkv_policy == "any_subs"
    assert preset.video.ten_bit_source is None


def test_minimal_mkv_remux_uses_shared_schema_without_false_errors(
    tmp_path: Path,
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(
        directory,
        "minimal-mkv",
        overrides={
            "container": "mkv",
            "video.action": "remux",
            "video.backend_order": [],
        },
        remove=("video.ten_bit_source",),
    )
    preset = load_presets(directory)["minimal-mkv"]
    assert preset.container == "mkv"
    assert preset.video.ten_bit_source is None


def test_symlink_in_preset_folder_is_ignored_and_reported(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "main-preset")
    outside = tmp_path / "outside"
    target = write_preset(outside, "linked-preset")
    link = directory / "linked-preset.toml"
    os.symlink(target, link)

    presets = load_presets(directory)

    assert set(presets) == {"main-preset"}
    assert any("symlink" in warning for warning in presets.warnings)
    assert any(str(link) in warning for warning in presets.warnings)


def test_loader_is_non_recursive_and_ignores_non_toml(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "main-preset")
    (directory / "README.txt").write_text("not a preset", encoding="utf-8")
    nested = directory / "nested"
    nested.mkdir()
    (nested / "broken.toml").write_text("this is not toml [", encoding="utf-8")

    presets = load_presets(directory)
    assert set(presets) == {"main-preset"}


def test_missing_preset_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PresetError, match="not found"):
        load_presets(tmp_path / "absent")


def test_missing_experimental_directory_is_a_warning(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "main-preset")
    presets = load_presets(directory, include_experimental=True)
    assert set(presets) == {"main-preset"}
    assert any("experimental" in warning for warning in presets.warnings)


def test_disabled_experimental_preset_is_returned_but_not_enabled(
    tmp_path: Path,
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "main-preset")
    exp = directory / "experimental"
    write_preset(
        exp,
        "exp-preset",
        overrides={"meta.experimental": True, "meta.enabled": False},
    )

    presets = load_presets(directory, include_experimental=True)
    assert set(presets) == {"main-preset", "exp-preset"}
    assert presets["exp-preset"].enabled is False
    assert list(presets.enabled) == ["main-preset"]


def test_symlinked_experimental_folder_is_ignored(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "main-preset")
    real_exp = tmp_path / "real-experimental"
    write_preset(
        real_exp,
        "exp-preset",
        overrides={"meta.experimental": True, "meta.enabled": False},
    )
    os.symlink(real_exp, directory / "experimental")

    presets = load_presets(directory, include_experimental=True)
    assert set(presets) == {"main-preset"}
    assert any("symlink" in warning for warning in presets.warnings)


def test_duplicate_id_across_base_and_experimental_is_rejected(
    tmp_path: Path,
) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "same-id")
    exp = directory / "experimental"
    write_preset(exp, "same-id")

    with pytest.raises(PresetError, match="duplicate preset id"):
        load_presets(directory, include_experimental=True)


def test_load_preset_file_reports_validation_errors(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    path = write_preset(directory, overrides={"video.quality": 99})
    with pytest.raises(PresetError, match="quality"):
        load_preset_file(path)


def test_disabled_base_preset_is_still_returned(tmp_path: Path) -> None:
    directory = fresh_dir(tmp_path)
    write_preset(directory, "enabled-preset")
    write_preset(
        directory,
        "disabled-preset",
        overrides={"meta.enabled": False},
    )
    presets = load_presets(directory)
    assert presets["disabled-preset"].enabled is False
    assert set(presets.enabled) == {"enabled-preset"}


def test_parse_window_helper() -> None:
    assert parse_window("") is None
    assert parse_window("01:00-07:00") == (60, 420)
    assert parse_window("23:59-00:00") == (1439, 0)
    for bad in ("1:00-07:00", "01:00", "01:60-07:00", "24:00-07:00", " 01:00-07:00"):
        with pytest.raises(ValueError, match="window"):
            parse_window(bad)


def test_preset_map_is_a_plain_dict_for_callers() -> None:
    presets = load_presets(REPO_PRESETS)
    assert isinstance(presets, dict)
    preset = get_preset(presets, "720p-small-mp4")
    assert isinstance(preset, Preset)
    assert preset.window == ""
    assert preset.action == "encode"
    assert preset.quality == 28
