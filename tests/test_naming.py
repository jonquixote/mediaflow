"""Tests for media name parsing, sanitising, formatting and sidecars."""

from __future__ import annotations

from datetime import date

import pytest

from mediaflow.naming import (
    MAX_COMPONENT_BYTES,
    ParsedName,
    format_media_name,
    parse_media_name,
    sanitize_component,
    sidecar_language,
)

# (name, expected title, year, container, kind, multidisc, extra, sample)
MESSY_FIXTURES = [
    ("2001: A Space Odyssey (1968).mkv", "2001: A Space Odyssey", 1968, "mkv", "movie", False, False, False),
    ("2001.A.Space.Odyssey.1968.1080p.BluRay.x264.mkv", "2001 A Space Odyssey", 1968, "mkv", "movie", False, False, False),
    ("Blade Runner 2049 (2017) [1080p.BluRay.x264-GROUP].mkv", "Blade Runner 2049", 2017, "mkv", "movie", False, False, False),
    ("Blade.Runner.2049.2017.2160p.UHD.BluRay.x265-TERMiNAL.mkv", "Blade Runner 2049", 2017, "mkv", "movie", False, False, False),
    ("The Matrix 1999 1080p BluRay x264-GRP.mkv", "The Matrix", 1999, "mkv", "movie", False, False, False),
    ("Amélie (2001) 1080p BluRay DTS x264.mkv", "Amélie", 2001, "mkv", "movie", False, False, False),
    ("Le Fabuleux Destin d'Amélie Poulain (2001).mkv", "Le Fabuleux Destin d'Amélie Poulain", 2001, "mkv", "movie", False, False, False),
    ("Movie.Name.2020.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv", "Movie Name", 2020, "mkv", "movie", False, False, False),
    ("Spider-Man: Across the Spider-Verse (2023) [tmdbid-569094].mkv", "Spider-Man: Across the Spider-Verse", 2023, "mkv", "movie", False, False, False),
    ("Spider-Man Across the Spider-Verse 2023 2160p WEB-DL DDP5.1 Atmos HDR x265-GRP.mkv", "Spider-Man Across the Spider-Verse", 2023, "mkv", "movie", False, False, False),
    ("Das Boot (1981) (Director's Cut) [1080p].mkv", "Das Boot", 1981, "mkv", "movie", False, False, False),
    ("Das.Boot.1981.German.DL.1080p.BluRay.x264.mkv", "Das Boot", 1981, "mkv", "movie", False, False, False),
    ("12 Monkeys (1995).mkv", "12 Monkeys", 1995, "mkv", "movie", False, False, False),
    ("12.Monkeys.1995.1080p.mkv", "12 Monkeys", 1995, "mkv", "movie", False, False, False),
    ("1984 (1984).mkv", "1984", 1984, "mkv", "movie", False, False, False),
    ("2012 (2009).mkv", "2012", 2009, "mkv", "movie", False, False, False),
    ("Wonder Woman 1984 (2020).mkv", "Wonder Woman 1984", 2020, "mkv", "movie", False, False, False),
    ("1917.2019.1080p.BluRay.x264.mkv", "1917", 2019, "mkv", "movie", False, False, False),
    ("Se7en (1995).mkv", "Se7en", 1995, "mkv", "movie", False, False, False),
    ("The 355 (2022).mkv", "The 355", 2022, "mkv", "movie", False, False, False),
    ("Fast & Furious (2009).mkv", "Fast & Furious", 2009, "mkv", "movie", False, False, False),
    ("[Group] Movie Title (2019) [1080p][x265].mkv", "Movie Title", 2019, "mkv", "movie", False, False, False),
    ("Movie.Title.(2019).720p.BRRip.XviD.AC3-EVO.avi", "Movie Title", 2019, "avi", "movie", False, False, False),
    ("Movie.Title.2019.NF.WEBRip.DDP5.1.x264-NTb.mkv", "Movie Title", 2019, "mkv", "movie", False, False, False),
    ("Movie Title (2019) {tmdb-12345}.mkv", "Movie Title", 2019, "mkv", "movie", False, False, False),
    ("Movie Title (2019) [tmdbid-12345].mkv", "Movie Title", 2019, "mkv", "movie", False, False, False),
    ("The Lord of the Rings The Fellowship of the Ring (2001) EXTENDED.mkv", "The Lord of the Rings The Fellowship of the Ring", 2001, "mkv", "movie", False, False, False),
    ("LOTR.The.Fellowship.of.the.Ring.2001.Extended.Edition.1080p.mkv", "LOTR The Fellowship of the Ring", 2001, "mkv", "movie", False, False, False),
    ("Movie (2020) CD1.avi", "Movie", 2020, "avi", "multidisc", True, False, False),
    ("Movie (2020) CD2.avi", "Movie", 2020, "avi", "multidisc", True, False, False),
    ("Movie (2020) Disc 2.iso", "Movie", 2020, "iso", "multidisc", True, False, False),
    ("Movie (2020) cd2.mkv", "Movie", 2020, "mkv", "multidisc", True, False, False),
    ("Movie Title (2019) (CD2).avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("Movie Title (2019) - CD 2.avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("Movie Title (2019) - Disc 2.avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("Movie Title (2019) DVD2.avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("Movie Title (2019) [Disc2].avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("Movie Title (2019) disc_2.avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("Movie Title 2019 1080p BluRay x264 CD1.mkv", "Movie Title", 2019, "mkv", "multidisc", True, False, False),
    ("Movie Title 2019 1080p BluRay x264 CD2.mkv", "Movie Title", 2019, "mkv", "multidisc", True, False, False),
    ("Movie (2020)/Extras/Behind the Scenes.mkv", "Movie", 2020, "mkv", "extra", False, True, False),
    ("Movie (2020)/Featurettes/Feature 1.mkv", "Movie", 2020, "mkv", "extra", False, True, False),
    ("Movie (2020)/Trailers/Trailer 1.mkv", "Movie", 2020, "mkv", "extra", False, True, False),
    ("Movie (2020)/Deleted Scenes/Scene 1.mkv", "Movie", 2020, "mkv", "extra", False, True, False),
    ("Movie (2020)/Extras/Interview with Director.mkv", "Movie", 2020, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Featurettes.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Behind the Scenes.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Deleted Scenes.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Bloopers.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Outtakes.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Trailer.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Teaser Trailer.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie Title (2019) - Bonus Features.mkv", "Movie Title", 2019, "mkv", "extra", False, True, False),
    ("Movie (2020) sample.mkv", "Movie", 2020, "mkv", "extra", False, True, True),
    ("Movie.Title.2019.1080p.mkv.sample", "Movie Title", 2019, "mkv", "extra", False, True, True),
    ("sample.mkv", "sample", None, "mkv", "extra", False, True, True),
    ("Movie Title (2019) [Sample].mkv", "Movie Title", 2019, "mkv", "extra", False, True, True),
    ("Movie Title (2019) - sample.mkv", "Movie Title", 2019, "mkv", "extra", False, True, True),
    ("The.Office.US.S01E01.1080p.mkv", "The Office", None, "mkv", "movie", False, False, False),
    ("Movie Title (2019).en.srt", "Movie Title", 2019, "srt", "movie", False, False, False),
    ("Movie.Title.2019.1080p.BluRay.x264-GRP.en.forced.srt", "Movie Title", 2019, "srt", "movie", False, False, False),
    ("Movie Title (2019).mkv.part", "Movie Title", 2019, "mkv", "movie", False, False, False),
    ("Film.2019.mkv.en.srt", "Film", 2019, "srt", "movie", False, False, False),
    ("  Movie   Title  (2019)  ", "Movie Title", 2019, None, "movie", False, False, False),
    ("Movie Title (2019) DVD 1 of 2.avi", "Movie Title", 2019, "avi", "multidisc", True, False, False),
    ("NoYear.Movie.1080p.mkv", "NoYear Movie", None, "mkv", "movie", False, False, False),
    ("Some Weird ??? Name.mkv", "Some Weird ??? Name", None, "mkv", "movie", False, False, False),
    ("Movie (2020)/Disc 2/Movie Title.mkv", "Movie", 2020, "mkv", "multidisc", True, False, False),
    ("", "", None, None, "movie", False, False, False),
]


def test_messy_fixture_count_is_large() -> None:
    assert len(MESSY_FIXTURES) >= 50


@pytest.mark.parametrize(
    "name,title,year,container,kind,multidisc,extra,sample",
    MESSY_FIXTURES,
    ids=[fixture[0] or "<empty>" for fixture in MESSY_FIXTURES],
)
def test_parse_messy_real_world_name(
    name: str,
    title: str,
    year: int | None,
    container: str | None,
    kind: str,
    multidisc: bool,
    extra: bool,
    sample: bool,
) -> None:
    parsed = parse_media_name(name)
    assert isinstance(parsed, ParsedName)
    assert parsed.title == title
    assert parsed.year == year
    assert parsed.container == container
    assert parsed.kind == kind
    assert parsed.is_multidisc is multidisc
    assert parsed.is_extra is extra
    assert parsed.is_sample is sample
    if multidisc:
        assert parsed.disc_number is not None
        assert parsed.disc_number >= 1


def test_release_noise_is_removed_from_title_and_ordered() -> None:
    parsed = parse_media_name("The.Matrix.1999.2160p.UHD.BluRay.x265-TERMiNAL.mkv")
    assert parsed.title == "The Matrix"
    assert parsed.year == 1999
    assert "2160p" in parsed.release_noise
    assert "Ultra HD Blu-ray" in parsed.release_noise
    assert "H.265" in parsed.release_noise
    assert "TERMiNAL" in parsed.release_noise
    assert all(tag not in parsed.title for tag in ("2160p", "x265", "TERMiNAL"))


def test_tmdb_id_from_release_name() -> None:
    assert parse_media_name("Movie Title (2019) [tmdbid-569094].mkv").tmdb_id == 569094
    assert parse_media_name("Movie Title (2019) {tmdb-569094}.mkv").tmdb_id == 569094


def test_extra_labels_are_human_readable() -> None:
    assert parse_media_name("Movie (2020)/Extras/Behind the Scenes.mkv").extra == "Behind the Scenes"
    assert parse_media_name("Movie (2020)/Featurettes/Feature 1.mkv").extra == "Featurettes"
    assert parse_media_name("Movie (2020) sample.mkv").extra == "Sample"


def test_multidisc_disc_numbers() -> None:
    assert parse_media_name("Movie (2020) CD1.avi").disc_number == 1
    assert parse_media_name("Movie (2020) Disc 2.iso").disc_number == 2
    assert parse_media_name("Movie Title (2019) DVD 1 of 2.avi").disc_number == 1
    assert parse_media_name("Movie Title (2019) - Part II.mkv").is_multidisc is False


def test_dvd_format_is_not_a_disc_number() -> None:
    assert parse_media_name("Movie (2019) DVD5.avi").is_multidisc is False
    assert parse_media_name("Movie (2019) DVD9.avi").is_multidisc is False


def test_title_words_do_not_make_an_extra() -> None:
    assert parse_media_name("The Interview (2014).mkv").is_extra is False
    assert parse_media_name("Interview with the Vampire (1994).mkv").is_extra is False
    assert parse_media_name("Trailer Park Boys (2001).mkv").is_extra is False
    assert parse_media_name("Sample This (2012).mkv").is_extra is False


def test_bare_extra_filenames_are_detected() -> None:
    assert parse_media_name("Featurettes.mkv").is_extra is True
    assert parse_media_name("Featurettes.mkv").extra == "Featurettes"
    assert parse_media_name("Trailer 1.mkv").is_extra is True
    assert parse_media_name("Behind the Scenes.mkv").is_extra is True
    assert parse_media_name("Sample 1.mkv").is_sample is True


def test_special_features_folder_is_an_extra() -> None:
    parsed = parse_media_name("Movie (2020)/Special Features/Video 1.mkv")
    assert parsed.is_extra is True
    assert parsed.extra == "Special Features"


def test_parse_does_not_treat_year_in_title_as_release_noise() -> None:
    assert parse_media_name("2001: A Space Odyssey (1968).mkv").title == "2001: A Space Odyssey"
    assert parse_media_name("Blade Runner 2049 (2017).mkv").title == "Blade Runner 2049"
    assert parse_media_name("Wonder Woman 1984 (2020).mkv").title == "Wonder Woman 1984"
    assert parse_media_name("1984 (1984).mkv").year == 1984
    assert parse_media_name("2012 (2009).mkv").title == "2012"


def test_parse_is_repeatable() -> None:
    name = "Blade Runner 2049 (2017) [1080p.BluRay.x264-GROUP].mkv"
    first = parse_media_name(name)
    second = parse_media_name(name)
    assert first == second
    assert first.release_noise == second.release_noise


def test_parse_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        parse_media_name(123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2001: A Space Odyssey", "2001 - A Space Odyssey"),
        ("A: B: C", "A - B - C"),
        ("Foo:Bar", "Foo - Bar"),
        ("Foo::Bar", "Foo - Bar"),
        ("  Foo  :  Bar  ", "Foo - Bar"),
        ("A/B\\C?D*E\"F<G>H|I", "ABCDEFGHI"),
        ("  My\tMovie\n\"Quote\"  ", "My Movie Quote"),
        ("..Movie..", "Movie"),
        ("...", None),
        ("", None),
        ("   ", None),
        ("///", None),
        ("Amélie", "Amélie"),
        ("Ame\u0301lie", "Amélie"),
        ("千と千尋の神隠し", "千と千尋の神隠し"),
        ("NUL\x00char", "NUL char"),
        ("Dots. In. Title.", "Dots. In. Title"),
    ],
)
def test_sanitize_component(value: str, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            sanitize_component(value)
    else:
        assert sanitize_component(value) == expected


def test_sanitize_component_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        sanitize_component(42)  # type: ignore[arg-type]


def test_sanitize_component_byte_limit_ascii() -> None:
    result = sanitize_component("a" * 500)
    assert len(result.encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert len(result) == MAX_COMPONENT_BYTES


def test_sanitize_component_byte_limit_never_splits_character() -> None:
    result = sanitize_component("é" * 300)
    assert result.encode("utf-8").decode("utf-8") == result
    assert len(result.encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert set(result) == {"é"}


def test_sanitize_component_byte_limit_emoji() -> None:
    result = sanitize_component("🎬" * 100)
    assert result.encode("utf-8").decode("utf-8") == result
    assert len(result.encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert set(result) == {"🎬"}


def test_sanitize_component_truncates_on_word_boundary() -> None:
    result = sanitize_component("Word " * 100)
    assert len(result.encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert result.endswith("Word")
    assert not result.endswith(" ")


def test_sanitize_component_long_unbroken_unicode_is_valid() -> None:
    result = sanitize_component("漢" * 100)
    assert len(result.encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert result == "漢" * (MAX_COMPONENT_BYTES // 3)


def test_sanitize_component_result_is_nfc() -> None:
    result = sanitize_component("Cafe\u0301: Noir")
    assert result == "Café - Noir"
    assert result == __import__("unicodedata").normalize("NFC", result)


def test_format_media_name_basic() -> None:
    assert format_media_name("The Matrix", 1999) == "The Matrix (1999)"


def test_format_media_name_colon_and_tmdb_id() -> None:
    assert (
        format_media_name("2001: A Space Odyssey", 1968, 569094, True)
        == "2001 - A Space Odyssey (1968) [tmdbid-569094]"
    )


def test_format_media_name_appends_tmdb_only_when_requested() -> None:
    assert format_media_name("The Matrix", 1999, 603, False) == "The Matrix (1999)"
    assert format_media_name("The Matrix", 1999, 603, True) == "The Matrix (1999) [tmdbid-603]"


def test_format_media_name_folder_and_stem_are_identical() -> None:
    folder = format_media_name("Spider-Man: Across the Spider-Verse", 2023, 569094, True)
    stem = format_media_name("Spider-Man: Across the Spider-Verse", 2023, 569094, True)
    assert folder == stem
    assert f"movies/{folder}/{stem}.mkv".count(folder) == 2


def test_format_media_name_year_bounds() -> None:
    current = date.today().year
    assert format_media_name("Old Film", 1870) == "Old Film (1870)"
    assert format_media_name("New Film", current + 2) == f"New Film ({current + 2})"
    with pytest.raises(ValueError):
        format_media_name("Too Old", 1869)
    with pytest.raises(ValueError):
        format_media_name("Too New", current + 3)


@pytest.mark.parametrize("year", [None, "1999", 1999.0, True])
def test_format_media_name_rejects_bad_year_types(year: object) -> None:
    with pytest.raises(ValueError):
        format_media_name("Movie", year)  # type: ignore[arg-type]


def test_format_media_name_rejects_bad_tmdb_id() -> None:
    with pytest.raises(ValueError):
        format_media_name("Movie", 2000, 0, True)
    with pytest.raises(ValueError):
        format_media_name("Movie", 2000, True, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_media_name("Movie", 2000, None, True)


def test_format_media_name_rejects_empty_title() -> None:
    with pytest.raises(ValueError):
        format_media_name("...", 2000)


def test_format_media_name_sanitises_title() -> None:
    assert format_media_name('Bad/Name?: "Yes"', 2000) == "BadName - Yes (2000)"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Movie Title (2019).en.srt", "en"),
        ("Movie Title (2019).eng.srt", "en"),
        ("Movie Title (2019).en.forced.srt", "en"),
        ("Movie Title (2019).eng.forced.srt", "en"),
        ("Movie.Title.2019.1080p.BluRay.x264-GRP.en.forced.srt", "en"),
        ("Movie Title (2019).EN.SRT", "en"),
        ("Movie Title (2019).fr.srt", "fr"),
        ("Movie Title (2019).fra.srt", "fr"),
        ("Movie Title (2019).fre.srt", "fr"),
        ("Movie Title (2019).de.srt", "de"),
        ("Movie Title (2019).ger.srt", "de"),
        ("Movie Title (2019).deu.srt", "de"),
        ("Movie Title (2019).es.srt", "es"),
        ("Movie Title (2019).spa.srt", "es"),
        ("Movie Title (2019).pt-BR.srt", "pt-BR"),
        ("Movie Title (2019).zh-Hans.srt", "zh-Hans"),
        ("Movie Title (2019).en-US.srt", "en-US"),
        ("Movie Title (2019).en.sdh.srt", "en"),
        ("Movie Title (2019).en.cc.srt", "en"),
        ("Movie Title (2019).en.hi.srt", "en"),
        ("Movie Title (2019).hi.srt", "hi"),
        ("Movie Title (2019).hi.forced.srt", "hi"),
        ("Movie Title (2019).English.srt", "en"),
        ("Movie Title (2019).no.srt", "no"),
        ("Movie Title (2019).nb.srt", "nb"),
        ("folder/Movie Title (2019).eng.srt", "en"),
    ],
)
def test_sidecar_language(filename: str, expected: str) -> None:
    assert sidecar_language(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "Movie Title (2019).srt",
        "Movie Title (2019).mkv",
        "Movie Title (2019).und.srt",
        "Movie Title (2019).mul.srt",
        "Movie Title (2019).xyz.srt",
        "Movie Title (2019).GRP.srt",
        "Movie Title (2019).SRT",
        "Movie (2019) (English).mkv",
    ],
)
def test_sidecar_language_absent(filename: str) -> None:
    assert sidecar_language(filename) is None


def test_sidecar_language_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        sidecar_language(None)  # type: ignore[arg-type]
