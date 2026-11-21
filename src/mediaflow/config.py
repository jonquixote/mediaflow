"""Configuration and secret loading for mediaflow.

The configuration file follows SPEC v1.0 section 10.  It is *not* a place for
secrets: the TMDb read token and the notification URL live only in
``tmdb.env`` and ``notify.env`` (SAF-10).  Loading a config that mentions them
is therefore a hard error.

``MEDIAFLOW_ROOT`` exists for tests and sandboxes.  When it is set it always
wins over ``root`` in the file and the production media root is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Any, Iterable, Mapping

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_PRESETS_DIR",
    "DEFAULT_ROOT",
    "DEFAULT_RUNTIME_DIR",
    "DEFAULT_STATE_DIR",
    "REDACTED",
    "Config",
    "ConfigError",
    "Secrets",
    "config_mtime_fingerprint",
    "load_config",
    "load_secrets",
    "redact",
]

DEFAULT_ROOT = Path("/srv/media")
DEFAULT_STATE_DIR = Path("/var/lib/mediaflow")
DEFAULT_RUNTIME_DIR = Path("/run/mediaflow")
DEFAULT_CONFIG_PATH = Path("/etc/mediaflow/config.toml")
DEFAULT_PRESETS_DIR = Path("/etc/mediaflow/presets.d")
PRODUCTION_ROOT = DEFAULT_ROOT

REDACTED = "***"

PAUSE_METHODS = frozenset({"sigstop", "idle_after_job"})
TEMP_SENSORS = frozenset({"auto", "none"})

#: Directory names below the pipeline root.  ``convert`` is the directory that
#: contains one sub-directory per preset (``_pipeline/convert/movies``).
_PIPELINE_DIRS = ("inbox", "work", "review", "failed", "trash")


class ConfigError(ValueError):
    """Raised when ``config.toml`` is missing, malformed or unsafe."""


def redact(value: str | None) -> str:
    """Return a log-safe representation of a secret.

    Nothing from the original value (not even its length or first characters)
    is retained.  Empty and missing values become an empty string so callers
    can distinguish "not configured" from "configured but hidden".
    """

    if value is None or value == "":
        return ""
    return REDACTED


@dataclass(frozen=True, slots=True)
class Secrets:
    """The two optional secrets, loaded only from their env files.

    ``token`` is the TMDb v4 read token and ``url`` is the notification
    webhook/ntfy URL.  The field names are deliberately generic; the
    ``tmdb_token`` and ``notify_url`` aliases exist for readability at call
    sites.  The custom repr/str never exposes either value.
    """

    token: str | None = None
    url: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def tmdb_token(self) -> str | None:
        return self.token

    @property
    def tmdb_read_token(self) -> str | None:
        return self.token

    @property
    def notify_url(self) -> str | None:
        return self.url

    @property
    def notification_url(self) -> str | None:
        return self.url

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def has_notify_url(self) -> bool:
        return bool(self.url)

    def __repr__(self) -> str:
        # SAF-10: never let the dataclass repr leak a token or URL.
        return (
            "Secrets("
            f"token={redact(self.token)!r}, "
            f"url={redact(self.url)!r}, "
            f"warnings={len(self.warnings)})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class Config:
    """Validated configuration with all paths derived from ``root``.

    Defaults are exactly the production defaults in SPEC section 10.  The
    active config is created by :func:`load_config`; constructing directly is
    supported for tests, but no range validation is repeated there.
    """

    # --- section 10 -----------------------------------------------------
    root: Path = DEFAULT_ROOT
    ffmpeg: str = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
    ffprobe: str = "/usr/lib/jellyfin-ffmpeg/ffprobe"
    dry_run: bool = True  # SAF-11: fresh installs are dry-run first.
    poll_seconds: int = 30
    stable_seconds: int = 120
    trash_retention_days: int = 14
    failed_retention_days: int = 30
    min_free_gb: float = 40.0
    root_min_free_gb: float = 2.0
    max_trash_gb: float = 120.0
    space_watchdog_seconds: int = 60
    append_tmdbid: bool = False
    pause_when_jellyfin_transcoding: bool = True
    pause_on_battery: bool = True
    pause_method: str = "sigstop"
    temp_sensor: str = "auto"
    start_if_temp_below_c: float = 65.0
    thermal_pause_c: float = 85.0
    thermal_resume_c: float = 75.0
    progress_seconds: int = 30
    stall_minutes: int = 10
    max_restarts: int = 3
    min_output_kbps: int = 300
    min_bpp: float = 0.01
    review_reminder_days: int = 3
    notify_cooldown_hours: int = 6
    jellyfin_user: str = "jellyfin"

    # --- internal sandbox/deployment overrides --------------------------
    state_dir: Path = DEFAULT_STATE_DIR
    runtime_dir: Path = DEFAULT_RUNTIME_DIR

    def __post_init__(self) -> None:
        # Normalise the path fields even when constructed directly.  Keep the
        # loader's absolute-path checks as the authoritative guard.
        for name in ("root", "state_dir", "runtime_dir"):
            value = getattr(self, name)
            if isinstance(value, Path):
                continue
            if isinstance(value, (str, os.PathLike)):
                object.__setattr__(self, name, Path(value))
            else:  # pragma: no cover - defensive, loader rejects this first
                raise TypeError(f"{name} must be a path, not {type(value).__name__}")
        for name in ("ffmpeg", "ffprobe"):
            value = getattr(self, name)
            if isinstance(value, Path):
                object.__setattr__(self, name, str(value))

    # --- derived pipeline paths -----------------------------------------
    @property
    def movies(self) -> Path:
        return self.root / "movies"

    @property
    def library(self) -> Path:
        return self.movies

    @property
    def library_dir(self) -> Path:
        return self.movies

    @property
    def pipeline(self) -> Path:
        return self.root / "_pipeline"

    @property
    def inbox(self) -> Path:
        return self.pipeline / "inbox"

    @property
    def convert_root(self) -> Path:
        return self.pipeline / "convert"

    @property
    def convert(self) -> Path:
        """The folder holding one sub-directory per preset id."""

        return self.convert_root / "movies"

    @property
    def convert_movies(self) -> Path:
        return self.convert

    @property
    def work(self) -> Path:
        return self.pipeline / "work"

    @property
    def review(self) -> Path:
        return self.pipeline / "review"

    @property
    def failed(self) -> Path:
        return self.pipeline / "failed"

    @property
    def trash(self) -> Path:
        return self.pipeline / "trash"

    # --- state and runtime paths ----------------------------------------
    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backup"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def capabilities_path(self) -> Path:
        return self.state_dir / "capabilities.json"

    @property
    def status_path(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def progress_path(self) -> Path:
        return self.state_dir / "progress.json"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "daemon.lock"

    # --- helpers ---------------------------------------------------------
    def path_map(self) -> dict[str, Path]:
        """Named paths in a stable order, useful for ``doctor`` output."""

        return {
            "root": self.root,
            "movies": self.movies,
            "pipeline": self.pipeline,
            "inbox": self.inbox,
            "convert": self.convert,
            "work": self.work,
            "review": self.review,
            "failed": self.failed,
            "trash": self.trash,
            "state_dir": self.state_dir,
            "backup_dir": self.backup_dir,
            "log_dir": self.log_dir,
            "db": self.db_path,
            "capabilities": self.capabilities_path,
            "status": self.status_path,
            "progress": self.progress_path,
            "runtime_dir": self.runtime_dir,
            "lock": self.lock_path,
        }

    def paths(self) -> list[Path]:
        """All managed paths, for free-space and permission checks."""

        return list(self.path_map().values())

    def ensure_directories(self) -> list[Path]:
        """Create the pipeline/state/runtime directories with mode 02775.

        The experimental preset folder is intentionally not part of this list:
        ``install.sh`` must never create it.  Returns the directories touched.

        Existing directories have their mode normalised to setgid 02775 as the
        SPEC requires.  Symlinked managed directories are refused rather than
        chmod'ing an unexpected target.
        """

        # A pre-existing ``root`` may be a mount point with its own ownership
        # policy; only give a newly created root the setgid mode.  The managed
        # children below are always normalised.
        root_existed = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ConfigError(f"root must not be a symlink: {self.root}")
        if not root_existed:
            os.chmod(self.root, 0o2775)

        managed = [
            self.movies,
            self.pipeline,
            self.inbox,
            self.convert_root,
            self.convert,
            self.work,
            self.review,
            self.failed,
            self.trash,
            self.state_dir,
            self.backup_dir,
            self.log_dir,
            self.runtime_dir,
        ]
        seen: set[Path] = set()
        touched: list[Path] = []
        for directory in sorted(managed, key=lambda p: (len(p.parts), str(p))):
            if directory in seen:
                continue
            seen.add(directory)
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise ConfigError(f"managed directory must not be a symlink: {directory}")
            os.chmod(directory, 0o2775)
            touched.append(directory)
        return touched


# ---------------------------------------------------------------------------
# config.toml loading
# ---------------------------------------------------------------------------

_BOOL_FIELDS = frozenset(
    {
        "dry_run",
        "append_tmdbid",
        "pause_when_jellyfin_transcoding",
        "pause_on_battery",
    }
)
_INT_FIELDS = frozenset(
    {
        "poll_seconds",
        "stable_seconds",
        "trash_retention_days",
        "failed_retention_days",
        "space_watchdog_seconds",
        "progress_seconds",
        "stall_minutes",
        "max_restarts",
        "min_output_kbps",
        "review_reminder_days",
        "notify_cooldown_hours",
    }
)
_NUMBER_FIELDS = frozenset(
    {
        "min_free_gb",
        "root_min_free_gb",
        "max_trash_gb",
        "start_if_temp_below_c",
        "thermal_pause_c",
        "thermal_resume_c",
        "min_bpp",
    }
)
_STRING_FIELDS = frozenset(
    {
        "ffmpeg",
        "ffprobe",
        "pause_method",
        "temp_sensor",
        "jellyfin_user",
    }
)
_PATH_FIELDS = frozenset({"root", "state_dir", "runtime_dir"})
_CONFIG_KEYS = frozenset(field.name for field in fields(Config))

# Keep this explicit so a new Config field cannot silently be accepted by the
# loader without a type/range rule.
if _CONFIG_KEYS != (
    _BOOL_FIELDS
    | _INT_FIELDS
    | _NUMBER_FIELDS
    | _STRING_FIELDS
    | _PATH_FIELDS
):  # pragma: no cover - programming error
    raise RuntimeError("config field validation rules are incomplete")

_SECRET_KEY_NAMES = frozenset(
    {
        "token",
        "apikey",
        "api_key",
        "secret",
        "password",
        "tmdbreadtoken",
        "tmdbtoken",
        "tmdbapitoken",
        "tmdbapikey",
        "tmdburl",
        "notifyurl",
        "notificationurl",
        "webhookurl",
        "ntfyurl",
        "ntfytopic",
        "url",
    }
)
_SECRET_HINTS = ("token", "apikey", "secret", "password", "credential")
_URL_HINTS = ("url", "webhook", "ntfy")


def _normalise_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _looks_like_secret_key(name: str) -> bool:
    normalised = _normalise_key(name)
    if normalised in {_normalise_key(k) for k in _SECRET_KEY_NAMES}:
        return True
    if any(hint in normalised for hint in _SECRET_HINTS):
        return True
    if "tmdb" in normalised and any(hint in normalised for hint in _URL_HINTS):
        return True
    if any(service in normalised for service in ("notify", "webhook", "ntfy")):
        if any(hint in normalised for hint in _URL_HINTS):
            return True
    return False


def _secret_paths(value: Any, prefix: tuple[str, ...] = ()) -> Iterable[tuple[str, ...]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            path = prefix + (key_text,)
            if _looks_like_secret_key(key_text):
                yield path
            if isinstance(child, Mapping):
                yield from _secret_paths(child, path)


def _canonical_path(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(value))))
    )


def _refuse_production_root(root: Path) -> None:
    """Refuse a MEDIAFLOW_ROOT that names or resolves to /srv/media.

    The lexical compare avoids opening anything.  The realpath compare catches
    a sandbox path symlinked at the production library without reading media.
    """

    production = Path(_canonical_path(PRODUCTION_ROOT))

    def is_production(path_text: str) -> bool:
        candidate = Path(path_text)
        try:
            return candidate == production or candidate.is_relative_to(production)
        except ValueError:  # pragma: no cover - different drives on Windows
            return False

    if is_production(_canonical_path(root)):
        raise ConfigError(
            "MEDIAFLOW_ROOT resolves inside /srv/media; refusing to use the "
            "production media root in sandbox mode"
        )
    try:
        real = _canonical_path(os.path.realpath(root))
    except OSError:  # pragma: no cover - defensive
        real = _canonical_path(root)
    if is_production(real):
        raise ConfigError(
            "MEDIAFLOW_ROOT resolves inside /srv/media; refusing to use the "
            "production media root in sandbox mode"
        )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        reason = exc.strerror or "read error"
        raise ConfigError(f"cannot read config file {path}: {reason}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # TOMLDecodeError can carry the offending document; never echo it in
        # case the line contained a secret.
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        message = getattr(exc, "msg", "invalid TOML")
        where = f" at line {line}" if line is not None else ""
        if column is not None:
            where += f", column {column}"
        raise ConfigError(f"invalid TOML in {path}{where}: {message}") from None
    if not isinstance(data, dict):  # pragma: no cover - tomllib always returns dict
        raise ConfigError(f"invalid config in {path}: top level must be a table")
    return data


def _validate_raw_config(raw: Mapping[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(raw) - _CONFIG_KEYS)
    if unknown:
        errors.append(
            "unknown config key(s): " + ", ".join(repr(key) for key in unknown)
        )
    for key, value in raw.items():
        if key in _BOOL_FIELDS:
            if type(value) is not bool:
                errors.append(f"{key} must be a boolean")
        elif key in _INT_FIELDS:
            if type(value) is not int:
                errors.append(f"{key} must be an integer")
        elif key in _NUMBER_FIELDS:
            if type(value) is bool or not isinstance(value, (int, float)):
                errors.append(f"{key} must be a number")
            elif not math.isfinite(float(value)):
                errors.append(f"{key} must be finite")
        elif key in _STRING_FIELDS or key in _PATH_FIELDS:
            if not isinstance(value, str):
                errors.append(f"{key} must be a string")
        # Unknown keys were reported above; no further validation possible.
    if errors:
        raise ConfigError(
            f"invalid config {path}:\n  - " + "\n  - ".join(errors)
        )
    return []


def _parse_path(value: str, name: str) -> Path:
    expanded = os.path.expanduser(value)
    if not expanded:
        raise ConfigError(f"{name} must not be empty")
    path = Path(expanded)
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path: {value!r}")
    if "\x00" in value:
        raise ConfigError(f"{name} contains an invalid NUL byte")
    return path


def _validate_config(config: Config) -> list[str]:
    errors: list[str] = []

    def non_negative(name: str, value: float) -> None:
        if not math.isfinite(float(value)) or value < 0:
            errors.append(f"{name} must be >= 0")

    def positive(name: str, value: int | float) -> None:
        if not math.isfinite(float(value)) or value <= 0:
            errors.append(f"{name} must be > 0")

    if not config.ffmpeg:
        errors.append("ffmpeg must not be empty")
    if not config.ffprobe:
        errors.append("ffprobe must not be empty")
    if config.pause_method not in PAUSE_METHODS:
        errors.append(
            f"pause_method must be one of {sorted(PAUSE_METHODS)}, "
            f"got {config.pause_method!r}"
        )
    if config.temp_sensor not in TEMP_SENSORS:
        errors.append(
            f"temp_sensor must be one of {sorted(TEMP_SENSORS)}, "
            f"got {config.temp_sensor!r}"
        )
    if not config.jellyfin_user.strip():
        errors.append("jellyfin_user must not be empty")

    positive("poll_seconds", config.poll_seconds)
    non_negative("stable_seconds", config.stable_seconds)
    non_negative("trash_retention_days", config.trash_retention_days)
    non_negative("failed_retention_days", config.failed_retention_days)
    non_negative("min_free_gb", config.min_free_gb)
    non_negative("root_min_free_gb", config.root_min_free_gb)
    non_negative("max_trash_gb", config.max_trash_gb)
    positive("space_watchdog_seconds", config.space_watchdog_seconds)
    non_negative("start_if_temp_below_c", config.start_if_temp_below_c)
    non_negative("thermal_pause_c", config.thermal_pause_c)
    non_negative("thermal_resume_c", config.thermal_resume_c)
    positive("progress_seconds", config.progress_seconds)
    positive("stall_minutes", config.stall_minutes)
    non_negative("max_restarts", config.max_restarts)
    non_negative("min_output_kbps", config.min_output_kbps)
    non_negative("min_bpp", config.min_bpp)
    non_negative("review_reminder_days", config.review_reminder_days)
    non_negative("notify_cooldown_hours", config.notify_cooldown_hours)
    if config.min_bpp > 10.0:
        errors.append(
            "min_bpp is implausibly large (expected bits per pixel per frame, <= 10)"
        )
    if config.start_if_temp_below_c > 120.0:
        errors.append("start_if_temp_below_c must be <= 120")
    if config.thermal_pause_c > 120.0:
        errors.append("thermal_pause_c must be <= 120")
    if config.thermal_resume_c > 120.0:
        errors.append("thermal_resume_c must be <= 120")
    if config.thermal_resume_c >= config.thermal_pause_c:
        errors.append("thermal_resume_c must be lower than thermal_pause_c")
    if config.pause_method == "sigstop" and config.thermal_resume_c >= config.thermal_pause_c:
        # Covered by the message above; kept separate to make the intent clear.
        pass

    for name in ("root", "state_dir", "runtime_dir"):
        path = getattr(config, name)
        if not path.is_absolute():
            errors.append(f"{name} must be an absolute path: {path}")
    return errors


def load_config(
    path: Path,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Load and validate ``path`` according to SPEC section 10.

    ``MEDIAFLOW_ROOT`` overrides ``root``.  In that mode ``state_dir`` and
    ``runtime_dir`` default to ``<root>/_state`` and ``<root>/_run`` unless the
    config file explicitly provides them.  A sandbox root that resolves to
    ``/srv/media`` is refused (SAF-10/SAF-11 test isolation).
    """

    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    raw = _read_toml(path)

    # SAF-10: a secret key anywhere in config.toml is an error.  Report only
    # the key path, never its value.
    secret_paths = list(_secret_paths(raw))
    if secret_paths:
        rendered = ", ".join(".".join(parts) for parts in secret_paths)
        raise ConfigError(
            "secret key(s) in config.toml: "
            f"{rendered}; secrets belong only in tmdb.env/notify.env (SAF-10)"
        )

    _validate_raw_config(raw, path)
    env = os.environ if environ is None else environ

    # Type/absolute-path checks happen before an env override so a malformed
    # file is never half-accepted.
    file_root = _parse_path(str(raw.get("root", DEFAULT_ROOT)), "root")
    file_state = _parse_path(
        str(raw.get("state_dir", DEFAULT_STATE_DIR)), "state_dir"
    )
    file_runtime = _parse_path(
        str(raw.get("runtime_dir", DEFAULT_RUNTIME_DIR)), "runtime_dir"
    )

    env_root_value = env.get("MEDIAFLOW_ROOT")
    if env_root_value is None or env_root_value == "":
        root = file_root
        state_dir = file_state
        runtime_dir = file_runtime
    else:
        if not isinstance(env_root_value, str):  # pragma: no cover - os.environ
            raise ConfigError("MEDIAFLOW_ROOT must be a string path")
        root = _parse_path(env_root_value, "MEDIAFLOW_ROOT")
        _refuse_production_root(root)
        if "state_dir" in raw:
            state_dir = file_state
        else:
            state_dir = root / "_state"
        if "runtime_dir" in raw:
            runtime_dir = file_runtime
        else:
            runtime_dir = root / "_run"

    values: dict[str, Any] = {}
    for field in fields(Config):
        if field.name in raw:
            values[field.name] = raw[field.name]
    values["root"] = root
    values["state_dir"] = state_dir
    values["runtime_dir"] = runtime_dir

    config = Config(**values)
    errors = _validate_config(config)
    if errors:
        raise ConfigError(
            f"invalid config {path}:\n  - " + "\n  - ".join(errors)
        )
    return config


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

