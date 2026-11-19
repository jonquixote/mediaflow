"""Audio language, commentary and codec decisions.

Requirement markers: MP4-3 (MP4 copy/transcode table), MP4-4 (selection order,
commentary/description exclusion, unknown_language, fallback, max_tracks and
the never-zero-audio rule), MKV-1 (MKV copies every selected codec).
"""

from __future__ import annotations

import pytest

from mediaflow.plan import (
    PlanError,
    audio_choice,
    normalise_language,
    select_audio,
)


def stream(
    index: int,
    codec: str = "aac",
    language: str | None = "eng",
    channels: int = 2,
    *,
    default: bool = False,
    title: str | None = None,
    comment: bool = False,
    descriptions: bool = False,
):
    tags = {}
    if language is not None:
        tags["language"] = language
    if title is not None:
        tags["title"] = title
    return {
        "index": index,
        "codec_type": "audio",
        "codec_name": codec,
        "channels": channels,
        "tags": tags,
        "disposition": {
            "default": 1 if default else 0,
            "comment": 1 if comment else 0,
            "descriptions": 1 if descriptions else 0,
        },
    }


def cfg(
    languages=("original", "eng"),
    *,
    max_tracks=3,
    copy_codecs=None,
    exclude_commentary=True,
    unknown_language="keep_first",
):
    result = {
        "languages": tuple(languages),
        "max_tracks": max_tracks,
        "exclude_commentary": exclude_commentary,
        "unknown_language": unknown_language,
    }
    if copy_codecs is not None:
        result["copy_codecs"] = tuple(copy_codecs)
    return result


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("en", "eng"),
        ("eng", "eng"),
        ("EN-us", "eng"),
        ("de", "deu"),
        ("ger", "deu"),
        ("deu", "deu"),
        ("fr", "fra"),
        ("fre", "fra"),
        ("fra", "fra"),
        ("es", "spa"),
        ("it", "ita"),
        ("ja", "jpn"),
        ("pt-BR", "por"),
        ("dut", "nld"),
        ("zho", "zho"),
        ("chi", "zho"),
        (None, "und"),
        ("", "und"),
        ("und", "und"),
        ("unknown", "und"),
        ("zzz", "und"),
        ("qaa", "und"),
    ],
)
def test_normalise_language_table(tag, expected):
    assert normalise_language(tag) == expected


@pytest.mark.parametrize("codec", ["aac", "ac3", "eac3"])
def test_mp4_copies_aac_ac3_eac3(codec):
    """MP4-3: AAC, AC-3 and E-AC-3 are stream-copied."""
    choices, warnings = select_audio(
        [stream(3, codec, "eng", 6, default=True)], cfg(["eng"]), "mp4"
    )
    assert warnings == []
    assert choices[0]["action"] == "copy"
    assert choices[0]["codec"] == codec
    assert choices[0]["bitrate"] is None
    assert choices[0]["index"] == 3
    assert choices[0]["default"] is True


@pytest.mark.parametrize(
    "channels,codec,bitrate,downmix",
    [
        (1, "aac", 96, False),
        (2, "aac", 160, False),
        (3, "eac3", 448, False),
        (5, "eac3", 448, False),
        (6, "eac3", 448, False),
        (7, "eac3", 640, False),
        (8, "eac3", 640, False),
        (9, "eac3", 640, True),
        (10, "eac3", 640, True),
    ],
)
@pytest.mark.parametrize("source_codec", ["dts", "truehd", "flac", "pcm_s16le", "opus"])
def test_mp4_transcode_table_by_channels(source_codec, channels, codec, bitrate, downmix):
    """MP4-3: DTS/TrueHD/FLAC/PCM/Opus follow the channel-count table."""
    choices, _ = select_audio(
        [stream(1, source_codec, "eng", channels, default=True)], cfg(["eng"]), "mp4"
    )
    entry = choices[0]
    assert entry["source_codec"] == source_codec
    assert entry["action"] == "transcode"
    assert entry["codec"] == codec
    assert entry["bitrate"] == bitrate
    assert entry["channels"] == channels
    assert bool(entry.get("downmix")) is downmix


