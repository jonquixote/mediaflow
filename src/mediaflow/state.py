"""One authoritative state vocabulary for jobs, reviews and persisted work.

Every state the engine can persist is declared here exactly once.  Database
queries, engine decisions, CLI views, notifications, retention sweeps and
recovery all derive their classifications from these definitions.  Never add a
second tuple of state strings in another module.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable

__all__ = [
    "ACTIVE_STATES",
    "ALLOWED_TRANSITIONS",
    "COMMAND_NAMES",
    "COMMAND_STATUSES",
    "INTENT_KINDS",
    "INTENT_STATUSES",
    "NOTIFICATION_EVENTS",
    "RESUMABLE_STATES",
    "REVIEW_HINTS",
    "REVIEW_MOVABLE",
    "REVIEW_REASONS",
    "REVIEW_RESUME",
    "REVIEW_STATES",
    "SCAN_BLOCKING_STATES",
    "TERMINAL_STATES",
    "USER_VISIBLE_STATES",
    "JobState",
    "classify",
    "command_status",
    "in_clause",
    "is_active",
    "is_review",
    "is_terminal",
    "is_valid_transition",
    "validate_state_model",
    "values",
]


class JobState(str, Enum):
    """Every persistable job state.

    ``RESTORED`` is terminal for scanning and queue purposes, but its history,
    manifest and trash/published copies remain visible to status, listing,
    retention and restore-related commands.
    """

    DETECTED = "DETECTED"
    STABLE = "STABLE"
    PROBED = "PROBED"
    PLANNED = "PLANNED"
    ENCODING = "ENCODING"
    ENCODED = "ENCODED"
    VERIFIED = "VERIFIED"
    NAMED = "NAMED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"
    REVIEW = "REVIEW"
    DONE = "DONE"
    FAILED = "FAILED"
    RESTORED = "RESTORED"


#: States that still represent live work and therefore block a re-drop of the
#: same preset/source path.  REVIEW is active: the original is parked there.
ACTIVE_STATES: frozenset[JobState] = frozenset({
    JobState.DETECTED,
    JobState.STABLE,
    JobState.PROBED,
    JobState.PLANNED,
    JobState.ENCODING,
    JobState.ENCODED,
    JobState.VERIFIED,
    JobState.NAMED,
    JobState.PUBLISHED,
    JobState.RETIRED,
    JobState.REVIEW,
})

#: No further pipeline work is expected.  History remains queryable.
TERMINAL_STATES: frozenset[JobState] = frozenset({
    JobState.DONE,
    JobState.FAILED,
    JobState.RESTORED,
})

#: States a recoverable job may resume from.  Terminal history is excluded.
RESUMABLE_STATES: frozenset[JobState] = frozenset(ACTIVE_STATES)

#: A review is an active hold with a fixed resume state (REV-1).
REVIEW_STATES: frozenset[JobState] = frozenset({JobState.REVIEW})

#: Everything status/queue/JSON may show.  Terminal history is included.
USER_VISIBLE_STATES: frozenset[JobState] = frozenset(JobState)

#: Alias used by scanning: only active work blocks a fresh drop.
SCAN_BLOCKING_STATES: frozenset[JobState] = ACTIVE_STATES

#: Every REVIEW reason the planner, engine or identity resolver can produce.
#: ``None`` means the reason can only be rejected, not accepted.
REVIEW_REASON_RESUME: dict[str, JobState | None] = {
    "multiple-videos": JobState.PROBED,
    "unsupported-format": None,
    "hdr": JobState.PLANNED,
    "10bit": JobState.PLANNED,
    "10bit-no-hw": JobState.PLANNED,
    "interlaced": JobState.PLANNED,
    "remux-incompatible-video": JobState.PLANNED,
    "quality-floor": JobState.NAMED,
    "identity-low-confidence": JobState.NAMED,
    "identity-tie": JobState.NAMED,
    "duplicate": JobState.PUBLISHED,
    "multi-disc": None,
}
REVIEW_REASONS: frozenset[str] = frozenset(REVIEW_REASON_RESUME)
REVIEW_RESUME: dict[str, JobState | None] = dict(REVIEW_REASON_RESUME)

#: Operator-facing hint for each review reason, kept with the reason itself so
#: the CLI cannot drift from the engine's vocabulary.
#: Reasons cleared by moving the job to another preset.  Derived here so the
#: engine and CLI cannot disagree about which holds are movable.
REVIEW_MOVABLE: frozenset[str] = frozenset({
    "hdr", "10bit", "10bit-no-hw", "interlaced",
    "remux-incompatible-video", "quality-floor",
})

REVIEW_HINTS: dict[str, str] = {
    "multiple-videos": "review accept --main FILE",
    "unsupported-format": "review reject",
    "hdr": "review move --preset",
    "10bit": "review move --preset",
    "10bit-no-hw": "review move --preset",
    "interlaced": "review move --preset",
    "remux-incompatible-video": "review move --preset",
    "quality-floor": "review accept --publish-anyway",
    "identity-low-confidence": "review accept --candidate N",
    "identity-tie": "review accept --candidate N",
    "duplicate": "review accept --replace",
    "multi-disc": "review reject",
}

#: Legitimate forward transitions.  FAILED is reachable from every active
#: state; REVIEW is a hold that resumes at the recorded state.  A transition
#: outside this map is a programming error, not a recovery case.
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DETECTED: frozenset({JobState.STABLE, JobState.FAILED, JobState.REVIEW}),
    JobState.STABLE: frozenset({JobState.PROBED, JobState.FAILED, JobState.REVIEW}),
    JobState.PROBED: frozenset({JobState.PLANNED, JobState.FAILED, JobState.REVIEW}),
    JobState.PLANNED: frozenset({JobState.ENCODING, JobState.FAILED, JobState.REVIEW}),
    JobState.ENCODING: frozenset({
        JobState.PROBED, JobState.PLANNED, JobState.ENCODED,
        JobState.FAILED, JobState.REVIEW,
    }),
    JobState.ENCODED: frozenset({
        JobState.PROBED, JobState.PLANNED, JobState.VERIFIED,
        JobState.FAILED, JobState.REVIEW,
    }),
    JobState.VERIFIED: frozenset({
        JobState.PROBED, JobState.NAMED, JobState.FAILED, JobState.REVIEW,
    }),
    JobState.NAMED: frozenset({JobState.PUBLISHED, JobState.FAILED, JobState.REVIEW}),
    JobState.PUBLISHED: frozenset({
        JobState.NAMED, JobState.RETIRED, JobState.FAILED, JobState.REVIEW,
    }),
    JobState.RETIRED: frozenset({JobState.DONE, JobState.FAILED}),
    JobState.REVIEW: frozenset({
        JobState.PROBED, JobState.PLANNED, JobState.NAMED,
        JobState.PUBLISHED, JobState.FAILED,
    }),
    JobState.FAILED: frozenset({JobState.PROBED}),
    JobState.DONE: frozenset({JobState.RESTORED}),
    # Re-running a restore intent on an already-restored job is idempotent.
    JobState.RESTORED: frozenset({JobState.RESTORED}),
}


def is_valid_transition(source: str | JobState, target: str | JobState) -> bool:
    """Whether a compare-and-set transition is structurally legitimate.

    Same-state writes are idempotent adoption/recovery operations (for example
    re-committing a PUBLISHED marker after a crash), so they are always legal.
    """

    source_state = classify(source)
    target_state = classify(target)
    if source_state is target_state:
        return True
    return target_state in ALLOWED_TRANSITIONS[source_state]


#: Persisted two-phase intents.
INTENT_KINDS: frozenset[str] = frozenset({"replace", "restore"})

#: Lifecycle values of `commands.status` and `intents.status`.
COMMAND_STATUSES: frozenset[str] = frozenset({"pending", "running", "done", "failed"})

#: Real intents use `pending`/`done`; `running`/`failed` are retained in the
#: shared status vocabulary for commands and future two-phase work.
INTENT_STATUSES: frozenset[str] = frozenset({"pending", "done"})

#: Every command the daemon can dispatch.
COMMAND_NAMES: frozenset[str] = frozenset({
    "review_accept", "review_move", "review_reject", "retry", "cancel",
    "pause", "resume", "trash_purge", "failed_sweep", "plan_refresh", "restore",
})

#: Notification event keys emitted by the engine and notifier.
NOTIFICATION_EVENTS: frozenset[str] = frozenset({
    "failed", "review", "review-reminder", "paused", "trash-purge", "success",
})


def classify(state: str | JobState) -> JobState:
    """Return the shared enum for a persisted state string."""

    return state if isinstance(state, JobState) else JobState(str(state))


def is_active(state: str | JobState) -> bool:
    return classify(state) in ACTIVE_STATES


def is_terminal(state: str | JobState) -> bool:
    return classify(state) in TERMINAL_STATES


def is_review(state: str | JobState) -> bool:
    return classify(state) in REVIEW_STATES


def values(states: Iterable[str | JobState]) -> tuple[str, ...]:
    """Stable string values for SQL parameters or audit output."""

    return tuple(classify(state).value for state in states)


def command_status(name: str) -> str:
    """Validate and return a command/intent lifecycle status."""

    if name not in COMMAND_STATUSES:
        raise ValueError(f"unknown command status: {name!r}")
    return name


def in_clause(states: Iterable[str | JobState], *, prefix: str = "") -> tuple[str, tuple[str, ...]]:
    """Return ``"?,?,..."`` and ordered values generated from shared states.

    SQL never hardcodes a state set: placeholders are derived from the same
    enumeration the engine uses for decisions.
    """

    vals = values(states)
    return ",".join(f"{prefix}?" for _ in vals), vals


def validate_state_model(enum_cls: type[Enum] = JobState) -> None:
    """Fail when a state is added without a classification."""

    members = set(enum_cls)
    assert members == set(USER_VISIBLE_STATES), (
        f"unclassified job states: {sorted(members - set(USER_VISIBLE_STATES))}"
    )
    assert members == set(ACTIVE_STATES) | set(TERMINAL_STATES)
    assert not set(ACTIVE_STATES) & set(TERMINAL_STATES)
    assert set(REVIEW_STATES) <= set(ACTIVE_STATES)
    assert set(REVIEW_RESUME) == set(REVIEW_HINTS) == set(REVIEW_REASONS)
    assert REVIEW_MOVABLE <= set(REVIEW_REASONS)
    assert set(ALLOWED_TRANSITIONS) == members
    for source, targets in ALLOWED_TRANSITIONS.items():
        assert targets <= members


# Fail fast at import time if a new state is added without classification.
validate_state_model()
