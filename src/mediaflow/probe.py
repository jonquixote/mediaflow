"""Safe ffprobe access and media stream inspection.

All probing goes through :func:`ffprobe_json`, which builds an argv list (never
a shell), applies a timeout, and always passes
``-protocol_whitelist file,crypto,data`` (SAF-13).

The container allowlist is deliberately small.  Playlists and every other
demuxer are rejected before their metadata is used to plan a job.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .supervision import OperationState, run_process

DEFAULT_FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"
DEFAULT_TIMEOUT = 120
PROTOCOL_WHITELIST = "file,crypto,data"

#: ffprobe ``format.format_name`` tokens accepted by mediaflow.
#:
#: ffprobe reports several aliases for the same demuxer (for example
#: ``matroska,webm`` and ``mov,mp4,m4a,3gp,3g2,mj2``).  A format is accepted
#: only when *every* comma-separated token is in this set.  This rejects HLS
#: and other playlist demuxers (``hls``, ``applehttp``, ``m3u8``), concat,
#: image2, raw data and anything else not explicitly listed (SAF-13).
ALLOWED_FORMATS: frozenset[str] = frozenset(
    {
        # Matroska / WebM
        "matroska",
        "webm",
        # QuickTime / MP4 family
        "mov",
        "mp4",
        "m4a",
        "3gp",
        "3g2",
        "mj2",
        # AVI
        "avi",
        # MPEG transport stream and program stream
        "mpegts",
        "mpeg",
        # ASF / Windows Media
        "asf",
    }
)

#: Names that must never be accepted even if a future set is broadened.
FORBIDDEN_FORMATS: frozenset[str] = frozenset(
    {"hls", "applehttp", "m3u8", "m3u", "concat", "image2", "lavfi", "rtmp", "rtsp", "sdp", "udp"}
)

SIDECAR_SUFFIXES: frozenset[str] = frozenset({".srt", ".ass", ".vtt"})


class ProbeError(RuntimeError):
    """Base class for probe failures."""


class ProbeFailed(ProbeError):
    """ffprobe could not be run or exited unsuccessfully."""


class UnsupportedFormat(ProbeError):
    """The probed container is not in :data:`ALLOWED_FORMATS`."""


class ProbeInterrupted(ProbeError):
    """A supervised probe ended before it produced a complete result."""

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


def container_allowed(format_name: str | None) -> bool:
    """Return True only for the explicit container allowlist.

    ``format_name`` is compared case-insensitively.  Comma-separated ffprobe
    aliases are tolerated only when every token is allowed; an empty or
    missing name is rejected.
    """
    if not format_name or not isinstance(format_name, str):
        return False
    tokens = [token.strip().lower() for token in format_name.split(",") if token.strip()]
    if not tokens:
        return False
    if any(token in FORBIDDEN_FORMATS for token in tokens):
        return False
    return all(token in ALLOWED_FORMATS for token in tokens)


def _validate_regular_file(path: str | Path) -> Path:
    """Reject missing paths, directories, devices and symlinks (SAF-13)."""
    p = Path(path)
    try:
        st = p.lstat()
    except FileNotFoundError as exc:
        raise ProbeError(f"file not found: {p}") from exc
    except OSError as exc:
        raise ProbeError(f"cannot stat {p}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise ProbeError(f"refusing symlink: {p}")
    if not stat.S_ISREG(st.st_mode):
        raise ProbeError(f"not a regular file: {p}")
    return p


def ffprobe_json(
    path: str | Path,
    extra_args: Sequence[str],
    ffprobe: str | Path = DEFAULT_FFPROBE,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> dict[str, Any]:
    """Run ffprobe with a safe argv and return one JSON object.

    ``extra_args`` are appended after the mandatory protocol whitelist and
    before ``-of json``.  No shell is used.  Any non-zero exit, timeout,
    spawn failure or unparsable output raises :class:`ProbeFailed`.

    When supervision arguments are supplied, the probe is owned by the shared
    process supervisor.  Cancellation, protection interruption, deferral and
    timeout then retain their typed outcome in :class:`ProbeInterrupted`.
    The legacy call shape remains available to callers that do not need
    operation-level cancellation.
    """
    p = _validate_regular_file(path)
    if any(arg == "-protocol_whitelist" for arg in extra_args):
        raise ProbeError("extra_args must not override -protocol_whitelist")
    if "-of" in extra_args or "-print_format" in extra_args:
        raise ProbeError("extra_args must not set the output format")
    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        *[str(arg) for arg in extra_args],
        "-of",
        "json",
        str(p),
    ]
    supervised = cancel_event is not None or pause_event is not None or monitor is not None
    if supervised:
        from .supervision import SupervisionPolicy

        process = run_process(
            argv,
            policy=SupervisionPolicy(
                stall_timeout=None,
                startup_grace=min(30.0, max(0.0, float(timeout))),
                finalization_grace=min(30.0, max(0.0, float(timeout))),
                overall_timeout=timeout if timeout and timeout > 0 else None,
                monitor_interval=0.2,
                terminate_grace=5.0,
            ),
            cancel_event=cancel_event,
            pause_event=pause_event,
            monitor=monitor,
        )
        if process.state is not OperationState.SUCCEEDED:
            if process.state in {
                OperationState.CANCELLED,
                OperationState.INTERRUPTED,
                OperationState.DEFERRED,
                OperationState.TIMED_OUT,
                OperationState.STALLED,
            }:
                raise ProbeInterrupted(
                    process.reason or "ffprobe did not complete",
                    state=process.state,
                    reasons=process.reasons,
                    timeout_type=process.timeout_type,
                )
            detail = (process.stderr or "").strip().splitlines()
            message = detail[-1] if detail else (process.reason or "unknown error")
            raise ProbeFailed(f"ffprobe failed ({process.returncode}) for {p}: {message}")
        stdout = process.stdout
        stderr = process.stderr
        returncode = process.returncode
    else:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeFailed(f"ffprobe timed out after {timeout}s for {p}") from exc
        except OSError as exc:
            raise ProbeFailed(f"could not run ffprobe: {exc}") from exc
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    if returncode != 0:
        detail = (stderr or "").strip().splitlines()
        message = detail[-1] if detail else "unknown error"
        raise ProbeFailed(f"ffprobe failed ({returncode}) for {p}: {message}")
    try:
        payload = json.loads(stdout or "")
    except json.JSONDecodeError as exc:
        raise ProbeFailed(f"ffprobe returned invalid JSON for {p}") from exc
    if not isinstance(payload, dict):
        raise ProbeFailed(f"ffprobe returned a non-object for {p}")
    return payload


@dataclass(frozen=True)
class ProbeResult:
    """A validated ffprobe result plus convenience accessors.

    ``raw`` is the unmodified ffprobe JSON.  Stream dictionaries preserve their
    absolute input indexes, which the planner records verbatim.
    """

    path: Path
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))

    @property
    def format(self) -> Mapping[str, Any]:
        value = self.raw.get("format")
        return value if isinstance(value, Mapping) else {}

    @property
    def streams(self) -> list[dict[str, Any]]:
        value = self.raw.get("streams")
        return [dict(item) for item in value or [] if isinstance(item, Mapping)]

    @property
    def chapters(self) -> list[dict[str, Any]]:
        value = self.raw.get("chapters")
        return [dict(item) for item in value or [] if isinstance(item, Mapping)]

    def _streams_of(self, codec_type: str) -> list[dict[str, Any]]:
        return [stream for stream in self.streams if stream.get("codec_type") == codec_type]

    @property
    def video_streams(self) -> list[dict[str, Any]]:
        return self._streams_of("video")

    @property
    def audio_streams(self) -> list[dict[str, Any]]:
        return self._streams_of("audio")

    @property
    def subtitle_streams(self) -> list[dict[str, Any]]:
        return self._streams_of("subtitle")

    @property
    def duration(self) -> float:
        """Container duration in seconds, falling back to the longest stream."""
        value = _as_float(self.format.get("duration"))
        if value is not None and value >= 0:
            return value
        durations = [_as_float(stream.get("duration")) for stream in self.streams]
        return max((duration for duration in durations if duration is not None), default=0.0)

    @property
    def size(self) -> int:
        """Container size in bytes (format metadata, then ``stat``)."""
        value = _as_int(self.format.get("size"))
        if value is not None:
            return value
        try:
            return int(self.path.stat().st_size)
        except OSError:
            return 0

    # PLAN_SCHEMA spells this size_bytes; keep both names available.
    @property
    def size_bytes(self) -> int:
        return self.size

    @property
    def format_name(self) -> str:
        return str(self.format.get("format_name") or "")

    @property
    def main_video(self) -> dict[str, Any] | None:
        """The feature video stream.

        For a file with one video stream that stream is used.  With several
        streams, attached cover art is ignored when a real video stream exists,
        then the stream with the longest duration wins and stream size breaks a
        tie.  Ties keep the earliest stream (source order).
        """
        videos = self.video_streams
        if not videos:
            return None
        if len(videos) == 1:
            return videos[0]
        real = [stream for stream in videos if not _is_attached_pic(stream)]
        candidates = real or videos
        if len(candidates) == 1:
            return candidates[0]

        def key(stream: Mapping[str, Any]) -> tuple[float, int]:
            return (
                _as_float(stream.get("duration")) or 0.0,
                _as_int(stream.get("size")) or 0,
            )

        return max(candidates, key=key)


PLAYLIST_SUFFIXES: frozenset[str] = frozenset({".m3u8", ".m3u", ".strm", ".url", ".pls"})


def probe_media(
    path: str | Path,
    ffprobe: str | Path = DEFAULT_FFPROBE,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> ProbeResult:
    """Probe a regular file and enforce the container allowlist (SAF-13).

    Raises :class:`ProbeError` for missing/non-regular/symlink paths,
    :class:`ProbeFailed` for ffprobe failures and
    :class:`UnsupportedFormat` when the probed container is not allowed.
    Playlist suffixes are refused before ffprobe is invoked so a remote URL
    inside a ``.strm`` file can never be opened.
    """
    p = _validate_regular_file(path)
    if p.suffix.lower() in PLAYLIST_SUFFIXES:
        raise UnsupportedFormat(f"playlist files are not media: {p}")
    raw = ffprobe_json(
        p,
        ["-show_format", "-show_streams", "-show_chapters"],
        ffprobe=ffprobe,
        timeout=timeout,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    result = ProbeResult(path=p, raw=raw)
    if not container_allowed(result.format_name):
        raise UnsupportedFormat(
            f"unsupported container {result.format_name or '<unknown>'!r} for {p}"
        )
    return result


def video_packet_bytes(
    path: str | Path,
    stream_index: int,
    ffprobe: str | Path = DEFAULT_FFPROBE,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Sum packet payload bytes for one video stream (SIZE-1/SIZE-3 input).

    The result is the sum of ffprobe ``packet.size`` values whose
    ``stream_index`` matches.  Missing or malformed packet sizes are ignored;
    ffprobe itself is allowed to reject a broken file.
    """
    if int(stream_index) < 0:
        raise ProbeError("stream_index must be non-negative")
    payload = ffprobe_json(
        path,
        [
            "-select_streams",
            str(int(stream_index)),
            "-show_entries",
            "packet=stream_index,size",
        ],
        ffprobe=ffprobe,
        timeout=timeout,
    )
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return 0
    total = 0
    for packet in packets:
        if not isinstance(packet, Mapping):
            continue
        packet_index = _as_int(packet.get("stream_index"))
        if packet_index is not None and packet_index != int(stream_index):
            continue
        total += _as_int(packet.get("size")) or 0
    return total


