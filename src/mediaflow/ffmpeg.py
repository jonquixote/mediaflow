"""The ffmpeg boundary: argv construction and the encoder interface.

Only explicit argument lists are used here; nothing ever goes through a
shell.  Every command carries ``-protocol_whitelist file,crypto,data`` and
maps each selected input stream explicitly with ``-map 0:<index>``.

Requirement coverage in this module: [MP4-1], [MP4-9], [MP4-12], [SAF-13],
[ENC-1].
"""

from __future__ import annotations

import logging
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

log = logging.getLogger(__name__)

FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"
DEFAULT_FFMPEG = FFMPEG
DEFAULT_FFPROBE = FFPROBE
PROTOCOL_WHITELIST = "file,crypto,data"
VAAPI_DEVICE = "/dev/dri/renderD128"

DEFAULT_STDERR_LIMIT = 64 * 1024

BITMAP_SUBTITLE_CODECS = frozenset(
    {
        "hdmv_pgs_subtitle",
        "pgs",
        "dvd_subtitle",
        "dvdsub",
        "vobsub",
        "dvb_subtitle",
        "dvb_teletext",
        "xsub",
    }
)

ProgressCallback = Callable[[dict[str, Any]], None]


class PlanError(ValueError):
    """The plan mapping is missing a field or contains an unsupported value."""


# ---------------------------------------------------------------------------
# plan helpers


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


def _require_even(obj: Mapping, key: str, where: str) -> int:
    value = _require_int(obj, key, where)
    if value <= 0:
        raise PlanError(f"plan field {where}.{key} must be positive: {value}")
    if value % 2:
        raise PlanError(f"plan field {where}.{key} must be even: {value}")
    return value