def test_mkv_copies_every_selected_codec():
    """MKV-1: no codec restrictions on MKV; every selected track is copied."""
    streams = [
        stream(1, "dts", "eng", 6, default=True),
        stream(2, "truehd", "eng", 8),
        stream(3, "flac", "deu", 2),
        stream(4, "opus", "eng", 6),
    ]
    choices, warnings = select_audio(
        streams, cfg(["original", "eng", "deu"], max_tracks=10), "mkv"
    )
    assert warnings == []
    assert [entry["index"] for entry in choices] == [1, 2, 3, 4]
    assert all(entry["action"] == "copy" for entry in choices)
    assert all(entry["bitrate"] is None for entry in choices)


def test_mp4_copy_codecs_override_only_narrows_allowed_set():
    """MP4-3: preset copy_codecs can never make DTS or TrueHD copyable."""
    streams = [stream(1, "aac", "eng", 6), stream(2, "ac3", "eng", 6)]
    choices, _ = select_audio(streams, cfg(["eng"], max_tracks=5, copy_codecs=["aac"]), "mp4")
    assert choices[0]["action"] == "copy"
    assert choices[1]["action"] == "transcode"
    assert (choices[1]["codec"], choices[1]["bitrate"]) == ("eac3", 448)

    choices, _ = select_audio(
        [stream(1, "dts", "eng", 6)], cfg(["eng"], copy_codecs=["dts"]), "mp4"
    )
    assert choices[0]["action"] == "transcode"
    assert choices[0]["codec"] == "eac3"

    choices, _ = select_audio(
        [stream(1, "eac3", "eng", 6)], cfg(["eng"], copy_codecs=["*"]), "mp4"
    )
    assert choices[0]["action"] == "copy"


def test_original_means_default_track_language_and_is_first():
    """MP4-4: original resolves from the default track, then output is default-first."""
    streams = [
        stream(1, "aac", "eng", 2),
        stream(2, "dts", "deu", 6, default=True),
        stream(3, "aac", "fra", 2),
    ]
    choices, warnings = select_audio(
        streams, cfg(["original", "eng"], max_tracks=3), "mp4"
    )
    assert warnings == []
    assert [entry["index"] for entry in choices] == [2, 1]
    assert [entry["language"] for entry in choices] == ["deu", "eng"]
    assert [entry["default"] for entry in choices] == [True, False]
    assert choices[0]["action"] == "transcode"


def test_default_track_first_then_source_order_and_max_tracks():
    """MP4-4: default first, source order afterwards, max_tracks after filtering."""
    streams = [
        stream(1, "aac", "eng", 2),
        stream(2, "aac", "deu", 2),
        stream(3, "aac", "fra", 2),
        stream(4, "aac", "spa", 2, default=True),
        stream(5, "aac", "ita", 2),
    ]
    choices, _ = select_audio(
        streams,
        cfg(["eng", "deu", "fra", "spa", "ita"], max_tracks=3),
        "mp4",
    )
    assert [entry["index"] for entry in choices] == [4, 1, 2]
    assert [entry["default"] for entry in choices] == [True, False, False]


def test_commentary_and_audio_description_are_excluded():
    """MP4-4: disposition.comment and commentary/director/description titles drop."""
    streams = [
        stream(1, "aac", "eng", 2, default=True, comment=True),
        stream(2, "aac", "eng", 2, title="Director's commentary"),
        stream(3, "aac", "eng", 2, title="English audio description"),
        stream(4, "aac", "eng", 2, title="Descriptions"),
        stream(5, "ac3", "fra", 6),
    ]
    choices, warnings = select_audio(
        streams, cfg(["original", "eng", "fra"], max_tracks=5), "mp4"
    )
    assert warnings == []
    assert [entry["index"] for entry in choices] == [5]

    choices, _ = select_audio(
        streams, cfg(["eng", "fra"], exclude_commentary=False, max_tracks=5), "mp4"
    )
    assert {entry["index"] for entry in choices} == {1, 2, 3, 4, 5}


