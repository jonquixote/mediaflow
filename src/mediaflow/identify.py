"""TMDb-backed movie identity resolution.

[SAF-10] The TMDb token is only ever placed in an ``Authorization: Bearer``
         header.  It is never put in a URL, a cache key, a candidate, a reason
         or a logged message, and error text is redacted before it is exposed.
[REV-1]  Every ambiguous result is a REVIEW with its origin recorded as
         ``identity-tie`` or ``identity-low-confidence``; only an unambiguous
         exact title/year match becomes AUTO.

The module deliberately knows nothing about SQLite or the pipeline database.
It accepts any object implementing the small cache protocol below, which keeps
it testable without network access and refuses directories.
"""

from __future__ import annotations

import inspect
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .naming import ParsedName, parse_media_name

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
AUTO_TITLE_THRESHOLD = 0.90
TIE_EPSILON = 1e-9
MIN_YEAR = 1870

# Latin letters NFKD does not decompose.  These are transliterated so a TMDb
# title using an ASCII spelling still matches a release name using the
# traditional letter.  Non-Latin scripts are never transliterated.
_LATIN_TRANSLITERATION = str.maketrans(
    {
        "Æ": "AE",
        "æ": "ae",
        "Ð": "D",
        "ð": "d",
        "Đ": "D",
        "đ": "d",
        "Ł": "L",
        "ł": "l",
        "Ø": "O",
        "ø": "o",
        "Œ": "OE",
        "œ": "oe",
        "Þ": "TH",
        "þ": "th",
        "ı": "i",
    }
)


@runtime_checkable
class TmdbCache(Protocol):
    """Minimal cache protocol; no database types leak into this module."""

    def get_tmdb(self, key: str) -> dict[str, Any] | None: ...

    def put_tmdb(self, key: str, value: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class TmdbCandidate:
    """One TMDb search hit with its deterministic title score."""

    tmdb_id: int
    title: str
    original_title: str
    year: int | None
    popularity: float = 0.0
    score: float = 0.0


@dataclass(slots=True)
class IdentityResult:
    """Identity decision returned to the planner or CLI.

    ``source`` is ``"tmdb"`` for a fresh lookup, ``"cache"`` for a previously
    recorded response, and ``"parse-only"`` when no lookup was possible.
    ``candidates`` is always the deterministic top three (or fewer).
    """

    status: Literal["auto", "review"]
    title: str
    year: int | None
    tmdb_id: int | None
    confidence: float
    reason: str
    candidates: list[TmdbCandidate] = field(default_factory=list)
    source: Literal["tmdb", "parse-only", "cache"] = "parse-only"
    warning: str | None = None


def _fold_title(value: str) -> str:
    """Case-fold and strip punctuation while preserving non-Latin scripts.

    NFKD decomposes Latin accents so ``Amélie`` becomes ``amelie``.  Combining
    marks are only removed when they follow a Latin base character; marks in
    Cyrillic, Indic, Arabic and other scripts are data, not decoration.
    """

    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value).translate(_LATIN_TRANSLITERATION)
    out: list[str] = []
    previous_was_latin = False
    for char in decomposed:
        if char == "&":
            out.append(" and ")
            previous_was_latin = False
        elif unicodedata.combining(char):
            if not previous_was_latin:
                out.append(char)
        elif unicodedata.category(char)[0] in {"L", "N"}:
            out.append(char.casefold())
            previous_was_latin = "LATIN" in unicodedata.name(char, "")
        elif char in "'\u2019\u02bc`.":
            # Apostrophes and abbreviation dots join rather than split words:
            # Schindler's -> schindlers, S.W.A.T. -> swat.
            previous_was_latin = False
        else:
            out.append(" ")
            previous_was_latin = False
    return re.sub(r"\s+", " ", "".join(out)).strip()


