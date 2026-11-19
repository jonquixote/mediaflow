"""Already-efficient, not-smaller fallback and size/aspect rules.

Requirement markers: SIZE-1 (pixel-scaled threshold and remux decision),
SIZE-2 (fallback plan shape and reused selections), SIZE-3 (remux exemption),
SIZE-4 (frame rate, SAR and no upscale) and MP4-2 (remux compatibility).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mediaflow.plan import (
    ReviewRequired,
    build_plan,
    compute_dimensions,
    efficient_threshold_kbps,
)
from mediaflow.probe import ProbeResult


def make_probe(
    tmp_path: Path,
    *,
    codec: str = "hevc",
    width: int = 1920,
    height: int = 1080,
    duration: float = 100.0,
    bit_rate: str | None = None,
    sar: str = "1:1",
    frame_rate: str = "24000/1001",
    with_audio: bool = False,
    with_subtitle: bool = False,
) -> ProbeResult:
    path = tmp_path / "Movie (2001).mkv"
    path.write_bytes(b"source")
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": codec,
            "width": width,
            "height": height,
            "pix_fmt": "yuv420p",
            "sample_aspect_ratio": sar,
            "avg_frame_rate": frame_rate,
            "field_order": "progressive",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_space": "bt709",
            "color_range": "tv",
            "disposition": {"default": 1},
            **({"bit_rate": bit_rate} if bit_rate else {}),
        }
    ]
    if with_audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "dts",
                "channels": 6,
                "tags": {"language": "eng"},
                "disposition": {"default": 1},
            }
        )
    if with_subtitle:
        streams.append(
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "eng"},
                "disposition": {},
            }
        )
    return ProbeResult(
        path,
        {
            "format": {
                "format_name": "matroska,webm",
                "duration": str(duration),
                "size": "200000000",
            },
            "streams": streams,
        },
    )


def preset(
    *,
    action: str = "encode",
    skip_already_efficient: bool = True,
    container: str = "mp4",
    codec: str = "hevc",
    backend_order=("qsv", "vaapi"),
    max_width: int = 1920,
    max_height: int = 1080,
) -> dict:
    return {
        "id": "test-preset",
        "container": container,
        "video": {
            "action": action,
            "codec": codec,
            "backend_order": list(backend_order),
            "allow_software": False,
            "quality": 24,
            "look_ahead": True,
            "max_width": max_width,
            "max_height": max_height,
            "ten_bit_source": "keep" if action == "encode" else None,
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
            "already_efficient": skip_already_efficient,
            "efficient_kbps_1080p": 4000,
            "min_savings_pct": 15,
        },
        "schedule": {"window": ""},
    }


CAPS = {"qsv": {"hevc8": True, "hevc10": True}}


def build(probe, *, video_bytes=None, plan_version=1, action_override=None, p=None):
    return build_plan(
        source_path=probe.path,
        probe=probe,
        preset=p or preset(),
        config={"dry_run": False},
        identity={"title": "Movie", "year": 2001},
        capabilities=CAPS,
        speed_factor=2.0,
        plan_version=plan_version,
        action_override=action_override,
        video_bytes=video_bytes,
    )


def test_efficient_threshold_numbers():
    """SIZE-1: 4000 kbps at 1080p scales to ~2178 at 720p and ~11314 at 2160p."""
    assert efficient_threshold_kbps(4000, 1280, 720) == pytest.approx(2177.3, abs=1.0)
    assert efficient_threshold_kbps(4000, 3840, 2160) == pytest.approx(11313.7, abs=1.0)
    assert efficient_threshold_kbps(4000, 1920, 1080) == pytest.approx(4000)
    assert efficient_threshold_kbps(0, 1920, 1080) == 0


def test_already_efficient_hevc_remuxes_even_without_capabilities(tmp_path):
    """SIZE-1: HEVC below threshold becomes remux, not encode."""
    probe = make_probe(tmp_path, width=1280, height=720)
    # 10 MB over 100 s = 800 kbps, below ~2178.
    plan = build_plan(
        source_path=probe.path,
        probe=probe,
        preset=preset(),
        config={"dry_run": False},
        identity={"title": "Movie", "year": 2001},
        capabilities={},
        speed_factor=2.0,
        video_bytes=10_000_000,
    )
    assert plan["action"] == "remux"
    assert plan["video"]["backend"] is None
    assert plan["source"]["video_bytes"] == 10_000_000
    assert any("already_efficient" in warning for warning in plan["warnings"])
    assert plan["video"]["tag"] == "hvc1"


def test_not_efficient_hevc_encodes(tmp_path):
    """SIZE-1: above the threshold the encode preset is preserved."""
    probe = make_probe(tmp_path, width=1280, height=720)
    # 100 MB over 100 s = 8000 kbps, above ~2178.
    plan = build(probe, video_bytes=100_000_000)
    assert plan["action"] == "encode"
    assert plan["video"]["backend"] == "qsv"
    assert plan["video"]["encoder"] == "hevc_qsv"
    assert not any("already_efficient" in warning for warning in plan["warnings"])


def test_already_efficient_can_be_disabled(tmp_path):
    """SIZE-1: skip.already_efficient = false keeps encoding."""
    probe = make_probe(tmp_path, width=1280, height=720)
    plan = build(probe, video_bytes=10_000_000, p=preset(skip_already_efficient=False))
    assert plan["action"] == "encode"


def test_already_efficient_only_for_hevc(tmp_path):
    """SIZE-1: an efficient H.264 source is not remuxed by this rule."""
    probe = make_probe(tmp_path, codec="h264", width=1280, height=720)
    plan = build(probe, video_bytes=10_000_000, p=preset(codec="hevc"))
    assert plan["action"] == "encode"


def test_already_efficient_scales_to_2160p(tmp_path):
    """SIZE-1: 4K threshold is ~11314 kbps."""
    probe = make_probe(tmp_path, width=3840, height=2160)
    # 125 MB / 100 s = 10000 kbps < ~11314.
    plan = build(probe, video_bytes=125_000_000)
    assert plan["action"] == "remux"


def test_already_efficient_uses_stream_bit_rate_when_bytes_are_missing(tmp_path):
    """SIZE-1: a stream bit_rate is enough to derive the threshold decision."""
    probe = make_probe(tmp_path, width=1280, height=720, bit_rate="800000")
    plan = build(probe, video_bytes=0)
    assert plan["action"] == "remux"
    assert plan["source"]["video_bytes"] > 0


def test_size2_fallback_remux_shape_and_reused_selections(tmp_path):
    """SIZE-2: plan_version+1 remux records fallback_from=encode and reuses A/S."""
    probe = make_probe(tmp_path, with_audio=True, with_subtitle=True)
    encode_preset = preset(skip_already_efficient=False)
    media_bytes = 100_000_000  # 8 Mbps, above the SIZE-1 threshold
    initial = build(probe, video_bytes=media_bytes, p=encode_preset)
    assert initial["action"] == "encode"
    fallback = build(
        probe,
        video_bytes=media_bytes,
        p=encode_preset,
        plan_version=2,
        action_override="remux",
    )
    assert fallback["action"] == "remux"
    assert fallback["plan_version"] == 2
    assert fallback["fallback_from"] == "encode"
    assert fallback["audio"] == initial["audio"]
    assert fallback["subtitles"] == initial["subtitles"]
    assert fallback["container"] == "mp4"


def test_size3_remux_jobs_are_exempt_from_must_be_smaller(tmp_path):
    """SIZE-3: a remux plan is selected regardless of source video size."""
    probe = make_probe(tmp_path, codec="h264", with_audio=True)
    plan = build(
        probe,
        video_bytes=500_000_000,
        p=preset(action="remux", codec="copy", skip_already_efficient=False),
    )
    assert plan["action"] == "remux"
    assert plan["video"]["codec"] == "h264"
    assert plan["fallback_from"] is None


def test_size4_keeps_frame_rate_sar_and_explicit_even_dimensions(tmp_path):
    """SIZE-4/SIZE-5: scaling keeps fps and SAR; dimensions stay even/capped."""
    probe = make_probe(
        tmp_path,
        width=1440,
        height=1080,
        sar="4:3",
        frame_rate="30000/1001",
    )
    plan = build(
        probe,
        video_bytes=100_000_000,
        p=preset(max_width=1280, max_height=720),
    )
    video = plan["video"]
    assert (video["target_width"], video["target_height"]) == (960, 720)
    assert video["scaled"] is True
    assert video["sar"] == "4:3"
    assert video["frame_rate"] == "30000/1001"
    assert video["target_width"] <= 1280 and video["target_height"] <= 720
    assert compute_dimensions(1440, 1080, "4:3", 1920, 1080)[2] is False


def test_mp4_remux_rejects_non_h264_hevc_video(tmp_path):
    """MP4-2: MPEG-2 video cannot be remuxed into MP4; REVIEW with the right reason."""
    probe = make_probe(tmp_path, codec="mpeg2video")
    with pytest.raises(ReviewRequired) as caught:
        build(probe, p=preset(action="remux", skip_already_efficient=False))
    assert "remux-incompatible-video" in str(getattr(caught.value, "reason", caught.value))
