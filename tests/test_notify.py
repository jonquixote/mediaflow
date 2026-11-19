"""Notification safety, redaction, cooldown and event tests (SAF-10)."""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from mediaflow.notify import Notifier

URL = "https://ntfy.example/secret-topic?auth=TOKEN123"
SECRET_TOKEN = "TOKEN123"


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeStore:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.sent: set[tuple[str, str | None]] = set()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def notification_was_sent(self, event_key: str, job_id: str | None) -> bool:
        return (event_key, job_id) in self.sent

    def notification_mark(self, event_key: str, job_id: str | None) -> None:
        self.sent.add((event_key, job_id))


class Response:
    status = 200

    def __init__(self) -> None:
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self) -> None:
        self.requests: list[SimpleNamespace] = []

    def open(self, request, timeout=None):
        self.requests.append(
            SimpleNamespace(
                url=request.full_url,
                body=request.data.decode("utf-8"),
                headers={key.lower(): value for key, value in request.header_items()},
                method=request.get_method(),
                timeout=timeout,
            )
        )
        return Response()

    @property
    def count(self) -> int:
        return len(self.requests)


class FailingOpener:
    def __init__(self, message: str = URL) -> None:
        self.message = message

    def open(self, request, timeout=None):
        raise RuntimeError(self.message)


def config(**overrides):
    values = {
        "notify_cooldown_hours": 6,
        "review_reminder_days": 3,
        "movies": "/srv/media/movies",
        "ffprobe": "/usr/lib/jellyfin-ffmpeg/ffprobe",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def secrets(url: str | None = URL):
    return SimpleNamespace(notify_url=url, token="tmdb-token-must-not-leak")


def make_notifier(store=None, opener=None, clock=None, cfg=None, sec=None) -> Notifier:
    return Notifier(
        cfg or config(),
        sec if sec is not None else secrets(),
        store if store is not None else FakeStore(),
        opener=opener if opener is not None else RecordingOpener(),
        clock=clock or Clock(),
    )


def test_missing_url_is_a_silent_noop():
    opener = RecordingOpener()
    store = FakeStore()
    notifier = make_notifier(store=store, opener=opener, sec=secrets(None))
    assert notifier.notify("failed", "job1", {"reason": "encode"}) is False
    assert opener.count == 0
    assert store.settings == {}
    assert store.sent == set()
    assert notifier.redacted_url() == ""


def test_payload_contains_ids_and_counts_but_never_titles_or_paths():
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener)
    details = {
        "reason": "encode-failed",
        "count": 3,
        "age_seconds": 120,
        "title": "The Secret Movie Title",
        "filename": "The.Secret.Movie.Title.mkv",
        "path": "/srv/media/_pipeline/convert/movies/The Secret Movie/file.mkv",
        "subtitle": "A title-like value",
    }
    assert notifier.notify("failed", "JOB-123", details) is True

    assert opener.count == 1
    request = opener.requests[0]
    assert request.method == "POST"
    assert request.url == URL
    assert "text/plain" in request.headers["content-type"]
    assert "authorization" not in request.headers
    assert request.headers["title"] == "mediaflow: job failed"
    assert request.timeout and request.timeout > 0

    body = request.body
    assert "JOB-123" in body
    assert "count: 3" in body
    assert "reason: encode-failed" in body
    assert "age_seconds: 120" in body
    assert "timestamp:" in body
    assert "The Secret Movie Title" not in body
    assert "The.Secret.Movie.Title.mkv" not in body
    assert "/srv/media/_pipeline/convert" not in body
    assert "A title-like value" not in body
    assert SECRET_TOKEN not in body


