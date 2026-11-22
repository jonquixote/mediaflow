"""Quality/runtime calibration of the *production* encode path.

Calibration answers one question: how fast is this preset on this machine
for this source, and what does each quality setting cost?  The measurement
is only meaningful when it exercises the same operation production uses:

* the same planner (:func:`mediaflow.plan.build_plan`) decides the backend,
  the target geometry, the bit depth and the encoder;
* the same command builder (:func:`mediaflow.ffmpeg.build_command`) and the
  same encoder (:class:`mediaflow.ffmpeg.FfmpegEncoder`) run the trial, so
  look-ahead, scaling, pixel format and quality handling are the production
  ones;
* the trial is encoded from an explicitly requested source interval
  (:class:`mediaflow.ffmpeg.SampleSpec`), and the quality metric compares
  that trial with *the same interval of the source*.

A stream-copy "reference" file is deliberately not used: a copy is
keyframe-aligned, so it does not cover the frames the trial covers, and it
keeps the source geometry.  Both sides of the comparison are decoded from
the same accurate seek instead, so they cover the same intended interval by
construction.

Requirement coverage: [SAF-14] (calibration uses production semantics and
never silently substitutes a backend), [SAF-13] (every shell-out carries
the protocol whitelist), [SAF-2] (a trial is validated before it is
measured or seeded).
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ffmpeg import (
    FFMPEG,
    FFPROBE,
    PROTOCOL_WHITELIST,
    FfmpegEncoder,
    SampleSpec,
)
from .guards import MachineProtectionMonitor
from .plan import PlanError, ReviewRequired, build_plan
from .probe import ProbeError, ProbeInterrupted, probe_media
from .safety import FileSafety
from .supervision import (
    OperationState,
    ProtectionAction,
    SlotBusy,
    SupervisionPolicy,
    processing_slot,
    run_process,
    slot_path,
)
from .util import human_bytes

#: What the measurement actually covers.  The trial encodes the video
#: stream only; audio and subtitle handling are not part of it.
SCOPE = "video-encode-only"

#: Frame-count tolerance between the trial and the reference interval.  One
#: frame absorbs a rounding difference, 2 % absorbs a container's frame-rate
#: rounding on long samples.
_FRAME_TOLERANCE = 0.02


class CalibrationInterrupted(RuntimeError):
    """A calibration operation ended before producing a usable measurement."""

    def __init__(
        self,
        message: str,
        *,
        state: OperationState,
        reasons: tuple[str, ...] = (),
        timeout_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.reasons = tuple(reasons)
        self.timeout_type = timeout_type


def _interrupted_result(result: dict[str, Any], exc: CalibrationInterrupted) -> dict[str, Any]:
    status = {
        OperationState.CANCELLED: "cancelled",
        OperationState.INTERRUPTED: "interrupted",
        OperationState.DEFERRED: "deferred",
        OperationState.TIMED_OUT: "timed-out",
        OperationState.STALLED: "stalled",
        OperationState.FAILED: "failed",
    }.get(exc.state, "refused")
    result["status"] = status
    result["reason"] = str(exc)
    result["state"] = exc.state.value
    result["reasons"] = list(exc.reasons)
    result["timeout_type"] = exc.timeout_type
    result["seeded"] = None
    result["cancelled"] = exc.state is OperationState.CANCELLED
    result["interrupted"] = exc.state is OperationState.INTERRUPTED
    result["deferred"] = exc.state is OperationState.DEFERRED
    result["stalled"] = exc.state is OperationState.STALLED
    return result


def _supervision_policy() -> SupervisionPolicy:
    return SupervisionPolicy(
        stall_timeout=600.0,
        startup_grace=30.0,
        finalization_grace=30.0,
        monitor_interval=0.2,
        terminate_grace=5.0,
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _capabilities(config: Any) -> dict[str, Any]:
    """The capability report the planner uses to pick a backend."""

    # An explicitly injected dictionary (tests and in-process callers) is
    # trusted; a persisted file must carry the tri-state schema marker.
    injected = getattr(config, "capabilities", None)
    if isinstance(injected, dict):
        return injected
    path = getattr(config, "capabilities_path", None)
    if not path:
        return {}
    from .doctor import read_capabilities

    return read_capabilities(path)


def _has_filter(
    ffmpeg: str,
    name: str,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> bool:
    process = run_process(
        [ffmpeg, "-hide_banner", "-filters"],
        policy=SupervisionPolicy(
            stall_timeout=None,
            overall_timeout=30,
            monitor_interval=0.2,
            terminate_grace=5.0,
        ),
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    if process.state is not OperationState.SUCCEEDED:
        raise CalibrationInterrupted(
            process.reason or "filter capability check did not complete",
            state=process.state,
            reasons=process.reasons,
            timeout_type=process.timeout_type,
        )
    return name in (process.stdout or "")


def _probe_file(
    ffprobe: str,
    path: Path,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> dict[str, Any]:
    """Probe one file; returns the format and the first video stream."""
    argv = [
        ffprobe,
        "-v",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    process = run_process(
        argv,
        policy=SupervisionPolicy(
            stall_timeout=None,
            overall_timeout=120,
            monitor_interval=0.2,
            terminate_grace=5.0,
        ),
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    if process.state in {
        OperationState.CANCELLED,
        OperationState.INTERRUPTED,
        OperationState.DEFERRED,
        OperationState.TIMED_OUT,
        OperationState.STALLED,
    }:
        raise CalibrationInterrupted(
            process.reason or "ffprobe did not complete",
            state=process.state,
            reasons=process.reasons,
            timeout_type=process.timeout_type,
        )
    if process.state is not OperationState.SUCCEEDED:
        detail = (process.stderr or "").strip()[-300:]
        raise RuntimeError(f"ffprobe failed for {path.name}: {detail}")
    try:
        data = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path.name}") from exc
    streams = data.get("streams") if isinstance(data, Mapping) else None
    streams = streams if isinstance(streams, list) else []
    video = next(
        (s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "video"),
        None,
    )
    return {
        "streams": streams,
        "video": dict(video) if video else {},
        "format": data.get("format") if isinstance(data, Mapping) else {},
    }


def _frame_count(
    ffprobe: str,
    path: Path,
    stream: Mapping[str, Any],
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> int | None:
    """The number of frames in one file, or ``None`` when it is unknown."""
    raw = stream.get("nb_frames")
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        return count
    argv = [
        ffprobe,
        "-v",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "json",
        str(path),
    ]
    process = run_process(
        argv,
        policy=SupervisionPolicy(
            stall_timeout=None,
            overall_timeout=300,
            monitor_interval=0.2,
            terminate_grace=5.0,
        ),
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    if process.state in {
        OperationState.CANCELLED,
        OperationState.INTERRUPTED,
        OperationState.DEFERRED,
        OperationState.TIMED_OUT,
        OperationState.STALLED,
    }:
        raise CalibrationInterrupted(
            process.reason or "frame count did not complete",
            state=process.state,
            reasons=process.reasons,
            timeout_type=process.timeout_type,
        )
    if process.state is not OperationState.SUCCEEDED:
        return None
    try:
        data = json.loads(process.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") if isinstance(data, Mapping) else None
    if not isinstance(streams, list) or not streams:
        return None
    try:
        count = int(streams[0].get("nb_read_frames"))
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _frame_rate(stream: Mapping[str, Any]) -> float | None:
    """The average frame rate of a stream, when it is usable."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        text = str(stream.get(key) or "").strip()
        if "/" not in text:
            continue
        num, den = text.split("/", 1)
        try:
            value = float(num) / float(den)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return None


