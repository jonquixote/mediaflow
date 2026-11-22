"""Machine and space protection gates.

This module is deliberately side-effect free with respect to the machine: it
only *reads* sysfs, procfs and filesystem usage, and it never signals a
process, writes sysfs, touches a fan/PROCHOT setting, or deletes anything.

The gates are fail-closed.  An unreadable sensor, an unavailable ``/proc``, or
an unknown power supply state blocks a start unless the corresponding setting
explicitly disables that protection.

Requirements implemented here: [SAF-8] free-space guard, [SAF-9] machine
protection, and [SCHED-1] schedule window as a start gate.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .supervision import ProtectionAction, ProtectionDecision

try:  # POSIX only; mediaflow itself only runs on Linux.
    import pwd
except ImportError:  # pragma: no cover - keeps imports testable on non-POSIX
    pwd = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

GIB = 1024**3

DEFAULT_MIN_FREE_GB = 40.0
DEFAULT_ROOT_MIN_FREE_GB = 2.0
DEFAULT_MAX_TRASH_GB = 120.0
DEFAULT_START_IF_TEMP_BELOW_C = 65.0
DEFAULT_THERMAL_PAUSE_C = 85.0
DEFAULT_THERMAL_RESUME_C = 75.0
DEFAULT_THERMAL_SUSTAIN_SECONDS = 60.0
DEFAULT_JELLYFIN_USER = "jellyfin"

_MISSING = object()

_PATH_FIELDS = (
    "sysfs_root",
    "proc_root",
    "filesystem_root",
    "db_dir",
    "trash",
    "pipeline",
    "library",
    "run_dir",
)

_REASON_MESSAGES = {
    "jellyfin-transcoding": "Jellyfin is transcoding",
    "on-battery": "the machine is running on battery",
    "no-ac-source": "no AC power supply status is available",
    "temp-sensor-missing": "the package temperature sensor is unavailable",
    "too-hot": "the package temperature is above the start threshold",
    "space-low": "free space is below the required reserve",
    "root-space-low": "the root filesystem is below its reserve",
    "db-space-low": "the database filesystem is below its reserve",
    "trash-too-big": "trash has reached its configured limit",
    "user-paused": "the queue is paused by the user",
    "estimate-exceeds-window": "the estimated run would not finish inside the window",
    "estimate-cold-start": "no speed estimate is available for the schedule window",
    "ffmpeg-changed": "ffmpeg changed and capability tests have not passed",
    "outside-window": "the current time is outside the allowed start window",
}


# ---------------------------------------------------------------------------
# Decisions


@dataclass(frozen=True)
class GuardDecision:
    """The result of one or more safety gates.

    ``reasons`` contains stable machine slugs only; they are what the daemon
    stores and reports as pause reasons.  ``messages`` contains one
    human-readable message per reason (same order) for logs and notifications.
    """

    ok: bool
    reasons: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "reasons", tuple(str(r) for r in self.reasons))
        object.__setattr__(self, "messages", tuple(str(m) for m in self.messages))


class _DecisionBuilder:
    """Collect reasons once while preserving deterministic order."""

    def __init__(self) -> None:
        self._reasons: list[str] = []
        self._messages: list[str] = []
        self._seen: set[str] = set()

    def add(self, reason: str, message: str | None = None) -> None:
        reason = str(reason)
        if reason in self._seen:
            return
        self._seen.add(reason)
        self._reasons.append(reason)
        self._messages.append(message or _REASON_MESSAGES.get(reason, reason))

    def extend(self, decision: GuardDecision) -> None:
        messages = list(decision.messages)
        for index, reason in enumerate(decision.reasons):
            message = messages[index] if index < len(messages) else None
            self.add(reason, message)

    def build(self) -> GuardDecision:
        return GuardDecision(
            ok=not self._reasons,
            reasons=tuple(self._reasons),
            messages=tuple(self._messages),
        )


# ---------------------------------------------------------------------------
# Configuration


@dataclass
class GuardConfig:
    """Configuration for the protection layer.

    The fields mirror the SPEC config.  Paths are plain :class:`Path` values so
    tests can point every read at a sandbox.  ``from_config`` adapts a config
    mapping/object without importing ``mediaflow.config`` (which would create
    an import cycle and race with concurrent edits).
    """

    min_free_gb: float = DEFAULT_MIN_FREE_GB
    root_min_free_gb: float = DEFAULT_ROOT_MIN_FREE_GB
    max_trash_gb: float = DEFAULT_MAX_TRASH_GB
    pause_when_jellyfin_transcoding: bool = True
    pause_on_battery: bool = True
    temp_sensor: str = "auto"
    start_if_temp_below_c: float = DEFAULT_START_IF_TEMP_BELOW_C
    thermal_pause_c: float = DEFAULT_THERMAL_PAUSE_C
    thermal_resume_c: float = DEFAULT_THERMAL_RESUME_C
    thermal_sustain_seconds: float = DEFAULT_THERMAL_SUSTAIN_SECONDS
    jellyfin_user: str = DEFAULT_JELLYFIN_USER

    sysfs_root: Path = Path("/sys")
    proc_root: Path = Path("/proc")
    filesystem_root: Path = Path("/")
    db_dir: Path = Path("/var/lib/mediaflow")
    trash: Path = Path("/srv/media/_pipeline/trash")
    pipeline: Path = Path("/srv/media/_pipeline")
    library: Path = Path("/srv/media/movies")
    run_dir: Path = Path("/run/mediaflow")

    def __post_init__(self) -> None:
        for name in _PATH_FIELDS:
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, Path(value))

    @classmethod
    def from_config(cls, config: Any) -> "GuardConfig":
        """Best-effort adapter for a config mapping or duck-typed object.

        Missing values keep the defaults above.  Flat fields win over values
        under an optional ``paths`` sub-object/mapping.
        """

        defaults = cls()
        nested = _lookup(config, "paths")
        if callable(nested):
            nested = _MISSING

        def raw(name: str) -> Any:
            value = _lookup(config, name)
            if value is not _MISSING and value is not None:
                return value
            if name in _PATH_FIELDS and nested is not _MISSING and nested is not None:
                nested_value = _lookup(nested, name)
                if nested_value is not _MISSING and nested_value is not None:
                    return nested_value

            # Best-effort derivations for the production Config shape, which
            # exposes state_dir/runtime_dir/db_path and derived properties.
            if name == "db_dir":
                db_path = _lookup(config, "db_path")
                if db_path is not _MISSING and db_path is not None:
                    return Path(db_path).parent
                state_dir = _lookup(config, "state_dir")
                if state_dir is not _MISSING and state_dir is not None:
                    return state_dir
            elif name == "run_dir":
                runtime_dir = _lookup(config, "runtime_dir")
                if runtime_dir is not _MISSING and runtime_dir is not None:
                    return runtime_dir
            elif name == "library":
                movies = _lookup(config, "movies")
                if movies is not _MISSING and movies is not None:
                    return movies
                root = _lookup(config, "root")
                if root is not _MISSING and root is not None:
                    return Path(root) / "movies"
            elif name == "pipeline":
                root = _lookup(config, "root")
                if root is not _MISSING and root is not None:
                    return Path(root) / "_pipeline"
            elif name == "trash":
                pipeline = _lookup(config, "pipeline")
                if pipeline is not _MISSING and pipeline is not None:
                    return Path(pipeline) / "trash"
            return _MISSING

        def number(name: str, default: float) -> float:
            value = raw(name)
            if value is _MISSING:
                return float(default)
            try:
                return float(value)
            except (TypeError, ValueError):
                log.warning("ignoring invalid %s=%r in guard config", name, value)
                return float(default)

        def boolean(name: str, default: bool) -> bool:
            value = raw(name)
            if value is _MISSING:
                return default
            return _coerce_bool(value, default)

        def text(name: str, default: str) -> str:
            value = raw(name)
            if value is _MISSING:
                return default
            return str(value)

        def path(name: str, default: Path) -> Path:
            value = raw(name)
            if value is _MISSING:
                return Path(default)
            return Path(value)

        return cls(
            min_free_gb=number("min_free_gb", defaults.min_free_gb),
            root_min_free_gb=number("root_min_free_gb", defaults.root_min_free_gb),
            max_trash_gb=number("max_trash_gb", defaults.max_trash_gb),
            pause_when_jellyfin_transcoding=boolean(
                "pause_when_jellyfin_transcoding",
                defaults.pause_when_jellyfin_transcoding,
            ),
            pause_on_battery=boolean("pause_on_battery", defaults.pause_on_battery),
            temp_sensor=text("temp_sensor", defaults.temp_sensor),
            start_if_temp_below_c=number(
                "start_if_temp_below_c", defaults.start_if_temp_below_c
            ),
            thermal_pause_c=number("thermal_pause_c", defaults.thermal_pause_c),
            thermal_resume_c=number("thermal_resume_c", defaults.thermal_resume_c),
            thermal_sustain_seconds=number(
                "thermal_sustain_seconds", defaults.thermal_sustain_seconds
            ),
            jellyfin_user=text("jellyfin_user", defaults.jellyfin_user),
            sysfs_root=path("sysfs_root", defaults.sysfs_root),
            proc_root=path("proc_root", defaults.proc_root),
            filesystem_root=path("filesystem_root", defaults.filesystem_root),
            db_dir=path("db_dir", defaults.db_dir),
            trash=path("trash", defaults.trash),
            pipeline=path("pipeline", defaults.pipeline),
            library=path("library", defaults.library),
            run_dir=path("run_dir", defaults.run_dir),
        )


def _lookup(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping or duck-typed object, best effort."""

    if isinstance(obj, Mapping):
        return obj.get(name, _MISSING)
    try:
        return getattr(obj, name, _MISSING)
    except Exception:  # a broken property should not crash a safety read
        return _MISSING


