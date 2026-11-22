"""Shared supervision for mediaflow's long-running work.

The production encoder, verifier, calibration trials and capability probes all
need the same answers to the same questions:

* is the operation still alive?
* did a status callback arrive?
* did real work advance?
* was the operation deliberately paused?
* was it interrupted by a protection condition?
* was it cancelled by the user?
* did it stall or exceed a time limit?
* did it fail, or did it complete?

This module is the single supervision boundary for those operations.  It does
not decide *why* a machine must stop; callers provide a typed
:class:`ProtectionDecision`.  It does not run a second ffmpeg process: the
existing encoder owns its child and this module supervises that boundary.

A callback is visibility, not proof of progress.  The liveness clock advances
only for a finite, monotonic work value such as a frame count, media timestamp
or byte count.  Repeated, malformed and regressing values are ignored.
"""

from __future__ import annotations

import errno
import fcntl
import inspect
import math
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, Mapping, Protocol, TypeVar

T = TypeVar("T")


class OperationState(str, Enum):
    """States reported by a supervised operation.

    ``ALIVE``, ``HEARTBEAT`` and ``PROGRESS`` are observable states, while the
    remaining values are terminal outcomes.  ``PAUSED`` is deliberately not a
    terminal outcome: an intentional pause must not consume a retry budget.
    """

    ALIVE = "alive"
    HEARTBEAT = "heartbeat"
    PROGRESS = "progress"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALLED = "stalled"
    TIMED_OUT = "timed-out"
    INTERRUPTED = "interrupted"
    DEFERRED = "deferred"

    # Compatibility spellings used by a few callers.  They are enum aliases,
    # not additional states, so comparisons and serialisation stay stable.
    TIMEOUT = "timed-out"
    PROTECTION_INTERRUPTED = "interrupted"


#: Outcomes that must not be treated as a successful operation.
NON_SUCCESS_STATES = frozenset({
    OperationState.FAILED,
    OperationState.CANCELLED,
    OperationState.STALLED,
    OperationState.TIMED_OUT,
    OperationState.INTERRUPTED,
    OperationState.DEFERRED,
})


class ProtectionAction(str, Enum):
    """What a monitor wants the supervisor to do now."""

    CONTINUE = "continue"
    PAUSE = "pause"
    TERMINATE = "terminate"
    DEFER = "defer"


@dataclass(frozen=True)
class ProtectionDecision:
    """A typed decision from an operation-specific protection monitor."""

    action: ProtectionAction = ProtectionAction.CONTINUE
    reasons: tuple[str, ...] = ()
    detail: str | None = None
    kind: str = "protection"

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ProtectionAction(self.action))
        object.__setattr__(self, "reasons", tuple(str(reason) for reason in self.reasons))
        if self.detail is not None:
            object.__setattr__(self, "detail", str(self.detail))

    @property
    def terminal(self) -> bool:
        return self.action in {ProtectionAction.TERMINATE, ProtectionAction.DEFER}

    @property
    def reason_text(self) -> str:
        return self.detail or ", ".join(self.reasons) or self.kind


@dataclass(frozen=True)
class SupervisionPolicy:
    """Operation-specific liveness and termination limits.

    ``stall_timeout`` is the allowed time without *real work*.  ``None``
    disables stall detection for short bounded probes.  ``startup_grace`` and
    ``finalization_grace`` are explicit, bounded allowances; neither is an
    unlimited substitute for progress.
    """

    stall_timeout: float | None = 600.0
    startup_grace: float = 30.0
    finalization_grace: float = 30.0
    overall_timeout: float | None = None
    monitor_interval: float = 0.2
    terminate_grace: float = 5.0


class OperationCancelled(RuntimeError):
    """The user cancelled an operation."""


class OperationInterrupted(RuntimeError):
    """A protection condition interrupted an operation."""

    def __init__(self, message: str, reasons: tuple[str, ...] = ()):
        super().__init__(message)
        self.reasons = tuple(reasons)


class OperationDeferred(RuntimeError):
    """A protection condition deferred an operation."""


class SlotBusy(RuntimeError):
    """The single expensive-operation slot is already held."""


ProgressExtractor = Callable[[Mapping[str, Any]], float | None]
ProgressCallback = Callable[[Mapping[str, Any]], None]
Monitor = Callable[[], ProtectionDecision]
ProgressParser = Callable[[str, dict[str, str]], Mapping[str, Any] | None]


