"""Verification tests using synthetic lavfi media.

Most checks are exercised against real, tiny files made with ffmpeg; a few
edge cases (extra video streams, a truncated packet tail) monkeypatch the
ffprobe boundary.  Requirement markers: SAF-2, SIZE-3, MP4-1, MP4-9, MP4-12.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import mediaflow.verify as verify_mod
from mediaflow.ffmpeg import FFMPEG, FFPROBE
from mediaflow.verify import VideoVerifyResult, verify_output, video_packet_bytes


# ---------------------------------------------------------------------------
# media fixtures


def _run_ffmpeg(*args: str, timeout: float = 30) -> None:
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict:
    directory = tmp_path_factory.mktemp("verify-media")

    source = directory / "source.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=1",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        str(source),
    )

    remux = directory / "remux.mp4"
    _run_ffmpeg(
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(remux),
    )

    long_video = directory / "long.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=25:duration=3",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        str(long_video),
    )

    hevc_hvc1 = directory / "hevc_hvc1.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=32x32:rate=10:duration=0.2",
        "-c:v",
        "libx265",
        "-preset",
        "ultrafast",
        "-x265-params",
        "log-level=error",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "hvc1",
        str(hevc_hvc1),
    )
    hevc_hev1 = directory / "hevc_hev1.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=32x32:rate=10:duration=0.2",
        "-c:v",
        "libx265",
        "-preset",
        "ultrafast",
        "-x265-params",
        "log-level=error",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "hev1",
        str(hevc_hev1),
    )

    low = directory / "low.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=10:duration=0.5",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "51",
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
        str(low),
    )

    subtitles = directory / "subs.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:00,800\nHello\n", encoding="utf-8"
    )
    subtitled = directory / "subtitled.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=1",
        "-i",
        str(subtitles),
        "-map",
        "0:v:0",
        "-map",
        "1:s:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:s",
        "mov_text",
        "-t",
        "1",
        str(subtitled),
    )

    return {
        "dir": directory,
        "source": source,
        "remux": remux,
        "long": long_video,
        "hevc_hvc1": hevc_hvc1,
        "hevc_hev1": hevc_hev1,
        "low": low,
        "subtitled": subtitled,
        "source_video_bytes": video_packet_bytes(source),
        "remux_video_bytes": video_packet_bytes(remux),
    }


# ---------------------------------------------------------------------------
# plan builder


def _plan(
    source: Path,
    *,
    action: str = "encode",
    container: str = "mp4",
    duration: float = 1.0,
    target: tuple[int, int] = (320, 240),
    frame_rate: str = "25/1",
    bit_depth: int = 8,
    sar: str = "1:1",
    video_codec: str = "h264",
    pix_fmt: str = "yuv420p",
    audio_count: int = 1,
    subtitles: int = 0,
    source_video_bytes: int = 10**9,
    color: dict | None = None,
) -> dict:
    audio = [
        {
            "index": 1 + index,
            "source_codec": "aac",
            "language": "eng",
            "channels": 2,
            "action": "copy",
            "codec": "aac",
            "bitrate": 96,
            "default": index == 0,
        }
        for index in range(audio_count)
    ]
    subtitle_entries = [
        {
            "index": 1 + audio_count + index,
            "source_codec": "subrip",
            "language": "eng",
            "forced": False,
            "action": "mov_text",
            "sidecar": None,
        }
        for index in range(subtitles)
    ]
    return {
        "schema": 1,
        "action": action,
        "container": container,
        "ext": container,
        "source": {
            "path": str(source),
            "duration": duration,
            "size_bytes": source.stat().st_size if source.exists() else 0,
            "video_bytes": source_video_bytes,
            "main_video_index": 0,
        },
        "video": {
            "index": 0,
            "codec": video_codec,
            "width": target[0],
            "height": target[1],
            "target_width": target[0],
            "target_height": target[1],
            "pix_fmt": pix_fmt,
            "bit_depth": bit_depth,
            "sar": sar,
            "frame_rate": frame_rate,
            "color": color or {},
            "backend": "software",
            "encoder": "libx265",
            "quality": 24,
        },
        "audio": audio,
        "subtitles": subtitle_entries,
        "sidecars": [],
        "chapters": False,
        "attachments": False,
        "dropped": [],
    }


# ---------------------------------------------------------------------------
# happy paths


@pytest.mark.integration
def test_valid_remux_passes_every_check_without_mutating(media: dict) -> None:
    """SAF-2: valid remux opens, matches, decodes, hashes, and is untouched."""
    output = media["remux"]
    before_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    before_mtime = output.stat().st_mtime_ns
    plan = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
    )
    events: list[dict] = []

    result = verify_output(plan, output, progress_cb=events.append)

    assert isinstance(result, VideoVerifyResult)
    assert result.ok, result.failures
    assert result.failures == []
    assert result.quality_floor_breach is False
    assert result.sha256 == before_sha
    assert result.output_size == output.stat().st_size
    assert result.output_video_bytes == media["remux_video_bytes"]
    assert result.output_duration == pytest.approx(1.0, abs=0.05)
    assert result.probe["streams"]
    assert {"verifying", "hashing"} <= {event["phase"] for event in events}
    assert output.stat().st_mtime_ns == before_mtime
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before_sha


@pytest.mark.integration
def test_valid_h264_encode_passes(media: dict) -> None:
    """SAF-2/MP4-1: an encode plan is verified against the real output."""
    plan = _plan(
        media["source"],
        action="encode",
        source_video_bytes=10**9,
    )
    result = verify_output(plan, media["remux"])
    assert result.ok, result.failures
    assert result.output_video_bytes > 0


def test_duration_mismatch_fails_but_tolerance_is_one_second(media: dict) -> None:
    """SAF-2: duration tolerance is max(1s, 0.1%); beyond it fails."""
    too_long = _plan(
        media["source"],
        action="remux",
        duration=31.0,
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(too_long, media["remux"])
    assert not result.ok
    assert any("duration" in failure for failure in result.failures)

    within_tolerance = _plan(
        media["source"],
        action="remux",
        duration=1.5,
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(within_tolerance, media["remux"])
    assert result.ok, result.failures


# ---------------------------------------------------------------------------
# structural failures


def test_zero_byte_missing_and_symlink_outputs_fail(media: dict, tmp_path: Path) -> None:
    """SAF-2: empty, missing and symlink outputs are never accepted."""
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    plan = _plan(media["source"], action="remux", source_video_bytes=0)
    result = verify_output(plan, empty)
    assert not result.ok
    assert any("empty" in failure for failure in result.failures)
    assert result.sha256 == hashlib.sha256(b"").hexdigest()
    assert result.output_size == 0
    assert result.probe == {}

    missing = tmp_path / "missing.mp4"
    result = verify_output(plan, missing)
    assert not result.ok
    assert any("does not exist" in failure for failure in result.failures)

    symlink = tmp_path / "link.mp4"
    symlink.symlink_to(media["remux"])
    result = verify_output(plan, symlink)
    assert not result.ok
    assert any("symlink" in failure for failure in result.failures)


@pytest.mark.integration
def test_stream_counts_must_match_the_plan(media: dict) -> None:
    """SAF-2: planned audio/subtitle counts are exact, sidecars excluded."""
    no_audio_plan = _plan(
        media["source"],
        action="remux",
        audio_count=0,
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(no_audio_plan, media["remux"])
    assert not result.ok
    assert any("audio stream" in failure for failure in result.failures)

    subtitled_plan = _plan(
        media["source"],
        action="remux",
        audio_count=0,
        subtitles=1,
        source_video_bytes=10**9,
    )
    result = verify_output(subtitled_plan, media["subtitled"])
    assert result.ok, result.failures

    missing_subtitle_plan = _plan(
        media["source"],
        action="remux",
        audio_count=0,
        subtitles=0,
        source_video_bytes=10**9,
    )
    result = verify_output(missing_subtitle_plan, media["subtitled"])
    assert not result.ok
    assert any("subtitle stream" in failure for failure in result.failures)


def test_more_than_one_video_stream_fails(media: dict, tmp_path: Path, monkeypatch) -> None:
    """SAF-2: exactly one video stream is required."""
    fake_output = tmp_path / "fake.bin"
    fake_output.write_bytes(b"not media")
    probe = {
        "streams": [
            {"codec_type": "video", "width": 320, "height": 240},
            {"codec_type": "video", "width": 320, "height": 240},
        ],
        "format": {"duration": "1.0", "bit_rate": "1000000"},
    }
    monkeypatch.setattr(verify_mod, "_probe_json", lambda *args, **kwargs: probe)
    monkeypatch.setattr(
        verify_mod,
        "_probe_video_packets",
        lambda *args, **kwargs: [{"pts_time": "0.0", "duration_time": "1.0", "size": "1"}],
    )
    plan = _plan(media["source"], action="remux", source_video_bytes=10**9)
    result = verify_output(plan, fake_output)
    assert not result.ok
    assert any("exactly one video" in failure for failure in result.failures)


@pytest.mark.integration
def test_resolution_pix_fmt_and_sar_mismatches_fail(media: dict) -> None:
    """SAF-2: geometry, pixel format and SAR must match exactly."""
    wrong_size = _plan(
        media["source"],
        action="remux",
        target=(640, 480),
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(wrong_size, media["remux"])
    assert not result.ok
    assert any("resolution" in failure for failure in result.failures)

    wrong_pix_fmt = _plan(
        media["source"],
        action="encode",
        bit_depth=10,
        source_video_bytes=10**9,
    )
    result = verify_output(wrong_pix_fmt, media["remux"])
    assert not result.ok
    assert any("pixel format" in failure for failure in result.failures)

    wrong_sar = _plan(
        media["source"],
        action="remux",
        sar="2:1",
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(wrong_sar, media["remux"])
    assert not result.ok
    assert any("aspect ratio" in failure for failure in result.failures)


@pytest.mark.integration
def test_frame_rate_and_colour_mismatches_fail_and_unknown_is_allowed(
    media: dict,
) -> None:
    """SAF-2: fps within 0.001; unknown colour metadata is acceptable."""
    wrong_rate = _plan(
        media["source"],
        action="remux",
        duration=0.5,
        target=(160, 120),
        frame_rate="30/1",
        source_video_bytes=10**9,
    )
    result = verify_output(wrong_rate, media["low"], min_output_kbps=1, min_bpp=0)
    assert not result.ok
    assert any("frame rate" in failure for failure in result.failures)

    wrong_colour = _plan(
        media["source"],
        action="remux",
        duration=0.5,
        target=(160, 120),
        frame_rate="10/1",
        source_video_bytes=10**9,
        color={"space": "bt2020nc", "primaries": "bt2020"},
    )
    result = verify_output(wrong_colour, media["low"], min_output_kbps=1, min_bpp=0)
    assert not result.ok
    assert any("colour space" in failure for failure in result.failures)

    unknown_source = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
        color={"primaries": "bt2020", "transfer": "smpte2084"},
    )
    result = verify_output(unknown_source, media["remux"])
    assert result.ok, result.failures


@pytest.mark.integration
def test_mp4_hevc_requires_hvc1_tag(media: dict) -> None:
    """MP4-1: MP4 HEVC with hvc1 passes; hev1 fails SAF-2 verification."""
    good_plan = _plan(
        media["source"],
        action="remux",
        duration=0.2,
        target=(32, 32),
        frame_rate="10/1",
        video_codec="hevc",
        audio_count=0,
        source_video_bytes=10**9,
    )
    result = verify_output(good_plan, media["hevc_hvc1"], min_output_kbps=1)
    assert result.ok, result.failures

    result = verify_output(good_plan, media["hevc_hev1"], min_output_kbps=1)
    assert not result.ok
    assert any("hvc1" in failure for failure in result.failures)


# ---------------------------------------------------------------------------
# size and quality floors


@pytest.mark.integration
def test_bitrate_floor_applies_to_remux(media: dict) -> None:
    """SAF-2: remux is exempt from must-be-smaller, not the bitrate floor."""
    plan = _plan(
        media["source"],
        action="remux",
        duration=0.5,
        target=(160, 120),
        frame_rate="10/1",
        source_video_bytes=10**9,
    )
    result = verify_output(
        plan, media["low"], min_output_kbps=10_000, min_bpp=0
    )
    assert not result.ok
    assert any("bitrate" in failure for failure in result.failures)


@pytest.mark.integration
def test_bpp_breach_sets_flag_without_failing(media: dict) -> None:
    """SAF-2: quality floor breach is REVIEW, not FAIL."""
    plan = _plan(
        media["source"],
        action="encode",
        source_video_bytes=10**9,
    )
    result = verify_output(plan, media["remux"], min_bpp=1000)
    assert result.quality_floor_breach is True
    assert result.ok, result.failures
    assert result.failures == []


@pytest.mark.integration
def test_remux_same_size_is_accepted(media: dict) -> None:
    """SIZE-3: remux jobs are exempt from must-be-smaller."""
    plan = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(plan, media["remux"])
    assert result.ok, result.failures
    assert result.output_video_bytes == media["remux_video_bytes"]


@pytest.mark.integration
def test_encode_larger_than_source_video_fails(media: dict) -> None:
    """SIZE-3: an encode cannot be larger than the source video stream."""
    plan = _plan(
        media["source"],
        action="encode",
        source_video_bytes=1,
    )
    result = verify_output(plan, media["remux"])
    assert not result.ok
    assert any("larger than the source" in failure for failure in result.failures)


# ---------------------------------------------------------------------------
# decode, packet tail, protocol whitelist and plan errors


def test_saf2_decode_cannot_be_disabled() -> None:
    """SAF-2: no caller-visible switch can skip the full decode."""
    import inspect

    assert "verify_level" not in inspect.signature(verify_output).parameters


def test_decode_check_covers_video_and_all_audio(monkeypatch, tmp_path: Path) -> None:
    """SAF-2: a corrupt audio stream is caught by the full decode too."""
    calls: list[list[str]] = []

    class Ok:
        returncode = 0
        stderr = ""

    def fake_run(argv, timeout):
        calls.append(list(argv))
        return Ok()

    monkeypatch.setattr(verify_mod, "_run", fake_run)
    ok, message = verify_mod._decode_check(tmp_path / "out.mp4", "/bin/ffmpeg", None)
    assert ok, message
    maps = [calls[0][i + 1] for i, token in enumerate(calls[0]) if token == "-map"]
    assert maps == ["0:v:0", "0:a?"]


@pytest.mark.integration
def test_decode_failure_fails(media: dict, tmp_path: Path) -> None:
    """SAF-2: the mandatory full decode requires empty stderr."""
    fake_ffmpeg = _write_script(
        tmp_path / "fake-ffmpeg",
        "import sys\nsys.stderr.write('decode error: synthetic\\n')\nsys.exit(0)\n",
    )
    plan = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(plan, media["remux"], ffmpeg=str(fake_ffmpeg))
    assert not result.ok
    assert any("decode test wrote to stderr" in failure for failure in result.failures)


@pytest.mark.integration
def test_last_video_packet_must_match_container_duration(
    media: dict, monkeypatch
) -> None:
    """SAF-2: a truncated video packet tail is detected."""
    plan = _plan(
        media["source"],
        action="remux",
        duration=3.0,
        target=(160, 120),
        source_video_bytes=10**9,
    )
    monkeypatch.setattr(
        verify_mod,
        "_probe_video_packets",
        lambda *args, **kwargs: [
            {"pts_time": "0.0", "duration_time": "0.04", "size": "100"}
        ],
    )
    result = verify_output(plan, media["long"], min_output_kbps=1)
    assert not result.ok
    assert any("last video packet" in failure for failure in result.failures)


@pytest.mark.integration
def test_every_shellout_carries_protocol_whitelist_and_uses_argv(
    media: dict, monkeypatch
) -> None:
    """SAF-13: ffprobe/ffmpeg subprocesses always include the whitelist."""
    commands: list[list[str]] = []
    original_run = verify_mod.subprocess.run

    def capture(argv, *args, **kwargs):
        assert isinstance(argv, list)
        commands.append(list(argv))
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(verify_mod.subprocess, "run", capture)
    plan = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(plan, media["remux"])
    assert result.ok, result.failures
    assert len(commands) >= 2
    for command in commands:
        assert "-protocol_whitelist" in command
        assert command[command.index("-protocol_whitelist") + 1] == "file,crypto,data"


def test_unopenable_output_fails_cleanly(media: dict, tmp_path: Path) -> None:
    """SAF-2: a corrupt/non-media file yields failures, not an exception."""
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"this is not a media file")
    plan = _plan(media["source"], action="remux", source_video_bytes=0)
    result = verify_output(plan, corrupt)
    assert not result.ok
    assert any("ffprobe failed" in failure for failure in result.failures)
    assert result.sha256 == hashlib.sha256(corrupt.read_bytes()).hexdigest()
    assert result.output_size == corrupt.stat().st_size


def test_plan_errors_are_valueerror(media: dict) -> None:
    """SAF-2 boundary: malformed plans raise ValueError, not KeyError."""
    missing_duration = _plan(media["source"], action="remux")
    del missing_duration["source"]["duration"]
    with pytest.raises(ValueError, match="duration"):
        verify_output(missing_duration, media["remux"])

    missing_target = _plan(media["source"], action="remux")
    del missing_target["video"]["target_width"]
    with pytest.raises(ValueError, match="target_width"):
        verify_output(missing_target, media["remux"])

    missing_audio = _plan(media["source"], action="remux")
    del missing_audio["audio"]
    with pytest.raises(ValueError, match="audio"):
        verify_output(missing_audio, media["remux"])