def _number(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise PlanError(f"plan field {what} must be a number: {value!r}") from None


def _fmt_number(value: Any, what: str) -> str:
    number = _number(value, what)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _str(value: Any, what: str) -> str:
    if value is None:
        raise PlanError(f"plan field {what} must not be null")
    text = str(value)
    if not text:
        raise PlanError(f"plan field {what} must not be empty")
    return text


def _codec_family(name: str) -> str:
    lowered = name.lower()
    if "hevc" in lowered or "265" in lowered:
        return "hevc"
    if "avc" in lowered or "264" in lowered:
        return "h264"
    return lowered


def codec_family(name: str) -> str:
    """Public codec-family helper shared by the planner and the verifier.

    ``hevc_qsv``/``hevc_vaapi``/``libx265``/``hevc`` all map to ``hevc``;
    ``h264``/``libx264`` map to ``h264``.  Anything else is returned in
    lower case so unknown codecs stay comparable.
    """
    return _codec_family(str(name or ""))


def expected_output_codec(plan: Mapping) -> str:
    """The codec family the plan says the output video stream must carry.

    For an encode the planned encoder decides (``hevc_qsv`` -> ``hevc``).
    For a remux the copied source codec decides, which is what the planner
    records in ``video.codec``; the generic preset label is never used.
    """
    if not isinstance(plan, Mapping):
        raise PlanError(f"plan must be a mapping, got {type(plan).__name__}")
    video = plan.get("video")
    video = video if isinstance(video, Mapping) else {}
    action = str(plan.get("action") or "").strip().lower()
    if action == "encode":
        encoder = str(video.get("encoder") or "").strip()
        if encoder:
            return _codec_family(encoder)
    return _codec_family(str(video.get("codec") or "").strip())


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


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ---------------------------------------------------------------------------
# command construction


def output_partial_path(output: Path | str) -> Path:
    """Return the working name: the final output with ``.partial`` appended."""
    return Path(f"{Path(output)}.partial")


@dataclass(frozen=True)
class SampleSpec:
    """An explicit input sampling window.

    ``start`` is the offset in seconds from the beginning of the input and
    ``duration`` is the length of the sampled interval.  Both must be finite
    numbers, ``start`` must not be negative and ``duration`` must be
    positive; invalid values raise :class:`PlanError` instead of producing a
    broken ffmpeg command.

    The same spec is used for the candidate trial and for the reference
    comparison, so both cover the *same intended frame interval*.  Ordinary
    production jobs never pass a sample and are unaffected.
    """

    start: float
    duration: float

    def __post_init__(self) -> None:
        try:
            start = float(self.start)
            duration = float(self.duration)
        except (TypeError, ValueError) as exc:
            raise PlanError("sample start and duration must be numbers") from exc
        if not math.isfinite(start) or start < 0:
            raise PlanError(
                f"sample start must be a finite number >= 0: {self.start!r}"
            )
        if not math.isfinite(duration) or duration <= 0:
            raise PlanError(
                f"sample duration must be a finite number > 0: {self.duration!r}"
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "duration", duration)

    def as_args(self) -> list[str]:
        """The ffmpeg input options for this window."""
        return ["-ss", f"{self.start:.3f}", "-t", f"{self.duration:.3f}"]


def build_command(
    plan: Mapping,
    output: Path | str,
    *,
    ffmpeg: str = FFMPEG,
    software: bool = False,
    sample: SampleSpec | None = None,
    video_only: bool = False,
) -> list[str]:
    """Build one explicit ffmpeg argv for a plan.

    ``output`` is normally the partial working path; the muxer is always
    selected explicitly with ``-f`` so a ``.partial`` suffix is harmless.

    ``software=True`` is the explicit phase-1 fallback: it overrides the
    plan's hardware backend and uses libx265 with software scaling.

    ``sample`` restricts the run to one explicit input interval
    (``-ss``/``-t`` before ``-i``).  It exists for calibration trials; a
    production job never passes it and its command is unchanged.

    ``video_only`` maps the video stream alone and disables audio, subtitle
    and data output.  It is used for the encode-only calibration trial and
    never changes an ordinary production command.

    Raises :class:`ValueError` (``PlanError``) for missing or unknown plan
    fields rather than leaking ``KeyError``.
    """
    if not isinstance(plan, Mapping):
        raise PlanError(f"plan must be a mapping, got {type(plan).__name__}")
    if plan.get("schema") not in (None, 1, "1"):
        raise PlanError(f"unsupported plan schema: {plan.get('schema')!r}")
    if sample is not None and not isinstance(sample, SampleSpec):
        raise PlanError("sample must be a SampleSpec")

    container = _container(plan)
    action = _str(_require(plan, "action", "plan"), "action").strip().lower()
    if action not in {"encode", "remux"}:
        raise PlanError(f"unsupported plan action: {action!r}")

    source = _mapping(_require(plan, "source", "plan"), "plan.source")
    source_path = Path(_str(_require(source, "path", "plan.source"), "source.path"))
    if not source_path.is_absolute():
        raise PlanError(f"plan.source.path must be absolute: {source_path}")

    video = _mapping(_require(plan, "video", "plan"), "plan.video")
    video_index = _require_int(video, "index", "video")
    target_width = _require_even(video, "target_width", "video")
    target_height = _require_even(video, "target_height", "video")
    source_width = video.get("width")
    source_height = video.get("height")
    scaled = video.get("scaled")
    if scaled is None:
        if source_width is not None and source_height is not None:
            scaled = (target_width, target_height) != (int(source_width), int(source_height))
        else:
            scaled = False
    scaled = _bool(scaled)
    bit_depth = int(video.get("bit_depth") or 8)

    audio = _plan_streams(plan, "audio")
    subtitles = _plan_streams(plan, "subtitles")
    if video_only:
        # An encode-only trial: the plan's audio/subtitle contract is not
        # exercised at all, so it is not mapped and not configured.
        audio = []
        subtitles = []

    # -- backend / output video codec -----------------------------------
    backend = str(video.get("backend") or "").strip().lower()
    if software:
        backend = "software"
    if backend in {"sw", "cpu"}:
        backend = "software"
    if action == "encode" and backend not in {"qsv", "vaapi", "software"}:
        if not backend:
            raise PlanError("plan.video.backend is required for an encode")
        raise PlanError(f"unsupported video backend: {backend!r}")

    encoder = str(video.get("encoder") or "")
    if action == "encode":
        if backend == "qsv":
            encoder = encoder or "hevc_qsv"
            if not encoder.endswith("_qsv"):
                raise PlanError(f"QSV encoder must end with _qsv, got {encoder!r}")
        elif backend == "vaapi":
            if not encoder.endswith("_vaapi"):
                encoder = "hevc_vaapi"
        else:  # software: v1's only software encoder is libx265
            encoder = "libx265"
        output_codec = _codec_family(encoder)
    else:
        source_codec = _str(_require(video, "codec", "video"), "video.codec")
        output_codec = _codec_family(source_codec)
        if container == "mp4" and output_codec not in {"h264", "hevc"}:
            raise PlanError(
                "MP4 remux requires H.264 or HEVC video; "
                f"got {source_codec!r} (planner must review remux-incompatible-video)"
            )

    command: list[str] = [str(ffmpeg), "-hide_banner", "-nostdin"]

    if action == "encode" and backend == "qsv":
        command += ["-hwaccel", "qsv"]
    elif action == "encode" and backend == "vaapi":
        command += ["-vaapi_device", VAAPI_DEVICE]

    command += ["-protocol_whitelist", PROTOCOL_WHITELIST]
    if sample is not None:
        # Input-side seek and duration: the trial covers exactly the
        # requested interval, decoded (not keyframe-copied).
        command += sample.as_args()
    command += ["-i", str(source_path)]

    # -- explicit stream mapping ----------------------------------------
    command += ["-map", f"0:{video_index}"]
    for entry in audio:
        command += ["-map", f"0:{_require_int(entry, 'index', 'audio[]')}"]
    mapped_subtitles = [
        entry
        for entry in subtitles
        if str(entry.get("action") or "").strip().lower() != "drop"
    ]
    for entry in mapped_subtitles:
        command += ["-map", f"0:{_require_int(entry, 'index', 'subtitles[]')}"]

    keep_attachments = container == "mkv" and _bool(plan.get("attachments", False))
    if keep_attachments:
        command += ["-map", "0:t?", "-c:t", "copy"]

    if video_only:
        # Explicitly disable every non-video output stream.
        command += ["-an", "-sn", "-dn"]

    command += ["-map_metadata", "0"]
    chapters = _bool(plan.get("chapters", container == "mkv"))
    command += ["-map_chapters", "0" if chapters else "-1"]

    # -- video -----------------------------------------------------------
    # The colour properties the plan knows are carried into the encode.
    # `setparams` sets them on the frames (that is what the encoder writes
    # into the VUI and the container); the output options repeat them for
    # the encoders that read them from the context.  A QSV decode already
    # produced hardware frames, so no software filter is inserted there -
    # the hardware path carries the values through the encoder context and
    # is verified on the box (docs/QUESTIONS.md).
    colour_filter = _colour_filter(video.get("color")) if action == "encode" else ""
    if action == "remux":
        command += ["-c:v", "copy"]
    elif backend == "qsv":
        command += [
            "-c:v",
            encoder,
            "-global_quality",
            _fmt_number(_require(video, "quality", "video"), "video.quality"),
            "-look_ahead",
            "1" if _bool(video.get("look_ahead", False)) else "0",
        ]
        if bit_depth >= 10:
            command += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        else:
            # A 10-bit source with ten_bit_source="to8" must really produce
            # 8-bit output; never let QSV keep the source depth implicitly.
            command += ["-pix_fmt", "nv12"]
        if scaled:
            command += ["-vf", f"vpp_qsv=w={target_width}:h={target_height}"]
    elif backend == "vaapi":
        command += [
            "-c:v",
            encoder,
            "-qp",
            _fmt_number(_require(video, "quality", "video"), "video.quality"),
        ]
        if bit_depth >= 10:
            command += ["-profile:v", "main10"]
        upload_format = "p010le" if bit_depth >= 10 else "nv12"
        chain = [part for part in (colour_filter,) if part]
        if scaled:
            chain.append(f"scale={target_width}:{target_height}:flags=lanczos")
        chain += [f"format={upload_format}", "hwupload"]
        command += ["-vf", ",".join(chain)]
    else:  # software
        command += [
            "-c:v",
            encoder,
            "-crf",
            _fmt_number(_require(video, "quality", "video"), "video.quality"),
            "-preset",
            "slow",
            "-pix_fmt",
            "p010le" if bit_depth >= 10 else "yuv420p",
        ]
        chain = [part for part in (colour_filter,) if part]
        if scaled:
            chain.append(f"scale={target_width}:{target_height}:flags=lanczos")
        if chain:
            command += ["-vf", ",".join(chain)]

    is_mp4_hevc = container == "mp4" and output_codec == "hevc"
    if is_mp4_hevc:
        command += ["-tag:v", "hvc1", "-movflags", "+faststart"]

    if action == "encode":
        # Colour metadata is part of the output contract: when the plan
        # knows a value the encoder is told to carry it, so a known tag
        # cannot silently disappear.  Unknown/unset values are not
        # invented and are never passed on.
        command += _colour_options(video.get("color"))

    # -- audio -----------------------------------------------------------
    for ordinal, entry in enumerate(audio):
        codec = _audio_codec(entry, container, ordinal)
        command += [f"-c:a:{ordinal}", codec]
        if codec != "copy":
            bitrate = _require(entry, "bitrate", "audio[]")
            command += [f"-b:a:{ordinal}", f"{_fmt_number(bitrate, 'audio[].bitrate')}k"]
            if _bool(entry.get("downmix", False)):
                command += [f"-ac:a:{ordinal}", "8"]
        _stream_metadata_and_disposition(command, entry, "a", ordinal)

    # -- subtitles -------------------------------------------------------
    for ordinal, entry in enumerate(mapped_subtitles):
        source_codec = str(
            entry.get("source_codec") or entry.get("codec") or ""
        ).strip().lower()
        if container == "mp4":
            if source_codec in BITMAP_SUBTITLE_CODECS:
                raise PlanError(
                    "bitmap subtitles cannot be mapped into MP4 "
                    f"(stream {entry.get('index')!r}, {source_codec!r}); "
                    "the planner must choose MKV fallback or drop them"
                )
            command += [f"-c:s:{ordinal}", "mov_text"]
        else:
            command += [f"-c:s:{ordinal}", "copy"]
        _stream_metadata_and_disposition(command, entry, "s", ordinal)

    command += ["-f", "mp4" if container == "mp4" else "matroska", str(output)]
    return command


def _plan_streams(plan: Mapping, key: str) -> list[Mapping]:
    raw = _require(plan, key, "plan")
    if not isinstance(raw, (list, tuple)):
        raise PlanError(f"plan.{key} must be a list")
    result: list[Mapping] = []
    for index, entry in enumerate(raw):
        result.append(_mapping(entry, f"plan.{key}[{index}]"))
    return result


#: Plan colour key -> ffmpeg output option.  Only these four properties are
#: part of the v1 colour contract.
COLOUR_OPTIONS: tuple[tuple[str, str], ...] = (
    ("primaries", "-color_primaries"),
    ("transfer", "-color_trc"),
    ("space", "-colorspace"),
    ("range", "-color_range"),
)

#: Values that mean "not known" and must never be written to the output.
UNKNOWN_COLOUR_VALUES = frozenset(
    {"", "unknown", "n/a", "none", "reserved", "unspecified", "und"}
)


def _colour_options(colour: Any) -> list[str]:
    """ffmpeg output options for the known colour properties in *colour*.

    Unknown, unset or placeholder values are skipped: mediaflow never
    invents a colour tag.  A remux keeps the source metadata through the
    stream copy and is not touched here.
    """
    if not isinstance(colour, Mapping):
        return []
    options: list[str] = []
    for key, option in COLOUR_OPTIONS:
        value = colour.get(key)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if not text or text.lower() in UNKNOWN_COLOUR_VALUES:
            continue
        options += [option, text]
    return options


def _colour_filter(colour: Any) -> str:
    """A ``setparams`` filter that stamps the known colour on the frames.

    The encoders write the VUI from the frame properties, so this is what
    keeps a known colour tag from disappearing during an encode.  Unknown
    values are skipped and never invented.
    """
    if not isinstance(colour, Mapping):
        return ""
    parts: list[str] = []
    for key, option in (
        ("primaries", "color_primaries"),
        ("transfer", "color_trc"),
        ("space", "colorspace"),
        ("range", "range"),
    ):
        value = colour.get(key)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if not text or text.lower() in UNKNOWN_COLOUR_VALUES:
            continue
        parts.append(f"{option}={text}")
    if not parts:
        return ""
    return "setparams=" + ":".join(parts)


def _audio_codec(entry: Mapping, container: str, ordinal: int) -> str:
    action = str(entry.get("action") or "").strip().lower()
    codec = str(entry.get("codec") or "").strip().lower()
    if container == "mkv":
        # MKV presets are remux-only in v1.
        return "copy"
    if action in {"copy", "remux"} or (not action and codec == "copy"):
        return "copy"
    if action in {"transcode", "encode"} or (not action and codec):
        if not codec or codec == "copy":
            raise PlanError(f"audio stream {ordinal} needs a codec for transcode")
        return codec
    raise PlanError(f"unsupported audio action on stream {ordinal}: {action!r}")


def _stream_metadata_and_disposition(
    command: list[str], entry: Mapping, kind: str, ordinal: int
) -> None:
    language = entry.get("language")
    if language:
        command += [f"-metadata:s:{kind}:{ordinal}", f"language={language}"]
    title = entry.get("title")
    if title:
        command += [f"-metadata:s:{kind}:{ordinal}", f"title={title}"]

    dispositions: list[str] = []
    if kind == "s":
        if _bool(entry.get("forced", False)):
            dispositions.append("forced")
        if _bool(entry.get("default", False)):
            dispositions.append("default")
    else:
        if _bool(entry.get("default", False)):
            dispositions.append("default")
    command += [
        f"-disposition:{kind}:{ordinal}",
        "+".join(dispositions) if dispositions else "0",
    ]


# ---------------------------------------------------------------------------
# encoder interface


@dataclass
class EncodeResult:
    """Outcome of one ffmpeg invocation.

    A non-zero ``returncode`` is not raised: the engine records it, the
    original working partial may remain for diagnosis, and the final output
    path is untouched.  Only a successful, non-empty encode is atomically
    renamed into place.
    """

    output: Path
    returncode: int
    command: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    stderr: str = ""
    output_size: int = 0
    partial: Path | None = None
    log_path: Path | None = None
    cancelled: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled and not self.timed_out

    @property
    def success(self) -> bool:
        return self.ok

    @property
    def output_path(self) -> Path:
        return self.output

    @property
    def path(self) -> Path:
        return self.output

    @property
    def return_code(self) -> int:
        return self.returncode

    @property
    def duration(self) -> float:
        return self.duration_seconds

    @property
    def partial_path(self) -> Path | None:
        return self.partial


def _plan_output_path(
    plan: Mapping,
    *,
    explicit: Path | str | None,
    configured: Path | str | None,
    work_dir: Path | str | None,
) -> Path:
    """Resolve where encode() should write, accepting engine conventions."""
    candidate: Path | str | None = explicit
    if candidate is None:
        candidate = configured
    if candidate is None:
        for key in (
            "working_output",
            "output_path",
            "output",
            "work_output",
            "work_output_path",
            "work_path",
        ):
            value = plan.get(key)
            if value:
                candidate = value
                break
    if candidate is None and work_dir is not None:
        relative = plan.get("destination_rel")
        if relative:
            candidate = Path(work_dir) / Path(str(relative)).name
        else:
            ext = plan.get("ext") or plan.get("container") or "bin"
            source = plan.get("source")
            stem = "output"
            if isinstance(source, Mapping) and source.get("path"):
                stem = Path(str(source["path"])).stem
            candidate = Path(work_dir) / f"{stem}.{ext}"
    if candidate is None:
        raise PlanError(
            "output path is not specified: pass output=..., configure "
            "FfmpegEncoder(output=...), or include plan.output_path"
        )
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.name in {"", ".", ".."}:
        raise PlanError(f"invalid output path: {path}")
    return path


def _insert_progress_flags(command: Sequence[str]) -> list[str]:
    """Add global progress/overwrite flags after the executable."""
    return [
        command[0],
        "-progress",
        "pipe:1",
        "-nostats",
        "-y",
        *command[1:],
    ]


def _parse_progress_line(line: str, state: dict[str, Any]) -> bool:
    """Parse one key=value progress line; True means emit a callback.

    Real ffmpeg marks the end of a block with ``progress=``; emitting on the
    first timestamp line as well keeps cancellation responsive and supports
    small test doubles that omit the terminator.
    """
    line = line.strip()
    if not line or "=" not in line:
        return False
    key, value = line.split("=", 1)
    state[key] = value
    if key == "progress":
        return True
    if key == "out_time_us":
        return True
    return key == "out_time_ms" and "out_time_us" not in state


def _speed_value(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith("x"):
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _progress_event(
    state: Mapping[str, Any], duration: float, elapsed: float
) -> dict[str, Any]:
    out_time: float | None = None
    for key in ("out_time_us", "out_time_ms"):
        raw = state.get(key)
        if raw in (None, "", "N/A"):
            continue
        try:
            # ffmpeg calls both fields microseconds; out_time_ms is historical.
            out_time = float(raw) / 1_000_000.0
            break
        except ValueError:
            continue
    speed = _speed_value(state.get("speed"))
    percent: float | None = None
    if out_time is not None and duration > 0:
        percent = max(0.0, min(100.0, out_time / duration * 100.0))
    eta: float | None = None
    if out_time is not None and duration > 0 and speed:
        eta = max(0.0, (duration - out_time) / speed)
    event: dict[str, Any] = {
        "phase": "encoding",
        "percent": percent,
        "out_time": out_time,
        "out_time_seconds": out_time,
        "speed": speed,
        "eta": eta,
        "eta_seconds": eta,
        "elapsed": elapsed,
    }
    for key in ("frame", "fps", "total_size", "bitrate", "progress"):
        if key in state and state[key] not in ("", "N/A"):
            event[key] = state[key]
    return event


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM, wait briefly, then SIGKILL; never leave a runaway child."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        log.error("ffmpeg process %s did not die after SIGKILL", proc.pid)


def _bounded_stderr(handle: Any, limit: int) -> str:
    try:
        handle.flush()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        data = handle.read()
    except (OSError, ValueError):
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", "replace")


class FfmpegEncoder:
    """[ENC-1] Phase-1 encoder implementing ``encode(plan, resume_from=None)``.

    Requirement ENC-1: v1 never resumes and always passes ``resume_from=None``
    (anything else raises ``NotImplementedError``).  No caller may assume an
    encode is one indivisible run.

    The output path comes from the ``output=`` call argument, the
    constructor's ``output``/``output_path``, or ``plan.working_output`` (the
    engine's convention), in that order where supplied.  The ffmpeg process is
    started with an argument list only.  It writes to ``output + ".partial"``;
    on success that file is fsynced and atomically renamed to ``output``
    followed by an fsynced parent directory.  A missing or empty partial is
    never renamed into the final working name.

    ``timeout`` is an overall wall-clock limit in seconds.  Cancellation is a
    ``threading.Event``; either condition sends SIGTERM, then SIGKILL after a
    grace period, and returns a non-zero result with details.
    """

    def __init__(
        self,
        ffmpeg: str = FFMPEG,
        output: Path | str | None = None,
        *,
        work_dir: Path | str | None = None,
        timeout: float | None = None,
        software: bool = False,
        stderr_log: Path | str | None = None,
        log_path: Path | str | None = None,
        output_path: Path | str | None = None,
        stderr_limit: int = DEFAULT_STDERR_LIMIT,
    ):
        self.ffmpeg = str(ffmpeg)
        if output is None:
            output = output_path
        self.output = Path(output) if output is not None else None
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self.timeout = timeout
        self.software = software
        if stderr_log is None:
            stderr_log = log_path
        self.stderr_log = Path(stderr_log) if stderr_log is not None else None
        self.stderr_limit = max(1, int(stderr_limit))

    def encode(
        self,
        plan: Mapping,
        resume_from: Path | str | None = None,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_event: Any = None,
        pause_event: Any = None,
        env: Mapping[str, str] | None = None,
        output: Path | str | None = None,
        timeout: float | None = None,
        software: bool | None = None,
        sample: SampleSpec | None = None,
        video_only: bool = False,
    ) -> EncodeResult:
        """Run ffmpeg for ``plan`` and return an :class:`EncodeResult`.

        ``sample`` and ``video_only`` are the explicit calibration knobs:
        they restrict one run to an input interval and to the video stream
        alone.  Production calls never pass them, so their command is the
        ordinary production command.
        """
        if resume_from is not None:
            raise NotImplementedError(
                "mediaflow v1 does not resume encodes; pass resume_from=None"
            )
        if not isinstance(plan, Mapping):
            raise PlanError(f"plan must be a mapping, got {type(plan).__name__}")

        final = _plan_output_path(
            plan,
            explicit=output,
            configured=self.output,
            work_dir=self.work_dir,
        )
        if final.is_symlink():
            raise PlanError(f"refusing to write through symlink output: {final}")
        if final.exists() and not final.is_file():
            raise PlanError(f"output path is not a regular file: {final}")
        partial = output_partial_path(final)
        if partial.is_symlink():
            raise PlanError(f"refusing to write through symlink partial: {partial}")

        use_software = self.software if software is None else software
        effective_timeout = self.timeout if timeout is None else timeout
        command = _insert_progress_flags(
            build_command(
                plan,
                partial,
                ffmpeg=self.ffmpeg,
                software=use_software,
                sample=sample,
                video_only=video_only,
            )
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            _truncate_partial(partial)
        except OSError as exc:
            raise PlanError(f"cannot create fresh partial output: {exc}") from None
        duration = _plan_duration(plan)
        started = time.monotonic()
        process_env = dict(os.environ)
        if env:
            process_env.update({str(k): str(v) for k, v in env.items()})

        stderr_handle = tempfile.TemporaryFile()
        cancelled = False
        timed_out = False
        returncode = -1
        progress_state: dict[str, Any] = {}
        last_progress: dict[str, Any] = {}
        proc: subprocess.Popen | None = None
        paused = False

        def notify(event: dict[str, Any]) -> None:
            if progress_cb is not None:
                progress_cb(event)

        notify({"phase": "encoding", "percent": 0.0, "out_time": 0.0,
                "out_time_seconds": 0.0, "speed": None, "eta": None,
                "eta_seconds": None, "elapsed": 0.0})

        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                env=process_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                close_fds=True,
            )
            assert proc.stdout is not None
            lines: queue.Queue[str | None] = queue.Queue()

            def read_progress() -> None:
                try:
                    for line in proc.stdout:  # type: ignore[union-attr]
                        lines.put(line)
                except (OSError, ValueError):
                    pass
                finally:
                    lines.put(None)

            threading.Thread(
                target=read_progress, name="ffmpeg-progress", daemon=True
            ).start()

            while True:
                if pause_event is not None and proc.poll() is None:
                    try:
                        should_pause = bool(pause_event.is_set())
                    except Exception:
                        should_pause = False
                    if should_pause and not paused:
                        try:
                            os.kill(proc.pid, signal.SIGSTOP)
                            paused = True
                            notify({"phase": "encoding", "paused": True,
                                    "out_time_seconds": last_progress.get("out_time_seconds"),
                                    "percent": last_progress.get("percent")})
                        except OSError:
                            paused = False
                    elif not should_pause and paused:
                        try:
                            os.kill(proc.pid, signal.SIGCONT)
                            paused = False
                            notify({"phase": "encoding", "paused": False,
                                    "out_time_seconds": last_progress.get("out_time_seconds"),
                                    "percent": last_progress.get("percent"),
                                    "resumed": True})
                        except OSError:
                            paused = False
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    if paused:
                        try:
                            os.kill(proc.pid, signal.SIGCONT)
                        except OSError:
                            pass
                        paused = False
                    _terminate(proc)
                    break
                if (
                    effective_timeout is not None
                    and time.monotonic() - started > effective_timeout
                ):
                    timed_out = True
                    _terminate(proc)
                    break
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                if _parse_progress_line(line, progress_state):
                    event = _progress_event(
                        progress_state, duration, time.monotonic() - started
                    )
                    last_progress = event
                    notify(event)

            # Drain any progress lines already queued after the child exits.
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    break
                if _parse_progress_line(line, progress_state):
                    event = _progress_event(
                        progress_state, duration, time.monotonic() - started
                    )
                    last_progress = event
                    notify(event)
            returncode = proc.wait()
            if progress_state and not last_progress and proc.returncode == 0:
                last_progress = _progress_event(
                    progress_state, duration, time.monotonic() - started
                )
                notify(last_progress)
        except FileNotFoundError as exc:
            returncode = 127
            stderr_handle.write(f"ffmpeg executable not found: {exc}\n".encode())
        except Exception as exc:  # launch or selector failure
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            returncode = 126
            stderr_handle.write(f"failed to run ffmpeg: {exc}\n".encode())
        finally:
            if paused and proc is not None and proc.poll() is None:
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                except OSError:
                    pass
            elapsed = time.monotonic() - started
            stderr_text = _bounded_stderr(stderr_handle, self.stderr_limit)
            stderr_handle.close()

        if cancelled:
            stderr_text = _append_note(stderr_text, "encode cancelled")
        if timed_out:
            stderr_text = _append_note(
                stderr_text, f"encode timed out after {effective_timeout}s"
            )

        log_file: Path | None = None
        if self.stderr_log is not None:
            try:
                self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
                self.stderr_log.write_bytes(stderr_text.encode("utf-8", "replace"))
                log_file = self.stderr_log
            except OSError:
                log.exception("could not write encoder stderr log: %s", self.stderr_log)

        output_size = 0
        if returncode == 0 and not cancelled and not timed_out:
            problem = _publish_partial(partial, final)
            if problem is not None:
                stderr_text = _append_note(stderr_text, problem)
                returncode = 1
            else:
                try:
                    output_size = final.stat().st_size
                except OSError:
                    output_size = 0
                notify(
                    {
                        "phase": "encoding",
                        "percent": 100.0,
                        "out_time": duration,
                        "out_time_seconds": duration,
                        "speed": last_progress.get("speed"),
                        "eta": 0.0,
                        "eta_seconds": 0.0,
                        "elapsed": elapsed,
                    }
                )

        result = EncodeResult(
            output=final,
            returncode=returncode,
            command=command,
            duration_seconds=elapsed,
            stderr=stderr_text,
            output_size=output_size,
            partial=partial,
            log_path=log_file,
            cancelled=cancelled,
            timed_out=timed_out,
        )
        if not result.ok:
            log.warning(
                "encode failed output=%s rc=%s cancelled=%s timeout=%s: %s",
                final,
                returncode,
                cancelled,
                timed_out,
                stderr_text[-500:],
            )
        return result


def _append_note(text: str, note: str) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}[mediaflow] {note}\n"


def _truncate_partial(partial: Path) -> None:
    """Create/truncate the working partial so a stale file is never adopted."""
    fd = os.open(
        partial,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0),
        0o664,
    )
    os.close(fd)


