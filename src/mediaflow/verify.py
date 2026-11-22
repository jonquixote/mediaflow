"""Output verification before publish.

Implements the SAF-2 gate: the output must open, have exactly the planned
streams, carry the planned codecs and per-track contract, match the planned
geometry/timing/metadata, clear the bitrate floor, and decode fully.  An
encode that falls below the bits-per-pixel quality floor sets
``quality_floor_breach`` but is *not* a hard failure; the engine routes that
job to REVIEW quality-floor.

The advisory quality metric is **bits per pixel per frame**::

    video_payload_bytes * 8 / (presented_video_frame_count * width * height)

The frame-count basis is reported next to the metric (``bpp_frame_basis``):
``container`` is ffprobe's ``nb_frames``, ``decoded`` is ffprobe's
``-count_frames`` result and ``estimated`` is duration x frame rate.  An
estimate is always labelled; for variable-frame-rate media it is not exact.
When the frame count cannot be established the metric is not evaluated and
the reason is surfaced in ``bpp_note``/``warnings``; the mandatory structural
and decode checks are unaffected.

This module also enforces the encode-only must-be-smaller rule from SIZE-3;
remux jobs are exempt from that rule but not from the bitrate floor.

Requirement coverage: [SAF-2], [SIZE-3], plus the output-facing parts of
[MP4-1], [MP4-3], [MP4-4], [MP4-5], [MP4-9] and [MP4-12].  Every
ffprobe/ffmpeg call carries the protocol whitelist from [SAF-13].
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .supervision import (
    NON_SUCCESS_STATES,
    OperationCancelled,
    OperationDeferred,
    OperationInterrupted,
    OperationState,
    ProtectionAction,
    ProtectionDecision,
    SupervisionPolicy,
    byte_work_value,
    run_process,
    supervise_callable,
)

from .ffmpeg import (
    FFMPEG,
    FFPROBE,
    PROTOCOL_WHITELIST,
    expected_output_codec,
)

log = logging.getLogger(__name__)

HASH_CHUNK_SIZE = 1024 * 1024
ProgressCallback = Callable[[dict[str, Any]], None]

UNKNOWN_VALUES = {"", "unknown", "unspecified", "n/a", "none", "reserved"}
COLOR_ALIASES = {
    "limited": "tv",
    "full": "pc",
}

#: Documented codec spellings that mean the same codec.
CODEC_ALIASES = {
    "h265": "hevc",
    "h.265": "hevc",
    "hevc": "hevc",
    "h264": "h264",
    "h.264": "h264",
    "avc": "h264",
    "avc1": "h264",
    "ac-3": "ac3",
    "ec3": "eac3",
    "eac-3": "eac3",
    "mlp": "truehd",
}

#: Pixel formats that describe the same depth and chroma layout in a
#: different but equivalent spelling.  8-bit semi-planar NV12 is the
#: format the hardware encoders emit for an 8-bit 4:2:0 stream; P010 is
#: the 10-bit equivalent of planar 10-bit 4:2:0.
PIX_FMT_EQUIVALENTS: tuple[frozenset[str], ...] = (
    frozenset({"yuv420p", "nv12"}),
    frozenset({"p010le", "yuv420p10le"}),
    frozenset({"p010be", "yuv420p10be"}),
)

#: Channel layouts that describe the same speaker set.  "5.1" and
#: "5.1(side)" only differ in the name ffprobe uses for the back/side
#: pair; the channel count and the mix are the same.
CHANNEL_LAYOUT_EQUIVALENTS: tuple[frozenset[str], ...] = (
    frozenset({"5.1", "5.1(side)"}),
    frozenset({"7.1", "7.1(side)"}),
)


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
    # Advisory quality metric: video payload bits per pixel per frame.
    bpp: float | None = None
    bpp_frame_count: int | None = None
    bpp_frame_basis: str | None = None
    bpp_note: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: Typed operation outcome.  ``ok`` is true only for a completed,
    #: uninterrupted verification.
    state: OperationState = OperationState.SUCCEEDED
    interrupted: bool = False
    cancelled: bool = False
    deferred: bool = False
    stalled: bool = False
    timeout_type: str | None = None
    timeout_phase: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PacketScanSummary:
    """Bounded aggregate of one video packet scan.

    ``presentation_end`` is the maximum ``pts + duration`` observed across
    all packets.  It is deliberately not the timestamp of the last packet:
    packet order is not presentation order.
    """

    packet_count: int
    video_bytes: int
    presentation_end: float | None = None
    packets: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class FrameScanSummary:
    """Bounded aggregate of one decoded-video frame scan."""

    frame_count: int
    presentation_end: float | None = None


@dataclass(frozen=True)
class _OperationOutcome:
    """Internal typed outcome for one supervised verification step."""

    state: OperationState
    value: Any = None
    reason: str | None = None
    reasons: tuple[str, ...] = ()
    timeout_type: str | None = None
    phase: str = ""


class VerificationOperationError(RuntimeError):
    """A verification step ended before it could produce a valid result."""

    def __init__(
        self,
        message: str,
        *,
        state: OperationState,
        reasons: tuple[str, ...] = (),
        timeout_type: str | None = None,
        phase: str = "",
    ) -> None:
        super().__init__(message)
        self.state = state
        self.reasons = tuple(reasons)
        self.timeout_type = timeout_type
        self.phase = phase


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
    """Compatibility helper for callers outside the production verifier."""

    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout or None,
        check=False,
    )


def _event_is_set(event: Any) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except Exception:
        return False


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"n/a", "nan", "none", "unknown"}:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _step_policy(
    base: SupervisionPolicy | None,
    timeout: float | None,
    *,
    progress: bool,
) -> SupervisionPolicy:
    """Build one step policy without losing the caller's type distinctions."""

    base = base or SupervisionPolicy()
    overall = timeout if timeout is not None and timeout > 0 else base.overall_timeout
    return SupervisionPolicy(
        stall_timeout=(base.stall_timeout if progress else None),
        startup_grace=base.startup_grace,
        finalization_grace=base.finalization_grace,
        overall_timeout=overall,
        monitor_interval=base.monitor_interval,
        terminate_grace=base.terminate_grace,
    )


