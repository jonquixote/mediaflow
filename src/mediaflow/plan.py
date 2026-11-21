"""Planning: turn a probe result and a preset into the internal plan contract.

The plan is data only.  It never builds ffmpeg arguments and never uses
``-map 0`` (MP4-12 is the encoder's job); it records absolute input stream
indexes and explicit output dimensions for the encoder and verifier.

Requirement coverage in this module: MP4-2, MP4-3, MP4-4, MP4-5, MP4-6,
MP4-7, MP4-8, MP4-9 (plan flags), MP4-10, MP4-11, MKV-1, SIZE-1, SIZE-2
(fallback shape), SIZE-4, SIZE-5, SCHED-1 and SAF-13 (selection only).

Public API
----------
``normalise_language(tag)``
    Fixed ISO 639-1/2 alias table; unknown/``und`` becomes ``und``.
``compute_dimensions(width, height, sar, max_width, max_height)``
    Return ``(target_width, target_height, scaled)`` with even dimensions,
    never upscaling and never exceeding either cap.
``efficient_threshold_kbps(base, width, height)``
    ``base * (pixels / 2073600) ** 0.75``.
``select_audio`` / ``select_subtitles``
    Pure selection helpers used by :func:`build_plan` and by SIZE-2 replans.
``audio_choice``
    Build one PLAN_SCHEMA audio entry for a stream.
``hdr_flags`` / ``is_10bit`` / ``is_interlaced``
    Re-exported from :mod:`mediaflow.probe` for the documented planner API.
``schedule_window`` / ``estimate_fits_window``
    Pure SCHED-1 helpers.
``build_plan``
    Return a dict with exactly the keys in ``docs/PLAN_SCHEMA.md``.

Preset input is the raw TOML mapping (as returned by the preset loader) with
keys ``id``, ``container`` and ``[video]``/``[audio]``/``[subtitles]``/
``[skip]``/``[schedule]`` sections.  A loaded ``Preset`` dataclass is also
accepted; its ``raw`` mapping (or its section dataclasses) is used.  The
planner intentionally tolerates missing sub-keys so old raw mappings and unit
tests remain usable, but it never guesses a review decision.

Extra optional keyword: ``fallback_from`` records ``"encode"`` for a SIZE-2
remux replan.  When omitted it is inferred for ``plan_version > 1`` remuxes.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .probe import (
    ProbeResult,
    UnsupportedFormat,
    container_allowed,
    find_sidecars,
    hdr_flags,
    is_10bit,
    is_interlaced,
)

__all__ = [
    "MP4_AUDIO_COPY_CODECS",
    "MP4_TEXT_SUBTITLE_CODECS",
    "BITMAP_SUBTITLE_CODECS",
    "PlanError",
    "ReviewRequired",
    "audio_choice",
    "build_plan",
    "compute_dimensions",
    "efficient_threshold_kbps",
    "estimate_fits_window",
    "hdr_flags",
    "is_10bit",
    "is_interlaced",
    "normalise_language",
    "schedule_window",
    "select_audio",
    "select_subtitles",
]

DEFAULT_MAX_WIDTH = 1920
DEFAULT_MAX_HEIGHT = 1080
DEFAULT_QUALITY = 24
DEFAULT_EFFICIENT_KBPS_1080P = 4000.0
REFERENCE_PIXELS_1080P = 1920 * 1080

MP4_AUDIO_COPY_CODECS: frozenset[str] = frozenset({"aac", "ac3", "eac3"})
MP4_TEXT_SUBTITLE_CODECS: frozenset[str] = frozenset(
    {"subrip", "ass", "ssa", "webvtt", "mov_text"}
)
BITMAP_SUBTITLE_CODECS: frozenset[str] = frozenset(
    {
        "hdmv_pgs_subtitle",
        "pgs",
        "pgssub",
        "dvd_subtitle",
        "dvdsub",
        "vobsub",
        "dvb_subtitle",
        "dvb_teletext",
        "xsub",
    }
)

_DEFAULT_AUDIO_CONFIG: dict[str, Any] = {
    "languages": ("original", "eng"),
    "max_tracks": 3,
    "copy_codecs": ("aac", "ac3", "eac3"),
    "exclude_commentary": True,
    "unknown_language": "keep_first",
}

_DEFAULT_SUBTITLE_CONFIG: dict[str, Any] = {
    "languages": ("eng",),
    "include_forced": True,
    "text": "mov_text",
    "bitmap": "mkv_fallback",
    "mkv_policy": "bitmap_only",
}

_DEFAULT_SKIP_CONFIG: dict[str, Any] = {
    "already_efficient": True,
    "efficient_kbps_1080p": DEFAULT_EFFICIENT_KBPS_1080P,
}

_SOFTWARE_ASSUMED_10BIT = True


class PlanError(ValueError):
    """The preset/probe cannot produce a valid plan."""


class ReviewRequired(PlanError):
    """A human decision is required before continuing.

    ``reason`` is the stable review-table key, ``detail`` is optional context
    and ``from_state`` is where clearing the review resumes (REV-1).  The
    planner raises this instead of returning a plan with ``review`` set.
    """

    def __init__(
        self,
        reason: str,
        detail: str | None = None,
        from_state: str = "PLANNED",
    ) -> None:
        self.reason = str(reason)
        self.detail = detail
        self.from_state = from_state
        message = self.reason if detail is None else f"{self.reason}: {detail}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# language normalisation


_LANGUAGE_ALIASES: dict[str, str] = {
    # English + common European languages
    "en": "eng", "eng": "eng",
    "de": "deu", "ger": "deu", "deu": "deu",
    "fr": "fra", "fre": "fra", "fra": "fra",
    "es": "spa", "spa": "spa",
    "it": "ita", "ita": "ita",
    "ja": "jpn", "jpn": "jpn",
    "nl": "nld", "dut": "nld", "nld": "nld",
    "pt": "por", "por": "por",
    "ru": "rus", "rus": "rus",
    "pl": "pol", "pol": "pol",
    "sv": "swe", "swe": "swe",
    "no": "nor", "nor": "nor", "nb": "nob", "nob": "nob", "nn": "nno", "nno": "nno",
    "da": "dan", "dan": "dan",
    "fi": "fin", "fin": "fin",
    "is": "isl", "ice": "isl", "isl": "isl",
    "cs": "ces", "cze": "ces", "ces": "ces",
    "sk": "slk", "slo": "slk", "slk": "slk",
    "hu": "hun", "hun": "hun",
    "ro": "ron", "rum": "ron", "ron": "ron",
    "el": "ell", "gre": "ell", "ell": "ell",
    "tr": "tur", "tur": "tur",
    "he": "heb", "heb": "heb",
    "ar": "ara", "ara": "ara",
    "fa": "fas", "per": "fas", "fas": "fas",
    "hi": "hin", "hin": "hin",
    "bn": "ben", "ben": "ben",
    "ta": "tam", "tam": "tam",
    "te": "tel", "tel": "tel",
    "ml": "mal", "mal": "mal",
    "kn": "kan", "kan": "kan",
    "mr": "mar", "mar": "mar",
    "gu": "guj", "guj": "guj",
    "pa": "pan", "pan": "pan",
    "ur": "urd", "urd": "urd",
    "th": "tha", "tha": "tha",
    "vi": "vie", "vie": "vie",
    "id": "ind", "ind": "ind",
    "ms": "msa", "may": "msa", "msa": "msa",
    "tl": "tgl", "tgl": "tgl", "fil": "fil",
    "ko": "kor", "kor": "kor",
    "zh": "zho", "chi": "zho", "zho": "zho",
    "yue": "yue",
    "uk": "ukr", "ukr": "ukr",
    "be": "bel", "bel": "bel",
    "bg": "bul", "bul": "bul",
    "hr": "hrv", "hrv": "hrv",
    "sr": "srp", "srp": "srp",
    "sl": "slv", "slv": "slv",
    "lt": "lit", "lit": "lit",
    "lv": "lav", "lav": "lav",
    "et": "est", "est": "est",
    "ca": "cat", "cat": "cat",
    "gl": "glg", "glg": "glg",
    "eu": "eus", "baq": "eus", "eus": "eus",
    "cy": "cym", "wel": "cym", "cym": "cym",
    "ga": "gle", "gle": "gle",
    "gd": "gla", "gla": "gla",
    "mt": "mlt", "mlt": "mlt",
    "sq": "sqi", "alb": "sqi", "sqi": "sqi",
    "mk": "mkd", "mac": "mkd", "mkd": "mkd",
    "bs": "bos", "bos": "bos",
    "hy": "hye", "arm": "hye", "hye": "hye",
    "ka": "kat", "geo": "kat", "kat": "kat",
    "az": "aze", "aze": "aze",
    "kk": "kaz", "kaz": "kaz",
    "uz": "uzb", "uzb": "uzb",
    "mn": "mon", "mon": "mon",
    "ne": "nep", "nep": "nep",
    "si": "sin", "sin": "sin",
    "km": "khm", "khm": "khm",
    "lo": "lao", "lao": "lao",
    "my": "mya", "bur": "mya", "mya": "mya",
    "bo": "bod", "tib": "bod", "bod": "bod",
    "ps": "pus", "pus": "pus",
    "ku": "kur", "kur": "kur",
    "yi": "yid", "yid": "yid",
    # Remaining ISO 639-1 languages seen in media tags
    "aa": "aar", "aar": "aar",
    "ab": "abk", "abk": "abk",
    "af": "afr", "afr": "afr",
    "ak": "aka", "aka": "aka",
    "am": "amh", "amh": "amh",
    "an": "arg", "arg": "arg",
    "as": "asm", "asm": "asm",
    "av": "ava", "ava": "ava",
    "ay": "aym", "aym": "aym",
    "ba": "bak", "bak": "bak",
    "bm": "bam", "bam": "bam",
    "br": "bre", "bre": "bre",
    "ce": "che", "che": "che",
    "co": "cos", "cos": "cos",
    "cr": "cre", "cre": "cre",
    "cu": "chu", "chu": "chu",
    "cv": "chv", "chv": "chv",
    "dv": "div", "div": "div",
    "dz": "dzo", "dzo": "dzo",
    "ee": "ewe", "ewe": "ewe",
    "eo": "epo", "epo": "epo",
    "ff": "ful", "ful": "ful",
    "fj": "fij", "fij": "fij",
    "fo": "fao", "fao": "fao",
    "fy": "fry", "fry": "fry",
    "gn": "grn", "grn": "grn",
    "gv": "glv", "glv": "glv",
    "ha": "hau", "hau": "hau",
    "ho": "hmo", "hmo": "hmo",
    "ht": "hat", "hat": "hat",
    "hz": "her", "her": "her",
    "ia": "ina", "ina": "ina",
    "ie": "ile", "ile": "ile",
    "ig": "ibo", "ibo": "ibo",
    "ii": "iii", "iii": "iii",
    "ik": "ipk", "ipk": "ipk",
    "io": "ido", "ido": "ido",
    "iu": "iku", "iku": "iku",
    "jv": "jav", "jav": "jav",
    "kg": "kon", "kon": "kon",
    "ki": "kik", "kik": "kik",
    "kj": "kua", "kua": "kua",
    "kl": "kal", "kal": "kal",
    "kr": "kau", "kau": "kau",
    "ks": "kas", "kas": "kas",
    "kv": "kom", "kom": "kom",
    "kw": "cor", "cor": "cor",
    "ky": "kir", "kir": "kir",
    "la": "lat", "lat": "lat",
    "lb": "ltz", "ltz": "ltz",
    "lg": "lug", "lug": "lug",
    "li": "lim", "lim": "lim",
    "ln": "lin", "lin": "lin",
    "lu": "lub", "lub": "lub",
    "mg": "mlg", "mlg": "mlg",
    "mh": "mah", "mah": "mah",
    "mi": "mri", "mao": "mri", "mri": "mri",
    "na": "nau", "nau": "nau",
    "nd": "nde", "nde": "nde",
    "ng": "ndo", "ndo": "ndo",
    "nr": "nbl", "nbl": "nbl",
    "nv": "nav", "nav": "nav",
    "ny": "nya", "nya": "nya",
    "oc": "oci", "oci": "oci",
    "oj": "oji", "oji": "oji",
    "om": "orm", "orm": "orm",
    "or": "ori", "ori": "ori",
    "os": "oss", "oss": "oss",
    "pi": "pli", "pli": "pli",
    "qu": "que", "que": "que",
    "rm": "roh", "roh": "roh",
    "rn": "run", "run": "run",
    "rw": "kin", "kin": "kin",
    "sa": "san", "san": "san",
    "sc": "srd", "srd": "srd",
    "sd": "snd", "snd": "snd",
    "se": "sme", "sme": "sme",
    "sg": "sag", "sag": "sag",
    "sm": "smo", "smo": "smo",
    "sn": "sna", "sna": "sna",
    "so": "som", "som": "som",
    "ss": "ssw", "ssw": "ssw",
    "st": "sot", "sot": "sot",
    "su": "sun", "sun": "sun",
    "sw": "swa", "swa": "swa",
    "tg": "tgk", "tgk": "tgk",
    "ti": "tir", "tir": "tir",
    "tk": "tuk", "tuk": "tuk",
    "tn": "tsn", "tsn": "tsn",
    "to": "ton", "ton": "ton",
    "ts": "tso", "tso": "tso",
    "tt": "tat", "tat": "tat",
    "tw": "twi", "twi": "twi",
    "ug": "uig", "uig": "uig",
    "ve": "ven", "ven": "ven",
    "vo": "vol", "vol": "vol",
    "wa": "wln", "wln": "wln",
    "wo": "wol", "wol": "wol",
    "xh": "xho", "xho": "xho",
    "yo": "yor", "yor": "yor",
    "za": "zha", "zha": "zha",
    "zu": "zul", "zul": "zul",
    "ast": "ast",
    "gsw": "gsw",
    "hbs": "hbs",
    "sco": "sco",
    # Common English language names, useful for hand-made sidecars
    "english": "eng", "german": "deu", "french": "fra", "spanish": "spa",
    "italian": "ita", "japanese": "jpn", "dutch": "nld", "portuguese": "por",
    "russian": "rus", "polish": "pol", "swedish": "swe", "danish": "dan",
    "finnish": "fin", "norwegian": "nor", "czech": "ces", "greek": "ell",
    "turkish": "tur", "hebrew": "heb", "arabic": "ara", "hindi": "hin",
    "chinese": "zho", "korean": "kor", "thai": "tha", "vietnamese": "vie",
    "ukrainian": "ukr", "hungarian": "hun", "romanian": "ron",
}

_UNKNOWN_LANGUAGE_TAGS = frozenset(
    {
        "",
        "und",
        "unk",
        "unknown",
        "none",
        "null",
        "n/a",
        "na",
        "???",
        "mul",
        "zxx",
        "qaa",
        "mis",
    }
)


def normalise_language(tag: Any) -> str:
    """Return a lower-case ISO 639-2 code, or ``und`` when unknown.

    The primary subtag is used, so ``en-US`` and ``pt_BR`` normalise to
    ``eng`` and ``por``.  Bibliographic and terminological aliases such as
    ``ger``/``deu`` converge on one code.  Unknown, empty and ``und`` tags
    become ``und`` so callers can apply their unknown_language policy.
    """
    if tag is None or isinstance(tag, bool):
        return "und"
    text = unicodedata.normalize("NFC", str(tag)).strip().lower()
    if text in _UNKNOWN_LANGUAGE_TAGS:
        return "und"
    primary = re.split(r"[-_]", text, maxsplit=1)[0].strip()
    if not primary or primary in _UNKNOWN_LANGUAGE_TAGS:
        return "und"
    return _LANGUAGE_ALIASES.get(primary, "und")


# ---------------------------------------------------------------------------
# dimensions and efficiency


def _parse_ratio(value: Any) -> tuple[float, float]:
    """Parse ``a:b``/``a/b``/a single number; invalid or zero => 1:1."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            numerator, denominator = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return 1.0, 1.0
    else:
        text = str(value or "").strip()
        if not text or text.upper() in {"N/A", "NA", "0:1", "0/1", "UNKNOWN"}:
            return 1.0, 1.0
        separator = ":" if ":" in text else ("/" if "/" in text else None)
        if separator:
            left, _, right = text.partition(separator)
            try:
                numerator, denominator = float(left), float(right)
            except ValueError:
                return 1.0, 1.0
        else:
            try:
                numerator, denominator = float(text), 1.0
            except ValueError:
                return 1.0, 1.0
    if (
        numerator <= 0
        or denominator <= 0
        or not math.isfinite(numerator)
        or not math.isfinite(denominator)
    ):
        return 1.0, 1.0
    return numerator, denominator


