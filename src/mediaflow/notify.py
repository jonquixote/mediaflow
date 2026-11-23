"""Outbound notifications with strict secret and payload hygiene.

The notification URL is a secret and comes *only* from ``notify.env`` via
:class:`mediaflow.config.Secrets` (SAF-10).  A URL placed in the ordinary
configuration is rejected at construction time.  Nothing in this module logs,
returns in an exception, or puts in a payload the URL, the URL topic, a media
title, a filename or a path under ``convert``.

Payloads contain only a small, whitelisted set of fields:

``event``, ``job_id``, ``reason`` (a slug), ``count``/``review_count``,
``age_seconds``, ``free_gb``, ``trash_gb`` and ``timestamp``.

Cooldown state is persisted as an ISO-8601 timestamp in the existing settings
table under ``notify_sent:<event>:<jobid>``.  No database schema change is
required.  The in-memory ``_last_sent`` map is authoritative for the lifetime
of a notifier instance; the settings value lets a restarted daemon continue to
honour the same cooldown.
"""

from __future__ import annotations

import inspect
import logging
import math
import re
import time
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .state import NOTIFICATION_EVENTS, is_review

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_HOURS = 6.0
DEFAULT_TIMEOUT_SECONDS = 10.0
PAUSED_MIN_SECONDS = 30 * 60
SETTING_PREFIX = "notify_sent:"

#: Only used when a caller explicitly configures an effectively permanent
#: cooldown and no timestamp has been persisted yet.
_PERMANENT_COOLDOWN_HOURS = 24 * 365 * 100

_CONFIG_URL_KEYS = frozenset(
    {"url", "notifyurl", "notificationurl", "webhook", "webhookurl", "ntfy", "ntfyurl"}
)
_SECRET_URL_KEYS = (
    "notify_url",
    "NOTIFY_URL",
    "notification_url",
    "NOTIFICATION_URL",
    "url",
    "URL",
)
_DETAIL_KEYS = ("reason", "count", "age_seconds", "free_gb", "trash_gb")
_HEADER_UNSAFE = re.compile(r"[\x00-\x1f\x7f]+")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_EVENT_TITLES = {
    "failed": "mediaflow: job failed",
    "review": "mediaflow: review required",
    "review-reminder": "mediaflow: review waiting",
    "paused": "mediaflow: queue paused",
    "trash-purge": "mediaflow: trash purged",
    "success": "mediaflow: job done",
}
assert set(_EVENT_TITLES) == NOTIFICATION_EVENTS


class NotificationError(RuntimeError):
    """Base class for notification construction errors (never carries a URL)."""


def _config_value(config: Any, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


def _config_items(config: Any) -> list[tuple[str, Any]]:
    """Return top-level config fields without triggering arbitrary properties."""
    if config is None:
        return []
    if isinstance(config, Mapping):
        return list(config.items())
    if is_dataclass(config) and not isinstance(config, type):
        result: list[tuple[str, Any]] = []
        for field in fields(config):
            try:
                result.append((field.name, getattr(config, field.name)))
            except Exception:  # pragma: no cover - defensive
                continue
        return result
    if hasattr(config, "__dict__"):
        items = list(vars(config).items())
        known = {name for name, _ in items}
        for name in ("notify_url", "NOTIFY_URL", "notification_url", "url", "webhook", "ntfy"):
            if name not in known and hasattr(config, name):
                items.append((name, getattr(config, name)))
        return items
    candidate_names = (
        "notify_url",
        "NOTIFY_URL",
        "notification_url",
        "NOTIFICATION_URL",
        "url",
        "URL",
        "webhook",
        "ntfy",
    )
    return [(name, getattr(config, name, None)) for name in candidate_names if hasattr(config, name)]


def _normalise_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def _reject_config_url(config: Any, *, _nested: bool = False) -> None:
    """Raise ValueError when the non-secret config carries a notify URL.

    The value is never included in the exception (SAF-10).  Ordinary settings
    such as ``notify_cooldown_hours`` are unrelated and accepted.
    """
    for key, value in _config_items(config):
        normalised = _normalise_key(key)
        if normalised in _CONFIG_URL_KEYS or (
            ("notify" in normalised or "webhook" in normalised or "ntfy" in normalised)
            and "url" in normalised
        ):
            if value not in (None, "", False):
                raise ValueError(
                    "notify URL must come from notify.env, never from config (SAF-10)"
                )
        if normalised in {"notify", "notification", "webhook", "ntfy"} and isinstance(value, str):
            # e.g. config = {"notify": "https://..."} is still a URL in config.
            if "://" in value:
                raise ValueError(
                    "notify URL must come from notify.env, never from config (SAF-10)"
                )
        if (
            isinstance(value, str)
            and "://" in value
            and any(token in normalised for token in ("notify", "webhook", "ntfy", "url"))
        ):
            raise ValueError(
                "notify URL must come from notify.env, never from config (SAF-10)"
            )
        if not _nested and normalised in {
            "notify",
            "notification",
            "notifications",
            "webhook",
            "webhooks",
            "ntfy",
        }:
            if isinstance(value, Mapping):
                _reject_config_url(value, _nested=True)
            elif value is not None and not isinstance(value, (str, bool, int, float)):
                _reject_config_url(value, _nested=True)


def _secret_url(secrets: Any) -> str | None:
    """Read the URL only from a Secrets-like object or mapping."""
    if secrets is None:
        return None
    for name in _SECRET_URL_KEYS:
        value = secrets.get(name) if isinstance(secrets, Mapping) else getattr(secrets, name, None)
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
    return None


def _valid_http_url(value: str | None) -> bool:
    if not value or any(char in value for char in "\r\n\t"):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)


