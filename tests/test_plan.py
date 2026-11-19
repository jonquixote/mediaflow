"""Plan decisions: containers, subtitles, HDR, 10-bit, naming and schema.

Requirement markers: MP4-2, MP4-5, MP4-6, MP4-7, MP4-8, MP4-9, MP4-10,
MP4-11, MP4-12 (planner records explicit indexes only), MKV-1, SIZE-4,
SIZE-5, SAF-13 (probed container gate) and SCHED-1 (software window rule).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mediaflow.plan import PlanError, ReviewRequired, build_plan, select_subtitles
from mediaflow.probe import ProbeResult

SCHEMA_KEYS = {
    "schema",
    "preset",
    "action",
    "container",
    "plan_version",
    "fallback_from",
    "source",
    "video",
    "audio",
    "subtitles",
    "sidecars",
    "chapters",
    "attachments",
    "name",
    "ext",
    "destination_rel",
    "dry_run",
    "warnings",
    "dropped",
    "estimated_seconds",
    "speed_source",
    "review",
}
VIDEO_KEYS = {
    "index",
    "codec",
    "width",
    "height",
    "target_width",
    "target_height",
    "pix_fmt",
    "bit_depth",
    "sar",
    "frame_rate",
    "field_order",
    "hdr",
    "dolby_vision",
    "color",
    "backend",
    "encoder",
    "quality",
    "look_ahead",
    "tag",
    "scaled",
}
SOURCE_KEYS = {
    "path",
    "format",
    "duration",
    "size_bytes",
    "video_bytes",
    "main_video_index",
    "mtime",
}
CAPS = {"qsv": {"hevc8": True, "hevc10": True}}


def video_stream(**overrides):
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "sample_aspect_ratio": "1:1",
        "avg_frame_rate": "24000/1001",
        "r_frame_rate": "24000/1001",
        "field_order": "progressive",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
        "disposition": {"default": 1},
    }
    stream.update(overrides)
    return stream


def audio_stream(
    index,
    codec="aac",
    language="eng",
    channels=2,
    *,
    default=False,
    title=None,
    comment=False,
):
    tags = {}
    if language is not None:
        tags["language"] = language
    if title:
        tags["title"] = title
    return {
        "index": index,
        "codec_type": "audio",
        "codec_name": codec,
        "channels": channels,
        "tags": tags,
        "disposition": {"default": 1 if default else 0, "comment": 1 if comment else 0},
    }


def subtitle_stream(index, codec="subrip", language="eng", *, forced=False):
    return {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": codec,
        "tags": {"language": language} if language else {},
        "disposition": {"forced": 1 if forced else 0},
    }


def make_probe(
    tmp_path: Path,
    *,
    video=None,
    audio=(),
    subtitles=(),
    folder: str = "Movie (2001)",
    filename: str | None = None,
    duration: float = 100.0,
    size: int = 200_000_000,
) -> ProbeResult:
    parent = tmp_path / folder
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / (filename or f"{folder}.mkv")
    path.write_bytes(b"source")
    streams = [video_stream(**(video or {}))]
    streams.extend(audio)
    streams.extend(subtitles)
    return ProbeResult(
        path,
        {
            "format": {
                "format_name": "matroska,webm",
                "duration": str(duration),
                "size": str(size),
            },
            "streams": streams,
        },
    )


def make_preset(
    *,
    container: str = "mp4",
    action: str | None = None,
    video: dict | None = None,
    audio: dict | None = None,
    subtitles: dict | None = None,
    skip: dict | None = None,
    schedule: dict | None = None,
) -> dict:
    action = action or ("remux" if container == "mkv" else "encode")
    base_video = {
        "action": action,
        "codec": "copy" if container == "mkv" else "hevc",
        "backend_order": [] if container == "mkv" else ["qsv", "vaapi"],
        "allow_software": False,
        "quality": 24,
        "look_ahead": True,
        "max_width": 1920,
        "max_height": 1080,
        "ten_bit_source": None if action == "remux" else "keep",
        "tag": "hvc1",
        "hdr": "review",
        "interlaced": "review",
    }
    base_video.update(video or {})
    base_audio = {
        "languages": ["original", "eng"],
        "max_tracks": 3,
        "copy_codecs": ["aac", "ac3", "eac3"],
        "exclude_commentary": True,
        "unknown_language": "keep_first",
    }
    base_audio.update(audio or {})
    base_subtitles = {
        "languages": ["eng"],
        "include_forced": True,
        "text": "copy" if container == "mkv" else "mov_text",
        "bitmap": "mkv_fallback",
        "mkv_policy": "bitmap_only",
    }
    base_subtitles.update(subtitles or {})
    base_skip = {
        "already_efficient": False,
        "efficient_kbps_1080p": 4000,
        "min_savings_pct": 15,
    }
    base_skip.update(skip or {})
    base_schedule = {"window": ""}
    base_schedule.update(schedule or {})
    return {
        "id": "test-preset",
        "container": container,
        "video": base_video,
        "audio": base_audio,
        "subtitles": base_subtitles,
        "skip": base_skip,
        "schedule": base_schedule,
    }


_DEFAULT_IDENTITY = object()


def build(
    probe: ProbeResult,
    *,
    preset: dict | None = None,
    config: dict | None = None,
    capabilities: dict | None = None,
    identity: dict | None | object = _DEFAULT_IDENTITY,
    speed_factor: float | None = 2.0,
    plan_version: int = 1,
    action_override: str | None = None,
    video_bytes: int | None = 100_000_000,
    name: str | None = None,
    estimated_seconds: float | None = None,
):
    return build_plan(
        source_path=probe.path,
        probe=probe,
        preset=preset or make_preset(),
        config=config if config is not None else {"dry_run": False},
        identity=(
            {"title": "Movie", "year": 2001}
            if identity is _DEFAULT_IDENTITY
            else identity
        ),
        capabilities=CAPS if capabilities is None else capabilities,
        speed_factor=speed_factor,
        plan_version=plan_version,
        action_override=action_override,
        video_bytes=video_bytes,
        name=name,
        estimated_seconds=estimated_seconds,
    )


def test_plan_matches_plan_schema(tmp_path):
    """PLAN_SCHEMA: exact top-level, source, video and name keys."""
    probe = make_probe(
        tmp_path,
        audio=[audio_stream(1, "dts", "eng", 6, default=True)],
        subtitles=[subtitle_stream(2)],
    )
    plan = build(probe)
    assert set(plan) == SCHEMA_KEYS
    assert set(plan["source"]) == SOURCE_KEYS
    assert set(plan["video"]) == VIDEO_KEYS
    assert set(plan["name"]) == {"title", "year", "tmdbid", "stem"}
    assert set(plan["subtitles"][0]) == {
        "index",
        "source_codec",
        "language",
        "forced",
        "action",
        "sidecar",
    }
    assert plan["schema"] == 1
    assert plan["preset"] == "test-preset"
    assert plan["review"] is None
    assert plan["action"] == "encode"
    assert plan["container"] == "mp4"
    assert plan["ext"] == "mp4"
    assert plan["destination_rel"] == "Movie (2001)/Movie (2001).mp4"
    assert plan["source"]["path"] == str(probe.path)
    assert plan["source"]["format"] == "matroska,webm"
    assert plan["source"]["duration"] == pytest.approx(100.0)
    assert plan["source"]["main_video_index"] == 0
    assert plan["video"]["frame_rate"] == "24000/1001"
    assert plan["chapters"] is True
    assert plan["attachments"] is False
    json.dumps(plan)  # must be stored in jobs.plan_json


def test_planner_records_explicit_stream_indexes_and_never_map_zero(tmp_path):
    """MP4-12: the plan carries absolute indexes; -map 0 belongs to the encoder."""
    probe = make_probe(
        tmp_path,
        audio=[audio_stream(1), audio_stream(2, "ac3", "deu")],
        subtitles=[subtitle_stream(3), subtitle_stream(4, "ass", "deu")],
    )
    preset = make_preset(audio={"languages": ["original", "eng", "deu"], "max_tracks": 5})
    plan = build(probe, preset=preset)
    assert [entry["index"] for entry in plan["audio"]] == [1, 2]
    assert [entry["index"] for entry in plan["subtitles"]] == [3]
    rendered = json.dumps(plan)
    assert "-map 0" not in rendered
    assert '"index": 1' in rendered


def test_text_subtitles_convert_to_mov_text_in_mp4(tmp_path):
    """MP4-5: subrip/ass/ssa/webvtt/mov_text all become mov_text."""
    codecs = ["subrip", "ass", "ssa", "webvtt", "mov_text"]
    probe = make_probe(
        tmp_path,
        subtitles=[subtitle_stream(2 + i, codec) for i, codec in enumerate(codecs)],
    )
    plan = build(probe, preset=make_preset(action="remux"))
    assert [entry["source_codec"] for entry in plan["subtitles"]] == codecs
    assert all(entry["action"] == "mov_text" for entry in plan["subtitles"])
    assert plan["container"] == "mp4"


def test_forced_subtitles_only_included_when_allowed(tmp_path):
    """MP4-5/MP4-6: forced tracks obey include_forced."""
    probe = make_probe(
        tmp_path,
        subtitles=[subtitle_stream(2, "subrip", forced=True), subtitle_stream(3, "pgs")],
    )
    preset = make_preset(action="remux", subtitles={"include_forced": False})
    plan = build(probe, preset=preset)
    assert [entry["index"] for entry in plan["subtitles"]] == [3]
    assert plan["subtitles"][0]["forced"] is False

    preset = make_preset(action="remux", subtitles={"include_forced": True})
    plan = build(probe, preset=preset)
    assert [entry["index"] for entry in plan["subtitles"]] == [2, 3]
    assert plan["subtitles"][0]["forced"] is True


def test_bitmap_fallback_switches_to_mkv_and_copies_subtitles(tmp_path):
    """MP4-6/MP4-7: mkv_fallback makes MKV, copies PGS and records the note."""
    probe = make_probe(
        tmp_path,
        audio=[audio_stream(1, "dts", "eng", 6, default=True)],
        subtitles=[subtitle_stream(3, "hdmv_pgs_subtitle"), subtitle_stream(4, "subrip")],
    )
    plan = build(probe, preset=make_preset(action="remux"))
    assert plan["container"] == "mkv"
    assert plan["ext"] == "mkv"
    assert plan["attachments"] is True
    assert plan["destination_rel"].endswith(".mkv")
    assert {entry["index"]: entry["action"] for entry in plan["subtitles"]} == {
        3: "copy",
        4: "copy",
    }
    assert plan["audio"][0]["action"] == "copy"
    assert "container: mkv (bitmap subs)" in plan["warnings"]


def test_bitmap_drop_stays_mp4_and_records_dropped_stream(tmp_path):
    """MP4-6: bitmap=drop records the discarded stream in dropped."""
    probe = make_probe(
        tmp_path,
        subtitles=[subtitle_stream(2, "hdmv_pgs_subtitle"), subtitle_stream(3, "subrip")],
    )
    preset = make_preset(action="remux", subtitles={"bitmap": "drop"})
    plan = build(probe, preset=preset)
    assert plan["container"] == "mp4"
    assert plan["ext"] == "mp4"
    assert [entry["index"] for entry in plan["subtitles"]] == [3]
    assert plan["subtitles"][0]["action"] == "mov_text"
    assert plan["dropped"] == [
        {
            "index": 2,
            "codec": "hdmv_pgs_subtitle",
            "language": "eng",
            "reason": "bitmap-subtitle-not-supported-in-mp4",
        }
    ]
    assert plan["attachments"] is False


def test_mkv_policy_never_forbids_mkv_and_drops_bitmaps(tmp_path):
    """MP4-7: mkv_policy=never never switches container; bitmaps are recorded."""
    probe = make_probe(tmp_path, subtitles=[subtitle_stream(2, "dvb_subtitle")])
    preset = make_preset(
        action="remux", subtitles={"bitmap": "mkv_fallback", "mkv_policy": "never"}
    )
    plan = build(probe, preset=preset)
    assert plan["container"] == "mp4"
    assert plan["subtitles"] == []
    assert plan["dropped"][0]["index"] == 2
    assert plan["dropped"][0]["reason"] == "bitmap-subtitle-not-supported-in-mp4"


def test_mkv_policy_any_subs_selects_mkv_for_text_only(tmp_path):
    """MP4-7: any_subs selects MKV whenever one subtitle is kept."""
    probe = make_probe(tmp_path, subtitles=[subtitle_stream(2, "subrip")])
    preset = make_preset(
        action="remux", subtitles={"mkv_policy": "any_subs"}
    )
    plan = build(probe, preset=preset)
    assert plan["container"] == "mkv"
    assert plan["subtitles"][0]["action"] == "copy"
    assert "container: mkv (any_subs)" in plan["warnings"]

    # No subtitles means no reason to leave MP4.
    no_subs = make_probe(tmp_path, folder="No Subs (2020)")
    plan = build(no_subs, preset=preset)
    assert plan["container"] == "mp4"
    assert plan["warnings"] == []


def test_any_subs_with_bitmap_drop_keeps_text_and_records_bitmap(tmp_path):
    """MP4-6/MP4-7: drop still records bitmaps when text triggers MKV."""
    probe = make_probe(
        tmp_path,
        subtitles=[subtitle_stream(2, "hdmv_pgs_subtitle"), subtitle_stream(3, "subrip")],
    )
    preset = make_preset(
        action="remux",
        subtitles={"bitmap": "drop", "mkv_policy": "any_subs"},
    )
    plan = build(probe, preset=preset)
    assert plan["container"] == "mkv"
    assert [entry["index"] for entry in plan["subtitles"]] == [3]
    assert plan["dropped"] == [
        {
            "index": 2,
            "codec": "hdmv_pgs_subtitle",
            "language": "eng",
            "reason": "bitmap-subtitle-not-supported-in-mp4",
        }
    ]
    assert "container: mkv (any_subs)" in plan["warnings"]


def test_mkv_preset_is_remux_only_and_copies_everything(tmp_path):
    """MKV-1: MKV preset copies video/audio/subtitles, keeps attachments."""
    probe = make_probe(
        tmp_path,
        video={"codec_name": "hevc", "pix_fmt": "yuv420p10le", "color_transfer": "bt709"},
        audio=[audio_stream(1, "truehd", "eng", 8, default=True)],
        subtitles=[subtitle_stream(2, "hdmv_pgs_subtitle")],
    )
    preset = make_preset(container="mkv")
    plan = build(probe, preset=preset, capabilities={})
    assert plan["action"] == "remux"
    assert plan["container"] == "mkv"
    assert plan["video"]["codec"] == "hevc"
    assert plan["video"]["tag"] is None
    assert plan["video"]["backend"] is None
    assert plan["audio"][0]["action"] == "copy"
    assert plan["subtitles"][0]["action"] == "copy"
    assert plan["attachments"] is True
    assert plan["chapters"] is True


def test_mkv_preset_cannot_encode(tmp_path):
    """MKV-1: an MKV preset with action=encode is rejected."""
    probe = make_probe(tmp_path)
    with pytest.raises(PlanError, match="remux-only"):
        build(probe, preset=make_preset(container="mkv", action="encode"))
    with pytest.raises(PlanError, match="remux-only"):
        build(
            probe,
            preset=make_preset(container="mkv"),
            action_override="encode",
        )


@pytest.mark.parametrize("action", ["encode", "remux"])
def test_hdr_reviews_unless_keep(tmp_path, action):
    """MP4-10: HDR/HLG/DV sources are REVIEW unless the preset keeps them."""
    probe = make_probe(
        tmp_path,
        video={"codec_name": "hevc", "pix_fmt": "yuv420p10le",
               "color_transfer": "smpte2084", "color_primaries": "bt2020"},
        audio=[],
        subtitles=[],
    )
    preset = make_preset(action=action, video={"hdr": "review"})
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=preset)
    assert excinfo.value.reason == "hdr"
    assert excinfo.value.from_state == "PLANNED"


def test_hdr_keep_remux_ignores_capabilities(tmp_path):
    """MP4-10: a keep remux copies HDR without any hardware capability."""
    probe = make_probe(
        tmp_path,
        video={"codec_name": "hevc", "pix_fmt": "yuv420p10le",
               "color_transfer": "smpte2084", "color_primaries": "bt2020"},
        audio=[],
        subtitles=[],
    )
    plan = build(
        probe,
        preset=make_preset(action="remux", video={"hdr": "keep"}),
        capabilities={},
    )
    assert plan["action"] == "remux"
    assert plan["video"]["hdr"] is True
    assert plan["video"]["dolby_vision"] is False
    assert plan["video"]["bit_depth"] == 10
    assert plan["video"]["backend"] is None
    assert plan["video"]["tag"] == "hvc1"


def test_hdr_keep_encode_requires_ten_bit_keep(tmp_path):
    """MP4-10: an HDR keep encode without ten_bit_source=keep is REVIEW hdr."""
    probe = make_probe(
        tmp_path,
        video={"pix_fmt": "yuv420p10le", "color_transfer": "smpte2084"},
    )
    preset = make_preset(video={"hdr": "keep", "ten_bit_source": None})
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=preset)
    assert excinfo.value.reason == "hdr"


def test_hdr_keep_encode_uses_10bit_hardware(tmp_path):
    """MP4-10/SIZE-4: keep encode selects a working 10-bit hardware backend."""
    probe = make_probe(
        tmp_path,
        video={"pix_fmt": "yuv420p10le", "color_transfer": "smpte2084"},
    )
    preset = make_preset(video={"hdr": "keep", "ten_bit_source": "keep"})
    plan = build(probe, preset=preset, capabilities=CAPS)
    assert plan["action"] == "encode"
    assert plan["video"]["backend"] == "qsv"
    assert plan["video"]["encoder"] == "hevc_qsv"
    assert plan["video"]["bit_depth"] == 10
    assert plan["video"]["pix_fmt"] == "yuv420p10le"
    assert plan["video"]["hdr"] is True


def test_10bit_keep_without_hardware_reviews_or_uses_software_window(tmp_path):
    """MP4-10: no 10-bit hardware is 10bit-no-hw unless software is windowed."""
    probe = make_probe(
        tmp_path,
        video={"pix_fmt": "yuv420p10le", "color_transfer": "bt709"},
    )
    preset = make_preset(video={"ten_bit_source": "keep"})
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=preset, capabilities={"qsv": {"hevc8": True}})
    assert excinfo.value.reason == "10bit-no-hw"

    software = make_preset(
        video={"ten_bit_source": "keep", "allow_software": True},
        schedule={"window": "22:00-06:00"},
    )
    plan = build(probe, preset=software, capabilities={"qsv": {"hevc8": True}})
    assert plan["video"]["backend"] == "software"
    assert plan["video"]["encoder"] == "libx265"
    assert plan["video"]["bit_depth"] == 10

    no_window = make_preset(
        video={"ten_bit_source": "keep", "allow_software": True},
        schedule={"window": ""},
    )
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=no_window, capabilities={})
    assert excinfo.value.reason == "10bit-no-hw"


def test_10bit_review_and_to8_policies(tmp_path):
    """MP4-10: review raises 10bit; to8 warns, records and changes pix_fmt only."""
    probe = make_probe(
        tmp_path,
        video={"pix_fmt": "yuv420p10le", "color_transfer": "bt709", "frame_rate": "24000/1001"},
    )
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=make_preset(video={"ten_bit_source": "review"}))
    assert excinfo.value.reason == "10bit"

    to8 = make_preset(video={"ten_bit_source": "to8"})
    plan = build(probe, preset=to8)
    assert plan["video"]["bit_depth"] == 8
    assert plan["video"]["pix_fmt"] == "yuv420p"
    assert plan["video"]["frame_rate"] == "24000/1001"
    assert any("10bit_to_8" in warning for warning in plan["warnings"])
    assert plan["dropped"] == [
        {
            "type": "video",
            "index": 0,
            "reason": "10bit-to-8",
            "from": "yuv420p10le",
            "to": "yuv420p",
        }
    ]


def test_10bit_remux_ignores_capability_and_policy(tmp_path):
    """MP4-10: a remux copies a 10-bit stream and never needs encode capability."""
    probe = make_probe(
        tmp_path,
        video={"codec_name": "hevc", "pix_fmt": "yuv420p10le", "color_transfer": "bt709"},
    )
    plan = build(
        probe,
        preset=make_preset(action="remux", video={"hdr": "keep"}),
        capabilities={},
    )
    assert plan["action"] == "remux"
    assert plan["video"]["bit_depth"] == 10
    assert plan["video"]["pix_fmt"] == "yuv420p10le"


@pytest.mark.parametrize("action", ["encode", "remux"])
def test_interlaced_always_reviews(tmp_path, action):
    """MP4-11: interlaced sources are REVIEW regardless of action."""
    probe = make_probe(tmp_path, video={"field_order": "tt"})
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=make_preset(action=action))
    assert excinfo.value.reason == "interlaced"


def test_mp4_remux_rejects_incompatible_video(tmp_path):
    """MP4-2: only H.264/HEVC may be stream-copied into MP4."""
    probe = make_probe(tmp_path, video={"codec_name": "mpeg4"})
    with pytest.raises(ReviewRequired) as excinfo:
        build(probe, preset=make_preset(action="remux"))
    assert excinfo.value.reason == "remux-incompatible-video"

    # MKV has no codec restrictions.
    mkv = build(probe, preset=make_preset(container="mkv"), capabilities={})
    assert mkv["container"] == "mkv"
    assert mkv["video"]["codec"] == "mpeg4"


@pytest.mark.parametrize(
    "codec,tag",
    [
        ("hevc", "hvc1"),
        ("h264", None),
    ],
)
def test_mp4_remux_video_tag(tmp_path, codec, tag):
    """MP4-2/MP4-1: HEVC remuxes are tagged hvc1; H.264 needs no tag."""
    probe = make_probe(tmp_path, video={"codec_name": codec})
    plan = build(probe, preset=make_preset(action="remux"))
    assert plan["action"] == "remux"
    assert plan["video"]["tag"] == tag


def test_backend_falls_back_to_vaapi_and_software_requires_window(tmp_path):
    """MP4-2/SCHED-1: backend order and the software schedule-window gate."""
    probe = make_probe(tmp_path)
    plan = build(probe, capabilities={"vaapi": {"hevc8": True}})
    assert plan["video"]["backend"] == "vaapi"
    assert plan["video"]["encoder"] == "hevc_vaapi"

    plan = build(probe, capabilities={"qsv": {"hevc8": False}, "vaapi": {"hevc8": True}})
    assert plan["video"]["backend"] == "vaapi"

    with pytest.raises(PlanError):
        build(probe, capabilities={})

    software = make_preset(
        video={"allow_software": True}, schedule={"window": "21:00-07:00"}
    )
    plan = build(probe, preset=software, capabilities={})
    assert plan["video"]["backend"] == "software"
    assert plan["video"]["encoder"] == "libx265"

    with pytest.raises(PlanError):
        build(
            probe,
            preset=make_preset(video={"allow_software": True}, schedule={"window": ""}),
            capabilities={},
        )


def test_naming_identity_tmdbid_and_destination(tmp_path):
    """MP4-8/naming: identity title/year/tmdbid produce the destination stem."""
    probe = make_probe(tmp_path)
    plan = build(
        probe,
        identity={"title": "2001: A Space Odyssey", "year": 1968, "tmdbid": 62},
        config={"dry_run": False, "append_tmdbid": True},
    )
    assert plan["name"]["title"] == "2001 - A Space Odyssey"
    assert plan["name"]["year"] == 1968
    assert plan["name"]["tmdbid"] == 62
    assert plan["name"]["stem"] == "2001 - A Space Odyssey (1968) [tmdbid-62]"
    assert plan["destination_rel"] == (
        "2001 - A Space Odyssey (1968) [tmdbid-62]/"
        "2001 - A Space Odyssey (1968) [tmdbid-62].mp4"
    )


def test_naming_falls_back_to_parsed_source_folder(tmp_path):
    probe = make_probe(
        tmp_path,
        folder="Some Movie (1999)",
        filename="Some.Movie.1999.1080p.BluRay.x264-GRP.mkv",
    )
    plan = build(probe, identity=None)
    assert plan["name"]["year"] == 1999
    assert "Some Movie" in plan["name"]["title"]
    assert plan["name"]["stem"].startswith("Some Movie (1999)")


def test_explicit_name_override_and_dry_run(tmp_path):
    probe = make_probe(tmp_path)
    plan = build(probe, name="Custom: Name", config={"dry_run": True})
    assert plan["name"]["stem"] == "Custom - Name"
    assert plan["destination_rel"] == "Custom - Name/Custom - Name.mp4"
    assert plan["dry_run"] is True

    plan = build(probe, config={"dry_run": False})
    assert plan["dry_run"] is False


def test_sidecars_are_carried_with_language_and_srt_destination(tmp_path):
    """MP4-8: same-stem srt/ass/vtt sidecars are planned with a language."""
    probe = make_probe(tmp_path)
    folder = probe.path.parent
    (folder / "Movie (2001).en.srt").write_text("sub")
    (folder / "Movie (2001).fr.ass").write_text("sub")
    (folder / "Movie (2001).vtt").write_text("sub")
    plan = build(probe)
    sidecars = plan["sidecars"]
    assert [side["language"] for side in sidecars] == ["en", "fr", "und"]
    assert [side["action"] for side in sidecars] == ["copy", "convert", "convert"]
    assert all(Path(side["path"]).is_absolute() for side in sidecars)
    assert all(Path(side["path"]).is_file() for side in sidecars)


def test_estimate_and_speed_source(tmp_path):
    probe = make_probe(tmp_path)
    plan = build(probe, speed_factor=2.0)
    assert plan["estimated_seconds"] == pytest.approx(50.0)
    assert plan["speed_source"] == "table"

    plan = build(probe, speed_factor=None)
    assert plan["estimated_seconds"] is None
    assert plan["speed_source"] == "missing"

    plan = build(probe, speed_factor=2.0, estimated_seconds=7.5)
    assert plan["estimated_seconds"] == pytest.approx(7.5)
    assert plan["speed_source"] == "table"


def test_plan_version_and_fallback_from(tmp_path):
    probe = make_probe(tmp_path, video={"codec_name": "hevc"})
    preset = make_preset(action="remux")
    plan = build(probe, preset=preset)
    assert plan["plan_version"] == 1
    assert plan["fallback_from"] is None
    fallback = build(probe, preset=preset, plan_version=2)
    assert fallback["fallback_from"] == "encode"
    assert fallback["plan_version"] == 2


def test_audio_fallback_is_integrated_in_plan(tmp_path):
    """MP4-4: a commentary-only source still gets audio plus fallback_used."""
    probe = make_probe(
        tmp_path,
        audio=[audio_stream(1, "aac", "eng", 2, default=True, title="Commentary")],
    )
    plan = build(probe)
    assert len(plan["audio"]) == 1
    assert "fallback_used" in plan["warnings"]


def test_select_subtitles_returns_effective_container_note_and_indexes(tmp_path):
    streams = [subtitle_stream(7, "hdmv_pgs_subtitle"), subtitle_stream(8, "subrip")]
    choices, dropped, container, note = select_subtitles(
        streams,
        {"languages": ["eng"], "include_forced": True, "bitmap": "mkv_fallback",
         "mkv_policy": "bitmap_only"},
        "matroska,webm",
        "mp4",
        "bitmap_only",
        100.0,
    )
    assert [choice["index"] for choice in choices] == [7, 8]
    assert dropped == []
    assert container == "mkv"
    assert note == "container: mkv (bitmap subs)"
    assert all(choice["action"] == "copy" for choice in choices)


def test_source_video_bytes_are_derived_from_stream_metadata(tmp_path):
    """SIZE-1: bit_rate is a derivable video_bytes source."""
    probe = make_probe(
        tmp_path,
        video={"codec_name": "hevc", "bit_rate": "2000000", "width": 1280, "height": 720},
    )
    plan = build(
        probe,
        video_bytes=0,
        preset=make_preset(skip={"already_efficient": True}),
    )
    assert plan["source"]["video_bytes"] == 25_000_000
    assert plan["action"] == "remux"  # 2000 kbps < 2178 at 720p