_TOKEN_KEYS = ("TMDB_READ_TOKEN", "TMDB_TOKEN")
_URL_KEYS = ("NOTIFY_URL",)


def _check_secret_permissions(path: Path) -> list[str]:
    warnings: list[str] = []
    try:
        info = path.stat()
    except OSError:
        return warnings
    mode = stat.S_IMODE(info.st_mode)
    # 0640 is the maximum: owner rw, group r, nothing for others and no
    # execute/write bits for group or others.
    forbidden = mode & 0o137
    if forbidden:
        warnings.append(
            f"{path.name}: permissions {mode:04o} are more open than 0640; "
            "recommended mode is 0640"
        )

    allowed_uids = {0, os.geteuid()}
    if info.st_uid not in allowed_uids:
        warnings.append(
            f"{path.name}: owner uid {info.st_uid} is neither root nor the "
            "service user; expected root:mediaflow 0640"
        )
    allowed_gids = {0, os.getegid(), *os.getgroups()}
    if info.st_gid not in allowed_gids:
        warnings.append(
            f"{path.name}: group gid {info.st_gid} is not a group of the "
            "service user; expected root:mediaflow 0640"
        )
    return warnings


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Do not include the contents; there are none in an OSError anyway.
        reason = exc.strerror or "read error"
        raise ConfigError(f"cannot read secrets file {path}: {reason}") from exc

    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ConfigError(
                f"{path.name}: invalid line {lineno}; expected KEY=VALUE"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path.name}: invalid key on line {lineno}")
        value = value.strip()
        if value and value[0] in ("'", '"'):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing != -1:
                value = value[1:closing]
            # No closing quote: leave it verbatim; it is still a usable value
            # and no secret is echoed in errors.
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_secrets(env_dir: Path, *, warnings: list[str] | None = None) -> Secrets:
    """Read optional ``tmdb.env`` and ``notify.env`` from ``env_dir``.

    Permission/ownership problems are reported as non-fatal warnings (also
    stored on the returned :class:`Secrets`).  Missing files are normal: TMDb
    and notifications are optional.  A malformed file is a hard error, but its
    contents are never included in the message.
    """

    env_dir = Path(env_dir)
    collected: list[str] = []
    tmdb_path = env_dir / "tmdb.env"
    notify_path = env_dir / "notify.env"

    token: str | None = None
    url: str | None = None

    if tmdb_path.exists():
        collected.extend(_check_secret_permissions(tmdb_path))
        tmdb = _parse_env_file(tmdb_path)
        for key in _TOKEN_KEYS:
            candidate = tmdb.get(key)
            if candidate:
                token = candidate
                break
        if token == "":
            token = None

    if notify_path.exists():
        collected.extend(_check_secret_permissions(notify_path))
        notify = _parse_env_file(notify_path)
        for key in _URL_KEYS:
            candidate = notify.get(key)
            if candidate:
                url = candidate
                break

    if warnings is not None:
        warnings.extend(collected)
    return Secrets(token=token or None, url=url or None, warnings=tuple(collected))