def _even_floor(value: float) -> int:
    number = int(value) // 2 * 2
    return max(2, number)


def compute_dimensions(
    width: int,
    height: int,
    sar: Any,
    max_width: int,
    max_height: int,
) -> tuple[int, int, bool]:
    """Fit a source frame inside a cap box without upscaling (SIZE-5).

    The source sample aspect ratio is used for the display width: a 1440x1080
    stream tagged 4:3 displays as 1920x1080 and is not needlessly downscaled.
    The returned frame keeps the source storage aspect (and therefore its SAR)
    and is floored to even numbers.  Both caps are hard limits.  A frame that
    already fits has its dimensions preserved except for odd-to-even flooring.

    Examples (all caps even and >= 2)::

        compute_dimensions(1920, 800, "1:1", 1920, 1080) == (1920, 800, False)
        compute_dimensions(1920, 804, "1:1", 1920, 1080) == (1920, 804, False)
        compute_dimensions(1440, 1080, "1:1", 1920, 1080) == (1440, 1080, False)
        compute_dimensions(3840, 2160, "1:1", 1920, 1080) == (1920, 1080, True)

    The 2.39:1 widescreen case is represented by its stored frame (for
    example 1920x804), which fits a 1920x1080 cap unchanged.
    """
    try:
        source_width = int(width)
        source_height = int(height)
        cap_width = int(max_width)
        cap_height = int(max_height)
    except (TypeError, ValueError) as exc:
        raise PlanError("dimensions must be integers") from exc
    if source_width <= 0 or source_height <= 0:
        raise PlanError("source dimensions must be positive")
    if cap_width < 2 or cap_height < 2:
        raise PlanError("dimension caps must be at least 2x2")

    sar_x, sar_y = _parse_ratio(sar)
    display_width = source_width * sar_x / sar_y

    scale = 1.0
    # The display-width cap protects anamorphic sources; the storage caps are
    # absolute and keep the returned frame within both limits.
    if display_width > cap_width:
        scale = min(scale, cap_width / display_width)
    if source_width > cap_width:
        scale = min(scale, cap_width / source_width)
    if source_height > cap_height:
        scale = min(scale, cap_height / source_height)

    if scale >= 1.0:
        target_w, target_h = source_width, source_height
    else:
        target_w = _even_floor(source_width * scale)
        target_h = _even_floor(source_height * scale)
    target_w = _even_floor(target_w)
    target_h = _even_floor(target_h)
    # Even flooring cannot exceed the caps, but guard against pathological
    # floating point/rounding input anyway.
    target_w = min(target_w, _even_floor(cap_width))
    target_h = min(target_h, _even_floor(cap_height))
    scaled = (target_w, target_h) != (source_width, source_height)
    return target_w, target_h, scaled


