"""Aggregate library scanning and reporting.

MP4-7: ``movies/`` may legitimately contain a mix of MP4 and MKV outputs.  The
library report lists that container mix (and the codec/resolution mix) without
ever recording or logging titles, filenames or per-file paths.  Scanning is
strictly read-only: only regular, non-hidden files below the supplied movies
root are considered, symlinks are ignored, and ffprobe results are cached in
the existing ``library_cache`` table (no schema change).

Only aggregate counts and byte totals leave this module by default, so the
JSON form is safe to hand to ``mediaflow library --json``.
"""

from __future__ import annotations

import inspect
import logging
import math
import os
import stat
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .probe import DEFAULT_FFPROBE, ProbeResult, probe_media
from .util import human_bytes

log = logging.getLogger(__name__)

#: Extensions treated as video media.  MP4 and MKV are the two normal outputs;
#: anything else is still counted as ``other`` when it is a regular file.
MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".mpg",
        ".mpeg",
        ".m2ts",
        ".mts",
        ".ts",
        ".wmv",
        ".flv",
        ".ogv",
        ".3gp",
        ".vob",
    }
)

CONTAINERS: tuple[str, ...] = ("mp4", "mkv", "other")
RESOLUTION_BUCKETS: tuple[str, ...] = ("<=720", "<=1080", ">1080", "unknown")
CONTAINER_BY_SUFFIX = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mp4",
    ".mkv": "mkv",
    ".webm": "mkv",
}

_CODEC_ALIASES = {
    "avc1": "h264",
    "avc": "h264",
    "h264": "h264",
    "h265": "hevc",
    "hev1": "hevc",
    "hvc1": "hevc",
    "hevc": "hevc",
}


def _stat_empty() -> dict[str, dict[str, int]]:
    return {name: {"count": 0, "bytes": 0} for name in CONTAINERS}


def _resolution_empty() -> dict[str, dict[str, int]]:
    return {name: {"count": 0, "bytes": 0} for name in RESOLUTION_BUCKETS}


def _root_is_directory(root: Path) -> bool:
    try:
        info = root.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def scan_library(movies: str | Path) -> Iterator[Path]:
    """Yield regular media files below *movies*, in stable name order.

    Missing roots yield nothing.  Hidden files/directories (including
    ``.mediaflow-*`` markers), symlinks and non-media extensions are skipped.
    The generator never follows a directory symlink.
    """
    root = Path(movies)
    if not _root_is_directory(root):
        return
    yield from _walk(root)


def _walk(directory: Path) -> Iterator[Path]:
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if Path(name).suffix.lower() in MEDIA_EXTENSIONS:
            yield Path(entry.path)


def _container_for(path: Path) -> str:
    return CONTAINER_BY_SUFFIX.get(path.suffix.lower(), "other")