def _score(
    ffmpeg: str,
    source: Path,
    trial: Path,
    *,
    start: float,
    duration: float,
    width: int,
    height: int,
    pix_fmt: str | None,
    filter_name: str,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    progress_cb: Any = None,
) -> tuple[str, float | None, str | None]:
    """Score the trial against the source interval it was encoded from.

    The reference is the *source interval* scaled to the planned geometry
    and normalised to the trial's pixel format.  Both inputs are decoded
    from the same accurate seek, so they cover the same frames.

    Returns ``(metric, score, reason)``.  A failed scoring run is reported
    as ``n/a`` with a reason; a score is never reported for a run that
    failed.
    """
    fmt = pix_fmt or "yuv420p"
    graph = (
        f"[1:v]scale={width}:{height}:flags=lanczos,format={fmt}[ref];"
        f"[0:v][ref]{filter_name}"
    )
    argv = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-i",
        str(trial),
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-lavfi",
        graph,
        "-f",
        "null",
        "-",
        "-progress",
        "pipe:1",
    ]

    def parse_progress(line: str, _state: dict[str, str]) -> Mapping[str, Any] | None:
        text = line.strip()
        if "=" not in text:
            return None
        key, raw = text.split("=", 1)
        if key == "frame":
            try:
                frame = int(raw)
            except ValueError:
                return None
            return {
                "phase": "calibration-scoring",
                "frame": frame,
                "work_progress": frame,
                "work_dimension": "frame",
            }
        if key == "out_time_us":
            try:
                value = int(raw)
            except ValueError:
                return None
            return {
                "phase": "calibration-scoring",
                "out_time_seconds": value / 1_000_000.0,
                "work_progress": value,
                "work_dimension": "time",
            }
        return None

    process = run_process(
        argv,
        policy=SupervisionPolicy(
            stall_timeout=600.0,
            startup_grace=30.0,
            finalization_grace=30.0,
            overall_timeout=1800,
            monitor_interval=0.2,
            terminate_grace=5.0,
        ),
        progress_parser=parse_progress,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    if process.state in {
        OperationState.CANCELLED,
        OperationState.INTERRUPTED,
        OperationState.DEFERRED,
        OperationState.TIMED_OUT,
        OperationState.STALLED,
    }:
        raise CalibrationInterrupted(
            process.reason or "scoring did not complete",
            state=process.state,
            reasons=process.reasons,
            timeout_type=process.timeout_type,
        )
    text = process.stderr or ""
    if process.state is not OperationState.SUCCEEDED:
        detail = process.stderr.strip().splitlines()[-1:] if process.stderr else []
        reason = f"scoring failed (rc={process.returncode})"
        if detail:
            reason += f": {detail[0][:200]}"
        return "n/a", None, reason
    if filter_name == "libvmaf":
        marker = "VMAF score:"
        label = "vmaf"
    else:
        marker = "All:"
        label = "ssim"
    if marker in text:
        try:
            value = float(text.split(marker, 1)[1].split()[0])
        except (IndexError, ValueError):
            return label, None, "scoring produced no usable score"
        if not math.isfinite(value):
            return label, None, "scoring produced a non-finite score"
        return label, value, None
    return label, None, "scoring produced no score"


def _preset_action(preset: Any) -> str:
    """The action the preset asks for (``encode`` or ``remux``)."""
    video = getattr(preset, "video", None)
    action = getattr(video, "action", None)
    if action is None and isinstance(preset, Mapping):
        action = (preset.get("video") or {}).get("action")
    if action is None and isinstance(preset, Mapping):
        action = preset.get("action")
    text = str(action or "encode").strip().lower()
    return "remux" if text in {"copy", "remux"} else text


def _preset_id(preset: Any) -> str | None:
    value = getattr(preset, "id", None)
    if value is None and isinstance(preset, Mapping):
        value = preset.get("id")
    return str(value) if value else None


def _quality_set(base: int, qualities: Sequence[int] | None) -> list[int]:
    if qualities:
        return sorted({max(1, min(50, int(q))) for q in qualities})
    return sorted({max(1, min(50, base + delta)) for delta in (-6, -3, 0, 4, 8)})


def _scratch_root(config: Any) -> Path:
    """The configured work area; calibration never writes to the system temp."""
    work = getattr(config, "work", None)
    if work is None:
        root = getattr(config, "root", None)
        if root is None:
            raise PlanError("the configuration has no work area")
        work = Path(root) / "_pipeline" / "work"
    return Path(work) / "calibration"


def _remove_scratch(config: Any, path: Path) -> None:
    """Remove the scratch directory through the approved deletion path."""
    try:
        safety = FileSafety(
            root=Path(getattr(config, "root", path.parent)),
            work=Path(getattr(config, "work", path.parent)),
            trash=Path(getattr(config, "trash", path.parent)),
        )
        safety.remove(path, "calibration-complete", scratch=path.parent)
    except Exception:
        # Cleanup must never mask the calibration result; the scratch area
        # is inside the configured work directory and is swept later.
        pass


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


def _event_is_set(event: Any) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except Exception:
        return False


def _decision_report(result: dict[str, Any], decision: Any) -> dict[str, Any]:
    """Map a protection decision to the calibration's typed outcome."""

    action = ProtectionAction(decision.action)
    reasons = list(getattr(decision, "reasons", ()) or ())
    detail = str(getattr(decision, "reason_text", "") or "")
    if action is ProtectionAction.PAUSE:
        result.update(
            status="paused",
            reason=detail or "calibration is paused",
            reasons=reasons,
            state=OperationState.PAUSED.value,
            deferred=False,
        )
    elif action is ProtectionAction.DEFER:
        result.update(
            status="deferred",
            reason=detail or "machine protection deferred calibration",
            reasons=reasons,
            state=OperationState.DEFERRED.value,
            deferred=True,
        )
    elif action is ProtectionAction.TERMINATE:
        cancelled = str(getattr(decision, "kind", "")) == "cancelled"
        result.update(
            status="cancelled" if cancelled else "interrupted",
            reason=detail or ("operation cancelled" if cancelled else "protection interrupted calibration"),
            reasons=reasons,
            state=(OperationState.CANCELLED if cancelled else OperationState.INTERRUPTED).value,
            cancelled=cancelled,
            interrupted=not cancelled,
            deferred=False,
        )
    return result


def _calibration_template(source: Path, preset: Any) -> dict[str, Any]:
    """Return the stable, honest shape of a calibration report."""

    return {
        "status": "refused",
        "reason": None,
        "scope": SCOPE,
        "file": str(source),
        "preset": _preset_id(preset),
        "duration": 0.0,
        "sample_start": 0.0,
        "sample_seconds": 0.0,
        "backend": None,
        "encoder": None,
        "source_geometry": None,
        "target_geometry": None,
        "bit_depth": None,
        "rows": [],
        "libvmaf": None,
        "metric": "n/a",
        "seeded": None,
        "state": OperationState.FAILED.value,
        "reasons": [],
        "timeout_type": None,
    }


def _calibration_preflight(source: Path, preset: Any) -> dict[str, Any] | None:
    """Return a completed refusal when no subprocess may be run."""

    result = _calibration_template(source, preset)
    if preset is None:
        result["reason"] = "no preset was selected"
        return result
    if not source.is_file():
        result["reason"] = f"source file does not exist: {source}"
        return result
    action = _preset_action(preset)
    if action != "encode":
        result["status"] = "not-applicable"
        result["reason"] = (
            f"preset {result['preset']} is a {action} preset; calibration "
            "measures the encode path"
        )
        return result
    return None


def calibrate(
    file: Path,
    config: Any,
    *,
    seconds: int = 90,
    preset: Any = None,
    store: Any = None,
    qualities: list[int] | None = None,
    progress: Any = None,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> dict:
    """Run a bounded, supervised calibration under the expensive-operation slot.

    Cancellation and pause events belong to the caller.  The calibration path
    never mutates either event.  Machine protection is configured to defer
    rather than wait indefinitely, and every subprocess (source probe, filter
    probe, trial, scoring and cleanup) runs while the single processing slot
    is held.
    """

    source = Path(file).expanduser()
    refusal = _calibration_preflight(source, preset)
    if refusal is not None:
        return refusal

    cancel = cancel_event if cancel_event is not None else threading.Event()
    pause = pause_event if pause_event is not None else threading.Event()
    slot = processing_slot(slot_path(config), timeout=0)
    try:
        slot.__enter__()
    except SlotBusy:
        result = _calibration_template(source, preset)
        result.update(
            status="deferred",
            reason="the expensive-operation slot is busy",
            reasons=["processing-slot-busy"],
            state=OperationState.DEFERRED.value,
        )
        return result
    except OSError as exc:
        result = _calibration_template(source, preset)
        result.update(
            status="refused",
            reason=f"the expensive-operation slot is unavailable: {exc}",
            reasons=["processing-slot-unavailable"],
            state=OperationState.FAILED.value,
        )
        return result

    try:
        return _calibrate_locked(
            source,
            config,
            seconds=seconds,
            preset=preset,
            store=store,
            qualities=qualities,
            progress=progress,
            cancel_event=cancel,
            pause_event=pause,
            monitor=monitor,
        )
    finally:
        try:
            slot.__exit__(None, None, None)
        except Exception:
            pass


def _calibrate_locked(
    file: Path,
    config: Any,
    *,
    seconds: int = 90,
    preset: Any = None,
    store: Any = None,
    qualities: list[int] | None = None,
    progress: Any = None,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> dict:
    """Encode a short sample at several qualities and return a table.

    The returned mapping always carries an honest ``status``:

    ``ok``
        the trials ran; ``rows`` holds one entry per quality.
    ``not-applicable``
        the preset does not measure anything (a remux preset).
    ``refused``
        the measurement could not be made (no usable backend, a source that
        needs review, an unreadable file).  ``reason`` says why.

    A row is only seeded into the speed table when the trial itself was
    valid: the encode succeeded, the artifact is non-empty and its geometry
    is the planned geometry.  A missing quality score does not invalidate a
    trial - the size and the elapsed time stay usable.
    """
    source = Path(file).expanduser()
    ffmpeg = str(getattr(config, "ffmpeg", FFMPEG))
    ffprobe = str(getattr(config, "ffprobe", FFPROBE))
    result = _calibration_template(source, preset)

    refusal = _calibration_preflight(source, preset)
    if refusal is not None:
        return refusal

    cancel = cancel_event if cancel_event is not None else threading.Event()
    pause = pause_event if pause_event is not None else threading.Event()
    protection = monitor if monitor is not None else MachineProtectionMonitor(
        config,
        cancel_event=cancel,
        pause_event=pause,
        defer_on_protection=True,
    )
    try:
        initial = protection()
    except Exception as exc:
        result.update(
            status="failed",
            reason=f"protection monitor failed: {exc}",
            reasons=["protection-monitor-error"],
            state=OperationState.FAILED.value,
        )
        return result
    if initial.action is not ProtectionAction.CONTINUE:
        return _decision_report(result, initial)
    if _event_is_set(pause):
        result.update(
            status="paused",
            reason="calibration is paused by the caller",
            reasons=["caller-paused"],
            state=OperationState.PAUSED.value,
            deferred=False,
        )
        return result
    if _event_is_set(cancel):
        result.update(
            status="cancelled",
            reason="operation cancelled",
            reasons=["user-cancelled"],
            state=OperationState.CANCELLED.value,
            cancelled=True,
        )
        return result

    try:
        probe = probe_media(
            source,
            ffprobe=ffprobe,
            cancel_event=cancel,
            pause_event=pause,
            monitor=protection,
        )
    except ProbeInterrupted as exc:
        return _interrupted_result(
            result,
            CalibrationInterrupted(
                str(exc),
                state=exc.state,
                reasons=exc.reasons,
                timeout_type=exc.timeout_type,
            ),
        )
    except ProbeError as exc:
        result["reason"] = f"the source could not be probed: {exc}"
        return result

    duration = float(probe.duration or 0.0)
    result["duration"] = duration
    if duration <= 0:
        result["reason"] = "the source has no usable duration"
        return result

    clip = max(1.0, min(float(seconds), duration))
    start = 0.0 if clip >= duration else max(0.0, (duration - clip) / 2.0)
    result["sample_start"] = start
    result["sample_seconds"] = clip

    capabilities = _capabilities(config)
    identity = {"title": source.stem, "year": None}
    try:
        plan = build_plan(
            source_path=source,
            probe=probe,
            preset=preset,
            config=config,
            identity=identity,
            capabilities=capabilities,
            speed_factor=None,
            plan_version=1,
            # Calibration always measures the encode path, even for a
            # source that production would keep (SIZE-1).
            action_override="encode",
            name=source.stem,
        )
    except ReviewRequired as exc:
        result["reason"] = f"the source needs review before it can be measured: {exc}"
        return result
    except PlanError as exc:
        result["reason"] = f"no production plan could be built: {exc}"
        return result

    video = plan.get("video") or {}
    backend = str(video.get("backend") or "")
    encoder_name = str(video.get("encoder") or "")
    target_width = int(video.get("target_width") or 0)
    target_height = int(video.get("target_height") or 0)
    result["backend"] = backend
    result["encoder"] = encoder_name
    result["source_geometry"] = {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }
    result["target_geometry"] = {
        "width": target_width,
        "height": target_height,
    }
    result["bit_depth"] = int(video.get("bit_depth") or 0)
    if target_width <= 0 or target_height <= 0:
        result["reason"] = "the plan has no usable target geometry"
        return result

    base_quality = int(video.get("quality") or 24)
    quality_list = _quality_set(base_quality, qualities)
    try:
        libvmaf = _has_filter(
            ffmpeg,
            "libvmaf",
            cancel_event=cancel,
            pause_event=pause,
            monitor=protection,
        )
        result["libvmaf"] = libvmaf
        if libvmaf:
            metric_filter = "libvmaf"
        elif _has_filter(
            ffmpeg,
            "ssim",
            cancel_event=cancel,
            pause_event=pause,
            monitor=protection,
        ):
            metric_filter = "ssim"
        else:
            metric_filter = ""
    except CalibrationInterrupted as exc:
        return _interrupted_result(result, exc)
    result["metric"] = metric_filter or "n/a"
    try:
        expected_frames = _expected_frames(
            source,
            ffprobe,
            clip,
            cancel_event=cancel,
            pause_event=pause,
            monitor=protection,
        )
    except CalibrationInterrupted as exc:
        return _interrupted_result(result, exc)

    scratch_root = _scratch_root(config)
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="calib-", dir=str(scratch_root)))
    sample = SampleSpec(start=start, duration=clip)

    rows: list[dict[str, Any]] = []
    try:
        try:
            for quality in quality_list:
                row = _run_trial(
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    plan=plan,
                    quality=quality,
                    scratch=scratch,
                    sample=sample,
                    source=source,
                    metric_filter=metric_filter,
                    start=start,
                    duration=clip,
                    target_width=target_width,
                    target_height=target_height,
                    expected_frames=expected_frames,
                    cancel_event=cancel,
                    pause_event=pause,
                    monitor=protection,
                    supervision_policy=_supervision_policy(),
                    progress_cb=progress,
                )
                rows.append(row)
                if progress:
                    progress(row)
        except CalibrationInterrupted as exc:
            result["rows"] = rows
            return _interrupted_result(result, exc)
    finally:
        _remove_scratch(config, scratch)

    result["rows"] = rows
    result["status"] = "ok"
    result["state"] = OperationState.SUCCEEDED.value
    result["reasons"] = []

    valid = [row for row in rows if row.get("valid") and row.get("speed_factor")]
    if store is not None and result["preset"] and valid and backend:
        chosen = min(valid, key=lambda row: abs(int(row["quality"]) - base_quality))
        # The trial runs at the planned target geometry.  Use that same
        # geometry for the speed-table bucket so the row is found by the
        # planner/estimate path (a scaled 4K source must not seed a >1080
        # row for a 1080p operation).
        speed_height = target_height
        try:
            store.speed_seed(
                result["preset"], backend, speed_height, float(chosen["speed_factor"])
            )
            result["seeded"] = {
                "preset": result["preset"],
                "backend": backend,
                "height": speed_height,
                "factor": chosen["speed_factor"],
                "quality": chosen["quality"],
            }
        except Exception as exc:
            result["seed_error"] = str(exc)[:200]
    return result


def _run_trial(
    *,
    ffmpeg: str,
    ffprobe: str,
    plan: Mapping[str, Any],
    quality: int,
    scratch: Path,
    sample: SampleSpec,
    source: Path,
    metric_filter: str,
    start: float,
    duration: float,
    target_width: int,
    target_height: int,
    expected_frames: int | None = None,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
    supervision_policy: SupervisionPolicy | None = None,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """Run one quality trial and measure it."""
    trial_plan = copy.deepcopy(dict(plan))
    trial_video = dict(trial_plan.get("video") or {})
    trial_video["quality"] = int(quality)
    trial_plan["video"] = trial_video
    ext = str(trial_plan.get("ext") or trial_plan.get("container") or "mp4")
    output = scratch / f"q{quality}.{ext}"

    row: dict[str, Any] = {
        "quality": int(quality),
        "size_bytes": None,
        "encode_seconds": None,
        "speed_factor": None,
        "score": None,
        "metric": "n/a",
        "frames": None,
        "valid": False,
        "reason": None,
        "command": None,
    }

    encoder = FfmpegEncoder(ffmpeg=ffmpeg)

    def trial_progress(event: Mapping[str, Any]) -> None:
        if progress_cb is not None:
            progress_cb({"quality": int(quality), **dict(event)})

    began = time.monotonic()
    try:
        encoded = encoder.encode(
            trial_plan,
            output=output,
            sample=sample,
            video_only=True,
            cancel_event=cancel_event,
            pause_event=pause_event,
            monitor=monitor,
            supervision_policy=supervision_policy,
            progress_cb=trial_progress,
        )
    except CalibrationInterrupted:
        raise
    except Exception as exc:
        row["reason"] = f"the trial could not run: {exc}"[:300]
        return row
    elapsed = max(0.001, time.monotonic() - began)
    row["command"] = list(encoded.command)
    row["encode_seconds"] = round(elapsed, 3)
    encoded_state = getattr(encoded, "state", OperationState.SUCCEEDED)
    if encoded_state is not OperationState.SUCCEEDED:
        raise CalibrationInterrupted(
            getattr(encoded, "stderr", "") or f"trial ended as {encoded_state.value}",
            state=encoded_state,
            reasons=tuple(getattr(encoded, "reasons", ()) or ()),
            timeout_type=getattr(encoded, "timeout_type", None),
        )
    if not encoded.ok:
        detail = (encoded.stderr or "").strip().splitlines()
        tail = detail[-1][:200] if detail else f"rc={encoded.returncode}"
        row["reason"] = f"the encode failed: {tail}"
        return row
    if not output.is_file() or output.stat().st_size <= 0:
        row["reason"] = "the encode produced no output"
        return row

    row["size_bytes"] = output.stat().st_size
    row["speed_factor"] = round(duration / elapsed, 4)

    try:
        probed = _probe_file(
            ffprobe,
            output,
            cancel_event=cancel_event,
            pause_event=pause_event,
            monitor=monitor,
        )
    except CalibrationInterrupted:
        raise
    except RuntimeError as exc:
        row["reason"] = f"the trial output could not be probed: {exc}"
        return row
    stream = probed["video"]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if (width, height) != (target_width, target_height):
        row["reason"] = (
            f"the trial is {width}x{height}, the planned geometry is "
            f"{target_width}x{target_height}"
        )
        return row

    frames = _frame_count(
        ffprobe,
        output,
        stream,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    row["frames"] = frames

    # The trial and the reference must cover the same interval; a frame
    # count that disagrees with the requested window means the two sides
    # are not comparable and the score is not reported.
    if frames is not None and expected_frames is not None:
        tolerance = max(1, int(round(expected_frames * _FRAME_TOLERANCE)))
        if abs(frames - expected_frames) > tolerance:
            row["reason"] = (
                f"frame count mismatch: the trial has {frames} frames, the "
                f"sample window holds about {expected_frames}"
            )
            return row

    if not metric_filter:
        row["reason"] = row["reason"] or "no quality metric is available"
        row["valid"] = True
        return row

    metric, score, reason = _score(
        ffmpeg,
        source,
        output,
        start=start,
        duration=duration,
        width=target_width,
        height=target_height,
        pix_fmt=str(stream.get("pix_fmt") or "") or None,
        filter_name=metric_filter,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
        progress_cb=trial_progress,
    )
    row["metric"] = metric
    row["score"] = round(score, 4) if score is not None else None
    if reason:
        row["reason"] = reason
    row["valid"] = True
    return row


def _expected_frames(
    source: Path,
    ffprobe: str,
    duration: float,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> int | None:
    """How many frames the requested source interval holds (approximately)."""
    try:
        probed = _probe_file(
            ffprobe,
            source,
            cancel_event=cancel_event,
            pause_event=pause_event,
            monitor=monitor,
        )
    except CalibrationInterrupted:
        raise
    except RuntimeError:
        return None
    rate = _frame_rate(probed["video"])
    if not rate or rate <= 0:
        return None
    return int(round(duration * rate))


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def format_calibration(result: dict, *, short: bool = False) -> str:
    """Render a calibration result as text."""
    rows = result.get("rows", [])
    status = result.get("status", "ok")
    name = Path(str(result.get("file") or "")).name
    backend = result.get("backend") or "-"
    lines = [f"calibrate {name} {backend}"]
    if status != "ok":
        lines.append(f"status: {status}: {result.get('reason') or 'no reason given'}")
        return "\n".join(lines)
    scope = result.get("scope", SCOPE)
    target = result.get("target_geometry") or {}
    lines.append(
        f"scope {scope} preset={result.get('preset')} encoder={result.get('encoder')} "
        f"target={target.get('width')}x{target.get('height')} "
        f"sample={result.get('sample_seconds')}s"
    )
    lines.append("quality     size   time  score")
    for row in rows:
        size = human_bytes(row["size_bytes"]) if row.get("size_bytes") else "fail"
        elapsed = f"{row['encode_seconds']:.1f}s" if row.get("encode_seconds") else "-"
        score = (
            f"{row['metric']}={row['score']:.3f}"
            if row.get("score") is not None
            else "n/a"
        )
        lines.append(f"{row['quality']:>7} {size:>8} {elapsed:>6}  {score}")
        if row.get("reason") and not row.get("valid"):
            lines.append(f"        {row['reason']}")
    if result.get("seeded"):
        seeded = result["seeded"]
        lines.append(
            f"seeded {seeded['preset']} {seeded['backend']} "
            f"factor={seeded['factor']:.2f}"
        )
    if result.get("seed_error"):
        lines.append(f"seed failed: {result['seed_error']}")
    if short:
        return "\n".join(line[:60] for line in lines[:12])
    return "\n".join(lines)