def _cfg_get(cfg: Any, name: str, default: Any) -> Any:
    value = _lookup(cfg, name)
    return default if value is _MISSING or value is None else value


def _cfg_path(cfg: Any, name: str, default: Path) -> Path:
    return Path(_cfg_get(cfg, name, default))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default if value is None else bool(value)


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


# ---------------------------------------------------------------------------
# [SAF-8] Free space and trash


@dataclass(frozen=True)
class SpaceStatus:
    """Observed space values plus the resulting decision.

    ``remaining_work_bytes`` and ``emergency_required_bytes`` make the
    in-flight reserve explicit.  The latter is an additive low-water
    reserve; it is not a replacement for the work estimate and trash is
    never purged to satisfy it.
    """

    free_bytes: int
    required_bytes: int
    trash_bytes: int
    root_free_bytes: int
    db_free_bytes: int
    decision: GuardDecision
    remaining_work_bytes: int = 0
    emergency_required_bytes: int = 0
    output_consumed_bytes: int = 0
    additional_required_bytes: int = 0


def _free_bytes(disk_usage: Callable[[Any], Any], path: Path) -> int | None:
    """Return free bytes for the filesystem containing ``path``.

    A not-yet-created path is resolved to its nearest existing parent, which is
    the filesystem that path will live on.  Any other error is an unknown and
    therefore fails closed (``None``).
    """

    candidate = Path(path)
    while True:
        try:
            usage = disk_usage(candidate)
            try:
                return int(usage.free)
            except AttributeError:
                # Accept a plain (total, used, free) tuple as well as the
                # shutil.disk_usage named tuple.
                try:
                    return int(usage[2])
                except (IndexError, TypeError, ValueError, OverflowError):
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
        except (FileNotFoundError, NotADirectoryError):
            parent = candidate.parent
            if parent == candidate:
                return None
            candidate = parent
        except OSError:
            return None


