"""Preset loading and the SPEC section 6 validation matrix.

Presets are data: one TOML file per preset, with the file name equal to the
preset id.  The loader is deliberately strict.  Unknown keys, wrong types,
out-of-range values and the section 6 cross-field rules all fail loudly before
a preset can reach the planner.

Experimental presets live in a separate directory and are not loaded unless
the caller explicitly asks for them.  An enabled experimental preset with no
speed-table entry is a *warning*, because the speed table is in the database
and cannot be known here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import copy
import math
from pathlib import Path
import re
import tomllib
from typing import Any

__all__ = [
    "ACTIONS",
    "BACKENDS",
    "BITMAP_POLICIES",
    "CONTAINERS",
    "HDR_MODES",
    "INTERLACED_MODES",
    "MKV_POLICIES",
    "SUBTITLE_TEXT_CODECS",
    "TEN_BIT_SOURCES",
    "UNKNOWN_LANGUAGE_MODES",
    "VIDEO_CODECS",
    "Audio",
    "Meta",
    "Preset",
    "PresetError",
    "PresetMap",
    "PresetValidationError",
    "Schedule",
    "Skip",
    "Subtitles",
    "ValidationResult",
    "Video",
    "get_preset",
    "load_preset_file",
    "load_presets",
    "parse_window",
    "validate_presets",
]

CONTAINERS = frozenset({"mp4", "mkv"})
ACTIONS = frozenset({"encode", "remux"})
# v1 encodes HEVC only; remux presets say "copy" (MP4's H.264/HEVC rule is
# applied by the planner) or "any" (MKV copies whatever the source has).
VIDEO_CODECS = frozenset({"hevc", "copy", "any"})
BACKENDS = frozenset({"qsv", "vaapi", "software"})
TEN_BIT_SOURCES = frozenset({"keep", "to8", "review"})
HDR_MODES = frozenset({"review", "keep"})
INTERLACED_MODES = frozenset({"review"})
UNKNOWN_LANGUAGE_MODES = frozenset({"keep_first", "keep_all"})
SUBTITLE_TEXT_CODECS = frozenset({"mov_text", "copy"})
BITMAP_POLICIES = frozenset({"mkv_fallback", "drop"})
MKV_POLICIES = frozenset({"bitmap_only", "any_subs", "never"})

_WINDOW_RE = re.compile(
    r"^((?:[01]\d|2[0-3]):[0-5]\d)-((?:[01]\d|2[0-3]):[0-5]\d)$"
)
_PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_TOP_KEYS = frozenset(
    {
        "id",
        "description",
        "container",
        "meta",
        "video",
        "audio",
        "subtitles",
        "skip",
        "schedule",
    }
)
_SECTION_KEYS = {
    "meta": frozenset({"experimental", "enabled"}),
    "video": frozenset(
        {
            "action",
            "codec",
            "backend_order",
            "allow_software",
            "quality",
            "look_ahead",
            "max_width",
            "max_height",
            "ten_bit_source",
            "tag",
            "hdr",
            "interlaced",
        }
    ),
    "audio": frozenset(
        {
            "languages",
            "max_tracks",
            "copy_codecs",
            "exclude_commentary",
            "unknown_language",
        }
    ),
    "subtitles": frozenset(
        {"languages", "include_forced", "text", "bitmap", "mkv_policy"}
    ),
    "skip": frozenset(
        {"already_efficient", "efficient_kbps_1080p", "min_savings_pct"}
    ),
    "schedule": frozenset({"window"}),
}


class PresetError(ValueError):
    """A preset file is syntactically or semantically invalid."""

    def __init__(self, messages: str | Iterable[str]) -> None:
        if isinstance(messages, str):
            self.messages: tuple[str, ...] = (messages,)
        else:
            self.messages = tuple(str(message) for message in messages)
        super().__init__("\n".join(self.messages))


# Backwards-friendly alias for callers that like an explicit name.
PresetValidationError = PresetError


@dataclass(frozen=True, slots=True)
class Meta:
    experimental: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Video:
    action: str = "encode"
    codec: str = "hevc"
    backend_order: tuple[str, ...] = ("qsv", "vaapi")
    allow_software: bool = False
    quality: int = 24
    look_ahead: bool = True
    max_width: int = 1920
    max_height: int = 1080
    ten_bit_source: str | None = None
    tag: str | None = "hvc1"
    hdr: str = "review"
    interlaced: str = "review"


@dataclass(frozen=True, slots=True)
class Audio:
    languages: tuple[str, ...] = ("original", "eng")
    max_tracks: int = 3
    copy_codecs: tuple[str, ...] = ("aac", "ac3", "eac3")
    exclude_commentary: bool = True
    unknown_language: str = "keep_first"


@dataclass(frozen=True, slots=True)
class Subtitles:
    languages: tuple[str, ...] = ("eng",)
    include_forced: bool = True
    text: str = "mov_text"
    bitmap: str = "mkv_fallback"
    mkv_policy: str = "bitmap_only"


@dataclass(frozen=True, slots=True)
class Skip:
    already_efficient: bool = True
    efficient_kbps_1080p: float = 4000.0
    min_savings_pct: float = 15.0


@dataclass(frozen=True, slots=True)
class Schedule:
    window: str = ""


@dataclass(frozen=True, slots=True)
class Preset:
    """One preset, with its sections exposed as typed dataclasses."""

    id: str
    description: str = ""
    container: str = "mp4"
    meta: Meta = field(default_factory=Meta)
    video: Video = field(default_factory=Video)
    audio: Audio = field(default_factory=Audio)
    subtitles: Subtitles = field(default_factory=Subtitles)
    skip: Skip = field(default_factory=Skip)
    schedule: Schedule = field(default_factory=Schedule)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)
    path: Path | None = field(default=None, repr=False, compare=False)

    @property
    def enabled(self) -> bool:
        return self.meta.enabled

    @property
    def experimental(self) -> bool:
        return self.meta.experimental

    # Small conveniences for planners; the grouped dataclasses remain the
    # canonical interface.
    @property
    def action(self) -> str:
        return self.video.action

    @property
    def quality(self) -> int:
        return self.video.quality

    @property
    def window(self) -> str:
        return self.schedule.window


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Errors and warnings from :func:`validate_presets`."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise PresetError(self.errors)


class PresetMap(dict[str, Preset]):
    """A normal ``dict[str, Preset]`` that also carries loader warnings."""

    def __init__(
        self,
        *args: Any,
        warnings: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.warnings: list[str] = list(warnings)

    @property
    def enabled(self) -> dict[str, Preset]:
        """Only the presets whose ``meta.enabled`` is true."""

        return {preset_id: preset for preset_id, preset in self.items() if preset.enabled}


def parse_window(window: str) -> tuple[int, int] | None:
    """Return ``(start_minutes, end_minutes)`` or ``None`` for an empty window.

    ``ValueError`` is raised for anything that is not ``""`` or exactly
    ``HH:MM-HH:MM``.  Equal endpoints are accepted by the parser; the scheduler
    may treat that as a zero-length window, but the preset schema only fixes
    the format.
    """

    if window == "":
        return None
    match = _WINDOW_RE.fullmatch(window)
    if match is None:
        raise ValueError(f"window must be 'HH:MM-HH:MM' or '', got {window!r}")
    start_hour, start_minute = (int(part) for part in match.group(1).split(":"))
    end_hour, end_minute = (int(part) for part in match.group(2).split(":"))
    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return (
        type(value) is not bool
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _not_enum(value: Any, allowed: frozenset[str]) -> bool:
    """Type-safe inverse membership test for a string enum."""

    return not isinstance(value, str) or value not in allowed


def _as_tuple(value: Any) -> Any:
    # TOML only produces lists.  Keep a wrong type intact so the validator can
    # report it precisely instead of turning a string into characters.
    if isinstance(value, list):
        return tuple(value)
    return value


def _check_unknown_keys(
    table: Mapping[str, Any],
    allowed: frozenset[str],
    where: str,
    errors: list[str],
) -> None:
    for key in sorted(set(table) - allowed):
        location = where if where else "top level"
        errors.append(f"{location}: unknown key {key!r}")


def _section(
    raw: Mapping[str, Any],
    name: str,
    errors: list[str],
) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        errors.append(f"{name}: section must be a table")
        return {}
    _check_unknown_keys(value, _SECTION_KEYS[name], f"[{name}]", errors)
    return value


def _structural_errors(
    raw: Mapping[str, Any],
    path: Path,
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    errors: list[str] = []
    _check_unknown_keys(raw, _TOP_KEYS, "", errors)

    for required in ("id", "description", "container"):
        if required not in raw:
            errors.append(f"missing required key {required!r}")

    sections: dict[str, Mapping[str, Any]] = {}
    for name in ("meta", "video", "audio", "subtitles", "skip", "schedule"):
        sections[name] = _section(raw, name, errors)

    if "action" not in sections["video"]:
        errors.append("missing required key 'action' in [video]")

    raw_id = raw.get("id")
    if "id" in raw and not isinstance(raw_id, str):
        errors.append("id must be a string")
    elif isinstance(raw_id, str) and raw_id != path.stem:
        errors.append(
            f"preset id {raw_id!r} must equal the folder/file name "
            f"{path.stem!r} (in {path.name})"
        )

    for key in ("description", "container"):
        if key in raw and not isinstance(raw[key], str):
            errors.append(f"{key} must be a string")

    return sections, errors


def _parse_preset_file(
    path: Path,
) -> tuple[Preset | None, list[str]]:
    """Parse one file, returning a preset even when semantic checks fail.

    Returning the preset lets :func:`validate_presets` report all remaining
    errors in one pass.  The caller raises if any structural errors exist.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or "read error"
        return None, [f"{path.name}: cannot read preset: {reason}"]
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        message = getattr(exc, "msg", "invalid TOML")
        where = f" at line {line}" if line is not None else ""
        if column is not None:
            where += f", column {column}"
        return None, [f"{path.name}: invalid TOML{where}: {message}"]
    if not isinstance(raw, dict):  # pragma: no cover - tomllib always returns dict
        return None, [f"{path.name}: top level must be a table"]

    sections, errors = _structural_errors(raw, path)

    meta = sections["meta"]
    video = sections["video"]
    audio = sections["audio"]
    subtitles = sections["subtitles"]
    skip = sections["skip"]
    schedule = sections["schedule"]

    preset_id = raw.get("id")
    if not isinstance(preset_id, str):
        # Structural errors already record the bad/missing id; use the file
        # name so downstream messages still have a stable identity.
        preset_id = path.stem

    try:
        preset = Preset(
            id=preset_id,
            description=raw.get("description", ""),
            container=raw.get("container", "mp4"),
            meta=Meta(
                experimental=meta.get("experimental", False),
                enabled=meta.get("enabled", True),
            ),
            video=Video(
                action=video.get("action", "encode"),
                codec=video.get("codec", "hevc"),
                backend_order=_as_tuple(video.get("backend_order", ("qsv", "vaapi"))),
                allow_software=video.get("allow_software", False),
                quality=video.get("quality", 24),
                look_ahead=video.get("look_ahead", True),
                max_width=video.get("max_width", 1920),
                max_height=video.get("max_height", 1080),
                ten_bit_source=video.get("ten_bit_source"),
                tag=video.get("tag"),
                hdr=video.get("hdr", "review"),
                interlaced=video.get("interlaced", "review"),
            ),
            audio=Audio(
                languages=_as_tuple(audio.get("languages", ("original", "eng"))),
                max_tracks=audio.get("max_tracks", 3),
                copy_codecs=_as_tuple(
                    audio.get("copy_codecs", ("aac", "ac3", "eac3"))
                ),
                exclude_commentary=audio.get("exclude_commentary", True),
                unknown_language=audio.get("unknown_language", "keep_first"),
            ),
            subtitles=Subtitles(
                languages=_as_tuple(subtitles.get("languages", ("eng",))),
                include_forced=subtitles.get("include_forced", True),
                text=subtitles.get("text", "mov_text"),
                bitmap=subtitles.get("bitmap", "mkv_fallback"),
                mkv_policy=subtitles.get("mkv_policy", "bitmap_only"),
            ),
            skip=Skip(
                already_efficient=skip.get("already_efficient", True),
                efficient_kbps_1080p=skip.get("efficient_kbps_1080p", 4000.0),
                min_savings_pct=skip.get("min_savings_pct", 15.0),
            ),
            schedule=Schedule(window=schedule.get("window", "")),
            raw=copy.deepcopy(raw),
            path=path,
        )
    except Exception as exc:  # defensive: keep one bad file from hiding others
        errors.append(f"{path.name}: could not construct preset: {exc}")
        return None, errors

    return preset, errors


