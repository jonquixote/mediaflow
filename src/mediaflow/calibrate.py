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
from .plan import PlanError, ReviewRequired, build_plan
from .probe import ProbeError, probe_media
from .safety import FileSafety
from .util import human_bytes

#: What the measurement actually covers.  The trial encodes the video
#: stream only; audio and subtitle handling are not part of it.
SCOPE = "video-encode-only"

#: Frame-count tolerance between the trial and the reference interval.  One
#: frame absorbs a rounding difference, 2 % absorbs a container's frame-rate
#: rounding on long samples.
_FRAME_TOLERANCE = 0.02


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _capabilities(config: Any) -> dict[str, Any]:
    """The capability report the planner uses to pick a backend."""
    path = getattr(config, "capabilities_path", None)
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_filter(ffmpeg: str, name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return name in (proc.stdout or "")


def _probe_file(ffprobe: str, path: Path) -> dict[str, Any]:
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
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-300:]
        raise RuntimeError(f"ffprobe failed for {path.name}: {detail}")
    try:
        data = json.loads(proc.stdout or "{}")
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


def _frame_count(ffprobe: str, path: Path, stream: Mapping[str, Any]) -> int | None:
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
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
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
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "n/a", None, "scoring timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return "n/a", None, f"scoring could not run: {exc}"
    text = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] if proc.stderr else []
        reason = f"scoring failed (rc={proc.returncode})"
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


def calibrate(
    file: Path,
    config: Any,
    *,
    seconds: int = 90,
    preset: Any = None,
    store: Any = None,
    qualities: list[int] | None = None,
    progress: Any = None,
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
    result: dict[str, Any] = {
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
        "libvmaf": _has_filter(ffmpeg, "libvmaf"),
        "metric": "n/a",
        "seeded": None,
    }

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

    try:
        probe = probe_media(source, ffprobe=ffprobe)
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
    libvmaf = _has_filter(ffmpeg, "libvmaf")
    result["libvmaf"] = libvmaf
    if libvmaf:
        metric_filter = "libvmaf"
    elif _has_filter(ffmpeg, "ssim"):
        metric_filter = "ssim"
    else:
        metric_filter = ""
    result["metric"] = metric_filter or "n/a"
    expected_frames = _expected_frames(source, ffprobe, clip)

    scratch_root = _scratch_root(config)
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="calib-", dir=str(scratch_root)))
    sample = SampleSpec(start=start, duration=clip)

    rows: list[dict[str, Any]] = []
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
            )
            rows.append(row)
            if progress:
                progress(row)
    finally:
        _remove_scratch(config, scratch)

    result["rows"] = rows
    result["status"] = "ok"

    valid = [row for row in rows if row.get("valid") and row.get("speed_factor")]
    if store is not None and result["preset"] and valid and backend:
        chosen = min(valid, key=lambda row: abs(int(row["quality"]) - base_quality))
        source_height = int((video.get("height") or 0))
        try:
            store.speed_seed(
                result["preset"], backend, source_height, float(chosen["speed_factor"])
            )
            result["seeded"] = {
                "preset": result["preset"],
                "backend": backend,
                "height": source_height,
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
    began = time.monotonic()
    try:
        encoded = encoder.encode(
            trial_plan,
            output=output,
            sample=sample,
            video_only=True,
        )
    except Exception as exc:
        row["reason"] = f"the trial could not run: {exc}"[:300]
        return row
    elapsed = max(0.001, time.monotonic() - began)
    row["command"] = list(encoded.command)
    row["encode_seconds"] = round(elapsed, 3)
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
        probed = _probe_file(ffprobe, output)
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

    frames = _frame_count(ffprobe, output, stream)
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
    )
    row["metric"] = metric
    row["score"] = round(score, 4) if score is not None else None
    if reason:
        row["reason"] = reason
    row["valid"] = True
    return row


def _expected_frames(source: Path, ffprobe: str, duration: float) -> int | None:
    """How many frames the requested source interval holds (approximately)."""
    try:
        probed = _probe_file(ffprobe, source)
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
