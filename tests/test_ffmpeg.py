"""Tests for the explicit ffmpeg argv boundary and encoder interface.

Requirement markers: MP4-1, MP4-9, MP4-12, SAF-13, ENC-1.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mediaflow.ffmpeg import (
    FFMPEG,
    FFPROBE,
    FakeEncoder,
    FfmpegEncoder,
    PlanError,
    build_command,
    output_partial_path,
    probe_encoders,
)


# ---------------------------------------------------------------------------
# helpers


def _base_plan(tmp_path: Path) -> dict:
    """A complete valid plan matching docs/PLAN_SCHEMA.md."""
    return {
        "schema": 1,
        "preset": "1080p-balanced-mp4",
        "action": "encode",
        "container": "mp4",
        "plan_version": 1,
        "fallback_from": None,
        "source": {
            "path": str(tmp_path / "movie.mkv"),
            "format": "matroska,webm",
            "duration": 123.45,
            "size_bytes": 1_000_000,
            "video_bytes": 900_000,
            "main_video_index": 0,
            "mtime": 123.0,
        },
        "video": {
            "index": 0,
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "target_width": 1280,
            "target_height": 720,
            "pix_fmt": "yuv420p",
            "bit_depth": 8,
            "sar": "1:1",
            "frame_rate": "24000/1001",
            "field_order": "progressive",
            "hdr": False,
            "dolby_vision": False,
            "color": {
                "primaries": "bt709",
                "transfer": "bt709",
                "space": "bt709",
                "range": "tv",
            },
            "backend": "qsv",
            "encoder": "hevc_qsv",
            "quality": 24,
            "look_ahead": True,
            "tag": "hvc1",
            "scaled": True,
        },
        "audio": [
            {
                "index": 1,
                "source_codec": "dts",
                "language": "eng",
                "channels": 6,
                "action": "transcode",
                "codec": "eac3",
                "bitrate": 448,
                "default": True,
            },
            {
                "index": 2,
                "source_codec": "aac",
                "language": "fra",
                "channels": 2,
                "action": "copy",
                "codec": "aac",
                "bitrate": 96,
                "default": False,
            },
        ],
        "subtitles": [
            {
                "index": 3,
                "source_codec": "subrip",
                "language": "eng",
                "title": "English",
                "forced": True,
                "action": "mov_text",
                "sidecar": None,
            }
        ],
        "sidecars": [],
        "chapters": True,
        "attachments": False,
        "name": {
            "title": "Title",
            "year": 2001,
            "tmdbid": None,
            "stem": "Title (2001)",
        },
        "ext": "mp4",
        "destination_rel": "Title (2001)/Title (2001).mp4",
        "dry_run": False,
        "warnings": [],
        "dropped": [],
        "estimated_seconds": 120.0,
        "speed_source": "table",
        "review": None,
    }


def _value_after(command: list[str], option: str) -> str:
    index = command.index(option)
    return command[index + 1]


def _map_values(command: list[str]) -> list[str]:
    return [
        command[index + 1]
        for index, arg in enumerate(command)
        if arg == "-map"
    ]


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
def source_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("ffmpeg-source")
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
    return source


def _remux_plan(source: Path, output_hint: Path) -> dict:
    plan = _base_plan(output_hint.parent)
    plan["source"]["path"] = str(source)
    plan["source"]["duration"] = 1.0
    plan["source"]["video_bytes"] = 120_000
    plan["action"] = "remux"
    plan["container"] = "mp4"
    plan["ext"] = "mp4"
    plan["video"].update(
        {
            "codec": "h264",
            "width": 320,
            "height": 240,
            "target_width": 320,
            "target_height": 240,
            "scaled": False,
        }
    )
    plan["audio"] = [
        {
            "index": 1,
            "source_codec": "aac",
            "language": "eng",
            "channels": 1,
            "action": "copy",
            "codec": "aac",
            "bitrate": 96,
            "default": True,
        }
    ]
    plan["subtitles"] = []
    plan["chapters"] = False
    plan["attachments"] = False
    return plan


# ---------------------------------------------------------------------------
# argv construction


def test_qsv_encode_builds_explicit_maps_and_mp4_hevc_tag(tmp_path: Path) -> None:
    """MP4-1/MP4-5/MP4-9/MP4-12/SAF-13: maps, chapters, subtitles, HEVC flags."""
    plan = _base_plan(tmp_path)
    output = output_partial_path(tmp_path / "Title (2001).mp4")

    command = build_command(plan, output)

    assert command[0] == FFMPEG
    assert _value_after(command, "-protocol_whitelist") == "file,crypto,data"
    assert _value_after(command, "-i") == plan["source"]["path"]
    assert _value_after(command, "-map_chapters") == "0"
    assert _value_after(command, "-map_metadata") == "0"
    assert _map_values(command) == ["0:0", "0:1", "0:2", "0:3"]
    assert "0" not in _map_values(command)  # never -map 0

    # QSV is an input option, before -i.
    assert command.index("-hwaccel") < command.index("-i")
    assert _value_after(command, "-hwaccel") == "qsv"
    assert _value_after(command, "-c:v") == "hevc_qsv"
    assert _value_after(command, "-global_quality") == "24"
    assert _value_after(command, "-look_ahead") == "1"
    assert _value_after(command, "-pix_fmt") == "nv12"
    assert _value_after(command, "-vf") == "vpp_qsv=w=1280:h=720"
    assert "-1" not in _value_after(command, "-vf")

    # Audio is configured per output stream ordinal.
    assert _value_after(command, "-c:a:0") == "eac3"
    assert _value_after(command, "-b:a:0") == "448k"
    assert _value_after(command, "-c:a:1") == "copy"
    assert "-b:a:1" not in command
    assert command[command.index("-metadata:s:a:0") + 1] == "language=eng"
    assert _value_after(command, "-disposition:a:0") == "default"
    assert _value_after(command, "-disposition:a:1") == "0"

    # Text subtitles become mov_text, with language/title/forced metadata.
    assert _value_after(command, "-c:s:0") == "mov_text"
    assert command[command.index("-metadata:s:s:0") + 1] == "language=eng"
    assert command[command.index("-metadata:s:s:0") + 3] == "title=English"
    assert _value_after(command, "-disposition:s:0") == "forced"

    assert _value_after(command, "-tag:v") == "hvc1"
    assert _value_after(command, "-movflags") == "+faststart"
    assert _value_after(command, "-f") == "mp4"
    assert command[-1] == str(output)
    assert output.name.endswith(".partial")
    assert not any("0:t" == value for value in _map_values(command))


def test_mp4_without_chapters_does_not_preserve_them(tmp_path: Path) -> None:
    """MP4-9: -map_chapters -1 when the plan has chapters false."""
    plan = _base_plan(tmp_path)
    plan["chapters"] = False
    command = build_command(plan, tmp_path / "out.mp4")
    assert _value_after(command, "-map_chapters") == "-1"


def test_mkv_keeps_attachments_and_never_gets_hevc_mp4_flags(tmp_path: Path) -> None:
    """MP4-9/MP4-12/MKV-1: MKV attachments, copied streams, no hvc1/faststart."""
    plan = _base_plan(tmp_path)
    plan["container"] = "mkv"
    plan["ext"] = "mkv"
    plan["action"] = "remux"
    plan["attachments"] = True
    plan["chapters"] = True
    plan["video"]["codec"] = "hevc"

    command = build_command(plan, tmp_path / "out.mkv")

    assert _value_after(command, "-c:v") == "copy"
    assert "0:t?" in _map_values(command)
    assert _value_after(command, "-c:t") == "copy"
    assert _value_after(command, "-c:a:0") == "copy"
    assert _value_after(command, "-c:a:1") == "copy"
    assert _value_after(command, "-c:s:0") == "copy"
    assert _value_after(command, "-map_chapters") == "0"
    assert _value_after(command, "-f") == "matroska"
    assert "hvc1" not in command
    assert "+faststart" not in command
    assert "-tag:v" not in command


def test_remux_mp4_only_tags_hevc(tmp_path: Path) -> None:
    """MP4-1: remuxed HEVC gets hvc1/faststart; H.264 does not."""
    hevc_plan = _base_plan(tmp_path)
    hevc_plan["action"] = "remux"
    hevc_plan["video"]["codec"] = "hevc"
    hevc_command = build_command(hevc_plan, tmp_path / "hevc.mp4")
    assert _value_after(hevc_command, "-c:v") == "copy"
    assert _value_after(hevc_command, "-tag:v") == "hvc1"
    assert _value_after(hevc_command, "-movflags") == "+faststart"

    h264_plan = _base_plan(tmp_path)
    h264_plan["action"] = "remux"
    h264_plan["video"]["codec"] = "h264"
    h264_command = build_command(h264_plan, tmp_path / "h264.mp4")
    assert _value_after(h264_command, "-c:v") == "copy"
    assert "-tag:v" not in h264_command
    assert "-movflags" not in h264_command


def test_software_fallback_overrides_qsv_and_sets_pix_fmt(tmp_path: Path) -> None:
    """MP4-1/MP4-9: software fallback uses libx265, software scale, no hw."""
    plan = _base_plan(tmp_path)

    command = build_command(plan, tmp_path / "out.mp4", software=True)

    assert "-hwaccel" not in command
    assert "-vaapi_device" not in command
    assert _value_after(command, "-c:v") == "libx265"
    assert _value_after(command, "-crf") == "24"
    assert _value_after(command, "-preset") == "slow"
    assert _value_after(command, "-pix_fmt") == "yuv420p"
    vf = _value_after(command, "-vf")
    assert "scale=1280:720:flags=lanczos" in vf
    # The planned colour is stamped on the frames so the encoder keeps it.
    assert vf.startswith("setparams=color_primaries=bt709")
    assert all("qsv" not in arg and "vaapi" not in arg for arg in command)
    assert _value_after(command, "-tag:v") == "hvc1"

    ten_bit = _base_plan(tmp_path)
    ten_bit["video"]["bit_depth"] = 10
    ten_bit_command = build_command(ten_bit, tmp_path / "out10.mp4", software=True)
    assert _value_after(ten_bit_command, "-pix_fmt") == "p010le"


def test_software_backend_plan_needs_no_override(tmp_path: Path) -> None:
    """MP4-1: a plan with backend=software uses the software encoder."""
    plan = _base_plan(tmp_path)
    plan["video"]["backend"] = "software"
    plan["video"]["encoder"] = "libx265"
    command = build_command(plan, tmp_path / "out.mp4")
    assert _value_after(command, "-c:v") == "libx265"
    assert "-hwaccel" not in command


def test_vaapi_uploads_nv12_after_explicit_scale(tmp_path: Path) -> None:
    """MP4-1: VAAPI device, qp and a scale-before-upload filter chain."""
    plan = _base_plan(tmp_path)
    plan["video"]["backend"] = "vaapi"
    plan["video"]["encoder"] = "hevc_vaapi"

    command = build_command(plan, tmp_path / "out.mp4")

    assert command.index("-vaapi_device") < command.index("-i")
    assert _value_after(command, "-vaapi_device") == "/dev/dri/renderD128"
    assert _value_after(command, "-c:v") == "hevc_vaapi"
    assert _value_after(command, "-qp") == "24"
    assert "-global_quality" not in command
    vf = _value_after(command, "-vf")
    assert "scale=1280:720" in vf
    assert "format=nv12" in vf
    assert "hwupload" in vf
    assert vf.index("scale=") < vf.index("format=nv12") < vf.index("hwupload")
    assert _value_after(command, "-tag:v") == "hvc1"


def test_hardware_10bit_encodes_request_main10_and_10bit_upload(tmp_path: Path) -> None:
    """MP4-1/SIZE-3: 10-bit hardware output is explicit, not inferred."""
    qsv = _base_plan(tmp_path)
    qsv["video"]["bit_depth"] = 10
    qsv_command = build_command(qsv, tmp_path / "qsv10.mp4")
    assert _value_after(qsv_command, "-profile:v") == "main10"
    assert _value_after(qsv_command, "-pix_fmt") == "p010le"

    vaapi = _base_plan(tmp_path)
    vaapi["video"]["backend"] = "vaapi"
    vaapi["video"]["encoder"] = "hevc_vaapi"
    vaapi["video"]["bit_depth"] = 10
    vaapi_command = build_command(vaapi, tmp_path / "vaapi10.mp4")
    assert _value_after(vaapi_command, "-profile:v") == "main10"
    assert "format=p010le" in _value_after(vaapi_command, "-vf")


def test_audio_downmix_only_when_flagged(tmp_path: Path) -> None:
    """MP4-3 boundary: downmix flag adds -ac:a:N 8; 3-6ch is untouched."""
    plan = _base_plan(tmp_path)
    plan["audio"][0]["channels"] = 10
    plan["audio"][0]["downmix"] = True
    plan["audio"][0]["bitrate"] = 640
    plan["audio"][1]["bitrate"] = None  # planner uses None for copied tracks
    command = build_command(plan, tmp_path / "out.mp4")
    assert _value_after(command, "-ac:a:0") == "8"
    assert _value_after(command, "-b:a:0") == "640k"
    assert _value_after(command, "-c:a:1") == "copy"
    assert "-b:a:1" not in command

    six_channel = _base_plan(tmp_path)
    command = build_command(six_channel, tmp_path / "out.mp4")
    assert "-ac:a:0" not in command


def test_dropped_subtitles_are_not_mapped_and_bitmap_mp4_is_refused(
    tmp_path: Path,
) -> None:
    """MP4-6/MP4-12: map only selected subtitles; refuse bitmap in MP4."""
    plan = _base_plan(tmp_path)
    plan["dropped"] = [
        {"index": 9, "source_codec": "hdmv_pgs_subtitle", "reason": "bitmap"}
    ]
    command = build_command(plan, tmp_path / "out.mp4")
    assert "0:9" not in _map_values(command)

    bitmap = _base_plan(tmp_path)
    bitmap["subtitles"][0]["source_codec"] = "hdmv_pgs_subtitle"
    with pytest.raises(ValueError, match="[Bb]itmap"):
        build_command(bitmap, tmp_path / "out.mp4")


def test_build_command_rejects_missing_and_unknown_plan_fields(
    tmp_path: Path,
) -> None:
    """MP4-12/SIZE-5: a bad plan raises ValueError, never KeyError; even dims."""
    missing = _base_plan(tmp_path)
    del missing["video"]["target_width"]
    with pytest.raises(ValueError, match="target_width"):
        build_command(missing, tmp_path / "out.mp4")

    no_container = _base_plan(tmp_path)
    no_container.pop("container")
    no_container.pop("ext")
    with pytest.raises(ValueError, match="container"):
        build_command(no_container, tmp_path / "out.mp4")

    bad_action = _base_plan(tmp_path)
    bad_action["action"] = "transcode"
    with pytest.raises(ValueError, match="action"):
        build_command(bad_action, tmp_path / "out.mp4")

    bad_backend = _base_plan(tmp_path)
    bad_backend["video"]["backend"] = "amf"
    with pytest.raises(ValueError, match="backend"):
        build_command(bad_backend, tmp_path / "out.mp4")

    odd = _base_plan(tmp_path)
    odd["video"]["target_width"] = 1279
    with pytest.raises(ValueError, match="even"):
        build_command(odd, tmp_path / "out.mp4")

    no_codec = _base_plan(tmp_path)
    no_codec["audio"][0].pop("codec")
    with pytest.raises(ValueError, match="codec"):
        build_command(no_codec, tmp_path / "out.mp4")

    no_audio = _base_plan(tmp_path)
    no_audio.pop("audio")
    with pytest.raises(ValueError, match="audio"):
        build_command(no_audio, tmp_path / "out.mp4")

    no_subtitles = _base_plan(tmp_path)
    no_subtitles.pop("subtitles")
    with pytest.raises(ValueError, match="subtitles"):
        build_command(no_subtitles, tmp_path / "out.mp4")

    incompatible = _base_plan(tmp_path)
    incompatible["action"] = "remux"
    incompatible["video"]["codec"] = "vp9"
    with pytest.raises(ValueError, match="H.264 or HEVC"):
        build_command(incompatible, tmp_path / "out.mp4")

    with pytest.raises(ValueError, match="mapping"):
        build_command(["not", "a", "plan"], tmp_path / "out.mp4")  # type: ignore[arg-type]


def test_command_is_argv_only_and_has_no_tokens(tmp_path: Path) -> None:
    """SAF-13/MP4-12: argv is strings, no shell, and no secret-like fields."""
    plan = _base_plan(tmp_path)
    plan["name"]["title"] = "Bearer secret token"
    plan["tmdb_token"] = "super-secret-token"  # ignored, must not reach argv
    command = build_command(plan, tmp_path / "out.mp4", ffmpeg="/opt/fake/ffmpeg")
    assert command[0] == "/opt/fake/ffmpeg"
    assert all(isinstance(arg, str) for arg in command)
    lowered = " ".join(command).lower()
    assert "bearer" not in lowered
    assert "super-secret" not in lowered
    assert "token" not in lowered


def test_output_partial_path_appends_suffix() -> None:
    """ENC-1: the working name is always final + .partial."""
    assert str(output_partial_path("/pipeline/work/movie.mp4")) == (
        "/pipeline/work/movie.mp4.partial"
    )
    assert str(output_partial_path(Path("/tmp/x.mkv"))) == "/tmp/x.mkv.partial"


# ---------------------------------------------------------------------------
# FfmpegEncoder


@pytest.mark.integration
def test_encoder_remuxes_atomically_with_progress(
    tmp_path: Path, source_mp4: Path
) -> None:
    """ENC-1: argv-only run, progress callback, fsync+rename, no partial left."""
    output = tmp_path / "out.mp4"
    plan = _remux_plan(source_mp4, output)
    # The engine passes the working path inside the plan, not the constructor.
    plan["working_output"] = str(output)
    events: list[dict] = []

    result = FfmpegEncoder(FFMPEG, timeout=30).encode(
        plan, progress_cb=events.append
    )

    assert result.ok, result.stderr
    assert result.returncode == 0
    assert result.output == output
    assert result.output_size > 0
    assert output.is_file() and output.stat().st_size > 0
    assert not output_partial_path(output).exists()
    assert "-progress" in result.command
    assert _value_after(result.command, "-progress") == "pipe:1"
    assert "-nostats" in result.command
    assert "-y" in result.command
    assert result.command[-1] == str(output_partial_path(output))
    assert events and events[0]["phase"] == "encoding"
    assert any(event.get("percent") == 100.0 for event in events)

    probe = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,crypto,data",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "duration=" in probe.stdout


def test_encoder_refuses_resume(tmp_path: Path) -> None:
    """ENC-1: v1 always passes resume_from=None."""
    encoder = FfmpegEncoder(FFMPEG, tmp_path / "out.mp4")
    with pytest.raises(NotImplementedError, match="resume"):
        encoder.encode(_base_plan(tmp_path), resume_from=tmp_path / "old.partial")


def test_encoder_does_not_publish_empty_success(tmp_path: Path) -> None:
    """ENC-1: exit 0 with no output never renames an empty/absent file."""
    fake = _write_script(tmp_path / "fake-ffmpeg", "import sys\nsys.exit(0)\n")
    output = tmp_path / "out.mp4"
    result = FfmpegEncoder(str(fake), output).encode(_base_plan(tmp_path))
    assert not result.ok
    assert result.returncode != 0
    assert "partial output" in result.stderr
    assert not output.exists()


def test_encoder_returns_nonzero_details_and_keeps_final_absent(
    tmp_path: Path,
) -> None:
    """ENC-1: non-zero ffmpeg return is reported, final path untouched."""
    fake = _write_script(
        tmp_path / "fake-ffmpeg",
        "import sys\n"
        "open(sys.argv[-1], 'wb').write(b'junk')\n"
        "sys.stderr.write('boom: synthetic failure\\n')\n"
        "sys.exit(7)\n",
    )
    output = tmp_path / "out.mp4"
    result = FfmpegEncoder(str(fake), output).encode(_base_plan(tmp_path))
    assert result.returncode == 7
    assert "boom" in result.stderr
    assert not output.exists()
    assert output_partial_path(output).exists()
    assert result.partial == output_partial_path(output)


def test_encoder_stderr_is_bounded(tmp_path: Path) -> None:
    """ENC-1: stderr capture is tail-bounded instead of unbounded."""
    fake = _write_script(
        tmp_path / "fake-ffmpeg",
        "import sys\n"
        "sys.stderr.write('x' * 200000)\n"
        "sys.exit(2)\n",
    )
    result = FfmpegEncoder(
        str(fake), tmp_path / "out.mp4", stderr_limit=4096
    ).encode(_base_plan(tmp_path))
    assert result.returncode == 2
    assert 0 < len(result.stderr) <= 4096


def test_encoder_cancellation_sends_signal_and_returns_details(
    tmp_path: Path,
) -> None:
    """ENC-1/SAF-12: cancellation terminates and never publishes."""
    fake = _write_script(
        tmp_path / "fake-ffmpeg",
        "import sys, time\n"
        "print('out_time_us=1000000', flush=True)\n"
        "print('speed=1.0x', flush=True)\n"
        "print('progress=continue', flush=True)\n"
        "time.sleep(30)\n",
    )
    cancel = threading.Event()
    output = tmp_path / "out.mp4"

    def on_progress(event: dict) -> None:
        if event.get("progress") == "continue":
            cancel.set()

    started = time.monotonic()
    result = FfmpegEncoder(str(fake), output).encode(
        _base_plan(tmp_path), progress_cb=on_progress, cancel_event=cancel
    )
    assert time.monotonic() - started < 10
    assert result.cancelled
    assert not result.ok
    assert "cancelled" in result.stderr
    assert not output.exists()


def test_encoder_timeout_kills_and_returns_details(tmp_path: Path) -> None:
    """ENC-1: an optional wall-clock timeout sends SIGTERM then SIGKILL."""
    fake = _write_script(
        tmp_path / "fake-ffmpeg",
        "import time\nprint('out_time_us=1', flush=True)\ntime.sleep(30)\n",
    )
    output = tmp_path / "out.mp4"
    started = time.monotonic()
    result = FfmpegEncoder(str(fake), output, timeout=0.5).encode(
        _base_plan(tmp_path)
    )
    assert time.monotonic() - started < 10
    assert result.timed_out
    assert not result.ok
    assert "timed out" in result.stderr
    assert not output.exists()


def test_fake_encoder_copies_source_bytes(tmp_path: Path) -> None:
    """ENC-1: FakeEncoder is a byte-copy test double, not a media encoder."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"not really a movie")
    output = tmp_path / "out.mp4"
    plan = _base_plan(tmp_path)
    plan["source"]["path"] = str(source)
    result = FakeEncoder(output).encode(plan)
    assert result.ok
    assert output.read_bytes() == b"not really a movie"
    assert not output_partial_path(output).exists()


def test_probe_encoders_parses_capability_output(tmp_path: Path) -> None:
    """ENC-1: capability helper is argv-only and parses encoder names."""
    fake = _write_script(
        tmp_path / "fake-ffmpeg",
        "import sys\n"
        "if '-version' in sys.argv:\n"
        "    print('ffmpeg version 9.9-fake')\n"
        "    sys.exit(0)\n"
        "print('Encoders:')\n"
        "print(' V....D libx265              H.265/HEVC')\n"
        "print(' V....D hevc_qsv             H.265/HEVC (Intel QSV)')\n",
    )
    facts = probe_encoders(str(fake))
    assert facts["version"].startswith("ffmpeg version 9.9-fake")
    assert facts["libx265"] is True
    assert facts["hevc_qsv"] is True
    assert facts["libx264"] is False
    assert facts["error"] is None