def efficient_threshold_kbps(
    efficient_kbps_1080p: float,
    width: int,
    height: int,
) -> float:
    """Pixel-scaled already-efficient threshold (SIZE-1).

    ``efficient_kbps_1080p * (pixels / 2073600) ** 0.75``.  With 4000 this is
    about 2178 kbps at 1280x720 and about 11314 kbps at 3840x2160.
    """
    base = float(efficient_kbps_1080p)
    try:
        pixels = int(width) * int(height)
    except (TypeError, ValueError) as exc:
        raise PlanError("width and height must be integers") from exc
    if pixels <= 0:
        return 0.0
    return base * (pixels / REFERENCE_PIXELS_1080P) ** 0.75


# ---------------------------------------------------------------------------
# small mapping/value helpers


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Read the first present key from a mapping or attribute-style object."""
    for key in keys:
        if isinstance(obj, Mapping):
            if key in obj and obj[key] is not None:
                return obj[key]
        elif hasattr(obj, key):
            value = getattr(obj, key)
            if value is not None and not callable(value):
                return value
    return default


def _section(obj: Any, name: str) -> Mapping[str, Any]:
    value = _get(obj, name, default={})
    return value if isinstance(value, Mapping) else {}


def _object_mapping(value: Any) -> Mapping[str, Any]:
    """Accept mappings and simple attribute/dataclass objects for config/caps."""
    if isinstance(value, Mapping):
        return value
    if value is None:
        return {}
    if is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in dataclass_fields(value)}
    namespace = getattr(value, "__dict__", None)
    if isinstance(namespace, Mapping):
        return {key: item for key, item in namespace.items() if not key.startswith("_")}
    return {}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _xml_number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# stream helpers


def _stream_index(stream: Mapping[str, Any], fallback: int = 0) -> int:
    value = _int(stream.get("index"), fallback)
    return max(0, value)


def _stream_language(stream: Mapping[str, Any]) -> str:
    tags = _mapping(stream.get("tags"))
    return normalise_language(stream.get("language") or tags.get("language"))


def _stream_title(stream: Mapping[str, Any]) -> str | None:
    tags = _mapping(stream.get("tags"))
    for value in (tags.get("title"), stream.get("title"), tags.get("handler_name")):
        if isinstance(value, str) and value.strip() and value.strip() != "?":
            return value.strip()
    return None


def _stream_channels(stream: Mapping[str, Any]) -> int:
    return _int(stream.get("channels"), 0)


def _channel_layout(stream: Mapping[str, Any]) -> str | None:
    """The source channel layout, when ffprobe reports a usable one.

    ffprobe reports ``channel_layout`` as a string ("5.1"); newer builds may
    expose the structured ``ch_layout`` object instead.  Unknown layouts
    return ``None`` so the verifier never invents a layout requirement.
    """
    value: Any = stream.get("channel_layout")
    if value is None:
        value = stream.get("ch_layout")
    if isinstance(value, Mapping):
        value = value.get("description") or value.get("name")
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "unspecified", "none", "n/a", ""}:
        return None
    return text


def _disposition(stream: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(stream.get("disposition"))


def _is_default(stream: Mapping[str, Any]) -> bool:
    return _bool(_disposition(stream).get("default"))


def _is_forced(stream: Mapping[str, Any]) -> bool:
    return _bool(_disposition(stream).get("forced"))


def _is_commentary(stream: Mapping[str, Any]) -> bool:
    disposition = _disposition(stream)
    if any(
        _bool(disposition.get(key))
        for key in ("comment", "descriptions", "visual_impaired")
    ):
        return True
    title = " ".join(
        filter(
            None,
            [
                _stream_title(stream),
                _mapping(stream.get("tags")).get("handler_name"),
                stream.get("handler_name"),
            ],
        )
    ).casefold()
    if not title:
        return False
    return any(
        marker in title
        for marker in (
            "commentary",
            "director",
            "audio description",
            "descriptive",
            "description",
        )
    )


def _tag_number(stream: Mapping[str, Any], prefix: str) -> int | None:
    """Read Matroska BPS/NUMBER_OF_BYTES tags, including ``BPS-eng``."""
    tags = _mapping(stream.get("tags"))
    prefix = prefix.upper()
    for key, value in tags.items():
        key_text = str(key).upper()
        if key_text == prefix or key_text.startswith(prefix + "-"):
            number = _xml_number(value)
            if number is not None and number > 0:
                return number
    return None


def _source_video_bytes(
    probe: ProbeResult,
    stream: Mapping[str, Any],
    supplied: Any,
) -> int:
    supplied_int = _int(supplied, 0)
    if supplied_int > 0:
        return supplied_int
    size = _int(stream.get("size"), 0)
    if size > 0:
        return size
    duration = float(probe.duration or 0)
    tag_bytes = _tag_number(stream, "NUMBER_OF_BYTES")
    if tag_bytes:
        return tag_bytes
    bit_rate = _float(stream.get("bit_rate"), 0.0)
    if bit_rate > 0 and duration > 0:
        return int(bit_rate * duration / 8)
    tag_bps = _tag_number(stream, "BPS")
    if tag_bps and duration > 0:
        return int(tag_bps * duration / 8)
    return 0


def _video_kbps(
    stream: Mapping[str, Any],
    video_bytes: int,
    duration: float,
) -> float | None:
    if video_bytes > 0 and duration > 0:
        return video_bytes * 8.0 / duration / 1000.0
    bit_rate = _float(stream.get("bit_rate"), 0.0)
    if bit_rate > 0:
        return bit_rate / 1000.0
    tag_bps = _tag_number(stream, "BPS")
    if tag_bps:
        return tag_bps / 1000.0
    tag_bytes = _tag_number(stream, "NUMBER_OF_BYTES")
    if tag_bytes and duration > 0:
        return tag_bytes * 8.0 / duration / 1000.0
    return None


def _video_codec(stream: Mapping[str, Any]) -> str:
    codec = str(stream.get("codec_name") or stream.get("codec") or "").strip().lower()
    aliases = {
        "h265": "hevc",
        "h.265": "hevc",
        "hvc1": "hevc",
        "hev1": "hevc",
        "h264": "h264",
        "h.264": "h264",
        "avc": "h264",
        "avc1": "h264",
    }
    return aliases.get(codec, codec)


def _audio_codec_key(stream: Mapping[str, Any]) -> str:
    raw = str(stream.get("codec_name") or stream.get("codec") or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]", "", raw)
    aliases = {
        "eac3": "eac3",
        "ec3": "eac3",
        "ac3": "ac3",
        "aac": "aac",
        "dts": "dts",
        "dtshd": "dts",
        "dtshires": "dts",
        "truehd": "truehd",
        "flac": "flac",
        "opus": "opus",
        "vorbis": "vorbis",
        "mp3": "mp3",
    }
    return aliases.get(compact, raw)


def _bit_depth(stream: Mapping[str, Any]) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = _int(stream.get(key), 0)
        if value >= 8:
            return value
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    for depth in (16, 14, 12, 10, 9):
        token = str(depth)
        if f"p{depth:02d}" in pix_fmt or f"p{depth}le" in pix_fmt or f"p{depth}be" in pix_fmt:
            return depth
        if token + "le" in pix_fmt or token + "be" in pix_fmt:
            return depth
    return 8


def _to_8bit_pix_fmt(stream: Mapping[str, Any]) -> str:
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    mapping = {
        "yuv420p10le": "yuv420p",
        "yuv420p10be": "yuv420p",
        "yuv422p10le": "yuv422p",
        "yuv422p10be": "yuv422p",
        "yuv444p10le": "yuv444p",
        "yuv444p10be": "yuv444p",
        "yuv420p12le": "yuv420p",
        "p010le": "yuv420p",
        "p010be": "yuv420p",
        "p016le": "yuv420p",
        "p016be": "yuv420p",
        "gbrp10le": "gbrp",
        "gbrp12le": "gbrp",
        "gray10le": "gray",
        "gray12le": "gray",
    }
    if pix_fmt in mapping:
        return mapping[pix_fmt]
    # A conservative generic fallback keeps standard 4:2:0 output.
    return "yuv420p"


def _sar(stream: Mapping[str, Any]) -> str:
    value = str(stream.get("sample_aspect_ratio") or "").strip()
    if not value or value.upper() in {"N/A", "0:1", "0/1", "UNKNOWN"}:
        return "1:1"
    return value


def _frame_rate(stream: Mapping[str, Any]) -> str:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = str(stream.get(key) or "").strip()
        if value and value not in {"0/0", "0", "N/A"}:
            return value
    return "0/0"


#: Values that mean "this property is not stated by the file".
UNKNOWN_COLOUR_VALUES = frozenset(
    {"", "unknown", "unspecified", "und", "n/a", "none", "reserved"}
)


def _known_colour_value(value: Any) -> Any:
    """Return *value* when the file states it, otherwise ``None``.

    ffprobe reports placeholder strings for properties the container does
    not carry.  Those are not part of the output contract: mediaflow never
    invents a colour tag.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in UNKNOWN_COLOUR_VALUES:
        return None
    return text


def _colour(stream: Mapping[str, Any]) -> dict[str, Any]:
    """The colour properties the source *states*.

    ffprobe infers ``color_range=tv`` for ordinary 8-bit 4:2:0 pixel
    formats even when the stream carries no range tag at all.  Such an
    inferred value is not a statement by the file, so it is not put into
    the plan: the plan is the output contract and it may only carry
    colour metadata the source actually declares.

    A range is therefore kept when

    * it is ``pc`` (full range) - ffprobe never infers that for the
      standard pixel formats, so it is stated by the source; or
    * the source also states a colour matrix, in which case the range
      belongs to a complete colour description (an MP4 ``nclx`` box
      always carries the full-range flag next to the matrix).
    """
    primaries = _known_colour_value(stream.get("color_primaries"))
    transfer = _known_colour_value(stream.get("color_transfer"))
    space = _known_colour_value(stream.get("color_space"))
    raw_range = _known_colour_value(stream.get("color_range"))
    if raw_range is None:
        colour_range: Any = None
    elif raw_range.lower() == "pc" or space is not None:
        colour_range = raw_range
    else:
        # ffprobe inferred the limited range from the pixel format.
        colour_range = None
    return {
        "primaries": primaries,
        "transfer": transfer,
        "space": space,
        "range": colour_range,
    }