def _trash_size_bytes(root: Path) -> tuple[int, bool]:
    """Recursively sum regular files under ``root`` without following links.

    Returns ``(size, complete)``.  Symlinks are skipped (never followed) and a
    symlinked trash root is treated as unmeasurable.  ``complete`` is false if
    any directory or entry could not be read; callers treat that as over-limit.
    """

    root = Path(root)
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return 0, True
    except OSError as exc:
        log.warning("cannot stat trash %s: %s", root, exc)
        return 0, False

    if stat.S_ISLNK(root_stat.st_mode):
        log.warning("refusing to follow symlinked trash root %s", root)
        return 0, False
    if stat.S_ISREG(root_stat.st_mode):
        return int(root_stat.st_size), True
    if not stat.S_ISDIR(root_stat.st_mode):
        return 0, True

    total = 0
    complete = True
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError as exc:
                        complete = False
                        log.warning("cannot measure trash entry %s: %s", entry.path, exc)
        except OSError as exc:
            complete = False
            log.warning("cannot scan trash directory %s: %s", directory, exc)
    return total, complete


def _fmt_gib(value: int | float) -> str:
    return f"{float(value) / GIB:.2f} GiB"


def check_space(
    cfg: GuardConfig,
    *,
    source_size: int = 0,
    remaining_work: int | None = None,
    output_consumed: int = 0,
    emergency_low_water: int | None = None,
    additional_required_bytes: int = 0,
    mid_encode: bool = False,
    db_path: Path | None = None,
    disk_usage: Callable[[Any], Any] | None = None,
) -> tuple[SpaceStatus, GuardDecision]:
    """Check all free-space reserves.  Never purges trash.

    Admission reserves ``max(1.2 * source_size, min_free_gb)``.  During an
    encode, callers provide ``remaining_work``: the conservative number of
    output bytes still to be written.  ``output_consumed`` is reported for
    observability but is not subtracted again, because ``remaining_work`` is
    already a net remaining-work value.  The emergency low-water reserve is
    additive to that value, so a nearly-complete encode does not accidentally
    remove the safety margin.

    The legacy call shape (no ``remaining_work``) retains the old
    ``max(1.2 * source_size, min_free_gb / 2)`` mid-encode behaviour for
    compatibility with callers outside the engine.  Root and database
    filesystems each need ``root_min_free_gb`` independently and trash is a
    policy cap, never a source of early-purged space.
    """

    builder = _DecisionBuilder()
    usage = disk_usage if disk_usage is not None else shutil.disk_usage
    min_free_gb = _number(_cfg_get(cfg, "min_free_gb", DEFAULT_MIN_FREE_GB), DEFAULT_MIN_FREE_GB)
    source_bytes = max(0.0, _number(source_size, 0.0))
    remaining_bytes: int | None = None
    consumed_bytes = max(0, int(math.ceil(_number(output_consumed, 0.0))))
    if remaining_work is not None:
        # ``remaining_work`` is already net: callers have accounted for the
        # bytes consumed by the current partial.  Do not subtract the same
        # bytes a second time.
        remaining_bytes = max(0, int(math.ceil(_number(remaining_work, 0.0))))
    emergency_bytes = 0
    if mid_encode:
        if emergency_low_water is not None:
            emergency_bytes = max(0, int(math.ceil(_number(emergency_low_water, 0.0))))
        else:
            emergency_bytes = int(math.ceil((min_free_gb / 2.0) * GIB))
        if remaining_bytes is None:
            # Compatibility path for legacy callers.  The engine always
            # supplies remaining_work for an in-flight check.
            required = int(math.ceil(max(1.2 * source_bytes, emergency_bytes)))
        else:
            # The emergency reserve is additional to the remaining work.
            required = remaining_bytes + emergency_bytes
    else:
        required = int(math.ceil(max(1.2 * source_bytes, min_free_gb * GIB)))

    # Additive work-specific reserve (for example the bytes a publish may copy
    # when a hard link is unavailable).  It never replaces the source-based
    # SAF-8 reservation.
    additional = max(0, int(math.ceil(_number(additional_required_bytes, 0.0))))
    required += additional

    pipeline = _cfg_path(cfg, "pipeline", Path("/srv/media/_pipeline"))
    free = _free_bytes(usage, pipeline)
    if free is None:
        builder.add(
            "space-low",
            f"cannot determine free space for {pipeline}",
        )
        free = 0
    elif free < required:
        builder.add(
            "space-low",
            f"{_fmt_gib(free)} free on {pipeline} is below the required {_fmt_gib(required)}",
        )

    root_min_gb = _number(
        _cfg_get(cfg, "root_min_free_gb", DEFAULT_ROOT_MIN_FREE_GB),
        DEFAULT_ROOT_MIN_FREE_GB,
    )
    root_required = int(math.ceil(root_min_gb * GIB))
    filesystem_root = _cfg_path(cfg, "filesystem_root", Path("/"))
    root_free = _free_bytes(usage, filesystem_root)
    if root_free is None:
        builder.add("root-space-low", f"cannot determine free space for {filesystem_root}")
        root_free = 0
    elif root_free < root_required:
        builder.add(
            "root-space-low",
            f"{_fmt_gib(root_free)} free on {filesystem_root} is below {_fmt_gib(root_required)}",
        )

    db_target = Path(db_path) if db_path is not None else _cfg_path(
        cfg, "db_dir", Path("/var/lib/mediaflow")
    )
    db_free = _free_bytes(usage, db_target)
    if db_free is None:
        builder.add("db-space-low", f"cannot determine free space for {db_target}")
        db_free = 0
    elif db_free < root_required:
        builder.add(
            "db-space-low",
            f"{_fmt_gib(db_free)} free on {db_target} is below {_fmt_gib(root_required)}",
        )

    trash = _cfg_path(cfg, "trash", Path("/srv/media/_pipeline/trash"))
    trash_bytes, trash_complete = _trash_size_bytes(trash)
    max_trash_gb = _number(
        _cfg_get(cfg, "max_trash_gb", DEFAULT_MAX_TRASH_GB), DEFAULT_MAX_TRASH_GB
    )
    max_trash_bytes = int(math.ceil(max_trash_gb * GIB))
    if not trash_complete:
        builder.add(
            "trash-too-big",
            f"cannot measure trash at {trash}; treating it as over the limit",
        )
    elif trash_bytes > max_trash_bytes:
        builder.add(
            "trash-too-big",
            f"trash is {_fmt_gib(trash_bytes)}, above the {_fmt_gib(max_trash_bytes)} limit",
        )

    decision = builder.build()
    status = SpaceStatus(
        free_bytes=free,
        required_bytes=required,
        trash_bytes=trash_bytes,
        root_free_bytes=root_free,
        db_free_bytes=db_free,
        decision=decision,
        remaining_work_bytes=(remaining_bytes or 0),
        emergency_required_bytes=emergency_bytes,
        output_consumed_bytes=consumed_bytes,
        additional_required_bytes=additional,
    )
    return status, decision