def title_similarity(a: str, b: str) -> float:
    """Return a normalised similarity in ``[0.0, 1.0]``.

    Matching ignores case, punctuation, Latin diacritics and separator style,
    treats ``&`` as ``and``, and leaves non-Latin text intact.
    """

    if not isinstance(a, str) or not isinstance(b, str):
        raise TypeError("titles must be strings")
    left = _fold_title(a)
    right = _fold_title(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _canonical_query(title: str) -> str:
    text = unicodedata.normalize("NFC", title)
    return re.sub(r"\s+", " ", text).strip()


def tmdb_cache_key(title: str, year: int | None) -> str:
    """Return the canonical cache key: normalised query plus year.

    Case, whitespace, punctuation, separator style and Latin diacritics do not
    matter, so ``"The  Matrix"`` and ``"the matrix"`` share one cache entry.
    The key never contains a token.
    """

    query = _fold_title(_canonical_query(title))
    return f"tmdb:search:v1:{query}|{'' if year is None else int(year)}"


def _valid_year(year: object) -> bool:
    if isinstance(year, bool) or not isinstance(year, int):
        return False
    return MIN_YEAR <= year <= date.today().year + 2


def _clear_title(title: str) -> bool:
    return bool(title.strip()) and any(char.isalnum() for char in title)


def _coerce_parsed(parsed: ParsedName | str | Mapping[str, Any]) -> ParsedName:
    if isinstance(parsed, ParsedName):
        return parsed
    if isinstance(parsed, str):
        return parse_media_name(parsed)
    if isinstance(parsed, Mapping):
        title = parsed.get("title")
        year = parsed.get("year")
        if isinstance(year, str) and year.isdigit():
            year = int(year)
        if not isinstance(year, int) or isinstance(year, bool):
            year = None
        return ParsedName(
            title=str(title) if title is not None else "",
            year=year,
        )
    raise TypeError("parsed must be a ParsedName, string or mapping")


def _redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(api[_-]?key\s*[=:]\s*)[^\s&,;]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def _safe_error(exc: BaseException, token: str | None) -> str:
    message = _redact(str(exc), token)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _candidate_year(raw: Mapping[str, Any]) -> int | None:
    value = raw.get("release_date")
    if isinstance(value, str):
        match = re.match(r"\s*(\d{4})", value)
        if match:
            year = int(match.group(1))
            if MIN_YEAR <= year <= 2099:
                return year
    value = raw.get("year")
    if isinstance(value, int) and not isinstance(value, bool) and MIN_YEAR <= value <= 2099:
        return value
    if isinstance(value, str) and value.isdigit() and MIN_YEAR <= int(value) <= 2099:
        return int(value)
    return None


def _candidate_from_raw(raw: Mapping[str, Any], parsed_title: str) -> TmdbCandidate | None:
    raw_id = raw.get("id", raw.get("tmdb_id"))
    if isinstance(raw_id, bool):
        return None
    try:
        tmdb_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if tmdb_id <= 0:
        return None

    title = raw.get("title", raw.get("name"))
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title:
        return None

    original = raw.get("original_title", raw.get("original_name", ""))
    original_title = original.strip() if isinstance(original, str) else ""

    try:
        popularity = float(raw.get("popularity", 0.0) or 0.0)
    except (TypeError, ValueError):
        popularity = 0.0

    scores = [title_similarity(parsed_title, title)]
    if original_title and original_title.casefold() != title.casefold():
        scores.append(title_similarity(parsed_title, original_title))

    return TmdbCandidate(
        tmdb_id=tmdb_id,
        title=title,
        original_title=original_title,
        year=_candidate_year(raw),
        popularity=popularity,
        score=max(scores),
    )


def _normalise_payload(payload: Any) -> dict[str, Any]:
    data = payload
    if hasattr(data, "json") and not isinstance(data, Mapping):
        data = data.json()
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, list):
        return {"results": data}
    if not isinstance(data, Mapping):
        raise ValueError("TMDb response is not an object")
    result = dict(data)
    if "results" in result and not isinstance(result["results"], list):
        raise ValueError("TMDb results is not a list")
    if "results" not in result and "id" in result:
        result = {"results": [result]}
    if "results" not in result:
        raise ValueError("TMDb response has no results")
    return result


def _sorted_candidates(payload: Mapping[str, Any], parsed_title: str) -> list[TmdbCandidate]:
    results = payload.get("results", [])
    candidates: list[TmdbCandidate] = []
    for raw in results if isinstance(results, list) else []:
        if not isinstance(raw, Mapping):
            continue
        candidate = _candidate_from_raw(raw, parsed_title)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (-item.score, -item.popularity, item.tmdb_id))
    return candidates


def _review_reason(candidates: Sequence[TmdbCandidate]) -> str:
    if not candidates:
        return "identity-low-confidence"
    top = max(candidate.score for candidate in candidates)
    if top <= 0.0:
        return "identity-low-confidence"
    tied = sum(1 for candidate in candidates if abs(candidate.score - top) <= TIE_EPSILON)
    return "identity-tie" if tied >= 2 else "identity-low-confidence"


