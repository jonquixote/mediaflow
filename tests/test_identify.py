"""Tests for offline identity resolution and manual review acceptance."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from mediaflow.identify import (
    AUTO_TITLE_THRESHOLD,
    IdentityResult,
    TmdbCandidate,
    resolve_identity,
    resolve_manual,
    title_similarity,
    tmdb_cache_key,
)
from mediaflow.naming import ParsedName

TOKEN = "super-secret-token-123"


def result(
    tmdb_id: int,
    title: str,
    year: int | None,
    original_title: str = "",
    popularity: float = 0.0,
) -> dict[str, object]:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original_title or title,
        "release_date": f"{year}-01-01" if year is not None else "",
        "popularity": popularity,
    }


def payload(*items: dict[str, object]) -> dict[str, object]:
    return {"page": 1, "results": list(items), "total_results": len(items)}


class FakeCache:
    def __init__(
        self,
        data: dict[str, object] | None = None,
        *,
        get_error: Exception | None = None,
        put_error: Exception | None = None,
    ) -> None:
        self.data = data
        self.get_error = get_error
        self.put_error = put_error
        self.gets: list[str] = []
        self.puts: list[tuple[str, dict[str, object]]] = []

    def get_tmdb(self, key: str) -> dict[str, object] | None:
        self.gets.append(key)
        if self.get_error is not None:
            raise self.get_error
        return self.data

    def put_tmdb(self, key: str, value: dict[str, object]) -> None:
        self.puts.append((key, value))
        if self.put_error is not None:
            raise self.put_error


class FakeHttp:
    def __init__(self, value: object = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> object:
        self.calls.append((url, headers, timeout))
        if self.error is not None:
            raise self.error
        return self.value


def test_title_similarity_identical() -> None:
    assert title_similarity("The Matrix", "The Matrix") == 1.0


def test_title_similarity_case_and_punctuation() -> None:
    assert title_similarity("2001: A Space Odyssey", "2001 - A Space Odyssey") == 1.0
    assert title_similarity("Spider-Man", "spider man") == 1.0
    assert title_similarity("Fast & Furious", "Fast and Furious") == 1.0


def test_title_similarity_apostrophes_and_abbreviation_dots() -> None:
    assert title_similarity("Schindler's List", "Schindlers List") == 1.0
    assert title_similarity("Ocean's Eleven", "Oceans Eleven") == 1.0
    assert title_similarity("S.W.A.T.", "SWAT") == 1.0
    assert title_similarity("Mr. Robot", "Mr Robot") == 1.0


def test_title_similarity_latin_diacritics() -> None:
    assert title_similarity("Amélie", "Amelie") == 1.0
    assert title_similarity("Café", "Cafe") == 1.0


def test_title_similarity_latin_letters_without_decomposition() -> None:
    assert title_similarity("Søren", "Soren") == 1.0
    assert title_similarity("Bjørn", "Bjorn") == 1.0
    assert title_similarity("Encyclopædia", "Encyclopaedia") == 1.0
    assert title_similarity("Łódź", "Lodz") == 1.0


def test_title_similarity_preserves_non_latin() -> None:
    assert title_similarity("千と千尋の神隠し", "千と千尋の神隠し") == 1.0
    assert title_similarity("千と千尋の神隠し", "千与千寻") < 1.0
    assert title_similarity("Иван Васильевич", "Иван Васильевич") == 1.0


def test_title_similarity_range_and_difference() -> None:
    score = title_similarity("The Matrix", "Blade Runner")
    assert 0.0 <= score < 0.5
    assert title_similarity("", "") == 1.0
    assert title_similarity("", "The Matrix") == 0.0


def test_title_similarity_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        title_similarity(None, "x")  # type: ignore[arg-type]


def test_resolve_identity_exact_match_auto() -> None:
    parsed = ParsedName("The Matrix", 1999)
    cache = FakeCache()
    http = FakeHttp(payload(result(603, "The Matrix", 1999, popularity=100.0)))

    identity = resolve_identity(parsed, TOKEN, cache, http)

    assert identity.status == "auto"
    assert identity.title == "The Matrix"
    assert identity.year == 1999
    assert identity.tmdb_id == 603
    assert identity.confidence == 1.0
    assert identity.reason == "title-and-year-match"
    assert identity.source == "tmdb"
    assert identity.warning is None
    assert [candidate.tmdb_id for candidate in identity.candidates] == [603]


def test_http_request_uses_bearer_header_not_url() -> None:
    parsed = ParsedName("The Matrix", 1999)
    cache = FakeCache()
    http = FakeHttp(payload(result(603, "The Matrix", 1999)))

    identity = resolve_identity(parsed, TOKEN, cache, http, timeout=2.5)

    assert identity.status == "auto"
    assert len(http.calls) == 1
    url, headers, timeout = http.calls[0]
    assert TOKEN not in url
    assert "api_key" not in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert timeout == 2.5
    query = parse_qs(urlparse(url).query)
    assert query["query"] == ["The Matrix"]
    assert query["year"] == ["1999"]
    assert TOKEN not in repr(identity)


def test_network_result_is_cached_under_canonical_key() -> None:
    parsed = ParsedName("The Matrix", 1999)
    cache = FakeCache()
    fixture = payload(result(603, "The Matrix", 1999))
    http = FakeHttp(fixture)

    resolve_identity(parsed, TOKEN, cache, http)

    assert cache.gets == [tmdb_cache_key("The Matrix", 1999)]
    assert len(cache.puts) == 1
    key, stored = cache.puts[0]
    assert key == tmdb_cache_key("the matrix", 1999)
    assert stored == fixture
    assert TOKEN not in key


def test_http_positional_only_callable() -> None:
    def get(url: str, headers: dict[str, str], timeout: float, /) -> object:
        return payload(result(1, "The Matrix", 1999))

    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), get)
    assert identity.status == "auto"


def test_http_keyword_only_callable() -> None:
    def get(*, url: str, headers: dict[str, str], timeout: float) -> object:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        return payload(result(1, "The Matrix", 1999))

    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), get)
    assert identity.status == "auto"


def test_http_response_can_be_json_text() -> None:
    parsed = ParsedName("The Matrix", 1999)
    http = FakeHttp(json.dumps(payload(result(603, "The Matrix", 1999))))
    identity = resolve_identity(parsed, TOKEN, FakeCache(), http)
    assert identity.status == "auto"


def test_http_response_can_be_object_with_json_method() -> None:
    class Response:
        def json(self) -> dict[str, object]:
            return payload(result(603, "The Matrix", 1999))

    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), Response())
    assert identity.status == "auto"


def test_network_failure_uses_parse_only_when_clear() -> None:
    http = FakeHttp(error=RuntimeError("connection refused"))
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), http)

    assert identity.status == "auto"
    assert identity.source == "parse-only"
    assert identity.reason == "parse-only"
    assert identity.tmdb_id is None
    assert identity.title == "The Matrix"
    assert identity.year == 1999
    assert identity.candidates == []
    assert identity.warning is not None


def test_network_failure_without_year_is_review() -> None:
    http = FakeHttp(error=RuntimeError("connection refused"))
    identity = resolve_identity(ParsedName("The Matrix"), TOKEN, FakeCache(), http)

    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"
    assert identity.source == "parse-only"


def test_network_failure_without_clear_title_is_review() -> None:
    parsed = ParsedName("???", 1999)
    identity = resolve_identity(parsed, TOKEN, FakeCache(), FakeHttp(error=RuntimeError("boom")))
    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"


def test_no_token_uses_parse_only_and_never_calls_http() -> None:
    http = FakeHttp(payload(result(603, "The Matrix", 1999)))
    identity = resolve_identity(ParsedName("The Matrix", 1999), None, FakeCache(), http)

    assert identity.status == "auto"
    assert identity.source == "parse-only"
    assert identity.tmdb_id is None
    assert http.calls == []
    assert identity.warning is not None


def test_blank_token_is_missing() -> None:
    http = FakeHttp(payload(result(603, "The Matrix", 1999)))
    identity = resolve_identity(ParsedName("The Matrix", 1999), "   ", FakeCache(), http)

    assert identity.source == "parse-only"
    assert http.calls == []


def test_no_token_without_year_is_review() -> None:
    identity = resolve_identity(ParsedName("The Matrix"), None, FakeCache(), FakeHttp())
    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"


def test_no_token_without_title_is_review() -> None:
    identity = resolve_identity(ParsedName("", 1999), None, FakeCache(), FakeHttp())
    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"
    assert identity.warning is not None


def test_no_token_with_invalid_future_year_is_review() -> None:
    parsed = ParsedName("Movie", date.today().year + 3)
    identity = resolve_identity(parsed, None, FakeCache(), FakeHttp())
    assert identity.status == "review"


def test_cached_response_reused_without_token() -> None:
    fixture = payload(result(603, "The Matrix", 1999))
    cache = FakeCache(fixture)
    http = FakeHttp(error=AssertionError("network must not be used"))

    identity = resolve_identity(ParsedName("The Matrix", 1999), None, cache, http)

    assert identity.status == "auto"
    assert identity.source == "cache"
    assert identity.tmdb_id == 603
    assert http.calls == []
    assert cache.puts == []


def test_cached_response_reused_even_with_token() -> None:
    fixture = payload(result(603, "The Matrix", 1999))
    cache = FakeCache(fixture)
    http = FakeHttp(error=AssertionError("network must not be used"))

    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, cache, http)

    assert identity.source == "cache"
    assert http.calls == []


def test_empty_cached_response_is_not_parse_fallback() -> None:
    cache = FakeCache({"results": []})
    identity = resolve_identity(ParsedName("The Matrix", 1999), None, cache, FakeHttp())

    assert identity.status == "review"
    assert identity.source == "cache"
    assert identity.reason == "identity-low-confidence"
    assert identity.candidates == []


def test_empty_network_results_is_review_not_parse_fallback() -> None:
    identity = resolve_identity(
        ParsedName("The Matrix", 1999),
        TOKEN,
        FakeCache(),
        FakeHttp({"results": []}),
    )
    assert identity.status == "review"
    assert identity.source == "tmdb"


def test_year_mismatch_is_review() -> None:
    http = FakeHttp(payload(result(603, "The Matrix", 2003)))
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), http)

    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"
    assert identity.tmdb_id is None
    assert identity.candidates[0].tmdb_id == 603


def test_title_below_threshold_is_review() -> None:
    http = FakeHttp(payload(result(1, "Matrix", 1999)))
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), http)

    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"
    assert identity.candidates[0].score < AUTO_TITLE_THRESHOLD


def test_exactly_one_strong_title_without_year_is_auto() -> None:
    http = FakeHttp(payload(result(603, "The Matrix", 1999)))
    identity = resolve_identity(ParsedName("The Matrix"), TOKEN, FakeCache(), http)

    assert identity.status == "auto"
    assert identity.reason == "single-title-match"
    assert identity.tmdb_id == 603
    assert identity.year == 1999


def test_single_tmdb_result_without_year_is_auto() -> None:
    http = FakeHttp(payload(result(603, "Matrix", 1999)))
    identity = resolve_identity(ParsedName("The Matrix"), TOKEN, FakeCache(), http)

    # One result is not enough when its title is below the threshold.
    assert identity.status == "review"


def test_two_equal_top_candidates_are_tie() -> None:
    fixture = payload(
        result(1, "The Matrix", 1999, popularity=10.0),
        result(2, "the matrix", 1999, popularity=20.0),
    )
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), FakeHttp(fixture))

    assert identity.status == "review"
    assert identity.reason == "identity-tie"
    assert [candidate.tmdb_id for candidate in identity.candidates] == [2, 1]


def test_two_high_but_untied_candidates_are_low_confidence() -> None:
    fixture = payload(
        result(1, "The Matrix", 1999, popularity=100.0),
        result(2, "The Matrixx", 1999, popularity=1.0),
    )
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), FakeHttp(fixture))

    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"
    assert identity.candidates[0].score > identity.candidates[1].score
    assert identity.candidates[1].score >= AUTO_TITLE_THRESHOLD


def test_missing_year_two_equal_candidates_are_tie() -> None:
    fixture = payload(
        result(1, "The Matrix", 1999),
        result(2, "The Matrix", 2003),
    )
    identity = resolve_identity(ParsedName("The Matrix"), TOKEN, FakeCache(), FakeHttp(fixture))

    assert identity.status == "review"
    assert identity.reason == "identity-tie"


def test_missing_year_two_high_untied_candidates_are_low_confidence() -> None:
    fixture = payload(
        result(1, "The Matrix", 1999),
        result(2, "The Matrixx", 2003),
    )
    identity = resolve_identity(ParsedName("The Matrix"), TOKEN, FakeCache(), FakeHttp(fixture))

    assert identity.status == "review"
    assert identity.reason == "identity-low-confidence"


def test_original_title_is_used_for_matching() -> None:
    fixture = payload(result(129, "Spirited Away", 2001, original_title="千と千尋の神隠し"))
    identity = resolve_identity(
        ParsedName("千と千尋の神隠し", 2001),
        TOKEN,
        FakeCache(),
        FakeHttp(fixture),
    )

    assert identity.status == "auto"
    assert identity.tmdb_id == 129


def test_candidate_ordering_is_deterministic() -> None:
    items = [
        result(10, "The Matrix", 1999, popularity=1.0),
        result(20, "the matrix", 1999, popularity=5.0),
        result(30, "The Matrix!", 1999, popularity=3.0),
        result(40, "Matrix", 1999, popularity=1000.0),
    ]
    first = resolve_identity(
        ParsedName("The Matrix", 1999),
        TOKEN,
        FakeCache(),
        FakeHttp(payload(*items)),
    )
    second = resolve_identity(
        ParsedName("The Matrix", 1999),
        TOKEN,
        FakeCache(),
        FakeHttp(payload(*reversed(items))),
    )

    expected = [20, 30, 10]
    assert [candidate.tmdb_id for candidate in first.candidates] == expected
    assert [candidate.tmdb_id for candidate in second.candidates] == expected
    assert first.status == second.status == "review"
    assert first.reason == second.reason == "identity-tie"


def test_top_three_are_returned_on_review() -> None:
    fixture = payload(
        result(1, "The Matrix", 1999),
        result(2, "The Matrixx", 1999),
        result(3, "The Matrixxx", 1999),
        result(4, "Matrix", 1999),
    )
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), FakeHttp(fixture))
    assert identity.status == "review"
    assert len(identity.candidates) == 3


def test_candidate_year_can_come_from_year_field() -> None:
    item = {
        "id": 603,
        "title": "The Matrix",
        "original_title": "The Matrix",
        "year": 1999,
        "popularity": 1,
    }
    identity = resolve_identity(
        ParsedName("The Matrix", 1999),
        TOKEN,
        FakeCache(),
        FakeHttp({"results": [item]}),
    )
    assert identity.status == "auto"
    assert identity.year == 1999


def test_error_warning_redacts_token() -> None:
    http = FakeHttp(error=RuntimeError(f"request failed for {TOKEN} with Authorization: Bearer {TOKEN}"))
    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), http)

    assert identity.status == "auto"
    assert identity.warning is not None
    assert TOKEN not in identity.warning
    assert TOKEN not in repr(identity)
    assert "[REDACTED]" in identity.warning


def test_broken_cache_get_falls_back_to_network() -> None:
    cache = FakeCache(get_error=RuntimeError("cache is corrupt"))
    http = FakeHttp(payload(result(603, "The Matrix", 1999)))

    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, cache, http)

    assert identity.status == "auto"
    assert identity.source == "tmdb"
    assert len(http.calls) == 1


def test_broken_cache_put_does_not_fail_resolution() -> None:
    cache = FakeCache(put_error=RuntimeError("disk full"))
    http = FakeHttp(payload(result(603, "The Matrix", 1999)))

    identity = resolve_identity(ParsedName("The Matrix", 1999), TOKEN, cache, http)

    assert identity.status == "auto"
    assert identity.source == "tmdb"


def test_cache_key_is_canonical() -> None:
    assert tmdb_cache_key("  The   Matrix ", 1999) == tmdb_cache_key("the matrix", 1999)
    assert tmdb_cache_key("2001: A Space Odyssey", 1968) == tmdb_cache_key("2001 A Space Odyssey", 1968)
    assert tmdb_cache_key("Fast & Furious", 2009) == tmdb_cache_key("Fast and Furious", 2009)
    assert tmdb_cache_key("Amélie", 2001) == tmdb_cache_key("Amelie", 2001)
    assert tmdb_cache_key("The Matrix", 1999) != tmdb_cache_key("The Matrix", 2000)
    assert tmdb_cache_key("The Matrix", None) != tmdb_cache_key("The Matrix", 1999)
    assert TOKEN not in tmdb_cache_key("The Matrix", 1999)


def test_timeout_must_be_positive() -> None:
    for timeout in (0, -1, True):
        with pytest.raises(ValueError):
            resolve_identity(ParsedName("The Matrix", 1999), TOKEN, FakeCache(), FakeHttp(), timeout)
    with pytest.raises(ValueError):
        resolve_identity(
            ParsedName("The Matrix", 1999),
            TOKEN,
            FakeCache(),
            FakeHttp(),
            "5",  # type: ignore[arg-type]
        )


def test_identity_result_dataclass_fields() -> None:
    identity = IdentityResult(
        status="review",
        title="The Matrix",
        year=1999,
        tmdb_id=None,
        confidence=0.0,
        reason="identity-low-confidence",
    )
    assert identity.candidates == []
    assert identity.source == "parse-only"
    assert identity.warning is None


def test_resolve_manual_accepts_selected_candidate() -> None:
    candidates = [
        TmdbCandidate(1, "The Matrix", "The Matrix", 1999, 10.0, 1.0),
        TmdbCandidate(2, "The Matrix Reloaded", "The Matrix Reloaded", 2003, 20.0, 0.7),
        TmdbCandidate(3, "The Matrix Revolutions", "The Matrix Revolutions", 2003, 5.0, 0.6),
    ]
    identity = resolve_manual(candidates, "The Matrix", 1999, 0)

    assert identity.status == "auto"
    assert identity.title == "The Matrix"
    assert identity.year == 1999
    assert identity.tmdb_id == 1
    assert identity.reason == "manual-accept"
    assert identity.source == "tmdb"
    assert identity.confidence == 1.0
    assert identity.candidates == candidates


def test_resolve_manual_uses_candidate_year_when_year_is_none() -> None:
    candidates = [TmdbCandidate(2, "The Matrix Reloaded", "The Matrix Reloaded", 2003, 20.0, 0.7)]
    identity = resolve_manual(candidates, "The Matrix Reloaded", None, 0)
    assert identity.year == 2003
    assert identity.tmdb_id == 2


def test_resolve_manual_requires_a_valid_effective_year() -> None:
    candidates = [TmdbCandidate(2, "Unknown", "Unknown", None, 20.0, 0.7)]
    with pytest.raises(ValueError):
        resolve_manual(candidates, "Unknown", None, 0)


def test_resolve_manual_rejects_empty_candidates() -> None:
    with pytest.raises(ValueError):
        resolve_manual([], "The Matrix", 1999, 0)


@pytest.mark.parametrize("index", [-1, 1, 99])
def test_resolve_manual_rejects_out_of_range_index(index: int) -> None:
    candidates = [TmdbCandidate(1, "The Matrix", "The Matrix", 1999, 1.0, 1.0)]
    with pytest.raises(ValueError):
        resolve_manual(candidates, "The Matrix", 1999, index)


@pytest.mark.parametrize("index", ["0", 0.0, True, None])
def test_resolve_manual_rejects_bad_index_type(index: object) -> None:
    candidates = [TmdbCandidate(1, "The Matrix", "The Matrix", 1999, 1.0, 1.0)]
    with pytest.raises(TypeError):
        resolve_manual(candidates, "The Matrix", 1999, index)  # type: ignore[arg-type]


@pytest.mark.parametrize("title", ["", "   ", "???"])
def test_resolve_manual_rejects_bad_title(title: str) -> None:
    candidates = [TmdbCandidate(1, "The Matrix", "The Matrix", 1999, 1.0, 1.0)]
    with pytest.raises(ValueError):
        resolve_manual(candidates, title, 1999, 0)


@pytest.mark.parametrize("year", [1869, date.today().year + 3, "1999", True])
def test_resolve_manual_rejects_bad_year(year: object) -> None:
    candidates = [TmdbCandidate(1, "The Matrix", "The Matrix", 1999, 1.0, 1.0)]
    with pytest.raises(ValueError):
        resolve_manual(candidates, "The Matrix", year, 0)  # type: ignore[arg-type]


def test_resolve_manual_rejects_non_candidates() -> None:
    with pytest.raises(TypeError):
        resolve_manual(["not a candidate"], "The Matrix", 1999, 0)  # type: ignore[list-item]


def test_resolve_manual_rejects_invalid_candidate_id() -> None:
    candidates = [TmdbCandidate(0, "The Matrix", "The Matrix", 1999, 1.0, 1.0)]
    with pytest.raises(ValueError):
        resolve_manual(candidates, "The Matrix", 1999, 0)
