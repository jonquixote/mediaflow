"""Configuration generations, reload classification and component coherence.

[CONFIG-1] Every configuration key has one authoritative reload classification.
[CONFIG-1] A reload applies a complete generation atomically and reports keys
that require a daemon restart without applying them partially.
"""

from __future__ import annotations

import json
import os
import threading
import tomllib
from dataclasses import fields
from pathlib import Path

import pytest

from mediaflow.config import (
    Config,
    KEY_CLASS,
    ReloadClass,
    classify_config_change,
    classify_secret_change,
    config_mtime_fingerprint,
    load_config,
)
from mediaflow.daemon import Daemon
from mediaflow.db import Store
from mediaflow.guards import GuardConfig
from mediaflow.presets import load_presets

REPO = Path(__file__).resolve().parents[1]


def _config_text(tmp_path: Path, body: str = "") -> str:
    return (
        'root = "' + str(tmp_path / "media") + '"\n'
        'state_dir = "' + str(tmp_path / "state") + '"\n'
        'runtime_dir = "' + str(tmp_path / "run") + '"\n' + body
    )


def _config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_config_text(tmp_path, body), encoding="utf-8")
    return path


def _daemon(tmp_path: Path, body: str = ""):
    from mediaflow.config import Secrets

    path = _config(tmp_path, body)
    presets_dir = REPO / "presets"
    cfg = load_config(path, environ={})
    (path.parent / "tmdb.env").write_text("TMDB_READ_TOKEN=start-token\n", encoding="utf-8")
    (path.parent / "notify.env").write_text("NOTIFY_URL=http://127.0.0.1/start\n", encoding="utf-8")
    presets = load_presets(presets_dir)
    store = Store(cfg.db_path)
    store.initialize()
    daemon = Daemon(
        cfg, store, presets, config_path=path, presets_dir=presets_dir,
        secrets=Secrets(token="start-token", url="http://127.0.0.1/start"),
    )
    return daemon, path, store


def test_every_config_key_has_exactly_one_reload_classification():
    user_keys = {field.name for field in fields(Config) if field.name != "generation"}
    assert set(KEY_CLASS) == user_keys
    example = tomllib.loads((REPO / "config" / "config.toml.example").read_text())
    assert set(example) == set(KEY_CLASS)
    for key in user_keys:
        assert KEY_CLASS[key] in set(ReloadClass)


def test_restart_required_changes_are_reported_and_not_applied():
    old = Config(dry_run=True, ffmpeg="/old/ffmpeg", generation=4)
    new = Config(dry_run=False, ffmpeg="/new/ffmpeg", generation=5)
    plan = classify_config_change(old, new)

    assert "ffmpeg" in plan.restart_required
    assert "dry_run" in plan.live_changed
    assert plan.effective_config.ffmpeg == "/old/ffmpeg"
    assert plan.effective_config.generation == 6
    assert "restart-required:ffmpeg" in plan.notes


def test_live_reload_updates_the_component_that_consumes_it(tmp_path):
    daemon, path, store = _daemon(tmp_path, "min_free_gb = 40\n")
    try:
        daemon._reload_if_changed()
        assert GuardConfig.from_config(daemon.engine.cfg).min_free_gb == 40

        path.write_text(_config_text(tmp_path, "min_free_gb = 55\n"), encoding="utf-8")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000))
        daemon._reload_if_changed()

        assert daemon.engine.cfg.min_free_gb == 55
        # GuardConfig is derived from the active generation on every check.
        assert GuardConfig.from_config(daemon.engine.cfg).min_free_gb == 55
    finally:
        store.close()


def test_valid_reload_clears_only_the_config_invalid_hold(tmp_path):
    daemon, path, store = _daemon(tmp_path)
    try:
        daemon.engine.add_global_pause_reasons(["ffmpeg-changed"])
        daemon._config_mtime = (-1.0,)
        path.write_text("this is not toml [[[\n", encoding="utf-8")
        daemon._reload_if_changed()
        assert any(r.startswith("config-invalid:") for r in daemon.engine.pause_reasons)
        assert "ffmpeg-changed" in daemon.engine.pause_reasons

        daemon._config_mtime = (-1.0,)
        path.write_text(_config_text(tmp_path), encoding="utf-8")
        daemon._reload_if_changed()
        assert not any(r.startswith("config-invalid:") for r in daemon.engine.pause_reasons)
        assert "ffmpeg-changed" in daemon.engine.pause_reasons
    finally:
        store.close()