def _review(
    parsed: ParsedName,
    candidates: Sequence[TmdbCandidate],
    source: Literal["tmdb", "parse-only", "cache"],
    warning: str | None,
) -> IdentityResult:
    top = list(candidates[:3])
    confidence = top[0].score if top else 0.0
    return IdentityResult(
        status="review",
        title=parsed.title,
        year=parsed.year,
        tmdb_id=None,
        confidence=confidence,
        reason=_review_reason(candidates),
        candidates=top,
        source=source,
        warning=warning,
    )


def _auto(
    parsed: ParsedName,
    candidate: TmdbCandidate,
    top: Sequence[TmdbCandidate],
    source: Literal["tmdb", "parse-only", "cache"],
    reason: str,
    warning: str | None = None,
) -> IdentityResult:
    return IdentityResult(
        status="auto",
        title=candidate.title or parsed.title,
        year=candidate.year,
        tmdb_id=candidate.tmdb_id,
        confidence=candidate.score,
        reason=reason,
        candidates=list(top[:3]),
        source=source,
        warning=warning,
    )


def _decide(
    parsed: ParsedName,
    payload: Mapping[str, Any],
    source: Literal["tmdb", "cache"],
    warning: str | None = None,
) -> IdentityResult:
    candidates = _sorted_candidates(payload, parsed.title)
    top = list(candidates[:3])
    if not candidates:
        return _review(parsed, candidates, source, warning)

    if parsed.year is None:
        qualifying = [candidate for candidate in candidates if candidate.score >= AUTO_TITLE_THRESHOLD]
        if len(qualifying) == 1:
            return _auto(
                parsed,
                qualifying[0],
                top,
                source,
                reason="single-title-match",
                warning=warning,
            )
        return _review(parsed, candidates, source, warning)

    qualifying = [
        candidate
        for candidate in candidates
        if candidate.score >= AUTO_TITLE_THRESHOLD and candidate.year == parsed.year
    ]
    if len(qualifying) == 1:
        return _auto(
            parsed,
            qualifying[0],
            top,
            source,
            reason="title-and-year-match",
            warning=warning,
        )
    return _review(parsed, candidates, source, warning)


def _parse_only(parsed: ParsedName, warning: str) -> IdentityResult:
    if _clear_title(parsed.title) and _valid_year(parsed.year):
        return IdentityResult(
            status="auto",
            title=parsed.title,
            year=parsed.year,
            tmdb_id=None,
            confidence=0.5,
            reason="parse-only",
            candidates=[],
            source="parse-only",
            warning=warning,
        )
    return IdentityResult(
        status="review",
        title=parsed.title,
        year=parsed.year,
        tmdb_id=None,
        confidence=0.0,
        reason="identity-low-confidence",
        candidates=[],
        source="parse-only",
        warning=warning,
    )


def _default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> Any:
    request = Request(url, headers=dict(headers), method="GET")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS URL
        return json.loads(response.read().decode("utf-8"))