def _resolution_bucket(height: int | None) -> str:
    if height is None or height <= 0:
        return "unknown"
    if height <= 720:
        return "<=720"
    if height <= 1080:
        return "<=1080"
    return ">1080"


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probe_mapping(result: Any) -> dict[str, Any]:
    """Normalise ProbeResult / ffprobe JSON / test doubles into a mapping."""
    if isinstance(result, ProbeResult):
        return dict(result.raw)
    if isinstance(result, Mapping):
        return dict(result)
    raw = getattr(result, "raw", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    raise TypeError("probe did not return a mapping or ProbeResult")


def _main_video(streams: Iterable[Any]) -> Mapping[str, Any] | None:
    videos = [
        stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
    ]
    if not videos:
        return None
    real = [
        stream
        for stream in videos
        if not (
            isinstance(stream.get("disposition"), Mapping)
            and stream["disposition"].get("attached_pic")
        )
    ]
    candidates = real or videos
    if len(candidates) == 1:
        return candidates[0]

    def score(stream: Mapping[str, Any]) -> tuple[float, int]:
        return (
            _as_float(stream.get("duration")) or 0.0,
            _as_int(stream.get("size")) or 0,
        )

    return max(candidates, key=score)


def _summarise_probe(payload: Mapping[str, Any]) -> tuple[str, int | None]:
    """Return ``(codec, height)`` from raw ffprobe data or a cached summary."""
    streams = payload.get("streams")
    video: Mapping[str, Any] | None = None
    if isinstance(streams, list):
        video = _main_video(streams)

    if video is not None:
        codec_value = video.get("codec_name") or video.get("codec")
        height = _as_int(video.get("height"))
    else:
        # Tolerate a pre-computed cache payload from an older version / test.
        codec_value = payload.get("codec_name") or payload.get("codec") or payload.get("video_codec")
        height = _as_int(payload.get("height"))

    codec = str(codec_value or "").strip().lower()
    codec = _CODEC_ALIASES.get(codec, codec) or "unknown"
    return codec, height


def _cache_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _probe_file(
    path: Path,
    ffprobe: Any,
    probe: Callable[[Path], Any] | None,
) -> Any:
    if probe is not None:
        return probe(path)
    if callable(ffprobe):
        return ffprobe(path)
    try:
        parameters = inspect.signature(probe_media).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return probe_media(path, ffprobe=ffprobe)
    accepts_ffprobe = "ffprobe" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_ffprobe:
        return probe_media(path, ffprobe=ffprobe)
    return probe_media(path)


def _add(bucket: dict[str, dict[str, int]], name: str, size: int) -> None:
    entry = bucket.setdefault(name, {"count": 0, "bytes": 0})
    entry["count"] += 1
    entry["bytes"] += size


def _stat_count(entry: Any) -> int:
    if isinstance(entry, Mapping):
        return int(entry.get("count") or 0)
    return int(entry or 0)


def _stat_bytes(entry: Any) -> int:
    if isinstance(entry, Mapping):
        return int(entry.get("bytes") or 0)
    return 0


def library_report(
    movies: str | Path,
    store: Any = None,
    ffprobe: Any = DEFAULT_FFPROBE,
    cache: bool = True,
    *,
    probe: Callable[[Path], Any] | None = None,
) -> dict:
    """Scan *movies* and return an aggregate, JSON-serialisable report.

    Each regular media file is counted once.  New files are probed with
    :func:`mediaflow.probe.probe_media`; successful probe JSON is cached by
    real path, size and mtime in *store*.  Probe failures increment the
    ``unreadable`` count and classify the file as codec/resolution ``unknown``
    instead of aborting the scan.  A missing movies directory is an empty
    library, not an error.

    MP4-7: ``containers`` reports both MP4 and MKV so the documented mixed
    container policy is visible to operators.
    """
    root = Path(movies)
    containers = _stat_empty()
    codecs: dict[str, dict[str, int]] = {}
    resolutions = _resolution_empty()

    total_count = 0
    total_bytes = 0
    unreadable = 0
    probed = 0
    cached = 0

    for path in scan_library(root):
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        size = int(info.st_size)
        mtime = float(info.st_mtime)
        container = _container_for(path)
        total_count += 1
        total_bytes += size
        _add(containers, container, size)

        payload: Mapping[str, Any] | None = None
        if cache and store is not None:
            try:
                candidate = store.get_library_cache(_cache_key(path), size, mtime)
            except Exception as exc:  # noqa: BLE001 - a cache fault must not stop a scan
                log.warning("library cache read failed error=%s", type(exc).__name__)
                candidate = None
            if isinstance(candidate, Mapping):
                payload = candidate
                cached += 1

        if payload is None:
            probed += 1
            try:
                raw = _probe_mapping(_probe_file(path, ffprobe, probe))
            except Exception as exc:  # noqa: BLE001 - one bad file is counted
                unreadable += 1
                log.debug("library probe failed error=%s", type(exc).__name__)
                codec, height = "unknown", None
            else:
                payload = raw
                codec, height = _summarise_probe(raw)
                if cache and store is not None:
                    try:
                        store.put_library_cache(_cache_key(path), size, mtime, raw)
                    except Exception as cache_exc:  # noqa: BLE001
                        log.warning(
                            "library cache write failed error=%s",
                            type(cache_exc).__name__,
                        )
        else:
            codec, height = _summarise_probe(payload)

        _add(codecs, codec, size)
        _add(resolutions, _resolution_bucket(height), size)

    return {
        "root": str(root),
        "count": total_count,
        "total_count": total_count,
        "total_files": total_count,
        "bytes": total_bytes,
        "total_bytes": total_bytes,
        "total_size": total_bytes,
        "totals": {"count": total_count, "bytes": total_bytes},
        "total": {"count": total_count, "bytes": total_bytes},
        "containers": containers,
        "container_counts": {name: containers[name]["count"] for name in CONTAINERS},
        "container_mix": {name: containers[name]["count"] for name in CONTAINERS},
        "codecs": codecs,
        "video_codecs": codecs,
        "codec_counts": {name: stats["count"] for name, stats in codecs.items()},
        "resolutions": resolutions,
        "resolution_buckets": resolutions,
        "resolution_counts": {
            name: resolutions[name]["count"] for name in RESOLUTION_BUCKETS
        },
        "unreadable": unreadable,
        "probed": probed,
        "cached": cached,
        "cache_hits": cached,
    }


def _numbered(entry: Any) -> str:
    return f"{_stat_count(entry)} files, {human_bytes(_stat_bytes(entry))}"


def _clip(line: str, width: int = 60) -> str:
    if len(line) <= width:
        return line
    return line[: width - 1] + "\u2026"


def _short_line(label: str, entries: Mapping[str, Any], order: Iterable[str] | None = None,
                limit: int | None = None) -> str:
    names = list(order) if order is not None else sorted(
        entries, key=lambda name: (-_stat_count(entries[name]), str(name))
    )
    parts = [f"{name} {_stat_count(entries.get(name))}" for name in names]
    if limit is not None and len(parts) > limit:
        parts = parts[:limit] + ["\u2026"]
    return f"{label}: " + " | ".join(parts)


def format_report(report: Mapping[str, Any], short: bool = False) -> str:
    """Render a plain-text report.

    MP4-7: the normal form spells out the MP4/MKV container mix and sizes.
    ``short=True`` is phone-width: at most 12 lines and at most 60 columns per
    line.  The normal form adds per-container sizes and the probe-cache counts.
    """
    count = int(report.get("total_count", report.get("count", 0)) or 0)
    size = int(report.get("total_bytes", report.get("bytes", 0)) or 0)
    containers = report.get("containers") if isinstance(report.get("containers"), Mapping) else {}
    codecs = report.get("codecs") if isinstance(report.get("codecs"), Mapping) else {}
    resolutions = (
        report.get("resolutions")
        if isinstance(report.get("resolutions"), Mapping)
        else {}
    )
    unreadable = int(report.get("unreadable", 0) or 0)

    if short:
        lines = [
            f"Library: {count} files, {human_bytes(size)}",
            _clip(_short_line("Containers (MP4/MKV)", containers, order=CONTAINERS)),
            _clip(_short_line("Codecs", codecs, limit=5)),
            _clip(_short_line("Resolution", resolutions, order=RESOLUTION_BUCKETS)),
            _clip(f"Unreadable: {unreadable}"),
        ]
        return "\n".join(_clip(line) for line in lines[:12])

    lines = [f"Library: {count} files, {human_bytes(size)}", "Containers (MP4/MKV):"]
    for name in CONTAINERS:
        lines.append(f"  {name}: {_numbered(containers.get(name, {}))}")
    lines.append("Video codecs:")
    if codecs:
        for name in sorted(codecs, key=lambda key: (-_stat_count(codecs[key]), str(key))):
            lines.append(f"  {name}: {_numbered(codecs[name])}")
    else:
        lines.append("  none")
    lines.append("Resolution:")
    for name in RESOLUTION_BUCKETS:
        lines.append(f"  {name}: {_numbered(resolutions.get(name, {}))}")
    lines.append(f"Unreadable: {unreadable}")
    cached = int(report.get("cached", report.get("cache_hits", 0)) or 0)
    probed = int(report.get("probed", 0) or 0)
    if cached or probed:
        lines.append(f"Probe cache: {cached} hit(s), {probed} probe(s)")
    return "\n".join(lines)


__all__ = [
    "CONTAINERS",
    "MEDIA_EXTENSIONS",
    "RESOLUTION_BUCKETS",
    "format_report",
    "library_report",
    "scan_library",
]
