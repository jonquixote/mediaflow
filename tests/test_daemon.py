"""Daemon lock, reload and ffmpeg-change safety."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mediaflow.config import Config, load_config
from mediaflow.daemon import AlreadyRunning, Daemon, daemon_lock, daemon_running
from mediaflow.db import Store
from mediaflow.presets import load_presets


def _config(tmp_path: Path):
    cfgdir = tmp_path / "etc"
    (cfgdir / "presets.d").mkdir(parents=True)
    for p in (Path(__file__).resolve().parents[1] / "presets").glob("*.toml"):
        shutil.copy(p, cfgdir / "presets.d" / p.name)
    path = cfgdir / "config.toml"
    path.write_text(f'''root = "{tmp_path / "media"}"
state_dir = "{tmp_path / "state"}"
runtime_dir = "{tmp_path / "run"}"
dry_run = true
stable_seconds = 0
''')
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    return path, cfg


def test_daemon_lock_is_exclusive(tmp_path):
    """[SAF-7] Only one daemon lock holder exists."""
    path, cfg = _config(tmp_path)
    with daemon_lock(cfg):
        assert daemon_running(cfg) is True
        with pytest.raises(AlreadyRunning):
            with daemon_lock(cfg):
                pass
    assert daemon_running(cfg) is False


def test_invalid_reload_keeps_previous_config(tmp_path):
    path, cfg = _config(tmp_path)
    presets = load_presets(path.parent / "presets.d")
    store = Store(cfg.db_path)
    store.initialize()
    try:
        daemon = Daemon(cfg, store, presets, config_path=path, presets_dir=path.parent / "presets.d")
        old_dry = daemon.engine.dry_run
        old_stable = daemon.engine.cfg.stable_seconds
        daemon._config_mtime = (-1.0,)
        path.write_text("this is not toml [[[")
        daemon._reload_if_changed()
        assert daemon.engine.cfg.stable_seconds == old_stable
        assert daemon.engine.dry_run == old_dry
        assert any("config-invalid" in r for r in daemon.engine._pause_reasons)
        # Valid file with changed setting applies.
        daemon._config_mtime = (-1.0,)
        path.write_text(f'''root = "{tmp_path / "media"}"
state_dir = "{tmp_path / "state"}"
runtime_dir = "{tmp_path / "run"}"
dry_run = true
stable_seconds = 3
''')
        daemon._reload_if_changed()
        assert daemon.engine.cfg.stable_seconds == 3
    finally:
        store.close()


def test_ffmpeg_change_retest_is_cancellable_and_keeps_old_capabilities(tmp_path, monkeypatch):
    """SUP-2/3: a stopped daemon cannot overwrite capability evidence."""

    path, cfg = _config(tmp_path)
    caps_path = Path(cfg.capabilities_path)
    caps_path.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "capabilities_schema": 2,
        "ffmpeg_version": "old",
        "backends": {"qsv": {"hevc8": True}},
    }
    caps_path.write_text(json.dumps(original), encoding="utf-8")
    store = Store(cfg.db_path)
    store.initialize()
    calls = []

    class Completed:
        stdout = "ffmpeg version new\n"

    monkeypatch.setattr("mediaflow.daemon.subprocess.run", lambda *a, **k: Completed())

    def fake_tests(config, **kwargs):
        calls.append(kwargs)
        return {
            "status": "cancelled",
            "state": "cancelled",
            "completed": False,
            "ok": False,
            "reasons": ["user-cancelled"],
        }

    monkeypatch.setattr("mediaflow.doctor.run_capability_tests", fake_tests)
    try:
        daemon = Daemon(cfg, store, {}, config_path=path)
        daemon._stop.set()
        daemon._check_ffmpeg()

        assert calls and calls[0]["cancel_event"] is daemon._stop
        assert json.loads(caps_path.read_text()) == original
        assert any(
            "capability tests cancelled" in reason
            for reason in daemon.engine.pause_reasons
        )
    finally:
        store.close()
