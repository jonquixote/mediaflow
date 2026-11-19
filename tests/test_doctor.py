"""Doctor checks and fail-closed behaviour."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaflow.config import load_config
from mediaflow.doctor import DoctorReport, Check, format_doctor, run_doctor


def _setup(tmp_path):
    etc = tmp_path / "etc"
    (etc / "presets.d").mkdir(parents=True)
    for p in (Path(__file__).resolve().parents[1] / "presets").glob("*.toml"):
        shutil.copy(p, etc / "presets.d" / p.name)
    path = etc / "config.toml"
    path.write_text(f'''root = "{tmp_path / "media"}"
state_dir = "{tmp_path / "state"}"
runtime_dir = "{tmp_path / "run"}"
dry_run = true
pause_on_battery = false
temp_sensor = "none"
min_free_gb = 1
''')
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    for preset in ("keep-remux-mp4", "keep-remux-mkv", "1080p-balanced-mp4",
                   "1080p-small-mp4", "720p-small-mp4"):
        (cfg.convert / preset).mkdir(parents=True, exist_ok=True)
    from mediaflow.db import Store
    store = Store(cfg.db_path)
    store.initialize()
    backup = store.backup(cfg.backup_dir)
    store.close()
    return path, cfg


def test_doctor_reports_capabilities_and_rate_control(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    caps = {
        "ok": True, "ffmpeg_version": "ffmpeg test", "libvmaf": True,
        "backends": {
            "qsv": {"available": True, "hevc8": True, "hevc10": False,
                    "rate_control": "ICQ", "error": None, "error_10bit": "unsupported"},
            "vaapi": {"available": True, "hevc8": True, "hevc10": True,
                      "rate_control": "CQP", "error": None, "error_10bit": None},
        },
        "qsv": {"hevc8": True, "hevc10": False}, "vaapi": {"hevc8": True, "hevc10": True},
    }
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests", lambda *a, **k: caps)
    monkeypatch.setattr("mediaflow.doctor.os.access", lambda *a, **k: True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("- [x] resolved\n")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    assert isinstance(report, DoctorReport)
    names = {c.name: c for c in report.checks}
    assert names["encode-qsv"].status == "ok"
    assert "10-bit=no" in names["encode-qsv"].detail
    assert "ICQ" in names["encode-qsv"].detail
    assert names["encode-vaapi"].status == "ok"
    assert report.ok, [c for c in report.checks if c.status == "fail"]
    assert json.loads(cfg.capabilities_path.read_text())["backends"]["vaapi"]["hevc10"] is True
    short = format_doctor(report, short=True)
    assert len(short.splitlines()) <= 12
    assert all(len(line) <= 60 for line in short.splitlines())


def test_doctor_does_not_call_software_10bit_hardware(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    caps = {
        "ok": True, "ffmpeg_version": "ffmpeg test", "libvmaf": False,
        "backends": {
            "qsv": {"available": False, "hevc8": False, "hevc10": False,
                    "rate_control": "unknown", "error": "no device"},
            "vaapi": {"available": False, "hevc8": False, "hevc10": False,
                      "rate_control": "unknown", "error": "no device"},
            "software": {"available": True, "hevc8": True, "hevc10": True,
                         "rate_control": "unknown"},
        },
        "software": {"hevc8": True, "hevc10": True},
    }
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests", lambda *a, **k: caps)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    names = {c.name: c for c in report.checks}
    assert names["hevc-10bit"].status == "warn"
    assert names["hardware-encode"].status == "warn"


def test_doctor_fails_closed_without_temp_sensor(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    # Force the sensor path back to auto and simulate a missing coretemp hwmon.
    object.__setattr__(cfg, "temp_sensor", "auto")
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests",
                        lambda *a, **k: {"ok": True, "backends": {}, "ffmpeg_version": "x"})
    monkeypatch.setattr("mediaflow.guards.discover_temp_sensor", lambda cfg: None)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    sensor = next(c for c in report.checks if c.name == "temp-sensor")
    assert sensor.status == "fail"
    assert not report.ok


def test_doctor_flags_unknown_preset_folder(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    (cfg.convert / "mystery-preset").mkdir()
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests",
                        lambda *a, **k: {"ok": True, "backends": {}, "ffmpeg_version": "x"})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    assert any(c.name == "unknown-preset-mystery-preset" and c.status == "fail"
               for c in report.checks)