# ---------------------------------------------------------------------------
# [SAF-9] Temperature sensor discovery and reads


@dataclass(frozen=True)
class SensorInfo:
    """A discovered hwmon package-temperature input.

    ``disabled`` marks an explicit ``temp_sensor = "none"`` configuration; it
    is not a missing sensor.  ``to_json`` keeps the representation JSON-safe.
    """

    name: str
    hwmon_dir: Path
    temp_input: Path | None
    label: str | None = None
    disabled: bool = False

    @property
    def path(self) -> Path | None:
        """Alias used by some callers for the input file."""

        return self.temp_input

    @property
    def kind(self) -> str:
        """``"none"`` for an explicitly disabled sensor, else the driver name."""

        return "none" if self.disabled else self.name

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "hwmon_dir": str(self.hwmon_dir),
            "temp_input": str(self.temp_input) if self.temp_input is not None else None,
            "label": self.label,
            "disabled": self.disabled,
        }


def _sensor_from_hwmon(
    hwmon_dir: Path,
    *,
    name: str,
    input_path: Path,
    label: str | None,
) -> SensorInfo:
    return SensorInfo(
        name=name,
        hwmon_dir=hwmon_dir,
        temp_input=input_path,
        label=label,
        disabled=False,
    )


def _sensor_in_dirs(dirs: list[Path], desired_name: str) -> SensorInfo | None:
    """Choose the best package input from already-matched hwmon dirs."""

    # Prefer an exact "Package id 0" label, then any Package label.  Never use
    # a fixed hwmon or channel index; enumerate what the driver exposes.
    preferred: list[tuple[int, str, int, Path, Path, str]] = []
    for hwmon_dir in dirs:
        try:
            children = sorted(hwmon_dir.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            match = re.fullmatch(r"temp(\d+)_label", child.name)
            if match is None:
                continue
            try:
                label = child.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if "package" not in label.casefold():
                continue
            channel = int(match.group(1))
            input_path = hwmon_dir / f"temp{channel}_input"
            if not input_path.is_file():
                continue
            rank = 0 if label.casefold() == "package id 0" else 1
            preferred.append((rank, hwmon_dir.name, channel, hwmon_dir, input_path, label))

    if preferred:
        preferred.sort(key=lambda item: (item[0], item[1], item[2]))
        _rank, _dir_name, _channel, hwmon_dir, input_path, label = preferred[0]
        return _sensor_from_hwmon(
            hwmon_dir, name=desired_name, input_path=input_path, label=label
        )

    # Some coretemp-like drivers omit labels.  Only accept temp1_input when the
    # hwmon name itself is clearly the requested driver, and say so loudly.
    for hwmon_dir in dirs:
        input_path = hwmon_dir / "temp1_input"
        if input_path.is_file():
            log.warning(
                "hwmon %s (%s) has no Package label; falling back to temp1_input",
                hwmon_dir,
                desired_name,
            )
            return _sensor_from_hwmon(
                hwmon_dir, name=desired_name, input_path=input_path, label=None
            )
    return None


def _discover_named_sensor(sysfs_root: Path, desired_name: str) -> SensorInfo | None:
    base = sysfs_root / "class" / "hwmon"
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError:
        return None

    matches: list[Path] = []
    for hwmon_dir in entries:
        try:
            if not hwmon_dir.is_dir():
                continue
            name = (hwmon_dir / "name").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if name == desired_name:
            matches.append(hwmon_dir)

    if not matches:
        return None
    return _sensor_in_dirs(matches, desired_name)


def discover_temp_sensor(cfg: GuardConfig) -> SensorInfo | None:
    """Find the package temperature sensor by hwmon name.

    ``temp_sensor = "auto"`` searches for the ``coretemp`` driver and selects a
    package-level input, preferring ``Package id 0``.  ``"none"`` returns an
    explicit disabled sensor.  Any other value is treated as an exact hwmon
    name (or an absolute hwmon/input path) for testability.
    """

    setting = str(_cfg_get(cfg, "temp_sensor", "auto")).strip()
    sysfs_root = _cfg_path(cfg, "sysfs_root", Path("/sys"))

    if setting.casefold() == "none":
        return SensorInfo(
            name="none",
            hwmon_dir=sysfs_root,
            temp_input=None,
            label="disabled",
            disabled=True,
        )

    if setting.casefold() in {"", "auto"}:
        return _discover_named_sensor(sysfs_root, "coretemp")

    candidate = Path(setting)
    if candidate.is_absolute():
        if candidate.is_dir():
            name_path = candidate / "name"
            try:
                driver_name = name_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except OSError:
                driver_name = "configured"
            return _sensor_in_dirs([candidate], driver_name or "configured")
        if candidate.is_file():
            return _sensor_from_hwmon(
                candidate.parent,
                name="configured",
                input_path=candidate,
                label=None,
            )
        return None

    return _discover_named_sensor(sysfs_root, setting)


def read_package_temp(sensor: SensorInfo | None) -> float | None:
    """Read a sensor in Celsius, or return ``None`` on any uncertainty.

    hwmon reports millidegrees Celsius.  Small values are accepted as already
    being Celsius so synthetic fixtures stay simple; any implausible reading is
    rejected rather than allowed to fail open.
    """

    if sensor is None or sensor.disabled or sensor.temp_input is None:
        return None
    try:
        raw = Path(sensor.temp_input).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    if abs(value) >= 1000.0:
        value /= 1000.0
    if value < -40.0 or value > 200.0:
        return None
    return value


# ---------------------------------------------------------------------------
# [SAF-9] AC power


def read_ac_state(cfg: GuardConfig) -> bool | None:
    """Return mains state: True online, False on battery, None unknown.

    Every ``online`` file under ``class/power_supply`` is inspected.  Some
    supplies (for example a battery) do not expose ``online``; those are
    ignored.  No readable files means unknown, which callers fail closed on.
    """

    sysfs_root = _cfg_path(cfg, "sysfs_root", Path("/sys"))
    base = sysfs_root / "class" / "power_supply"
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError:
        return None

    valid_offline = False
    uncertain = False
    for supply in entries:
        online = supply / "online"
        try:
            if not online.is_file():
                continue
        except OSError:
            uncertain = True
            continue
        try:
            value = online.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            uncertain = True
            continue
        if value == "1":
            return True
        if value == "0":
            valid_offline = True
            continue
        # A readable but malformed sysfs value is unknown, not proof that the
        # machine is running on battery.
        uncertain = True
    if uncertain:
        return None
    return False if valid_offline else None


def ac_should_pause(ac_online: bool | None, cfg: GuardConfig) -> bool:
    """Whether AC state should pause, honouring ``pause_on_battery``."""

    ac_online, cfg = _normalise_ac_args(ac_online, cfg)
    if not _coerce_bool(
        _cfg_get(cfg, "pause_on_battery", True), True
    ):
        return False
    return _normalise_ac_state(ac_online) is not True


def ac_should_resume(ac_online: bool | None, cfg: GuardConfig) -> bool:
    """Whether AC state permits resuming."""

    ac_online, cfg = _normalise_ac_args(ac_online, cfg)
    if not _coerce_bool(
        _cfg_get(cfg, "pause_on_battery", True), True
    ):
        return True
    return _normalise_ac_state(ac_online) is True


def _normalise_ac_state(state: Any) -> bool | None:
    """Normalise bool-like AC readings; unknown values stay unknown."""

    if isinstance(state, bool):
        return state
    if isinstance(state, int):
        if state == 1:
            return True
        if state == 0:
            return False
        return None
    if state is None:
        return None
    text = str(state).strip().lower()
    if text in {"1", "true", "online", "yes"}:
        return True
    if text in {"0", "false", "offline", "no"}:
        return False
    return None


def _normalise_ac_args(first: Any, second: Any) -> tuple[Any, Any]:
    """Accept either ``(state, cfg)`` or ``(cfg, state)``."""

    if _looks_like_config(first) and not _looks_like_config(second):
        return second, first
    return first, second


def _looks_like_config(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "pause_on_battery" in value
    return hasattr(value, "pause_on_battery")


def _ac_reason(ac_online: bool | None, cfg: GuardConfig) -> str | None:
    if not _coerce_bool(_cfg_get(cfg, "pause_on_battery", True), True):
        return None
    state = _normalise_ac_state(ac_online)
    if state is True:
        return None
    if state is False:
        return "on-battery"
    return "no-ac-source"


# ---------------------------------------------------------------------------
# [SAF-9] Jellyfin transcoding detection


def _resolve_uid(user: Any) -> int | None:
    if isinstance(user, bool):
        return None
    if isinstance(user, int):
        return user
    text = str(user).strip()
    if text.isdigit():
        return int(text)
    if pwd is None:  # pragma: no cover - non-POSIX
        return None
    try:
        return int(pwd.getpwnam(text).pw_uid)
    except (KeyError, OSError):
        return None


def _status_uid(status_text: str) -> int | None:
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            fields = line.split()[1:]
            if not fields:
                return None
            try:
                return int(fields[0])
            except ValueError:
                return None
    return None


def _cmdline_has_ffmpeg(raw: bytes) -> bool:
    for part in raw.split(b"\0"):
        if not part:
            continue
        argument = os.fsdecode(part).strip().lower()
        if "ffmpeg" in argument:
            return True
    return False


def jellyfin_transcoding(cfg: GuardConfig, *, proc_root: Path | None = None) -> bool:
    """Detect a jellyfin-owned process whose command line includes ffmpeg.

    Only procfs is read; no shell is used and no process is signalled.  If the
    proc filesystem or the Jellyfin uid cannot be read, the result is
    conservative: True when ``pause_when_jellyfin_transcoding`` is enabled.
    Per-process ``PermissionError`` is ignored as required.
    """

    pause_enabled = _coerce_bool(
        _cfg_get(cfg, "pause_when_jellyfin_transcoding", True), True
    )
    root = (
        Path(proc_root)
        if proc_root is not None
        else _cfg_path(cfg, "proc_root", Path("/proc"))
    )
    uid = _resolve_uid(_cfg_get(cfg, "jellyfin_user", DEFAULT_JELLYFIN_USER))
    if uid is None:
        log.warning(
            "cannot resolve Jellyfin user %r; treating transcode state as unknown",
            _cfg_get(cfg, "jellyfin_user", DEFAULT_JELLYFIN_USER),
        )
        return pause_enabled

    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        log.warning("cannot scan proc root %s: %s; failing closed", root, exc)
        return pause_enabled

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status_text = (entry / "status").read_text(
                encoding="utf-8", errors="replace"
            )
        except PermissionError:
            continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            continue
        if _status_uid(status_text) != uid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except PermissionError:
            log.warning("cannot read cmdline for jellyfin process %s", entry)
            return pause_enabled
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            log.warning("cannot read cmdline for %s: %s; failing closed", entry, exc)
            return pause_enabled
        if _cmdline_has_ffmpeg(cmdline):
            return True
    return False


# ---------------------------------------------------------------------------
# [SAF-9] Thermal helpers (pure; they never signal processes)


def _elapsed_seconds(above_since: Any) -> float | None:
    """Interpret an ``above_since`` marker.

    Accepted forms are an elapsed-seconds number, a ``timedelta``, a wall-clock
    datetime, an epoch timestamp, or a ``time.monotonic()`` timestamp.  Values
    below one million are elapsed seconds for compatibility with the original
    helper; larger values are timestamps, which keeps the helper usable both
    from tests and from the monitor.
    """

    if above_since is None or isinstance(above_since, bool):
        return None
    if isinstance(above_since, timedelta):
        return above_since.total_seconds()
    if isinstance(above_since, datetime):
        now = datetime.now(above_since.tzinfo)
        return (now - above_since).total_seconds()
    if isinstance(above_since, (int, float)):
        value = float(above_since)
        if not math.isfinite(value):
            return None
        if value > 1e12:
            return time.time() - value / 1000.0
        if value >= 1e9:
            return time.time() - value
        if value >= 1_000_000:
            return time.monotonic() - value
        return value
    return None


def thermal_should_pause(
    current_c: float | None,
    cfg: GuardConfig,
    above_since: Any,
    *,
    elapsed_seconds: float | None = None,
) -> bool:
    """True only at/above ``thermal_pause_c`` for the sustain period.

    ``elapsed_seconds`` is the unambiguous value used by monotonic callers.
    The legacy ``above_since`` forms remain accepted for compatibility.
    """

    if current_c is None:
        return False
    current = _number(current_c, float("nan"))
    if not math.isfinite(current):
        return False
    pause_c = _number(
        _cfg_get(cfg, "thermal_pause_c", DEFAULT_THERMAL_PAUSE_C),
        DEFAULT_THERMAL_PAUSE_C,
    )
    sustain = _number(
        _cfg_get(cfg, "thermal_sustain_seconds", DEFAULT_THERMAL_SUSTAIN_SECONDS),
        DEFAULT_THERMAL_SUSTAIN_SECONDS,
    )
    if current < pause_c:
        return False
    if sustain <= 0:
        return True
    elapsed = (
        max(0.0, float(elapsed_seconds))
        if elapsed_seconds is not None
        else _elapsed_seconds(above_since)
    )
    return elapsed is not None and elapsed >= sustain


def thermal_should_resume(current_c: float | None, cfg: GuardConfig) -> bool:
    """True at or below ``thermal_resume_c`` (hysteresis is intentional)."""

    if current_c is None:
        return False
    current = _number(current_c, float("nan"))
    if not math.isfinite(current):
        return False
    resume_c = _number(
        _cfg_get(cfg, "thermal_resume_c", DEFAULT_THERMAL_RESUME_C),
        DEFAULT_THERMAL_RESUME_C,
    )
    return current <= resume_c


class MachineProtectionMonitor:
    """Compose AC and thermal conditions into one typed monitor.

    AC and thermal policy remain separate: ``on-battery``/``no-ac-source``
    never masquerade as a thermal condition and vice versa.  Unknown AC state
    fails closed when ``pause_on_battery`` is enabled.

    The monitor never clears a caller-owned pause event.  An external user
    pause and a machine pause therefore compose without either owner being
    able to erase the other.  Set ``defer_on_protection`` for bounded probes
    (calibration and capability tests): those operations cannot wait for an
    unplugged charger indefinitely and must return a typed deferral instead.
    """

    def __init__(
        self,
        config: Any,
        *,
        cancel_event: Any = None,
        pause_event: Any = None,
        defer_on_protection: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = GuardConfig.from_config(config)
        self.cancel_event = cancel_event
        # Retained for API compatibility.  The monitor deliberately does not
        # mutate it because it may contain an unrelated user-pause request.
        self.pause_event = pause_event
        self.defer_on_protection = bool(defer_on_protection)
        self.clock = clock
        self.above_since: float | None = None
        self.thermal_active = False
        self.sensor: Any = None
        self.sensor_checked = False

    def _event_set(self, event: Any) -> bool:
        if event is None:
            return False
        try:
            return bool(event.is_set())
        except Exception:
            return False

    def _thermal_reason(self) -> str | None:
        if str(_cfg_get(self.cfg, "temp_sensor", "auto")).strip().casefold() == "none":
            return None
        try:
            if not self.sensor_checked:
                self.sensor = discover_temp_sensor(self.cfg)
                self.sensor_checked = True
            current = read_package_temp(self.sensor) if self.sensor else None
            if current is None:
                return "thermal-sensor-missing"
            if thermal_should_resume(current, self.cfg):
                self.thermal_active = False
                self.above_since = None
                return None
            if self.thermal_active:
                return "thermal"
            if current >= float(_cfg_get(self.cfg, "thermal_pause_c", DEFAULT_THERMAL_PAUSE_C)):
                if self.above_since is None:
                    self.above_since = float(self.clock())
                elapsed = max(0.0, float(self.clock()) - self.above_since)
                if thermal_should_pause(
                    current, self.cfg, self.above_since, elapsed_seconds=elapsed
                ):
                    self.thermal_active = True
                    return "thermal"
            return None
        except Exception:
            return "thermal-sensor-error"

    def __call__(self) -> ProtectionDecision:
        if self._event_set(self.cancel_event):
            return ProtectionDecision(
                ProtectionAction.TERMINATE,
                ("user-cancelled",),
                detail="operation cancelled",
                kind="cancelled",
            )
        reasons: list[str] = []
        ac_reason = _ac_reason(read_ac_state(self.cfg), self.cfg)
        if ac_reason is not None:
            reasons.append(ac_reason)
        thermal_reason = self._thermal_reason()
        if thermal_reason is not None:
            reasons.append(thermal_reason)
        if reasons:
            action = (
                ProtectionAction.DEFER
                if self.defer_on_protection
                else ProtectionAction.PAUSE
            )
            detail = (
                "machine protection deferred the operation"
                if action is ProtectionAction.DEFER
                else "machine protection requires a pause"
            )
            return ProtectionDecision(
                action,
                tuple(reasons),
                detail=detail,
                kind="protection",
            )
        return ProtectionDecision(ProtectionAction.CONTINUE)


# ---------------------------------------------------------------------------
# [SCHED-1] Schedule window helpers


def _parse_clock(value: Any) -> clock_time:
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, clock_time):
        return value.replace(tzinfo=None)
    if isinstance(value, bool):
        raise ValueError(f"invalid window time: {value!r}")
    if isinstance(value, int):
        # presets.parse_window() returns minutes since midnight.
        if 0 <= value < 24 * 60:
            return clock_time(value // 60, value % 60)
        raise ValueError(f"invalid window minute: {value!r}")
    if isinstance(value, str):
        text = value.strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            second = int(match.group(3) or 0)
            return clock_time(hour, minute, second)
        match = re.fullmatch(r"(\d{1,2})", text)
        if match:
            return clock_time(int(match.group(1)), 0)
    raise ValueError(f"invalid window time: {value!r}")


def _parse_window(window: Any) -> tuple[clock_time, clock_time] | None:
    if window is None or window is False:
        return None
    if isinstance(window, (tuple, list)) and not window:
        return None
    if isinstance(window, Mapping) and not window:
        return None
    if isinstance(window, str):
        text = window.strip()
        if not text:
            return None
        match = re.fullmatch(
            r"(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)",
            text,
        )
        if match is None:
            raise ValueError(f"invalid window: {window!r}")
        return _parse_clock(match.group(1)), _parse_clock(match.group(2))
    if isinstance(window, Mapping):
        for start_key, close_key in (
            ("start", "end"),
            ("start", "close"),
            ("open", "close"),
            ("from", "to"),
            ("start_time", "end_time"),
        ):
            if start_key in window and close_key in window:
                return _parse_clock(window[start_key]), _parse_clock(window[close_key])
        raise ValueError(f"invalid window mapping: {window!r}")
    if hasattr(window, "start") and hasattr(window, "end"):
        return _parse_clock(window.start), _parse_clock(window.end)
    try:
        start, close = window
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid window: {window!r}") from exc
    return _parse_clock(start), _parse_clock(close)


def _as_datetime(now: Any) -> datetime:
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    if isinstance(now, clock_time):
        return datetime.combine(date.today(), now.replace(tzinfo=None))
    if isinstance(now, str):
        return datetime.combine(date.today(), _parse_clock(now))
    raise TypeError(f"invalid now value: {now!r}")


def window_contains(window: Any, now: datetime | clock_time | str | None = None) -> bool:
    """Whether ``now`` is inside the window; no window means always."""

    bounds = _parse_window(window)
    if bounds is None:
        return True
    start, close = bounds
    current = _as_datetime(now).time()
    if start == close:
        # A zero-length window never opens.  presets.parse_window accepts
        # equal endpoints, so fail closed rather than treating it as 24 hours.
        return False
    if start < close:
        return start <= current < close
    # Overnight window, e.g. 22:00 -> 07:00.
    return current >= start or current < close


def window_close(
    window: Any, now: datetime | clock_time | str | None = None
) -> datetime | None:
    """Return the next close instant after ``now``, or None without a window."""

    bounds = _parse_window(window)
    if bounds is None:
        return None
    _start, close = bounds
    current = _as_datetime(now)
    closing = datetime.combine(current.date(), close, tzinfo=current.tzinfo)
    if closing <= current:
        closing += timedelta(days=1)
    return closing


def estimate_fits(
    estimate_seconds: float | None,
    window: Any,
    now: datetime | clock_time | str | None = None,
) -> bool:
    """Whether an estimate completes before the next window close.

    No window means it fits.  A missing/negative estimate cannot be proven to
    fit and therefore returns False.
    """

    if _parse_window(window) is None:
        return True
    if estimate_seconds is None:
        return False
    try:
        estimate = float(estimate_seconds)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(estimate) or estimate < 0:
        return False
    current = _as_datetime(now)
    closing = window_close(window, current)
    if closing is None:
        return True
    return current + timedelta(seconds=estimate) < closing


# ---------------------------------------------------------------------------
# Start and mid-encode gates


def start_gate(
    cfg: GuardConfig,
    *,
    source_size: int,
    estimate_seconds: float | None = None,
    window: Any = None,
    now: datetime | clock_time | str | None = None,
    speed_known: bool = True,
    temp_sensor: SensorInfo | None = None,
    user_paused: bool = False,
    extra_reasons: Iterable[str] = (),
    disk_usage: Callable[[Any], Any] | None = None,
) -> GuardDecision:
    """Evaluate every start gate in a deterministic order.

    Gates: user pause, Jellyfin transcoding, AC power, start temperature, free
    space/trash, caller-supplied reasons (for example ``ffmpeg-changed``) and
    the schedule window.  ``run --force`` is the caller's override and is not
    implemented here.
    """

    builder = _DecisionBuilder()

    if user_paused:
        builder.add("user-paused")

    if _coerce_bool(
        _cfg_get(cfg, "pause_when_jellyfin_transcoding", True), True
    ) and jellyfin_transcoding(cfg):
        builder.add("jellyfin-transcoding")

    if _coerce_bool(_cfg_get(cfg, "pause_on_battery", True), True):
        ac_reason = _ac_reason(read_ac_state(cfg), cfg)
        if ac_reason is not None:
            builder.add(ac_reason)

    setting = str(_cfg_get(cfg, "temp_sensor", "auto")).strip().casefold()
    if setting != "none":
        sensor = temp_sensor if temp_sensor is not None else discover_temp_sensor(cfg)
        if sensor is None:
            builder.add("temp-sensor-missing")
        elif not sensor.disabled:
            current = read_package_temp(sensor)
            if current is None:
                builder.add("temp-sensor-missing")
            else:
                start_below = _number(
                    _cfg_get(
                        cfg,
                        "start_if_temp_below_c",
                        DEFAULT_START_IF_TEMP_BELOW_C,
                    ),
                    DEFAULT_START_IF_TEMP_BELOW_C,
                )
                if current >= start_below:
                    builder.add(
                        "too-hot",
                        f"package temperature {current:.1f} C is at or above "
                        f"the {start_below:.1f} C start limit",
                    )

    _space_status, space_decision = check_space(
        cfg, source_size=source_size, disk_usage=disk_usage
    )
    builder.extend(space_decision)

    if extra_reasons is None:
        extra_reasons = ()
    for reason in (extra_reasons,) if isinstance(extra_reasons, str) else extra_reasons:
        if reason is None:
            continue
        text = str(reason).strip()
        if text:
            builder.add(text)

    if _parse_window(window) is not None:
        if not speed_known or estimate_seconds is None:
            builder.add("estimate-cold-start")
        elif not estimate_fits(estimate_seconds, window, now):
            builder.add("estimate-exceeds-window")
        if not window_contains(window, now):
            builder.add("outside-window")

    return builder.build()


def mid_encode_gate(
    cfg: GuardConfig,
    *,
    source_size: int,
    remaining_work: int | None = None,
    output_consumed: int = 0,
    emergency_low_water: int | None = None,
    disk_usage: Callable[[Any], Any] | None = None,
) -> GuardDecision:
    """Mid-encode watchdog gate: remaining work/space and AC only.

    ``remaining_work`` is the net number of bytes still to be written;
    ``output_consumed`` is observational and is not subtracted again.  The
    emergency low-water reserve is additive.  Thermal handling and
    SIGSTOP/SIGCONT belong to the coarse monitor, not here.  Trash is still
    checked and is never purged.
    """

    builder = _DecisionBuilder()

    _space_status, space_decision = check_space(
        cfg,
        source_size=source_size,
        remaining_work=remaining_work,
        output_consumed=output_consumed,
        emergency_low_water=emergency_low_water,
        mid_encode=True,
        disk_usage=disk_usage,
    )
    builder.extend(space_decision)

    if _coerce_bool(_cfg_get(cfg, "pause_on_battery", True), True):
        ac_reason = _ac_reason(read_ac_state(cfg), cfg)
        if ac_reason is not None:
            builder.add(ac_reason)

    return builder.build()
