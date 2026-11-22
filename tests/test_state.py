"""The shared job-state vocabulary and its classifications.

[STATE-1] Every state the code can persist must be classified exactly once by
one shared model.  A new state without a classification is a test failure.
"""

from __future__ import annotations

import pytest

from mediaflow.state import (
    ACTIVE_STATES,
    COMMAND_STATUSES,
    INTENT_KINDS,
    REVIEW_REASONS,
    REVIEW_STATES,
    RESUMABLE_STATES,
    TERMINAL_STATES,
    USER_VISIBLE_STATES,
    JobState,
)


def test_every_job_state_has_exactly_one_primary_classification():
    all_states = set(JobState)
    assert all_states == set(USER_VISIBLE_STATES)
    assert not (set(ACTIVE_STATES) & set(TERMINAL_STATES))
    assert not (set(REVIEW_STATES) - set(ACTIVE_STATES))
    assert set(REVIEW_STATES) == {JobState.REVIEW}
    assert set(TERMINAL_STATES) == {
        JobState.DONE,
        JobState.FAILED,
        JobState.RESTORED,
    }
    assert set(ACTIVE_STATES) | set(TERMINAL_STATES) == all_states


def test_resumable_states_exclude_terminal_history():
    assert not (set(RESUMABLE_STATES) & set(TERMINAL_STATES))
    assert JobState.ENCODING in RESUMABLE_STATES
    assert JobState.PUBLISHED in RESUMABLE_STATES
    assert JobState.DONE not in RESUMABLE_STATES


def test_review_reasons_and_intents_are_enumerated():
    assert set(REVIEW_REASONS) >= {
        "hdr", "10bit", "10bit-no-hw", "interlaced",
        "remux-incompatible-video", "quality-floor", "duplicate",
        "identity-low-confidence", "identity-tie", "multiple-videos",
        "unsupported-format", "multi-disc",
    }
    assert set(INTENT_KINDS) == {"replace", "restore"}
    assert set(COMMAND_STATUSES) == {"pending", "running", "done", "failed"}


def test_unclassified_state_is_rejected():
    """Adding an enum member without updating the sets must fail loudly."""

    from enum import Enum

    from mediaflow.state import validate_state_model

    class ExtraState(str, Enum):
        DETECTED = "DETECTED"
        FAILED = "FAILED"
        NEW_STATE = "NEW_STATE"

    with pytest.raises(AssertionError):
        validate_state_model(ExtraState)