def _finite_number(value: Any) -> float | None:
    """Return a finite non-negative number, or ``None`` for malformed data."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _progress_dimension(event: Mapping[str, Any]) -> str:
    """Identify the independent axis represented by a progress event.

    Frame counts, media timestamps and byte counts are all real work, but a
    frame count must never be compared with a timestamp.  A caller may set an
    explicit ``work_dimension`` when it has a more specific axis.
    """

    explicit = event.get("work_dimension")
    if explicit:
        return str(explicit)
    if "work_progress" in event:
        return "work"
    for key in ("frame", "frames"):
        if key in event and _finite_number(event.get(key)) is not None:
            return "frame"
    for key in ("out_time_seconds", "out_time"):
        if key in event and _finite_number(event.get(key)) is not None:
            return "time"
    for key in ("bytes", "bytes_done", "records", "packets", "packet_count"):
        if key in event and _finite_number(event.get(key)) is not None:
            return "bytes"
    return "value"


def media_work_value(event: Mapping[str, Any]) -> float | None:
    """Extract real media progress from a frame or timestamp event.

    Frame count is preferred because it is independent of the reported speed.
    Media time is the fallback for decoders that do not expose frames.  The
    two values are never compared with one another.
    """

    for key in ("frame", "out_time_seconds", "out_time"):
        if key in event:
            value = _finite_number(event.get(key))
            if value is not None:
                return value
    return None


def byte_work_value(event: Mapping[str, Any]) -> float | None:
    """Extract real progress from a byte count."""

    for key in ("bytes", "bytes_done", "records"):
        if key in event:
            value = _finite_number(event.get(key))
            if value is not None:
                return value
    return None


@dataclass(frozen=True)
class ProgressObservation:
    """The facts learned from one progress event."""

    state: OperationState
    work_value: float | None
    heartbeat: bool = False
    advanced: bool = False
    regressed: bool = False
    malformed: bool = False


class ProgressTracker:
    """Track whether an operation's *work* is advancing.

    Repeated identical values, malformed values, regressions and explicit
    heartbeats are visible but do not refresh the work clock.
    """

    def __init__(
        self,
        *,
        policy: SupervisionPolicy,
        extractor: ProgressExtractor = media_work_value,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.extractor = extractor
        self.clock = clock
        self._lock = threading.RLock()
        self.started_at = float(clock())
        self.last_work_at = self.started_at
        self.last_heartbeat_at = self.started_at
        self.last_value: float | None = None
        # Progress dimensions are tracked independently.  A frame counter and
        # a media timestamp are both real work signals, but they are not
        # interchangeable numbers: comparing them would turn a perfectly
        # healthy timestamp advance into a false regression.
        self.last_values: dict[str, float] = {}
        self.observed_work = False
        self.finalizing_at: float | None = None
        self.paused = False
        self.paused_at: float | None = None
        self.state = OperationState.ALIVE
        self.last_observation: ProgressObservation | None = None

    def observe(self, event: Mapping[str, Any]) -> ProgressObservation:
        """Observe one progress event without treating it as proof of work."""

        now = float(self.clock())
        with self._lock:
            self.last_heartbeat_at = now
            if event.get("finalizing") is True:
                if self.finalizing_at is None:
                    self.finalizing_at = now
                self.state = OperationState.PROGRESS
                observation = ProgressObservation(self.state, None, heartbeat=False)
                self.last_observation = observation
                return observation
            if event.get("heartbeat") is True:
                self.state = OperationState.HEARTBEAT
                observation = ProgressObservation(self.state, None, heartbeat=True)
                self.last_observation = observation
                return observation

            explicit = event.get("work_progress")
            value = _finite_number(explicit)
            malformed = explicit is not None and value is None
            if value is None:
                try:
                    value = self.extractor(event)
                except Exception:
                    value = None
                if value is None and any(key in event for key in ("frame", "out_time_seconds", "out_time")):
                    malformed = True
            value = _finite_number(value)

            if value is None:
                self.state = OperationState.HEARTBEAT
                observation = ProgressObservation(self.state, None, malformed=malformed)
                self.last_observation = observation
                return observation

            dimension = _progress_dimension(event)
            previous = self.last_values.get(dimension)
            if previous is None:
                # The first observation of a new dimension is real work.  It
                # must not be compared with a value from another dimension.
                self.last_values[dimension] = value
                self.last_value = value
                self.last_work_at = now
                self.observed_work = True
                self.state = OperationState.PROGRESS
                observation = ProgressObservation(self.state, value, advanced=True)
                self.last_observation = observation
                return observation

            if value > previous:
                self.last_values[dimension] = value
                self.last_value = value
                self.last_work_at = now
                self.state = OperationState.PROGRESS
                observation = ProgressObservation(self.state, value, advanced=True)
            else:
                regressed = value < previous
                self.state = OperationState.HEARTBEAT
                observation = ProgressObservation(self.state, value, regressed=regressed)
            self.last_observation = observation
            return observation

    def mark_paused(self) -> None:
        with self._lock:
            if self.paused:
                return
            self.paused = True
            self.paused_at = float(self.clock())
            self.state = OperationState.PAUSED

    def mark_resumed(self) -> None:
        with self._lock:
            if not self.paused:
                self.state = OperationState.ALIVE
                return
            now = float(self.clock())
            started = self.paused_at if self.paused_at is not None else now
            paused_for = max(0.0, now - started)
            # Paused time is not charged to startup, work or finalization.
            self.last_work_at += paused_for
            self.started_at += paused_for
            if self.finalizing_at is not None:
                self.finalizing_at += paused_for
            self.paused = False
            self.paused_at = None
            self.state = OperationState.ALIVE

    def stalled(self) -> bool:
        """Whether the operation has exceeded its no-real-progress limit."""

        timeout = self.policy.stall_timeout
        if timeout is None or timeout <= 0:
            return False
        now = float(self.clock())
        with self._lock:
            if self.paused:
                return False
            if self.finalizing_at is not None:
                deadline = self.finalizing_at + max(0.0, self.policy.finalization_grace)
            elif self.observed_work:
                deadline = self.last_work_at + timeout
            else:
                deadline = self.started_at + max(0.0, self.policy.startup_grace) + timeout
            return now > deadline


@dataclass(frozen=True)
class ProcessResult:
    """The typed result of one owned child process."""

    state: OperationState
    returncode: int
    stdout: str = ""
    stderr: str = ""
    reason: str | None = None
    reasons: tuple[str, ...] = ()
    elapsed: float = 0.0
    #: ``overall`` for a wall-clock deadline and ``stall`` for a no-progress
    #: deadline.  Process failures and protection interruptions have no timeout
    #: type.
    timeout_type: str | None = None
    #: True when the executable could not be started.  A spawn failure is an
    #: unknown capability, not evidence that the tested feature is unsupported.
    spawn_failed: bool = False


@dataclass(frozen=True)
class CallableResult(Generic[T]):
    """The typed result of one supervised callable operation."""

    state: OperationState
    value: T | None = None
    error: BaseException | None = None
    reason: str | None = None
    reasons: tuple[str, ...] = ()
    elapsed: float = 0.0
    worker_stopped: bool = True
    timeout_type: str | None = None
    #: Set when the worker exits, even if it ignored the cancellation request.
    #: Owners that hold an external resource can release it only after this
    #: event fires, preventing a late worker from overlapping the next job.
    worker_done: threading.Event | None = None


class _TailBuffer:
    """Keep only the last ``limit`` bytes of a stream in memory."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._data = bytearray()

    def append(self, text: str) -> None:
        if not text:
            return
        data = text.encode("utf-8", "replace")
        self._data.extend(data)
        if len(self._data) > self.limit:
            del self._data[: len(self._data) - self.limit]

    def text(self) -> str:
        return self._data.decode("utf-8", "replace")


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal only the process group created for this owned child."""

    try:
        os.killpg(proc.pid, sig)
        return
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno not in {errno.ESRCH, errno.EPERM}:
            raise
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def terminate_process(proc: subprocess.Popen, *, grace: float = 5.0) -> int:
    """Stop and reap an owned process, escalating after a bounded grace.

    A stopped child is continued before SIGTERM so it can handle the signal;
    SIGKILL is the final escalation.  Only the process group created for this
    child is signalled.
    """

    if proc.poll() is not None:
        try:
            return int(proc.wait(timeout=1.0))
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            return int(proc.returncode or -1)
    _signal_group(proc, signal.SIGCONT)
    _signal_group(proc, signal.SIGTERM)
    try:
        return int(proc.wait(timeout=max(0.0, grace)))
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        try:
            return int(proc.wait(timeout=max(1.0, grace)))
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            return int(proc.returncode or -1)


def _terminal_state(decision: ProtectionDecision) -> OperationState:
    if decision.action is ProtectionAction.DEFER:
        return OperationState.DEFERRED
    if decision.kind == "cancelled":
        return OperationState.CANCELLED
    return OperationState.INTERRUPTED


def _handle_monitor_state(
    tracker: ProgressTracker,
    decision: ProtectionDecision,
) -> OperationState | None:
    if decision.action is ProtectionAction.PAUSE:
        tracker.mark_paused()
        return None
    tracker.mark_resumed()
    if decision.action in {ProtectionAction.TERMINATE, ProtectionAction.DEFER}:
        return _terminal_state(decision)
    return None


def _event_is_set(event: Any) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except Exception:
        return False


def _operation_accepts_stop(operation: Callable[..., T]) -> bool:
    """Whether a callable can receive the supervisor's internal stop event.

    The event is private to the supervisor.  It lets a cooperative worker stop
    after an internally detected stall, timeout or protection interruption
    without mutating the caller's cancellation event.  One-argument callables
    remain supported for compatibility; their eventual exit is still exposed
    through ``worker_done``.
    """

    try:
        parameters = inspect.signature(operation).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [
        parameter for parameter in parameters
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    ]
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return True
    return len(positional) >= 2


def run_process(
    command: list[str] | tuple[str, ...],
    *,
    policy: SupervisionPolicy | None = None,
    progress_parser: ProgressParser | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Monitor | None = None,
    capture_stdout: bool = True,
    stdout_limit: int = 4 * 1024 * 1024,
    stderr_limit: int = 256 * 1024,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> ProcessResult:
    """Run and supervise one owned child process.

    The child is placed in its own process group.  Cancellation, a stall, a
    timeout or a protection termination stops that group, escalates from
    SIGTERM to SIGKILL after ``terminate_grace``, and reaps the child.

    Progress is consumed by a dedicated reader rather than a lossy queue.
    This matters for packet scans: dropping a line would silently change the
    byte/count result.  The reader keeps only the current line in memory;
    captured stdout and stderr are bounded tails.
    """

    policy = policy or SupervisionPolicy()
    started = time.monotonic()
    tracker = ProgressTracker(policy=policy)
    stdout_tail = _TailBuffer(stdout_limit)
    stderr_tail = _TailBuffer(stderr_limit)
    proc: subprocess.Popen | None = None
    stdout_reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    reader_errors: list[BaseException] = []
    terminal_state: OperationState | None = None
    terminal_reason: str | None = None
    terminal_reasons: tuple[str, ...] = ()
    terminal_timeout_type: str | None = None
    monitor_paused = False
    external_paused = False
    process_paused = False
    last_monitor = 0.0
    progress_state: dict[str, str] = {}

    def handle_progress(line: str) -> None:
        if progress_parser is None:
            return
        try:
            event = progress_parser(line, progress_state)
        except Exception:
            # A malformed progress record is not a process failure.  It is
            # deliberately ignored, just like a malformed callback value.
            return
        if not isinstance(event, Mapping):
            return
        try:
            tracker.observe(event)
            if progress_cb is not None:
                progress_cb(dict(event))
        except BaseException as exc:  # pragma: no cover - defensive callback
            reader_errors.append(exc)

    def read_stream(handle: Any, *, lines: bool, on_line: Callable[[str], None] | None = None,
                    tail: _TailBuffer | None = None) -> None:
        try:
            if lines:
                # Iteration over a text pipe yields each complete line as soon
                # as it arrives.  ``read(n)`` would wait for n characters (or
                # EOF), delaying cancellation until a noisy child exits.
                for line in handle:
                    if on_line is not None:
                        on_line(line)
                return
            read_one = getattr(handle, "read1", None)
            while True:
                chunk = read_one(4096) if callable(read_one) else handle.read(4096)
                if not chunk:
                    break
                if tail is not None:
                    tail.append(chunk)
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)
        finally:
            try:
                handle.close()
            except OSError:
                pass

    try:
        if progress_parser is not None:
            stdout_target: Any = subprocess.PIPE
        elif capture_stdout:
            stdout_target = subprocess.PIPE
        else:
            stdout_target = subprocess.DEVNULL
        process_env = dict(os.environ)
        if env:
            process_env.update({str(key): str(value) for key, value in env.items()})
        try:
            proc = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=stdout_target,
                stderr=subprocess.PIPE,
                env=process_env,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            return ProcessResult(
                state=OperationState.FAILED,
                returncode=127,
                stderr=str(exc),
                reason=f"could not start process: {exc}",
                elapsed=time.monotonic() - started,
                spawn_failed=True,
            )

        if proc.stdout is not None:
            stdout_reader = threading.Thread(
                target=read_stream,
                args=(proc.stdout,),
                kwargs={"lines": progress_parser is not None, "on_line": handle_progress,
                        "tail": stdout_tail},
                name="mediaflow-process-reader",
                daemon=True,
            )
            stdout_reader.start()
        if proc.stderr is not None:
            stderr_reader = threading.Thread(
                target=read_stream,
                args=(proc.stderr,),
                kwargs={"lines": False, "tail": stderr_tail},
                name="mediaflow-process-stderr-reader",
                daemon=True,
            )
            stderr_reader.start()

        def apply_pause() -> None:
            nonlocal process_paused
            effective = monitor_paused or external_paused
            if effective and not process_paused and proc is not None and proc.poll() is None:
                _signal_group(proc, signal.SIGSTOP)
                process_paused = True
            elif not effective and process_paused and proc is not None and proc.poll() is None:
                _signal_group(proc, signal.SIGCONT)
                process_paused = False

        while True:
            now = time.monotonic()
            if monitor is not None and now - last_monitor >= policy.monitor_interval:
                last_monitor = now
                try:
                    decision = monitor()
                except BaseException as exc:
                    terminal_state = OperationState.FAILED
                    terminal_reason = f"protection monitor failed: {exc}"
                    terminal_reasons = ()
                    if proc.poll() is None:
                        terminate_process(proc, grace=policy.terminate_grace)
                    break
                if decision.action is ProtectionAction.PAUSE:
                    monitor_paused = True
                    tracker.mark_paused()
                else:
                    if monitor_paused:
                        monitor_paused = False
                        tracker.mark_resumed()
                    if decision.action in {ProtectionAction.TERMINATE, ProtectionAction.DEFER}:
                        terminal_state = _terminal_state(decision)
                        terminal_reason = decision.reason_text
                        terminal_reasons = decision.reasons
                        if proc.poll() is None:
                            terminate_process(proc, grace=policy.terminate_grace)
                        break

            external_paused = _event_is_set(pause_event)
            if external_paused and not tracker.paused:
                tracker.mark_paused()
            elif not external_paused and tracker.paused and not monitor_paused:
                tracker.mark_resumed()
            apply_pause()

            if _event_is_set(cancel_event) and terminal_state is None:
                terminal_state = OperationState.CANCELLED
                terminal_reason = "operation cancelled"
                if proc.poll() is None:
                    terminate_process(proc, grace=policy.terminate_grace)
                break
            if tracker.stalled():
                terminal_state = OperationState.STALLED
                terminal_reason = "no real progress within the supervision limit"
                terminal_timeout_type = "stall"
                if proc.poll() is None:
                    terminate_process(proc, grace=policy.terminate_grace)
                break
            if (
                policy.overall_timeout is not None
                and policy.overall_timeout > 0
                and not tracker.paused
                and now - tracker.started_at > policy.overall_timeout
            ):
                terminal_state = OperationState.TIMED_OUT
                terminal_reason = f"operation timed out after {policy.overall_timeout}s"
                terminal_timeout_type = "overall"
                if proc.poll() is None:
                    terminate_process(proc, grace=policy.terminate_grace)
                break

            readers_alive = any(
                reader is not None and reader.is_alive()
                for reader in (stdout_reader, stderr_reader)
            )
            if proc.poll() is not None and not readers_alive:
                break
            time.sleep(min(0.05, max(0.005, policy.monitor_interval)))

        if proc.poll() is None:
            terminate_process(proc, grace=policy.terminate_grace)
        returncode = int(proc.wait())
    finally:
        if proc is not None and proc.poll() is None:
            terminate_process(proc, grace=policy.terminate_grace)
        # Join before closing the pipes.  Closing them first can wake the
        # reader with ``ValueError`` and lose the final progress records.
        for reader in (stdout_reader, stderr_reader):
            if reader is not None:
                reader.join(timeout=5.0)
        if proc is not None:
            for handle in (proc.stdout, proc.stderr):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass

    elapsed = time.monotonic() - started
    stdout = stdout_tail.text() if progress_parser is None else ""
    stderr = stderr_tail.text()
    if terminal_state is not None:
        state = terminal_state
        reason = terminal_reason
        reasons = terminal_reasons
    elif reader_errors:
        state = OperationState.FAILED
        reason = f"progress reader failed: {reader_errors[0]}"
        reasons = ()
    elif returncode == 0:
        state = OperationState.SUCCEEDED
        reason = None
        reasons = ()
    else:
        state = OperationState.FAILED
        reason = f"process exited with status {returncode}"
        reasons = ()
    return ProcessResult(
        state=state,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        reason=reason,
        reasons=reasons,
        elapsed=elapsed,
        timeout_type=terminal_timeout_type,
    )


def supervise_callable(
    operation: Callable[[ProgressCallback], T],
    *,
    policy: SupervisionPolicy | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Monitor | None = None,
    extractor: ProgressExtractor = media_work_value,
    clock: Callable[[], float] = time.monotonic,
) -> CallableResult[T]:
    """Supervise a callable operation with the same liveness semantics.

    The callable receives a progress reporter.  A callable that accepts a
    second argument also receives a private stop event.  The supervisor sets
    that event for an internally detected stall, timeout, monitor failure or
    protection termination; it never mutates the caller's cancellation event.
    The worker is owned by this call.  A cancellation or protection decision
    that wins the race suppresses a late successful value, and ``worker_done``
    exposes the eventual exit of a non-cooperative worker.
    """

    policy = policy or SupervisionPolicy()
    started = float(clock())
    tracker = ProgressTracker(policy=policy, extractor=extractor, clock=clock)
    box: dict[str, Any] = {}
    errors: list[BaseException] = []
    worker_done = threading.Event()
    stop_event = threading.Event()
    terminal_state: OperationState | None = None
    terminal_reason: str | None = None
    terminal_reasons: tuple[str, ...] = ()
    terminal_timeout_type: str | None = None
    monitor_paused = False
    last_monitor = 0.0
    accepts_stop = _operation_accepts_stop(operation)

    def report(event: Mapping[str, Any]) -> None:
        tracker.observe(event)
        if progress_cb is not None:
            progress_cb(dict(event))

    def worker() -> None:
        try:
            if accepts_stop:
                box["value"] = operation(report, stop_event)
            else:
                box["value"] = operation(report)
        except BaseException as exc:  # classified by the supervisor
            errors.append(exc)
        finally:
            worker_done.set()

    thread = threading.Thread(target=worker, name="mediaflow-operation", daemon=True)
    thread.start()
    while thread.is_alive():
        now = float(clock())
        if monitor is not None and now - last_monitor >= policy.monitor_interval:
            last_monitor = now
            try:
                decision = monitor()
            except BaseException as exc:
                terminal_state = OperationState.FAILED
                terminal_reason = f"protection monitor failed: {exc}"
                terminal_reasons = ()
                stop_event.set()
                break
            if decision.action is ProtectionAction.PAUSE:
                monitor_paused = True
                tracker.mark_paused()
            else:
                if monitor_paused:
                    monitor_paused = False
                    tracker.mark_resumed()
                state = _handle_monitor_state(tracker, decision)
                if state is not None:
                    terminal_state = state
                    terminal_reason = decision.reason_text
                    terminal_reasons = decision.reasons
                    stop_event.set()
        if _event_is_set(pause_event) and not tracker.paused:
            tracker.mark_paused()
        elif not _event_is_set(pause_event) and tracker.paused and not monitor_paused:
            tracker.mark_resumed()
        if _event_is_set(cancel_event) and terminal_state is None:
            terminal_state = OperationState.CANCELLED
            terminal_reason = "operation cancelled"
            stop_event.set()
        if tracker.stalled() and terminal_state is None:
            terminal_state = OperationState.STALLED
            terminal_reason = "no real progress within the supervision limit"
            terminal_timeout_type = "stall"
            stop_event.set()
        if (
            policy.overall_timeout is not None
            and policy.overall_timeout > 0
            and not tracker.paused
            and now - tracker.started_at > policy.overall_timeout
        ) and terminal_state is None:
            terminal_state = OperationState.TIMED_OUT
            terminal_reason = f"operation timed out after {policy.overall_timeout}s"
            terminal_timeout_type = "overall"
            stop_event.set()
        if terminal_state is not None:
            # Do not wait forever for a non-cooperative callable here.  The
            # result reports whether it stopped, and the resource owner can
            # use ``worker_done`` to retain exclusivity until it really exits.
            thread.join(min(0.1, max(0.01, policy.monitor_interval)))
            break
        thread.join(min(0.1, max(0.01, policy.monitor_interval)))

    thread.join(max(0.1, policy.terminate_grace))
    worker_stopped = not thread.is_alive()
    elapsed = float(clock()) - started
    if terminal_state is not None:
        return CallableResult(
            state=terminal_state,
            value=None,
            reason=terminal_reason,
            reasons=terminal_reasons,
            elapsed=elapsed,
            worker_stopped=worker_stopped,
            timeout_type=terminal_timeout_type,
            worker_done=worker_done,
        )
    if _event_is_set(cancel_event):
        # Cancellation won the race with the worker's return.  Suppress the
        # late value even when the callable itself exited cleanly.
        return CallableResult(
            state=OperationState.CANCELLED,
            value=None,
            reason="operation cancelled",
            elapsed=elapsed,
            worker_stopped=worker_stopped,
            worker_done=worker_done,
        )
    if errors:
        exc = errors[0]
        if isinstance(exc, OperationCancelled):
            state = OperationState.CANCELLED
            reason = str(exc)
            reasons: tuple[str, ...] = ()
        elif isinstance(exc, OperationDeferred):
            state = OperationState.DEFERRED
            reason = str(exc)
            reasons = ()
        elif isinstance(exc, OperationInterrupted):
            state = OperationState.INTERRUPTED
            reason = str(exc)
            reasons = exc.reasons
        else:
            state = OperationState.FAILED
            reason = str(exc)
            reasons = ()
        return CallableResult(
            state=state,
            value=None,
            error=exc,
            reason=reason,
            reasons=reasons,
            elapsed=elapsed,
            worker_stopped=worker_stopped,
            worker_done=worker_done,
        )
    return CallableResult(
        state=OperationState.SUCCEEDED,
        value=box.get("value"),
        elapsed=elapsed,
        worker_stopped=worker_stopped,
        worker_done=worker_done,
    )


@contextmanager
def processing_slot(path: str | os.PathLike[str], *, blocking: bool = False,
                   timeout: float = 0.0) -> Iterator[Path]:
    """Hold the single expensive-operation slot.

    The lock is advisory and local to the configured runtime/state directory;
    it is not a system-wide media lock.  A busy slot raises :class:`SlotBusy`
    rather than being reported as an unsupported hardware capability.
    """

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o664)
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            # A finite timeout must never use an uninterruptible blocking
            # flock.  Poll with LOCK_NB so a busy slot becomes a typed
            # deferral at the deadline instead of hanging the daemon.
            flags = fcntl.LOCK_EX
            if not (blocking and timeout <= 0):
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SlotBusy(str(lock_path))
                time.sleep(0.05)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass
        yield lock_path
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def slot_path(config: Any) -> Path:
    """Return the configured path of the expensive-operation slot."""

    explicit = getattr(config, "processing_slot_path", None)
    if explicit:
        return Path(explicit)
    runtime = getattr(config, "runtime_dir", None)
    if runtime:
        return Path(runtime) / "processing-slot.lock"
    state = getattr(config, "state_dir", None)
    if state:
        return Path(state) / "processing-slot.lock"
    return Path("/run/mediaflow/processing-slot.lock")


# Compatibility aliases for callers that prefer an explicit outcome name.
SupervisionResult = ProcessResult
OperationResult = CallableResult
