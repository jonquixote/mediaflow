"""Shared supervision tests: real progress, pauses and owned processes.

Requirement coverage: [SAF-2], [SAF-9], [SAF-12].
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from mediaflow.supervision import (
    OperationState,
    ProgressTracker,
    ProtectionAction,
    ProtectionDecision,
    SupervisionPolicy,
    media_work_value,
    run_process,
    supervise_callable,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(clock: Clock, **policy_kwargs) -> ProgressTracker:
    policy = SupervisionPolicy(
        stall_timeout=10.0,
        startup_grace=5.0,
        finalization_grace=5.0,
        **policy_kwargs,
    )
    return ProgressTracker(
        policy=policy,
        extractor=media_work_value,
        clock=clock,
    )


def test_repeated_identical_media_progress_does_not_refresh_the_work_clock() -> None:
    """SAF-12: a callback is not proof that the encoder advanced."""
    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe({"frame": 10, "out_time_seconds": 1.0, "speed": 0.0})
    clock.advance(9.0)
    tracker.observe({"frame": 10, "out_time_seconds": 1.0, "speed": 0.0})
    assert not tracker.stalled()
    clock.advance(2.0)
    assert tracker.stalled()


def test_advancing_timestamp_is_real_progress_even_without_speed() -> None:
    """SAF-12: a zero/unavailable speed does not make advancing work stalled."""
    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe({"out_time_seconds": 1.0, "speed": 0.0})
    clock.advance(9.0)
    tracker.observe({"out_time_seconds": 2.0, "speed": None})
    assert not tracker.stalled()
    clock.advance(20.0)
    assert tracker.stalled()


def test_malformed_and_regressing_progress_never_resets_liveness() -> None:
    """SAF-12: malformed, non-finite and regressing values are not progress."""
    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe({"frame": 10})
    clock.advance(5.0)
    tracker.observe({"frame": "not-a-number"})
    tracker.observe({"frame": float("nan")})
    tracker.observe({"frame": 5})
    tracker.observe({"heartbeat": True})
    clock.advance(6.0)
    assert tracker.stalled()


def test_startup_and_finalization_allowances_are_bounded() -> None:
    """SAF-12: startup and finalization cannot hide a stall forever."""
    clock = Clock()
    tracker = _tracker(clock)
    clock.advance(14.0)
    assert not tracker.stalled()
    clock.advance(2.0)
    assert tracker.stalled()

    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe({"frame": 10})
    tracker.observe({"finalizing": True, "percent": 100.0})
    clock.advance(4.0)
    assert not tracker.stalled()
    clock.advance(2.0)
    assert tracker.stalled()


def test_pause_suspends_the_stall_clock_and_resume_does_not_trip_immediately() -> None:
    """SAF-9/SAF-12: intentional pause time is not charged to the stall."""
    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe({"frame": 10})
    clock.advance(1.0)
    tracker.mark_paused()
    clock.advance(100.0)
    tracker.mark_resumed()
    assert not tracker.stalled()
    clock.advance(9.0)
    assert not tracker.stalled()
    clock.advance(2.0)
    assert tracker.stalled()


def test_run_process_cancellation_terminates_and_reaps_the_owned_child(
    tmp_path: Path,
) -> None:
    """SUP-2: cancellation stops owned work and leaves no child behind."""
    pid_file = tmp_path / "pid"
    script = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    cancel = threading.Event()
    timer = threading.Timer(0.25, cancel.set)
    timer.start()
    try:
        result = run_process(
            [sys.executable, "-c", script],
            cancel_event=cancel,
            policy=SupervisionPolicy(stall_timeout=None),
        )
    finally:
        timer.cancel()
    assert result.state is OperationState.CANCELLED
    assert result.returncode != 0
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_paused_child_can_be_cancelled_without_hanging(tmp_path: Path) -> None:
    """SAF-9/SAF-12: SIGCONT precedes termination of a stopped child."""
    pid_file = tmp_path / "pid"
    script = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    cancel = threading.Event()
    monitor_calls = 0

    def monitor() -> ProtectionDecision:
        nonlocal monitor_calls
        monitor_calls += 1
        if monitor_calls == 1:
            return ProtectionDecision(ProtectionAction.PAUSE, ("test-pause",))
        return ProtectionDecision(ProtectionAction.CONTINUE)

    timer = threading.Timer(0.75, cancel.set)
    timer.start()
    began = time.monotonic()
    try:
        result = run_process(
            [sys.executable, "-c", script],
            cancel_event=cancel,
            monitor=monitor,
            policy=SupervisionPolicy(stall_timeout=None),
        )
    finally:
        timer.cancel()
    assert time.monotonic() - began < 5
    assert result.state is OperationState.CANCELLED
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_supervise_callable_ignores_a_late_result_after_cancellation() -> None:
    """SUP-2: a cancelled operation cannot complete successfully afterwards."""
    cancel = threading.Event()

    def operation(report) -> str:
        report({"frame": 1})
        cancel.wait(2)
        return "late success"

    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = supervise_callable(
            operation,
            cancel_event=cancel,
            policy=SupervisionPolicy(stall_timeout=None),
        )
    finally:
        timer.cancel()
    assert result.state is OperationState.CANCELLED
    assert result.value is None


def test_protection_termination_is_distinct_from_process_failure() -> None:
    """SUP-3: a protection interruption is not an unexplained failure."""
    cancel = threading.Event()

    def operation(report, stop_event) -> str:
        while not stop_event.is_set():
            report({"frame": 1})
            time.sleep(0.01)
        return "stopped"

    def monitor() -> ProtectionDecision:
        return ProtectionDecision(
            ProtectionAction.TERMINATE,
            ("on-battery",),
            detail="AC was lost",
        )

    result = supervise_callable(
        operation,
        cancel_event=cancel,
        monitor=monitor,
        policy=SupervisionPolicy(stall_timeout=None),
    )
    assert result.state is OperationState.INTERRUPTED
    assert result.reasons == ("on-battery",)
    assert result.reason == "AC was lost"
    assert result.worker_stopped
    assert result.worker_done is not None and result.worker_done.is_set()
    # An internal protection outcome must not contaminate the caller's
    # cancellation event.
    assert not cancel.is_set()


def test_progress_dimensions_are_compared_independently() -> None:
    """A frame counter and a media timestamp are separate work dimensions."""

    clock = Clock()
    tracker = _tracker(clock)
    tracker.observe({"frame": 100, "out_time_seconds": 4.0})
    clock.advance(5.0)
    observation = tracker.observe({"out_time_seconds": 5.0})
    assert observation.advanced is True
    assert not tracker.stalled()


def test_run_process_does_not_drop_progress_lines() -> None:
    """SUP-1: every progress record is observable, even when the child is noisy."""

    script = (
        "import sys\n"
        "for i in range(2000):\n"
        "    print(f'progress={i}', flush=True)\n"
    )
    seen: list[str] = []

    def parse(line: str, _state: dict[str, str]):
        seen.append(line.strip())
        return {"work_progress": len(seen)}

    result = run_process(
        [sys.executable, "-c", script],
        progress_parser=parse,
        policy=SupervisionPolicy(stall_timeout=None),
    )
    assert result.state is OperationState.SUCCEEDED
    assert len(seen) == 2000
    assert seen[-1] == "progress=1999"


def test_processing_slot_finishes_with_a_bounded_deferral() -> None:
    """SUP-2/3: a busy expensive-operation slot is a typed, finite outcome."""

    from mediaflow.supervision import SlotBusy, processing_slot

    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"mediaflow-slot-{os.getpid()}.lock"
    try:
        with processing_slot(path):
            began = time.monotonic()
            with pytest.raises(SlotBusy):
                with processing_slot(path, timeout=0.15):
                    pass
            assert time.monotonic() - began < 2.0
    finally:
        path.unlink(missing_ok=True)
