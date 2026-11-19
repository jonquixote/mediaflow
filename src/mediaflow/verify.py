"""Output verification before publish.

Implements the SAF-2 gate: the output must open, have exactly the planned
streams, match the planned geometry/timing/metadata, clear the bitrate floor,
and decode fully.  An encode that falls below the bits-per-pixel quality
floor sets ``quality_floor_breach`` but is *not* a hard failure; the engine
routes that job to REVIEW quality-floor.

This module also enforces the encode-only must-be-smaller rule from SIZE-3;
remux jobs are exempt from that rule but not from the bitrate floor.

Requirement coverage: [SAF-2], [SIZE-3], plus the output-facing parts of
[MP4-1], [MP4-9] and [MP4-12].  Every ffprobe/ffmpeg call carries the
protocol whitelist from [SAF-13].
"""

from __future__ import annotations

import hashlib
import json
import logging
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .ffmpeg import FFMPEG, FFPROBE, PROTOCOL_WHITELIST

log = logging.getLogger(__name__)

HASH_CHUNK_SIZE = 1024 * 1024
ProgressCallback = Callable[[dict[str, Any]], None]

UNKNOWN_VALUES = {"", "unknown", "n/a", "none", "reserved"}
COLOR_ALIASES = {
    "limited": "tv",
    "full": "pc",
}


@dataclass
class VideoVerifyResult:
    """Everything the engine needs to publish, review, or fail a job."""

    ok: bool = False
    failures: list[str] = field(default_factory=list)
    quality_floor_breach: bool = False
    sha256: str = ""
    output_size: int = 0
    output_video_bytes: int = 0
    output_duration: float = 0.0
    probe: dict[str, Any] = field(default_factory=dict)


class ProbeError(RuntimeError):
    """ffprobe could not read the output."""


class PlanError(ValueError):
    """The plan is missing a field or contains an unsupported value."""


# ---------------------------------------------------------------------------
# small plan and metadata helpers