def _publish_partial(partial: Path, final: Path) -> str | None:
    """fsync + atomic rename, refusing missing/empty/non-regular partials."""
    try:
        st = partial.lstat()
    except FileNotFoundError:
        return "ffmpeg reported success but wrote no partial output"
    except OSError as exc:
        return f"cannot stat partial output: {exc}"
    if partial.is_symlink():
        return "refusing to publish symlink partial output"
    if not os.path.isfile(partial) or _is_special(st.st_mode):
        return "partial output is not a regular file"
    if st.st_size <= 0:
        return "ffmpeg reported success but partial output is empty"
    try:
        from .safety import fsync_dir, fsync_file

        fsync_file(partial)
        os.replace(partial, final)
        fsync_dir(final.parent)
    except OSError as exc:
        return f"could not publish partial output atomically: {exc}"
    return None


def _is_special(mode: int) -> bool:
    import stat

    return not stat.S_ISREG(mode)


def _plan_duration(plan: Mapping) -> float:
    source = plan.get("source")
    if isinstance(source, Mapping):
        raw = source.get("duration")
        if raw is not None:
            try:
                value = float(raw)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    return 0.0


class FakeEncoder:
    """A deliberately dumb encoder for tests.

    It copies the bytes of ``plan.source.path`` to the requested output and
    does not produce valid media.  Real engine tests should use their own
    fake; this only exercises the interface and file handoff.
    """

    def __init__(
        self,
        output: Path | str | None = None,
        *,
        work_dir: Path | str | None = None,
    ):
        self.output = Path(output) if output is not None else None
        self.work_dir = Path(work_dir) if work_dir is not None else None

    def encode(
        self,
        plan: Mapping,
        resume_from: Path | str | None = None,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_event: Any = None,
        env: Mapping[str, str] | None = None,
        output: Path | str | None = None,
    ) -> EncodeResult:
        del env
        if resume_from is not None:
            raise NotImplementedError(
                "mediaflow v1 does not resume encodes; pass resume_from=None"
            )
        if cancel_event is not None and cancel_event.is_set():
            final = _plan_output_path(
                plan, explicit=output, configured=self.output, work_dir=self.work_dir
            )
            return EncodeResult(
                output=final,
                returncode=-15,
                stderr="[mediaflow] cancelled before copy\n",
                cancelled=True,
                partial=output_partial_path(final),
            )

        source = _mapping(_require(plan, "source", "plan"), "plan.source")
        source_path = Path(_str(_require(source, "path", "plan.source"), "source.path"))
        final = _plan_output_path(
            plan, explicit=output, configured=self.output, work_dir=self.work_dir
        )
        partial = output_partial_path(final)
        final.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        if progress_cb is not None:
            progress_cb(
                {"phase": "encoding", "percent": 0.0, "out_time": 0.0,
                 "out_time_seconds": 0.0, "speed": None, "eta": None,
                 "eta_seconds": None, "elapsed": 0.0}
            )
        try:
            shutil.copyfile(source_path, partial)
            from .safety import fsync_dir, fsync_file

            fsync_file(partial)
            os.replace(partial, final)
            fsync_dir(final.parent)
        except OSError as exc:
            return EncodeResult(
                output=final,
                returncode=1,
                duration_seconds=time.monotonic() - started,
                stderr=f"[mediaflow] fake copy failed: {exc}\n",
                partial=partial,
            )
        size = final.stat().st_size
        if progress_cb is not None:
            duration = _plan_duration(plan)
            progress_cb(
                {"phase": "encoding", "percent": 100.0,
                 "out_time": duration, "out_time_seconds": duration,
                 "speed": None, "eta": 0.0, "eta_seconds": 0.0,
                 "elapsed": time.monotonic() - started}
            )
        return EncodeResult(
            output=final,
            returncode=0,
            duration_seconds=time.monotonic() - started,
            output_size=size,
            partial=partial,
        )