def test_cooldown_blocks_repeat_then_sends_after_advancing_clock():
    clock = Clock()
    opener = RecordingOpener()
    store = FakeStore()
    notifier = make_notifier(store=store, opener=opener, clock=clock)

    assert notifier.notify("failed", "job1", {"reason": "encode"}) is True
    assert notifier.notify("failed", "job1", {"reason": "encode"}) is False
    assert opener.count == 1

    clock.advance(6 * 3600 - 1)
    assert notifier.notify("failed", "job1", {"reason": "encode"}) is False
    assert opener.count == 1

    clock.advance(2)
    assert notifier.notify("failed", "job1", {"reason": "encode"}) is True
    assert opener.count == 2

    # The persisted ISO timestamp carries the cooldown across a restart.
    stored = store.settings["notify_sent:failed:job1"]
    parsed = datetime.fromisoformat(stored)
    assert parsed.tzinfo is not None
    restarted = make_notifier(store=store, opener=opener, clock=clock)
    assert restarted.notify("failed", "job1", {"reason": "encode"}) is False
    clock.advance(6 * 3600 + 1)
    assert restarted.notify("failed", "job1", {"reason": "encode"}) is True
    assert opener.count == 3


def test_event_keys_and_job_ids_have_independent_cooldowns():
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener)
    assert notifier.notify("failed", "job1", {"reason": "x"}) is True
    assert notifier.notify("review", "job1", {"reason": "hdr"}) is True
    assert notifier.notify("failed", "job2", {"reason": "x"}) is True
    assert notifier.notify("failed", "job1", {"reason": "x"}) is False
    assert notifier.notify("review-reminder", "job1", {"count": 1}) is True
    assert opener.count == 4


def test_url_in_config_is_rejected_without_leaking_the_value():
    with pytest.raises(ValueError) as excinfo:
        Notifier(config(notify_url=URL), secrets(), FakeStore(), opener=RecordingOpener())
    message = str(excinfo.value)
    assert URL not in message
    assert "secret-topic" not in message
    assert SECRET_TOKEN not in message

    with pytest.raises(ValueError) as nested:
        Notifier({"notify": {"url": URL}}, secrets(), FakeStore())
    assert URL not in str(nested.value)

    class ClassAttributeConfig:
        notify_url = URL
        notify_cooldown_hours = 6

    with pytest.raises(ValueError):
        Notifier(ClassAttributeConfig(), secrets(), FakeStore())

    # Ordinary notification settings are not mistaken for a URL.
    Notifier({"notify_cooldown_hours": 6}, secrets(None), FakeStore())


def test_redacted_url_and_repr_hide_topic_credentials_and_query():
    notifier = make_notifier(sec=secrets("https://user:pass@ntfy.example:8443/topic?token=SECRET"))
    assert notifier.redacted_url() == "https://ntfy.example:8443/\u2026"
    rendered = repr(notifier)
    assert notifier.redacted_url() in rendered
    for secret in ("user", "pass", "topic", "SECRET", "SECRET123"):
        assert secret not in rendered
    assert "https://user" not in rendered


def test_logs_never_print_url_even_when_transport_fails(caplog):
    caplog.set_level(logging.DEBUG, logger="mediaflow.notify")
    opener = FailingOpener(message=f"connection to {URL} failed")
    store = FakeStore()
    notifier = make_notifier(store=store, opener=opener)

    assert notifier.notify("failed", "job1", {"reason": "encode"}) is False
    assert "ntfy.example" not in caplog.text
    assert "secret-topic" not in caplog.text
    assert SECRET_TOKEN not in caplog.text
    # A failed transport is not recorded as sent.
    assert store.settings == {}
    assert store.sent == set()


def test_success_logs_do_not_print_the_url(caplog):
    caplog.set_level(logging.DEBUG, logger="mediaflow.notify")
    notifier = make_notifier(opener=RecordingOpener())
    assert notifier.notify("success", "job1", {"count": 1}) is True
    assert "notification sent" in caplog.text
    assert "ntfy.example" not in caplog.text
    assert "secret-topic" not in caplog.text


def test_header_injection_in_details_and_job_id_is_sanitised():
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener)
    assert (
        notifier.notify(
            "failed",
            "job\r\nX-Evil: injected-job",
            {"reason": "bad\r\nInjected: yes", "count": 2},
        )
        is True
    )
    request = opener.requests[0]
    assert "\r" not in request.headers["title"]
    assert "\n" not in request.headers["title"]
    assert "\r" not in request.body
    assert "\nInjected:" not in request.body
    assert "\nX-Evil:" not in request.body
    assert "\nX-Count:" not in request.body
    assert "reason: bad-injected-yes" in request.body
    assert "count: 2" in request.body