def _mapping(value: Any, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise PlanError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _require(obj: Mapping, key: str, where: str) -> Any:
    if key not in obj or obj[key] is None:
        raise PlanError(f"plan is missing required field {where}.{key}")
    return obj[key]


def _require_int(obj: Mapping, key: str, where: str) -> int:
    value = _require(obj, key, where)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise PlanError(f"plan field {where}.{key} must be an integer: {value!r}") from None


def _list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PlanError(f"{what} must be a list")
    return list(value)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _notify(cb: ProgressCallback | None, event: dict[str, Any]) -> None:
    if cb is not None:
        cb(dict(event))


def _container(plan: Mapping) -> str:
    raw = plan.get("container")
    if raw is None:
        raw = plan.get("ext")
    if raw is None:
        raise PlanError("plan is missing required field container")
    container = str(raw).strip().lower()
    if container not in {"mp4", "mkv"}:
        raise PlanError(f"unsupported output container: {container!r}")
    return container


def _plan_view(
    plan: Mapping,
) -> tuple[str, str, Mapping, float, Mapping, list[Mapping], list[Mapping]]:
    if not isinstance(plan, Mapping):
        raise PlanError(f"plan must be a mapping, got {type(plan).__name__}")
    if plan.get("schema") not in (None, 1, "1"):
        raise PlanError(f"unsupported plan schema: {plan.get('schema')!r}")
    container = _container(plan)
    action = str(_require(plan, "action", "plan")).strip().lower()
    if action not in {"encode", "remux"}:
        raise PlanError(f"unsupported plan action: {action!r}")
    source = _mapping(_require(plan, "source", "plan"), "plan.source")
    source_duration = _float(_require(source, "duration", "source"))
    if source_duration is None or source_duration <= 0:
        raise PlanError("plan.source.duration must be a positive number")
    video = _mapping(_require(plan, "video", "plan"), "plan.video")
    _require_int(video, "target_width", "video")
    _require_int(video, "target_height", "video")
    audio_raw = _list(_require(plan, "audio", "plan"), "plan.audio")
    subtitles_raw = _list(_require(plan, "subtitles", "plan"), "plan.subtitles")
    audio = [
        _mapping(entry, f"plan.audio[{index}]") for index, entry in enumerate(audio_raw)
    ]
    subtitles = [
        _mapping(entry, f"plan.subtitles[{index}]")
        for index, entry in enumerate(subtitles_raw)
    ]
    return action, container, source, source_duration, video, audio, subtitles


# ---------------------------------------------------------------------------
# ffprobe/ffmpeg helpers


def _run(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout or None,
        check=False,
    )


def _probe_json(
    path: Path, ffprobe: str, timeout: float | None
) -> dict[str, Any]:
    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = _run(argv, timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError(f"ffprobe timed out after {timeout}s") from None
    except OSError as exc:
        raise ProbeError(f"could not run ffprobe: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-1000:]
        raise ProbeError(f"ffprobe failed (rc={proc.returncode}): {detail}")
    if not (proc.stdout or "").strip():
        raise ProbeError("ffprobe produced no JSON")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe produced invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ProbeError("ffprobe JSON is not an object")
    return data


def _probe_video_packets(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    selector: str = "v:0",
) -> list[dict[str, Any]]:
    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-select_streams",
        selector,
        "-show_entries",
        "packet=pts_time,duration_time,size",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = _run(argv, timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError(f"ffprobe packet scan timed out after {timeout}s") from None
    except OSError as exc:
        raise ProbeError(f"could not run ffprobe: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-1000:]
        raise ProbeError(f"ffprobe packet scan failed (rc={proc.returncode}): {detail}")
    if not (proc.stdout or "").strip():
        raise ProbeError("ffprobe packet scan produced no JSON")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe packet scan produced invalid JSON: {exc}") from None
    packets = data.get("packets") if isinstance(data, dict) else None
    if not isinstance(packets, list):
        raise ProbeError("ffprobe packet scan has no packets list")
    return [p for p in packets if isinstance(p, dict)]


def video_packet_bytes(
    path: Path | str,
    stream_index: int = 0,
    ffprobe: str = FFPROBE,
    timeout: float | None = 3600,
) -> int:
    """Sum the encoded sizes of one video stream's packets.

    Kept here as a public fallback so the verifier does not depend on a
    concurrently-developed probe module.  The call shape mirrors
    :func:`mediaflow.probe.video_packet_bytes`.  It probes with the mandatory
    protocol whitelist and never uses a shell.
    """
    index = int(stream_index)
    if index < 0:
        raise ValueError("stream_index must be non-negative")
    packets = _probe_video_packets(Path(path), ffprobe, timeout, selector=str(index))
    return sum(max(0, _int(packet.get("size")) or 0) for packet in packets)


def _sha256_file(
    path: Path, progress_cb: ProgressCallback | None
) -> tuple[str, str | None]:
    digest = hashlib.sha256()
    try:
        total = path.stat().st_size
        done = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                percent = (done / total * 100.0) if total else 100.0
                _notify(
                    progress_cb,
                    {"phase": "hashing", "percent": percent, "bytes": done},
                )
    except OSError as exc:
        return "", f"could not hash output: {exc}"
    if total == 0:
        _notify(progress_cb, {"phase": "hashing", "percent": 100.0, "bytes": 0})
    return digest.hexdigest(), None


def _decode_check(
    path: Path, ffmpeg: str, timeout: float | None
) -> tuple[bool, str]:
    argv = [
        str(ffmpeg),
        "-v",
        "error",
        "-xerror",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-i",
        str(path),
        # Decode all playable payload, not just the first video stream: a
        # corrupt audio stream must fail SAF-2 as well.  Subtitle maps are
        # omitted because ffmpeg has no null subtitle encoder; subtitle
        # streams are covered by the exact stream-count checks.  The optional
        # audio map tolerates outputs that legitimately have no audio.
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = _run(argv, timeout)
    except subprocess.TimeoutExpired:
        return False, f"decode test timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run decode test: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-2000:]
        return False, f"decode test failed (rc={proc.returncode}): {detail}"
    stderr = (proc.stderr or "").strip()
    if stderr:
        return False, f"decode test wrote to stderr: {stderr[-2000:]}"
    return True, ""


# ---------------------------------------------------------------------------
# metadata comparisons


def _normalise_sar(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in UNKNOWN_VALUES or text in {"0:1", "0/1"}:
        return None
    text = text.replace("/", ":")
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        numerator, denominator = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    from math import gcd

    divisor = gcd(numerator, denominator)
    return f"{numerator // divisor}:{denominator // divisor}"


def _rational(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in UNKNOWN_VALUES:
        return None
    if ":" in text or "/" in text:
        text = text.replace(":", "/")
        numerator, _, denominator = text.partition("/")
        try:
            top, bottom = float(numerator), float(denominator)
        except ValueError:
            return None
        if bottom == 0:
            return None
        return top / bottom
    try:
        return float(text)
    except ValueError:
        return None


def _normalise_colour(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in UNKNOWN_VALUES:
        return None
    return COLOR_ALIASES.get(text, text)


def _expected_pix_fmt(action: str, video: Mapping) -> str | None:
    explicit = video.get("target_pix_fmt") or video.get("expected_pix_fmt")
    if explicit:
        return str(explicit)
    if action == "remux":
        source_pix_fmt = video.get("pix_fmt")
        return str(source_pix_fmt) if source_pix_fmt else None
    bit_depth = _int(video.get("bit_depth")) or 8
    return "p010le" if bit_depth >= 10 else "yuv420p"


def _pix_fmt_matches(expected: str, actual: str | None) -> bool:
    if actual is None:
        return False
    if actual == expected:
        return True
    tens = {"p010le", "yuv420p10le"}
    return expected in tens and actual in tens


# ---------------------------------------------------------------------------
# main verification


def verify_output(
    plan: Mapping,
    output: Path | str,
    *,
    ffprobe: str = FFPROBE,
    ffmpeg: str = FFMPEG,
    min_output_kbps: float = 300,
    min_bpp: float = 0.01,
    ffmpeg_timeout: float = 3600,
    progress_cb: ProgressCallback | None = None,
) -> VideoVerifyResult:
    """Run every SAF-2 output check without modifying the output file.

    The full decode is mandatory; there is deliberately no configuration
    switch that can weaken SAF-2 before publish.

    The bits-per-pixel floor is advisory: a breach sets
    ``quality_floor_breach`` and leaves ``ok`` true so the engine can route the
    job to REVIEW quality-floor.  Encode outputs larger than the source video
    stream fail per SIZE-3; remuxes are exempt from that comparison.
    """
    action, container, source, source_duration, video, audio, subtitles = _plan_view(
        plan
    )
    target_width = _require_int(video, "target_width", "video")
    target_height = _require_int(video, "target_height", "video")
    kbps_floor = float(min_output_kbps) if min_output_kbps is not None else None
    bpp_floor = float(min_bpp) if min_bpp is not None else None
    timeout = ffmpeg_timeout if ffmpeg_timeout and ffmpeg_timeout > 0 else None

    path = Path(output).expanduser().absolute()
    failures: list[str] = []
    result = VideoVerifyResult()
    _notify(progress_cb, {"phase": "verifying", "percent": 0.0})

    # -- 1. output file guards ------------------------------------------
    if path.is_symlink():
        failures.append(f"output is a symlink, not a regular file: {path}")
        result.failures = failures
        return result
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        failures.append(f"output does not exist: {path}")
        result.failures = failures
        return result
    except OSError as exc:
        failures.append(f"cannot stat output: {exc}")
        result.failures = failures
        return result
    if not stat.S_ISREG(stat_result.st_mode):
        failures.append(f"output is not a regular file: {path}")
        result.failures = failures
        return result

    result.output_size = stat_result.st_size
    sha256, hash_error = _sha256_file(path, progress_cb)
    result.sha256 = sha256
    if hash_error:
        failures.append(hash_error)
    _notify(progress_cb, {"phase": "verifying", "percent": 15.0})

    if result.output_size == 0:
        failures.append("output is empty (zero bytes)")
        result.failures = failures
        _notify(progress_cb, {"phase": "verifying", "percent": 100.0})
        return result

    # -- 2. ffprobe ------------------------------------------------------
    try:
        probe = _probe_json(path, ffprobe, timeout)
    except ProbeError as exc:
        failures.append(str(exc))
        result.failures = failures
        _notify(progress_cb, {"phase": "verifying", "percent": 100.0})
        return result
    result.probe = probe
    streams = probe.get("streams") or []
    if not isinstance(streams, list):
        failures.append("ffprobe returned malformed streams")
        streams = []
    format_info = probe.get("format") if isinstance(probe.get("format"), Mapping) else {}

    videos = [
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
    ]
    subtitle_streams = [
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "subtitle"
    ]

    if len(videos) != 1:
        failures.append(f"expected exactly one video stream, found {len(videos)}")
    if len(audio_streams) != len(audio):
        failures.append(
            f"expected {len(audio)} audio stream(s), found {len(audio_streams)}"
        )
    if len(subtitle_streams) != len(subtitles):
        failures.append(
            f"expected {len(subtitles)} subtitle stream(s), "
            f"found {len(subtitle_streams)}"
        )

    # -- duration --------------------------------------------------------
    output_duration = _float(format_info.get("duration"))
    if output_duration is None:
        stream_durations = [
            _float(stream.get("duration"))
            for stream in streams
            if isinstance(stream, Mapping)
        ]
        known = [value for value in stream_durations if value is not None and value > 0]
        output_duration = max(known) if known else 0.0
    result.output_duration = output_duration or 0.0
    tolerance = max(1.0, 0.001 * source_duration)
    if output_duration is None or output_duration <= 0:
        failures.append("output container duration is missing or non-positive")
    elif abs(output_duration - source_duration) > tolerance:
        failures.append(
            f"output duration {output_duration:.3f}s differs from source "
            f"{source_duration:.3f}s by more than {tolerance:.3f}s"
        )

    # -- video packets: sizes and last frame -----------------------------
    try:
        packets = _probe_video_packets(path, ffprobe, timeout)
    except ProbeError as exc:
        failures.append(str(exc))
        packets = []
    if not packets:
        if videos:
            failures.append("no video packets found in output")
    else:
        result.output_video_bytes = sum(
            max(0, _int(packet.get("size")) or 0) for packet in packets
        )
        last_pts: float | None = None
        last_duration = 0.0
        for packet in reversed(packets):
            pts = _float(packet.get("pts_time"))
            if pts is None:
                continue
            last_pts = pts
            last_duration = _float(packet.get("duration_time")) or 0.0
            break
        if last_pts is None:
            failures.append("could not determine the last video packet timestamp")
        elif output_duration is not None and output_duration > 0:
            last_end = last_pts + last_duration
            if abs(last_end - output_duration) > tolerance:
                failures.append(
                    f"last video packet ends at {last_end:.3f}s but container "
                    f"duration is {output_duration:.3f}s "
                    f"(tolerance {tolerance:.3f}s)"
                )

    # -- 4. video metadata ----------------------------------------------
    if videos:
        video_stream = videos[0]
        width = _int(video_stream.get("width"))
        height = _int(video_stream.get("height"))
        if width != target_width or height != target_height:
            failures.append(
                f"video resolution {width}x{height} does not match planned "
                f"{target_width}x{target_height}"
            )

        codec_name = str(video_stream.get("codec_name") or "").lower()
        plan_codec = str(video.get("codec") or "").lower()
        plan_encoder = str(video.get("encoder") or "").lower()
        plan_hevc = any(
            token in value
            for value in (plan_codec, plan_encoder)
            for token in ("hevc", "265")
        )
        # A missing codec_name is tolerated only so minimal probe fixtures can
        # still assert the tag; a known non-HEVC stream is never tag-checked.
        is_hevc = codec_name == "hevc" or (not codec_name and plan_hevc)
        if container == "mp4" and is_hevc:
            tag = str(video_stream.get("codec_tag_string") or "").lower()
            if tag != "hvc1":
                failures.append(
                    f"MP4 HEVC video tag is {tag or '(missing)'}, expected hvc1"
                )

        expected_pix_fmt = _expected_pix_fmt(action, video)
        actual_pix_fmt = (
            str(video_stream["pix_fmt"]) if video_stream.get("pix_fmt") else None
        )
        if expected_pix_fmt and not _pix_fmt_matches(expected_pix_fmt, actual_pix_fmt):
            failures.append(
                f"pixel format {actual_pix_fmt or '(missing)'} does not match "
                f"expected {expected_pix_fmt}"
            )

        expected_sar_raw = video.get("sar")
        if expected_sar_raw is not None:
            expected_sar = _normalise_sar(expected_sar_raw)
            actual_sar = _normalise_sar(video_stream.get("sample_aspect_ratio"))
            if expected_sar and actual_sar != expected_sar:
                failures.append(
                    f"sample aspect ratio {actual_sar or '(missing)'} does not "
                    f"match planned {expected_sar}"
                )

        colour = video.get("color")
        if isinstance(colour, Mapping):
            for plan_key, stream_key in (
                ("primaries", "color_primaries"),
                ("transfer", "color_transfer"),
                ("space", "color_space"),
                ("range", "color_range"),
            ):
                expected = _normalise_colour(colour.get(plan_key))
                if expected is None:
                    continue
                actual = _normalise_colour(video_stream.get(stream_key))
                if actual is None:
                    # "unknown" is explicitly acceptable for colour metadata.
                    continue
                if actual != expected:
                    failures.append(
                        f"colour {plan_key} is {actual}, expected {expected}"
                    )

        expected_rate = _rational(video.get("frame_rate"))
        if expected_rate is not None:
            actual_avg = _rational(video_stream.get("avg_frame_rate"))
            actual_r = _rational(video_stream.get("r_frame_rate"))
            actual_rate = actual_avg if actual_avg is not None else actual_r
            if actual_rate is None:
                failures.append("could not determine output frame rate")
            else:
                # Container timestamp rounding can move MP4 avg_frame_rate by a
                # few thousandths while the actual frame rate is unchanged.
                # Accept a rational-frame match or a sub-0.5% timing wobble.
                tolerance = max(0.01, abs(expected_rate) * 0.005)
                matches = abs(actual_rate - expected_rate) <= tolerance
                if not matches and actual_r is not None:
                    matches = abs(actual_r - expected_rate) <= tolerance
                if not matches:
                    failures.append(
                        f"frame rate {actual_rate:.6f} does not match planned "
                        f"{expected_rate:.6f}"
                    )

    # -- 5. bitrate floor (applies to remux as well) ---------------------
    total_kbps: float | None = None
    format_bitrate = _float(format_info.get("bit_rate"))
    if format_bitrate is not None and format_bitrate > 0:
        total_kbps = format_bitrate / 1000.0
    elif result.output_duration > 0:
        total_kbps = result.output_size * 8.0 / result.output_duration / 1000.0
    if kbps_floor is not None:
        if total_kbps is None:
            failures.append("could not determine total output bitrate")
        elif total_kbps < kbps_floor:
            failures.append(
                f"total bitrate {total_kbps:.1f} kbps is below the "
                f"{kbps_floor:.1f} kbps floor"
            )

    # -- 6. SIZE-3 must-be-smaller, encode only --------------------------
    if action == "encode":
        source_video_bytes = _int(source.get("video_bytes"))
        if (
            source_video_bytes is not None
            and source_video_bytes > 0
            and result.output_video_bytes > source_video_bytes
        ):
            failures.append(
                f"encoded video stream ({result.output_video_bytes} bytes) is "
                f"larger than the source video ({source_video_bytes} bytes)"
            )

    # -- 7. quality floor is REVIEW, not FAIL ---------------------------
    if (
        action == "encode"
        and bpp_floor is not None
        and bpp_floor > 0
        and result.output_duration > 0
        and target_width > 0
        and target_height > 0
    ):
        bpp = (
            result.output_video_bytes
            * 8.0
            / (result.output_duration * target_width * target_height)
        )
        if bpp < bpp_floor:
            result.quality_floor_breach = True

    # -- 8. full decode (mandatory) ---------------------------------------
    _notify(progress_cb, {"phase": "verifying", "percent": 60.0})
    decode_ok, decode_message = _decode_check(path, ffmpeg, timeout)
    if not decode_ok:
        failures.append(decode_message)

    result.failures = failures
    result.ok = not failures
    _notify(progress_cb, {"phase": "verifying", "percent": 100.0})
    return result