def _call_http_get(
    http_get: Callable[..., Any],
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> Any:
    """Call a caller-supplied HTTP function without assuming one signature.

    Tests commonly use ``lambda url, headers, timeout: ...`` while production
    code may use keyword-only arguments.  Introspection keeps both forms
    supported without retrying a request that failed internally.
    """

    try:
        signature = inspect.signature(http_get)
    except (TypeError, ValueError):
        return http_get(url, headers, timeout)

    parameters = signature.parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_kwargs or {"headers", "timeout"} <= set(parameters):
        return http_get(url, headers=headers, timeout=timeout)

    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 3:
        return http_get(url, headers, timeout)
    if len(positional) == 2:
        return http_get(url, headers)
    return http_get(url)


def _search_url(query: str, year: int | None) -> str:
    parameters: list[tuple[str, str]] = [("query", query)]
    if year is not None:
        parameters.append(("year", str(year)))
    # [SAF-10] Only the query and year are in the URL.  The token is a header.
    return f"{TMDB_SEARCH_URL}?{urlencode(parameters)}"


def resolve_identity(
    parsed: ParsedName | str | Mapping[str, Any],
    token: str | None,
    cache: TmdbCache | None,
    http_get: Callable[..., Any] | None = None,
    timeout: float = 5.0,
) -> IdentityResult:
    """Resolve a parsed movie name to a TMDb identity.

    [SAF-10] ``token`` is sent only as ``Authorization: Bearer``.  It is never
    included in the URL, cache key, result or warning.  A cached response is
    reused even when the token is no longer available.

    [REV-1] Auto acceptance requires an unambiguous title match at or above
    ``0.90`` and an exact year match when the filename supplied a year.  With
    no parsed year, exactly one strong title match is accepted.  Ties and all
    other ambiguity become REVIEW with the top three candidates.
    """

    parsed_name = _coerce_parsed(parsed)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be a positive number")

    clean_token = token.strip() if isinstance(token, str) else ""
    query = _canonical_query(parsed_name.title)

    if not query or not _clear_title(query):
        return IdentityResult(
            status="review",
            title=parsed_name.title,
            year=parsed_name.year,
            tmdb_id=None,
            confidence=0.0,
            reason="identity-low-confidence",
            candidates=[],
            source="parse-only",
            warning="No usable title to search for.",
        )

    key = tmdb_cache_key(query, parsed_name.year)

    cached_payload: dict[str, Any] | None = None
    if cache is not None:
        try:
            raw_cached = cache.get_tmdb(key)
            if raw_cached is not None:
                cached_payload = _normalise_payload(raw_cached)
        except Exception:
            # A broken cache must not prevent a fresh lookup.  Errors are not
            # exposed here because they may contain implementation detail.
            cached_payload = None

    if cached_payload is not None:
        return _decide(parsed_name, cached_payload, source="cache")

    if clean_token:
        url = _search_url(query, parsed_name.year)
        headers = {
            "Authorization": f"Bearer {clean_token}",
            "Accept": "application/json",
        }
        getter = http_get or _default_http_get
        try:
            raw_response = _call_http_get(getter, url, headers, float(timeout))
            payload = _normalise_payload(raw_response)
        except Exception as exc:
            warning = _redact(
                f"TMDb lookup failed ({_safe_error(exc, clean_token)}); "
                "using parsed title and year.",
                clean_token,
            )
            return _parse_only(parsed_name, warning)

        if cache is not None:
            try:
                cache.put_tmdb(key, payload)
            except Exception:
                pass
        return _decide(parsed_name, payload, source="tmdb")

    warning = "TMDb token unavailable; using parsed title and year."
    return _parse_only(parsed_name, warning)


def resolve_manual(
    candidates: Sequence[TmdbCandidate],
    title: str,
    year: int | None,
    candidate_index: int,
) -> IdentityResult:
    """Resolve a REVIEW by explicitly choosing a candidate.

    ``candidate_index`` is a zero-based index into the candidate list shown in
    the review (the CLI can display it as one-based).  The supplied title/year
    are the accepted overrides; an omitted year uses the candidate's year.
    Invalid types, empty titles, out-of-range years and bad indexes raise
    ``ValueError``/``TypeError`` so a bad CLI accept cannot silently publish.
    """

    if isinstance(candidates, (str, bytes)):
        raise TypeError("candidates must be a sequence of TmdbCandidate")
    try:
        items = list(candidates)
    except TypeError as exc:
        raise TypeError("candidates must be a sequence of TmdbCandidate") from exc
    if not items:
        raise ValueError("at least one candidate is required")
    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
        raise TypeError("candidate_index must be an integer")
    if not 0 <= candidate_index < len(items):
        raise ValueError("candidate_index is out of range")

    selected = items[candidate_index]
    if not isinstance(selected, TmdbCandidate):
        raise TypeError("candidates must contain TmdbCandidate objects")
    if not isinstance(selected.tmdb_id, int) or isinstance(selected.tmdb_id, bool) or selected.tmdb_id <= 0:
        raise ValueError("selected candidate has an invalid TMDb id")

    if not isinstance(title, str) or not _clear_title(title):
        raise ValueError("title must contain usable text")
    if year is not None and not _valid_year(year):
        raise ValueError(f"year must be between {MIN_YEAR} and the current year + 2")

    accepted_year = year if year is not None else selected.year
    if not _valid_year(accepted_year):
        raise ValueError(f"year must be between {MIN_YEAR} and the current year + 2")
    return IdentityResult(
        status="auto",
        title=title.strip(),
        year=accepted_year,
        tmdb_id=selected.tmdb_id,
        confidence=1.0,
        reason="manual-accept",
        candidates=list(items),
        source="tmdb",
        warning=None,
    )


__all__ = [
    "AUTO_TITLE_THRESHOLD",
    "TIE_EPSILON",
    "IdentityResult",
    "TmdbCache",
    "TmdbCandidate",
    "resolve_identity",
    "resolve_manual",
    "title_similarity",
    "tmdb_cache_key",
]
