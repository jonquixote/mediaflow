"""Verification tests using synthetic lavfi media.

Most checks are exercised against real, tiny files made with ffmpeg; a few
edge cases (extra video streams, a truncated packet tail) monkeypatch the
ffprobe boundary.  Requirement markers: SAF-2, SIZE-3, MP4-1, MP4-9, MP4-12.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import mediaflow.verify as verify_mod
from mediaflow.ffmpeg import FFMPEG, FFPROBE
from mediaflow.supervision import OperationState
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
        "-ac",
        "2",
        "-metadata:s:a:0",
        "language=eng",
        "-disposition:a:0",
        "default",
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
        # A complete, production-shaped colour description.
        "-vf",
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
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
        "-metadata:s:s:0",
        "language=eng",
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
    audio_codec: str = "aac",
    audio_channels: int = 2,
    audio_layout: str | None = None,
    audio_language: str = "eng",
    subtitle_codec: str = "subrip",
    subtitle_action: str = "mov_text",
    subtitle_language: str = "eng",
    subtitle_forced: bool = False,
) -> dict:
    audio = [
        {
            "index": 1 + index,
            "source_codec": "aac",
            "language": audio_language,
            "channels": audio_channels,
            "action": "copy",
            "codec": audio_codec,
            "bitrate": 96,
            "default": index == 0,
            **({"channel_layout": audio_layout} if audio_layout else {}),
        }
        for index in range(audio_count)
    ]
    subtitle_entries = [
        {
            "index": 1 + audio_count + index,
            "source_codec": subtitle_codec,
            "language": subtitle_language,
            "forced": subtitle_forced,
            "action": subtitle_action,
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
            "encoder": "libx264" if video_codec == "h264" else "libx265",
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
def test_frame_rate_mismatch_fails(media: dict) -> None:
    """SAF-2: the output frame rate must match the planned one."""
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


@pytest.mark.integration
def test_known_colour_metadata_must_survive(media: dict) -> None:
    """SAF-2: a known colour property may not disappear from the output."""
    conflicting = _plan(
        media["source"],
        action="remux",
        duration=0.5,
        target=(160, 120),
        frame_rate="10/1",
        source_video_bytes=10**9,
        color={"space": "bt2020nc", "primaries": "bt2020"},
    )
    result = verify_output(conflicting, media["low"], min_output_kbps=1, min_bpp=0)
    assert not result.ok
    assert any("colour space" in failure for failure in result.failures)

    missing = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
        color={"primaries": "bt709", "transfer": "bt709"},
    )
    result = verify_output(missing, media["remux"])
    assert not result.ok
    assert any("colour primaries is missing" in failure for failure in result.failures)
    assert any("colour transfer is missing" in failure for failure in result.failures)


@pytest.mark.integration
def test_unspecified_colour_metadata_creates_no_requirement(media: dict) -> None:
    """SAF-2: unknown/unset plan colour never invents an output requirement."""
    unspecified = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
        color={"primaries": None, "transfer": "unknown", "space": "", "range": None},
    )
    result = verify_output(unspecified, media["remux"])
    assert result.ok, result.failures

    absent = _plan(
        media["source"],
        action="remux",
        source_video_bytes=media["remux_video_bytes"],
        color={},
    )
    result = verify_output(absent, media["remux"])
    assert result.ok, result.failures


@pytest.mark.integration
def test_documented_colour_aliases_compare_equal(media: dict) -> None:
    """SAF-2: "limited" is the documented alias of the "tv" range."""
    alias = _plan(
        media["source"],
        action="remux",
        duration=0.5,
        target=(160, 120),
        frame_rate="10/1",
        audio_count=0,
        source_video_bytes=10**9,
        color={"primaries": "bt709", "transfer": "bt709", "space": "bt709",
               "range": "limited"},
    )
    result = verify_output(alias, media["low"], min_output_kbps=1, min_bpp=0)
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


def test_decode_check_covers_video_and_all_audio(tmp_path: Path) -> None:
    """SAF-2: a corrupt audio stream is caught by the full decode too."""
    args_file = tmp_path / "ffmpeg-args.txt"
    fake_ffmpeg = _write_script(
        tmp_path / "fake-ffmpeg",
        "import pathlib, sys\n"
        f"pathlib.Path({str(args_file)!r}).write_text('\\n'.join(sys.argv[1:]))\n",
    )
    ok, message = verify_mod._decode_check(
        tmp_path / "out.mp4", str(fake_ffmpeg), None
    )
    assert ok, message
    args = args_file.read_text(encoding="utf-8").splitlines()
    maps = [args[i + 1] for i, token in enumerate(args) if token == "-map"]
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
def test_packet_scan_aggregates_reordered_packets_incrementally(
    tmp_path: Path,
) -> None:
    """SUP-1: packet order is not presentation order and memory is bounded."""
    fake_ffprobe = _write_script(
        tmp_path / "fake-ffprobe",
        "print('0.000000,0.500000,100')\n"
        "print('5.000000,0.500000,200')\n"
        "print('1.000000,0.500000,300')\n",
    )
    events: list[dict] = []
    outcome = verify_mod._scan_video_packets(
        tmp_path / "out.mp4",
        str(fake_ffprobe),
        10,
        progress_cb=events.append,
    )
    assert outcome.state is OperationState.SUCCEEDED
    summary = outcome.value
    assert summary.packet_count == 3
    assert summary.video_bytes == 600
    assert summary.presentation_end == pytest.approx(5.5)
    assert events[-1]["phase"] == "packet-scan"
    assert events[-1]["packet_count"] == 3


@pytest.mark.integration
def test_packet_scan_is_cancellable(tmp_path: Path) -> None:
    """SUP-1: a noisy packet scan stops promptly when cancellation wins."""
    fake_ffprobe = _write_script(
        tmp_path / "fake-ffprobe",
        "import time\n"
        "for i in range(100000):\n"
        "    print(f'{i / 100.0:.6f},0.010000,1', flush=True)\n"
        "time.sleep(30)\n",
    )
    cancel = threading.Event()
    events: list[dict] = []

    def progress(event: dict) -> None:
        events.append(event)
        cancel.set()

    outcome = verify_mod._scan_video_packets(
        tmp_path / "out.mp4",
        str(fake_ffprobe),
        30,
        cancel_event=cancel,
        progress_cb=progress,
    )
    assert outcome.state is OperationState.CANCELLED
    assert events
    assert outcome.value is None


def test_hash_is_cancellable_and_never_returns_a_partial_digest(
    tmp_path: Path,
) -> None:
    """SUP-2: cancellation suppresses a late hash result."""
    output = tmp_path / "out.bin"
    output.write_bytes(b"x" * (3 * 1024 * 1024))
    cancel = threading.Event()

    def progress(event: dict) -> None:
        if event.get("bytes", 0) >= verify_mod.HASH_CHUNK_SIZE:
            cancel.set()

    with pytest.raises(verify_mod.VerificationOperationError) as raised:
        verify_mod._sha256_file(
            output,
            progress,
            cancel_event=cancel,
        )
    assert raised.value.state is OperationState.CANCELLED
    assert raised.value.phase == "hashing"


@pytest.mark.integration
def test_decode_timeout_is_typed_and_not_successful(tmp_path: Path) -> None:
    """SUP-3: an overall decode timeout is distinct from a content failure."""
    fake_ffmpeg = _write_script(
        tmp_path / "fake-ffmpeg",
        "import time\ntime.sleep(30)\n",
    )
    with pytest.raises(verify_mod.VerificationOperationError) as raised:
        verify_mod._decode_check(tmp_path / "out.mp4", str(fake_ffmpeg), 0.2)
    assert raised.value.state is OperationState.TIMED_OUT
    assert raised.value.timeout_type == "overall"
    assert raised.value.phase == "decode"


@pytest.mark.integration
def test_decode_protection_interruption_is_not_success(tmp_path: Path) -> None:
    """SUP-3: protection interruption has a typed non-success outcome."""
    fake_ffmpeg = _write_script(
        tmp_path / "fake-ffmpeg",
        "import time\ntime.sleep(30)\n",
    )

    def monitor():
        return verify_mod.ProtectionDecision(
            verify_mod.ProtectionAction.TERMINATE,
            ("thermal",),
            detail="too hot",
        )

    with pytest.raises(verify_mod.VerificationOperationError) as raised:
        verify_mod._decode_check(
            tmp_path / "out.mp4",
            str(fake_ffmpeg),
            30,
            monitor=monitor,
        )
    assert raised.value.state is OperationState.INTERRUPTED
    assert raised.value.reasons == ("thermal",)


@pytest.mark.integration
def test_frame_scan_count_is_distinct_from_packet_count(tmp_path: Path) -> None:
    """SUP-1: decoded frames and container packets are separate measures."""
    fake_ffprobe = _write_script(
        tmp_path / "fake-ffprobe",
        "print('0.000000,0.040000')\n"
        "print('0.040000,0.040000')\n"
        "print('0.080000,0.040000')\n",
    )
    outcome = verify_mod._scan_decoded_frames(
        tmp_path / "out.mp4", str(fake_ffprobe), 10
    )
    assert outcome.state is OperationState.SUCCEEDED
    summary = outcome.value
    assert summary.frame_count == 3
    assert summary.presentation_end == pytest.approx(0.12)
    assert not hasattr(summary, "packet_count")


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
        "_scan_video_packets",
        lambda *args, **kwargs: verify_mod._OperationOutcome(
            state=OperationState.SUCCEEDED,
            value=verify_mod.PacketScanSummary(
                packet_count=1,
                video_bytes=100,
                presentation_end=0.04,
            ),
            phase="packet-scan",
        ),
    )
    result = verify_output(plan, media["long"], min_output_kbps=1)
    assert not result.ok
    assert any("video presentation tail" in failure for failure in result.failures)


@pytest.mark.integration
def test_every_shellout_carries_protocol_whitelist_and_uses_argv(
    media: dict, monkeypatch
) -> None:
    """SAF-13: ffprobe/ffmpeg subprocesses always include the whitelist."""
    commands: list[list[str]] = []
    original_run = verify_mod.run_process

    def capture(argv, *args, **kwargs):
        assert isinstance(argv, list)
        commands.append(list(argv))
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(verify_mod, "run_process", capture)
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


# ---------------------------------------------------------------------------
# planned stream contract (A4)
# ---------------------------------------------------------------------------


def _make_output(
    path: Path,
    *,
    video_codec: str = "libx264",
    audio_codec: str | None = "aac",
    channels: int = 2,
    language: str = "eng",
    default: bool = True,
    size: str = "320x240",
    rate: int = 25,
    duration: float = 1.0,
) -> Path:
    """Build a tiny real output file with the requested track shape."""
    args = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate={rate}:duration={duration}",
    ]
    if audio_codec:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
        ]
    args += ["-map", "0:v:0"]
    if audio_codec:
        args += ["-map", "1:a:0"]
    args += [
        "-c:v",
        video_codec,
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]
    if video_codec == "libx265":
        args += ["-x265-params", "log-level=error"]
    if audio_codec:
        args += [
            "-c:a",
            audio_codec,
            "-ac",
            str(channels),
            "-b:a",
            "96k",
            "-metadata:s:a:0",
            f"language={language}",
            "-disposition:a:0",
            "default" if default else "0",
        ]
    args += ["-shortest"]
    _run_ffmpeg(*args, str(path))
    return path


@pytest.mark.integration
def test_planned_hevc_rejects_an_h264_output(media: dict) -> None:
    """SAF-2: the planned video codec is part of the output contract."""
    plan = _plan(media["source"], action="encode", video_codec="hevc")
    result = verify_output(plan, media["remux"])
    assert not result.ok
    assert any("video codec" in failure for failure in result.failures)


@pytest.mark.integration
def test_remux_plan_is_checked_against_the_source_codec(media: dict) -> None:
    """SAF-2: a remux keeps the source codec and is verified against it."""
    plan = _plan(
        media["source"],
        action="remux",
        video_codec="h264",
        source_video_bytes=media["remux_video_bytes"],
    )
    result = verify_output(plan, media["remux"])
    assert result.ok, result.failures


@pytest.mark.integration
def test_mp4_hevc_requires_the_hvc1_sample_entry(media: dict) -> None:
    """MP4-1: every HEVC stream in an MP4 output carries the hvc1 tag."""
    plan = _plan(
        media["source"],
        action="encode",
        video_codec="hevc",
        target=(32, 32),
        frame_rate="10/1",
        duration=0.2,
        audio_count=0,
    )
    good = verify_output(plan, media["hevc_hvc1"], min_output_kbps=1)
    assert good.ok, good.failures

    bad = verify_output(plan, media["hevc_hev1"], min_output_kbps=1)
    assert not bad.ok
    assert any("hvc1" in failure for failure in bad.failures)


@pytest.mark.integration
def test_audio_codec_mismatch_fails(media: dict, tmp_path: Path) -> None:
    """SAF-2: every planned audio track is checked codec by codec."""
    output = _make_output(tmp_path / "ac3.mp4", audio_codec="ac3")
    plan = _plan(media["source"], action="encode", audio_codec="aac")
    result = verify_output(plan, output)
    assert not result.ok
    assert any("audio track 0: codec" in failure for failure in result.failures)


@pytest.mark.integration
def test_audio_channel_count_mismatch_fails(media: dict, tmp_path: Path) -> None:
    """SAF-2: the planned audio channel count is enforced."""
    output = _make_output(tmp_path / "mono.mp4", channels=1)
    plan = _plan(media["source"], action="encode", audio_channels=2)
    result = verify_output(plan, output)
    assert not result.ok
    assert any("channel count" in failure for failure in result.failures)


@pytest.mark.integration
def test_audio_channel_layout_mismatch_fails(media: dict, tmp_path: Path) -> None:
    """SAF-2: the planned audio channel layout is enforced."""
    output = _make_output(tmp_path / "surround.mp4", channels=6)
    plan = _plan(
        media["source"],
        action="encode",
        audio_channels=6,
        audio_layout="stereo",
    )
    result = verify_output(plan, output)
    assert not result.ok
    assert any("channel layout" in failure for failure in result.failures)


@pytest.mark.integration
def test_audio_language_mismatch_fails(media: dict, tmp_path: Path) -> None:
    """SAF-2: the planned audio language is enforced."""
    output = _make_output(tmp_path / "german.mp4", language="deu")
    plan = _plan(media["source"], action="encode", audio_language="eng")
    result = verify_output(plan, output)
    assert not result.ok
    assert any("audio track 0: language" in failure for failure in result.failures)


@pytest.mark.integration
def test_audio_default_disposition_mismatch_fails(
    media: dict, tmp_path: Path
) -> None:
    """SAF-2: the planned default disposition is enforced.

    The MP4 muxer always marks the first track of a type as default, so
    the mismatch is produced in a container that keeps the disposition
    exactly as written (MKV).
    """
    output = tmp_path / "nodefault.mkv"
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
        "-ac",
        "2",
        "-b:a",
        "96k",
        "-metadata:s:a:0",
        "language=eng",
        "-disposition:a:0",
        "0",
        "-shortest",
        str(output),
    )
    plan = _plan(media["source"], action="encode")
    result = verify_output(plan, output)
    assert not result.ok
    assert any("default disposition" in failure for failure in result.failures)


@pytest.mark.integration
def test_a_downmix_is_checked_by_channel_count_only(
    media: dict, tmp_path: Path
) -> None:
    """SAF-2: a downmix states the channel count, not a speaker layout."""
    output = _make_output(tmp_path / "surround.mp4", channels=6)
    plan = _plan(media["source"], action="encode", audio_channels=6)
    plan["audio"][0]["channels"] = 12
    plan["audio"][0]["target_channels"] = 6
    plan["audio"][0]["downmix"] = True
    plan["audio"][0].pop("channel_layout", None)

    result = verify_output(plan, output)
    assert result.ok, result.failures


@pytest.mark.integration
def test_mov_text_subtitle_passes_and_a_copied_text_subtitle_fails(
    media: dict, tmp_path: Path
) -> None:
    """SAF-2/MP4-5: the planned subtitle codec is enforced."""
    plan = _plan(
        media["source"],
        action="encode",
        audio_count=0,
        subtitles=1,
        subtitle_action="mov_text",
        subtitle_codec="subrip",
    )
    good = verify_output(plan, media["subtitled"])
    assert good.ok, good.failures

    copied = tmp_path / "copied.mkv"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=1",
        "-i",
        str(media["dir"] / "subs.srt"),
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
        "srt",
        "-metadata:s:s:0",
        "language=eng",
        "-t",
        "1",
        str(copied),
    )
    bad = verify_output(plan, copied)
    assert not bad.ok
    assert any("subtitle track 0: codec" in failure for failure in bad.failures)


# ---------------------------------------------------------------------------
# bits per pixel per frame (A3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_bpp_is_bits_per_pixel_per_frame(media: dict) -> None:
    """SAF-2: the quality metric is bits per pixel per frame, not per second."""
    plan = _plan(media["source"], action="encode", source_video_bytes=10**9)
    result = verify_output(plan, media["remux"], min_bpp=0.0)
    assert result.ok, result.failures
    assert result.bpp is not None
    assert result.bpp_frame_count == 25
    assert result.bpp_frame_basis == "container"
    expected = result.output_video_bytes * 8.0 / (25 * 320 * 240)
    assert result.bpp == pytest.approx(expected)
    # The old (wrong) formula divided by the duration instead of the frame
    # count; for this 25 fps clip that is a factor of 25.
    assert result.bpp != pytest.approx(
        result.output_video_bytes * 8.0 / (1.0 * 320 * 240)
    )


@pytest.mark.integration
def test_bpp_floor_breach_is_review_not_failure(media: dict) -> None:
    """SAF-2: a quality-floor breach routes to REVIEW, it does not fail."""
    plan = _plan(media["source"], action="encode", source_video_bytes=10**9)
    result = verify_output(plan, media["remux"], min_bpp=10.0)
    assert result.ok, result.failures
    assert result.quality_floor_breach is True
    assert result.bpp is not None


@pytest.mark.integration
def test_bpp_is_not_evaluated_when_the_frame_count_is_unknown(
    media: dict, monkeypatch
) -> None:
    """SAF-2: an unavailable frame count is surfaced, never silently skipped."""
    original = verify_mod._probe_json

    def without_frame_count(path, ffprobe, timeout, **kwargs):
        data = original(path, ffprobe, timeout, **kwargs)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                stream.pop("nb_frames", None)
                stream["avg_frame_rate"] = "0/0"
                stream["r_frame_rate"] = "0/0"
        return data

    monkeypatch.setattr(verify_mod, "_probe_json", without_frame_count)
    monkeypatch.setattr(verify_mod, "_count_decoded_frames", lambda *a, **k: None)

    plan = _plan(media["source"], action="encode", source_video_bytes=10**9)
    result = verify_output(plan, media["remux"], min_bpp=0.01)
    assert result.bpp is None
    assert result.bpp_note and "frame count" in result.bpp_note
    assert any("frame count" in warning for warning in result.warnings)


def test_presented_frame_count_policy(media: dict, monkeypatch) -> None:
    """SAF-2: the frame-count basis is container, decoded or estimated."""
    path = media["remux"]
    frames, basis, note = verify_mod._presented_frame_count(
        path,
        FFPROBE,
        60,
        {"nb_frames": "25"},
        1.0,
    )
    assert (frames, basis, note) == (25, "container", None)

    frames, basis, note = verify_mod._presented_frame_count(
        path,
        FFPROBE,
        60,
        {"nb_frames": "0", "avg_frame_rate": "25/1"},
        2.0,
    )
    assert frames == 25
    assert basis == "decoded"
    assert note is None

    # Without a container count and without a decodable frame count the
    # metric falls back to duration x frame rate and says so.
    monkeypatch.setattr(verify_mod, "_count_decoded_frames", lambda *a, **k: None)
    frames, basis, note = verify_mod._presented_frame_count(
        path,
        FFPROBE,
        60,
        {"nb_frames": None, "avg_frame_rate": "24000/1001", "r_frame_rate": "25/1"},
        1.0,
    )
    assert frames == 24
    assert basis == "estimated"
    assert note and "variable-frame-rate" in note

    frames, basis, note = verify_mod._presented_frame_count(
        path,
        FFPROBE,
        60,
        {"nb_frames": None, "avg_frame_rate": "0/0", "r_frame_rate": "0/0"},
        0.0,
    )
    assert frames is None
    assert basis is None
    assert note and "unknown" in note

