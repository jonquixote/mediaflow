"""Quality/runtime calibration on a short middle sample."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .util import human_bytes


def _ffprobe_duration(ffprobe: str, path: Path) -> float:
    argv = [ffprobe, "-v", "error", "-protocol_whitelist", "file,crypto,data",
            "-show_entries", "format=duration", "-of", "json", str(path)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe could not read sample")
    return float(json.loads(proc.stdout)["format"]["duration"])


def _has_filter(ffmpeg: str, name: str) -> bool:
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                              text=True, timeout=30)
        return name in proc.stdout
    except Exception:
        return False


def _score(ffmpeg: str, reference: Path, encoded: Path) -> tuple[str, float | None]:
    if _has_filter(ffmpeg, "libvmaf"):
        filt = "libvmaf"
        pattern = "VMAF score:"
        label = "vmaf"
    elif _has_filter(ffmpeg, "ssim"):
        filt = "ssim"
        pattern = "SSIM"
        label = "ssim"
    else:
        return "n/a", None
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-v", "info",
            "-protocol_whitelist", "file,crypto,data",
            "-i", str(encoded), "-i", str(reference), "-lavfi", filt, "-f", "null", "-"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except Exception:
        return "n/a", None
    text = (proc.stderr or "") + (proc.stdout or "")
    if "VMAF score:" in text:
        try:
            return "vmaf", float(text.split("VMAF score:", 1)[1].split()[0])
        except Exception:
            return "vmaf", None
    if "All:" in text:
        try:
            return "ssim", float(text.split("All:", 1)[1].split()[0])
        except Exception:
            return "ssim", None
    return label, None


def _choose_backend(ffmpeg: str, preset: Any, config: Any | None = None) -> str:
    order = getattr(getattr(preset, "video", None), "backend_order", None) or []
    caps: dict = {}
    try:
        path = Path(getattr(config, "capabilities_path"))
        caps = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    for backend in order:
        if backend in {"qsv", "vaapi"} and caps.get(backend, {}).get("hevc8"):
            return backend
    if order:
        return "software"
    return "qsv" if Path("/dev/dri/renderD128").exists() else "software"


def _encode(ffmpeg: str, source: Path, dest: Path, *, start: float, duration: float,
            quality: int, backend: str, width: int | None = None, height: int | None = None) -> None:
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-protocol_whitelist", "file,crypto,data",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-an", "-sn", "-dn"]
    filters = []
    if backend == "qsv":
        argv += ["-c:v", "hevc_qsv", "-global_quality", str(quality), "-look_ahead", "1"]
        if width and height:
            filters.append(f"vpp_qsv=w={width}:h={height}")
    elif backend == "vaapi":
        argv += ["-vaapi_device", "/dev/dri/renderD128"]
        if width and height:
            filters.append(f"scale={width}:{height}")
        filters.append("format=nv12")
        argv += ["-vf", ",".join(filters), "-c:v", "hevc_vaapi", "-qp", str(quality)]
        filters = []
    else:
        argv += ["-c:v", "libx265", "-preset", "slow", "-crf", str(quality)]
        if width and height:
            filters.append(f"scale={width}:{height}")
    if filters:
        argv += ["-vf", ",".join(filters)]
    argv += ["-tag:v", "hvc1", "-f", "mp4", str(dest)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-500:] or "ffmpeg failed")


def calibrate(file: Path, config: Any, *, seconds: int = 90, preset: Any = None,
              store: Any = None, qualities: list[int] | None = None,
              progress: Any = None) -> dict:
    """Encode a middle sample at several qualities and return a table."""
    file = Path(file)
    ffmpeg = str(getattr(config, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg"))
    ffprobe = str(getattr(config, "ffprobe", "/usr/lib/jellyfin-ffmpeg/ffprobe"))
    duration = _ffprobe_duration(ffprobe, file)
    clip = max(1.0, min(float(seconds), duration, duration))
    if clip >= duration:
        start = 0.0
    else:
        start = max(0.0, (duration - clip) / 2)
    base_quality = int(getattr(getattr(preset, "video", None), "quality", 24) or 24)
    if qualities is None:
        qualities = sorted({max(1, min(50, base_quality + d)) for d in (-6, -3, 0, 4, 8)})
    backend = _choose_backend(ffmpeg, preset, config)
    rows = []
    with tempfile.TemporaryDirectory(prefix="mediaflow-calibrate-") as td:
        tdir = Path(td)
        reference = tdir / "reference.mp4"
        # Remux the exact sample losslessly as a reference where possible; the
        # source itself is also valid, but this normalises timestamps.
        subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
                        "-protocol_whitelist", "file,crypto,data",
                        "-ss", f"{start:.3f}", "-t", f"{clip:.3f}", "-i", str(file),
                        "-map", "0:v:0", "-c", "copy", str(reference)],
                       capture_output=True, timeout=180)
        ref = reference if reference.exists() else file
        for quality in qualities:
            out = tdir / f"q{quality}.mp4"
            began = time.monotonic()
            try:
                _encode(ffmpeg, file, out, start=start, duration=clip, quality=quality, backend=backend)
                elapsed = max(0.001, time.monotonic() - began)
                metric, score = _score(ffmpeg, ref, out)
                row = {
                    "quality": quality,
                    "size_bytes": out.stat().st_size if out.exists() else 0,
                    "encode_seconds": elapsed,
                    "score": round(score, 4) if score is not None else None,
                    "metric": metric,
                    "speed_factor": clip / elapsed,
                }
            except Exception as exc:
                row = {"quality": quality, "size_bytes": None, "encode_seconds": None,
                       "score": None, "metric": "n/a", "speed_factor": None,
                       "error": str(exc)[:300]}
            rows.append(row)
            if progress:
                progress(row)
    successful = [r for r in rows if r.get("speed_factor")]
    result = {
        "file": str(file), "duration": duration, "sample_start": start,
        "sample_seconds": clip, "backend": backend,
        "preset": getattr(preset, "id", None), "rows": rows,
        "libvmaf": _has_filter(ffmpeg, "libvmaf"),
    }
    if store is not None and preset is not None and successful:
        chosen = min(successful, key=lambda r: abs(r["quality"] - base_quality))
        try:
            from .probe import ffprobe_json
            # resolution bucket comes from the source video height
            probe = ffprobe_json(file, ["-show_streams", "-show_format"],
                                 ffprobe=ffprobe)
            height = next((int(s.get("height") or 0) for s in probe.get("streams", [])
                           if s.get("codec_type") == "video"), 0)
            store.speed_seed(preset.id, backend, height, float(chosen["speed_factor"]))
            result["seeded"] = {"preset": preset.id, "backend": backend,
                                "factor": chosen["speed_factor"], "quality": chosen["quality"]}
        except Exception as exc:
            result["seed_error"] = str(exc)[:200]
    return result


def format_calibration(result: dict, *, short: bool = False) -> str:
    rows = result.get("rows", [])
    lines = [f"calibrate {Path(result['file']).name} {result['backend']}"]
    lines.append("quality     size   time  score")
    for row in rows:
        size = human_bytes(row["size_bytes"]) if row.get("size_bytes") else "fail"
        elapsed = f"{row['encode_seconds']:.1f}s" if row.get("encode_seconds") else "-"
        score = f"{row['metric']}={row['score']:.3f}" if row.get("score") is not None else "n/a"
        lines.append(f"{row['quality']:>7} {size:>8} {elapsed:>6}  {score}")
    if result.get("seeded"):
        s = result["seeded"]
        lines.append(f"seeded {s['preset']} {s['backend']} factor={s['factor']:.2f}")
    if short:
        return "\n".join(line[:60] for line in lines[:12])
    return "\n".join(lines)
