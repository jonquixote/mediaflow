"""ffprobe safety and stream-inspection tests.

Requirement markers covered here: SAF-13 (argv, whitelist, regular files and
container allowlist), plus the planner-facing probe helpers used by SIZE-1 and
SIZE-3.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mediaflow.probe import (
    ALLOWED_FORMATS,
    ProbeError,
    ProbeFailed,
    ProbeResult,
    UnsupportedFormat,
    container_allowed,
    detect_review_conditions,
    ffprobe_json,
    find_sidecars,
    hdr_flags,
    is_10bit,
    is_interlaced,
    probe_media,
    video_packet_bytes,
)

FFPROBE = "/usr/bin/ffprobe-test"


def _raw(*, video_extra=None, audio=None, subtitle=None, duration="123.45", size="1000000"):
    video = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p10le",
        "sample_aspect_ratio": "1:1",
        "avg_frame_rate": "24000/1001",
        "r_frame_rate": "24000/1001",
        "field_order": "progressive",
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_space": "bt2020nc",
        "color_range": "tv",
        "disposition": {"default": 1},
        "tags": {"language": "eng", "title": "Feature"},
    }
    if video_extra:
        video.update(video_extra)
    streams = [video]
    if audio:
        streams.extend(audio)
    if subtitle:
        streams.extend(subtitle)
    return {
        "format": {
            "format_name": "matroska,webm",
            "duration": duration,
            "size": size,
            "bit_rate": "800000",
        },
        "streams": streams,
        "chapters": [{"id": 0, "start_time": "0.0", "end_time": "10.0"}],
    }


def _file(tmp_path: Path, name: str = "movie.mkv") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not really media")
    return path


def _patch_run(monkeypatch, payload, *, returncode=0, stderr="", captured=None):
    calls = captured if captured is not None else []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        stdout = json.dumps(payload) if not isinstance(payload, str) else payload
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("mediaflow.probe.subprocess.run", fake_run)
    return calls


def test_allowed_formats_are_explicit():
    """SAF-13: only Matroska/WebM, MOV/MP4, AVI, MPEG-TS/PS and ASF pass."""
    for name in (
        "matroska",
        "matroska,webm",
        "webm",
        "mov",
        "mov,mp4,m4a,3gp,3g2,mj2",
        "avi",
        "mpegts",
        "mpeg",
        "asf",
    ):
        assert container_allowed(name) is True, name
    for name in (
        "hls",
        "applehttp",
        "m3u8",
        "matroska,hls",
        "concat",
        "image2",
        "lavfi",
        "ogg",
        "flv",
        "rawvideo",
        "",
        None,
    ):
        assert container_allowed(name) is False, name
    assert {"matroska", "webm", "mov", "mp4", "avi", "mpegts", "mpeg", "asf"} <= ALLOWED_FORMATS


def test_probe_media_builds_safe_argv_and_exposes_conveniences(tmp_path, monkeypatch):
    """SAF-13: exact explicit argv, no shell, whitelist, JSON output."""
    path = _file(tmp_path)
    payload = _raw(
        audio=[{"index": 1, "codec_type": "audio", "codec_name": "dts", "channels": 6,
                "tags": {"language": "eng"}}],
        subtitle=[{"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                   "tags": {"language": "eng"}}],
    )
    calls = _patch_run(monkeypatch, payload)

    result = probe_media(path, ffprobe=FFPROBE, timeout=9)

    assert isinstance(result, ProbeResult)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == FFPROBE
    assert argv[-1] == str(path)
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,crypto,data"
    for flag in ("-show_format", "-show_streams", "-show_chapters"):
        assert flag in argv
    assert argv[argv.index("-of") + 1] == "json"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 9
    assert kwargs.get("shell") is not True
    assert result.streams and result.video_streams and result.audio_streams
    assert result.subtitle_streams[0]["index"] == 2
    assert result.duration == pytest.approx(123.45)
    assert result.size == 1_000_000
    assert result.format_name == "matroska,webm"
    assert result.main_video["index"] == 0
    assert result.chapters and result.raw == payload


def test_probe_rejects_missing_directory_and_symlink(tmp_path, monkeypatch):
    """SAF-13: only regular non-symlink files are processed."""
    calls = _patch_run(monkeypatch, _raw())
    missing = tmp_path / "missing.mkv"
    with pytest.raises(ProbeError):
        probe_media(missing, ffprobe=FFPROBE)
    with pytest.raises(ProbeError):
        probe_media(tmp_path, ffprobe=FFPROBE)

    real = _file(tmp_path, "real.mkv")
    link = tmp_path / "link.mkv"
    link.symlink_to(real)
    with pytest.raises(ProbeError):
        probe_media(link, ffprobe=FFPROBE)
    assert calls == []


def test_probe_rejects_playlist_suffix_before_ffprobe(tmp_path, monkeypatch):
    """SAF-13: .m3u8/.strm/.url playlists are unsupported, never followed."""
    calls = _patch_run(monkeypatch, _raw())
    for suffix in (".m3u8", ".strm", ".url"):
        path = _file(tmp_path, f"playlist{suffix}")
        with pytest.raises(UnsupportedFormat):
            probe_media(path, ffprobe=FFPROBE)
    assert calls == []


def test_probe_rejects_unsupported_container(tmp_path, monkeypatch):
    """SAF-13: a probed container outside the allowlist is UnsupportedFormat."""
    path = _file(tmp_path)
    payload = _raw()
    payload["format"]["format_name"] = "hls"
    _patch_run(monkeypatch, payload)
    with pytest.raises(UnsupportedFormat):
        probe_media(path, ffprobe=FFPROBE)


def test_probe_failures_are_wrapped(tmp_path, monkeypatch):
    path = _file(tmp_path)
    _patch_run(monkeypatch, "{}", returncode=1, stderr="boom\nbad input\n")
    with pytest.raises(ProbeFailed, match="bad input"):
        probe_media(path, ffprobe=FFPROBE)

    _patch_run(monkeypatch, "not json")
    with pytest.raises(ProbeFailed):
        probe_media(path, ffprobe=FFPROBE)

    def timeout_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr("mediaflow.probe.subprocess.run", timeout_run)
    with pytest.raises(ProbeFailed, match="timed out"):
        probe_media(path, ffprobe=FFPROBE)


def test_ffprobe_json_protects_mandatory_flags(tmp_path, monkeypatch):
    path = _file(tmp_path)
    calls = _patch_run(monkeypatch, {})
    with pytest.raises(ProbeError):
        ffprobe_json(path, ["-protocol_whitelist", "file"], ffprobe=FFPROBE)
    with pytest.raises(ProbeError):
        ffprobe_json(path, ["-of", "csv"], ffprobe=FFPROBE)
    assert calls == []
    payload = ffprobe_json(path, ["-show_entries", "format=format_name"], ffprobe=FFPROBE)
    assert payload == {}


def test_video_packet_bytes_sums_matching_packets(tmp_path, monkeypatch):
    """SIZE-1/SIZE-3: packet sizes are summed for one absolute video index."""
    path = _file(tmp_path)
    payload = {
        "packets": [
            {"stream_index": 0, "size": "100"},
            {"stream_index": 1, "size": "999"},
            {"stream_index": 0, "size": 250},
            {"stream_index": 0, "size": "bad"},
            {"size": 5},
        ]
    }
    calls = _patch_run(monkeypatch, payload)
    assert video_packet_bytes(path, 0, ffprobe=FFPROBE, timeout=4) == 355
    argv, kwargs = calls[0]
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,crypto,data"
    assert argv[argv.index("-select_streams") + 1] == "0"
    assert "packet=stream_index,size" in argv
    assert argv[argv.index("-of") + 1] == "json"
    assert kwargs["timeout"] == 4


def test_main_video_prefers_longest_real_stream_and_ignores_cover_art(tmp_path):
    path = _file(tmp_path)
    payload = _raw()
    payload["streams"] = [
        {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "duration": "999",
         "size": "999999", "disposition": {"attached_pic": 1}},
        {"index": 1, "codec_type": "video", "codec_name": "hevc", "duration": "100",
         "size": "1000"},
        {"index": 2, "codec_type": "video", "codec_name": "h264", "duration": "120",
         "size": "500"},
        {"index": 3, "codec_type": "audio", "codec_name": "aac"},
    ]
    result = ProbeResult(path, payload)
    assert result.main_video["index"] == 2
    # Single-video files simply use that stream.
    one = ProbeResult(path, {"format": {}, "streams": [payload["streams"][0]]})
    assert one.main_video["index"] == 0


def test_detect_review_conditions_and_stream_classifiers():
    stream = {
        "codec_type": "video",
        "pix_fmt": "yuv420p10le",
        "color_transfer": "smpte2084",
        "field_order": "tt",
    }
    flags = hdr_flags(stream)
    assert flags["hdr"] is True and flags["hdr10"] is True
    assert flags["dolby_vision"] is False
    assert is_10bit(stream) is True
    assert is_interlaced(stream) is True
    result = ProbeResult(Path("/tmp/x.mkv"), {"format": {}, "streams": [stream]})
    assert detect_review_conditions(result) == ["hdr", "10bit", "interlaced"]


@pytest.mark.parametrize(
    "stream, expected",
    [
        ({"pix_fmt": "nv12"}, False),
        ({"pix_fmt": "yuv420p"}, False),
        ({"pix_fmt": "p010le"}, True),
        ({"pix_fmt": "yuv422p10le"}, True),
        ({"bits_per_raw_sample": "12"}, True),
        ({}, False),
    ],
)
def test_is_10bit_avoids_nv12_false_positive(stream, expected):
    assert is_10bit(stream) is expected


def test_hdr_detection_variants():
    assert hdr_flags({"color_transfer": "arib-std-b67"})["format"] == "hlg"
    assert hdr_flags({"codec_tag_string": "dvh1"})["dolby_vision"] is True
    assert hdr_flags({"side_data_list": [{"side_data_type": "DOVI configuration record"}]})[
        "dolby_vision"
    ] is True
    assert hdr_flags({"color_transfer": "bt709"})["hdr"] is False
    assert is_interlaced({"field_order": "progressive"}) is False
    assert is_interlaced({}) is False


def test_find_sidecars_language_variants_and_symlink_safety(tmp_path):
    """MP4-8/SAF-13: same-stem text sidecars, regular files only."""
    source = _file(tmp_path, "Movie (2001).mkv")
    base = tmp_path / "Movie (2001)"
    expected = [
        tmp_path / "Movie (2001).ass",
        tmp_path / "Movie (2001).en.srt",
        tmp_path / "Movie (2001).en.forced.vtt",
        tmp_path / "Movie (2001).srt",
    ]
    for path in expected:
        path.write_text("subtitle")
    (tmp_path / "Movie (2001)").mkdir()
    (tmp_path / "Movie (2001).srt").unlink()  # replace with a directory
    (tmp_path / "Movie (2001).srt").mkdir()
    (tmp_path / "Other.srt").write_text("other")
    (tmp_path / "Movie (2001).en.sup").write_text("not a text sidecar")
    real = tmp_path / "Movie (2001).de.srt"
    real.write_text("de")
    link = tmp_path / "Movie (2001).fr.srt"
    link.symlink_to(real)

    found = find_sidecars(source)
    assert found == [
        tmp_path / "Movie (2001).ass",
        tmp_path / "Movie (2001).de.srt",
        tmp_path / "Movie (2001).en.forced.vtt",
        tmp_path / "Movie (2001).en.srt",
    ]
    assert all(path.is_file() and not path.is_symlink() for path in found)
    assert find_sidecars(tmp_path / "NoSuch.mkv") == []
