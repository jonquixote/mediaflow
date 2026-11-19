"""Movie name parsing, sanitising and formatting.

This module is deliberately independent of TMDb and of the rest of the
pipeline.  It turns a messy download name into a stable, machine-readable
:class:`ParsedName`, sanitises a single path component so it is safe on the
library filesystem, and formats the canonical ``Title (Year) [tmdbid-N]``
name used for both the library folder and the media file stem.

[SAF-10] Nothing here reads or logs credentials.  Parsing is local only.
[REV-1]  Ambiguous or incomplete names are never guessed silently; callers can
         use the ``is_extra``/``is_sample``/``is_multidisc`` flags to route a
         file to REVIEW before any library path is chosen.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from guessit import guessit as _guessit_library

MAX_COMPONENT_BYTES = 200
MIN_YEAR = 1870

#: Extensions we recognise as media or sidecar containers.  The last known
#: suffix wins, so ``Film.2019.mkv.en.srt`` is a subtitle (``srt``) while
#: ``Film.2019.mkv.part`` is still a Matroska file.
_KNOWN_CONTAINERS = frozenset(
    {
        "3gp",
        "ass",
        "avi",
        "divx",
        "flv",
        "idx",
        "iso",
        "m2ts",
        "m4v",
        "mkv",
        "mov",
        "mp4",
        "mpeg",
        "mpg",
        "mts",
        "ogm",
        "ogv",
        "srt",
        "ssa",
        "sub",
        "sup",
        "ts",
        "vob",
        "vtt",
        "webm",
        "wmv",
    }
)

# Sidecar language modifiers which may sit between the language and ``.srt``.
_LANGUAGE_MODIFIERS = frozenset(
    {
        "cc",
        "commentary",
        "default",
        "dub",
        "dubbed",
        "foreign",
        "forced",
        "full",
        "hearing",
        "hearing-impaired",
        "hi",
        "hoh",
        "impaired",
        "normal",
        "sdh",
        "signs",
        "songs",
        "sub",
        "subs",
        "subtitle",
        "subtitles",
    }
)

_ISO_639_1 = frozenset(
    """
    aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch
    co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga
    gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja
    jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv
    mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or
    os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr
    ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi
    vo wa wo xh yi yo za zh zu
    """.split()
)

# ISO 639-2/B and /T codes commonly seen in release names, mapped to their
# ISO 639-1 form.  Codes without a two-letter form are kept in _ISO_639_3.
_ISO_639_3_TO_1 = {
    "aar": "aa",
    "abk": "ab",
    "afr": "af",
    "aka": "ak",
    "alb": "sq",
    "amh": "am",
    "ara": "ar",
    "arm": "hy",
    "asm": "as",
    "ava": "av",
    "ave": "ae",
    "aym": "ay",
    "aze": "az",
    "bak": "ba",
    "bam": "bm",
    "baq": "eu",
    "bel": "be",
    "ben": "bn",
    "bih": "bh",
    "bis": "bi",
    "bod": "bo",
    "bos": "bs",
    "bre": "br",
    "bul": "bg",
    "bur": "my",
    "cat": "ca",
    "ces": "cs",
    "cha": "ch",
    "che": "ce",
    "chi": "zh",
    "chu": "cu",
    "chv": "cv",
    "cor": "kw",
    "cos": "co",
    "cre": "cr",
    "cym": "cy",
    "cze": "cs",
    "dan": "da",
    "deu": "de",
    "div": "dv",
    "dut": "nl",
    "dzo": "dz",
    "ell": "el",
    "eng": "en",
    "epo": "eo",
    "est": "et",
    "eus": "eu",
    "ewe": "ee",
    "fao": "fo",
    "fas": "fa",
    "fij": "fj",
    "fil": "tl",
    "fin": "fi",
    "fra": "fr",
    "fre": "fr",
    "fry": "fy",
    "geo": "ka",
    "ger": "de",
    "gla": "gd",
    "gle": "ga",
    "glg": "gl",
    "glv": "gv",
    "gre": "el",
    "grn": "gn",
    "guj": "gu",
    "hat": "ht",
    "hau": "ha",
    "heb": "he",
    "her": "hz",
    "hin": "hi",
    "hmo": "ho",
    "hrv": "hr",
    "hun": "hu",
    "hye": "hy",
    "ibo": "ig",
    "ice": "is",
    "ido": "io",
    "iii": "ii",
    "iku": "iu",
    "ile": "ie",
    "ina": "ia",
    "ind": "id",
    "ipk": "ik",
    "isl": "is",
    "ita": "it",
    "jav": "jv",
    "jpn": "ja",
    "kal": "kl",
    "kan": "kn",
    "kas": "ks",
    "kat": "ka",
    "kau": "kr",
    "kaz": "kk",
    "khm": "km",
    "kik": "ki",
    "kin": "rw",
    "kir": "ky",
    "kom": "kv",
    "kon": "kg",
    "kor": "ko",
    "kua": "kj",
    "kur": "ku",
    "lao": "lo",
    "lat": "la",
    "lav": "lv",
    "lim": "li",
    "lin": "ln",
    "lit": "lt",
    "ltz": "lb",
    "lub": "lu",
    "lug": "lg",
    "mac": "mk",
    "mah": "mh",
    "mal": "ml",
    "mao": "mi",
    "mar": "mr",
    "may": "ms",
    "mkd": "mk",
    "mlg": "mg",
    "mlt": "mt",
    "mon": "mn",
    "mri": "mi",
    "msa": "ms",
    "mul": "mul",
    "mya": "my",
    "nau": "na",
    "nav": "nv",
    "nbl": "nr",
    "nde": "nd",
    "ndo": "ng",
    "nep": "ne",
    "nld": "nl",
    "nno": "nn",
    "nob": "nb",
    "nor": "no",
    "nya": "ny",
    "oci": "oc",
    "oji": "oj",
    "ori": "or",
    "orm": "om",
    "oss": "os",
    "pan": "pa",
    "per": "fa",
    "pol": "pl",
    "por": "pt",
    "pus": "ps",
    "que": "qu",
    "roh": "rm",
    "ron": "ro",
    "rum": "ro",
    "run": "rn",
    "rus": "ru",
    "sag": "sg",
    "san": "sa",
    "sin": "si",
    "slk": "sk",
    "slo": "sk",
    "slv": "sl",
    "sme": "se",
    "smo": "sm",
    "sna": "sn",
    "snd": "sd",
    "som": "so",
    "sot": "st",
    "spa": "es",
    "sqi": "sq",
    "srd": "sc",
    "srp": "sr",
    "ssw": "ss",
    "sun": "su",
    "swa": "sw",
    "swe": "sv",
    "tah": "ty",
    "tam": "ta",
    "tat": "tt",
    "tel": "te",
    "tgk": "tg",
    "tgl": "tl",
    "tha": "th",
    "tib": "bo",
    "tir": "ti",
    "ton": "to",
    "tsn": "tn",
    "tso": "ts",
    "tuk": "tk",
    "tur": "tr",
    "twi": "tw",
    "uig": "ug",
    "ukr": "uk",
    "urd": "ur",
    "uzb": "uz",
    "ven": "ve",
    "vie": "vi",
    "vol": "vo",
    "wel": "cy",
    "wln": "wa",
    "wol": "wo",
    "xho": "xh",
    "yid": "yi",
    "yor": "yo",
    "zha": "za",
    "zho": "zh",
    "zul": "zu",
}

_ISO_639_3_EXTRA = frozenset({"ast", "gsw", "hbs", "sco", "yue"})

# A few English language names are common in hand-made sidecars.
_LANGUAGE_NAMES = {
    "arabic": "ar",
    "chinese": "zh",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "norwegian": "no",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "spanish": "es",
    "swedish": "sv",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "vietnamese": "vi",
}

_EXTRA_LABELS = {
    "behind the scenes": "Behind the Scenes",
    "bonus feature": "Bonus Features",
    "bonus features": "Bonus Features",
    "bonus material": "Bonus Material",
    "bonus": "Bonus Features",
    "behind the scene": "Behind the Scenes",
    "blooper": "Bloopers",
    "bloopers": "Bloopers",
    "clip": "Clips",
    "clips": "Clips",
    "commentary": "Commentary",
    "deleted scene": "Deleted Scenes",
    "deleted scenes": "Deleted Scenes",
    "documentary": "Documentary",
    "extra": "Extras",
    "extras": "Extras",
    "featurette": "Featurettes",
    "featurettes": "Featurettes",
    "interview": "Interviews",
    "interviews": "Interviews",
    "making of": "Making Of",
    "music video": "Music Video",
    "outtake": "Outtakes",
    "outtakes": "Outtakes",
    "other": "Other",
    "promo": "Promo",
    "sample": "Sample",
    "samples": "Sample",
    "scene": "Scenes",
    "scenes": "Scenes",
    "short": "Shorts",
    "shorts": "Shorts",
    "special feature": "Special Features",
    "special features": "Special Features",
    "teaser": "Teaser",
    "trailer": "Trailers",
    "trailers": "Trailers",
    "webisode": "Webisodes",
    "webisodes": "Webisodes",
}
_EXTRA_KEYWORD_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(sorted((re.escape(k) for k in _EXTRA_LABELS), key=len, reverse=True))
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)
_FOLDER_EXTRA_RE = re.compile(
    r"^(?:extras?|featurettes?|behind[ ._-]*the[ ._-]*scenes?|"
    r"bonus(?:[ ._-]+(?:features?|material|content))?|"
    r"special[ ._-]+features?|deleted[ ._-]+scenes?|interviews?|bloopers?|outtakes?|"
    r"trailers?|teasers?|samples?|shorts?|clips?|scenes?|commentary|promos?|"
    r"making[ ._-]+of|webisodes?|other)$",
    re.IGNORECASE,
)
_MULTIDISC_RE = re.compile(
    r"(?<![a-z0-9])(?:disc|disk|dvd|cd)[ ._-]*(\d{1,2})(?:[ ._-]*(?:of|/)[ ._-]*\d{1,2})?(?![a-z0-9])",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_RELEASE_TAG_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:"
    r"2160p|1080p|1080i|720p|576p|480p|4k|uhd|bluray|blu-ray|brrip|bdrip|webrip|"
    r"web-dl|webdl|hdtv|dvdrip|dvdscr|hdrip|x264|x265|h264|h265|hevc|avc|"
    r"aac|ac3|eac3|ddp|dts|truehd|atmos|remux|proper|repack|extended"
    r")(?![a-z0-9])"
)


@dataclass(slots=True)
class ParsedName:
    """The useful parts of a movie filename.

    ``release_noise`` holds guessit's normalised release tags (resolution,
    source, codecs, release group and similar).  It is ordered and deduplicated
    so tests and manifests are deterministic.  ``extra`` is the human label of
    an extra when one was detected (for example ``"Behind the Scenes"``).
    """

    title: str
    year: int | None = None
    container: str | None = None
    release_noise: list[str] = field(default_factory=list)
    extra: str | None = None
    kind: Literal["movie", "extra", "multidisc"] = "movie"
    is_multidisc: bool = False
    is_extra: bool = False
    is_sample: bool = False
    disc_number: int | None = None
    tmdb_id: int | None = None


def _max_release_year() -> int:
    """Latest year accepted by :func:`format_media_name` (current year + 2)."""

    return date.today().year + 2


def _clean_title(value: str) -> str:
    text = unicodedata.normalize("NFC", str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ._-\u2013\u2014")


def _split_path(name: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", name) if part not in ("", ".")]


def _container_from_filename(base: str, guessed: object) -> str | None:
    parts = base.split(".")
    if len(parts) > 1:
        for part in reversed(parts[1:]):
            candidate = part.casefold()
            if candidate in _KNOWN_CONTAINERS:
                return candidate

    if isinstance(guessed, str):
        candidate = guessed.casefold()
        if candidate in _KNOWN_CONTAINERS:
            return candidate
    elif isinstance(guessed, (list, tuple)):
        for item in reversed(guessed):
            candidate = str(item).casefold()
            if candidate in _KNOWN_CONTAINERS:
                return candidate
    return None


def _release_noise(guessed: dict[str, object]) -> list[str]:
    fields = (
        "screen_size",
        "source",
        "video_codec",
        "audio_codec",
        "audio_profile",
        "audio_channels",
        "release_group",
        "edition",
        "other",
        "streaming_service",
        "aspect_ratio",
        "bit_depth",
        "frame_rate",
    )
    noise: list[str] = []
    seen: set[str] = set()
    for key in fields:
        value = guessed.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if item is None:
                continue
            text = _clean_title(str(item))
            folded = text.casefold()
            if not text or folded in seen:
                continue
            seen.add(folded)
            noise.append(text)
    return noise


def _year_occurrences(stem: str, year: int | None) -> list[re.Match[str]]:
    if year is None:
        return []
    return [match for match in _YEAR_RE.finditer(stem) if int(match.group(1)) == year]


def _find_disc_number(text: str) -> int | None:
    found: int | None = None
    for match in _MULTIDISC_RE.finditer(text):
        number = int(match.group(1))
        marker = match.group(0).casefold()
        # DVD5/DVD9/DVD10/DVD18 are disc *formats*, not disc numbers.
        if (
            re.match(r"dvd[ ._-]*\d", marker)
            and number in {5, 9, 10, 18}
            and " of " not in marker
            and "/" not in marker
        ):
            continue
        found = number
    return found


def _repair_dropped_title_year(title: str, stem: str, year: int | None) -> str:
    """Re-attach a year-like word guessit mistook for an episode number.

    ``Blade Runner 2049 (2017)`` is sometimes parsed as title ``Blade Runner``
    with episode ``2049``.  When a four-digit word immediately precedes the
    release year we keep it in the title.  Real release years (``1984 (1984)``,
    ``2012 (2009)``) are excluded by the equality and membership checks.
    """

    if not title or year is None:
        return title
    matches = _year_occurrences(stem, year)
    if not matches:
        return title
    prefix = stem[: matches[-1].start()].rstrip(" ._-([{")
    token_match = re.search(r"(\d{4})$", prefix)
    if not token_match:
        return title
    token = token_match.group(1)
    value = int(token)
    if value < MIN_YEAR or value > 2099 or value == year:
        return title
    if token in title.split():
        return title
    return _clean_title(f"{title} {token}")


def _tail_after_year(stem: str, year: int | None) -> str:
    matches = _year_occurrences(stem, year)
    tail = stem[matches[-1].end() :] if matches else ""
    return tail.strip(" ._-[]{}()")


def _match_extra(text: str) -> str | None:
    normalised = re.sub(r"[._-]+", " ", text)
    match = _EXTRA_KEYWORD_RE.search(normalised)
    if not match:
        return None
    return _EXTRA_LABELS.get(re.sub(r"\s+", " ", match.group(0).casefold()).strip())


def _bare_extra_label(stem: str) -> str | None:
    """Recognise a filename that is itself an extra, not a movie title."""

    normalised = re.sub(r"[._-]+", " ", stem).casefold()
    normalised = re.sub(r"\s+", " ", normalised).strip()
    match = _EXTRA_KEYWORD_RE.match(normalised)
    if not match:
        return None
    remainder = normalised[match.end() :].strip()
    if remainder and not re.fullmatch(r"\d{1,3}(?:\s*(?:of|/)\s*\d{1,3})?", remainder):
        return None
    return _EXTRA_LABELS.get(re.sub(r"\s+", " ", match.group(0)))


def _match_folder_extra(folders: list[str]) -> str | None:
    found: str | None = None
    for folder in folders:
        if _FOLDER_EXTRA_RE.match(folder.strip()):
            found = _EXTRA_LABELS.get(
                re.sub(r"[ ._-]+", " ", folder.strip().casefold()).removeprefix("the ").strip()
            ) or _clean_title(folder)
    return found


def _match_sample_folder(folders: list[str]) -> bool:
    return any(
        re.sub(r"[ ._-]+", " ", folder.strip().casefold()) in {"sample", "samples"}
        for folder in folders
    )


def _guessit(name: str) -> dict[str, object]:
    try:
        return dict(_guessit_library(name))
    except Exception:
        # Parsing must never make a file unprocessable.  Callers get an empty
        # parse and the identity code routes it to REVIEW.
        return {}


def _guess_release_year(guessed: dict[str, object]) -> int | None:
    raw = guessed.get("year")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        return None
    if MIN_YEAR <= value <= 2099:
        return value
    return None


def _guess_tmdb_id(guessed: dict[str, object]) -> int | None:
    raw = guessed.get("tmdb_id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def _fallback_title(stem: str) -> str:
    text = re.sub(r"\[[^\]]*]|\{[^}]*}", " ", stem)
    text = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", text, count=1)
    text = _RELEASE_TAG_RE.sub(" ", text)
    text = re.sub(r"[._]+", " ", text)
    return _clean_title(text)


def parse_media_name(name: str) -> ParsedName:
    """Parse a movie filename or relative path without contacting TMDb.

    The parser tolerates absolute paths, Windows separators, subtitle names,
    release tags, multi-disc markers and extras folders.  It is intentionally
    conservative: flags are set when a marker is unambiguous, otherwise the
    filename is treated as a normal movie.
    """

    if not isinstance(name, str):
        raise TypeError("name must be a string")

    normalised = unicodedata.normalize("NFC", name)
    parts = _split_path(normalised)
    base = parts[-1] if parts else normalised
    folders = parts[:-1]

    guessed = _guessit(normalised.replace("\\", "/"))
    year = _guess_release_year(guessed)

    raw_title = guessed.get("title")
    title = _clean_title(raw_title) if isinstance(raw_title, str) else ""
    stem = base.rsplit(".", 1)[0] if "." in base else base
    title = _repair_dropped_title_year(title, stem, year)
    if not title and not folders:
        title = _fallback_title(stem)

    container = _container_from_filename(base, guessed.get("container"))
    noise = _release_noise(guessed)

    folder_extra = _match_folder_extra(folders)
    stem_tail = _tail_after_year(stem, year)
    stem_extra = _match_extra(stem_tail) if stem_tail else None
    bare_extra = _bare_extra_label(stem) if year is None else None
    if bare_extra and not stem_extra:
        stem_extra = bare_extra
    if folder_extra and not stem_extra:
        # A descriptive filename inside an Extras/Featurettes folder is more
        # useful than the folder name itself.
        stem_extra = _match_extra(stem)
    extra = stem_extra or folder_extra

    other = guessed.get("other")
    other_values = other if isinstance(other, (list, tuple)) else [other]
    other_text = " ".join(str(value) for value in other_values if value is not None)
    other_extra = _match_extra(other_text)
    if other_extra and not extra:
        extra = other_extra
    elif other_extra and other_extra.casefold() == "sample":
        extra = extra or other_extra

    is_sample = (
        _match_sample_folder(folders)
        or (other_extra is not None and other_extra.casefold() == "sample")
        or (stem_tail != "" and (_match_extra(stem_tail) or "").casefold() == "sample")
        or (bare_extra is not None and bare_extra.casefold() == "sample")
        or base.casefold().endswith(".sample")
    )
    if is_sample:
        extra = "Sample" if not extra or extra.casefold() != "sample" else extra

    is_extra = bool(extra) or is_sample

    disc_number = _find_disc_number(normalised.replace("\\", "/"))
    is_multidisc = disc_number is not None

    if is_extra:
        kind: Literal["movie", "extra", "multidisc"] = "extra"
    elif is_multidisc:
        kind = "multidisc"
    else:
        kind = "movie"

    return ParsedName(
        title=title,
        year=year,
        container=container,
        release_noise=noise,
        extra=extra,
        kind=kind,
        is_multidisc=is_multidisc,
        is_extra=is_extra,
        is_sample=is_sample,
        disc_number=disc_number,
        tmdb_id=_guess_tmdb_id(guessed),
    )


def _truncate_utf8(text: str, limit: int = MAX_COMPONENT_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text

    cut = encoded[:limit].decode("utf-8", "ignore")
    # Prefer the last word boundary when it does not throw most of the value
    # away.  Long unbroken words (CJK titles, hashes) are cut at the byte limit.
    boundary = cut.rfind(" ")
    if boundary > 0 and boundary >= len(cut) * 0.6:
        cut = cut[:boundary]
    cut = cut.rstrip(" .")
    if not cut:
        cut = encoded[:limit].decode("utf-8", "ignore").strip(" .")
    return cut


def sanitize_component(value: str) -> str:
    """Return one safe filesystem path component.

    Colons become ``" -"`` (``2001: A Space Odyssey`` ->
    ``2001 - A Space Odyssey``); ``/ \\ ? * " < > |`` and control characters
    are removed; whitespace is collapsed; leading/trailing dots and spaces are
    stripped; the result is NFC-normalised and limited to 200 UTF-8 bytes
    without splitting a character.  An empty result raises :class:`ValueError`.
    """

    if not isinstance(value, str):
        raise TypeError("value must be a string")

    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"\s*:+\s*", " - ", text)
    for char in '/\\?*"<>|':
        text = text.replace(char, "")
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .")
    text = _truncate_utf8(text, MAX_COMPONENT_BYTES)
    text = unicodedata.normalize("NFC", text).strip(" .")

    if not text:
        raise ValueError("value contains no usable filename characters")
    return text


def _valid_year(year: object) -> bool:
    if isinstance(year, bool) or not isinstance(year, int):
        return False
    return MIN_YEAR <= year <= _max_release_year()


def format_media_name(
    title: str,
    year: int,
    tmdbid: int | None = None,
    append_tmdbid: bool = False,
) -> str:
    """Format the canonical library folder and file-stem name.

    Returns ``Title (Year)`` or ``Title (Year) [tmdbid-N]``.  The title is
    passed through :func:`sanitize_component`; the year must be between 1870
    and the current year + 2.  The same return value is used for both the
    library folder and the media file stem.
    """

    if not _valid_year(year):
        raise ValueError(f"year must be between {MIN_YEAR} and {_max_release_year()}")
    if tmdbid is not None:
        if isinstance(tmdbid, bool) or not isinstance(tmdbid, int) or tmdbid <= 0:
            raise ValueError("tmdbid must be a positive integer")
    if append_tmdbid and tmdbid is None:
        raise ValueError("tmdbid is required when append_tmdbid is true")

    safe_title = sanitize_component(title)
    name = f"{safe_title} ({year})"
    if append_tmdbid:
        name += f" [tmdbid-{tmdbid}]"
    return name


def _normalise_language_token(token: str) -> str | None:
    text = token.strip().strip("[](){}")
    if not text:
        return None
    lowered = text.casefold().replace("_", "-")
    if lowered in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[lowered]
    if lowered in {"und", "mul", "zxx"}:
        return None

    parts = lowered.split("-")
    if not parts or len(parts) > 3:
        return None
    base = parts[0]
    if base in _ISO_639_1:
        language = base
    elif base in _ISO_639_3_TO_1:
        language = _ISO_639_3_TO_1[base]
    elif base in _ISO_639_3_EXTRA:
        language = base
    else:
        return None
    if language in {"mul", "und"}:
        return None

    result = [language]
    for subtag in parts[1:]:
        if len(subtag) == 4 and subtag.isalpha():
            result.append(subtag.title())
        elif len(subtag) == 2 and subtag.isalpha():
            result.append(subtag.upper())
        elif len(subtag) == 3 and subtag.isdigit():
            result.append(subtag)
        else:
            # Unknown suffix: keep the valid language rather than guessing.
            break
    return "-".join(result)


def sidecar_language(filename: str) -> str | None:
    """Return the normalised language token for a subtitle sidecar.

    ``Title (Year).en.srt``, ``Title (Year).eng.forced.srt`` and
    ``Title (Year).en-US.srt`` all yield ``en``, ``en`` and ``en-US``.  A token
    is only returned when it is a recognised ISO 639 code (or a common language
    name), so release groups such as ``GRP`` are not mistaken for languages.
    """

    if not isinstance(filename, str):
        raise TypeError("filename must be a string")

    parts = _split_path(unicodedata.normalize("NFC", filename))
    base = parts[-1] if parts else filename
    tokens = base.split(".")
    if len(tokens) < 2 or tokens[-1].casefold() not in _KNOWN_CONTAINERS:
        return None
    if tokens[-1].casefold() not in {"srt", "ass", "ssa", "vtt", "sub", "idx", "sup"}:
        return None

    window = tokens[:-1][-5:]
    other_languages = [
        language
        for token in window
        if token.strip().casefold().replace("_", "-") not in _LANGUAGE_MODIFIERS
        and (language := _normalise_language_token(token)) is not None
    ]

    for token in reversed(window):
        cleaned = token.strip()
        folded = cleaned.casefold().replace("_", "-")
        if folded in _LANGUAGE_MODIFIERS:
            # "hi" is both Hindi and the hearing-impaired modifier.  It is a
            # modifier only when another language token is also present.
            if folded == "hi" and not other_languages:
                return "hi"
            continue
        language = _normalise_language_token(cleaned)
        if language is not None:
            return language
        # Once a non-modifier token is not a language, older dot-separated
        # tokens belong to the title/release name, not to the sidecar tag.
        return None
    return None


__all__ = [
    "MAX_COMPONENT_BYTES",
    "MIN_YEAR",
    "ParsedName",
    "format_media_name",
    "parse_media_name",
    "sanitize_component",
    "sidecar_language",
]