def _raise_terminal(outcome: _OperationOutcome) -> None:
    """Raise for an interrupted/deferred/cancelled/timed-out operation."""

    if outcome.state is OperationState.SUCCEEDED:
        return
    if outcome.state is OperationState.FAILED:
        return
    raise VerificationOperationError(
        outcome.reason or f"verification {outcome.phase} did not complete",
        state=outcome.state,
        reasons=outcome.reasons,
        timeout_type=outcome.timeout_type,
        phase=outcome.phase,
    )


def _probe_json(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
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
    process = run_process(
        argv,
        policy=_step_policy(supervision_policy, timeout, progress=False),
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
        stdout_limit=16 * 1024 * 1024,
    )
    outcome = _OperationOutcome(
        state=process.state,
        reason=process.reason,
        reasons=process.reasons,
        timeout_type=process.timeout_type,
        phase="probe",
    )
    _raise_terminal(outcome)
    if process.state is not OperationState.SUCCEEDED:
        detail = (process.stderr or "").strip()[-1000:]
        raise ProbeError(f"ffprobe failed (rc={process.returncode}): {detail}")
    if not (process.stdout or "").strip():
        raise ProbeError("ffprobe produced no JSON")
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe produced invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ProbeError("ffprobe JSON is not an object")
    return data


def _scan_video_packets(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    selector: str = "v:0",
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
    progress_cb: ProgressCallback | None = None,
    collect_packets: bool = False,
) -> _OperationOutcome:
    """Stream and aggregate one video packet scan without buffering it."""

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
        "csv=p=0",
        str(path),
    ]
    count = 0
    total_bytes = 0
    presentation_end: float | None = None
    compatibility_packets: list[dict[str, Any]] = []

    def parse(line: str, _state: dict[str, str]) -> Mapping[str, Any] | None:
        nonlocal count, total_bytes, presentation_end
        text = line.strip()
        if not text:
            return None
        try:
            row = next(csv.reader([text]))
        except (csv.Error, StopIteration):
            return None
        if not row or not any(str(value).strip() for value in row):
            return None
        pts = _finite_float(row[0]) if len(row) > 0 else None
        duration = _finite_float(row[1]) if len(row) > 1 else None
        size = _int(row[2]) if len(row) > 2 else None
        count += 1
        if size is not None and size > 0:
            total_bytes += size
        if pts is not None:
            end = pts + max(0.0, duration or 0.0)
            presentation_end = end if presentation_end is None else max(presentation_end, end)
        if collect_packets:
            compatibility_packets.append(
                {
                    "pts_time": row[0] if len(row) > 0 else None,
                    "duration_time": row[1] if len(row) > 1 else None,
                    "size": row[2] if len(row) > 2 else None,
                }
            )
        return {
            "phase": "packet-scan",
            "packets": count,
            "packet_count": count,
            "bytes": total_bytes,
            "work_progress": count,
            "work_dimension": "packet",
        }

    process = run_process(
        argv,
        policy=_step_policy(supervision_policy, timeout, progress=True),
        progress_parser=parse,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    outcome = _OperationOutcome(
        state=process.state,
        reason=process.reason,
        reasons=process.reasons,
        timeout_type=process.timeout_type,
        phase="packet-scan",
    )
    if process.state is OperationState.SUCCEEDED:
        outcome = _OperationOutcome(
            state=OperationState.SUCCEEDED,
            value=PacketScanSummary(
                packet_count=count,
                video_bytes=total_bytes,
                presentation_end=presentation_end,
                packets=tuple(compatibility_packets),
            ),
            phase="packet-scan",
        )
    elif process.state is OperationState.FAILED:
        detail = (process.stderr or "").strip()[-1000:]
        outcome = _OperationOutcome(
            state=OperationState.FAILED,
            reason=f"ffprobe packet scan failed (rc={process.returncode}): {detail}",
            phase="packet-scan",
        )
    return outcome


def _probe_video_packets(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    selector: str = "v:0",
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning the historical packet-list shape.

    Production verification uses :func:`_scan_video_packets`, whose memory
    use is independent of the number of packets.  This wrapper is retained
    for small callers that explicitly need individual packet records.
    """

    outcome = _scan_video_packets(
        path,
        ffprobe,
        timeout,
        selector,
        collect_packets=True,
    )
    _raise_terminal(outcome)
    if outcome.state is not OperationState.SUCCEEDED:
        raise ProbeError(outcome.reason or "ffprobe packet scan failed")
    summary = outcome.value
    if not isinstance(summary, PacketScanSummary):
        raise ProbeError("ffprobe packet scan produced no summary")
    return [dict(packet) for packet in summary.packets]


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
    outcome = _scan_video_packets(
        Path(path), ffprobe, timeout, selector=str(index)
    )
    _raise_terminal(outcome)
    if outcome.state is not OperationState.SUCCEEDED:
        raise ProbeError(outcome.reason or "ffprobe packet scan failed")
    summary = outcome.value
    if not isinstance(summary, PacketScanSummary):
        raise ProbeError("ffprobe packet scan produced no summary")
    return summary.video_bytes


def _scan_decoded_frames(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
    progress_cb: ProgressCallback | None = None,
) -> _OperationOutcome:
    """Decode and count video frames incrementally."""

    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_duration_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    count = 0
    presentation_end: float | None = None

    def parse(line: str, _state: dict[str, str]) -> Mapping[str, Any] | None:
        nonlocal count, presentation_end
        text = line.strip()
        if not text:
            return None
        try:
            row = next(csv.reader([text]))
        except (csv.Error, StopIteration):
            return None
        if not row or not any(str(value).strip() for value in row):
            return None
        pts = _finite_float(row[0]) if len(row) > 0 else None
        duration = _finite_float(row[1]) if len(row) > 1 else None
        count += 1
        if pts is not None:
            end = pts + max(0.0, duration or 0.0)
            presentation_end = end if presentation_end is None else max(presentation_end, end)
        return {
            "phase": "frame-scan",
            "frames": count,
            "frame_count": count,
            "work_progress": count,
            "work_dimension": "frame",
        }

    process = run_process(
        argv,
        policy=_step_policy(supervision_policy, timeout, progress=True),
        progress_parser=parse,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    outcome = _OperationOutcome(
        state=process.state,
        reason=process.reason,
        reasons=process.reasons,
        timeout_type=process.timeout_type,
        phase="frame-scan",
    )
    if process.state is OperationState.SUCCEEDED:
        outcome = _OperationOutcome(
            state=OperationState.SUCCEEDED,
            value=FrameScanSummary(
                frame_count=count,
                presentation_end=presentation_end,
            ),
            phase="frame-scan",
        )
    elif process.state is OperationState.FAILED:
        detail = (process.stderr or "").strip()[-1000:]
        outcome = _OperationOutcome(
            state=OperationState.FAILED,
            reason=f"ffprobe frame scan failed (rc={process.returncode}): {detail}",
            phase="frame-scan",
        )
    return outcome


def _count_decoded_frames(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
    progress_cb: ProgressCallback | None = None,
) -> int | None:
    """Decode the video stream and count the frames that are presented.

    This is the expensive fallback used when the container does not record
    a frame count.  It returns ``None`` when ffprobe cannot answer.  A
    cancellation, protection interruption or timeout remains a typed
    exception and is never flattened into an unknown frame count.
    """

    outcome = _scan_decoded_frames(
        path,
        ffprobe,
        timeout,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
        supervision_policy=supervision_policy,
        progress_cb=progress_cb,
    )
    _raise_terminal(outcome)
    if outcome.state is not OperationState.SUCCEEDED:
        return None
    summary = outcome.value
    if not isinstance(summary, FrameScanSummary):
        return None
    return summary.frame_count if summary.frame_count > 0 else None


def _presented_frame_count(
    path: Path,
    ffprobe: str,
    timeout: float | None,
    video_stream: Mapping[str, Any],
    duration: float,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
    progress_cb: ProgressCallback | None = None,
) -> tuple[int | None, str | None, str | None]:
    """Return ``(frames, basis, note)`` for the presented video frame count.

    The basis is one of:

    ``container``
        ffprobe's ``nb_frames``: the number of samples the container holds.
        For MP4 and MKV without edit-list trimming this is the number of
        presented frames.
    ``decoded``
        ffprobe's ``-count_frames`` result: frames actually decoded.
    ``estimated``
        duration multiplied by the average frame rate.  This is only an
        estimate; for variable-frame-rate media the average frame rate is
        not an exact frame count, so the note says so.

    ``None`` means the frame count could not be established; the note
    explains why.  The metric is never evaluated from a missing, zero,
    malformed or non-finite count.
    """
    frames = _int(video_stream.get("nb_frames"))
    if frames is not None and frames > 0:
        return frames, "container", None

    decoded = _count_decoded_frames(
        path,
        ffprobe,
        timeout,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
        supervision_policy=supervision_policy,
        progress_cb=progress_cb,
    )
    if decoded is not None and decoded > 0:
        return decoded, "decoded", None

    avg = _rational(video_stream.get("avg_frame_rate"))
    real = _rational(video_stream.get("r_frame_rate"))
    rate = avg if avg is not None else real
    note = "frame count estimated from duration x frame rate"
    if avg is not None and real is not None and abs(avg - real) > max(0.01, avg * 0.01):
        note += "; the stream looks variable-frame-rate, so the estimate may be wrong"
    if rate is not None and rate > 0 and duration and duration > 0:
        estimate = int(round(duration * rate))
        if estimate > 0:
            return estimate, "estimated", note
    return (
        None,
        None,
        "presented frame count is unknown (no container count, no decoded "
        "count and no usable frame rate)",
    )


def _check_audio_tracks(
    audio: list[Mapping],
    audio_streams: list[Mapping],
    failures: list[str],
) -> None:
    """Check every planned output audio track against the output."""
    for ordinal, entry in enumerate(audio):
        if ordinal >= len(audio_streams):
            break  # the count mismatch is reported separately
        stream = audio_streams[ordinal]

        expected_codec = _normalise_codec(entry.get("codec") or entry.get("source_codec"))
        actual_codec = _normalise_codec(stream.get("codec_name"))
        if expected_codec is not None:
            if actual_codec is None:
                failures.append(
                    f"audio track {ordinal}: could not determine the output codec"
                )
            elif actual_codec != expected_codec:
                failures.append(
                    f"audio track {ordinal}: codec {actual_codec} does not match "
                    f"planned {expected_codec}"
                )

        expected_channels = _int(entry.get("target_channels")) or _int(entry.get("channels"))
        actual_channels = _int(stream.get("channels"))
        if expected_channels is not None and expected_channels > 0:
            if actual_channels is None or actual_channels <= 0:
                failures.append(
                    f"audio track {ordinal}: could not determine the output "
                    "channel count"
                )
            elif actual_channels != expected_channels:
                failures.append(
                    f"audio track {ordinal}: channel count {actual_channels} does "
                    f"not match planned {expected_channels}"
                )

        expected_layout = str(entry.get("channel_layout") or "").strip()
        target_channels = _int(entry.get("target_channels"))
        source_channels = _int(entry.get("channels"))
        if target_channels is not None and source_channels is not None:
            if target_channels != source_channels:
                # A downmix: the speaker layout of the output is chosen by
                # the encoder, so only the channel count is contractual.
                expected_layout = ""
        if expected_layout:
            actual_layout = _output_channel_layout(stream)
            if not _channel_layout_matches(expected_layout, actual_layout):
                failures.append(
                    f"audio track {ordinal}: channel layout "
                    f"{actual_layout or '(missing)'} does not match planned "
                    f"{expected_layout.lower()}"
                )

        expected_language = str(entry.get("language") or "").strip().lower()
        if expected_language and expected_language != "und":
            actual_language = _output_language(stream)
            if actual_language != expected_language:
                failures.append(
                    f"audio track {ordinal}: language "
                    f"{actual_language or '(missing)'} does not match planned "
                    f"{expected_language}"
                )

        if "default" in entry:
            expected_default = _bool(entry.get("default"))
            actual_default = _disposition_flag(stream, "default")
            if actual_default != expected_default:
                failures.append(
                    f"audio track {ordinal}: default disposition is "
                    f"{actual_default}, planned {expected_default}"
                )


def _check_subtitle_tracks(
    subtitles: list[Mapping],
    subtitle_streams: list[Mapping],
    failures: list[str],
) -> None:
    """Check every planned output subtitle track against the output."""
    for ordinal, entry in enumerate(subtitles):
        if ordinal >= len(subtitle_streams):
            break  # the count mismatch is reported separately
        stream = subtitle_streams[ordinal]
        action = str(entry.get("action") or "").strip().lower()

        expected_codec: str | None = None
        if action == "mov_text":
            expected_codec = "mov_text"
        elif action in {"copy", "remux"}:
            expected_codec = _normalise_codec(
                entry.get("source_codec") or entry.get("codec")
            )
        actual_codec = _normalise_codec(stream.get("codec_name"))
        if expected_codec is not None:
            if actual_codec is None:
                failures.append(
                    f"subtitle track {ordinal}: could not determine the output codec"
                )
            elif actual_codec != expected_codec:
                failures.append(
                    f"subtitle track {ordinal}: codec {actual_codec} does not "
                    f"match planned {expected_codec}"
                )

        expected_language = str(entry.get("language") or "").strip().lower()
        if expected_language and expected_language != "und":
            actual_language = _output_language(stream)
            if actual_language != expected_language:
                failures.append(
                    f"subtitle track {ordinal}: language "
                    f"{actual_language or '(missing)'} does not match planned "
                    f"{expected_language}"
                )

        if "forced" in entry:
            expected_forced = _bool(entry.get("forced"))
            actual_forced = _disposition_flag(stream, "forced")
            if actual_forced != expected_forced:
                failures.append(
                    f"subtitle track {ordinal}: forced disposition is "
                    f"{actual_forced}, planned {expected_forced}"
                )

        if "default" in entry:
            expected_default = _bool(entry.get("default"))
            actual_default = _disposition_flag(stream, "default")
            if actual_default != expected_default:
                failures.append(
                    f"subtitle track {ordinal}: default disposition is "
                    f"{actual_default}, planned {expected_default}"
                )


def _sha256_file(
    path: Path,
    progress_cb: ProgressCallback | None,
    *,
    timeout: float | None = None,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
) -> tuple[str, str | None]:
    """Hash a file through the shared callable supervision boundary."""

    def operation(
        report: Callable[[Mapping[str, Any]], None],
        stop_event: threading.Event,
    ) -> str:
        digest = hashlib.sha256()
        total = path.stat().st_size
        done = 0
        with path.open("rb") as handle:
            while True:
                if _event_is_set(cancel_event) or stop_event.is_set():
                    raise OperationCancelled("verification hashing cancelled")
                while _event_is_set(pause_event):
                    if _event_is_set(cancel_event) or stop_event.is_set():
                        raise OperationCancelled("verification hashing cancelled")
                    time.sleep(0.05)
                chunk = handle.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                percent = (done / total * 100.0) if total else 100.0
                report(
                    {
                        "phase": "hashing",
                        "percent": percent,
                        "bytes": done,
                        "work_progress": done,
                        "work_dimension": "bytes",
                    }
                )
        if total == 0:
            report(
                {
                    "phase": "hashing",
                    "percent": 100.0,
                    "bytes": 0,
                    "work_progress": 0,
                    "work_dimension": "bytes",
                }
            )
        return digest.hexdigest()

    supervised = supervise_callable(
        operation,
        policy=_step_policy(supervision_policy, timeout, progress=True),
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
        extractor=byte_work_value,
    )
    outcome = _OperationOutcome(
        state=supervised.state,
        value=supervised.value,
        reason=supervised.reason,
        reasons=supervised.reasons,
        timeout_type=supervised.timeout_type,
        phase="hashing",
    )
    _raise_terminal(outcome)
    if outcome.state is not OperationState.SUCCEEDED:
        return "", f"could not hash output: {outcome.reason or 'hash failed'}"
    return str(outcome.value or ""), None


def _decode_check(
    path: Path,
    ffmpeg: str,
    timeout: float | None,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
    progress_cb: ProgressCallback | None = None,
) -> tuple[bool, str]:
    """Decode all video and audio through the shared process supervisor."""

    argv = [
        str(ffmpeg),
        "-v",
        "error",
        "-nostats",
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
        "-progress",
        "pipe:1",
    ]
    frame = 0
    out_time_us = 0

    def parse(line: str, _state: dict[str, str]) -> Mapping[str, Any] | None:
        nonlocal frame, out_time_us
        text = line.strip()
        if not text or "=" not in text:
            return None
        key, raw = text.split("=", 1)
        if key == "frame":
            value = _finite_float(raw)
            if value is None:
                return None
            frame = int(value)
            return {
                "phase": "decoding",
                "frame": frame,
                "frames": frame,
                "work_progress": frame,
                "work_dimension": "frame",
            }
        if key == "out_time_us":
            value = _finite_float(raw)
            if value is None:
                return None
            out_time_us = int(value)
            return {
                "phase": "decoding",
                "out_time_seconds": out_time_us / 1_000_000.0,
                "work_progress": out_time_us,
                "work_dimension": "time",
            }
        if key == "progress" and raw == "end":
            return {
                "phase": "decoding",
                "finalizing": True,
                "work_progress": frame,
                "work_dimension": "frame",
            }
        return None

    process = run_process(
        argv,
        policy=_step_policy(supervision_policy, timeout, progress=True),
        progress_parser=parse,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    outcome = _OperationOutcome(
        state=process.state,
        reason=process.reason,
        reasons=process.reasons,
        timeout_type=process.timeout_type,
        phase="decode",
    )
    _raise_terminal(outcome)
    if outcome.state is not OperationState.SUCCEEDED:
        detail = (process.stderr or "").strip()[-2000:]
        return False, f"decode test failed (rc={process.returncode}): {detail}"
    stderr = (process.stderr or "").strip()
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


def _normalise_codec(value: Any) -> str | None:
    """Return a comparable codec name, or ``None`` when it is unknown."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text or text in UNKNOWN_VALUES:
        return None
    return CODEC_ALIASES.get(text, text)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _stream_tags(stream: Mapping[str, Any]) -> Mapping[str, Any]:
    tags = stream.get("tags")
    return tags if isinstance(tags, Mapping) else {}


def _output_language(stream: Mapping[str, Any]) -> str:
    """The output stream's language tag, normalised like the planner does."""
    from .plan import normalise_language

    tags = _stream_tags(stream)
    value = stream.get("language") or tags.get("language") or tags.get("LANGUAGE")
    return normalise_language(value)


def _disposition_flag(stream: Mapping[str, Any], key: str) -> bool:
    disposition = stream.get("disposition")
    if not isinstance(disposition, Mapping):
        return False
    return _bool(disposition.get(key))


def _output_channel_layout(stream: Mapping[str, Any]) -> str | None:
    value: Any = stream.get("channel_layout")
    if value is None:
        value = stream.get("ch_layout")
    if isinstance(value, Mapping):
        value = value.get("description") or value.get("name")
    text = str(value or "").strip().lower()
    if not text or text in UNKNOWN_VALUES:
        return None
    return text


def _channel_layout_matches(expected: str, actual: str | None) -> bool:
    if actual is None:
        return False
    expected = str(expected).strip().lower()
    if expected == actual:
        return True
    for group in CHANNEL_LAYOUT_EQUIVALENTS:
        if expected in group and actual in group:
            return True
    return False


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
    expected = str(expected).strip().lower()
    actual = str(actual).strip().lower()
    if actual == expected:
        return True
    for group in PIX_FMT_EQUIVALENTS:
        if expected in group and actual in group:
            return True
    return False


def _pix_fmt_bit_depth(pix_fmt: Any) -> int | None:
    """The bit depth implied by a pixel format, when it is unambiguous."""
    if not pix_fmt:
        return None
    text = str(pix_fmt).strip().lower()
    if not text:
        return None
    for depth in (16, 14, 12, 10, 9):
        if f"p{depth:02d}" in text or f"p{depth}le" in text or f"p{depth}be" in text:
            return depth
        if f"{depth}le" in text or f"{depth}be" in text:
            return depth
    if text in {
        "yuv420p",
        "yuvj420p",
        "nv12",
        "nv21",
        "yuv422p",
        "yuvj422p",
        "yuv444p",
        "yuvj444p",
        "gray",
    }:
        return 8
    return None


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
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
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

    # The caller's event is an external pause request.  Machine protection
    # has its own event, so the two sources can compose without either one
    # clearing the other.  The combined monitor is the sole owner of the
    # machine-pause event; individual workers only observe it.
    machine_pause = threading.Event()

    def combined_monitor() -> ProtectionDecision:
        if monitor is None:
            decision = ProtectionDecision(ProtectionAction.CONTINUE)
        else:
            decision = monitor()
        external_pause = _event_is_set(pause_event)
        reasons = list(decision.reasons)
        if external_pause and "user-paused" not in reasons:
            reasons.append("user-paused")
        if decision.action is ProtectionAction.TERMINATE or decision.action is ProtectionAction.DEFER:
            machine_pause.clear()
            return ProtectionDecision(
                decision.action,
                tuple(reasons),
                detail=decision.detail,
                kind=decision.kind,
            )
        if external_pause or decision.action is ProtectionAction.PAUSE:
            machine_pause.set()
            return ProtectionDecision(
                ProtectionAction.PAUSE,
                tuple(reasons),
                detail=decision.detail,
                kind=decision.kind,
            )
        machine_pause.clear()
        return decision

    def terminal_result(exc: VerificationOperationError) -> VideoVerifyResult:
        result.state = exc.state
        result.interrupted = exc.state is OperationState.INTERRUPTED
        result.cancelled = exc.state is OperationState.CANCELLED
        result.deferred = exc.state is OperationState.DEFERRED
        result.stalled = exc.state is OperationState.STALLED
        result.timeout_type = exc.timeout_type
        result.timeout_phase = exc.phase or None
        result.reasons = tuple(exc.reasons)
        result.ok = False
        result.failures = [*failures, str(exc)]
        _notify(progress_cb, {"phase": "verifying", "percent": 100.0})
        return result

    _notify(progress_cb, {"phase": "verifying", "percent": 0.0})

    # -- 1. output file guards ------------------------------------------
    if path.is_symlink():
        failures.append(f"output is a symlink, not a regular file: {path}")
        result.state = OperationState.FAILED
        result.failures = failures
        return result
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        failures.append(f"output does not exist: {path}")
        result.state = OperationState.FAILED
        result.failures = failures
        return result
    except OSError as exc:
        failures.append(f"cannot stat output: {exc}")
        result.state = OperationState.FAILED
        result.failures = failures
        return result
    if not stat.S_ISREG(stat_result.st_mode):
        failures.append(f"output is not a regular file: {path}")
        result.state = OperationState.FAILED
        result.failures = failures
        return result

    result.output_size = stat_result.st_size
    try:
        sha256, hash_error = _sha256_file(
            path,
            progress_cb,
            timeout=timeout,
            cancel_event=cancel_event,
            pause_event=machine_pause,
            monitor=combined_monitor,
            supervision_policy=supervision_policy,
        )
    except VerificationOperationError as exc:
        return terminal_result(exc)
    result.sha256 = sha256
    if hash_error:
        failures.append(hash_error)
    _notify(progress_cb, {"phase": "verifying", "percent": 15.0})

    if result.output_size == 0:
        failures.append("output is empty (zero bytes)")
        result.state = OperationState.FAILED
        result.failures = failures
        _notify(progress_cb, {"phase": "verifying", "percent": 100.0})
        return result

    # -- 2. ffprobe ------------------------------------------------------
    try:
        probe = _probe_json(
            path,
            ffprobe,
            timeout,
            cancel_event=cancel_event,
            pause_event=machine_pause,
            monitor=combined_monitor,
            supervision_policy=supervision_policy,
        )
    except VerificationOperationError as exc:
        return terminal_result(exc)
    except ProbeError as exc:
        failures.append(str(exc))
        result.state = OperationState.FAILED
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

    # -- video packets: sizes and presentation tail ----------------------
    packet_outcome = _scan_video_packets(
        path,
        ffprobe,
        timeout,
        cancel_event=cancel_event,
        pause_event=machine_pause,
        monitor=combined_monitor,
        supervision_policy=supervision_policy,
        progress_cb=progress_cb,
    )
    try:
        _raise_terminal(packet_outcome)
    except VerificationOperationError as exc:
        return terminal_result(exc)
    packet_summary: PacketScanSummary | None = None
    if packet_outcome.state is OperationState.SUCCEEDED:
        candidate = packet_outcome.value
        if isinstance(candidate, PacketScanSummary):
            packet_summary = candidate
    else:
        failures.append(packet_outcome.reason or "ffprobe packet scan failed")
    if packet_summary is None or packet_summary.packet_count <= 0:
        if videos:
            failures.append("no video packets found in output")
    else:
        result.output_video_bytes = packet_summary.video_bytes
        presentation_end = packet_summary.presentation_end
        if presentation_end is None:
            failures.append("could not determine the video presentation tail")
        elif output_duration is not None and output_duration > 0:
            if abs(presentation_end - output_duration) > tolerance:
                failures.append(
                    f"video presentation tail ends at {presentation_end:.3f}s but "
                    f"container duration is {output_duration:.3f}s "
                    f"(tolerance {tolerance:.3f}s)"
                )

    # -- 4. video metadata ----------------------------------------------
    video_stream: Mapping[str, Any] = videos[0] if videos else {}
    if videos:
        width = _int(video_stream.get("width"))
        height = _int(video_stream.get("height"))
        if width != target_width or height != target_height:
            failures.append(
                f"video resolution {width}x{height} does not match planned "
                f"{target_width}x{target_height}"
            )

        # The planned codec contract: an encode is decided by the planned
        # encoder, a remux by the copied source codec.  A known wrong codec
        # fails even when geometry, duration and stream counts match.
        expected_codec = expected_output_codec(plan)
        actual_codec = _normalise_codec(video_stream.get("codec_name"))
        if expected_codec:
            if actual_codec is None:
                failures.append("could not determine the output video codec")
            elif actual_codec != expected_codec:
                failures.append(
                    f"video codec {actual_codec} does not match planned "
                    f"{expected_codec}"
                )

        # MP4-1 is an additional check on top of the codec contract: every
        # HEVC stream in an MP4 output carries the hvc1 sample entry.
        if container == "mp4" and actual_codec == "hevc":
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

        # The planned bit depth is enforced through the pixel format; an
        # explicit depth check catches a plan that names one.
        planned_depth = _int(video.get("bit_depth"))
        actual_depth = _pix_fmt_bit_depth(actual_pix_fmt)
        if (
            planned_depth
            and planned_depth > 0
            and actual_depth is not None
            and actual_depth != planned_depth
        ):
            failures.append(
                f"video bit depth {actual_depth} does not match planned "
                f"{planned_depth}"
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
                    # Unknown or unspecified in the plan: no requirement is
                    # invented.
                    continue
                actual = _normalise_colour(video_stream.get(stream_key))
                if actual is None:
                    failures.append(
                        f"colour {plan_key} is missing in the output but the plan "
                        f"requires {expected}"
                    )
                elif actual != expected:
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

    # -- 5. planned audio contract ---------------------------------------
    _check_audio_tracks(audio, audio_streams, failures)

    # -- 6. planned subtitle contract ------------------------------------
    _check_subtitle_tracks(subtitles, subtitle_streams, failures)

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

    # -- 7. SIZE-3 must-be-smaller, encode only --------------------------
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

    # -- 8. quality floor is REVIEW, not FAIL ---------------------------
    # bits per pixel per frame:
    #     video_payload_bytes * 8 / (presented_frames * width * height)
    # The metric is always reported for an encode when it can be computed;
    # the frame-count basis is reported with it.  An estimate is labelled as
    # such.  A metric that cannot be evaluated is surfaced as a warning and
    # never silently passes.
    if action == "encode":
        try:
            frames, basis, note = _presented_frame_count(
                path,
                ffprobe,
                timeout,
                video_stream,
                result.output_duration,
                cancel_event=cancel_event,
                pause_event=machine_pause,
                monitor=combined_monitor,
                supervision_policy=supervision_policy,
                progress_cb=progress_cb,
            )
        except VerificationOperationError as exc:
            return terminal_result(exc)
        if note:
            result.bpp_note = note
            result.warnings.append(note)
        if frames is None or frames <= 0:
            reason = (
                "the bits-per-pixel floor was not evaluated: the presented "
                "frame count is unknown"
            )
            result.bpp_note = reason
            result.warnings.append(reason)
        elif result.output_video_bytes <= 0:
            reason = (
                "the bits-per-pixel floor was not evaluated: the video stream "
                "carries no payload bytes"
            )
            result.bpp_note = reason
            result.warnings.append(reason)
        elif target_width <= 0 or target_height <= 0:
            reason = (
                "the bits-per-pixel floor was not evaluated: the planned "
                "output geometry is invalid"
            )
            result.bpp_note = reason
            result.warnings.append(reason)
        else:
            bpp = (
                result.output_video_bytes
                * 8.0
                / (frames * target_width * target_height)
            )
            if not math.isfinite(bpp):
                reason = (
                    "the bits-per-pixel floor was not evaluated: the metric is "
                    "not finite"
                )
                result.bpp_note = reason
                result.warnings.append(reason)
            else:
                result.bpp = bpp
                result.bpp_frame_count = frames
                result.bpp_frame_basis = basis
                if bpp_floor is not None and bpp_floor > 0 and bpp < bpp_floor:
                    result.quality_floor_breach = True

    # -- 9. full decode (mandatory) ---------------------------------------
    _notify(progress_cb, {"phase": "verifying", "percent": 60.0})
    try:
        decode_ok, decode_message = _decode_check(
            path,
            ffmpeg,
            timeout,
            cancel_event=cancel_event,
            pause_event=machine_pause,
            monitor=combined_monitor,
            supervision_policy=supervision_policy,
            progress_cb=progress_cb,
        )
    except VerificationOperationError as exc:
        return terminal_result(exc)
    if not decode_ok:
        failures.append(decode_message)

    result.failures = failures
    result.state = OperationState.SUCCEEDED if not failures else OperationState.FAILED
    result.ok = not failures and result.state is OperationState.SUCCEEDED
    _notify(progress_cb, {"phase": "verifying", "percent": 100.0})
    return result
