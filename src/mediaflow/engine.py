"""The mediaflow state machine.

The engine is deliberately synchronous.  The daemon owns one Engine and one
global flock; CLI writes are commands executed by the daemon.  Every state
transition is committed to SQLite before the human-readable job snapshot is
replaced.  All file moves use :mod:`mediaflow.safety`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .state import (
    ACTIVE_STATES,
    COMMAND_NAMES,
    JobState,
    REVIEW_MOVABLE,
    REVIEW_RESUME,
    TERMINAL_STATES,
    is_review,
    is_terminal,
    values as state_values,
)
from .safety import (
    CrossDeviceError, FileSafety, PathDenied, SafetyError,
    atomic_write_json, check_one_filesystem, fsync_dir, fsync_file,
)
from .supervision import (
    OperationCancelled,
    OperationDeferred,
    OperationInterrupted,
    OperationState,
    ProtectionAction,
    ProtectionDecision,
    SlotBusy,
    SupervisionPolicy,
    processing_slot,
    slot_path,
    supervise_callable,
)
from .util import gzip_text, has_temp_marker, human_bytes, is_real_dir, is_regular_file, sha256_file, tree_stats

log = logging.getLogger(__name__)

# Shared state vocabulary: no duplicated tuples here (STATE-1).
ACTIVE_STATES = state_values(ACTIVE_STATES)
TERMINAL = state_values(TERMINAL_STATES)
# REVIEW_RESUME, REVIEW_MOVABLE and REVIEW_HINTS are imported from the shared
# state model; no duplicated review vocabulary here (STATE-1).
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg",
    ".wmv", ".webm", ".vob", ".flv", ".ogv", ".3gp",
}
TEMP_MARKERS = (".part", ".!qB", ".crdownload", ".aria2", ".tmp", ".filepart")
EXTRAS_RE = re.compile(r"^(extras?|featurettes?|behind[ ._-]?the[ ._-]?scenes|deleted[ ._-]?scenes|"
                       r"interviews?|trailers?|samples?|bonus|other)$", re.I)
MULTIDISC_RE = re.compile(r"(?:^|[ ._(])-(?:cd|disc|disk)[ ._-]?[0-9]{1,2}(?:$|[ ._)])|"
                          r"(?:^|[ ._(])(?:cd|disc|disk)[ ._-]?[0-9]{1,2}(?:$|[ ._)])", re.I)


class EngineError(RuntimeError):
    pass


class ReviewError(EngineError):
    pass


class CommandError(EngineError):
    pass


class EncodeStalled(EngineError):
    """The encoder was alive but made no progress."""


class EncodeCancelled(EngineError):
    """A user cancelled the running encode."""


class EncodeProtectionInterrupted(EngineError):
    """A protection condition stopped an encode without a failure retry."""

    def __init__(self, reasons: tuple[str, ...] = (), message: str | None = None):
        super().__init__(message or "encode interrupted by protection")
        self.reasons = tuple(reasons)


@dataclass
class SourceInspection:
    entry: Path
    kind: str
    main: Path
    probe: Any
    source_size: int
    video_candidates: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _CombinedEvent:
    """A read-only event view over caller cancellation and worker stop.

    The shared supervisor owns its private stop event.  Legacy injected
    workers can only observe an event, so this view lets them react to an
    internally detected stall, timeout or protection interruption without the
    supervisor mutating the caller's cancellation event.
    """

    def __init__(self, *events: Any) -> None:
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(
            bool(event.is_set())
            for event in self._events
            if hasattr(event, "is_set")
        )

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            for event in self._events:
                waiter = getattr(event, "wait", None)
                if callable(waiter):
                    try:
                        if waiter(remaining):
                            return True
                    except TypeError:
                        if waiter():
                            return True
            if deadline is not None and time.monotonic() >= deadline:
                return self.is_set()
            time.sleep(0.01)


@dataclass
class _VerificationWatchResult:
    """Typed result used when a legacy verifier is under outer supervision."""

    state: OperationState
    ok: bool = False
    failures: list[str] = field(default_factory=list)
    quality_floor_breach: bool = False
    sha256: str = ""
    output_size: int = 0
    output_video_bytes: int = 0
    output_duration: float = 0.0
    reasons: tuple[str, ...] = ()
    timeout_type: str | None = None
    interrupted: bool = False
    cancelled: bool = False
    deferred: bool = False
    stalled: bool = False
    worker_done: threading.Event | None = None


class Engine:
    """Owns all pipeline mutations for one daemon process."""

    def __init__(self, config: Any, store: Any, presets: Mapping[str, Any], *,
                 safety: FileSafety | None = None,
                 encoder: Any = None, verifier: Callable | None = None,
                 guards: Any = None, notifier: Any = None,
                 identity_resolver: Callable | None = None,
                 clock: Callable[[], float] = time.time,
                 dry_run_override: bool | None = None,
                  secrets: Any = None,
                   manage_media_dirs: bool = False):
        self.cfg = config
        self.store = store
        self.presets = dict(presets or {})
        self.clock = clock
        self.root = Path(_cfg(config, "root", "/srv/media"))
        self.movies = Path(_cfg(config, "movies", self.root / "movies"))
        self.pipeline = Path(_cfg(config, "pipeline", self.root / "_pipeline"))
        self.convert = Path(_cfg(config, "convert", self.pipeline / "convert"))
        explicit_convert = _cfg(config, "convert_movies", None)
        if explicit_convert is not None:
            self.convert_movies = Path(explicit_convert)
        elif self.convert.name == "movies":
            self.convert_movies = self.convert
        else:
            self.convert_movies = self.convert / "movies"
        self.work = Path(_cfg(config, "work", self.pipeline / "work"))
        self.review = Path(_cfg(config, "review", self.pipeline / "review"))
        self.failed = Path(_cfg(config, "failed", self.pipeline / "failed"))
        self.trash = Path(_cfg(config, "trash", self.pipeline / "trash"))
        self.inbox = Path(_cfg(config, "inbox", self.pipeline / "inbox"))
        self.log_dir = Path(_cfg(config, "log_dir", Path(_cfg(config, "state_dir", "/var/lib/mediaflow")) / "logs"))
        self.status_path = Path(_cfg(config, "status_path", Path(_cfg(config, "state_dir", "/var/lib/mediaflow")) / "status.json"))
        self.progress_path = Path(_cfg(config, "progress_path", Path(_cfg(config, "state_dir", "/var/lib/mediaflow")) / "progress.json"))
        self.capabilities_path = Path(_cfg(config, "capabilities_path",
                                           Path(_cfg(config, "state_dir", "/var/lib/mediaflow")) / "capabilities.json"))
        self.dry_run = bool(_cfg(config, "dry_run", True) if dry_run_override is None else dry_run_override)
        self.safety = safety or FileSafety(self.root, self.work, self.trash)
        self.encoder = encoder
        self.verifier = verifier
        self.guards = guards
        self.notifier = notifier
        self.identity_resolver = identity_resolver
        self.secrets = secrets if secrets is not None else _cfg(config, "secrets", None)
        self._manage_media_dirs = bool(manage_media_dirs)
        self._started = False
        self._user_paused = store.get_setting("user_paused", "0") == "1"
        self._pause_reasons: list[str] = []
        # Machine-wide holds (for example an ffmpeg-upgrade retest) are kept
        # separately from a single job's transient gate reasons.  A review or
        # an unrelated job must never erase a global safety hold.
        self._global_pause_reasons: list[str] = []
        self._progress_last = 0.0
        self._active_cancel = False
        self._cancel_reason: str | None = None
        self._shutdown_requested = False
        self._active_cancel_event: threading.Event | None = None
        self._active_cancel_job_id: str | None = None
        self._operation_state_lock = threading.RLock()
        self._operation_generation = 0
        # A validated configuration generation waiting for a safe boundary.
        # It is applied only when no expensive operation is active.
        self.pending_config: Any | None = None
        self.pending_presets: Mapping[str, Any] | None = None
        self.pending_secrets: Any | None = None
        self._restart_required: tuple[str, ...] = ()
        self._generation_lock = threading.RLock()
        self._deferred_cleanup_lock = threading.Lock()
        self._deferred_cleanups: set[tuple[str, int]] = set()
        self._encode_paused = False
        self._encode_pause_reasons: list[str] = []
        self._encode_progress: dict[str, Any] = {}
        self._encode_progress_fraction = 0.0
        self._encode_partial_bytes = 0
        self._space_checked_at = 0.0
        self._space_checked_monotonic = 0.0
        self._thermal_above_since: float | None = None
        self._encode_thermal_active = False
        self._encode_temp_sensor: Any = None
        self._encode_sensor_checked = False
        self.force_job: str | None = None

    # ------------------------------------------------------------------
    # lifecycle and public entry points
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._ensure_dirs()
        self._check_paths()
        self.recover()
        self._started = True
        self.write_status()

    def _ensure_dirs(self) -> None:
        """Ensure only non-media runtime paths by default.

        Production startup and scanning must never create, chmod or chown a
        library root.  Media-path initialization is a separate, explicitly
        approved step; it is enabled only for isolated scratch installations.
        """

        runtime_paths = (
            self.log_dir, self.status_path.parent, self.progress_path.parent,
            self.capabilities_path.parent,
        )
        for path in runtime_paths:
            path.mkdir(parents=True, exist_ok=True)
        if not self._manage_media_dirs:
            return
        for path in (self.movies, self.inbox, self.convert_movies, self.work,
                     self.review, self.failed, self.trash):
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)

    def _check_paths(self) -> None:
        try:
            check_one_filesystem([self.inbox, self.convert_movies, self.work, self.review, self.failed,
                                  self.trash, self.movies])
        except SafetyError as exc:
            raise EngineError(str(exc)) from exc

    def tick(self, *, once: bool = False, force_job: str | None = None) -> int:
        """Scan once and advance eligible jobs. Returns the number processed."""
        if not self._started:
            self.start()
        self.force_job = force_job
        self.scan()
        processed = 0
        seen: set[str] = set()
        while True:
            job = self._next_job(exclude=seen)
            if job is None:
                break
            seen.add(job["id"])
            before = (job["state"], job.get("updated_at"))
            try:
                self.advance(job["id"])
            except Exception as exc:  # keep the daemon alive; the job records the failure
                log.exception("advance failed job=%s", job["id"])
                self._fail_job(job["id"], "internal-error", detail=str(exc))
            processed += 1
            if self._shutdown_requested:
                # The active child has been terminated and reaped.  Do not
                # start another job during the same daemon tick.
                break
            after = self.store.get_job(job["id"])
            if after and (after["state"], after.get("updated_at")) == before:
                # No progress; gates/pauses will be reflected in status.
                if once:
                    continue
            if processed > 10000:
                break
            if once and processed >= 1000:
                break
        self._update_progress_counts()
        self.write_status()
        if not self._shutdown_requested:
            self.process_commands()
        self.housekeeping()
        return processed

    @property
    def pause_reasons(self) -> list[str]:
        """Return every active pause reason, including machine-wide holds."""

        return sorted(
            set(self._pause_reasons)
            | set(self._global_pause_reasons)
            | set(self._encode_pause_reasons)
            | ({"user-paused"} if self._user_paused else set())
        )

    def set_global_pause_reasons(self, reasons: list[str] | tuple[str, ...]) -> None:
        """Set machine-wide holds without disturbing per-job pause reasons."""

        self._global_pause_reasons = sorted({
            str(reason) for reason in reasons if str(reason).strip()
        })

    def add_global_pause_reasons(self, reasons: list[str] | tuple[str, ...]) -> None:
        """Add machine-wide holds while preserving unrelated holds."""

        self.set_global_pause_reasons([
            *self._global_pause_reasons,
            *(str(reason) for reason in reasons),
        ])

    def clear_global_pause_reasons(self, *prefixes: str) -> None:
        """Clear matching machine-wide holds, preserving unrelated reasons."""

        if not prefixes:
            self._global_pause_reasons = []
            return
        self._global_pause_reasons = [
            reason for reason in self._global_pause_reasons
            if not any(reason.startswith(prefix) for prefix in prefixes)
        ]

    def set_restart_required(self, reasons: Iterable[str]) -> None:
        """Record and expose keys that require a daemon restart."""

        with self._generation_lock:
            self._restart_required = tuple(sorted({
                str(reason) for reason in reasons if str(reason).strip()
            }))

    def apply_pending_config(self) -> bool:
        """Atomically publish a validated configuration generation.

        The swap is deferred while an expensive operation owns the operation
        boundary, so a reload can never change an in-flight policy.  All derived
        path/safety values are rebuilt from the same generation before the new
        config becomes visible.
        """

        with self._generation_lock:
            with self._operation_state_lock:
                if self._active_cancel_event is not None:
                    return False
                if self.pending_config is None:
                    return True
                new_cfg = self.pending_config
                new_presets = self.pending_presets
                new_secrets = self.pending_secrets
                self.pending_config = None
                self.pending_presets = None
                self.pending_secrets = None
            self.cfg = new_cfg
            if new_presets is not None:
                self.presets = dict(new_presets)
            if new_secrets is not None:
                self.secrets = new_secrets
            self.root = Path(_cfg(new_cfg, "root", self.root))
            self.movies = Path(_cfg(new_cfg, "movies", self.root / "movies"))
            self.pipeline = Path(_cfg(new_cfg, "pipeline", self.root / "_pipeline"))
            self.convert = Path(_cfg(new_cfg, "convert", self.pipeline / "convert"))
            explicit_convert = _cfg(new_cfg, "convert_movies", None)
            if explicit_convert is not None:
                self.convert_movies = Path(explicit_convert)
            elif self.convert.name == "movies":
                self.convert_movies = self.convert
            else:
                self.convert_movies = self.convert / "movies"
            self.work = Path(_cfg(new_cfg, "work", self.pipeline / "work"))
            self.review = Path(_cfg(new_cfg, "review", self.pipeline / "review"))
            self.failed = Path(_cfg(new_cfg, "failed", self.pipeline / "failed"))
            self.trash = Path(_cfg(new_cfg, "trash", self.pipeline / "trash"))
            self.inbox = Path(_cfg(new_cfg, "inbox", self.pipeline / "inbox"))
            self.log_dir = Path(_cfg(new_cfg, "log_dir",
                                    Path(_cfg(new_cfg, "state_dir", "/var/lib/mediaflow")) / "logs"))
            self.status_path = Path(_cfg(new_cfg, "status_path",
                                        Path(_cfg(new_cfg, "state_dir", "/var/lib/mediaflow")) / "status.json"))
            self.progress_path = Path(_cfg(new_cfg, "progress_path",
                                          Path(_cfg(new_cfg, "state_dir", "/var/lib/mediaflow")) / "progress.json"))
            self.capabilities_path = Path(_cfg(new_cfg, "capabilities_path",
                                              Path(_cfg(new_cfg, "state_dir", "/var/lib/mediaflow")) / "capabilities.json"))
            self.dry_run = bool(_cfg(new_cfg, "dry_run", True))
            self.safety = FileSafety(self.root, self.work, self.trash)
            self._user_paused = self.store.get_setting("user_paused", "0") == "1"
        self.write_status()
        return True

    def request_shutdown(self) -> None:
        """Stop the active expensive operation without failing its job.

        The daemon uses this for SIGTERM/SIGINT.  The operation monitor reports
        a typed shutdown interruption, its child is reaped, and the job remains
        in a recoverable state for the next daemon start.  The caller's
        cancellation event is deliberately not set: that event means a user
        cancellation, while shutdown is identified by its typed reason.

        The first accepted terminal intent wins.  A user cancellation accepted
        before shutdown is not relabelled as a daemon stop, and shutdown that
        arrives first cannot be overwritten by a later cancel command.
        """

        with self._operation_state_lock:
            self._shutdown_requested = True
            if not self._active_cancel:
                self._active_cancel = True
                self._cancel_reason = "daemon-shutdown"

    def _begin_operation(
        self,
        cancel_event: threading.Event,
        job_id: str | None = None,
    ) -> bool:
        """Reset per-operation cancellation state and publish its event.

        Returns whether this operation is starting during daemon shutdown.
        A shutdown is process-wide, so a new operation always inherits the
        shutdown intent even if the previous operation ended with a user
        cancellation.  Without a shutdown, cancellation belongs only to the
        current operation.
        """

        with self._operation_state_lock:
            self._operation_generation += 1
            if self._shutdown_requested:
                self._active_cancel = True
                self._cancel_reason = "daemon-shutdown"
            else:
                self._active_cancel = False
                self._cancel_reason = None
            self._active_cancel_event = cancel_event
            self._active_cancel_job_id = job_id
            return self._shutdown_requested

    def _cancel_outcome(
        self,
        outcome: OperationState | None,
        reasons: tuple[str, ...],
    ) -> tuple[OperationState | None, tuple[str, ...]]:
        """Apply an accepted terminal intent to a worker's result.

        A cancellation or shutdown that was accepted before result
        classification wins over both a late success and a concurrent worker
        failure.  The typed reason keeps daemon shutdown recoverable and
        distinct from an explicit user cancellation.
        """

        with self._operation_state_lock:
            if not self._active_cancel:
                return outcome, reasons
            reason = self._cancel_reason
        if reason == "daemon-shutdown":
            return (
                OperationState.INTERRUPTED,
                tuple(dict.fromkeys((*reasons, "daemon-shutdown"))),
            )
        return (
            OperationState.CANCELLED,
            tuple(dict.fromkeys((*reasons, "user-cancelled"))),
        )

    def _end_operation(self) -> None:
        with self._operation_state_lock:
            self._active_cancel_event = None
            self._active_cancel_job_id = None

    @staticmethod
    def _release_slot(slot: Any, worker_done: threading.Event | None) -> None:
        """Release the processing slot only after the worker really exits.

        A Python thread cannot be killed safely.  If a legacy worker ignores
        cancellation, the slot remains held until its ``worker_done`` event
        fires.  This prevents a late worker from overlapping the next encode,
        calibration or capability probe.
        """

        if worker_done is None or worker_done.is_set():
            try:
                slot.__exit__(None, None, None)
            except Exception:
                pass
            return

        def release() -> None:
            worker_done.wait()
            try:
                slot.__exit__(None, None, None)
            except Exception:
                pass

        threading.Thread(
            target=release,
            name="mediaflow-slot-release",
            daemon=True,
        ).start()

    def _cleanup_after_worker(self, job_id: str, worker_done: threading.Event | None) -> None:
        """Remove a failed job's work only after its worker has exited."""

        with self._operation_state_lock:
            generation = self._operation_generation
        if worker_done is None or worker_done.is_set():
            self._clean_work(job_id)
            return
        key = (job_id, generation)
        with self._deferred_cleanup_lock:
            if key in self._deferred_cleanups:
                return
            self._deferred_cleanups.add(key)

        def cleanup() -> None:
            worker_done.wait()
            try:
                with self._operation_state_lock:
                    if self._operation_generation != generation:
                        # A retried attempt owns this job directory now.  The
                        # new attempt performs its own pre-encode cleanup.
                        return
                self._clean_work(job_id)
            finally:
                with self._deferred_cleanup_lock:
                    self._deferred_cleanups.discard(key)

        threading.Thread(
            target=cleanup,
            name="mediaflow-worker-cleanup",
            daemon=True,
        ).start()

    def housekeeping(self) -> None:
        if self.notifier is None:
            return
        try:
            reviews = self.store.list_jobs(("REVIEW",))
            self.notifier.send_review_reminders(reviews)
        except Exception:
            log.warning("review reminder failed", exc_info=True)
        reasons = sorted(
            set(self._pause_reasons)
            | set(self._global_pause_reasons)
            | set(self._encode_pause_reasons)
            | ({"user-paused"} if self._user_paused else set())
        )
        key = ",".join(reasons)
        prev_key = self.store.get_setting("pause_key", "")
        if key != prev_key:
            self.store.set_setting("pause_key", key)
            if key:
                self.store.set_setting("pause_since", str(self.clock()))
            else:
                self.store.set_setting("pause_since", "")
        if key:
            try:
                since = float(self.store.get_setting("pause_since", "0") or 0)
                self.notifier.send_paused(reasons, queued_for_seconds=max(0, self.clock() - since))
            except Exception:
                log.warning("paused notification failed", exc_info=True)

    # ------------------------------------------------------------------
    # scanning and stability
    # ------------------------------------------------------------------
    def scan(self) -> list[dict]:
        self._ensure_dirs()
        found: list[dict] = []
        if not self.convert_movies.exists():
            return found
        for preset_dir in sorted(self.convert_movies.iterdir()):
            if preset_dir.is_symlink() or not preset_dir.is_dir():
                continue
            preset = self._preset(preset_dir.name)
            if preset is None or not self._preset_enabled(preset):
                continue
            for entry in sorted(preset_dir.iterdir()):
                if entry.is_symlink():
                    self.store.event(None, "ignored-symlink", str(entry))
                    continue
                if not (entry.is_dir() or is_regular_file(entry)):
                    continue
                rel = entry.name
                active = self.store.active_job_for(preset_dir.name, rel)
                if active:
                    continue
                latest = self.store.latest_job_for(preset_dir.name, rel)
                if latest and latest["state"] not in TERMINAL:
                    continue
                # A terminal job does not block a fresh drop with the same name.
                try:
                    size, mtime = tree_stats(entry)
                except OSError:
                    continue
                job = self.store.create_job(
                    preset=preset_dir.name, source_rel=rel, source_path=str(entry),
                    original_path=str(entry), entry_kind="dir" if entry.is_dir() else "file",
                    dry_run=self.dry_run, source_size=size, source_mtime=mtime,
                )
                self._set_setting_json(f"stability:{job['id']}", {"size": size, "mtime": mtime,
                                                                   "since": self.clock()})
                found.append(job)
                log.info("detected job=%s preset=%s rel=%s", job["id"], preset_dir.name, rel)
        return found

    def _next_job(self, exclude: set[str] | None = None) -> dict | None:
        exclude = exclude or set()
        if self.force_job:
            job = self.store.get_job(self.force_job)
            if job and job["state"] not in TERMINAL and job["id"] not in exclude:
                return job
        jobs = self.store.list_jobs(include_done=False)
        # Earlier pipeline states first; never start a second encode.
        order = {s: i for i, s in enumerate(ACTIVE_STATES)}
        jobs.sort(key=lambda j: (order.get(j["state"], 99), j["created_at"]))
        for job in jobs:
            if job["id"] in exclude:
                continue
            if is_review(job["state"]):
                continue
            if job["state"] == "PLANNED" and job["dry_run"] and self.dry_run:
                continue
            return job
        return None

    # ------------------------------------------------------------------
    # state machine
    # ------------------------------------------------------------------
    def advance(self, job_id: str) -> None:
        for _ in range(40):
            job = self.store.get_job(job_id)
            if not job or is_terminal(job["state"]) or is_review(job["state"]):
                return
            if self._shutdown_requested:
                # A completed operation may have committed its next state, but
                # shutdown must not start another stage or publish anything.
                return
            state = job["state"]
            if state == "DETECTED":
                if not self._source_exists(job):
                    self._fail_job(job_id, "source-vanished")
                    continue
                if not self._stable(job):
                    return
                self._transition(job, "STABLE")
            elif state == "STABLE":
                if not self._source_exists(job):
                    self._fail_job(job_id, "source-vanished")
                    continue
                self._transition(job, "PROBED")
            elif state == "PROBED":
                self._step_probe(job)
                if self.store.get_job(job_id)["state"] == "PROBED":
                    return
            elif state == "PLANNED":
                if job.get("dry_run") and self.dry_run and job.get("plan"):
                    return
                if not self._step_plan(job):
                    return
            elif state == "ENCODING":
                try:
                    if not self._step_encode(job):
                        return
                finally:
                    # The flag exists only to tell crash recovery whether an
                    # encoder was actually running.  Always clear it after a
                    # controlled return; a SIGKILL leaves it behind.
                    self.store.set_setting(f"encode-started:{job_id}", "")
            elif state == "ENCODED":
                if not self._step_encoded(job):
                    return
            elif state == "VERIFIED":
                if not self._step_verified(job):
                    return
            elif state == "NAMED":
                if not self._step_named(job):
                    return
            elif state == "PUBLISHED":
                if not self._step_published(job):
                    return
            elif state == "RETIRED":
                self._step_retired(job)
            else:
                log.error("unknown state job=%s state=%s", job_id, state)
                self._fail_job(job_id, "invalid-state")
                return

    def _stable(self, job: dict) -> bool:
        entry = Path(job["source_path"])
        marker = has_temp_marker(entry)
        if marker:
            self._pause_reasons = [f"temp-marker:{job['id']}"]
            return False
        try:
            size, mtime = tree_stats(entry)
        except OSError:
            return False
        key = f"stability:{job['id']}"
        prior = self._get_setting_json(key) or {"size": size, "mtime": mtime, "since": self.clock()}
        if size != prior.get("size") or mtime != prior.get("mtime"):
            prior = {"size": size, "mtime": mtime, "since": self.clock()}
            self._set_setting_json(key, prior)
            return False
        stable_seconds = float(_cfg(self.cfg, "stable_seconds", 120))
        if self.clock() - float(prior.get("since", self.clock())) < stable_seconds:
            return False
        self.store.update_job(job["id"], source_size=size, source_mtime=mtime)
        return True

    # -- PROBED ---------------------------------------------------------
    def _step_probe(self, job: dict) -> None:
        try:
            inspection = self._inspect_source(job)
        except ReviewNeeded as exc:
            self._review(job, exc.reason, "PROBED", exc.context)
            return
        except Exception as exc:
            log.warning("probe failed job=%s: %s", job["id"], exc)
            if self.dry_run:
                self._dry_review(job, "unsupported-format", {"error": str(exc)})
            else:
                self._review(job, "unsupported-format", "PROBED", {"error": str(exc)})
            return
        # The chosen main and probe are re-derived during planning; this state
        # exists to reject unsupported/multiple-video inputs early.
        self.store.update_job(
            job["id"], source_path=str(inspection.entry), source_size=inspection.source_size,
            source_video_bytes=getattr(inspection.probe, "video_bytes", None) or 0,
            source_duration=getattr(inspection.probe, "duration", None),
        )
        self._set_setting_json(f"inspect:{job['id']}", {
            "main": str(inspection.main),
            "video_candidates": inspection.video_candidates,
            "warnings": inspection.warnings,
        })
        self._transition(self.store.get_job(job["id"]), "PLANNED")

    # -- PLANNED --------------------------------------------------------
    def _step_plan(self, job: dict) -> bool:
        if not self._preset_available(job["preset"]):
            # A removed or disabled preset is a hold for queued/planned work.
            # Jobs already past PLANNING continue under their recorded plan.
            self._pause_reasons = [f"preset-unavailable:{job['id']}"]
            return False
        if job.get("plan") and job.get("dry_run") == self.dry_run:
            plan = job["plan"]
            expected_generation = int(getattr(self.cfg, "generation", 1) or 1)
            generation_changed = (
                plan.get("config_generation") != expected_generation
                or plan.get("plan_schema") != 2
            )
            fingerprint_changed = (
                plan.get("preset_fingerprint")
                != self._preset_fingerprint(self._preset(job["preset"]))
            )
            # A SIZE-2 fallback carries its own plan version and must not be
            # silently replaced by a fresh encode plan after a reload.
            if (generation_changed or fingerprint_changed) and not plan.get("fallback_from"):
                self.store.event(
                    job["id"], "replanned",
                    "configuration generation or preset identity changed",
                )
                self.store.update_job(
                    job["id"], plan_json=None, dry_run=self.dry_run, plan_version=0,
                )
                job = self.store.get_job(job["id"])
            else:
                self._transition(job, "ENCODING")
                return True
        if job.get("plan") and job.get("dry_run") != self.dry_run:
            # Explicit dry-run flip: forget the plan and build once in either
            # direction.  A flip back to dry-run parks the job at PLANNED.
            self.store.update_job(job["id"], plan_json=None, dry_run=self.dry_run, plan_version=0)
            job = self.store.get_job(job["id"])
        try:
            inspection = self._inspect_source(job)
            identity = self._provisional_identity(inspection)
            preset = self._preset(job["preset"])
            if preset is None:
                raise EngineError(f"preset not found: {job['preset']}")
            raw = self._preset_raw(preset)
            plan = self._build_plan(job, inspection, raw, identity)
        except ReviewNeeded as exc:
            if self.dry_run:
                self._dry_review(job, exc.reason, exc.context)
                return False
            self._review(job, exc.reason, "PLANNED", exc.context)
            return False
        except Exception as exc:
            log.exception("planning failed job=%s", job["id"])
            self._fail_job(job["id"], "planning-failed", detail=str(exc))
            return False
        plan["plan_version"] = 1
        plan["schema"] = 2
        plan["plan_schema"] = 2
        plan["dry_run"] = bool(self.dry_run)
        plan["config_generation"] = int(getattr(self.cfg, "generation", 1) or 1)
        plan["preset_fingerprint"] = self._preset_fingerprint(self._preset(job["preset"]))
        plan.setdefault("working_output", str(self.work / job["id"] / f"out.{plan['ext']}"))
        self.store.update_job(
            job["id"], plan_json=plan, plan_version=1, dry_run=self.dry_run,
            source_video_bytes=plan.get("source", {}).get("video_bytes"),
            source_duration=plan.get("source", {}).get("duration"),
            source_size=inspection.source_size,
        )
        self._write_snapshot(job["id"])
        if self.dry_run:
            return False
        self._transition(self.store.get_job(job["id"]), "ENCODING")
        return True

    def _plan_fallback_pending(self, job: dict) -> bool:
        plan = job.get("plan") or {}
        return plan.get("fallback_from") == "encode" and job.get("state") == "PLANNED" and not plan.get("_ready")

    def _build_plan(self, job: dict, inspection: SourceInspection, preset: Mapping, identity: Mapping) -> dict:
        from .plan import ReviewRequired, build_plan  # local import avoids cycles
        caps = self._capabilities()
        try:
            # The planner is the sole authority for the selected backend and
            # encoder.  Do not perform a second, weaker backend selection for
            # the speed table: doing so allowed a QSV factor to be attached
            # to a VAAPI plan (or to a software fallback).
            plan = build_plan(
                source_path=inspection.main, probe=inspection.probe,
                preset=preset, config=self._config_mapping(),
                identity=identity, capabilities=caps, speed_factor=None,
                plan_version=1, video_bytes=job.get("source_video_bytes"),
            )
        except ReviewRequired as exc:
            raise ReviewNeeded(exc.reason, getattr(exc, "detail", None)) from exc
        self._apply_plan_speed(plan, job)
        # Bind the plan to the exact configuration generation and preset that
        # defined it.  Safety settings are deliberately not bound: they are
        # read from the current generation on every gate/check.
        plan["config_generation"] = int(getattr(self.cfg, "generation", 1) or 1)
        plan["preset_fingerprint"] = self._preset_fingerprint(preset)
        plan["source_probe"] = getattr(inspection.probe, "raw", None)
        plan["job_id"] = job["id"]
        plan["source_entry"] = str(inspection.entry)
        plan["source_entry_kind"] = inspection.kind
        plan["warnings"] = list(plan.get("warnings", [])) + inspection.warnings
        plan.setdefault("ext", plan.get("container", "mp4"))
        plan["working_output"] = str(self.work / job["id"] / f"out.{plan['ext']}")
        return plan

    # -- ENCODING -------------------------------------------------------
    def _step_encode(self, job: dict) -> bool:
        if not self._source_exists(job):
            self._fail_job(job["id"], "source-moved")
            return False
        plan = job.get("plan") or {}
        if not plan:
            self._fail_job(job["id"], "missing-plan")
            return False
        gate = self._start_gate(job, plan)
        if not gate.ok:
            self._pause_reasons = list(gate.reasons)
            return False
        plan = dict(plan)
        plan["working_output"] = str(self.work / job["id"] / f"out.{plan['ext']}")
        out = Path(plan["working_output"])
        partial = out.with_name(out.name + ".partial")
        scratch = self.work / job["id"]
        for p in (partial, out.with_name(out.name + ".tmp")):
            try:
                self.safety.remove(p, "pre-encode-cleanup", scratch=scratch)
            except PathDenied:
                pass
            except FileNotFoundError:
                pass
        scratch.mkdir(parents=True, exist_ok=True)
        self.store.update_job(job["id"], started_at=job.get("started_at") or self.clock())
        self._write_snapshot(job["id"])
        started = time.monotonic()
        last = {"phase": "encoding", "percent": 0.0}

        def progress(info: Mapping[str, Any]) -> None:
            last.update(dict(info))
            self._encode_progress = dict(info)
            # Keep a monotonic media-work fraction for the disk reserve.  A
            # malformed or regressing timestamp never lowers the fraction, and
            # output growth by itself is not treated as progress.
            try:
                duration = float((plan.get("source") or {}).get("duration") or 0.0)
                out_time = float(info.get("out_time_seconds"))
                if duration > 0 and math.isfinite(out_time) and out_time >= 0:
                    fraction = min(1.0, max(0.0, out_time / duration))
                    self._encode_progress_fraction = max(
                        self._encode_progress_fraction,
                        fraction,
                    )
            except (TypeError, ValueError):
                pass
            try:
                self._encode_partial_bytes = int(partial.stat().st_size)
            except OSError:
                self._encode_partial_bytes = 0
            self._write_progress(job["id"], "encoding", info)

        # Persist that the encoder is about to run.  If the process dies during
        # the encode, recovery sees this flag and counts a restart.  A job that
        # is merely waiting for a start gate has no flag and must not be
        # mistaken for an interrupted encode.  Interruption markers describe
        # the *current* attempt and are cleared before a new attempt starts.
        self.store.set_setting(f"encode-protection-interrupted:{job['id']}", "")
        self.store.set_setting(f"encode-shutdown:{job['id']}", "")
        self.store.set_setting(f"encode-deferred:{job['id']}", "")
        self.store.set_setting(f"encode-started:{job['id']}", str(self.clock()))
        try:
            result = self._run_encode_watched(job, plan, progress)
        except EncodeCancelled:
            self._remove_partial(job["id"], out)
            self._fail_job(job["id"], "cancelled")
            return False
        except EncodeStalled as exc:
            self._remove_partial(job["id"], out)
            restarts = int(job.get("restarts", 0)) + 1
            if restarts > int(_cfg(self.cfg, "max_restarts", 3)):
                self._fail_job(job["id"], f"stall-restart-limit: {exc}")
            else:
                self.store.update_job(job["id"], restarts=restarts)
                self.store.transition(job["id"], "PROBED", expect_from="ENCODING",
                                      message=f"stalled: {exc}")
                self._write_snapshot(job["id"])
            return False
        except Exception as exc:
            with self._operation_state_lock:
                cancel_reason = self._cancel_reason
            if cancel_reason == "daemon-shutdown":
                self._remove_partial(job["id"], out)
                self.store.set_setting(f"encode-shutdown:{job['id']}", "daemon-shutdown")
                self.store.event(job["id"], "encode-shutdown", "daemon-shutdown")
                self._pause_reasons = ["daemon-shutdown"]
                self._write_snapshot(job["id"])
                return False
            if cancel_reason == "user-cancelled":
                self._remove_partial(job["id"], out)
                self._fail_job(job["id"], "cancelled")
                return False
            log.exception("encoder failed job=%s", job["id"])
            self._remove_partial(job["id"], out)
            if not self._source_exists(job):
                self._fail_job(job["id"], "source-moved")
                return False
            restarts = int(job.get("restarts", 0)) + 1
            if restarts > int(_cfg(self.cfg, "max_restarts", 3)):
                self._fail_job(job["id"], f"encoder-error: {exc}")
            else:
                self.store.update_job(job["id"], restarts=restarts)
                self.store.transition(job["id"], "PROBED", expect_from="ENCODING",
                                      message=f"encode interrupted: {exc}")
                self._write_snapshot(job["id"])
            return False

        # Typed supervision outcomes are authoritative.  A protection
        # interruption is not an encoder failure and must not consume the
        # restart budget; a stall or a process failure does.
        outcome = getattr(result, "state", None)
        worker_done = getattr(result, "worker_done", None)
        reasons = tuple(getattr(result, "reasons", ()) or ())
        with self._operation_state_lock:
            cancel_reason = self._cancel_reason
        # An accepted cancellation or shutdown wins over a late success and a
        # concurrent worker failure.  The typed reason keeps shutdown distinct
        # from an explicit user cancellation.
        outcome, reasons = self._cancel_outcome(outcome, reasons)
        shutdown = "daemon-shutdown" in reasons or cancel_reason == "daemon-shutdown"
        if outcome is OperationState.DEFERRED:
            # A busy exclusive slot means that no expensive operation ran.  It
            # is a resumable deferral, not a protection interruption and not
            # an encoder failure.
            marker = ",".join(reasons)
            self.store.set_setting(f"encode-deferred:{job['id']}", marker)
            self.store.event(job["id"], "encode-deferred", marker)
            self._pause_reasons = list(reasons)
            self._write_snapshot(job["id"])
            return False
        if outcome is OperationState.INTERRUPTED:
            self._remove_partial(job["id"], out, worker_done)
            marker = ",".join(reasons)
            if shutdown:
                self.store.set_setting(f"encode-shutdown:{job['id']}", marker)
                self.store.event(job["id"], "encode-shutdown", marker)
            else:
                self.store.set_setting(f"encode-protection-interrupted:{job['id']}", marker)
                self.store.event(job["id"], "encode-protection-interrupted", marker)
            self._pause_reasons = list(reasons)
            self._write_snapshot(job["id"])
            return False
        if outcome is OperationState.CANCELLED:
            self._remove_partial(job["id"], out, worker_done)
            self._fail_job(job["id"], "cancelled", worker_done=worker_done)
            return False
        if outcome in {OperationState.STALLED, OperationState.TIMED_OUT}:
            self._remove_partial(job["id"], out, worker_done)
            restarts = int(job.get("restarts", 0)) + 1
            reason = "stalled" if outcome is OperationState.STALLED else "timed-out"
            if restarts > int(_cfg(self.cfg, "max_restarts", 3)):
                self._fail_job(job["id"], f"{reason}-restart-limit", worker_done=worker_done)
            else:
                self.store.update_job(job["id"], restarts=restarts)
                self.store.transition(job["id"], "PROBED", expect_from="ENCODING",
                                      message=f"{reason}: supervised operation did not complete")
                self._write_snapshot(job["id"])
            return False

        failure: str | None = None
        if hasattr(result, "ok") and not result.ok:
            detail = getattr(result, "stderr_text", None)
            if detail is None:
                detail = getattr(result, "stderr", "")
            failure = f"encoder returned failure: {str(detail)[:200]}"
        elif hasattr(result, "returncode") and int(result.returncode) != 0:
            failure = f"encoder exit {result.returncode}"
        if failure is not None:
            self._remove_partial(job["id"], out, worker_done)
            if not self._source_exists(job):
                self._fail_job(job["id"], "source-moved", worker_done=worker_done)
                return False
            restarts = int(job.get("restarts", 0)) + 1
            if restarts > int(_cfg(self.cfg, "max_restarts", 3)):
                self._fail_job(job["id"], f"encoder-error: {failure}", worker_done=worker_done)
            else:
                self.store.update_job(job["id"], restarts=restarts)
                self.store.transition(job["id"], "PROBED", expect_from="ENCODING",
                                      message=f"encode failed: {failure}")
                self._write_snapshot(job["id"])
            return False
        self._encode_paused = False
        self.store.set_setting(f"encode-protection-interrupted:{job['id']}", "")
        self.store.set_setting(f"encode-shutdown:{job['id']}", "")
        self.store.set_setting(f"encode-deferred:{job['id']}", "")
        elapsed = time.monotonic() - started
        if not is_regular_file(out):
            self._fail_job(job["id"], "encoder-no-output", worker_done=worker_done)
            return False
        fsync_file(out)
        fsync_dir(out.parent)
        # Serialize the final cancellation check with the state commit.  A
        # cancel or shutdown accepted before this lock is acquired wins; one
        # accepted afterwards observes the already-committed ENCODED state.
        committed = False
        with self._operation_state_lock:
            if not self._active_cancel:
                self._transition(self.store.get_job(job["id"]), "ENCODED",
                                 encode_seconds=elapsed, progress=100.0)
                committed = True
            else:
                outcome, reasons = self._cancel_outcome(
                    OperationState.SUCCEEDED, reasons
                )
        if not committed:
            self._remove_partial(job["id"], out, worker_done)
            if outcome is OperationState.INTERRUPTED:
                marker = ",".join(reasons)
                self.store.set_setting(f"encode-shutdown:{job['id']}", marker)
                self.store.event(job["id"], "encode-shutdown", marker)
                self._pause_reasons = list(reasons)
            else:
                self._fail_job(job["id"], "cancelled", worker_done=worker_done)
            self._write_snapshot(job["id"])
            return False
        return True

    def _invoke_encoder(self, plan: dict, progress: Callable,
                        cancel_event: Any = None, pause_event: Any = None,
                        monitor: Callable[[], ProtectionDecision] | None = None,
                        supervision_policy: SupervisionPolicy | None = None) -> Any:
        if self.encoder is None:
            from .ffmpeg import FfmpegEncoder
            self.encoder = FfmpegEncoder(
                ffmpeg=str(_cfg(self.cfg, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg")),
                timeout=None,
            )
        import inspect
        try:
            params = inspect.signature(self.encoder.encode).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs: dict[str, Any] = {"resume_from": None}
        if "progress_cb" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["progress_cb"] = progress
        if "cancel_event" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["cancel_event"] = cancel_event
        if "pause_event" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["pause_event"] = pause_event
        if monitor is not None and ("monitor" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())):
            kwargs["monitor"] = monitor
        if supervision_policy is not None and ("supervision_policy" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())):
            kwargs["supervision_policy"] = supervision_policy
        return self.encoder.encode(plan, **kwargs)

    def _encoder_uses_shared_supervision(self) -> bool:
        """Whether the production encoder owns the shared process supervisor.

        The production :class:`FfmpegEncoder` does.  Legacy injected test
        encoders do not, so the engine keeps the shared callable boundary for
        them.  Production encodes therefore have exactly one liveness owner,
        not a nested process supervisor plus an outer stall detector.
        """

        import inspect

        try:
            params = inspect.signature(self.encoder.encode).parameters
        except (AttributeError, TypeError, ValueError):
            return False
        return "monitor" in params and "supervision_policy" in params

    def _run_encode_watched(self, job: dict, plan: dict, progress: Callable) -> Any:
        """Run the encoder through the shared supervision boundary.

        ``FfmpegEncoder`` remains the sole FFmpeg runner.  This wrapper owns
        the operation-level contract: real-progress liveness, cancellation,
        composed protection pauses, and typed terminal outcomes.  In
        particular, a callback which merely repeats the same value never
        refreshes the stall clock.
        """
        from .ffmpeg import EncodeResult

        if self.encoder is None:
            from .ffmpeg import FfmpegEncoder
            self.encoder = FfmpegEncoder(
                ffmpeg=str(_cfg(self.cfg, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg")),
                timeout=None,
            )
        cancel = threading.Event()
        pause = threading.Event()
        if self._begin_operation(cancel, job.get("id")):
            with self._operation_state_lock:
                reason = self._cancel_reason or "daemon-shutdown"
            self._end_operation()
            state = (
                OperationState.INTERRUPTED
                if reason == "daemon-shutdown"
                else OperationState.CANCELLED
            )
            return EncodeResult(
                output=Path(str(plan.get("working_output") or ".")),
                returncode=-1,
                stderr="the operation was not started because the daemon is shutting down",
                state=state,
                cancelled=state is OperationState.CANCELLED,
                interrupted=state is OperationState.INTERRUPTED,
                reasons=(reason,),
            )
        self._encode_paused = False
        self._encode_pause_reasons = []
        self._encode_progress = {}
        self._encode_progress_fraction = 0.0
        self._encode_partial_bytes = 0
        self._space_checked_at = 0.0
        self._space_checked_monotonic = 0.0
        self._thermal_above_since = None
        self._encode_temp_sensor = None
        self._encode_sensor_checked = False

        def monitor() -> ProtectionDecision:
            # Commands are serviced while the child is alive.  A user cancel
            # is a typed cancellation, never a generic process failure.
            with self._operation_state_lock:
                active_cancel = self._active_cancel
                cancel_reason = self._cancel_reason
            if not active_cancel:
                self._service_encode_commands(job, cancel)
                with self._operation_state_lock:
                    active_cancel = self._active_cancel
                    cancel_reason = self._cancel_reason
            if active_cancel:
                shutdown = cancel_reason == "daemon-shutdown"
                reason = "daemon-shutdown" if shutdown else "user-cancelled"
                return ProtectionDecision(
                    ProtectionAction.TERMINATE,
                    (reason,),
                    detail=(
                        "daemon is shutting down"
                        if shutdown
                        else "user cancelled the encode"
                    ),
                    kind="shutdown" if shutdown else "cancelled",
                )

            reasons, should_pause = self._encoding_pause_state(job)
            if should_pause != self._encode_paused:
                if should_pause:
                    self._encode_paused = True
                    self._encode_pause_reasons = reasons
                    pause.set()
                    self.store.event(job["id"], "encode-paused", ",".join(reasons))
                    log.warning("encoding paused job=%s reasons=%s", job["id"], reasons)
                else:
                    self._encode_paused = False
                    self._encode_pause_reasons = []
                    pause.clear()
                    self.store.event(job["id"], "encode-resumed", "conditions clear")
                    log.info("encoding resumed job=%s", job["id"])
                self._write_progress(job["id"], "encoding",
                                     {"paused": self._encode_paused, "force": True})
            elif self._encode_paused and reasons != self._encode_pause_reasons:
                self._encode_pause_reasons = reasons
            if should_pause:
                return ProtectionDecision(
                    ProtectionAction.PAUSE,
                    tuple(reasons),
                    detail="operation paused by protection policy",
                    kind="protection",
                )
            return ProtectionDecision(ProtectionAction.CONTINUE)

        stall_seconds = max(
            0.2, float(_cfg(self.cfg, "stall_minutes", 10)) * 60.0
        )
        policy = SupervisionPolicy(
            stall_timeout=stall_seconds,
            startup_grace=min(30.0, stall_seconds),
            finalization_grace=min(30.0, stall_seconds),
            monitor_interval=0.1,
            terminate_grace=5.0,
        )
        native_supervision = self._encoder_uses_shared_supervision()

        def operation(
            report: Callable[[Mapping[str, Any]], None],
            stop_event: threading.Event,
        ) -> Any:
            def wrapped(info: Mapping[str, Any]) -> None:
                report(info)
                progress(info)
            worker_cancel = _CombinedEvent(cancel, stop_event)
            return self._invoke_encoder(plan, wrapped, worker_cancel, pause)

        slot = processing_slot(slot_path(self.cfg), timeout=0)
        try:
            slot.__enter__()
        except SlotBusy:
            self._end_operation()
            return EncodeResult(
                output=Path(str(plan.get("working_output") or ".")),
                returncode=-1,
                stderr="the expensive-operation slot is busy",
                state=OperationState.DEFERRED,
                deferred=True,
                reasons=("processing-slot-busy",),
            )
        except OSError as exc:
            self._end_operation()
            return EncodeResult(
                output=Path(str(plan.get("working_output") or ".")),
                returncode=-1,
                stderr=f"the expensive-operation slot is unavailable: {exc}",
                state=OperationState.DEFERRED,
                deferred=True,
                reasons=("processing-slot-unavailable",),
            )
        watch = None
        try:
            with self._operation_state_lock:
                active_cancel = self._active_cancel
                cancel_reason = self._cancel_reason
            if active_cancel:
                state = (
                    OperationState.INTERRUPTED
                    if cancel_reason == "daemon-shutdown"
                    else OperationState.CANCELLED
                )
                reason = cancel_reason or "user-cancelled"
                return EncodeResult(
                    output=Path(str(plan.get("working_output") or ".")),
                    returncode=-1,
                    stderr="the operation was cancelled before the encoder started",
                    state=state,
                    cancelled=state is OperationState.CANCELLED,
                    interrupted=state is OperationState.INTERRUPTED,
                    reasons=(reason,),
                )
            if native_supervision:
                # The production encoder already owns ``run_process``.  Calling
                # it directly avoids two competing stall clocks and two
                # monitor callers.
                result = self._invoke_encoder(
                    plan,
                    progress,
                    cancel,
                    pause,
                    monitor=monitor,
                    supervision_policy=policy,
                )
            else:
                watch = supervise_callable(
                    operation,
                    policy=policy,
                    progress_cb=None,
                    cancel_event=cancel,
                    pause_event=pause,
                    monitor=monitor,
                )
        finally:
            self._end_operation()
            worker_done = watch.worker_done if watch is not None else None
            self._release_slot(slot, worker_done)
        if watch is None:
            return result
        if watch.state is OperationState.SUCCEEDED:
            return watch.value
        if watch.state is OperationState.STALLED:
            self.store.event(job["id"], "stalled",
                             "no real progress within the supervision limit")
        return EncodeResult(
            output=Path(str(plan.get("working_output") or ".")),
            returncode=-1,
            stderr=watch.reason or (str(watch.error) if watch.error is not None else ""),
            state=watch.state,
            cancelled=watch.state is OperationState.CANCELLED,
            interrupted=watch.state is OperationState.INTERRUPTED,
            deferred=watch.state is OperationState.DEFERRED,
            stalled=watch.state is OperationState.STALLED,
            reasons=tuple(watch.reasons or ()),
            timeout_type=watch.timeout_type,
            worker_done=watch.worker_done,
            error=watch.error,
        )

    def _service_encode_commands(self, job: dict, cancel: threading.Event) -> None:
        with self._operation_state_lock:
            if self._shutdown_requested:
                # A daemon stop is process-wide.  Do not apply a new mutating
                # command after shutdown has been accepted; the next daemon
                # may service it against the recovered state.
                return
        for cmd in self.store.pending_commands():
            name = cmd.get("command")
            payload = cmd.get("payload") or {}
            if name in {"review_accept", "review_move", "review_reject", "retry", "restore",
                        "trash_purge", "failed_sweep"}:
                continue
            target = payload.get("job_id")
            if target and target != job["id"] and name == "cancel":
                continue
            if not self.store.command_start(cmd["id"]):
                continue  # another loop already claimed it
            try:
                if name == "pause":
                    self._user_paused = True
                    self.store.set_setting("user_paused", "1")
                    if payload.get("now"):
                        self.store.set_setting("pause_now_requested", str(self.clock()))
                    self.store.event(job["id"], "pause", "requested during encode")
                elif name == "resume":
                    self._user_paused = False
                    self.store.set_setting("user_paused", "0")
                    self.store.set_setting("pause_now_requested", "")
                    self.store.event(job["id"], "resume", "requested during encode")
                elif name == "cancel" and (not target or target == job["id"]):
                    with self._operation_state_lock:
                        if not self._active_cancel:
                            self._active_cancel = True
                            self._cancel_reason = "user-cancelled"
                            cancel.set()
                    self.store.event(job["id"], "cancel", "requested during encode")
                else:
                    continue
                self.store.command_done(cmd["id"])
            except Exception as exc:
                self.store.command_done(cmd["id"], error=str(exc))

    def _encoding_pause_state(self, job: dict) -> tuple[list[str], bool]:
        """Return composed pause reasons and whether the child must stop.

        ``idle_after_job`` is only a user-pause policy.  It is never allowed
        to bypass AC, thermal, space, Jellyfin or ffmpeg-change protection:
        those conditions must stop the current expensive operation.
        """
        reasons: list[str] = []
        protection: list[str] = []

        def add(reason: str, *, protective: bool = True) -> None:
            reason = str(reason)
            if not reason or reason in reasons:
                return
            reasons.append(reason)
            if protective and reason != "user-paused":
                protection.append(reason)

        if self._user_paused:
            add("user-paused", protective=False)

        # AC policy is independent from thermal policy and fails closed when
        # the mains state is unknown.  Do not rely on a custom space guard to
        # implement this safety condition.
        if bool(_cfg(self.cfg, "pause_on_battery", True)):
            try:
                from .guards import GuardConfig, read_ac_state
                ac_online = read_ac_state(GuardConfig.from_config(self.cfg))
                if ac_online is False:
                    add("on-battery")
                elif ac_online is None:
                    add("no-ac-source")
            except Exception:
                add("no-ac-source")
                log.warning("could not read AC state; failing closed", exc_info=True)

        # The mid-operation guard owns remaining-work space accounting.  Its
        # legacy AC reason is de-duplicated with the explicit policy above.
        # Custom guards in tests and deployments may expose only the legacy
        # keyword, so pass the richer values opportunistically.
        watchdog = max(
            1.0, float(_cfg(self.cfg, "space_watchdog_seconds", 60))
        )
        # The watchdog cadence is monotonic.  Wall-clock jumps must not make a
        # guard fire early or keep a live operation from being checked.
        now = time.monotonic()
        if now - self._space_checked_monotonic >= watchdog:
            self._space_checked_monotonic = now
            try:
                if self.guards is not None and hasattr(self.guards, "mid_encode_gate"):
                    kwargs: dict[str, Any] = {"source_size": job.get("source_size") or 0}
                    remaining = self._remaining_encode_work(job)
                    if remaining is not None:
                        kwargs["remaining_work"] = remaining
                        kwargs["output_consumed"] = self._encode_output_consumed(job)
                    try:
                        decision = self.guards.mid_encode_gate(**kwargs)
                    except TypeError:
                        decision = self.guards.mid_encode_gate(
                            source_size=job.get("source_size") or 0
                        )
                    for reason in getattr(decision, "reasons", ()) or ():
                        add(reason)
                elif self.guards is None:
                    from .guards import mid_encode_gate
                    kwargs = {"source_size": job.get("source_size") or 0}
                    remaining = self._remaining_encode_work(job)
                    if remaining is not None:
                        kwargs["remaining_work"] = remaining
                        kwargs["output_consumed"] = self._encode_output_consumed(job)
                    decision = mid_encode_gate(self._guard_cfg(), **kwargs)
                    for reason in getattr(decision, "reasons", ()) or ():
                        add(reason)
            except Exception:
                add("guard-error")
                log.warning("mid-encode space/AC guard failed; pausing", exc_info=True)

        # Jellyfin is a machine-level protection reason, not an AC/thermal
        # alias.  Custom guard objects that do not expose it are left alone.
        try:
            jellyfin_active = False
            if self.guards is not None and hasattr(self.guards, "jellyfin_transcoding"):
                jellyfin_active = bool(self.guards.jellyfin_transcoding())
            elif self.guards is None:
                from .guards import jellyfin_transcoding
                jellyfin_active = bool(jellyfin_transcoding(self._guard_cfg()))
            if jellyfin_active:
                add("jellyfin-transcoding")
        except Exception:
            add("jellyfin-guard-error")

        # Machine-wide holds (notably an ffmpeg-change retest) are protection
        # reasons too.  They are independent of the current job's gate state.
        for reason in self._global_pause_reasons:
            add(str(reason))

        # Thermal policy is deliberately separate from AC.  It has its own
        # hysteresis and sustain timer; an unknown sensor fails closed.
        if str(_cfg(self.cfg, "temp_sensor", "auto")) != "none":
            try:
                from .guards import (discover_temp_sensor, read_package_temp,
                                     thermal_should_pause, thermal_should_resume)
                if not self._encode_sensor_checked:
                    self._encode_temp_sensor = discover_temp_sensor(self._guard_cfg())
                    self._encode_sensor_checked = True
                temp = read_package_temp(self._encode_temp_sensor) if self._encode_temp_sensor else None
                if temp is None:
                    add("thermal-sensor-missing")
                else:
                    if thermal_should_resume(temp, self._guard_cfg()):
                        self._encode_thermal_active = False
                        self._thermal_above_since = None
                    elif self._encode_thermal_active:
                        # Stay stopped until the lower hysteresis threshold is
                        # reached, even if the package cooled a little.
                        add("thermal")
                    else:
                        if temp >= float(_cfg(self.cfg, "thermal_pause_c", 85)):
                            if self._thermal_above_since is None:
                                self._thermal_above_since = time.monotonic()
                            elapsed = max(
                                0.0,
                                time.monotonic() - self._thermal_above_since,
                            )
                            if thermal_should_pause(
                                temp,
                                self._guard_cfg(),
                                self._thermal_above_since,
                                elapsed_seconds=elapsed,
                            ):
                                self._encode_thermal_active = True
                                add("thermal")
                        else:
                            self._thermal_above_since = None
            except Exception:
                add("thermal-sensor-error")

        # A user pause may be deferred to the end of the current job only
        # when the legacy idle_after_job method was explicitly selected.
        pause_method = str(_cfg(self.cfg, "pause_method", "sigstop"))
        should_pause = bool(protection)
        if "user-paused" in reasons and pause_method != "idle_after_job":
            should_pause = True
        return reasons, should_pause

    def _remaining_encode_work(self, job: dict) -> int | None:
        """Bounded estimate of output bytes still to be written.

        Definition: ``gross`` is the source video-byte count, falling back to
        total source bytes.  With no real media progress the estimate is the
        full gross budget.  Once progress exists, the estimate is the larger
        of ``gross * (1 - progress)`` and a linear projection from bytes
        written, capped by the 1.2x source-size admission envelope.  The value
        is clamped to ``[0, cap]``; a missing gross budget returns ``None`` so
        the guard keeps its full source-size admission fallback.

        Error bounds: this is a heuristic, not a guaranteed upper bound on the
        final output.  Nonlinear VBR tails or mux overhead can understate the
        true remainder; the additive emergency low-water reserve and the
        admission envelope exist for that reason.  The cap prevents a tiny
        progress fraction and a large partial from demanding an unbounded
        reserve.
        """
        plan = job.get("plan") or {}
        source = plan.get("source") if isinstance(plan.get("source"), Mapping) else {}

        def _positive_int(*values: Any) -> int:
            for value in values:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return number
            return 0

        gross = _positive_int(
            source.get("video_bytes"), job.get("source_video_bytes"),
            source.get("size_bytes"), job.get("source_size"),
        )
        if gross <= 0:
            return None
        source_size = _positive_int(source.get("size_bytes"), job.get("source_size"), gross)
        cap = max(gross, int(math.ceil(1.2 * source_size)))
        try:
            fraction = float(self._encode_progress_fraction)
        except (TypeError, ValueError):
            fraction = 0.0
        if not math.isfinite(fraction):
            fraction = 0.0
        fraction = min(1.0, max(0.0, fraction))
        if fraction <= 0.0:
            return min(gross, cap)
        source_remaining = min(cap, int(math.ceil(gross * (1.0 - fraction))))
        consumed = max(0, self._encode_output_consumed(job))
        projected_remaining = 0
        if consumed > 0:
            projected_total = min(cap, int(math.ceil(consumed / fraction)))
            projected_remaining = max(0, projected_total - consumed)
        return min(cap, max(source_remaining, projected_remaining))

    def _encode_output_consumed(self, job: dict) -> int:
        """Return bytes actually present in the current partial output."""
        plan = job.get("plan") or {}
        out = Path(str(plan.get("working_output") or ""))
        if not out.name or out.name in {".", ".."}:
            return max(0, int(self._encode_partial_bytes or 0))
        partial = out.with_name(out.name + ".partial")
        try:
            if partial.is_file() and not partial.is_symlink():
                return int(partial.stat().st_size)
        except OSError:
            pass
        return max(0, int(self._encode_partial_bytes or 0))

    # -- ENCODED / verification ----------------------------------------
    def _step_encoded(self, job: dict) -> bool:
        plan = job.get("plan") or {}
        out = Path(plan.get("working_output") or "")
        if not out or not is_regular_file(out):
            self._restart_from_probe(job, "missing-encoded-output")
            return False
        video_bytes = self._video_bytes(out, int(plan.get("video", {}).get("index", 0)))
        source_bytes = int(plan.get("source", {}).get("video_bytes") or 0)
        if plan.get("action") == "encode" and source_bytes > 0:
            raw = self._preset_raw(self._preset(job["preset"]))
            min_savings = float(raw.get("skip", {}).get("min_savings_pct", 15))
            if video_bytes > source_bytes * (1 - min_savings / 100.0):
                try:
                    self.safety.remove(out, "size-fallback", scratch=self.work / job["id"])
                except Exception:
                    log.warning("could not remove fallback output job=%s", job["id"])
                if not self._prepare_remux_fallback(job, plan):
                    return False
                self._transition(self.store.get_job(job["id"]), "PLANNED")
                return True
        gate = self._start_gate(job, plan)
        if not gate.ok:
            self._pause_reasons = list(gate.reasons)
            return False
        verify_started = time.monotonic()
        cancel = threading.Event()
        pause = threading.Event()
        self.store.set_setting(f"verification-protection-interrupted:{job['id']}", "")
        self.store.set_setting(f"verification-shutdown:{job['id']}", "")
        self.store.set_setting(f"verification-deferred:{job['id']}", "")
        self.store.set_setting(f"verification-interrupted:{job['id']}", "")
        if self._begin_operation(cancel, job.get("id")):
            with self._operation_state_lock:
                reason = self._cancel_reason or "daemon-shutdown"
            self._end_operation()
            marker = "daemon-shutdown" if reason == "daemon-shutdown" else reason
            self.store.set_setting(f"verification-shutdown:{job['id']}", marker)
            self.store.event(job["id"], "verification-shutdown", marker)
            self._pause_reasons = [marker]
            return False

        def verification_monitor() -> ProtectionDecision:
            with self._operation_state_lock:
                active_cancel = self._active_cancel
                cancel_reason = self._cancel_reason
            if not active_cancel:
                self._service_encode_commands(job, cancel)
                with self._operation_state_lock:
                    active_cancel = self._active_cancel
                    cancel_reason = self._cancel_reason
            if active_cancel or cancel.is_set():
                shutdown = cancel_reason == "daemon-shutdown"
                reason = "daemon-shutdown" if shutdown else "user-cancelled"
                return ProtectionDecision(
                    ProtectionAction.TERMINATE,
                    (reason,),
                    detail=(
                        "daemon is shutting down"
                        if shutdown
                        else "user cancelled verification"
                    ),
                    kind="shutdown" if shutdown else "cancelled",
                )
            reasons, should_pause = self._encoding_pause_state(job)
            if should_pause:
                pause.set()
                self._encode_pause_reasons = list(reasons)
                return ProtectionDecision(
                    ProtectionAction.PAUSE,
                    tuple(reasons),
                    detail="verification paused by protection policy",
                    kind="protection",
                )
            pause.clear()
            self._encode_pause_reasons = []
            return ProtectionDecision(ProtectionAction.CONTINUE)

        stall_seconds = max(
            0.2, float(_cfg(self.cfg, "stall_minutes", 10)) * 60.0
        )
        verification_policy = SupervisionPolicy(
            stall_timeout=stall_seconds,
            startup_grace=min(30.0, stall_seconds),
            finalization_grace=min(30.0, stall_seconds),
            monitor_interval=0.1,
            terminate_grace=5.0,
        )
        shared_verifier = self._verifier_uses_shared_supervision()
        watch = None
        slot = processing_slot(slot_path(self.cfg), timeout=0)
        try:
            slot.__enter__()
        except SlotBusy:
            self._end_operation()
            result = _VerificationWatchResult(
                state=OperationState.DEFERRED,
                reasons=("processing-slot-busy",),
                deferred=True,
                failures=["the expensive-operation slot is busy"],
            )
            slot = None  # type: ignore[assignment]
        except OSError as exc:
            self._end_operation()
            result = _VerificationWatchResult(
                state=OperationState.DEFERRED,
                reasons=("processing-slot-unavailable",),
                deferred=True,
                failures=[f"the expensive-operation slot is unavailable: {exc}"],
            )
            slot = None  # type: ignore[assignment]
        else:
            try:
                if shared_verifier:
                    # The production verifier owns hashing, scans and decode
                    # supervision.  The engine only supplies its typed monitor
                    # and policy; it does not add a nested supervisor.
                    result = self._invoke_verifier(
                        plan,
                        out,
                        job=job,
                        cancel_event=cancel,
                        pause_event=pause,
                        monitor=verification_monitor,
                        supervision_policy=verification_policy,
                    )
                else:
                    def operation(
                        report: Callable[[Mapping[str, Any]], None],
                        stop_event: threading.Event,
                    ) -> Any:
                        worker_cancel = _CombinedEvent(cancel, stop_event)

                        def verifier_progress(info: Mapping[str, Any]) -> None:
                            report(info)
                            self._write_progress(job["id"], "verifying", info)

                        return self._invoke_verifier(
                            plan,
                            out,
                            job=job,
                            cancel_event=worker_cancel,
                            pause_event=pause,
                            monitor=None,
                            supervision_policy=None,
                            progress_cb=verifier_progress,
                            include_monitor=False,
                        )

                    watch = supervise_callable(
                        operation,
                        policy=verification_policy,
                        progress_cb=None,
                        cancel_event=cancel,
                        pause_event=pause,
                        monitor=verification_monitor,
                    )
                    if watch.state is OperationState.SUCCEEDED:
                        result = watch.value
                    else:
                        result = _VerificationWatchResult(
                            state=watch.state,
                            failures=[watch.reason or str(watch.error or "verification failed")],
                            reasons=tuple(watch.reasons or ()),
                            timeout_type=watch.timeout_type,
                            interrupted=watch.state is OperationState.INTERRUPTED,
                            cancelled=watch.state is OperationState.CANCELLED,
                            deferred=watch.state is OperationState.DEFERRED,
                            stalled=watch.state is OperationState.STALLED,
                            worker_done=watch.worker_done,
                        )
            except Exception as exc:
                with self._operation_state_lock:
                    cancel_reason = self._cancel_reason
                if cancel_reason == "daemon-shutdown":
                    result = _VerificationWatchResult(
                        state=OperationState.INTERRUPTED,
                        reasons=("daemon-shutdown",),
                        failures=["daemon is shutting down"],
                    )
                elif cancel_reason == "user-cancelled":
                    result = _VerificationWatchResult(
                        state=OperationState.CANCELLED,
                        reasons=("user-cancelled",),
                        failures=["user cancelled verification"],
                    )
                else:
                    log.exception("verification raised job=%s", job["id"])
                    result = _VerificationWatchResult(
                        state=OperationState.FAILED,
                        failures=[str(exc)],
                    )
            finally:
                self._end_operation()
                if slot is not None:
                    self._release_slot(
                        slot,
                        watch.worker_done if watch is not None else None,
                    )

        state = getattr(result, "state", OperationState.SUCCEEDED)
        reasons = tuple(getattr(result, "reasons", ()) or ())
        worker_done = getattr(result, "worker_done", None)
        with self._operation_state_lock:
            cancel_reason = self._cancel_reason
        state, reasons = self._cancel_outcome(state, reasons)
        shutdown = "daemon-shutdown" in reasons or cancel_reason == "daemon-shutdown"
        if state is OperationState.DEFERRED:
            # The exclusive slot was busy, so verification never started.  It
            # is a resumable deferral rather than a protection interruption.
            marker = ",".join(reasons)
            self.store.set_setting(f"verification-deferred:{job['id']}", marker)
            self.store.event(job["id"], "verification-deferred", marker)
            self._pause_reasons = list(reasons)
            return False
        if state is OperationState.INTERRUPTED:
            marker = ",".join(reasons)
            if shutdown:
                self.store.set_setting(f"verification-shutdown:{job['id']}", marker)
                self.store.event(job["id"], "verification-shutdown", marker)
            else:
                self.store.set_setting(
                    f"verification-protection-interrupted:{job['id']}", marker
                )
                self.store.event(
                    job["id"], "verification-protection-interrupted", marker
                )
            self._pause_reasons = list(reasons)
            return False
        if state is OperationState.CANCELLED:
            self._fail_job(job["id"], "cancelled", worker_done=worker_done)
            return False
        if state in {OperationState.STALLED, OperationState.TIMED_OUT}:
            timeout_type = getattr(result, "timeout_type", None)
            reason = f"verification-{state.value}"
            if timeout_type:
                reason += f":{timeout_type}"
            self.store.set_setting(
                f"verification-interrupted:{job['id']}", reason
            )
            self.store.event(job["id"], "verification-interrupted", reason)
            self._fail_job(job["id"], reason, worker_done=worker_done)
            return False
        if state is not OperationState.SUCCEEDED or not getattr(result, "ok", False):
            failures = getattr(result, "failures", ["verification failed"])
            self._fail_job(
                job["id"],
                "verification-failed",
                detail="; ".join(failures),
                worker_done=worker_done,
            )
            return False
        if getattr(result, "quality_floor_breach", False):
            if self.dry_run:
                self._dry_review(job, "quality-floor", {"output": str(out)})
                return False
            self._review(job, "quality-floor", "VERIFIED", {"output": str(out)})
            return False
        sha = getattr(result, "sha256", None) or sha256_file(out)
        # Serialize the final cancellation check with the VERIFIED commit.  A
        # cancellation accepted first suppresses the otherwise successful
        # verifier; one accepted afterwards observes the committed state.
        committed = False
        with self._operation_state_lock:
            if not self._active_cancel:
                self.store.set_setting(f"verification-protection-interrupted:{job['id']}", "")
                self.store.set_setting(f"verification-shutdown:{job['id']}", "")
                self.store.set_setting(f"verification-deferred:{job['id']}", "")
                self.store.set_setting(f"verification-interrupted:{job['id']}", "")
                self.store.update_job(
                    job["id"], output_sha256=sha,
                    output_size=getattr(result, "output_size", out.stat().st_size),
                    output_video_bytes=getattr(result, "output_video_bytes", video_bytes),
                    output_path=str(out),
                )
                self._transition(
                    self.store.get_job(job["id"]),
                    "VERIFIED",
                    encode_seconds=job.get("encode_seconds")
                    or (time.monotonic() - verify_started),
                )
                committed = True
            else:
                state, reasons = self._cancel_outcome(
                    OperationState.SUCCEEDED, reasons
                )
        if not committed:
            if state is OperationState.INTERRUPTED:
                marker = ",".join(reasons)
                self.store.set_setting(f"verification-shutdown:{job['id']}", marker)
                self.store.event(job["id"], "verification-shutdown", marker)
                self._pause_reasons = list(reasons)
            else:
                self._fail_job(job["id"], "cancelled", worker_done=worker_done)
            return False
        return True

    def _prepare_remux_fallback(self, job: dict, plan: dict) -> bool:
        """Build a genuine remux plan from the failed encode plan.

        Stream copy cannot scale or transcode, so the fallback must describe
        the source codec and source geometry.  Encode-only fields are removed
        so verification, speed lookup and calibration cannot be misled.
        """

        raw = self._preset_raw(self._preset(job["preset"]))
        container = str(raw.get("container", "mp4"))
        video = dict(plan.get("video", {}))
        source = plan.get("source") if isinstance(plan.get("source"), Mapping) else {}
        probe = plan.get("source_probe") if isinstance(plan.get("source_probe"), Mapping) else {}
        stream = next(
            (
                item for item in (probe.get("streams") or [])
                if isinstance(item, Mapping)
                and str(item.get("codec_type") or "") == "video"
                and str(item.get("index")) == str(video.get("index", 0))
            ),
            {},
        )
        codec = str(
            video.get("source_codec")
            or stream.get("codec_name")
            or ""
        ).strip().lower()
        if not codec:
            codec = str(video.get("codec") or "").strip().lower()
        if container == "mp4" and codec not in {"h264", "hevc"}:
            self._review(job, "remux-incompatible-video", "PLANNED",
                         {"source_codec": codec, "fallback_from": "encode"})
            return False

        def _dimension(*values: Any) -> int:
            for value in values:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return number
            return 0

        width = _dimension(stream.get("width"), source.get("width"), video.get("width"))
        height = _dimension(stream.get("height"), source.get("height"), video.get("height"))
        new_video = {
            key: value for key, value in video.items()
            if key not in {"source_codec", "quality", "encoder", "backend",
                           "look_ahead", "tag", "scaled", "target_width", "target_height"}
        }
        new_video.update({
            "codec": codec,
            "source_codec": codec,
            "width": width,
            "height": height,
            "target_width": width,
            "target_height": height,
            "scaled": False,
        })
        if container == "mp4" and codec == "hevc":
            new_video["tag"] = "hvc1"

        new = dict(plan)
        new["action"] = "remux"
        new["fallback_from"] = "encode"
        new["plan_version"] = int(plan.get("plan_version", 1)) + 1
        new["schema"] = 2
        new["plan_schema"] = 2
        new["video"] = new_video
        # Audio and subtitle selections are reused unchanged (SIZE-2).
        duration = float(source.get("duration") or 0.0)
        new["estimated_seconds"] = duration if duration > 0 else None
        new["speed_source"] = "copy-estimate" if duration > 0 else "missing"
        new.pop("speed_factor", None)
        new["_ready"] = True
        new["working_output"] = str(self.work / job["id"] / f"out.{new.get('ext', 'mp4')}")
        self.store.update_job(job["id"], plan_json=new, plan_version=new["plan_version"],
                              output_path=None, output_sha256=None)
        return True

    def _ensure_verifier(self) -> Callable:
        """Install the production verifier once and mark its ownership."""

        if self.verifier is None:
            from .verify import verify_output

            def default_verifier(p, o, **kwargs):
                kwargs.setdefault(
                    "ffprobe", str(_cfg(self.cfg, "ffprobe", "/usr/lib/jellyfin-ffmpeg/ffprobe"))
                )
                kwargs.setdefault(
                    "ffmpeg", str(_cfg(self.cfg, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg"))
                )
                kwargs.setdefault(
                    "min_output_kbps", float(_cfg(self.cfg, "min_output_kbps", 300))
                )
                kwargs.setdefault("min_bpp", float(_cfg(self.cfg, "min_bpp", 0.01)))
                kwargs.setdefault(
                    "progress_cb",
                    lambda info: self._write_progress(p.get("job_id", ""), "verifying", info),
                )
                return verify_output(p, o, **kwargs)

            # The production verifier owns the shared supervision boundaries
            # for hashing, scans and full decode.  The engine must not wrap
            # it in a second callable supervisor.
            default_verifier._mediaflow_shared_supervision = True  # type: ignore[attr-defined]
            self.verifier = default_verifier
        return self.verifier

    def _verifier_uses_shared_supervision(self) -> bool:
        verifier = self._ensure_verifier()
        if bool(getattr(verifier, "_mediaflow_shared_supervision", False)):
            return True
        import inspect
        try:
            params = inspect.signature(verifier).parameters
        except (TypeError, ValueError):
            return False
        return "monitor" in params and "supervision_policy" in params

    def _invoke_verifier(
        self,
        plan: dict,
        out: Path,
        *,
        job: dict | None = None,
        cancel_event: Any = None,
        pause_event: Any = None,
        monitor: Any = None,
        supervision_policy: SupervisionPolicy | None = None,
        progress_cb: Callable | None = None,
        include_monitor: bool = True,
    ) -> Any:
        verifier = self._ensure_verifier()
        # Injected verifiers from applications/tests commonly have the old
        # two-argument shape.  Pass the new controls only when accepted.
        import inspect
        try:
            params = inspect.signature(verifier).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs: dict[str, Any] = {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if cancel_event is not None and ("cancel_event" in params or accepts_kwargs):
            kwargs["cancel_event"] = cancel_event
        if pause_event is not None and ("pause_event" in params or accepts_kwargs):
            kwargs["pause_event"] = pause_event
        if monitor is not None and include_monitor and ("monitor" in params or accepts_kwargs):
            kwargs["monitor"] = monitor
        if supervision_policy is not None and ("supervision_policy" in params or accepts_kwargs):
            kwargs["supervision_policy"] = supervision_policy
        if progress_cb is not None and ("progress_cb" in params or accepts_kwargs):
            kwargs["progress_cb"] = progress_cb
        return verifier(plan, out, **kwargs)

    # -- VERIFIED / naming ---------------------------------------------
    def _step_verified(self, job: dict) -> bool:
        if not job.get("output_sha256"):
            self._fail_job(job["id"], "missing-output-hash")
            return False
        plan = dict(job.get("plan") or {})
        identity = self._resolve_identity(job, plan)
        if identity.get("status") == "review":
            reason = identity.get("reason") or "identity-low-confidence"
            if self.dry_run:
                self._dry_review(job, reason, {"candidates": identity.get("candidates", [])})
                return False
            self._review(job, reason, "NAMED", {"candidates": identity.get("candidates", [])})
            return False
        plan["name"] = {
            "title": identity["title"], "year": identity["year"],
            "tmdbid": identity.get("tmdbid"),
            "stem": self._format_name(identity["title"], identity["year"], identity.get("tmdbid")),
        }
        ext = plan.get("ext") or ("mkv" if plan.get("container") == "mkv" else "mp4")
        plan["ext"] = ext
        stem = plan["name"]["stem"]
        plan["destination_rel"] = f"{stem}/{stem}.{ext}"
        self.store.update_job(job["id"], plan_json=plan, final_name=stem, phase="naming")
        self._transition(self.store.get_job(job["id"]), "NAMED")
        return True

    # -- NAMED / publish ------------------------------------------------
    def _step_named(self, job: dict) -> bool:
        plan = job.get("plan") or {}
        dest = self._destination(job, plan)
        marker = dest / f".mediaflow-{job['id']}"
        if dest.exists() or dest.is_symlink():
            if self._marker_matches(marker, job):
                self.store.update_job(job["id"], output_path=str(dest / (Path(plan["destination_rel"]).name)),
                                      adopted=True)
                self._transition(self.store.get_job(job["id"]), "PUBLISHED", message="adopted publish window")
                return True
            details = self._duplicate_details(job, dest, plan)
            if self.dry_run:
                self._dry_review(job, "duplicate", details)
                return False
            self._review(job, "duplicate", "PUBLISHED", details)
            return False
        gate = self._space_gate(job)
        if not gate.ok:
            self._pause_reasons = list(gate.reasons)
            return False
        try:
            self._publish(job, plan, dest)
        except FileExistsError:
            details = self._duplicate_details(job, dest, plan)
            self._review(job, "duplicate", "PUBLISHED", details)
            return False
        except Exception as exc:
            log.exception("publish failed job=%s", job["id"])
            self._fail_job(job["id"], f"publish-failed: {exc}")
            return False
        output = dest / Path(plan["destination_rel"]).name
        self.store.update_job(job["id"], output_path=str(output), phase="publishing")
        self._transition(self.store.get_job(job["id"]), "PUBLISHED")
        return True

    def _publish(self, job: dict, plan: dict, dest: Path) -> None:
        """Build the final folder in work/ and publish with one rename.

        The staged folder already lives on the same filesystem as movies/, so
        no output hard link or byte copy is needed.  The verified output is
        moved into the staged folder with ``os.rename``; only text sidecars are
        copied (or converted).  The marker is written last, so a staged folder
        with a valid marker is complete and may be published directly after a
        crash.  An unmarked folder is discarded or unwound before re-staging.
        """

        work = self.work / job["id"]
        pub_root = work / "pub"
        stem = plan["name"]["stem"]
        ext = plan["ext"]
        folder = pub_root / stem
        out_src = Path(plan["working_output"])
        out_dst = folder / f"{stem}.{ext}"
        marker = folder / f".mediaflow-{job['id']}"
        complete = (
            folder.is_dir()
            and not folder.is_symlink()
            and marker.is_file()
            and out_dst.is_file()
            and not out_dst.is_symlink()
            and self._marker_matches(marker, job)
        )
        if folder.exists() and not complete:
            # A partial stage has no valid marker.  If the output was already
            # moved into it, move it back so re-staging cannot lose the only
            # verified copy.  Both moves stay inside work/ on one filesystem.
            if out_dst.is_file() and not out_dst.is_symlink() and not out_src.exists():
                os.replace(out_dst, out_src)
                fsync_dir(out_src.parent)
            self.safety.remove(folder, "publish-partial", scratch=work)
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=False)
        if not complete:
            os.rename(out_src, out_dst)
            fsync_file(out_dst)
            self._copy_sidecars(job, plan, folder, stem)
            atomic_write_json(marker, {
                "job_id": job["id"], "output_sha256": job["output_sha256"],
                "published_at": self.clock(),
            })
            fsync_dir(folder)
        self.safety.rename_noreplace(folder, dest, what="publish")
        fsync_dir(dest.parent)
        # The staged tree is now empty; do not leave a reusable pub/ shell.
        if pub_root.exists():
            try:
                self.safety.remove(pub_root, "publish-cleanup", scratch=work)
            except Exception:
                log.warning("could not remove empty publish stage job=%s", job["id"])

    @staticmethod
    def _publish_sidecar_reserve(plan: dict) -> int:
        """Bytes a publish may still write for text sidecars."""

        total = 0
        for side in plan.get("sidecars", []) or []:
            path = Path(side.get("path", ""))
            try:
                if path.is_file() and not path.is_symlink():
                    total += int(path.stat().st_size)
            except OSError:
                continue
        return max(0, total)

    def _copy_sidecars(self, job: dict, plan: dict, folder: Path, stem: str) -> None:
        for side in plan.get("sidecars", []):
            src = Path(side.get("path", ""))
            if not is_regular_file(src):
                continue
            lang = side.get("language") or "und"
            dst = folder / f"{stem}.{lang}.srt"
            if src.suffix.lower() == ".srt":
                shutil.copyfile(src, dst)
                fsync_file(dst)
            else:
                ffmpeg = str(_cfg(self.cfg, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg"))
                argv = [ffmpeg, "-v", "error", "-y", "-protocol_whitelist", "file,crypto,data",
                        "-i", str(src), "-c:s", "srt", str(dst)]
                try:
                    import subprocess
                    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
                    fsync_file(dst)
                except Exception as exc:
                    log.warning("sidecar conversion failed job=%s file=%s: %s", job["id"], src, exc)
                    if dst.exists():
                        self.safety.remove(dst, "bad-sidecar", scratch=folder)

    def _duplicate_details(self, job: dict, dest: Path, plan: dict) -> dict:
        size = None
        duration = None
        try:
            for p in dest.rglob("*"):
                if p.is_file() and not p.name.startswith(".mediaflow-"):
                    size = p.stat().st_size
                    duration = self._probe_duration(p)
                    break
        except Exception:
            pass
        return {
            "destination": str(dest), "destination_size": size, "destination_duration": duration,
            "output_size": job.get("output_size"), "source_duration": job.get("source_duration"),
            "hint": "durations within 1% suggest the same cut; otherwise likely a different cut",
        }

    # -- PUBLISHED / retire --------------------------------------------
    def _step_published(self, job: dict) -> bool:
        plan = job.get("plan") or {}
        # PUBLISHED is also the resume state for a pre-publish duplicate review.
        # Only retire once this job's output actually exists in the library.
        recorded = Path(job.get("output_path") or "")
        if not (_under(recorded, self.movies) and recorded.exists()):
            self._step_named(job)
            return True
        if job.get("trash_path") and Path(job["trash_path"]).exists():
            self._write_manifest(job, Path(job["trash_path"]))
            self._transition(self.store.get_job(job["id"]), "RETIRED")
            return True
        found = self._find_trash(job["id"])
        if found:
            self.store.update_job(job["id"], trash_path=str(found))
            self._write_manifest(self.store.get_job(job["id"]), found)
            self._transition(self.store.get_job(job["id"]), "RETIRED", message="adopted retire window")
            return True
        src = Path(job.get("source_path") or "")
        if not src.exists():
            self._fail_job(job["id"], "original-missing-after-publish")
            return False
        try:
            source_sha = job.get("source_sha256")
            if not source_sha:
                main = Path((plan.get("source") or {}).get("path", ""))
                if main.exists() and is_regular_file(main):
                    source_sha = sha256_file(main)
                    self.store.update_job(job["id"], source_sha256=source_sha)
            trash_dest = self._retire_original(job)
        except Exception as exc:
            log.exception("retire failed job=%s", job["id"])
            self._fail_job(job["id"], f"retire-failed: {exc}")
            return False
        self.store.update_job(job["id"], trash_path=str(trash_dest), phase="retiring")
        self._write_manifest(self.store.get_job(job["id"]), trash_dest)
        self._transition(self.store.get_job(job["id"]), "RETIRED")
        return True

    def _retire_original(self, job: dict) -> Path:
        src = Path(job["source_path"])
        day = time.strftime("%Y-%m-%d")
        jobdir = self.trash / day / job["id"]
        jobdir.parent.mkdir(parents=True, exist_ok=True)
        if job.get("entry_kind") == "dir" or src.is_dir():
            if jobdir.exists():
                payload = [child for child in jobdir.iterdir() if child.name != "manifest.json"]
                if payload:
                    return jobdir
                # Empty leftover from an interrupted window: replace it with
                # the original rather than declaring the retire complete.
                self.safety.remove(jobdir, "empty-retire-window")
            self.safety.rename(src, jobdir, what="retire")
            return jobdir
        # file entry: keep the file inside a job directory so the manifest has a home
        jobdir.mkdir(parents=True, exist_ok=True)
        target = jobdir / src.name
        if not target.exists():
            self.safety.rename(src, target, what="retire")
        # sidecars belong to the original too; move any that remain
        for side in (job.get("plan") or {}).get("sidecars", []):
            p = Path(side.get("path", ""))
            if is_regular_file(p) and not p.is_symlink():
                self.safety.rename(p, jobdir / p.name, what="retire-sidecar")
        return jobdir

    def _write_manifest(self, job: dict, jobdir: Path) -> Path:
        plan = job.get("plan") or {}
        manifest_path = jobdir / "manifest.json"
        main_path = None
        for rel in (Path(str(plan.get("source", {}).get("path", ""))),):
            if rel and rel.exists():
                main_path = rel
        source_sha = job.get("source_sha256")
        if source_sha is None:
            # Look in the retired tree if the original was a directory.
            try:
                inside = jobdir / Path(plan.get("source", {}).get("path", "")).name
                candidate = inside if inside.exists() else jobdir / Path(job.get("source_path", "")).name
                if candidate.exists() and is_regular_file(candidate):
                    source_sha = sha256_file(candidate)
            except Exception:
                source_sha = None
        manifest = {
            "job_id": job["id"], "preset": job["preset"], "plan_version": job.get("plan_version"),
            "original_path": job.get("original_path") or job.get("source_path"),
            "trashed_path": str(jobdir), "output_path": job.get("output_path"),
            "output_sha256": job.get("output_sha256"), "source_sha256": source_sha,
            "source_size": job.get("source_size"), "output_size": job.get("output_size"),
            "source_duration": job.get("source_duration"), "ffprobe": plan.get("source_probe"),
            "created_at": job.get("created_at"), "finished_at": self.clock(),
            "timings": {"encode_seconds": job.get("encode_seconds")},
            "dropped": plan.get("dropped", []),
            "warnings": plan.get("warnings", []),
        }
        atomic_write_json(manifest_path, manifest)
        self.store.update_job(job["id"], manifest_path=str(manifest_path))
        return manifest_path

    # -- RETIRED / done -------------------------------------------------
    def _step_retired(self, job: dict) -> None:
        if not job.get("manifest_path"):
            found = self._find_trash(job["id"])
            if not found:
                self._fail_job(job["id"], "retired-without-trash")
                return
            self._write_manifest(job, found)
            job = self.store.get_job(job["id"])
        plan = job.get("plan") or {}
        if not self.store.stats_exists(job["id"]):
            self.store.add_stat(
                job["id"], job["preset"], (plan.get("video") or {}).get("backend"),
                job.get("source_size"), job.get("output_size"),
                plan.get("source", {}).get("video_bytes"), job.get("output_video_bytes"),
                job.get("encode_seconds"),
            )
        if plan.get("action") == "encode" and job.get("encode_seconds"):
            backend, height = self._plan_speed_key(plan)
            duration = float(plan.get("source", {}).get("duration") or 0)
            if backend and height > 0 and duration > 0 and job["encode_seconds"] > 0:
                try:
                    # The marker and EMA are committed together; replay after a
                    # kill before DONE cannot double-count this encode.
                    self.store.speed_update_once(
                        job["id"], job["preset"], backend, height,
                        duration / float(job["encode_seconds"]),
                    )
                except Exception:
                    log.warning("speed table update failed job=%s", job["id"])
        self._archive_log(job)
        self.store.update_job(job["id"], phase="done", finished_at=self.clock())
        self._transition(self.store.get_job(job["id"]), "DONE")
        self._clean_work(job["id"])
        self._update_progress_counts(done=True)

    def _clean_work(self, job_id: str) -> None:
        work = self.work / job_id
        if work.exists():
            try:
                self.safety.remove(work, "job-complete", scratch=self.work)
            except Exception:
                log.warning("work cleanup failed job=%s", job_id)

    def _archive_log(self, job: dict) -> None:
        target = self.log_dir / time.strftime("%Y-%m") / f"{job['id']}.log.gz"
        if target.exists():
            return
        lines = [f"job {job['id']} preset {job['preset']} state {job['state']}",
                 f"created {job.get('created_at')} finished {self.clock()}"]
        for ev in self.store.events(job["id"]):
            lines.append(f"{ev['ts']:.3f} {ev['state'] or '-'} {ev['kind']}: {ev['message'] or ''}")
        work_log = self.work / job["id"] / "job.json"
        if work_log.exists():
            lines.append(work_log.read_text(encoding="utf-8", errors="replace"))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            gzip_text("\n".join(lines) + "\n", target)
        except Exception:
            log.exception("log archive failed job=%s", job["id"])

    # ------------------------------------------------------------------
    # review handling
    # ------------------------------------------------------------------
    def _review(self, job: dict, reason: str, from_state: str, context: Mapping | None = None) -> None:
        if self.dry_run:
            self._dry_review(job, reason, context)
            return
        # Persist the review reason before moving the original. If the process
        # dies after the rename, recovery can find review/<job-id>/ and adopt
        # the parked file instead of leaving a REVIEW with a missing source.
        self.store.set_review(job["id"], reason, from_state, context)
        parked: Path | None = None
        try:
            parked = self._park(self.store.get_job(job["id"]))
        except Exception as exc:
            log.warning("review park failed job=%s: %s", job["id"], exc)
        if parked:
            self.store.update_job(job["id"], parked_path=str(parked), source_path=str(parked))
        self._write_snapshot(job["id"])
        self._pause_reasons = []

    def _dry_review(self, job: dict, reason: str, context: Mapping | None = None) -> None:
        plan = dict(job.get("plan") or {})
        plan["would_review"] = {"reason": reason, "context": dict(context or {})}
        self.store.update_job(job["id"], plan_json=plan, dry_run=True,
                              state="PLANNED" if job["state"] in {"PROBED", "PLANNED", "DETECTED", "STABLE"}
                              else job["state"])
        self.store.event(job["id"], "dry-run-review", reason)
        self._write_snapshot(job["id"])

    def _park(self, job: dict) -> Path | None:
        if job.get("parked_path"):
            return Path(job["parked_path"])
        src = Path(job.get("source_path") or "")
        if not src.exists():
            return None
        review_dir = self.review / job["id"]
        review_dir.mkdir(parents=True, exist_ok=True)
        dest = review_dir / src.name
        if dest.exists():
            raise ReviewError(f"review destination exists: {dest}")
        self.safety.rename(src, dest, what="review")
        return dest

    def _unpark(self, job: dict, new_preset: str | None = None) -> None:
        parked = job.get("parked_path")
        if not parked:
            return
        src = Path(parked)
        if not src.exists():
            raise ReviewError(f"parked original vanished: {src}")
        preset = new_preset or job["preset"]
        target = self.convert_movies / preset / job["source_rel"]
        if target.exists():
            raise ReviewError(f"cannot restore; destination exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Record the destination before the rename. A crash after the rename
        # but before the DB update is then recoverable on startup.
        self._set_setting_json(f"unpark:{job['id']}", {
            "target": str(target), "preset": preset,
        })
        self.safety.rename(src, target, what="unreview")
        # Do not remove review/<job-id> here: SAF-5 allows deletions only
        # through FileSafety under work/trash/scratch.  An empty review
        # directory is harmless and is reused if the job is reviewed again.
        self.store.update_job(job["id"], parked_path=None, source_path=str(target),
                              original_path=str(target), preset=preset)
        self.store.set_setting(f"unpark:{job['id']}", "")

    def review_accept(self, job_id: str, *, candidate: int | None = None,
                      title: str | None = None, year: int | None = None,
                      main: str | None = None, replace: bool = False,
                      publish_anyway: bool = False) -> dict:
        job = self._require_review(job_id)
        reason = job.get("review_reason")
        resume = REVIEW_RESUME.get(reason or "")
        if resume is None:
            raise ReviewError(f"review reason {reason!r} can only be rejected")
        if reason == "multiple-videos":
            if not main:
                raise ReviewError("--main FILE is required to clear multiple-videos")
            self._set_setting_json(f"chosen_main:{job_id}", {"main": main})
        elif reason in {"hdr", "10bit", "10bit-no-hw", "interlaced", "remux-incompatible-video"}:
            raise ReviewError(f"review reason {reason!r} must be moved to another preset or rejected")
        elif reason == "quality-floor":
            if not publish_anyway:
                raise ReviewError("--publish-anyway is required to clear quality-floor")
        elif reason in {"identity-low-confidence", "identity-tie"}:
            if candidate is None and not (title and year):
                raise ReviewError("provide --candidate N or --title ... --year ...")
            if title and year:
                self._set_plan_name(job, title, int(year))
            else:
                candidates = (job.get("review_context") or {}).get("candidates", [])
                if not candidate or candidate < 1 or candidate > len(candidates):
                    raise ReviewError(f"--candidate must be 1..{len(candidates)}")
                chosen = candidates[candidate - 1]
                self._set_plan_name(job, chosen["title"], int(chosen["year"]), chosen.get("tmdb_id"))
        elif reason == "duplicate":
            if not replace:
                raise ReviewError("--replace is required to clear duplicate")
        if reason == "duplicate":
            self._unpark(job)
            context = job.get("review_context") or {}
            destination = Path(context.get("destination", ""))
            trash_target = self._replace_target(job_id)
            payload = {"destination": str(destination), "trash_target": str(trash_target)}
            intent = self.store.create_intent("replace", job_id, payload)
            self._execute_replace_intent({"id": intent, "payload": payload,
                                          "job_id": job_id})
        else:
            self._unpark(job)
        self.store.clear_review(job_id)
        if reason == "duplicate":
            self.store.transition(job_id, "PUBLISHED", expect_from="REVIEW", message="replace intent")
            self.store.complete_intent(intent)
        else:
            self.store.transition(job_id, resume, expect_from="REVIEW", message="review accepted")
        self._write_snapshot(job_id)
        self.write_status()
        return self.store.get_job(job_id)

    def review_move(self, job_id: str, preset: str) -> dict:
        job = self._require_review(job_id)
        reason = job.get("review_reason")
        if reason not in REVIEW_MOVABLE:
            raise ReviewError(f"review reason {reason!r} cannot be moved")
        if self._preset(preset) is None or not self._preset_enabled(self._preset(preset)):
            raise ReviewError(f"unknown or disabled preset: {preset}")
        self._unpark(job, new_preset=preset)
        plan = job.get("plan") or {}
        if plan:
            plan.pop("would_review", None)
        self.store.update_job(job_id, plan_json=None, preset=preset, review_reason=None,
                              review_from_state=None, review_context=None, plan_version=0)
        self.store.transition(job_id, "PLANNED", expect_from="REVIEW", message=f"moved to {preset}")
        self._write_snapshot(job_id)
        return self.store.get_job(job_id)

    def review_reject(self, job_id: str) -> dict:
        job = self._require_review(job_id)
        # Leave the original in review/ as required.
        if not job.get("parked_path"):
            try:
                parked = self._park(job)
                if parked:
                    self.store.update_job(job_id, parked_path=str(parked), source_path=str(parked))
            except ReviewError:
                pass
        self.store.update_job(job_id, error="rejected", finished_at=self.clock())
        self.store.transition(job_id, "FAILED", expect_from="REVIEW", message="review rejected")
        self._clean_work(job_id)
        return self.store.get_job(job_id)

    def _require_review(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise ReviewError(f"no such job: {job_id}")
        if job["state"] != "REVIEW":
            raise ReviewError(f"job is not in REVIEW: {job_id}")
        return job

    def _set_plan_name(self, job: dict, title: str, year: int, tmdbid: int | None = None) -> None:
        plan = dict(job.get("plan") or {})
        stem = self._format_name(title, year, tmdbid)
        plan["name"] = {"title": title, "year": int(year), "tmdbid": tmdbid, "stem": stem}
        ext = plan.get("ext", "mp4")
        plan["destination_rel"] = f"{stem}/{stem}.{ext}"
        self.store.update_job(job["id"], plan_json=plan, final_name=stem)

    # ------------------------------------------------------------------
    # failure, restart, recovery
    # ------------------------------------------------------------------
    def _fail_job(
        self,
        job_id: str,
        reason: str,
        detail: str | None = None,
        *,
        worker_done: threading.Event | None = None,
    ) -> None:
        job = self.store.get_job(job_id)
        if not job or job["state"] in TERMINAL:
            return
        log.warning("job failed id=%s reason=%s detail=%s", job_id, reason, detail)
        out = Path((job.get("plan") or {}).get("working_output") or "")
        if out:
            self._remove_partial(job_id, out, worker_done)
        if not is_review(job["state"]) and not job.get("parked_path"):
            try:
                src = Path(job.get("source_path") or "")
                if src.exists() and _under(src, self.convert_movies):
                    failed_dir = self.failed / job_id
                    failed_dir.mkdir(parents=True, exist_ok=True)
                    dest = failed_dir / src.name
                    if not dest.exists():
                        self.safety.rename(src, dest, what="failed")
                        self.store.update_job(job_id, source_path=str(dest), parked_path=str(dest))
            except Exception:
                log.exception("failed to park failed job=%s", job_id)
        self.store.update_job(job_id, error=reason if not detail else f"{reason}: {detail}",
                              finished_at=self.clock(), phase="failed")
        self.store.transition(job_id, "FAILED", expect_from=job["state"], message=reason)
        self._cleanup_after_worker(job_id, worker_done)
        self._write_snapshot(job_id)
        self._notify_failure(job, reason)

    def _restart_from_probe(self, job: dict, reason: str) -> None:
        restarts = int(job.get("restarts", 0)) + 1
        if restarts > int(_cfg(self.cfg, "max_restarts", 3)):
            self._fail_job(job["id"], f"restart-limit: {reason}")
            return
        plan = job.get("plan") or {}
        out = Path(plan.get("working_output") or "")
        if out:
            self._remove_partial(job["id"], out)
        self.store.update_job(job["id"], restarts=restarts)
        self.store.transition(job["id"], "PROBED", expect_from=job["state"], message=reason)

    def _remove_partial(
        self,
        job_id: str,
        out: Path | None,
        worker_done: threading.Event | None = None,
    ) -> None:
        # A planless job has no working_output. Path("") normalises to ".",
        # whose name is empty and whose with_name() raises; never guess a path.
        if out is None or not out.name or out.name in {".", ".."}:
            return
        with self._operation_state_lock:
            generation = self._operation_generation
        if worker_done is not None and not worker_done.is_set():
            def cleanup() -> None:
                worker_done.wait()
                with self._operation_state_lock:
                    if self._operation_generation != generation:
                        # Do not remove a retried attempt's output.
                        return
                self._remove_partial(job_id, out)

            threading.Thread(
                target=cleanup,
                name="mediaflow-partial-cleanup",
                daemon=True,
            ).start()
            return
        scratch = self.work / job_id
        for p in (out, out.with_name(out.name + ".partial")):
            try:
                self.safety.remove(p, "partial-output", scratch=scratch)
            except (PathDenied, FileNotFoundError):
                pass
            except Exception:
                log.exception("partial cleanup failed job=%s path=%s", job_id, p)

    def _reconcile_review_windows(self) -> None:
        """Adopt a park/unpark rename that committed before its DB update."""
        for job in self.store.list_jobs(("REVIEW",)):
            job_id = job["id"]
            intent = self._get_setting_json(f"unpark:{job_id}") or {}
            target = Path(intent.get("target", "")) if intent.get("target") else None
            if target is not None:
                if target.exists() and not target.is_symlink():
                    self.store.update_job(
                        job_id, parked_path=None, source_path=str(target),
                        original_path=str(target), preset=intent.get("preset") or job["preset"],
                    )
                    self.store.set_setting(f"unpark:{job_id}", "")
                    self.store.event(job_id, "recovery", "adopted unpark window")
                    continue
                self.store.set_setting(f"unpark:{job_id}", "")
            parked = Path(job.get("parked_path") or "")
            if job.get("parked_path") and parked.exists():
                continue
            review_dir = self.review / job_id
            if review_dir.is_dir() and not review_dir.is_symlink():
                entries = [child for child in review_dir.iterdir() if not child.is_symlink()]
                if entries:
                    expected = review_dir / Path(
                        job.get("original_path") or job.get("source_path") or ""
                    ).name
                    chosen = expected if expected.exists() else entries[0]
                    self.store.update_job(job_id, parked_path=str(chosen), source_path=str(chosen))
                    self.store.event(job_id, "recovery", "adopted review park window")

    def recover(self) -> None:
        """Idempotently resume every persisted state. DB wins over snapshots."""
        self._check_paths()
        # Machine-wide holds that survive a process restart are persisted
        # independently of job rows.  Resolved holds are cleared by the daemon
        # before engine.start(), so recovery cannot resurrect them.
        persistent = []
        for key in ("ffmpeg_changed_hold", "capability_unknown_hold",
                    "ffmpeg_missing_hold"):
            reason = self.store.get_setting(key, "")
            if reason:
                persistent.append(reason)
        if persistent:
            self.add_global_pause_reasons(persistent)
        self._reconcile_review_windows()
        self._finish_intents()
        for job in self.store.list_jobs(include_done=False):
            state = job["state"]
            try:
                if state in {"DETECTED", "STABLE"}:
                    partial = self.work / job["id"] / "out.partial"
                    if partial.exists():
                        self.safety.remove(partial, "startup-cleanup", scratch=self.work / job["id"])
                    self._write_snapshot(job["id"])
                elif state in {"PROBED", "PLANNED"}:
                    partial = self.work / job["id"] / "out.partial"
                    if partial.exists():
                        self.safety.remove(partial, "startup-cleanup", scratch=self.work / job["id"])
                    self._write_snapshot(job["id"])
                elif state == "ENCODING":
                    started_flag = self.store.get_setting(f"encode-started:{job['id']}", "")
                    protection_marker = self.store.get_setting(
                        f"encode-protection-interrupted:{job['id']}", ""
                    )
                    shutdown_marker = self.store.get_setting(
                        f"encode-shutdown:{job['id']}", ""
                    )
                    leftovers = [p for p in (self.work / job["id"]).glob("out.*") if p.is_file()]
                    self.store.set_setting(f"encode-started:{job['id']}", "")
                    for p in leftovers:
                        self.safety.remove(p, "startup-interrupted-encode", scratch=self.work / job["id"])
                    if protection_marker or shutdown_marker:
                        # A controlled protection/shutdown interruption is not
                        # an encoder crash and must not consume the retry
                        # budget.  Resume at PLANNED with the original intact.
                        self.store.transition(job["id"], "PLANNED", expect_from="ENCODING",
                                              message=("shutdown interrupted encoding" if shutdown_marker
                                                       else "protection interrupted encoding"))
                        self.store.event(job["id"], "recovery",
                                         "controlled encode interruption; resumed at PLANNED")
                        self._write_snapshot(job["id"])
                        continue
                    if not started_flag and not leftovers:
                        # The job never left the start gate.  This is not an
                        # interrupted encode and must not consume a restart.
                        self.store.transition(job["id"], "PLANNED", expect_from="ENCODING",
                                              message="encode had not started")
                        self.store.event(job["id"], "recovery",
                                         "encode had not started; resumed planning")
                        self._write_snapshot(job["id"])
                        continue
                    restarts = int(job.get("restarts", 0)) + 1
                    if restarts > int(_cfg(self.cfg, "max_restarts", 3)):
                        self._fail_job(job["id"], "restart-limit: startup-interrupted")
                    else:
                        self.store.update_job(job["id"], restarts=restarts, state="PROBED")
                        self.store.event(job["id"], "recovery", "interrupted encoding; restarted at PROBED")
                        self._write_snapshot(job["id"])
                elif state in {"ENCODED", "VERIFIED"}:
                    plan = job.get("plan") or {}
                    out = Path(plan.get("working_output") or "")
                    if not is_regular_file(out):
                        self._restart_from_probe(job, "startup-missing-output")
                    else:
                        if state == "ENCODED":
                            shutdown_marker = self.store.get_setting(
                                f"verification-shutdown:{job['id']}", ""
                            )
                            protection_marker = self.store.get_setting(
                                f"verification-protection-interrupted:{job['id']}", ""
                            )
                            if shutdown_marker:
                                self.store.event(
                                    job["id"],
                                    "recovery",
                                    "controlled verification shutdown; verification remains pending",
                                )
                            elif protection_marker:
                                self.store.event(
                                    job["id"],
                                    "recovery",
                                    "controlled verification interruption; verification remains pending",
                                )
                        self._write_snapshot(job["id"])
                elif state == "NAMED":
                    plan = job.get("plan") or {}
                    dest = self._destination(job, plan)
                    marker = dest / f".mediaflow-{job['id']}"
                    if dest.exists() and self._marker_matches(marker, job):
                        self.store.update_job(job["id"], state="PUBLISHED", adopted=True,
                                              output_path=str(dest / Path(plan["destination_rel"]).name))
                        self.store.event(job["id"], "recovery", "adopted publish window")
                    elif dest.exists():
                        self._review(job, "duplicate", "PUBLISHED", self._duplicate_details(job, dest, plan))
                    self._write_snapshot(job["id"])
                elif state == "PUBLISHED":
                    found = self._find_trash(job["id"])
                    if found:
                        self.store.update_job(job["id"], trash_path=str(found))
                        self._write_manifest(self.store.get_job(job["id"]), found)
                        self.store.transition(job["id"], "RETIRED", expect_from="PUBLISHED",
                                              message="recovery adopted retire")
                    else:
                        plan = job.get("plan") or {}
                        dest = self._destination(job, plan)
                        marker = dest / f".mediaflow-{job['id']}"
                        if not dest.exists():
                            self.store.transition(job["id"], "NAMED", expect_from="PUBLISHED",
                                                  message="recovery: publish missing")
                        elif not self._marker_matches(marker, job):
                            self._review(job, "duplicate", "PUBLISHED", self._duplicate_details(job, dest, plan))
                    self._write_snapshot(job["id"])
                elif state == "RETIRED":
                    found = job.get("trash_path") or self._find_trash(job["id"])
                    if found and Path(found).exists():
                        if not job.get("manifest_path"):
                            self._write_manifest(job, Path(found))
                        self._write_snapshot(job["id"])
                    else:
                        self._fail_job(job["id"], "retired-without-trash")
                elif is_review(state):
                    self._write_snapshot(job["id"])
                elif state == "FAILED":
                    self._clean_work(job["id"])
                elif state == "DONE":
                    # Crash between the DONE commit and work cleanup.
                    self._clean_work(job["id"])
                # Adoption before PUBLISHED is needed for a kill after rename.
                if state not in {"PUBLISHED", "RETIRED", "REVIEW", "DONE", "FAILED", "RESTORED"}:
                    fresh = self.store.get_job(job["id"])
                    plan = fresh.get("plan") or {}
                    if plan:
                        dest = self._destination(fresh, plan)
                        if dest.exists() and self._marker_matches(dest / f".mediaflow-{fresh['id']}", fresh):
                            self.store.update_job(fresh["id"], state="PUBLISHED", adopted=True,
                                                  output_path=str(dest / Path(plan["destination_rel"]).name))
                            self.store.event(fresh["id"], "recovery", "adopted publish window")
                            self._write_snapshot(fresh["id"])
            except Exception:
                log.exception("recovery failed job=%s", job["id"])
        for job in self.store.list_jobs(state_values(TERMINAL_STATES)):
            self._clean_work(job["id"])
        known = {j["id"] for j in self.store.list_jobs(include_done=True)}
        if self.work.exists():
            for child in self.work.iterdir():
                if child.is_dir() and not child.is_symlink() and child.name not in known:
                    try:
                        self.safety.remove(child, "orphan-work-directory")
                        self.store.event(None, "recovery", f"removed orphan work/{child.name}")
                    except Exception:
                        log.warning("orphan work cleanup failed: %s", child)
        self._write_all_snapshots()

    def _finish_intents(self) -> None:
        for intent in self.store.pending_intents("replace"):
            try:
                self._execute_replace_intent(intent)
                job = self.store.get_job(intent["job_id"])
                if job and is_review(job["state"]) and job.get("review_reason") == "duplicate":
                    self.store.clear_review(job["id"])
                    self.store.transition(job["id"], "PUBLISHED", expect_from="REVIEW",
                                          message="replace completed")
                    self._write_snapshot(job["id"])
                self.store.complete_intent(intent["id"])
            except Exception:
                log.exception("replace intent failed id=%s", intent["id"])
        for intent in self.store.pending_intents("restore"):
            try:
                self._execute_restore_intent(intent)
            except Exception:
                log.exception("restore intent failed id=%s", intent["id"])

    def _replace_target(self, job_id: str) -> Path:
        day = time.strftime("%Y-%m-%d")
        target = self.trash / day / f"{job_id}-replaced"
        n = 1
        while target.exists():
            n += 1
            target = self.trash / day / f"{job_id}-replaced-{n}"
        return target

    def _execute_replace_intent(self, intent: dict) -> None:
        job_id = intent["job_id"]
        payload = intent["payload"]
        dest = Path(payload.get("destination", ""))
        recorded = payload.get("trash_target")
        target = Path(recorded) if recorded else self._replace_target(job_id)
        if dest.exists():
            if target.exists():
                target = self._replace_target(job_id)
            self.safety.rename(dest, target, what="replace")
        if target.exists() and not target.is_symlink():
            # The target is recorded in the intent before the rename, so a
            # crash between rename and manifest write is repairable here.
            manifest = (target / "manifest.json" if target.is_dir()
                        else target.parent / f"{target.name}.manifest.json")
            if not manifest.exists():
                atomic_write_json(manifest, {
                    "kind": "replaced-library-item", "job_id": job_id, "path": str(dest),
                    "replaced_at": self.clock(),
                })
        # The caller completes the intent only after the job state is committed.

    # ------------------------------------------------------------------
    # read-only previews and estimates
    # ------------------------------------------------------------------
    def preview_plan(self, path: Path, *, preset_id: str | None = None,
                     refresh: bool = False) -> dict:
        """Build or return a plan without moving, encoding, or publishing."""
        path = Path(path)
        job = self._job_for_path(path)
        if job and job.get("plan") and not refresh:
            return job["plan"]
        if job is None:
            preset = preset_id
            if preset is None:
                try:
                    rel = path.resolve().relative_to(self.convert_movies.resolve())
                    preset = rel.parts[0]
                except (ValueError, OSError):
                    preset = preset_id
            if not preset:
                raise EngineError("cannot infer preset; pass --preset")
            job = {"id": "preview", "preset": preset, "source_path": str(path),
                   "original_path": str(path), "source_rel": path.name,
                   "entry_kind": "dir" if path.is_dir() else "file",
                   "source_size": 0, "source_video_bytes": 0, "dry_run": self.dry_run,
                   "plan": None}
        inspection = self._inspect_source(job)
        identity = self._provisional_identity(inspection)
        preset = self._preset(job["preset"])
        if preset is None:
            raise EngineError(f"unknown preset: {job['preset']}")
        plan = self._build_plan(job, inspection, self._preset_raw(preset), identity)
        plan.setdefault("working_output", str(self.work / str(job["id"]) / f"out.{plan.get('ext', 'mp4')}"))
        return plan

    def estimate_path(self, path: Path, *, preset_id: str | None = None) -> dict:
        plan = self.preview_plan(path, preset_id=preset_id)
        duration = float((plan.get("source") or {}).get("duration") or 0)
        backend, height = self._plan_speed_key(plan)
        factor = self._speed_for_plan(plan)
        action = str(plan.get("action") or "encode").strip().lower()
        if action == "remux":
            try:
                seconds = float(plan.get("estimated_seconds"))
            except (TypeError, ValueError):
                seconds = 0.0
            if seconds <= 0:
                seconds = None
            source = str(plan.get("speed_source") or "copy-estimate")
        elif factor is not None and duration > 0:
            seconds = duration / factor
            source = "table"
        else:
            seconds = None
            source = "cold-start"
        return {"plan": plan, "duration_seconds": duration, "backend": backend,
                "height": height, "speed_factor": factor, "estimate_seconds": seconds,
                "source": source}

    def refresh_plan(self, job_id: str) -> dict:
        """Re-plan a PLANNED dry-run job on demand; no files are moved."""
        job = self.store.get_job(job_id)
        if not job or job["state"] != "PLANNED":
            raise ReviewError(f"job is not PLANNED: {job_id}")
        if not job.get("dry_run"):
            raise ReviewError("only a dry-run PLANNED job may be refreshed")
        inspection = self._inspect_source(job)
        identity = self._provisional_identity(inspection)
        preset = self._preset(job["preset"])
        if preset is None:
            raise EngineError(f"preset not found: {job['preset']}")
        try:
            plan = self._build_plan(job, inspection, self._preset_raw(preset), identity)
        except ReviewNeeded as exc:
            self._dry_review(job, exc.reason, exc.context)
            return self.store.get_job(job_id)["plan"]
        plan["plan_version"] = int(job.get("plan_version") or 1)
        plan["dry_run"] = True
        plan["job_id"] = job_id
        self.store.update_job(job_id, plan_json=plan,
                              source_video_bytes=plan.get("source", {}).get("video_bytes"),
                              source_duration=plan.get("source", {}).get("duration"))
        self._write_snapshot(job_id)
        return plan

    def _job_for_path(self, path: Path) -> dict | None:
        path = Path(path)
        for job in self.store.list_jobs(include_done=True):
            for key in ("source_path", "original_path", "parked_path"):
                value = job.get(key)
                if value and Path(value) == path:
                    return job
        if path.is_relative_to(self.convert_movies) if hasattr(path, "is_relative_to") else False:
            return self.store.latest_job_for(path.parent.name, path.name)
        return None

    # ------------------------------------------------------------------
    # commands, pause, retry, cancel, restore
    # ------------------------------------------------------------------
    def process_commands(self) -> None:
        for cmd in self.store.pending_commands():
            if not self.store.command_start(cmd["id"]):
                continue  # another loop already claimed it
            try:
                result = self.execute_command(cmd["command"], cmd["payload"])
                self.store.command_done(cmd["id"])
                if result:
                    self.store.event(None, "command", f"{cmd['command']} ok")
            except Exception as exc:
                log.exception("command failed id=%s", cmd["id"])
                self.store.command_done(cmd["id"], error=str(exc))

    def execute_command(self, command: str, payload: Mapping) -> Any:
        if command not in COMMAND_NAMES:
            raise CommandError(f"unknown command: {command}")
        if command == "review_accept":
            return self.review_accept(payload["job_id"], candidate=payload.get("candidate"),
                                      title=payload.get("title"), year=payload.get("year"),
                                      main=payload.get("main"), replace=bool(payload.get("replace")),
                                      publish_anyway=bool(payload.get("publish_anyway")))
        if command == "review_move":
            return self.review_move(payload["job_id"], payload["preset"])
        if command == "review_reject":
            return self.review_reject(payload["job_id"])
        if command == "retry":
            return self.retry(payload["job_id"])
        if command == "cancel":
            return self.cancel(payload["job_id"])
        if command == "pause":
            self.pause(now=bool(payload.get("now")))
            return True
        if command == "resume":
            self.resume()
            return True
        if command == "trash_purge":
            return self.trash_purge(payload.get("older_than_days"))
        if command == "failed_sweep":
            return self.failed_sweep(payload.get("older_than_days"))
        if command == "plan_refresh":
            return self.refresh_plan(payload["job_id"])
        if command == "restore":
            return self.restore(payload["job_id"])
        raise CommandError(f"unknown command: {command}")

    def pause(self, *, now: bool = False) -> None:
        self._user_paused = True
        self.store.set_setting("user_paused", "1")
        self.store.event(None, "pause", "user")
        if now:
            self.store.set_setting("pause_now_requested", str(self.clock()))
        self.write_status()

    def resume(self) -> None:
        self._user_paused = False
        self.store.set_setting("user_paused", "0")
        self.store.set_setting("pause_now_requested", "")
        self.store.event(None, "resume", "user")
        self.write_status()

    def retry(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job or job["state"] != "FAILED":
            raise CommandError(f"job is not FAILED: {job_id}")
        src = Path(job.get("parked_path") or job.get("source_path") or "")
        target = self.convert_movies / job["preset"] / job["source_rel"]
        if not src.exists():
            raise CommandError(f"original missing; cannot retry job {job_id}")
        if src != target:
            if target.exists():
                raise CommandError(f"retry destination exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.safety.rename(src, target, what="retry")
        self.store.update_job(job_id, source_path=str(target), parked_path=None, error=None,
                              restarts=0, finished_at=None, output_path=None, output_sha256=None,
                              output_size=None, plan_json=None, plan_version=0)
        self.store.transition(job_id, "PROBED", expect_from="FAILED", message="retry")
        self._write_snapshot(job_id)
        return self.store.get_job(job_id)

    def cancel(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job or job["state"] in TERMINAL:
            raise CommandError(f"job is not active: {job_id}")
        with self._operation_state_lock:
            active_event = self._active_cancel_event
            active_job_id = self._active_cancel_job_id
            shutdown = self._shutdown_requested
            unrelated_operation = (
                active_event is not None and active_job_id != job_id
            )
            if not unrelated_operation and not self._active_cancel:
                self._active_cancel = True
                self._cancel_reason = "user-cancelled"
        if shutdown:
            # Shutdown was accepted first and keeps operations recoverable.
            return self.store.get_job(job_id)
        if active_event is not None and not unrelated_operation:
            # The requested job owns the active operation.  Signal it and let
            # its typed supervisor perform failure/cleanup with the worker's
            # completion event; do not delete work under a live worker.
            active_event.set()
            return self.store.get_job(job_id)
        # No operation for this job is active.  Fail it directly without
        # touching another job's cancellation event.
        self._fail_job(job_id, "cancelled")
        return self.store.get_job(job_id)

    def restore(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise CommandError(f"no such job: {job_id}")
        if job["state"] != "DONE":
            raise CommandError(f"job is not DONE: {job_id}")
        found = job.get("trash_path") or self._find_trash(job_id)
        if not found or not Path(found).exists():
            raise RestorePurged("original already purged; nothing to restore")
        if not job.get("manifest_path") or not Path(job["manifest_path"]).exists():
            raise CommandError("manifest missing; refusing restore")
        manifest = json.loads(Path(job["manifest_path"]).read_text(encoding="utf-8"))
        output = Path(job.get("output_path") or "")
        if not output.exists() or sha256_file(output) != manifest.get("output_sha256"):
            raise CommandError("published output hash mismatch; refusing restore")
        intent = self.store.create_intent("restore", job_id, {
            "trash_path": str(found), "output_path": str(output),
        })
        self._execute_restore_intent({"id": intent, "job_id": job_id, "payload": {
            "trash_path": str(found), "output_path": str(output)}})
        return self.store.get_job(job_id)

    def _execute_restore_intent(self, intent: dict) -> None:
        job_id = intent["job_id"]
        job = self.store.get_job(job_id)
        payload = intent["payload"]
        trash_path = Path(payload["trash_path"])
        output = Path(payload["output_path"])
        if not job or job["state"] not in {JobState.DONE.value, JobState.RESTORED.value}:
            raise CommandError("restore intent refers to a non-DONE job")
        if output.exists():
            day = time.strftime("%Y-%m-%d")
            target = self.trash / day / f"{job_id}-published"
            n = 1
            while target.exists():
                n += 1
                target = self.trash / day / f"{job_id}-published-{n}"
            self.safety.rename(output.parent, target, what="restore-published")
            atomic_write_json(target / "manifest.json", {
                "kind": "restored-published-copy", "job_id": job_id,
                "output_sha256": job.get("output_sha256"), "moved_at": self.clock(),
            })
        if trash_path.exists():
            original_name = Path(job.get("original_path") or job.get("source_path") or job_id).name
            if job.get("entry_kind") == "dir":
                inbox_target = self.inbox / original_name
                if inbox_target.exists() and not inbox_target.is_dir():
                    raise CommandError(f"inbox destination exists: {inbox_target}")
                inbox_target.mkdir(parents=True, exist_ok=True)
                for child in list(trash_path.iterdir()):
                    if child.name == "manifest.json" or child.is_symlink():
                        continue
                    dest = inbox_target / child.name
                    if not child.exists():
                        continue
                    if dest.exists():
                        # A previous interrupted restore may already have moved it.
                        continue
                    self.safety.rename(child, dest, what="restore")
            else:
                for child in list(trash_path.iterdir()):
                    if child.name == "manifest.json" or child.is_symlink():
                        continue
                    dest = self.inbox / child.name
                    if not child.exists():
                        continue
                    if dest.exists():
                        continue
                    self.safety.rename(child, dest, what="restore")
            # Keep the manifest (and thus the audit trail) in trash until
            # retention removes it; the original media is now in inbox.
        # Commit the state before completing the intent.  A crash between the
        # two leaves the intent pending; recovery can safely re-run these
        # idempotent moves.  The reverse order could leave a DONE job with a
        # dangling output path and no pending intent.
        if not self.store.transition(
            job_id, "RESTORED", expect_from=job["state"],
            output_path=None, finished_at=self.clock(),
        ):
            raise CommandError("restore state changed concurrently; retry after recovery")
        self.store.complete_intent(intent["id"])
        self.store.event(job_id, "restore", "published copy moved to trash; original restored to inbox")
        self._write_snapshot(job_id)

    def trash_purge(self, older_than_days: int | None = None) -> int:
        days = int(older_than_days if older_than_days is not None else
                   _cfg(self.cfg, "trash_retention_days", 14))
        if days <= 0:
            return 0
        cutoff = self.clock() - days * 86400
        removed = 0
        for daydir in sorted(self.trash.iterdir()) if self.trash.exists() else []:
            if not daydir.is_dir() or daydir.is_symlink():
                continue
            for jobdir in list(daydir.iterdir()):
                try:
                    if jobdir.stat().st_mtime > cutoff:
                        continue
                    self.safety.remove(jobdir, "trash-retention")
                    removed += 1
                except Exception:
                    log.exception("trash purge failed path=%s", jobdir)
        # FileSafety.remove already prunes empty ancestors up to trash, so no
        # separate rmdir pass is needed (and no deletion bypasses SAF-5).
        self.store.event(None, "trash-purge", f"removed {removed} items older than {days}d")
        return removed

    def failed_sweep(self, older_than_days: int | None = None) -> int:
        """Move expired failed/rejected items into trash (never delete)."""
        days = int(older_than_days if older_than_days is not None else
                   _cfg(self.cfg, "failed_retention_days", 30))
        if days <= 0:
            return 0
        cutoff = self.clock() - days * 86400
        moved = 0
        for job in self.store.list_jobs((JobState.FAILED,)):
            finished = float(job.get("finished_at") or job.get("updated_at") or 0)
            if finished > cutoff:
                continue
            src = Path(job.get("parked_path") or job.get("source_path") or "")
            if not src.exists() or src.name == "manifest.json":
                continue
            try:
                dest = self.safety.retire_move(src, self.trash, job["id"])
                atomic_write_json(dest / "manifest.json" if dest.is_dir()
                                  else dest.parent / f"{dest.name}.manifest.json",
                                  {"kind": "failed-or-rejected", "job_id": job["id"],
                                   "reason": job.get("error"), "moved_at": self.clock()})
                self.store.update_job(job["id"], parked_path=None, source_path=str(dest),
                                      error=(job.get("error") or "") + " [retention-expired]")
                moved += 1
            except Exception:
                log.exception("failed retention sweep failed job=%s", job["id"])
        self.store.event(None, "failed-sweep", f"moved {moved} expired item(s) to trash")
        return moved

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _inspect_source(self, job: dict) -> SourceInspection:
        from .naming import parse_media_name
        from .probe import UnsupportedFormat, probe_media
        entry = Path(job.get("original_path") or job.get("source_path"))
        if job.get("parked_path"):
            entry = Path(job["parked_path"])
        if not entry.exists():
            raise EngineError("source vanished")
        if self._has_multidisc(entry.name):
            raise ReviewNeeded("multi-disc", {"name": entry.name})
        if entry.is_file():
            files = [entry]
        else:
            files = [p for p in sorted(entry.rglob("*")) if is_regular_file(p) and p.suffix.lower() in VIDEO_EXTENSIONS]
            files = [p for p in files if not any(EXTRAS_RE.match(part) for part in p.relative_to(entry).parts[:-1])]
        if not files:
            raise ReviewNeeded("unsupported-format", {"reason": "no video files"})
        chosen = self._get_setting_json(f"chosen_main:{job['id']}")
        if chosen and chosen.get("main"):
            candidates = [p for p in files if str(p.relative_to(entry)) == chosen["main"] or p.name == chosen["main"]]
            if candidates:
                files = [candidates[0]] + [p for p in files if p != candidates[0]]
        probed: list[tuple[Path, Any]] = []
        first_error: Exception | None = None
        for p in files:
            if p.suffix.lower() in {".strm", ".url", ".m3u8"}:
                first_error = UnsupportedFormat("playlist")
                continue
            try:
                pr = probe_media(p, ffprobe=str(_cfg(self.cfg, "ffprobe", "/usr/lib/jellyfin-ffmpeg/ffprobe")))
                probed.append((p, pr))
            except UnsupportedFormat as exc:
                first_error = exc
            except Exception as exc:
                first_error = exc
        if not probed:
            raise ReviewNeeded("unsupported-format", {"error": str(first_error) if first_error else "no video"})
        probed.sort(key=lambda item: (float(getattr(item[1], "duration", 0) or 0),
                                      int(getattr(item[1], "size", 0) or 0)), reverse=True)
        main, main_probe = probed[0]
        main_duration = float(getattr(main_probe, "duration", 0) or 0)
        others = []
        for p, pr in probed[1:]:
            dur = float(getattr(pr, "duration", 0) or 0)
            if "sample" in p.name.casefold() or (main_duration and dur < main_duration * 0.05):
                continue
            others.append({"path": str(p.relative_to(entry)), "duration": dur,
                           "size": getattr(pr, "size", None)})
        if others:
            raise ReviewNeeded("multiple-videos", {
                "videos": [{"path": str(main.relative_to(entry)), "duration": main_duration}] + others,
            })
        size, _ = tree_stats(entry)
        return SourceInspection(entry=entry, kind="dir" if entry.is_dir() else "file",
                                main=main, probe=main_probe, source_size=size)

    def _has_multidisc(self, name: str) -> bool:
        return bool(MULTIDISC_RE.search(name))

    def _source_exists(self, job: dict) -> bool:
        p = Path(job.get("source_path") or "")
        return p.exists() and not p.is_symlink()

    def _start_gate(self, job: dict, plan: dict) -> Any:
        reasons: list[str] = []
        if self._user_paused:
            reasons.append("user-paused")
        force = self.force_job == job["id"]
        window = (self._preset_raw(self._preset(job["preset"])).get("schedule", {}) or {}).get("window") or None
        estimate = self._estimate(job, plan)
        speed_known = estimate is not None
        try:
            if self.guards is not None and hasattr(self.guards, "start_gate"):
                decision = self.guards.start_gate(
                    source_size=job.get("source_size") or 0,
                    estimate_seconds=estimate,
                    window=None if force else window,
                    speed_known=speed_known,
                    user_paused=self._user_paused,
                    extra_reasons=tuple(self._global_pause_reasons),
                )
            else:
                from .guards import start_gate
                decision = start_gate(
                    self._guard_cfg(), source_size=job.get("source_size") or 0,
                    estimate_seconds=estimate,
                    window=None if force else window,
                    speed_known=speed_known,
                    user_paused=self._user_paused,
                    extra_reasons=tuple(self._global_pause_reasons),
                )
            if force:
                schedule = {"estimate-exceeds-window", "estimate-cold-start", "outside-window"}
                filtered = [r for r in getattr(decision, "reasons", ()) if r not in schedule]
                if reasons or filtered:
                    return _SimpleDecision(False, reasons + filtered)
                return _SimpleDecision(True, [])
            return decision
        except Exception as exc:
            log.exception("start gate failed job=%s", job["id"])
            return _SimpleDecision(False, reasons + [f"guard-error: {exc}"])

    def _space_gate(self, job: dict) -> Any:
        """Apply the SAF-8 source reserve plus staged sidecar bytes.

        The output is moved into a folder already staged on the same
        filesystem, so publishing writes no output copy.  Only text sidecars
        are copied during staging.
        """

        plan = job.get("plan") or {}
        copy_reserve = self._publish_sidecar_reserve(plan)
        try:
            source_size = int(job.get("source_size") or 0)
        except (TypeError, ValueError):
            source_size = 0
        try:
            if self.guards is not None and hasattr(self.guards, "check_space"):
                return self.guards.check_space(
                    source_size=source_size,
                    additional_required_bytes=copy_reserve,
                    db_path=Path(_cfg(self.cfg, "db_path", "/var/lib/mediaflow/state.db")),
                )[1]
            from .guards import check_space
            return check_space(
                self._guard_cfg(), source_size=source_size,
                additional_required_bytes=copy_reserve,
                db_path=Path(_cfg(self.cfg, "db_path", "/var/lib/mediaflow/state.db")),
            )[1]
        except Exception:
            log.exception("space gate failed; failing closed")
            return _SimpleDecision(False, ["space-guard-error"])

    def _guard_cfg(self) -> Any:
        if getattr(self.cfg, "guards", None) is not None:
            return self.cfg.guards
        try:
            from .guards import GuardConfig
            return GuardConfig.from_config(self.cfg)
        except Exception:
            return self.cfg

    def _plan_speed_key(self, plan: Mapping[str, Any]) -> tuple[str | None, int]:
        """Return the one canonical speed-table key for a planned operation.

        A remux has no encode backend and therefore has no encode-speed row.
        For an encode the backend is the planner-selected backend and the
        height is the *target* height, which is also the geometry used by
        calibration and by post-encode updates.
        """

        if str(plan.get("action") or "encode").lower() != "encode":
            return None, 0
        video = plan.get("video") if isinstance(plan.get("video"), Mapping) else {}
        backend = video.get("backend")
        if not backend:
            return None, 0
        try:
            height = int(video.get("target_height") or 0)
        except (TypeError, ValueError):
            height = 0
        return (str(backend), height) if height > 0 else (None, 0)

    def _speed_for_plan(self, plan: Mapping[str, Any]) -> float | None:
        backend, height = self._plan_speed_key(plan)
        if not backend or height <= 0:
            return None
        try:
            factor = self.store.speed_get(
                str(plan.get("preset") or ""), backend, height
            )
        except Exception:
            return None
        try:
            factor = float(factor) if factor is not None else None
        except (TypeError, ValueError):
            return None
        return factor if factor is not None and factor > 0 else None

    def _apply_plan_speed(self, plan: dict, job: Mapping[str, Any] | None = None) -> None:
        """Populate the estimate from the plan's authoritative speed key."""

        duration = float((plan.get("source") or {}).get("duration") or 0)
        action = str(plan.get("action") or "encode").strip().lower()
        factor = self._speed_for_plan(plan)
        if action == "remux":
            # Remuxing is not an encode and has no backend speed-table row.
            # Preserve the planner's explicit copy estimate (one times media
            # time by default) instead of attaching another backend's factor.
            estimate = plan.get("estimated_seconds")
            try:
                estimate = float(estimate) if estimate is not None else duration
            except (TypeError, ValueError):
                estimate = duration
            plan["estimated_seconds"] = estimate if estimate > 0 else None
            plan["speed_source"] = (
                "copy-estimate" if plan.get("estimated_seconds") is not None else "missing"
            )
            plan.pop("speed_factor", None)
        elif factor is not None and duration > 0:
            plan["estimated_seconds"] = duration / factor
            plan["speed_source"] = "table"
            plan["speed_factor"] = factor
        else:
            plan["estimated_seconds"] = None
            plan["speed_source"] = "missing"
            plan.pop("speed_factor", None)

    def _estimate(self, job: dict, plan: dict) -> float | None:
        duration = float(plan.get("source", {}).get("duration") or 0)
        action = str(plan.get("action") or "encode").strip().lower()
        if action == "remux":
            try:
                estimate = float(plan.get("estimated_seconds"))
            except (TypeError, ValueError):
                estimate = 0.0
            return estimate if estimate > 0 else None
        factor = self._speed_for_plan(plan)
        if factor is None or duration <= 0:
            # Only the selected backend and canonical target-height key may
            # establish an encode speed.  A stale estimate copied from an
            # earlier plan is not authoritative.
            return None
        return duration / factor

    def _video_bytes(self, path: Path, stream_index: int) -> int:
        try:
            from .probe import video_packet_bytes
            return int(video_packet_bytes(path, stream_index,
                                          ffprobe=str(_cfg(self.cfg, "ffprobe", "/usr/lib/jellyfin-ffmpeg/ffprobe"))))
        except Exception:
            return path.stat().st_size

    def _capabilities(self) -> dict:
        from .doctor import read_capabilities

        return read_capabilities(self.capabilities_path)

    def _choose_backend(self, preset: Mapping) -> str:
        order = ((preset.get("video") or {}).get("backend_order") or ["qsv", "vaapi"])
        caps = self._capabilities()
        for b in order:
            value = caps.get(b)
            if isinstance(value, Mapping):
                if value.get("hevc8"):
                    return b
            elif value is True:
                return b
        return str(order[0] if order else "qsv")

    def _resolve_identity(self, job: dict, plan: dict) -> dict:
        source = Path(plan.get("source_entry") or job.get("source_path") or "")
        try:
            from .naming import parse_media_name
            parsed = parse_media_name(source.name)
        except Exception:
            parsed = {"title": source.stem, "year": self._year_from_name(source.name)}
        if self.identity_resolver is not None:
            try:
                result = self.identity_resolver(parsed, self._tmdb_token(), self.store)
            except Exception as exc:
                log.warning("identity resolver failed job=%s: %s", job["id"], exc)
                result = None
            if result is not None:
                return _identity_to_dict(result)
        token = self._tmdb_token()
        if token:
            try:
                from .identify import resolve_identity
                result = resolve_identity(parsed, token, self.store)
                return _identity_to_dict(result)
            except Exception as exc:
                log.warning("TMDb identity failed job=%s: %s", job["id"], exc)
        title = getattr(parsed, "title", None) or (parsed.get("title") if isinstance(parsed, Mapping) else None)
        year = getattr(parsed, "year", None) or (parsed.get("year") if isinstance(parsed, Mapping) else None)
        if title and year:
            return {"status": "auto", "title": title, "year": int(year), "tmdbid": None,
                    "source": "parse-only", "candidates": []}
        return {"status": "review", "reason": "identity-low-confidence", "candidates": []}

    def _provisional_identity(self, inspection: SourceInspection) -> dict:
        try:
            from .naming import parse_media_name
            name_source = inspection.entry.name if inspection.entry.is_dir() else inspection.main.name
            parsed = parse_media_name(name_source)
            title = getattr(parsed, "title", None) or inspection.main.stem
            year = getattr(parsed, "year", None) or self._year_from_name(name_source)
        except Exception:
            title, year = inspection.main.stem, self._year_from_name(inspection.main.name)
        title = self._sanitize_title(str(title))
        return {"title": title, "year": int(year) if year else 1900, "tmdbid": None,
                "status": "provisional", "candidates": []}

    def _tmdb_token(self) -> str | None:
        token = _cfg(self.secrets, "tmdb_token", None) if self.secrets is not None else None
        if token:
            return str(token)
        return None

    def _format_name(self, title: str, year: int, tmdbid: int | None = None) -> str:
        try:
            from .naming import format_media_name
            return format_media_name(title, int(year), tmdbid,
                                     append_tmdbid=bool(_cfg(self.cfg, "append_tmdbid", False)))
        except Exception:
            base = f"{self._sanitize_title(title)} ({int(year)})"
            if tmdbid and _cfg(self.cfg, "append_tmdbid", False):
                base += f" [tmdbid-{tmdbid}]"
            return base

    @staticmethod
    def _sanitize_title(title: str) -> str:
        try:
            from .naming import sanitize_component
            return sanitize_component(title)
        except Exception:
            return re.sub(r"[\\/?*\"<>|]", "", title.replace(":", " -")).strip() or "Untitled"

    @staticmethod
    def _year_from_name(name: str) -> int | None:
        matches = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", name)
        return int(matches[-1]) if matches else None

    def _destination(self, job: dict, plan: dict) -> Path:
        stem = (plan.get("name") or {}).get("stem") or ""
        if not stem:
            stem = self._format_name("Untitled", 1900)
        return self.movies / stem

    def _marker_matches(self, marker: Path, job: dict) -> bool:
        if not marker.exists() or marker.is_symlink():
            return False
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            return False
        return data.get("job_id") == job["id"] and data.get("output_sha256") == job.get("output_sha256")

    def _probe_duration(self, path: Path) -> float | None:
        try:
            from .probe import probe_media
            return float(getattr(probe_media(path), "duration", 0) or 0)
        except Exception:
            return None

    def _find_trash(self, job_id: str) -> Path | None:
        if not self.trash.exists():
            return None
        for day in sorted(self.trash.iterdir(), reverse=True):
            if not day.is_dir() or day.is_symlink():
                continue
            candidate = day / job_id
            if not candidate.exists() or not candidate.is_dir() or candidate.is_symlink():
                continue
            # A file-entry retire creates the job directory before moving the
            # file. A crash in that window must not look like a completed
            # retire; the original is still in convert/.
            if any(child.name != "manifest.json" for child in candidate.iterdir()):
                return candidate
        return None

    def _write_progress(self, job_id: str, phase: str, info: Mapping) -> None:
        now = self.clock()
        interval = float(_cfg(self.cfg, "progress_seconds", 30))
        if now - self._progress_last < interval and not info.get("force"):
            return
        self._progress_last = now
        temp = None
        try:
            tmp = _cfg(self.cfg, "package_temp", None)
            temp = float(tmp) if tmp is not None else None
        except Exception:
            pass
        payload = {"job_id": job_id, "phase": phase, "updated_at": now, "temperature_c": temp}
        for key in ("percent", "speed", "eta_seconds", "out_time_seconds"):
            if key in info:
                payload[key] = info[key]
        atomic_write_json(self.progress_path, payload)

    def _update_progress_counts(self, done: bool = False) -> None:
        try:
            if done:
                atomic_write_json(self.progress_path, {"phase": "idle", "updated_at": self.clock()})
        except Exception:
            pass

    def write_status(self) -> dict:
        jobs = self.store.list_jobs(include_done=False)
        counts: dict[str, int] = {}
        oldest_review = None
        history: dict[str, int] = {}
        for job in self.store.list_jobs(include_done=True):
            if is_terminal(job["state"]):
                history[job["state"]] = history.get(job["state"], 0) + 1
        for job in jobs:
            counts[job["state"]] = counts.get(job["state"], 0) + 1
            if is_review(job["state"]):
                age = self.clock() - float(job.get("updated_at") or self.clock())
                oldest_review = max(oldest_review or 0, age)
        free = None
        trash_size = 0
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            pass
        try:
            for root, dirs, files in os.walk(self.trash, followlinks=False):
                for name in files:
                    p = Path(root) / name
                    if not p.is_symlink():
                        trash_size += p.stat().st_size
        except OSError:
            pass
        last_success = None
        try:
            done_jobs = self.store.list_jobs(("DONE",))
            if done_jobs:
                last_success = max(j.get("finished_at") or 0 for j in done_jobs) or None
        except Exception:
            pass
        payload = {
            "counts": counts,
            "history": history,
            "queue": sum(v for k, v in counts.items() if not is_review(k)),
            "reviews": counts.get(JobState.REVIEW.value, 0),
            "oldest_review_seconds": oldest_review,
            "paused_reasons": sorted(
                set(self._pause_reasons)
                | set(self._global_pause_reasons)
                | set(self._encode_pause_reasons)
                | ({"user-paused"} if self._user_paused else set())
            ),
            "free_bytes": free,
            "trash_bytes": trash_size, "last_success": last_success, "updated_at": self.clock(),
            "dry_run": self.dry_run,
            "config_generation": int(getattr(self.cfg, "generation", 1) or 1),
            "pending_config_generation": (
                int(getattr(self.pending_config, "generation", 0) or 0)
                if self.pending_config is not None else None
            ),
            "restart_required": sorted(self._restart_required),
        }
        atomic_write_json(self.status_path, payload)
        return payload

    def _write_snapshot(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job or is_terminal(job["state"]):
            # Terminal history is visible in status/queue, but it must not
            # recreate a work directory after cleanup.
            return
        work = self.work / job_id
        if not work.exists():
            work.mkdir(parents=True, exist_ok=True)
        snapshot = dict(job)
        snapshot.pop("source_path_private", None)
        atomic_write_json(work / "job.json", snapshot)

    def _write_all_snapshots(self) -> None:
        for job in self.store.list_jobs(include_done=False):
            if not is_terminal(job["state"]):
                self._write_snapshot(job["id"])

    def _transition(self, job: dict, to_state: str, **fields: Any) -> bool:
        ok = self.store.transition(job["id"], to_state, expect_from=job["state"], **fields)
        if ok:
            self._write_snapshot(job["id"])
            self.write_status()
        return ok

    def _config_mapping(self) -> dict:
        if isinstance(self.cfg, Mapping):
            return dict(self.cfg)
        return {k: getattr(self.cfg, k) for k in dir(self.cfg)
                if not k.startswith("_") and not callable(getattr(self.cfg, k))}

    def _preset(self, preset_id: str) -> Any:
        return self.presets.get(preset_id)

    def _preset_raw(self, preset: Any) -> Mapping:
        if preset is None:
            return {}
        if isinstance(preset, Mapping):
            return preset
        return getattr(preset, "raw", getattr(preset, "data", {}))

    def _preset_fingerprint(self, preset: Any) -> str:
        """Stable identity for the preset values that define a plan."""

        raw = self._preset_raw(preset)
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _preset_available(self, preset_id: str) -> bool:
        return self._preset_enabled(self._preset(preset_id))

    def _preset_enabled(self, preset: Any) -> bool:
        if preset is None:
            return False
        try:
            return bool(preset.enabled)
        except Exception:
            raw = self._preset_raw(preset)
            return bool((raw.get("meta") or {}).get("enabled", True))

    def _get_setting_json(self, key: str) -> Any:
        raw = self.store.get_setting(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _set_setting_json(self, key: str, value: Any) -> None:
        self.store.set_setting(key, json.dumps(value, ensure_ascii=False, sort_keys=True))

    def _notify_review(self, job: dict, reason: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify("review", job["id"], {"reason": reason})
        except Exception:
            log.warning("review notification failed")

    def _notify_failure(self, job: dict, reason: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify("failed", job["id"], {"reason": reason})
        except Exception:
            log.warning("failure notification failed")


class ReviewNeeded(Exception):
    def __init__(self, reason: str, context: Mapping | None = None):
        super().__init__(reason)
        self.reason = reason
        self.context = dict(context or {})


class RestorePurged(CommandError):
    exit_code = 3


@dataclass
class _SimpleDecision:
    ok: bool
    reasons: list[str]

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(self.reasons)


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _cfg(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _identity_to_dict(result: Any) -> dict:
    if isinstance(result, Mapping):
        return dict(result)
    out = {
        "status": getattr(result, "status", "review"),
        "title": getattr(result, "title", None), "year": getattr(result, "year", None),
        "tmdbid": getattr(result, "tmdb_id", getattr(result, "tmdbid", None)),
        "reason": getattr(result, "reason", None),
        "source": getattr(result, "source", None),
        "candidates": [c if isinstance(c, Mapping) else {
            "title": getattr(c, "title", None), "year": getattr(c, "year", None),
            "tmdb_id": getattr(c, "tmdb_id", None), "score": getattr(c, "score", None),
        } for c in (getattr(result, "candidates", []) or [])],
    }
    return out