def find_sidecars(source: str | Path) -> list[Path]:
    """Find same-stem ``.srt``/``.ass``/``.vtt`` files beside *source*.

    Language and flag variants such as ``Movie.en.srt`` and
    ``Movie.en.forced.ass`` match.  Only regular files are returned; symlinks
    are ignored rather than followed (SAF-13).
    """
    src = Path(source)
    parent = src.parent
    try:
        if not stat.S_ISREG(src.lstat().st_mode):
            return []
    except OSError:
        return []
    if not parent.is_dir():
        return []
    prefix = src.stem + "."
    found: list[Path] = []
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    for candidate in entries:
        if candidate.suffix.lower() not in SIDECAR_SUFFIXES:
            continue
        if not candidate.name.startswith(prefix):
            continue
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                continue
        except OSError:
            continue
        found.append(candidate)
    return sorted(found, key=lambda item: item.name.casefold())


def hdr_flags(stream: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a video stream's HDR metadata without running ffmpeg.

    Returns a dict with ``hdr``, ``hdr10``, ``hlg``, ``dolby_vision`` and
    ``format`` keys.  HDR10 is PQ (``smpte2084``) or mastering/light metadata;
    HLG is ``arib-std-b67``; Dolby Vision is detected from side data or a
    Dolby Vision codec tag.  SDR returns ``hdr=False`` and ``format='sdr'``.
    """
    transfer = str(stream.get("color_transfer") or "").lower()
    primaries = str(stream.get("color_primaries") or "").lower()
    codec_tag = str(stream.get("codec_tag_string") or "").lower()
    side_data = stream.get("side_data_list") or []
    side_types: list[str] = []
    if isinstance(side_data, Iterable):
        for item in side_data:
            if isinstance(item, Mapping):
                side_types.append(str(item.get("side_data_type") or "").casefold())
    side_blob = " ".join(side_types)

    dolby_vision = (
        "dovi" in side_blob
        or "dolby vision" in side_blob
        or codec_tag in {"dvh1", "dvhe", "dva1", "dvav"}
    )
    hlg = transfer in {"arib-std-b67", "arib_std_b67"}
    hdr10 = (
        transfer in {"smpte2084", "smpte-st-2084", "smpte_st_2084"}
        or "mastering display metadata" in side_blob
        or "content light level metadata" in side_blob
    )
    hdr = bool(hdr10 or hlg or dolby_vision)
    if dolby_vision:
        kind = "dolby_vision"
    elif hlg:
        kind = "hlg"
    elif hdr10:
        kind = "hdr10"
    else:
        kind = "sdr"
    return {
        "hdr": hdr,
        "hdr10": hdr10,
        "hlg": hlg,
        "dolby_vision": dolby_vision,
        "transfer": transfer or None,
        "primaries": primaries or None,
        "format": kind,
    }


# Matches p010, p10le, yuv420p10le, gbrp12le and explicit 10le/12be suffixes.
# It intentionally does not match NV12, which is an 8-bit format.
_10BIT_PIX_RE = re.compile(r"(?:p0?(?:9|10|12|14|16)(?:le|be)?(?:$|[^0-9])|(?:9|10|12|14|16)(?:le|be)$)")


def is_10bit(stream: Mapping[str, Any]) -> bool:
    """True when the video stream carries more than 8 bits per component."""
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = _as_int(stream.get(key))
        if value is not None and value >= 10:
            return True
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    if not pix_fmt:
        return False
    if _10BIT_PIX_RE.search(pix_fmt):
        return True
    return any(token in pix_fmt for token in ("gray10", "gray12", "gray16"))


def is_interlaced(stream: Mapping[str, Any]) -> bool:
    """True for field orders that carry interlaced fields."""
    field_order = str(stream.get("field_order") or "").strip().lower()
    return field_order in {"tt", "bb", "tb", "bt", "interlaced", "interleaved"}


def detect_review_conditions(probe: ProbeResult) -> list[str]:
    """List plan-time REVIEW reasons visible in probe metadata.

    This never runs ffmpeg and is only a convenience for callers that want to
    warn before planning.  The authoritative decisions live in
    :func:`mediaflow.plan.build_plan`.
    """
    stream = probe.main_video
    if stream is None:
        return []
    reasons: list[str] = []
    if hdr_flags(stream)["hdr"]:
        reasons.append("hdr")
    if is_10bit(stream):
        reasons.append("10bit")
    if is_interlaced(stream):
        reasons.append("interlaced")
    return reasons


def _is_attached_pic(stream: Mapping[str, Any]) -> bool:
    disposition = stream.get("disposition")
    return bool(isinstance(disposition, Mapping) and disposition.get("attached_pic"))


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "ALLOWED_FORMATS",
    "DEFAULT_FFPROBE",
    "DEFAULT_TIMEOUT",
    "FORBIDDEN_FORMATS",
    "PLAYLIST_SUFFIXES",
    "PROTOCOL_WHITELIST",
    "ProbeError",
    "ProbeFailed",
    "ProbeResult",
    "SIDECAR_SUFFIXES",
    "UnsupportedFormat",
    "container_allowed",
    "detect_review_conditions",
    "ffprobe_json",
    "find_sidecars",
    "hdr_flags",
    "is_10bit",
    "is_interlaced",
    "probe_media",
    "video_packet_bytes",
]