# ---------------------------------------------------------------------------
# capability helper (doctor)


def probe_encoders(ffmpeg: str = FFMPEG, *, timeout: float = 20.0) -> dict[str, Any]:
    """Return ffmpeg version/encoder capability facts for setup diagnostics.

    Optional helper: the daemon's doctor and capability gating can use it, but
    encoding itself does not depend on it.  Never uses a shell.
    """
    result: dict[str, Any] = {
        "ffmpeg": str(ffmpeg),
        "version": "",
        "encoders": [],
        "libx265": False,
        "libx264": False,
        "hevc_qsv": False,
        "hevc_vaapi": False,
        "hevc_nvenc": False,
        "error": None,
    }
    try:
        version_run = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if version_run.returncode == 0:
            first = version_run.stdout.splitlines()
            result["version"] = first[0].strip() if first else ""
        else:
            result["error"] = (version_run.stderr or "").strip()[-2000:]
        encoders_run = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        names: set[str] = set()
        if encoders_run.returncode == 0:
            for line in encoders_run.stdout.splitlines():
                match = re.match(r"^\s*[VAS][A-Z.]{5}\s+(\S+)", line)
                if match:
                    names.add(match.group(1))
        else:
            result["error"] = (encoders_run.stderr or "").strip()[-2000:]
        result["encoders"] = sorted(names)
        result["libx265"] = "libx265" in names
        result["libx264"] = "libx264" in names
        result["hevc_qsv"] = "hevc_qsv" in names
        result["hevc_vaapi"] = "hevc_vaapi" in names
        result["hevc_nvenc"] = "hevc_nvenc" in names
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    return result