def _validate_string_list(
    value: Any,
    name: str,
    preset_id: str,
    errors: list[str],
    *,
    allow_empty: bool,
) -> None:
    if not isinstance(value, (tuple, list)):
        errors.append(f"preset {preset_id!r}: {name} must be a list of strings")
        return
    if not allow_empty and not value:
        errors.append(f"preset {preset_id!r}: {name} must not be empty")
        return
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(
                f"preset {preset_id!r}: {name} entries must be non-empty strings"
            )
            return


def _speed_entry_exists(preset_id: str, speed_keys: Any) -> bool:
    """Best-effort check against a speed table's keys.

    The DB may hand us a mapping, a set of tuple keys
    ``(preset, backend, bucket)`` or strings such as ``preset:qsv:1080``.
    A callable predicate is accepted too.  Unknown shapes simply mean "no
    entry", which yields only the experimental warning required by section 6.
    """

    if speed_keys is None:
        return False
    if callable(speed_keys):
        try:
            return bool(speed_keys(preset_id))
        except TypeError:  # pragma: no cover - defensive for odd callables
            return False
    if isinstance(speed_keys, str):
        candidates: Iterable[Any] = (speed_keys,)
    elif isinstance(speed_keys, Mapping):
        candidates = speed_keys.keys()
    else:
        try:
            candidates = iter(speed_keys)
        except TypeError:
            return False

    separators = (":", "/", "|", ",", "\x00")
    for key in candidates:
        if isinstance(key, str):
            if key == preset_id:
                return True
            if any(key.startswith(preset_id + separator) for separator in separators):
                return True
        elif isinstance(key, (tuple, list)):
            if key and str(key[0]) == preset_id:
                return True
        elif isinstance(key, Mapping):
            key_preset = key.get("preset") or key.get("preset_id")
            if key_preset == preset_id:
                return True
        else:
            key_preset = getattr(key, "preset", None) or getattr(
                key, "preset_id", None
            )
            if key_preset == preset_id:
                return True
    return False