def _container_family(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    tokens = {token.strip() for token in text.split(",") if token.strip()}
    if tokens & {"matroska", "webm"}:
        return "mkv"
    if tokens & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}:
        return "mp4"
    if "avi" in tokens:
        return "avi"
    if tokens & {"mpegts", "mpeg"}:
        return "mpeg"
    if "asf" in tokens:
        return "asf"
    return None


def _normalise_container(value: Any, default: str = "mp4") -> str:
    if value is None or str(value).strip() == "":
        return default
    family = _container_family(value)
    text = str(value).strip().lower()
    if text in {"mkv", "matroska", "webm"} or family == "mkv":
        return "mkv"
    if text in {"mp4", "m4v", "mov"} or family == "mp4":
        return "mp4"
    raise PlanError(f"unsupported output container: {value!r}")


def _audio_codec_for_transcode(channels: int) -> tuple[str, int]:
    channels = channels if channels > 0 else 2
    if channels == 1:
        return "aac", 96
    if channels == 2:
        return "aac", 160
    if 3 <= channels <= 6:
        return "eac3", 448
    if 7 <= channels <= 8:
        return "eac3", 640
    return "eac3", 640


def _normalise_copy_set(value: Any, default: Iterable[str]) -> set[str]:
    if value is None:
        return set(default)
    if isinstance(value, str):
        values = [value]
    else:
        try:
            values = list(value)
        except TypeError:
            return set(default)
    if any(str(item).strip() == "*" for item in values):
        return set(default)
    result: set[str] = set()
    for item in values:
        key = _audio_codec_key({"codec_name": str(item)})
        if key in MP4_AUDIO_COPY_CODECS:
            result.add(key)
    return result


# ---------------------------------------------------------------------------
# audio selection (MP4-3, MP4-4)


def _original_language(streams: Sequence[Mapping[str, Any]]) -> str:
    if not streams:
        return "und"
    for stream in streams:
        if _is_default(stream):
            return _stream_language(stream)
    return _stream_language(streams[0])


def _audio_wanted_languages(
    cfg: Mapping[str, Any],
    original: str,
) -> tuple[list[str], bool]:
    raw = _get(cfg, "languages", "preferred_languages", "wanted_languages", "language")
    if raw is None:
        raw = cfg.get("original")
    if raw is None:
        return [], False
    if isinstance(raw, str):
        values: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        values = [raw]
    wanted: list[str] = []
    all_languages = False
    for value in values:
        text = str(value).strip()
        folded = text.casefold()
        if folded in {"*", "all", "any"}:
            all_languages = True
            continue
        if folded in {"original", "source", "orig"}:
            language = original
        else:
            language = normalise_language(text)
        if language not in wanted:
            wanted.append(language)
    return wanted, all_languages


def _unknown_policy(cfg: Mapping[str, Any]) -> str:
    value = str(_get(cfg, "unknown_language", "unknown", default="keep_first")).strip().lower()
    if value in {"keep_all", "keep-all", "all"}:
        return "keep_all"
    if value in {"drop", "ignore", "none"}:
        return "drop"
    return "keep_first"


def _track_title(stream: Mapping[str, Any]) -> str | None:
    return _stream_title(stream)


def audio_choice(
    stream: Mapping[str, Any],
    audio_cfg: Mapping[str, Any] | None,
    target_container: str,
    default: bool = False,
) -> dict[str, Any]:
    """Return one PLAN_SCHEMA audio choice for *stream*.

    MP4 copies AAC/AC-3/E-AC-3 only; a preset copy_codecs list narrows that
    set but can never make DTS/TrueHD/FLAC/PCM/Opus copyable into MP4.  MKV
    copies every selected codec (MKV-1).  Transcodes follow the MP4-3 table:
    mono AAC 96k, stereo AAC 160k, 3-6 E-AC-3 448k, 7-8 E-AC-3 640k and
    >8 E-AC-3 640k with ``downmix=True`` (the encoder applies ``-ac 8``).
    """
    cfg = _mapping(audio_cfg)
    container = _normalise_container(target_container)
    codec = _audio_codec_key(stream)
    channels = _stream_channels(stream)
    language = _stream_language(stream)

    can_copy = container == "mkv"
    if container == "mp4":
        allowed = _normalise_copy_set(cfg.get("copy_codecs"), MP4_AUDIO_COPY_CODECS)
        can_copy = codec in allowed

    entry: dict[str, Any] = {
        "index": _stream_index(stream),
        "source_codec": codec,
        "language": language,
        "channels": channels,
        "action": "copy" if can_copy else "transcode",
        "codec": codec if can_copy else _audio_codec_for_transcode(channels)[0],
        "bitrate": None if can_copy else _audio_codec_for_transcode(channels)[1],
        "default": bool(default),
    }
    layout = _channel_layout(stream)
    title = _track_title(stream)
    if title:
        entry["title"] = title
    if not can_copy and channels > 8:
        entry["downmix"] = True
        # The output channel count differs from the source here; the plan
        # states it explicitly so the verifier never has to guess.  The
        # speaker layout after a downmix is chosen by the encoder, so the
        # source layout is not part of the output contract in this case.
        entry["target_channels"] = 8
    elif layout:
        # Copy and same-count transcode keep the source layout, so it is
        # part of the output contract and the verifier checks it.
        entry["channel_layout"] = layout
    return entry


