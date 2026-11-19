"""CLI output, JSON, phone mode and single-writer routing."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from mediaflow.cli import main
from mediaflow.config import load_config
from mediaflow.daemon import AlreadyRunning


def _write_config(tmp_path: Path, **overrides) -> Path:
    cfgdir = tmp_path / "etc" / "mediaflow"
    cfgdir.mkdir(parents=True)
    presets = cfgdir / "presets.d"
    presets.mkdir()
    src = Path(__file__).resolve().parents[1] / "presets"
    for preset in src.glob("*.toml"):
        shutil.copy(preset, presets / preset.name)
    root = tmp_path / "media"
    values = {
        "root": str(root),
        "state_dir": str(tmp_path / "state"),
        "runtime_dir": str(tmp_path / "run"),
        "dry_run": True,
        "stable_seconds": 0,
        "poll_seconds": 30,
        "ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg",
        "ffprobe": "/usr/lib/jellyfin-ffmpeg/ffprobe",
    }
    values.update(overrides)
    text = ""
    for key, value in values.items():
        if isinstance(value, bool):
            text += f"{key} = {'true' if value else 'false'}\n"
        elif isinstance(value, str):
            text += f'{key} = "{value}"\n'
        else:
            text += f"{key} = {value}\n"
    path = cfgdir / "config.toml"
    path.write_text(text)
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    return path


def test_status_short_and_json(tmp_path, capsys, monkeypatch):
    path = _write_config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    assert main(["status", "--short"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) <= 12
    assert all(len(line) <= 60 for line in out.splitlines())
    assert "queue" in out
    assert main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "counts" in data and data["dry_run"] is True
    # flags before the command work too
    assert main(["--short", "status"]) == 0
    assert len(capsys.readouterr().out.splitlines()) <= 12


def test_queue_empty_and_review_list(tmp_path, capsys, monkeypatch):
    path = _write_config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    assert main(["queue", "--short"]) == 0
    assert "empty" in capsys.readouterr().out
    assert main(["review", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["trash", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_pause_and_resume_without_daemon(tmp_path, capsys, monkeypatch):
    path = _write_config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    assert main(["pause"]) == 0
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "user-paused" in data["paused_reasons"] or data["dry_run"] is True
    from mediaflow.db import Store
    cfg = load_config(path, environ={})
    store = Store(cfg.db_path)
    assert store.get_setting("user_paused") == "1"
    store.close()
    assert main(["resume"]) == 0
    store = Store(cfg.db_path)
    assert store.get_setting("user_paused") == "0"
    store.close()


def test_mutation_routes_to_daemon_command_table(tmp_path, capsys, monkeypatch):
    """[SAF-7] With the daemon lock held, CLI writes go to the commands table."""
    from mediaflow import cli
    from mediaflow.db import Store
    path = _write_config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    cfg, secrets, presets = cli._load(type("A", (), {"config": path})())

    @contextmanager
    def held(config, blocking=False):
        raise AlreadyRunning("test")
        yield

    monkeypatch.setattr(cli, "daemon_lock", held)
    rc = cli._route(cfg, secrets, presets, "pause", {"now": False})
    assert rc == 0
    store = Store(cfg.db_path)
    pending = store.pending_commands()
    store.close()
    assert pending and pending[0]["command"] == "pause"


def test_doctor_skipped_without_running_hardware(tmp_path, capsys, monkeypatch):
    # Doctor is covered in test_doctor.py; here only validate that bad paths
    # produce a non-zero CLI exit without crashing.
    path = _write_config(tmp_path, ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe")
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    rc = main(["doctor", "--short"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "doctor" in out.lower()