def _validate_preset(
    preset: Preset,
    *,
    speed_keys: Any = (),
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    pid = preset.id

    def err(message: str) -> None:
        errors.append(f"preset {pid!r}: {message}")

    # --- identity and top level -----------------------------------------
    if not isinstance(pid, str) or not pid:
        err("id must be a non-empty string")
    elif not _PRESET_ID_RE.fullmatch(pid):
        err("id must use lowercase letters, digits, '.', '_' or '-'")
    if not isinstance(preset.description, str) or not preset.description.strip():
        err("description must be a non-empty string")
    if _not_enum(preset.container, CONTAINERS):
        err(f"container must be one of {sorted(CONTAINERS)}, got {preset.container!r}")

    # --- meta ------------------------------------------------------------
    if not _is_bool(preset.meta.experimental):
        err("meta.experimental must be a boolean")
    if not _is_bool(preset.meta.enabled):
        err("meta.enabled must be a boolean")

    # --- video -----------------------------------------------------------
    if _not_enum(preset.video.action, ACTIONS):
        err(
            f"video.action must be one of {sorted(ACTIONS)}, "
            f"got {preset.video.action!r}"
        )
    if _not_enum(preset.video.codec, VIDEO_CODECS):
        err(
            f"video.codec must be one of {sorted(VIDEO_CODECS)}, "
            f"got {preset.video.codec!r}"
        )
    if preset.video.action == "encode" and preset.video.codec != "hevc":
        err("v1 encode presets must use video.codec = 'hevc'")

    if not isinstance(preset.video.backend_order, (tuple, list)):
        err("video.backend_order must be a list")
    else:
        for backend in preset.video.backend_order:
            if not isinstance(backend, str) or backend not in BACKENDS:
                err(
                    "video.backend_order entries must be one of "
                    f"{sorted(BACKENDS)}, got {backend!r}"
                )
        if preset.video.action == "encode" and not preset.video.backend_order:
            err("video.backend_order must not be empty for an encode preset")

    if not _is_bool(preset.video.allow_software):
        err("video.allow_software must be a boolean")
    if not _is_int(preset.video.quality) or not 1 <= preset.video.quality <= 50:
        err("video.quality must be an integer from 1 to 50")
    if not _is_bool(preset.video.look_ahead):
        err("video.look_ahead must be a boolean")

    for name, value in (
        ("max_width", preset.video.max_width),
        ("max_height", preset.video.max_height),
    ):
        if not _is_int(value) or not 2 <= value <= 16384 or value % 2:
            err(f"video.{name} must be an even integer from 2 to 16384")

    if preset.video.ten_bit_source is not None and (
        not isinstance(preset.video.ten_bit_source, str)
        or preset.video.ten_bit_source not in TEN_BIT_SOURCES
    ):
        err(
            "video.ten_bit_source must be one of "
            f"{sorted(TEN_BIT_SOURCES)} or absent, "
            f"got {preset.video.ten_bit_source!r}"
        )
    if preset.video.tag is not None and (
        not isinstance(preset.video.tag, str) or preset.video.tag not in {"hvc1"}
    ):
        err("video.tag must be 'hvc1' or absent")
    if _not_enum(preset.video.hdr, HDR_MODES):
        err(f"video.hdr must be one of {sorted(HDR_MODES)}, got {preset.video.hdr!r}")
    if _not_enum(preset.video.interlaced, INTERLACED_MODES):
        err("video.interlaced must be 'review' in v1")

    # --- audio -----------------------------------------------------------
    _validate_string_list(
        preset.audio.languages, "audio.languages", pid, errors, allow_empty=False
    )
    if not _is_int(preset.audio.max_tracks) or not 1 <= preset.audio.max_tracks <= 64:
        err("audio.max_tracks must be an integer from 1 to 64")
    _validate_string_list(
        preset.audio.copy_codecs, "audio.copy_codecs", pid, errors, allow_empty=False
    )
    if not _is_bool(preset.audio.exclude_commentary):
        err("audio.exclude_commentary must be a boolean")
    if _not_enum(preset.audio.unknown_language, UNKNOWN_LANGUAGE_MODES):
        err(
            "audio.unknown_language must be one of "
            f"{sorted(UNKNOWN_LANGUAGE_MODES)}, "
            f"got {preset.audio.unknown_language!r}"
        )

    # --- subtitles -------------------------------------------------------
    _validate_string_list(
        preset.subtitles.languages,
        "subtitles.languages",
        pid,
        errors,
        allow_empty=True,
    )
    if not _is_bool(preset.subtitles.include_forced):
        err("subtitles.include_forced must be a boolean")
    if _not_enum(preset.subtitles.text, SUBTITLE_TEXT_CODECS):
        err(
            "subtitles.text must be one of "
            f"{sorted(SUBTITLE_TEXT_CODECS)}, got {preset.subtitles.text!r}"
        )
    elif preset.container == "mp4" and preset.subtitles.text != "mov_text":
        err("container 'mp4' requires subtitles.text = 'mov_text'")
    # MKV presets copy subtitles as-is (MKV-1); the `text` field is retained in
    # the shared schema so MKV files can use either value without a false
    # validation failure, but the planner ignores it for MKV.
    if _not_enum(preset.subtitles.bitmap, BITMAP_POLICIES):
        err(
            "subtitles.bitmap must be one of "
            f"{sorted(BITMAP_POLICIES)}, got {preset.subtitles.bitmap!r}"
        )
    if _not_enum(preset.subtitles.mkv_policy, MKV_POLICIES):
        err(
            "subtitles.mkv_policy must be one of "
            f"{sorted(MKV_POLICIES)}, got {preset.subtitles.mkv_policy!r}"
        )

    # --- skip ------------------------------------------------------------
    if not _is_bool(preset.skip.already_efficient):
        err("skip.already_efficient must be a boolean")
    if not _is_number(preset.skip.efficient_kbps_1080p) or (
        preset.skip.efficient_kbps_1080p < 0
    ):
        err("skip.efficient_kbps_1080p must be a non-negative number")
    if not _is_number(preset.skip.min_savings_pct) or not (
        0 <= preset.skip.min_savings_pct <= 90
    ):
        err("skip.min_savings_pct must be a number from 0 to 90")

    # --- schedule --------------------------------------------------------
    if not isinstance(preset.schedule.window, str):
        err("schedule.window must be a string")
    else:
        try:
            parse_window(preset.schedule.window)
        except ValueError as exc:
            err(str(exc))

    # --- section 6 cross-field matrix -----------------------------------
    if (
        preset.subtitles.bitmap == "mkv_fallback"
        and preset.subtitles.mkv_policy == "never"
    ):
        err(
            "subtitles.bitmap = 'mkv_fallback' conflicts with "
            "subtitles.mkv_policy = 'never' (circular policy)"
        )
    if preset.container == "mkv" and preset.video.action == "encode":
        err("container 'mkv' cannot use action 'encode' (MKV presets are remux-only in v1)")
    if (
        preset.container == "mp4"
        and isinstance(preset.audio.copy_codecs, (tuple, list))
        and "*" in preset.audio.copy_codecs
    ):
        err("container 'mp4' cannot use copy_codecs = ['*']")
    if (
        preset.video.hdr == "keep"
        and preset.video.action == "encode"
        and preset.video.ten_bit_source != "keep"
    ):
        err(
            "video.hdr = 'keep' on an encode preset requires "
            "video.ten_bit_source = 'keep'"
        )
    if preset.video.hdr == "keep" and preset.video.ten_bit_source == "to8":
        err("video.ten_bit_source = 'to8' conflicts with video.hdr = 'keep'")
    if preset.video.action == "remux" and preset.video.ten_bit_source is not None:
        err(
            "video.ten_bit_source must be absent on a remux preset "
            "(it would be ignored)"
        )
    if preset.video.allow_software and (
        not isinstance(preset.schedule.window, str)
        or not preset.schedule.window.strip()
    ):
        err("video.allow_software = true requires a non-empty [schedule].window")

    # WARNING, not an error: the speed table is in the DB and is not visible
    # to this loader unless speed_keys is supplied.
    if (
        preset.meta.experimental is True
        and preset.meta.enabled is True
        and not _speed_entry_exists(pid, speed_keys)
    ):
        warnings.append(
            f"preset {pid!r}: experimental preset is enabled without a "
            "speed-table entry; calibrate it before enabling, or runs with a "
            "schedule window will not auto-start"
        )
    return errors, warnings


def validate_presets(
    presets: Mapping[str, Preset] | Iterable[Preset],
    speed_keys: Any = (),
) -> ValidationResult:
    """Validate a collection, returning all errors and warnings.

    This is usable without touching the filesystem, which is important for the
    speed-table warning: the loader can only know speed keys that the caller
    passes in.
    """

    errors: list[str] = []
    warnings: list[str] = []

    if isinstance(presets, Mapping):
        items: Iterable[tuple[str, Any]] = presets.items()
    else:
        items = ((getattr(p, "id", "<unknown>"), p) for p in presets)

    for mapping_id, preset in items:
        if not isinstance(preset, Preset):
            errors.append(f"preset {mapping_id!r}: value is not a Preset")
            continue
        if mapping_id != preset.id:
            errors.append(
                f"preset mapping key {mapping_id!r} does not match id {preset.id!r}"
            )
        preset_errors, preset_warnings = _validate_preset(
            preset, speed_keys=speed_keys
        )
        errors.extend(preset_errors)
        warnings.extend(preset_warnings)

    return ValidationResult(tuple(errors), tuple(warnings))


def load_preset_file(
    path: Path,
    *,
    speed_keys: Any = (),
    warnings: list[str] | None = None,
) -> Preset:
    """Load exactly one preset file (used by install/doctor/tests)."""

    path = Path(path)
    if not path.is_file():
        raise PresetError(f"preset file not found: {path}")
    preset, structural_errors = _parse_preset_file(path)
    errors = list(structural_errors)
    collected_warnings: list[str] = []
    if preset is not None:
        preset_errors, preset_warnings = _validate_preset(
            preset, speed_keys=speed_keys
        )
        errors.extend(preset_errors)
        collected_warnings.extend(preset_warnings)
    if warnings is not None:
        warnings.extend(collected_warnings)
    if errors:
        raise PresetError(errors)
    assert preset is not None
    return preset


def _preset_files(
    directory: Path,
    *,
    warn: Callable[[str], None],
) -> list[Path]:
    """Return regular ``*.toml`` files, ignoring (and reporting) symlinks."""

    files: list[Path] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            warn(f"ignoring symlink in preset folder: {entry}")
            continue
        if entry.is_file() and entry.suffix == ".toml":
            files.append(entry)
    return files


def load_presets(
    presets_dir: Path,
    *,
    experimental_dir: Path | None = None,
    include_experimental: bool = False,
    speed_keys: Any = (),
    warnings: list[str] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> PresetMap:
    """Load ``presets_dir/*.toml`` (non-recursive by default).

    The experimental folder is a separate data set and is only read when
    ``include_experimental`` is true.  A missing experimental folder is a
    warning, not an error, because ``install.sh`` never creates it.  Disabled
    presets are returned with ``enabled = False``; callers decide whether to
    ignore them.  ``PresetMap.warnings`` always carries every warning.
    """

    presets_dir = Path(presets_dir)
    if not presets_dir.exists():
        raise PresetError(f"preset directory not found: {presets_dir}")
    if not presets_dir.is_dir():
        raise PresetError(f"preset path is not a directory: {presets_dir}")

    collected_warnings: list[str] = []
    if warnings is not None:
        collected_warnings = warnings  # emit into the caller's list
    else:
        collected_warnings = []

    def warn(message: str) -> None:
        collected_warnings.append(message)
        if on_warning is not None:
            on_warning(message)

    errors: list[str] = []
    found: list[tuple[Path, Preset]] = []

    def load_directory(directory: Path) -> None:
        try:
            files = _preset_files(directory, warn=warn)
        except OSError as exc:
            reason = exc.strerror or "read error"
            errors.append(f"cannot list preset directory {directory}: {reason}")
            return
        for preset_path in files:
            preset, structural_errors = _parse_preset_file(preset_path)
            if structural_errors:
                errors.extend(structural_errors)
            if preset is not None:
                found.append((preset_path, preset))

    load_directory(presets_dir)

    if include_experimental:
        exp_dir = (
            Path(experimental_dir)
            if experimental_dir is not None
            else presets_dir / "experimental"
        )
        if exp_dir.is_symlink():
            warn(f"ignoring symlink in preset folder: {exp_dir}")
        elif exp_dir.exists():
            if not exp_dir.is_dir():
                errors.append(f"experimental preset path is not a directory: {exp_dir}")
            else:
                load_directory(exp_dir)
        else:
            warn(f"experimental preset directory not present (skipped): {exp_dir}")

    # Detect duplicate ids across the base and experimental sets before
    # validating, so the message can name both files.
    seen: dict[str, Path] = {}
    for preset_path, preset in found:
        previous = seen.get(preset.id)
        if previous is not None:
            errors.append(
                f"duplicate preset id {preset.id!r}: {previous} and {preset_path}"
            )
        else:
            seen[preset.id] = preset_path

    for _preset_path, preset in found:
        preset_errors, preset_warnings = _validate_preset(
            preset, speed_keys=speed_keys
        )
        errors.extend(preset_errors)
        for message in preset_warnings:
            warn(message)

    if errors:
        raise PresetError(errors)

    presets = {preset.id: preset for preset in (p for _path, p in found)}
    return PresetMap(presets, warnings=collected_warnings)


def get_preset(
    presets: Mapping[str, Preset],
    preset_id: str,
) -> Preset:
    """Look up a preset, raising a clear ``PresetError`` for unknown ids."""

    try:
        return presets[preset_id]
    except KeyError:
        known = ", ".join(sorted(presets)) or "<none>"
        raise PresetError(
            f"unknown preset id {preset_id!r}; known presets: {known}"
        ) from None