def test_reload_during_an_active_operation_is_deferred(tmp_path):
    daemon, path, store = _daemon(tmp_path)
    try:
        daemon.engine._active_cancel_event = threading.Event()
        daemon.engine._active_cancel_job_id = "job"
        path.write_text(_config_text(tmp_path, "stable_seconds = 7\n"), encoding="utf-8")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000))
        daemon._reload_if_changed()

        assert daemon.engine.cfg.stable_seconds == 120
        assert daemon.engine.pending_config is not None
        assert daemon.engine.pending_config.stable_seconds == 7

        daemon.engine._active_cancel_event = None
        daemon.engine._active_cancel_job_id = None
        daemon.engine.apply_pending_config()
        assert daemon.engine.cfg.stable_seconds == 7
        assert daemon.engine.pending_config is None
    finally:
        store.close()


def test_fingerprint_detects_a_same_second_mtime_change(tmp_path):
    path = _config(tmp_path)
    presets_dir = REPO / "presets"
    before = config_mtime_fingerprint(path, presets_dir)
    stat = path.stat()
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert config_mtime_fingerprint(path, presets_dir) != before


def test_temp_sensor_change_is_restart_required(tmp_path):
    """Thermal input must not change underneath a running operation."""

    daemon, path, store = _daemon(tmp_path)
    try:
        path.write_text(_config_text(tmp_path, 'temp_sensor = "none"\n'), encoding="utf-8")
        daemon._reload_if_changed()

        assert daemon.engine.cfg.temp_sensor == "auto"
        assert "restart-required:temp_sensor" in daemon.engine.pause_reasons
        assert daemon.engine.cfg.generation > 1
    finally:
        store.close()


def test_poll_seconds_and_notifier_keys_are_live(tmp_path, monkeypatch):
    """Consumers that can rebind at a boundary are not restart-required."""

    from mediaflow.notify import Notifier

    monkeypatch.delenv("MEDIAFLOW_ROOT", raising=False)
    daemon, path, store = _daemon(tmp_path)
    notifier = Notifier(daemon.config, daemon.secrets, store)
    daemon.engine.notifier = notifier
    try:
        path.write_text(
            _config_text(tmp_path, "poll_seconds = 5\nnotify_cooldown_hours = 2\n"
                                   "review_reminder_days = 1\n"),
            encoding="utf-8",
        )
        daemon._reload_if_changed()

        assert daemon.tick_seconds == 5
        assert notifier._cooldown_hours == 2
        assert notifier._config.review_reminder_days == 1
        assert not any(r.startswith("restart-required:") for r in daemon.engine.pause_reasons)
    finally:
        store.close()


def test_restart_hold_clears_only_at_startup(tmp_path, monkeypatch):
    """A pending restart is resolved by an actual restart, not by another reload."""

    monkeypatch.delenv("MEDIAFLOW_ROOT", raising=False)
    daemon, path, store = _daemon(tmp_path)
    try:
        path.write_text(
            _config_text(tmp_path, 'ffmpeg = "/new/ffmpeg"\n'), encoding="utf-8"
        )
        daemon._reload_if_changed()
        assert "restart-required:ffmpeg" in daemon.engine.pause_reasons
        assert store.get_setting("restart_required", "") == "ffmpeg"

        daemon._reload_if_changed()  # no file change: hold stays
        assert "restart-required:ffmpeg" in daemon.engine.pause_reasons

        daemon.clear_startup_holds()
        assert not any(r.startswith("restart-required:") for r in daemon.engine.pause_reasons)
        assert store.get_setting("restart_required", "") == ""
    finally:
        store.close()


def test_secret_changes_require_restart_and_are_never_serialised(tmp_path):
    from mediaflow.config import Secrets

    old = Secrets(token="token-one", url="http://secret-one")
    new = Secrets(token="token-two", url="http://secret-two")
    plan = classify_secret_change(old, new)

    assert plan.restart_required
    rendered = json.dumps(plan.as_dict())
    assert "token-one" not in rendered
    assert "token-two" not in rendered
    assert "secret-one" not in rendered
    assert "secret-two" not in rendered
