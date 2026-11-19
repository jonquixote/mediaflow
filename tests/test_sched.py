"""Pure schedule start-gate helpers.

Requirement marker: SCHED-1.  A job may start only when it is inside the
window and its estimate completes before the close; missing estimates fail
cold-start and there is no consultation of already-started jobs.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from mediaflow.plan import PlanError, estimate_fits_window, schedule_window


def test_schedule_window_parsing():
    assert schedule_window() is None
    assert schedule_window("") is None
    assert schedule_window("22:00-06:00") == (time(22, 0), time(6, 0))
    assert schedule_window({"start": "07:30", "end": "19:45"}) == (
        time(7, 30),
        time(19, 45),
    )
    assert schedule_window((time(1, 0), "02:30")) == (time(1, 0), time(2, 30))
    for bad in ("22:00", "24:00-06:00", "6:00-7:00", "22:00-06:00-01:00", "nope"):
        with pytest.raises(PlanError):
            schedule_window(bad)


def test_no_window_always_fits_even_without_estimate():
    now = datetime(2026, 1, 1, 12, 0)
    assert estimate_fits_window(3600, None, None, now) == (True, "no-window")
    assert estimate_fits_window(3600, None, "", now) == (True, "no-window")


def test_missing_estimate_is_cold_start():
    now = datetime(2026, 1, 1, 12, 0)
    assert estimate_fits_window(3600, None, "11:00-20:00", now) == (
        False,
        "cold-start",
    )


def test_fits_and_exceeds_same_day_window():
    now = datetime(2026, 1, 1, 12, 0)
    assert estimate_fits_window(3600, 600, "11:00-20:00", now) == (True, "fits")
    assert estimate_fits_window(3600, 9 * 3600, "11:00-20:00", now) == (
        False,
        "estimate-exceeds-window",
    )


def test_completion_exactly_at_close_fits_but_now_at_close_does_not():
    now = datetime(2026, 1, 1, 11, 0)
    assert estimate_fits_window(3600, 3600, "11:00-12:00", now) == (True, "fits")
    assert estimate_fits_window(3600, 0, "11:00-12:00", datetime(2026, 1, 1, 12, 0)) == (
        False,
        "outside-window",
    )


def test_outside_window_is_not_a_start():
    window = "11:00-20:00"
    assert estimate_fits_window(3600, 60, window, datetime(2026, 1, 1, 10, 59)) == (
        False,
        "outside-window",
    )
    assert estimate_fits_window(3600, 60, window, datetime(2026, 1, 1, 20, 1)) == (
        False,
        "outside-window",
    )


def test_overnight_window_supports_either_side_of_midnight():
    window = "22:00-06:00"
    # 23:00 -> 07:00 next day would overrun the 06:00 close.
    assert estimate_fits_window(3600, 2 * 3600, window, datetime(2026, 1, 1, 23, 0)) == (
        True,
        "fits",
    )
    assert estimate_fits_window(3600, 8 * 3600, window, datetime(2026, 1, 1, 23, 0)) == (
        False,
        "estimate-exceeds-window",
    )
    # 01:00 is still in the window that opened yesterday.
    assert estimate_fits_window(3600, 4 * 3600, window, datetime(2026, 1, 2, 1, 0)) == (
        True,
        "fits",
    )
    assert estimate_fits_window(3600, 6 * 3600, window, datetime(2026, 1, 2, 1, 0)) == (
        False,
        "estimate-exceeds-window",
    )
    # Midday is outside an overnight window.
    assert estimate_fits_window(3600, 60, window, datetime(2026, 1, 2, 12, 0)) == (
        False,
        "outside-window",
    )


def test_zero_length_window_never_fits():
    now = datetime(2026, 1, 1, 12, 0)
    assert estimate_fits_window(3600, 0, "12:00-12:00", now) == (
        False,
        "estimate-exceeds-window",
    )


def test_duration_and_timezone_do_not_change_the_gate():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert estimate_fits_window(7200, 60, "11:00-20:00", now) == (True, "fits")
    # duration_seconds is informational; only the wall-clock estimate matters.
    assert estimate_fits_window(999999, 60, "11:00-20:00", now) == (True, "fits")
