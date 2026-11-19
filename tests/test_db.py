"""State database tests: durability, transitions, backups, caches."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mediaflow.db import DatabaseError, Store, new_job_id


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    s.initialize()
    yield s
    s.close()


def test_job_lifecycle_and_transitions(store):
    job = store.create_job(preset="1080p-balanced-mp4", source_rel="Film", source_path="/x/Film")
    assert len(job["id"]) == 26
    assert job["state"] == "DETECTED"
    assert store.transition(job["id"], "STABLE", expect_from="DETECTED")
    assert store.transition(job["id"], "STABLE", expect_from="DETECTED") is False
    assert store.get_job(job["id"])["state"] == "STABLE"
    assert len(store.events(job["id"])) == 2


def test_plan_and_review_json_roundtrip(store):
    job = store.create_job(preset="p", source_rel="f", source_path="/x/f")
    store.update_job(job["id"], plan_json={"action": "encode", "n": 1})
    assert store.get_job(job["id"])["plan"]["action"] == "encode"
    store.set_review(job["id"], "hdr", "PLANNED", {"why": "pq"})
    row = store.get_job(job["id"])
    assert row["state"] == "REVIEW"
    assert row["review_context"]["why"] == "pq"
    store.clear_review(job["id"])
    assert store.get_job(job["id"])["review_reason"] is None


def test_active_job_lookup_and_terminal(store):
    a = store.create_job(preset="p", source_rel="rel", source_path="/x/rel")
    assert store.active_job_for("p", "rel")["id"] == a["id"]
    store.transition(a["id"], "DONE", expect_from="DETECTED")
    assert store.active_job_for("p", "rel") is None
    b = store.create_job(preset="p", source_rel="rel", source_path="/x/rel")
    assert b["id"] != a["id"]


def test_command_queue(store):
    cid = store.enqueue_command("pause", {"now": True})
    pending = store.pending_commands()
    assert pending[0]["id"] == cid and pending[0]["payload"]["now"] is True
    store.command_start(cid)
    store.command_done(cid)
    assert store.pending_commands() == []


def test_intents(store):
    job = store.create_job(preset="p", source_rel="f", source_path="/x/f")
    i = store.create_intent("replace", job["id"], {"destination": "/m/Film"})
    assert len(store.pending_intents("replace")) == 1
    store.complete_intent(i)
    assert store.pending_intents() == []


def test_tmdb_cache(store):
    assert store.get_tmdb("movie:heat:1995") is None
    store.put_tmdb("movie:heat:1995", {"results": [{"id": 1}]})
    assert store.get_tmdb("movie:heat:1995")["results"][0]["id"] == 1


def test_speed_table_ema(store):
    assert store.speed_get("p", "qsv", 1080) is None
    store.speed_seed("p", "qsv", 1080, 2.0)
    assert store.speed_get("p", "qsv", 1000) == 2.0
    assert store.speed_get("p", "qsv", 1081) is None
    value = store.speed_update("p", "qsv", 1080, 4.0, alpha=0.5)
    assert value == 3.0
    assert store.speed_has_any("p")


def test_backup_and_retention(store, tmp_path):
    job = store.create_job(preset="p", source_rel="f", source_path="/x/f")
    backups = tmp_path / "backup"
    for i in range(4):
        target = store.backup(backups, keep=3)
        time.sleep(0.01)
    files = sorted(backups.glob("state-*.db"))
    assert len(files) == 3
    check = sqlite3.connect(files[-1])
    try:
        row = check.execute("SELECT id FROM jobs").fetchone()
    finally:
        check.close()
    assert row[0] == job["id"]


def test_corrupt_db_refuses(store, tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"this is not sqlite")
    with pytest.raises(DatabaseError):
        bad = Store(path)
        bad.initialize()
        bad.close()


def test_job_ids_are_sortable_and_unique():
    ids = [new_job_id() for _ in range(10)]
    assert len(set(ids)) == 10
    assert all(len(i) == 26 for i in ids)