def test_fallback_to_default_or_first_with_warning():
    """MP4-4: no preferred language falls back with an explicit warning."""
    streams = [stream(1, "aac", "eng", 2), stream(2, "dts", "deu", 6, default=True)]
    choices, warnings = select_audio(streams, cfg(["jpn"]), "mp4")
    assert warnings == ["fallback_used"]
    assert [entry["index"] for entry in choices] == [2]


def test_never_zero_audio_even_when_all_tracks_are_commentary():
    """MP4-4: filtering can never produce zero audio when the source had audio."""
    streams = [stream(1, "aac", "eng", 2, default=True, title="Commentary")]
    choices, warnings = select_audio(streams, cfg(["eng"]), "mp4")
    assert len(choices) == 1
    assert choices[0]["index"] == 1
    assert warnings == ["fallback_used"]

    with pytest.raises(PlanError):
        select_audio([], cfg(["eng"]), "mp4", source_had_audio=True)
    assert select_audio([], cfg(["eng"]), "mp4", source_had_audio=False) == ([], [])


def test_unknown_language_keep_first_and_keep_all():
    """MP4-4: unknown_language controls how many untagged/und tracks survive."""
    streams = [
        stream(1, "aac", "eng", 2),
        stream(2, "aac", None, 2),
        stream(3, "aac", "und", 2),
    ]
    choices, warnings = select_audio(
        streams, cfg(["eng"], unknown_language="keep_first", max_tracks=10), "mp4"
    )
    assert warnings == []
    assert [entry["index"] for entry in choices] == [1, 2]

    choices, _ = select_audio(
        streams, cfg(["eng"], unknown_language="keep_all", max_tracks=10), "mp4"
    )
    assert [entry["index"] for entry in choices] == [1, 2, 3]

    choices, warnings = select_audio(
        [stream(1, "aac", "und", 2, default=True)],
        cfg(["eng"], unknown_language="drop"),
        "mp4",
    )
    assert warnings == ["fallback_used"]
    assert [entry["index"] for entry in choices] == [1]


def test_untagged_original_track_is_kept_when_original_preferred():
    streams = [stream(1, "aac", None, 2, default=True), stream(2, "aac", "eng", 2)]
    choices, warnings = select_audio(
        streams, cfg(["original"], max_tracks=3), "mp4"
    )
    assert warnings == []
    assert [entry["index"] for entry in choices] == [1]


def test_max_tracks_is_applied_after_commentary_filtering():
    streams = [
        stream(1, "aac", "eng", 2, title="Commentary"),
        stream(2, "aac", "eng", 2, default=True),
        stream(3, "aac", "eng", 2),
        stream(4, "aac", "eng", 2),
    ]
    choices, _ = select_audio(streams, cfg(["eng"], max_tracks=2), "mp4")
    assert [entry["index"] for entry in choices] == [2, 3]
    assert [entry["default"] for entry in choices] == [True, False]


def test_choice_title_and_normalised_language_are_recorded():
    stream_with_title = stream(
        7, "aac", "ger", 2, default=True, title="Deutsch 5.1"
    )
    entry = audio_choice(stream_with_title, cfg(["eng"]), "mp4", default=True)
    assert entry["language"] == "deu"
    assert entry["title"] == "Deutsch 5.1"
    assert entry["default"] is True
    assert entry["index"] == 7

    choices, _ = select_audio([stream_with_title], cfg(["original"]), "mp4")
    assert choices[0]["language"] == "deu"
    assert choices[0]["title"] == "Deutsch 5.1"