# ---------------------------------------------------------------------------
# Reload fingerprint
# ---------------------------------------------------------------------------


def _stat_token(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError:
        return "missing"
    kind = "link" if stat.S_ISLNK(info.st_mode) else "node"
    return f"{kind}:{info.st_mtime_ns}:{info.st_size}"


def config_mtime_fingerprint(path: Path, presets_dir: Path) -> str:
    """Return a stable fingerprint of config.toml and the preset tree.

    The daemon compares fingerprints each poll and reloads when this changes.
    Only metadata is read; missing paths are represented by ``missing`` so the
    first creation is also detected.
    """

    config_path = Path(path)
    preset_path = Path(presets_dir)
    entries: list[str] = [f"config:{config_path}:{_stat_token(config_path)}"]
    entries.append(f"presets:{preset_path}:{_stat_token(preset_path)}")

    try:
        candidates = sorted(preset_path.rglob("*"), key=lambda p: str(p))
    except OSError:
        candidates = []
    for candidate in candidates:
        try:
            relative = candidate.relative_to(preset_path)
        except ValueError:  # pragma: no cover - rglob children are relative
            relative = candidate
        entries.append(f"{relative.as_posix()}:{_stat_token(candidate)}")

    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.encode("utf-8", "surrogateescape"))
        digest.update(b"\n")
    return digest.hexdigest()