def select_audio(
    streams: Sequence[Mapping[str, Any]],
    audio_cfg: Mapping[str, Any] | None,
    target_container: str,
    source_had_audio: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Select and describe output audio tracks (MP4-3, MP4-4).

    Order of operations: normalise language tags, exclude commentary and
    audio-description tracks when configured, prefer configured languages
    (``original`` resolves to the default track's language or the first
    track), apply unknown_language, then fall back to the default or first
    remaining track with a ``fallback_used`` warning.  The default output
    track is first, the rest keep source order, and max_tracks is applied
    after every filter.  If the source had audio this never returns zero
    tracks.
    """
    cfg = _mapping(audio_cfg)
    container = _normalise_container(target_container)
    stream_list = [stream for stream in streams if isinstance(stream, Mapping)]
    if not stream_list:
        if source_had_audio:
            raise PlanError("source had audio but no audio streams were provided")
        return [], []

    warnings: list[str] = []
    original = _original_language(stream_list)
    exclude_commentary = _bool(cfg.get("exclude_commentary"), True)

    indexed = sorted(
        enumerate(stream_list),
        key=lambda item: (_stream_index(item[1], item[0]), item[0]),
    )
    candidates = indexed
    if exclude_commentary:
        candidates = [(pos, stream) for pos, stream in indexed if not _is_commentary(stream)]

    fallback_used = False
    if not candidates:
        # Every track was commentary/description.  Never emit zero audio.
        fallback_used = True
        candidates = [(_default_or_first(indexed))]

    wanted, all_languages = _audio_wanted_languages(cfg, original)
    unknown_policy = _unknown_policy(cfg)
    selected: list[tuple[int, Mapping[str, Any]]] = []

    if all_languages:
        selected = list(candidates)
    else:
        matched: list[tuple[int, Mapping[str, Any]]] = []
        unknown: list[tuple[int, Mapping[str, Any]]] = []
        for pos, stream in candidates:
            language = _stream_language(stream)
            if language in wanted:
                matched.append((pos, stream))
            elif language == "und" and "und" not in wanted:
                unknown.append((pos, stream))
        selected = list(matched)
        if unknown_policy == "keep_all":
            selected.extend(unknown)
        elif unknown_policy == "keep_first" and unknown:
            selected.append(unknown[0])

        if not selected:
            fallback_used = True
            selected = [_default_or_first(candidates)]

    # De-duplicate and put the source default track first, then source order.
    unique: list[tuple[int, Mapping[str, Any]]] = []
    seen: set[int] = set()
    for pos, stream in selected:
        key = id(stream)
        if key in seen:
            continue
        seen.add(key)
        unique.append((pos, stream))
    primary_default = next((stream for _pos, stream in unique if _is_default(stream)), None)
    unique.sort(
        key=lambda item: (
            0 if primary_default is not None and item[1] is primary_default else 1,
            item[0],
        )
    )

    max_tracks = max(1, _int(cfg.get("max_tracks"), 3))
    unique = unique[:max_tracks]

    choices = [
        audio_choice(stream, cfg, container, default=(ordinal == 0))
        for ordinal, (_pos, stream) in enumerate(unique)
    ]
    if fallback_used:
        warnings.append("fallback_used")
    return choices, warnings


def _default_or_first(
    items: Sequence[tuple[int, Mapping[str, Any]]],
) -> tuple[int, Mapping[str, Any]]:
    for item in items:
        if _is_default(item[1]):
            return item
    return items[0]


# ---------------------------------------------------------------------------
# subtitle selection (MP4-5, MP4-6, MP4-7, MKV-1)


def _subtitle_kind(codec: str) -> str:
    value = codec.casefold()
    if value in MP4_TEXT_SUBTITLE_CODECS:
        return "text"
    if value in BITMAP_SUBTITLE_CODECS:
        return "bitmap"
    return "unknown"


def _subtitle_wanted(cfg: Mapping[str, Any]) -> tuple[list[str] | None, bool]:
    raw = _get(cfg, "languages", "preferred_languages", "wanted_languages")
    if raw is None:
        return None, True
    if isinstance(raw, str):
        values = [raw]
    else:
        try:
            values = list(raw)
        except TypeError:
            return None, True
    if not values:
        return None, True
    original = _get(cfg, "original_language", default=None)
    wanted: list[str] = []
    unknown_requested = False
    for value in values:
        text = str(value).strip()
        if text.casefold() in {"*", "all", "any"}:
            return None, True
        if text.casefold() in {"original", "source"} and original:
            language = normalise_language(original)
        else:
            language = normalise_language(text)
        if language == "und":
            unknown_requested = True
        elif language not in wanted:
            wanted.append(language)
    if not wanted:
        return (["und"], False) if unknown_requested else (None, True)
    return wanted, False


def _normalise_mkv_arguments(
    subtitle_cfg: Mapping[str, Any],
    mkv_policy: str | None,
) -> tuple[str, str]:
    """Return ``(bitmap_mode, policy)`` from all supported spellings."""
    bitmap_mode = str(
        _get(subtitle_cfg, "bitmap", "bitmap_policy", "mkv_fallback", default="mkv_fallback")
    ).strip().lower()
    if bitmap_mode in {"true", "1", "yes", "fallback", "mkv"}:
        bitmap_mode = "mkv_fallback"
    elif bitmap_mode in {"false", "0", "no", "never", "drop"}:
        bitmap_mode = "drop"
    if bitmap_mode not in {"mkv_fallback", "drop"}:
        bitmap_mode = "mkv_fallback"

    raw_policy = mkv_policy
    if raw_policy is None:
        raw_policy = _get(subtitle_cfg, "mkv_policy", default="bitmap_only")
    policy = str(raw_policy or "bitmap_only").strip().lower()
    if policy in {"mkv_fallback", "fallback"}:
        # The task-level spelling combines the two preset concepts.
        bitmap_mode = "mkv_fallback"
        policy = "bitmap_only"
    elif policy in {"drop"}:
        bitmap_mode = "drop"
        policy = "bitmap_only"
    elif policy in {"any", "any_subs", "any-subs"}:
        policy = "any_subs"
    elif policy in {"never", "no"}:
        policy = "never"
    elif policy in {"bitmap_only", "bitmap-only", "bitmap"}:
        policy = "bitmap_only"
    else:
        raise PlanError(f"unknown mkv_policy: {raw_policy!r}")
    return bitmap_mode, policy


def select_subtitles(
    streams: Sequence[Mapping[str, Any]],
    subtitle_cfg: Mapping[str, Any] | None,
    source_container: str | None,
    base_container: str,
    mkv_policy: str | None,
    source_duration: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str | None]:
    """Select subtitles and decide whether the output container changes.

    Text codecs become ``mov_text`` in MP4 and are copied in MKV.  Bitmap
    codecs (PGS, VobSub/dvd_subtitle, DVB, XSUB) cannot be muxed into MP4:
    ``bitmap=mkv_fallback``/``mkv_policy=bitmap_only`` switches the output to
    MKV and copies all kept subtitles; ``bitmap=drop`` or
    ``mkv_policy=never`` drops them with a manifest record.  ``any_subs``
    switches to MKV whenever any subtitle is kept.

    Returns ``(choices, dropped, effective_container, note)``.  The note is
    exactly ``"container: mkv (bitmap subs)"`` for a bitmap fallback and
    ``"container: mkv (any_subs)"`` when only the any_subs rule triggered.
    """
    cfg = _mapping(subtitle_cfg)
    source_family = _container_family(source_container)
    try:
        base = _normalise_container(base_container)
    except PlanError:
        if source_family in {"mp4", "mkv"}:
            base = source_family  # type: ignore[assignment]
        else:
            raise
    bitmap_mode, policy = _normalise_mkv_arguments(cfg, mkv_policy)
    wanted, all_languages = _subtitle_wanted(cfg)
    include_forced = _bool(cfg.get("include_forced"), True)
    _ = source_duration  # kept in the signature for callers/manifests

    selected: list[tuple[Mapping[str, Any], str, str]] = []
    for stream in streams:
        if not isinstance(stream, Mapping) or stream.get("codec_type") not in (None, "subtitle"):
            continue
        codec = str(stream.get("codec_name") or stream.get("codec") or "").strip().lower()
        kind = _subtitle_kind(codec)
        if kind == "unknown":
            continue
        if _is_forced(stream) and not include_forced:
            continue
        language = _stream_language(stream)
        if not all_languages and wanted is not None and language not in wanted:
            continue
        selected.append((stream, kind, codec))
    selected.sort(key=lambda item: _stream_index(item[0]))

    dropped: list[dict[str, Any]] = []

    if base == "mkv":
        choices = [
            {
                "index": _stream_index(stream),
                "source_codec": codec,
                "language": _stream_language(stream),
                "forced": _is_forced(stream),
                "action": "copy",
                "sidecar": None,
            }
            for stream, _kind, codec in selected
        ]
        return choices, dropped, "mkv", None

    selected_bitmap = [item for item in selected if item[1] == "bitmap"]
    selected_text = [item for item in selected if item[1] == "text"]
    bitmaps_kept = bitmap_mode == "mkv_fallback" and policy != "never"
    fallback_bitmap = (
        bool(selected_bitmap) and bitmap_mode == "mkv_fallback" and policy == "bitmap_only"
    )

    def drop_bitmaps() -> None:
        for stream, _kind, codec in selected_bitmap:
            record = {
                "index": _stream_index(stream),
                "codec": codec,
                "language": _stream_language(stream),
                "reason": "bitmap-subtitle-not-supported-in-mp4",
            }
            if record not in dropped:
                dropped.append(record)

    if policy == "any_subs":
        any_kept = bool(selected_text) or (bool(selected_bitmap) and bitmaps_kept)
        if any_kept:
            note = (
                "container: mkv (bitmap subs)"
                if (selected_bitmap and bitmaps_kept)
                else "container: mkv (any_subs)"
            )
            if not bitmaps_kept:
                drop_bitmaps()
            choices = [
                {
                    "index": _stream_index(stream),
                    "source_codec": codec,
                    "language": _stream_language(stream),
                    "forced": _is_forced(stream),
                    "action": "copy",
                    "sidecar": None,
                }
                for stream, kind, codec in selected
                if kind == "text" or bitmaps_kept
            ]
            return choices, dropped, "mkv", note

    if fallback_bitmap:
        choices = [
            {
                "index": _stream_index(stream),
                "source_codec": codec,
                "language": _stream_language(stream),
                "forced": _is_forced(stream),
                "action": "copy",
                "sidecar": None,
            }
            for stream, _kind, codec in selected
        ]
        return choices, dropped, "mkv", "container: mkv (bitmap subs)"

    # MP4 path.  Bitmaps are recorded and dropped; text is converted.
    drop_bitmaps()
    choices = [
        {
            "index": _stream_index(stream),
            "source_codec": codec,
            "language": _stream_language(stream),
            "forced": _is_forced(stream),
            "action": "mov_text",
            "sidecar": None,
        }
        for stream, kind, codec in selected_text
    ]
    return choices, dropped, "mp4", None


# ---------------------------------------------------------------------------
# capabilities / backend choice


def _backend_spec(capabilities: Mapping[str, Any], backend: str) -> Any:
    direct = capabilities.get(backend)
    if direct is not None:
        return direct
    for key in ("backends", "encoders"):
        section = capabilities.get(key)
        if isinstance(section, Mapping):
            if backend in section:
                return section[backend]
            # Some capability files list the exact ffmpeg encoder names
            # (``hevc_qsv``) instead of backend names.
            matching = [
                value
                for name, value in section.items()
                if str(name).endswith("_" + backend)
            ]
            if matching:
                for value in matching:
                    if _spec_available(value):
                        return value
                return False
            if backend == "software":
                matching = [
                    value
                    for name, value in section.items()
                    if str(name) in {"libx265", "libx264", "libsvtav1"}
                ]
                if matching:
                    return any(_spec_available(value) for value in matching)
    available = capabilities.get("available_backends") or capabilities.get("available")
    if isinstance(available, (list, tuple, set, frozenset)):
        return backend in available
    # A capability file may only list encoder names, e.g. ffmpeg -encoders.
    names = capabilities.get("encoders")
    if isinstance(names, (list, tuple, set, frozenset)):
        if backend == "software":
            return bool({"libx265", "libx264"} & set(map(str, names)))
        return any(str(name).endswith("_" + backend) for name in names)
    if backend == "software":
        if capabilities.get("libx265") is True:
            return {"available": True}
        if capabilities.get("libx264") is True:
            return {"available": True}
    return None


def _spec_available(spec: Any) -> bool:
    if spec is None or spec is False:
        return False
    if spec is True:
        return True
    if not isinstance(spec, Mapping):
        return bool(spec)
    if not spec:
        return False
    for key in ("available", "ok", "enabled"):
        if key in spec and not _bool(spec[key]):
            return False
    return True


def _spec_supports_codec(spec: Any, codec: str) -> bool:
    codec = codec.lower()
    if spec is None or spec is False:
        return False
    if spec is True:
        return True
    if not isinstance(spec, Mapping):
        return bool(spec)
    if not _spec_available(spec):
        return False
    codec_section = spec.get("codecs") or spec.get("encoders")
    if isinstance(codec_section, Mapping):
        value = codec_section.get(codec)
        if value is not None:
            return _bool(value) if not isinstance(value, Mapping) else _spec_available(value)
    elif isinstance(codec_section, (list, tuple, set, frozenset)):
        names = {str(item).lower() for item in codec_section}
        if names and not any(codec in name or name == codec for name in names):
            return False
    for key in (codec, codec + "8", codec + "_8"):
        if key in spec:
            value = spec[key]
            if isinstance(value, Mapping):
                return _spec_available(value)
            return _bool(value)
    return True


def _spec_supports_10bit(spec: Any, backend: str, capabilities: Mapping[str, Any]) -> bool:
    if backend == "software":
        return _SOFTWARE_ASSUMED_10BIT
    candidates: list[Any] = []
    if isinstance(spec, Mapping):
        for key in ("hevc10", "hevc_10bit", "ten_bit", "10bit", "10-bit", "hdr10"):
            if key in spec:
                candidates.append(spec[key])
        codec_section = spec.get("codecs")
        if isinstance(codec_section, Mapping) and isinstance(codec_section.get("hevc"), Mapping):
            candidates.append(codec_section["hevc"])
    for key in ("ten_bit", "10bit", "hevc10"):
        top = capabilities.get(key)
        if isinstance(top, Mapping):
            if backend in top:
                candidates.append(top[backend])
            elif "hevc" in top:
                candidates.append(top["hevc"])
        elif top is not None:
            candidates.append(top)
    for value in candidates:
        if isinstance(value, Mapping):
            if any(_bool(value.get(key)) for key in ("hevc", "hevc10", "10bit", "ten_bit")):
                return True
            if value.get("available") is False:
                continue
        elif _bool(value):
            return True
    return False


def _backend_order(preset_map: Mapping[str, Any], config: Mapping[str, Any]) -> list[str]:
    video = _section(preset_map, "video")
    raw = _get(video, "backend_order", default=None)
    if raw is None:
        raw = _get(preset_map, "backend_order", default=None)
    if raw is None:
        raw = _get(_section(config, "video"), "backend_order", default=None)
    if raw is None:
        raw = _get(config, "backend_order", default=None)
    if raw is None:
        return ["qsv", "vaapi"]
    if isinstance(raw, str):
        return [raw]
    try:
        return [str(item).lower() for item in raw]
    except TypeError:
        return ["qsv", "vaapi"]


def _start_end(point: Any) -> time:
    if isinstance(point, time):
        return point.replace(tzinfo=None, second=0, microsecond=0)
    text = str(point).strip()
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)
    if not match:
        raise PlanError(f"invalid schedule time: {point!r}")
    return time(int(match.group(1)), int(match.group(2)))


def schedule_window(
    window: str | Mapping[str, Any] | Sequence[Any] | None = None,
) -> tuple[time, time] | None:
    """Parse ``"HH:MM-HH:MM"`` into ``(start, end)`` or ``None``.

    Empty/``None`` means no window.  A mapping with ``start``/``end`` and a
    two-item sequence are also accepted.  The function is pure: callers pass
    the preset's ``[schedule].window`` value.  Overnight windows are allowed.
    """
    if window is None:
        return None
    if isinstance(window, Mapping):
        for start_key, end_key in (
            ("start", "end"),
            ("start", "close"),
            ("open", "close"),
            ("from", "to"),
            ("start_time", "end_time"),
        ):
            if start_key in window and end_key in window:
                return _start_end(window[start_key]), _start_end(window[end_key])
        raise PlanError("schedule window mapping needs start and end")
    if isinstance(window, (tuple, list)) and len(window) == 2:
        return _start_end(window[0]), _start_end(window[1])
    text = str(window).strip()
    if not text:
        return None
    match = re.fullmatch(
        r"([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)",
        text,
    )
    if not match:
        raise PlanError(f"window must be 'HH:MM-HH:MM' or empty, got {window!r}")
    return (
        time(int(match.group(1)), int(match.group(2))),
        time(int(match.group(3)), int(match.group(4))),
    )


def _as_datetime(now: Any) -> datetime:
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    if isinstance(now, date):
        return datetime.combine(now, time())
    if isinstance(now, time):
        return datetime.combine(date.today(), now.replace(tzinfo=None))
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.fromtimestamp(float(now))
    if isinstance(now, str):
        try:
            return datetime.combine(date.today(), _start_end(now))
        except PlanError as exc:
            raise PlanError(f"invalid now value: {now!r}") from exc
    raise PlanError(f"now must be a datetime, date, time or timestamp, got {type(now).__name__}")


def estimate_fits_window(
    duration_seconds: float,
    estimated_seconds: float | None,
    window: str | Mapping[str, Any] | Sequence[Any] | None,
    now: datetime | date | time | float | str | None = None,
) -> tuple[bool, str]:
    """Pure SCHED-1 start-gate decision.

    A job fits only when it is inside the window now and its estimated
    completion is no later than the next close.  A missing estimate fails
    with ``cold-start``; no window is always allowed.  Once started a job may
    overrun, so this helper never looks at running jobs.  Overnight windows
    (``22:00-06:00``) are supported.
    """
    parsed = schedule_window(window)
    if parsed is None:
        return True, "no-window"
    if estimated_seconds is None:
        return False, "cold-start"
    try:
        estimate = float(estimated_seconds)
    except (TypeError, ValueError):
        return False, "cold-start"
    if estimate < 0 or not math.isfinite(estimate):
        return False, "cold-start"

    start, end = parsed
    moment = _as_datetime(now).replace(second=0, microsecond=0)
    if start == end:
        return False, "estimate-exceeds-window"

    if start < end:
        open_dt = moment.replace(hour=start.hour, minute=start.minute)
        close_dt = moment.replace(hour=end.hour, minute=end.minute)
        if moment < open_dt or moment >= close_dt:
            return False, "outside-window"
    else:
        # Overnight window: before the close belongs to yesterday's opening.
        if moment.time() >= start:
            open_dt = moment.replace(hour=start.hour, minute=start.minute)
            close_dt = (moment + timedelta(days=1)).replace(
                hour=end.hour, minute=end.minute, second=0, microsecond=0
            )
        elif moment.time() < end:
            close_dt = moment.replace(hour=end.hour, minute=end.minute)
        else:
            return False, "outside-window"

    completion = moment + timedelta(seconds=estimate)
    if completion <= close_dt:
        return True, "fits"
    return False, "estimate-exceeds-window"


# ---------------------------------------------------------------------------
# preset/config/name helpers


def _preset_mapping(preset: Any) -> dict[str, Any]:
    if isinstance(preset, Mapping):
        return dict(preset)
    raw = _get(preset, "raw", "data", default=None)
    if isinstance(raw, Mapping) and raw:
        result = dict(raw)
    else:
        result = {}
    result.setdefault("id", _get(preset, "id", "preset", default="unknown"))
    result.setdefault("container", _get(preset, "container", default="mp4"))
    for section_name in ("meta", "video", "audio", "subtitles", "skip", "schedule"):
        obj = _get(preset, section_name, default=None)
        if is_dataclass(obj):
            result.setdefault(
                section_name,
                {field.name: getattr(obj, field.name) for field in dataclass_fields(obj)},
            )
        elif isinstance(obj, Mapping):
            result.setdefault(section_name, dict(obj))
    return result


def _merged_section(
    preset_map: Mapping[str, Any],
    config: Mapping[str, Any],
    name: str,
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(defaults)
    config_section = _section(config, name)
    if config_section:
        merged.update(config_section)
    preset_section = _section(preset_map, name)
    if preset_section:
        merged.update(preset_section)
    return merged


def _schedule_value(
    preset_map: Mapping[str, Any],
    config: Mapping[str, Any],
) -> str:
    preset_value = ""
    preset_schedule = _section(preset_map, "schedule")
    if "window" in preset_schedule:
        preset_value = str(preset_schedule.get("window") or "")
    elif "schedule_window" in preset_map:
        preset_value = str(preset_map.get("schedule_window") or "")
    elif "window" in preset_map:
        preset_value = str(preset_map.get("window") or "")
    if preset_value.strip():
        return preset_value
    config_value = ""
    config_schedule = _section(config, "schedule")
    if "window" in config_schedule:
        config_value = str(config_schedule.get("window") or "")
    else:
        config_value = str(_get(config, "schedule_window", default="") or "")
    return config_value if config_value.strip() else preset_value


def _config_dry_run(config: Mapping[str, Any]) -> bool:
    return _bool(_get(config, "dry_run", default=True), True)


def _config_append_tmdbid(config: Mapping[str, Any]) -> bool:
    return _bool(_get(config, "append_tmdbid", default=False), False)


def _import_naming():
    try:
        from . import naming as naming_module  # imported lazily; optional

        return naming_module
    except Exception:  # pragma: no cover - defensive during concurrent work
        return None


def _fallback_sanitize(text: str) -> str:
    value = unicodedata.normalize("NFC", str(text))
    value = re.sub(r"\s*:\s*", " - ", value)
    for char in '/\\?*"<>|':
        value = value.replace(char, "")
    value = "".join(
        " " if unicodedata.category(char) == "Cc" else char for char in value
    )
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value


def _local_parse_name(value: str) -> tuple[str, int | None]:
    text = _fallback_sanitize(value)
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
    year: int | None = None
    if match:
        candidate = int(match.group(1))
        if 1870 <= candidate <= date.today().year + 2:
            year = candidate
            text = (text[: match.start()] + " " + text[match.end() :]).strip(" .-_")
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text, year


def _resolve_name(
    identity: Mapping[str, Any] | None,
    source_path: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    ident = _object_mapping(identity)
    naming = _import_naming()

    title = str(ident.get("title") or "").strip()
    year_value = ident.get("year")
    year = _int(year_value, 0) if year_value is not None else None
    if year is not None and not (1870 <= year <= date.today().year + 2):
        year = None
    tmdbid_value = ident.get("tmdbid", ident.get("tmdb_id"))
    tmdbid = _int(tmdbid_value, 0) if tmdbid_value is not None else None
    if tmdbid is not None and tmdbid <= 0:
        tmdbid = None

    folder = source_path.parent.name
    if not title:
        if naming is not None and hasattr(naming, "parse_media_name"):
            try:
                parsed = naming.parse_media_name(folder)
                title = str(getattr(parsed, "title", "") or "").strip()
                if year is None:
                    parsed_year = getattr(parsed, "year", None)
                    if isinstance(parsed_year, int):
                        year = parsed_year
            except Exception:
                title = ""
        if not title:
            title, local_year = _local_parse_name(folder)
            if year is None:
                year = local_year
    if not title:
        title, local_year = _local_parse_name(source_path.stem)
        if year is None:
            year = local_year
    if not title:
        title = "Untitled"

    if naming is not None and hasattr(naming, "sanitize_component"):
        try:
            safe_title = naming.sanitize_component(title)
        except Exception:
            safe_title = _fallback_sanitize(title) or "Untitled"
    else:
        safe_title = _fallback_sanitize(title) or "Untitled"

    append_tmdbid = _config_append_tmdbid(config)
    if year is not None:
        if naming is not None and hasattr(naming, "format_media_name"):
            try:
                stem = naming.format_media_name(
                    safe_title, int(year), tmdbid, append_tmdbid=append_tmdbid and tmdbid is not None
                )
            except Exception:
                stem = f"{safe_title} ({int(year)})"
        else:
            stem = f"{safe_title} ({int(year)})"
        if append_tmdbid and tmdbid is not None and "[tmdbid-" not in stem:
            stem = f"{stem} [tmdbid-{tmdbid}]"
    else:
        stem = safe_title
    return {"title": safe_title, "year": year, "tmdbid": tmdbid, "stem": stem}


def _sidecar_language(path: Path, source: Path) -> str:
    naming = _import_naming()
    if naming is not None and hasattr(naming, "sidecar_language"):
        try:
            language = naming.sidecar_language(path.name)
            if language:
                return str(language)
        except Exception:
            pass
    prefix = source.stem + "."
    if path.name.startswith(prefix):
        remainder = path.name[len(prefix) :]
        tokens = remainder.split(".")[:-1]
        for token in reversed(tokens):
            language = normalise_language(token)
            if language != "und":
                return language
    return "und"


def _plan_sidecars(source_path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in find_sidecars(source_path):
        action = "copy" if path.suffix.lower() == ".srt" else "convert"
        result.append(
            {
                "path": str(path.absolute()),
                "language": _sidecar_language(path, source_path),
                "action": action,
            }
        )
    return result


# ---------------------------------------------------------------------------
# build_plan


def _coerce_probe(probe: Any, source_path: Path) -> ProbeResult:
    if isinstance(probe, ProbeResult):
        return probe
    raw = _get(probe, "raw", default=None)
    if isinstance(raw, Mapping):
        return ProbeResult(path=source_path, raw=raw)
    if isinstance(probe, Mapping):
        return ProbeResult(path=source_path, raw=probe)
    streams = _get(probe, "streams", default=None)
    if streams is not None:
        fmt = _get(probe, "format", default=None)
        format_info = dict(fmt) if isinstance(fmt, Mapping) else {}
        format_info.setdefault("format_name", _get(probe, "format_name", default=""))
        format_info.setdefault("duration", _get(probe, "duration", default=0.0))
        format_info.setdefault("size", _get(probe, "size", default=0))
        return ProbeResult(
            path=source_path,
            raw={
                "format": format_info,
                "streams": list(streams),
                "chapters": _get(probe, "chapters", default=[]),
            },
        )
    raise PlanError("probe must be a ProbeResult or an ffprobe JSON mapping")


def _preset_id(preset_map: Mapping[str, Any]) -> str:
    value = preset_map.get("id") or preset_map.get("preset") or preset_map.get("name")
    if value is None:
        return "unknown"
    return str(value)


def _preset_action(
    preset_map: Mapping[str, Any],
    container: str,
    action_override: str | None,
) -> str:
    if action_override is not None:
        action = str(action_override).strip().lower()
    else:
        video = _section(preset_map, "video")
        value = _get(video, "action", default=None)
        if value is None:
            value = _get(preset_map, "action", default=None)
        if value is None:
            action = "remux" if container == "mkv" else "encode"
        else:
            action = str(value).strip().lower()
    if action in {"copy", "remux"}:
        action = "remux"
    elif action in {"encode"}:
        action = "encode"
    else:
        raise PlanError(f"unsupported preset action: {action!r}")
    if container == "mkv" and action == "encode":
        raise PlanError("MKV presets are remux-only in v1 (MKV-1)")
    return action


def _preset_container_and_action(
    preset_map: Mapping[str, Any],
    action_override: str | None,
) -> tuple[str, str]:
    container_raw = preset_map.get("container")
    if container_raw is None:
        output = _section(preset_map, "output")
        container_raw = output.get("container")
    if container_raw is None:
        video = _section(preset_map, "video")
        container_raw = video.get("container")
    container = _normalise_container(container_raw, "mp4")
    return container, _preset_action(preset_map, container, action_override)


def _backend_available(
    capabilities: Mapping[str, Any],
    backend: str,
) -> tuple[bool, Any]:
    spec = _backend_spec(capabilities, backend)
    return _spec_available(spec), spec


def _select_encode_backend(
    *,
    codec: str,
    source_10bit: bool,
    ten_bit_policy: str,
    preset_map: Mapping[str, Any],
    config: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    window: str,
) -> tuple[str, str, bool]:
    """Choose a backend and whether output stays 10-bit.

    Hardware backends are tried in preset/config order.  Software is only
    allowed when the preset sets allow_software and a non-empty schedule
    window exists (SAF-1 software guard / SCHED-1).  For a 10-bit keep policy
    with no 10-bit hardware the result is REVIEW reason ``10bit-no-hw``;
    review/to8 are handled by the caller before this function.
    """
    video = _section(preset_map, "video")
    allow_software = _bool(
        _get(
            video,
            "allow_software",
            default=_get(
                preset_map,
                "allow_software",
                default=_get(
                    capabilities,
                    "allow_software",
                    default=_get(config, "allow_software", default=False),
                ),
            ),
        )
    )
    order = _backend_order(preset_map, config)
    hardware_order = [backend for backend in order if backend != "software"]
    want_10bit = bool(source_10bit and ten_bit_policy == "keep")
    seen: set[str] = set()
    for backend in hardware_order:
        backend = backend.lower()
        if backend in seen or backend == "software":
            continue
        seen.add(backend)
        available, spec = _backend_available(capabilities, backend)
        if not available:
            continue
        codec_ok = _spec_supports_codec(spec, codec)
        ten_bit_ok = _spec_supports_10bit(spec, backend, capabilities)
        if want_10bit:
            if not ten_bit_ok:
                continue
        elif not (codec_ok or (source_10bit and ten_bit_ok)):
            continue
        encoder = _encoder_name(backend, codec)
        return backend, encoder, want_10bit

    if allow_software and str(window or "").strip():
        # Parse now so an invalid preset window cannot silently enable CPU.
        schedule_window(window)
        encoder = _encoder_name("software", codec)
        return "software", encoder, want_10bit

    if want_10bit:
        raise ReviewRequired(
            "10bit-no-hw",
            "no 10-bit hardware encoder and software encoding is not allowed",
        )
    raise PlanError(
        "no capable hardware encoder is available; enable allow_software with a "
        "non-empty schedule window or fix the capability report"
    )


def _encoder_name(backend: str, codec: str) -> str:
    if backend == "software":
        return {"hevc": "libx265", "h264": "libx264", "av1": "libsvtav1"}.get(
            codec, "libx265"
        )
    return f"{codec}_{backend}"


def build_plan(
    *,
    source_path: str | Path,
    probe: ProbeResult | Mapping[str, Any],
    preset: Mapping[str, Any],
    config: Mapping[str, Any],
    identity: Mapping[str, Any] | None,
    capabilities: Mapping[str, Any] | None,
    speed_factor: float | None,
    plan_version: int = 1,
    action_override: str | None = None,
    video_bytes: int | None = None,
    name: str | None = None,
    estimated_seconds: float | None = None,
    fallback_from: str | None = None,
) -> dict[str, Any]:
    """Build the PLAN_SCHEMA v1 dictionary.

    ``preset`` is the raw TOML mapping (a loaded Preset is also accepted).
    ``name`` is an optional explicit output stem override; otherwise the
    identity mapping or a sanitised parsed source folder supplies it.

    Review decisions raise :class:`ReviewRequired`; no plan with a non-null
    review is ever returned.  SIZE-1 may turn an HEVC encode preset into a
    remux before backend selection.  Explicit stream indexes (never
    ``-map 0``) are recorded for the encoder.
    """
    path = Path(source_path)
    if not path.is_absolute():
        path = path.absolute()
    probe_result = _coerce_probe(probe, path)
    preset_map = _preset_mapping(preset)
    config_map = _object_mapping(config)
    capabilities_map = _object_mapping(capabilities)
    main_video = probe_result.main_video
    if main_video is None:
        raise PlanError("source has no video stream")
    if not container_allowed(probe_result.format_name):
        raise PlanError(
            f"source container is not allowed: {probe_result.format_name or '<unknown>'!r}"
        )

    video_cfg = _section(preset_map, "video")
    container, action = _preset_container_and_action(preset_map, action_override)

    subtitle_cfg = _merged_section(
        preset_map, config_map, "subtitles", _DEFAULT_SUBTITLE_CONFIG
    )
    audio_cfg = _merged_section(preset_map, config_map, "audio", _DEFAULT_AUDIO_CONFIG)
    skip_cfg = _merged_section(preset_map, config_map, "skip", _DEFAULT_SKIP_CONFIG)
    if not _section(preset_map, "skip"):
        for key in ("already_efficient", "efficient_kbps_1080p", "min_savings_pct"):
            if key in preset_map:
                skip_cfg[key] = preset_map[key]
            elif key in config_map and key not in _section(config_map, "skip"):
                skip_cfg[key] = config_map[key]
    window = _schedule_value(preset_map, config_map)
    if not window.strip():
        window = str(
            _get(
                capabilities_map,
                "schedule_window",
                "window",
                default="",
            )
            or ""
        )

    source_container = probe_result.format_name
    mkv_policy = subtitle_cfg.get("mkv_policy")
    subtitle_choices, dropped, effective_container, subtitle_note = select_subtitles(
        probe_result.subtitle_streams,
        subtitle_cfg,
        source_container,
        container,
        mkv_policy,
        probe_result.duration,
    )

    warnings: list[str] = []
    if subtitle_note:
        warnings.append(subtitle_note)

    video_codec = _video_codec(main_video)
    source_width = _int(main_video.get("width"), 0)
    source_height = _int(main_video.get("height"), 0)
    if source_width <= 0 or source_height <= 0:
        raise PlanError("main video stream has no usable dimensions")

    duration = float(probe_result.duration or 0.0)
    source_video_bytes = _source_video_bytes(probe_result, main_video, video_bytes)
    source_kbps = _video_kbps(main_video, source_video_bytes, duration)

    # SIZE-1: an already-efficient HEVC source is remuxed, not re-encoded.
    if action == "encode":
        already_enabled = _bool(skip_cfg.get("already_efficient"), True)
        threshold_base = _float(
            skip_cfg.get("efficient_kbps_1080p"), DEFAULT_EFFICIENT_KBPS_1080P
        )
        if (
            action_override is None
            and already_enabled
            and video_codec == "hevc"
            and source_kbps is not None
        ):
            threshold = efficient_threshold_kbps(
                threshold_base, source_width, source_height
            )
            if source_kbps < threshold:
                action = "remux"
                warnings.append(
                    "already_efficient: "
                    f"{source_kbps:.1f} kbps below {threshold:.1f} kbps; remuxing"
                )

    flags = hdr_flags(main_video)
    hdr_mode = str(
        _get(video_cfg, "hdr", default=_get(preset_map, "hdr", default="review"))
    ).strip().lower()
    ten_bit_policy = _get(
        video_cfg,
        "ten_bit_source",
        default=_get(preset_map, "ten_bit_source", default=None),
    )
    ten_bit_policy_text = (
        "review" if ten_bit_policy is None else str(ten_bit_policy).strip().lower()
    )

    # MP4-10: never tone-map silently.  This applies to remux and encode.
    if flags["hdr"] and hdr_mode != "keep":
        raise ReviewRequired("hdr", detail=str(flags.get("format") or "hdr"))
    if flags["hdr"] and action == "encode" and ten_bit_policy_text != "keep":
        raise ReviewRequired(
            "hdr",
            detail="hdr = keep on an encode requires ten_bit_source = keep",
        )

    # MP4-11: interlaced sources always need a human choice.
    if is_interlaced(main_video):
        raise ReviewRequired("interlaced", detail=str(main_video.get("field_order") or ""))

    # MP4-2: MP4 remux can copy only H.264/HEVC.  The effective container may
    # have become MKV because of a bitmap-subtitle fallback.
    if action == "remux" and effective_container == "mp4" and video_codec not in {
        "h264",
        "hevc",
    }:
        raise ReviewRequired(
            "remux-incompatible-video",
            detail=f"MP4 cannot copy {video_codec or 'unknown'} video",
        )

    source_10bit = is_10bit(main_video)
    output_10bit = source_10bit
    target_pix_fmt = str(main_video.get("pix_fmt") or "yuv420p")  # noqa: F841 (contract)
    target_bit_depth = _bit_depth(main_video)
    backend: str | None = None
    encoder: str | None = None

    if action == "encode":
        requested_codec = str(
            _get(video_cfg, "codec", default=preset_map.get("codec", "hevc"))
        ).strip().lower()
        if requested_codec in {"h265", "h.265"}:
            requested_codec = "hevc"
        if requested_codec in {"copy", "any"}:
            raise PlanError("an encode preset must name an output video codec")
        if requested_codec != "hevc":
            # v1 encodes HEVC only, but keep a clear error rather than a plan
            # the encoder cannot honour.
            raise PlanError(f"unsupported output video codec: {requested_codec!r}")
        output_codec = requested_codec

        if source_10bit:
            if ten_bit_policy_text == "review":
                raise ReviewRequired(
                    "10bit",
                    detail=f"source pixel format {target_pix_fmt} needs an explicit decision",
                )
            if ten_bit_policy_text == "to8":
                target_pix_fmt = _to_8bit_pix_fmt(main_video)
                target_bit_depth = 8
                output_10bit = False
                warnings.append(
                    "10bit_to_8: "
                    f"{main_video.get('pix_fmt') or 'source'} -> {target_pix_fmt}"
                )
                dropped.append(
                    {
                        "type": "video",
                        "index": _stream_index(main_video),
                        "reason": "10bit-to-8",
                        "from": str(main_video.get("pix_fmt") or ""),
                        "to": target_pix_fmt,
                    }
                )
            elif ten_bit_policy_text == "keep":
                output_10bit = True
            else:
                raise ReviewRequired(
                    "10bit", detail=f"unknown ten_bit_source policy {ten_bit_policy_text!r}"
                )

        backend, encoder, output_10bit = _select_encode_backend(
            codec=output_codec,
            source_10bit=source_10bit,
            ten_bit_policy=str(ten_bit_policy_text),
            preset_map=preset_map,
            config=config_map,
            capabilities=capabilities_map,
            window=window,
        )
        if source_10bit and ten_bit_policy_text == "to8":
            output_10bit = False
        if output_10bit and target_bit_depth < 10:
            target_bit_depth = 10
    else:
        output_codec = video_codec
        target_pix_fmt = str(main_video.get("pix_fmt") or "yuv420p")
        target_bit_depth = _bit_depth(main_video)
        output_10bit = source_10bit

    # Explicit target dimensions; encode may scale down, remux never does.
    max_width = _int(
        _get(
            video_cfg,
            "max_width",
            default=_get(
                preset_map,
                "max_width",
                default=_get(config_map, "max_width", default=DEFAULT_MAX_WIDTH),
            ),
        ),
        DEFAULT_MAX_WIDTH,
    )
    max_height = _int(
        _get(
            video_cfg,
            "max_height",
            default=_get(
                preset_map,
                "max_height",
                default=_get(config_map, "max_height", default=DEFAULT_MAX_HEIGHT),
            ),
        ),
        DEFAULT_MAX_HEIGHT,
    )
    if action == "encode":
        target_width, target_height, scaled = compute_dimensions(
            source_width, source_height, _sar(main_video), max_width, max_height
        )
    else:
        target_width, target_height = _even_floor(source_width), _even_floor(source_height)
        scaled = False

    if effective_container == "mp4" and output_codec == "hevc":
        tag = "hvc1"
    else:
        tag = None

    quality = _int(
        _get(video_cfg, "quality", default=_get(preset_map, "quality", default=DEFAULT_QUALITY)),
        DEFAULT_QUALITY,
    )
    look_ahead = _bool(
        _get(
            video_cfg,
            "look_ahead",
            default=_get(preset_map, "look_ahead", default=True),
        ),
        True,
    )

    audio_choices, audio_warnings = select_audio(
        probe_result.audio_streams,
        audio_cfg,
        effective_container,
        source_had_audio=bool(probe_result.audio_streams),
    )
    warnings.extend(audio_warnings)

    source_name = _resolve_name(identity, path, config_map)
    stem = source_name["stem"]
    if name:
        stem = str(name)
        if _import_naming() is not None:
            try:
                from .naming import sanitize_component

                stem = sanitize_component(stem)
            except Exception:
                stem = _fallback_sanitize(stem) or source_name["stem"]
        else:
            stem = _fallback_sanitize(stem) or source_name["stem"]
        source_name["stem"] = stem
    ext = effective_container
    destination_rel = f"{stem}/{stem}.{ext}"

    if speed_factor is not None:
        factor = _float(speed_factor, 0.0)
    else:
        factor = 0.0
    if estimated_seconds is not None:
        estimate = float(estimated_seconds)
    elif factor > 0:
        estimate = duration / factor
    else:
        estimate = None
    speed_source = "table" if factor > 0 else "missing"

    if fallback_from is None and plan_version > 1 and action == "remux":
        fallback_from = "encode"
    if fallback_from not in {None, "encode", "remux"}:
        raise PlanError(f"unsupported fallback_from: {fallback_from!r}")

    plan = {
        "schema": 1,
        "preset": _preset_id(preset_map),
        "action": action,
        "container": effective_container,
        "plan_version": int(plan_version),
        "fallback_from": fallback_from,
        "source": {
            "path": str(path),
            "format": probe_result.format_name,
            "duration": duration,
            "size_bytes": probe_result.size,
            "video_bytes": source_video_bytes,
            "main_video_index": _stream_index(main_video),
            "mtime": _source_mtime(path),
        },
        "video": {
            "index": _stream_index(main_video),
            "codec": output_codec,
            "width": source_width,
            "height": source_height,
            "target_width": target_width,
            "target_height": target_height,
            "pix_fmt": target_pix_fmt,
            "bit_depth": target_bit_depth,
            "sar": _sar(main_video),
            "frame_rate": _frame_rate(main_video),
            "field_order": str(main_video.get("field_order") or "progressive"),
            "hdr": bool(flags["hdr"]),
            "dolby_vision": bool(flags["dolby_vision"]),
            "color": _colour(main_video),
            "backend": backend,
            "encoder": encoder,
            "quality": quality,
            "look_ahead": look_ahead,
            "tag": tag,
            "scaled": scaled,
        },
        "audio": audio_choices,
        "subtitles": subtitle_choices,
        "sidecars": _plan_sidecars(path),
        "chapters": True,
        "attachments": effective_container == "mkv",
        "name": source_name,
        "ext": ext,
        "destination_rel": destination_rel,
        "dry_run": _config_dry_run(config_map),
        "warnings": warnings,
        "dropped": dropped,
        "estimated_seconds": estimate,
        "speed_source": speed_source,
        "review": None,
    }
    return plan


def _source_mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0
