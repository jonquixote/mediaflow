"""SQLite state store: the single source of truth for mediaflow jobs."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping

from .safety import fsync_dir
from .state import (
    INTENT_KINDS,
    JobState,
    REVIEW_REASONS,
    command_status,
    INTENT_KINDS,
    TERMINAL_STATES as _TERMINAL_STATES,
    in_clause,
    is_valid_transition,
    values,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

JOB_COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "preset": "TEXT NOT NULL",
    "source_rel": "TEXT NOT NULL",
    "source_path": "TEXT NOT NULL",
    "original_path": "TEXT",
    "parked_path": "TEXT",
    "trash_path": "TEXT",
    "manifest_path": "TEXT",
    "entry_kind": "TEXT",
    "state": "TEXT NOT NULL",
    "created_at": "REAL NOT NULL",
    "updated_at": "REAL NOT NULL",
    "first_seen": "REAL NOT NULL",
    "plan_version": "INTEGER NOT NULL DEFAULT 0",
    "plan_json": "TEXT",
    "dry_run": "INTEGER NOT NULL DEFAULT 0",
    "restarts": "INTEGER NOT NULL DEFAULT 0",
    "review_reason": "TEXT",
    "review_from_state": "TEXT",
    "review_context": "TEXT",
    "error": "TEXT",
    "source_size": "INTEGER",
    "source_mtime": "REAL",
    "source_video_bytes": "INTEGER",
    "source_duration": "REAL",
    "output_path": "TEXT",
    "output_sha256": "TEXT",
    "output_size": "INTEGER",
    "output_video_bytes": "INTEGER",
    "final_name": "TEXT",
    "phase": "TEXT",
    "progress": "REAL",
    "encode_seconds": "REAL",
    "backend": "TEXT",
    "started_at": "REAL",
    "finished_at": "REAL",
    "fail_count": "INTEGER NOT NULL DEFAULT 0",
    "last_error_at": "REAL",
    "adopted": "INTEGER NOT NULL DEFAULT 0",
}

# Compatibility alias: still a tuple of strings, but derived from the one
# shared state model rather than hand-maintained here.
TERMINAL_STATES: tuple[str, ...] = values(_TERMINAL_STATES)


class DatabaseError(RuntimeError):
    pass


class Store:
    """Thread-safe-enough wrapper around one SQLite connection.

    The daemon is the only writer in production; a lock serialises access in
    process.  WAL plus synchronous=FULL gives crash durability and the CLI
    can read while the daemon writes.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError as exc:
            self._conn.close()
            raise DatabaseError(f"cannot open state.db: {exc}") from exc
        self._lock = __import__("threading").RLock()
        self._closed = False

    # -- lifecycle ------------------------------------------------------
    def initialize(self) -> None:
        with self._tx() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            cols = ", ".join(f"{k} {v}" for k, v in JOB_COLUMNS.items())
            c.execute(f"CREATE TABLE IF NOT EXISTS jobs ({cols})")
            c.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)")
            c.execute("CREATE INDEX IF NOT EXISTS jobs_preset_source ON jobs(preset, source_rel)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                " seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, ts REAL NOT NULL,"
                " state TEXT, kind TEXT NOT NULL, message TEXT)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS stats ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, preset TEXT,"
                " backend TEXT, finished_at REAL NOT NULL, bytes_before INTEGER,"
                " bytes_after INTEGER, video_bytes_before INTEGER, video_bytes_after INTEGER,"
                " encode_seconds REAL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS speed_table ("
                " preset TEXT NOT NULL, backend TEXT NOT NULL, bucket TEXT NOT NULL,"
                " factor REAL NOT NULL, samples INTEGER NOT NULL DEFAULT 1,"
                " updated_at REAL NOT NULL, PRIMARY KEY(preset, backend, bucket))"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS tmdb_cache ("
                " cache_key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS library_cache ("
                " path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL,"
                " probe TEXT, updated_at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS commands ("
                " id TEXT PRIMARY KEY, command TEXT NOT NULL, payload TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,"
                " started_at REAL, finished_at REAL, error TEXT)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS commands_status ON commands(status, created_at)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS intents ("
                " id TEXT PRIMARY KEY, kind TEXT NOT NULL, job_id TEXT NOT NULL,"
                " payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
                " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS intents_status ON intents(status, kind)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS notifications ("
                " event_key TEXT NOT NULL, job_id TEXT, sent_at REAL NOT NULL,"
                " PRIMARY KEY(event_key, job_id))"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.integrity_check()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # -- health ---------------------------------------------------------
    def integrity_check(self) -> None:
        rows = [r[0] for r in self._conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise DatabaseError("state.db integrity_check failed: " + "; ".join(rows))

    def backup(self, destination: Path, keep: int = 3) -> Path:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
        target = destination / f"state-{stamp}.db"
        out = sqlite3.connect(str(target))
        try:
            with self._lock:
                self._conn.backup(out)
            out.execute("PRAGMA synchronous=FULL")
            out.commit()
            with open(target, "rb") as fh:
                os.fsync(fh.fileno())
        finally:
            out.close()
        fsync_dir(destination)
        backups = sorted(destination.glob("state-*.db"))
        keep = max(1, int(keep))
        parent = Path(destination).resolve()
        for old in backups[:-keep]:
            # State backups are not media and live outside the pipeline trees
            # guarded by FileSafety.  Keep strict containment and log every
            # removal so maintenance is never silent.
            try:
                resolved = old.resolve()
            except OSError:
                continue
            if resolved.parent != parent or not resolved.name.startswith("state-"):
                log.error("refusing to prune unexpected backup path=%s", old)
                continue
            old.unlink()
            log.warning("removed state backup path=%s reason=retention", old)
        return target

    def newest_backup(self, destination: Path) -> Path | None:
        backups = sorted(Path(destination).glob("state-*.db"))
        return backups[-1] if backups else None

    # -- jobs -----------------------------------------------------------
    def create_job(self, *, preset: str, source_rel: str, source_path: str, state: str = "DETECTED",
                   now: float | None = None, **fields: Any) -> dict:
        now = now or time.time()
        job_id = fields.pop("id", None) or new_job_id()
        row = {
            "id": job_id, "preset": preset, "source_rel": source_rel, "source_path": source_path,
            "state": state, "created_at": now, "updated_at": now, "first_seen": now,
        }
        row.update({k: self._encode_field(k, v) for k, v in fields.items() if k in JOB_COLUMNS})
        cols = ", ".join(row)
        ph = ", ".join("?" for _ in row)
        with self._tx() as c:
            c.execute(f"INSERT INTO jobs({cols}) VALUES({ph})", tuple(row.values()))
            c.execute("INSERT INTO events(job_id,ts,state,kind,message) VALUES(?,?,?,?,?)",
                      (job_id, now, state, "created", f"preset={preset}"))
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _decode_job(dict(row)) if row else None

    def list_jobs(self, states: Iterable[str] | None = None, *, include_done: bool = True) -> list[dict]:
        sql = "SELECT * FROM jobs"
        args: list[Any] = []
        if states:
            states = list(states)
            sql += " WHERE state IN (" + ",".join("?" for _ in states) + ")"
            args.extend(states)
        elif not include_done:
            clause, excluded = in_clause(_TERMINAL_STATES)
            sql += f" WHERE state NOT IN ({clause})"
            args.extend(excluded)
        sql += " ORDER BY created_at, id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_decode_job(dict(r)) for r in rows]

    def active_job_for(self, preset: str, source_rel: str) -> dict | None:
        clause, excluded = in_clause(_TERMINAL_STATES)
        sql = (
            "SELECT * FROM jobs WHERE preset=? AND source_rel=? "
            f"AND state NOT IN ({clause}) ORDER BY created_at DESC LIMIT 1"
        )
        with self._lock:
            row = self._conn.execute(sql, (preset, source_rel, *excluded)).fetchone()
        return _decode_job(dict(row)) if row else None

    def latest_job_for(self, preset: str, source_rel: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE preset=? AND source_rel=? ORDER BY created_at DESC LIMIT 1",
                (preset, source_rel)).fetchone()
        return _decode_job(dict(row)) if row else None

    def get_review_by_source(self, preset: str, source_rel: str) -> dict | None:
        review = values({JobState.REVIEW})[0]  # shared vocabulary
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE preset=? AND source_rel=? AND state=? "
                "ORDER BY created_at DESC LIMIT 1", (preset, source_rel, review)).fetchone()
        return _decode_job(dict(row)) if row else None

    def update_job(self, job_id: str, **fields: Any) -> dict:
        allowed = {k: self._encode_field(k, v) for k, v in fields.items()
                   if k in JOB_COLUMNS and k not in {"id", "created_at"}}
        if not allowed:
            return self.get_job(job_id)  # type: ignore[return-value]
        allowed["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self._tx() as c:
            cur = c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*allowed.values(), job_id))
            if cur.rowcount != 1:
                raise DatabaseError(f"job not found: {job_id}")
        return self.get_job(job_id)  # type: ignore[return-value]

    def transition(self, job_id: str, to_state: str, *, expect_from: str | Iterable[str] | None = None,
                   message: str | None = None, **fields: Any) -> bool:
        """Compare-and-set state transition using the shared state model.

        Returns ``False`` when ``expect_from`` does not match.  Structurally
        impossible transitions raise :class:`DatabaseError`; recovery paths use
        only legal transitions, so this cannot weaken crash handling.
        """
        allowed = {k: self._encode_field(k, v) for k, v in fields.items()
                   if k in JOB_COLUMNS and k not in {"id", "created_at"}}
        allowed["state"] = to_state
        allowed["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in allowed)
        args = list(allowed.values())
        sql = f"UPDATE jobs SET {sets} WHERE id=?"
        args.append(job_id)
        expected: tuple[str, ...] | None = None
        if expect_from:
            expected = values(
                [expect_from] if isinstance(expect_from, str) else list(expect_from)
            )
            clause, clause_values = in_clause(expected)
            sql += f" AND state IN ({clause})"
            args.extend(clause_values)
        with self._tx() as c:
            row = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            source = str(row[0])
            if expected is not None and source not in expected:
                return False
            if not is_valid_transition(source, to_state):
                raise DatabaseError(
                    f"invalid job transition {source} -> {to_state} for {job_id}"
                )
            cur = c.execute(sql, args)
            if cur.rowcount != 1:
                return False
            c.execute("INSERT INTO events(job_id,ts,state,kind,message) VALUES(?,?,?,?,?)",
                      (job_id, time.time(), to_state, "transition", message))
        return True

    def event(self, job_id: str | None, kind: str, message: str, *, state: str | None = None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO events(job_id,ts,state,kind,message) VALUES(?,?,?,?,?)",
                      (job_id, time.time(), state, kind, message))

    def events(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_review(self, job_id: str, reason: str, from_state: str, context: Mapping | None = None) -> None:
        if reason not in REVIEW_REASONS:
            raise ValueError(f"unknown review reason: {reason!r}")
        if not is_valid_transition(from_state, JobState.REVIEW):
            raise DatabaseError(f"invalid review transition from {from_state}")
        review = values({JobState.REVIEW})[0]
        with self._tx() as c:
            c.execute(
                "UPDATE jobs SET state=?, review_reason=?, review_from_state=?, review_context=?,"
                " updated_at=? WHERE id=?",
                (review, reason, from_state,
                 json.dumps(context or {}, ensure_ascii=False, sort_keys=True), time.time(), job_id))
            c.execute("INSERT INTO events(job_id,ts,state,kind,message) VALUES(?,?,?,?,?)",
                      (job_id, time.time(), review, "review", reason))

    def clear_review(self, job_id: str) -> None:
        self.update_job(job_id, review_reason=None, review_from_state=None, review_context=None)

    def add_stat(self, job_id: str, preset: str, backend: str | None, bytes_before: int | None,
                 bytes_after: int | None, video_bytes_before: int | None, video_bytes_after: int | None,
                 encode_seconds: float | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO stats(job_id,preset,backend,finished_at,bytes_before,bytes_after,"
                "video_bytes_before,video_bytes_after,encode_seconds) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, preset, backend, time.time(), bytes_before, bytes_after,
                 video_bytes_before, video_bytes_after, encode_seconds))

    def stats_exists(self, job_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM stats WHERE job_id=? LIMIT 1", (job_id,)).fetchone() is not None

    # -- speed table ----------------------------------------------------
    BUCKETS = ("<=720", "<=1080", ">1080")

    @staticmethod
    def bucket(height: int) -> str:
        if height <= 720:
            return "<=720"
        if height <= 1080:
            return "<=1080"
        return ">1080"

    def speed_get(self, preset: str, backend: str, height: int) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT factor FROM speed_table WHERE preset=? AND backend=? AND bucket=?",
                (preset, backend, self.bucket(height))).fetchone()
        return float(row[0]) if row else None

    def speed_has_any(self, preset: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM speed_table WHERE preset=? LIMIT 1", (preset,)).fetchone()
        return row is not None

    def speed_seed(self, preset: str, backend: str, height: int, factor: float) -> None:
        if not (factor > 0):
            raise DatabaseError("speed factor must be positive")
        with self._tx() as c:
            c.execute(
                "INSERT INTO speed_table(preset,backend,bucket,factor,samples,updated_at) VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(preset,backend,bucket) DO UPDATE SET factor=excluded.factor,samples=1,updated_at=excluded.updated_at",
                (preset, backend, self.bucket(height), float(factor), time.time()))

    def speed_update(self, preset: str, backend: str, height: int, real_factor: float, alpha: float = 0.3) -> float:
        if not (real_factor > 0):
            raise DatabaseError("speed factor must be positive")
        with self._tx() as c:
            row = c.execute("SELECT factor,samples FROM speed_table WHERE preset=? AND backend=? AND bucket=?",
                            (preset, backend, self.bucket(height))).fetchone()
            if row:
                value = alpha * real_factor + (1 - alpha) * float(row["factor"])
                samples = int(row["samples"]) + 1
            else:
                value = real_factor
                samples = 1
            c.execute(
                "INSERT INTO speed_table(preset,backend,bucket,factor,samples,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(preset,backend,bucket) DO UPDATE SET factor=excluded.factor,"
                "samples=excluded.samples,updated_at=excluded.updated_at",
                (preset, backend, self.bucket(height), value, samples, time.time()))
        return value

    # -- TMDb cache -----------------------------------------------------
    def get_tmdb(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM tmdb_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_tmdb(self, key: str, body: Mapping) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO tmdb_cache(cache_key,body,fetched_at) VALUES(?,?,?)",
                      (key, json.dumps(body, ensure_ascii=False, sort_keys=True), time.time()))

    # -- commands -------------------------------------------------------
    def enqueue_command(self, command: str, payload: Mapping) -> str:
        cmd_id = "cmd_" + uuid.uuid4().hex
        with self._tx() as c:
            c.execute("INSERT INTO commands(id,command,payload,created_at) VALUES(?,?,?,?)",
                      (cmd_id, command, json.dumps(payload, ensure_ascii=False), time.time()))
        return cmd_id

    def pending_commands(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM commands WHERE status IN (?,?) ORDER BY created_at",
                (command_status("pending"), command_status("running"))).fetchall()
        return [_decode_command(dict(r)) for r in rows]

    def command_done(self, cmd_id: str, *, error: str | None = None) -> None:
        with self._tx() as c:
            c.execute("UPDATE commands SET status=?, finished_at=?, error=? WHERE id=?",
                      (command_status("failed" if error else "done"), time.time(), error, cmd_id))

    def command_start(self, cmd_id: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE commands SET status=?, started_at=? WHERE id=? AND status=?",
                (command_status("running"), time.time(), cmd_id, command_status("pending")),
            )

    # -- intents --------------------------------------------------------
    def create_intent(self, kind: str, job_id: str, payload: Mapping) -> str:
        if kind not in INTENT_KINDS:
            raise ValueError(f"unknown intent kind: {kind!r}")
        intent_id = "int_" + uuid.uuid4().hex
        now = time.time()
        with self._tx() as c:
            c.execute("INSERT INTO intents(id,kind,job_id,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      (intent_id, kind, job_id, json.dumps(payload, ensure_ascii=False), now, now))
        return intent_id

    def pending_intents(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM intents WHERE status=?"
        args: list[Any] = [command_status("pending")]
        if kind:
            if kind not in INTENT_KINDS:
                raise ValueError(f"unknown intent kind: {kind!r}")
            sql += " AND kind=?"
            args.append(kind)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at", args).fetchall()
        return [_decode_intent(dict(r)) for r in rows]

    def complete_intent(self, intent_id: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE intents SET status=?,updated_at=? WHERE id=?",
                (command_status("done"), time.time(), intent_id),
            )

    # -- library / settings --------------------------------------------
    def get_library_cache(self, path: str, size: int, mtime: float) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT probe FROM library_cache WHERE path=? AND size=? AND mtime=?", (path, size, mtime)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def put_library_cache(self, path: str, size: int, mtime: float, probe: Mapping) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO library_cache(path,size,mtime,probe,updated_at) VALUES(?,?,?,?,?)",
                      (path, size, mtime, json.dumps(probe, sort_keys=True), time.time()))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))

    # -- notifications --------------------------------------------------
    def notification_was_sent(self, event_key: str, job_id: str | None) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM notifications WHERE event_key=? AND job_id IS ?", (event_key, job_id)).fetchone()
        return row is not None

    def notification_mark(self, event_key: str, job_id: str | None) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO notifications(event_key,job_id,sent_at) VALUES(?,?,?)",
                      (event_key, job_id, time.time()))

    @staticmethod
    def _encode_field(key: str, value: Any) -> Any:
        if key in {"plan_json", "review_context"} and value is not None and not isinstance(value, str):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in {"dry_run", "adopted"}:
            return int(bool(value))
        return value


def new_job_id() -> str:
    """A ULID-like, lexicographically sortable, 26-character Crockford id."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = int(time.time() * 1000) << 80
    value |= int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(alphabet[value & 31])
        value >>= 5
    return "".join(reversed(out))


def _decode_job(row: dict) -> dict:
    row = dict(row)
    for key in ("plan_json", "review_context"):
        if row.get(key):
            try:
                row[key[:-5] if key.endswith("_json") else key] = json.loads(row[key])
            except (TypeError, json.JSONDecodeError):
                pass
    row["dry_run"] = bool(row.get("dry_run"))
    row["adopted"] = bool(row.get("adopted"))
    return row


def _decode_command(row: dict) -> dict:
    row = dict(row)
    try:
        row["payload"] = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        row["payload"] = {}
    return row


def _decode_intent(row: dict) -> dict:
    row = dict(row)
    try:
        row["payload"] = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        row["payload"] = {}
    return row