def _slug(value: Any, *, limit: int = 64) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = _SLUG_UNSAFE.sub("-", text).strip("-")
    return text[:limit].strip("-")


def _clean(value: Any) -> str:
    """Flatten control characters so a value cannot forge payload lines."""
    if value is None:
        return ""
    return " ".join(_HEADER_UNSAFE.sub(" ", str(value)).split())


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _fmt_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _count(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _timestamp(now: float) -> str:
    try:
        return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):  # pragma: no cover - extreme clock
        return str(now)


def _parse_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):  # pragma: no cover
        return None


def _detail_fields(details: Mapping | None) -> dict[str, Any]:
    """Copy only explicitly whitelisted, non-identifying detail values."""
    if not isinstance(details, Mapping):
        return {}
    result: dict[str, Any] = {}
    if "reason" in details:
        reason = _slug(details.get("reason"))
        if reason:
            result["reason"] = reason
    if "count" in details:
        count = _count(details.get("count"))
        if count is not None:
            result["count"] = count
    for key in ("age_seconds", "free_gb", "trash_gb"):
        if key in details:
            number = _number(details.get(key))
            if number is not None:
                result[key] = _fmt_number(number)
    return result


def _build_payload(event: str, job_id: str | None, fields: Mapping[str, Any], now: float) -> bytes:
    lines = [f"event: {_slug(event)}", f"job_id: {_clean(job_id) if job_id else '-'}"]
    for key in _DETAIL_KEYS:
        if key in fields:
            lines.append(f"{key}: {_clean(fields[key])}")
            if key == "count" and event == "review-reminder":
                lines.append(f"review_count: {_clean(fields[key])}")
    lines.append(f"timestamp: {_timestamp(now)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _title(event: str) -> str:
    title = _EVENT_TITLES.get(event, f"mediaflow: {event}")
    return _clean(title)[:120]


class Notifier:
    """Send redacted operational notifications with a persisted cooldown.

    ``opener`` is any object with ``open(request, timeout=...)`` (normally a
    :class:`urllib.request.OpenerDirector`).  It is injected so tests never use
    the network.  ``clock`` returns epoch seconds.
    """

    def __init__(
        self,
        config: Any,
        secrets: Any,
        store: Any,
        opener: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _reject_config_url(config)
        self._config = config
        self._secrets = secrets
        self._store = store
        self._clock = clock or time.time
        self._opener = opener if opener is not None else build_opener()

        configured_url = _secret_url(secrets)
        if configured_url and not _valid_http_url(configured_url):
            # Never include the candidate value in the log.
            log.warning("notify URL is invalid or unsupported; notifications disabled")
            self._url: str | None = None
        else:
            self._url = configured_url

        self._cooldown_hours = self._config_number(
            "notify_cooldown_hours", DEFAULT_COOLDOWN_HOURS, minimum=0.0
        )
        self._timeout = self._config_number(
            "notify_timeout_seconds", DEFAULT_TIMEOUT_SECONDS, minimum=0.1
        )
        self._last_sent: dict[tuple[str, str | None], float] = {}

    # -- configuration --------------------------------------------------
    def rebind(self, config: Any, secrets: Any) -> None:
        """Adopt a new live configuration generation at a safe boundary.

        The notification URL is deliberately not re-read: secret rotation is
        restart-required, and ``secrets`` is the same object until then.
        In-memory cooldown history is preserved.
        """

        _reject_config_url(config)
        self._config = config
        self._secrets = secrets
        self._cooldown_hours = self._config_number(
            "notify_cooldown_hours", DEFAULT_COOLDOWN_HOURS, minimum=0.0
        )
        self._timeout = self._config_number(
            "notify_timeout_seconds", DEFAULT_TIMEOUT_SECONDS, minimum=0.1
        )

    def _config_number(self, name: str, default: float, *, minimum: float) -> float:
        value = _config_value(self._config, name, default)
        if isinstance(value, bool) or value is None:
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if math.isnan(number):
            return default
        return max(minimum, number)

    def __repr__(self) -> str:
        return (
            f"Notifier(url={self.redacted_url()!r}, "
            f"cooldown_hours={self._cooldown_hours:g})"
        )

    __str__ = __repr__

    def redacted_url(self) -> str:
        """Return ``scheme://host/…`` with path, topic and credentials removed."""
        if not self._url:
            return ""
        try:
            parts = urlsplit(self._url)
            host = parts.hostname or ""
            if not parts.scheme or not host:
                return ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
        except (ValueError, TypeError):
            return ""
        if port is not None:
            host = f"{host}:{port}"
        return f"{parts.scheme.lower()}://{host}/\u2026"

    # -- public API -----------------------------------------------------
    def notify(
        self,
        event_key: str,
        job_id: str | None,
        details: Mapping | None = None,
    ) -> bool:
        """POST one redacted notification.  Returns True only after a send.

        Missing URL and cooldown suppression return False without raising.
        Transport failures also return False; exception text is never logged
        because it may contain the secret URL.
        """
        return self._notify_at(event_key, job_id, details, now=float(self._clock()))

    def send_review_reminders(
        self,
        jobs: Iterable[Mapping] | None,
        now: float | None = None,
    ) -> int:
        """Send one reminder for each REVIEW job older than the configured age.

        A job's review age starts at ``updated_at`` (set when REVIEW was
        entered), falling back to ``created_at`` and ``first_seen``.  Returns
        the number of notifications actually delivered; cooldown-suppressed
        jobs are not counted.
        """
        now_ts = float(self._clock() if now is None else now)
        days = self._config_number("review_reminder_days", 3.0, minimum=0.0)
        threshold = days * 86_400.0
        sent = 0
        for job in jobs or []:
            if not isinstance(job, Mapping):
                continue
            if not is_review(str(job.get("state") or "REVIEW").upper()):
                continue
            job_id = job.get("id") or job.get("job_id")
            if not job_id:
                continue
            reviewed_at = _review_timestamp(job)
            if reviewed_at is None:
                continue
            age = max(0.0, now_ts - reviewed_at)
            if age < threshold:
                continue
            reason = job.get("review_reason") or job.get("reason") or "review"
            if self._notify_at(
                "review-reminder",
                str(job_id),
                {"reason": reason, "count": 1, "age_seconds": age},
                now=now_ts,
            ):
                sent += 1
        return sent

    def send_paused(
        self,
        reasons: Any,
        *,
        queued_for_seconds: float = PAUSED_MIN_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Notify after the queue has been paused for at least 30 minutes."""
        age = _number(queued_for_seconds)
        if age is None or age < PAUSED_MIN_SECONDS:
            return False
        reason_slugs, count = _normalise_reasons(reasons)
        if count <= 0:
            return False
        reason = reason_slugs[0] if len(reason_slugs) == 1 else "multiple"
        now_ts = float(self._clock() if now is None else now)
        return self._notify_at(
            "paused",
            None,
            {"reason": reason, "count": count, "age_seconds": age},
            now=now_ts,
        )

    def send_purge(self, count: int, *, now: float | None = None) -> bool:
        """Notify about an explicit trash purge using only an aggregate count."""
        value = _count(count)
        if value is None:
            value = 0
        now_ts = float(self._clock() if now is None else now)
        return self._notify_at("trash-purge", None, {"count": value}, now=now_ts)

    # -- implementation -------------------------------------------------
    def _open(self, request: Request) -> Any:
        """Call an injected opener or callable, with a timeout when supported."""
        open_method = getattr(self._opener, "open", None)
        if not callable(open_method):
            if not callable(self._opener):
                raise TypeError("opener is not callable and has no open()")
            open_method = self._opener
        try:
            parameters = inspect.signature(open_method).parameters
        except (TypeError, ValueError):  # pragma: no cover - C callables/mocks
            return open_method(request, timeout=self._timeout)
        accepts = "timeout" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts:
            return open_method(request, timeout=self._timeout)
        return open_method(request)

    def _notify_at(
        self,
        event_key: str,
        job_id: str | None,
        details: Mapping | None,
        *,
        now: float,
    ) -> bool:
        event = _slug(event_key)
        if not event:
            raise ValueError("event_key must be a non-empty slug")
        clean_job = _clean(job_id) if job_id else None

        if not self._url:
            log.debug("notification skipped event=%s job=%s reason=no-url", event, clean_job)
            return False

        if self._is_cooling(event, clean_job, now):
            log.debug("notification suppressed event=%s job=%s reason=cooldown", event, clean_job)
            return False

        payload = _build_payload(event, clean_job, _detail_fields(details), now)
        try:
            request = Request(
                self._url,
                data=payload,
                headers={
                    "Content-Type": "text/plain",
                    "Title": _title(event),
                    "User-Agent": "mediaflow/1.0",
                },
                method="POST",
            )
        except Exception as exc:  # noqa: BLE001 - never echo the URL
            log.warning(
                "notification request invalid event=%s job=%s error=%s",
                event,
                clean_job,
                type(exc).__name__,
            )
            return False
        try:
            response = self._open(request)
            try:
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if callable(getcode) else None
                try:
                    status_code = int(status) if status is not None else None
                except (TypeError, ValueError):
                    status_code = None
                if status_code is not None and not 200 <= status_code < 300:
                    log.warning(
                        "notification failed event=%s job=%s error=HTTPStatus", event, clean_job
                    )
                    return False
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except Exception as exc:  # noqa: BLE001 - never expose exception text
            log.warning(
                "notification failed event=%s job=%s error=%s",
                event,
                clean_job,
                type(exc).__name__,
            )
            return False

        self._last_sent[(event, clean_job)] = now
        self._persist_sent(event, clean_job, now)
        self._record_sent(event, clean_job)
        log.info("notification sent event=%s job=%s", event, clean_job)
        return True

    def _setting_key(self, event: str, job_id: str | None) -> str:
        suffix = _clean(job_id) if job_id else "-"
        # Settings keys are displayed by status tooling; keep them on one line.
        suffix = re.sub(r"\s+", "-", suffix)
        return f"{SETTING_PREFIX}{event}:{suffix}"

    def _is_cooling(self, event: str, job_id: str | None, now: float) -> bool:
        if self._cooldown_hours <= 0:
            return False
        key = (event, job_id)
        last = self._last_sent.get(key)
        if last is None:
            last = self._load_last_sent(event, job_id)
        if last is not None:
            return now - last < self._cooldown_hours * 3600.0
        # Legacy/permanent fallback: only when the configured cooldown is
        # effectively forever does the boolean notifications table matter.
        if self._cooldown_hours >= _PERMANENT_COOLDOWN_HOURS:
            return self._was_sent(event, job_id)
        return False

    def _load_last_sent(self, event: str, job_id: str | None) -> float | None:
        if self._store is None:
            return None
        key = (event, job_id)
        try:
            raw = self._store.get_setting(self._setting_key(event, job_id))
        except Exception as exc:  # noqa: BLE001 - state must not break notifications
            log.warning("notification cooldown read failed error=%s", type(exc).__name__)
            return None
        parsed = _parse_timestamp(raw)
        if parsed is not None:
            self._last_sent[key] = parsed
        return parsed

    def _persist_sent(self, event: str, job_id: str | None, now: float) -> None:
        if self._store is None:
            return
        try:
            self._store.set_setting(self._setting_key(event, job_id), _timestamp(now))
        except Exception as exc:  # noqa: BLE001
            log.warning("notification cooldown write failed error=%s", type(exc).__name__)

    def _record_sent(self, event: str, job_id: str | None) -> None:
        if self._store is None:
            return
        try:
            self._store.notification_mark(event, job_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("notification record failed error=%s", type(exc).__name__)

    def _was_sent(self, event: str, job_id: str | None) -> bool:
        if self._store is None:
            return False
        try:
            return bool(self._store.notification_was_sent(event, job_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("notification history read failed error=%s", type(exc).__name__)
            return False


def _review_timestamp(job: Mapping) -> float | None:
    for key in ("updated_at", "created_at", "first_seen"):
        value = job.get(key)
        number = _number(value)
        if number is not None:
            return number
    return None


def _normalise_reasons(reasons: Any) -> tuple[list[str], int]:
    """Return deduplicated reason slugs and the number of affected jobs."""
    if reasons is None:
        return [], 0
    if isinstance(reasons, str):
        slug = _slug(reasons)
        return ([slug], 1) if slug else ([], 0)
    if isinstance(reasons, Mapping):
        slugs: list[str] = []
        total = 0
        for reason, count in reasons.items():
            slug = _slug(reason)
            if not slug:
                continue
            slugs.append(slug)
            total += max(1, _count(count) or 1)
        return _dedupe(slugs), total
    slugs = []
    total = 0
    try:
        iterator = iter(reasons)
    except TypeError:
        return [], 0
    for item in iterator:
        if isinstance(item, Mapping):
            reason = item.get("reason") or item.get("kind") or item.get("name")
            amount = item.get("count", 1)
        else:
            reason = item
            amount = 1
        slug = _slug(reason)
        if not slug:
            continue
        slugs.append(slug)
        total += max(1, _count(amount) or 1)
    return _dedupe(slugs), total


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_TIMEOUT_SECONDS",
    "Notifier",
    "NotificationError",
    "PAUSED_MIN_SECONDS",
    "SETTING_PREFIX",
]