def test_review_reminders_only_for_old_review_jobs():
    now = 1_700_000_000.0
    clock = Clock(now + 10_000)  # `now=` must drive the reminder itself
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener, clock=clock)
    jobs = [
        {
            "id": "old-review",
            "state": "REVIEW",
            "updated_at": now - 4 * 86400,
            "review_reason": "hdr",
            "title": "Old Secret Title",
        },
        {
            "id": "fresh-review",
            "state": "REVIEW",
            "updated_at": now - 1 * 86400,
            "review_reason": "bitmap-subs",
            "title": "Fresh Secret Title",
        },
        {
            "id": "old-done",
            "state": "DONE",
            "updated_at": now - 20 * 86400,
            "title": "Done Secret Title",
        },
        {"id": "no-time", "state": "REVIEW", "title": "No Time Secret"},
    ]
    assert notifier.send_review_reminders(jobs, now=now) == 1
    assert opener.count == 1
    body = opener.requests[0].body
    assert "event: review-reminder" in body
    assert "old-review" in body
    assert "count: 1" in body
    assert "review_count: 1" in body
    assert "reason: hdr" in body
    assert "Old Secret Title" not in body
    assert "Fresh Secret Title" not in body

    # Cooldown suppresses an immediate repeat for the same job.
    assert notifier.send_review_reminders(jobs, now=now + 60) == 0
    assert opener.count == 1


def test_review_reminders_are_per_job_and_respect_cooldown():
    now = 1_700_000_000.0
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener, clock=Clock(now))
    jobs = [
        {"id": "a", "state": "REVIEW", "created_at": now - 5 * 86400},
        {"id": "b", "state": "REVIEW", "created_at": now - 6 * 86400},
    ]
    assert notifier.send_review_reminders(jobs, now=now) == 2
    assert opener.count == 2
    assert {"a", "b"} <= {
        line.split(": ", 1)[1]
        for request in opener.requests
        for line in request.body.splitlines()
        if line.startswith("job_id: ")
    }
    notifier2 = make_notifier(opener=opener, clock=Clock(now + 7 * 3600))
    assert notifier2.send_review_reminders(jobs, now=now + 7 * 3600) == 2
    assert opener.count == 4


def test_paused_only_after_thirty_minutes_and_independent_of_other_events():
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener)
    assert (
        notifier.send_paused(["estimate-exceeds-window"], queued_for_seconds=1799)
        is False
    )
    assert opener.count == 0
    assert (
        notifier.send_paused(
            ["estimate-exceeds-window", "thermal"],
            queued_for_seconds=1800,
        )
        is True
    )
    assert opener.count == 1
    body = opener.requests[0].body
    assert "event: paused" in body
    assert "job_id: -" in body
    assert "count: 2" in body
    assert "reason: multiple" in body
    assert "age_seconds: 1800" in body
    assert notifier.send_paused(["estimate-exceeds-window"], queued_for_seconds=1900) is False
    assert opener.count == 1


def test_pause_reason_mapping_counts_jobs():
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener)
    assert notifier.send_paused(
        {"estimate-exceeds-window": 2, "thermal": 1}, queued_for_seconds=1800
    )
    body = opener.requests[0].body
    assert "count: 3" in body
    assert "reason: multiple" in body


def test_trash_purge_notification_is_aggregate_only():
    opener = RecordingOpener()
    notifier = make_notifier(opener=opener)
    assert notifier.send_purge(4) is True
    body = opener.requests[0].body
    assert "event: trash-purge" in body
    assert "count: 4" in body
    assert "job_id: -" in body
    assert "." not in body.split("timestamp:")[0].replace("trash-purge", "")
    assert notifier.send_purge(4) is False


def test_no_url_and_unsupported_scheme_are_silent():
    opener = RecordingOpener()
    store = FakeStore()
    no_url = make_notifier(store=store, opener=opener, sec=secrets(""))
    assert no_url.notify("failed", "job", {"reason": "x"}) is False
    bad_scheme = make_notifier(
        store=store, opener=opener, sec=secrets("ftp://ntfy.example/topic")
    )
    assert bad_scheme.notify("failed", "job", {"reason": "x"}) is False
    assert opener.count == 0
    assert store.settings == {}
