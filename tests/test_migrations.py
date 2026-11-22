"""[COMPAT-1] Schema migrations and persisted-data compatibility."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mediaflow import db as db_module
from mediaflow.db import SCHEMA_VERSION, DatabaseError, Store
from mediaflow.doctor import read_capabilities

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _legacy_store(tmp_path: Path) -> Store:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_v1.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return Store(path)


def test_v1_database_migrates_with_backup_and_preserves_history(tmp_path):
    store = _legacy_store(tmp_path)
    try:
        store.initialize()
        with store._lock:
            version = store._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(jobs)")}
            speed_columns = {
                row[1] for row in store._conn.execute("PRAGMA table_info(speed_table)")
            }
        assert int(version) == SCHEMA_VERSION == 2
        assert {"plan_schema", "config_generation", "preset_fingerprint"} <= columns
        assert "key_version" in speed_columns

        job = store.get_job("LEGACYJOB000000000000000001")
        assert job is not None and job["state"] == "ENCODED"
        assert job["plan"]["video"]["color"]["range"] == "tv"
        assert len(store.pending_intents("replace")) == 1
        assert len(list((tmp_path / "backup").glob("state-*.db"))) == 1

        # Legacy speed rows are retained but never used as current estimates.
        assert store.speed_get("legacy", "qsv", 1080) is None
        assert store.speed_has_any("legacy") is False
        store.speed_seed("legacy", "qsv", 1080, 3.0)
        assert store.speed_get("legacy", "qsv", 1080) == 3.0
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path):
    store = _legacy_store(tmp_path)
    try:
        store.initialize()
        store.initialize()
        assert len(list((tmp_path / "backup").glob("state-*.db"))) == 1
        assert store.get_job("LEGACYJOB000000000000000001") is not None
    finally:
        store.close()


def test_newer_database_is_refused_without_being_restamped(tmp_path):
    store = _legacy_store(tmp_path)
    try:
        with store._tx() as c:
            c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        with pytest.raises(DatabaseError, match="newer than supported"):
            store.initialize()
        with store._lock:
            version = store._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        assert version == "99"
    finally:
        store.close()


def test_migration_failure_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    store = _legacy_store(tmp_path)
    try:
        monkeypatch.setattr(db_module, "MIGRATION_FAIL_AT", 2)
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            store.initialize()
        with store._lock:
            version = store._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        assert version == "1"
        assert store.get_job("LEGACYJOB000000000000000001") is not None

        monkeypatch.setattr(db_module, "MIGRATION_FAIL_AT", None)
        store.initialize()
        assert store.get_job("LEGACYJOB000000000000000001") is not None
    finally:
        store.close()


def test_backup_restore_preserves_the_old_database(tmp_path):
    store = _legacy_store(tmp_path)
    store.initialize()
    backup = sorted((tmp_path / "backup").glob("state-*.db"))[-1]
    store.close()

    restored = Store(tmp_path / "state.db")
    preserved = restored.restore_from_backup(backup)
    try:
        assert preserved.exists()
        assert restored.get_job("LEGACYJOB000000000000000001") is not None
        with restored._lock:
            version = restored._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        assert int(version) == 2  # restore re-runs the ordered upgrade
    finally:
        restored.close()


@pytest.mark.parametrize(
    "name", ["plan_pre_pass_a.json", "plan_pass_a.json"]
)
def test_historical_plan_fixtures_load_and_are_recognised_as_legacy(name, tmp_path):
    from mediaflow.verify import _plan_view

    plan = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    # Both eras remain readable for diagnosis; the engine re-plans them before
    # execution via the missing plan_schema marker.
    assert _plan_view(plan) is not None
    assert plan.get("plan_schema") is None


def test_unversioned_capability_file_is_unknown(tmp_path):
    caps = tmp_path / "capabilities.json"
    caps.write_text((FIXTURES / "capabilities_v1.json").read_text(encoding="utf-8"))
    assert read_capabilities(caps) == {}
