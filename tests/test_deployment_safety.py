"""Targeted deployment-safety regressions for the reviewed f058bb3 baseline."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediaflow.cli import main  # noqa: E402
from mediaflow.config import load_config  # noqa: E402
from mediaflow.doctor import run_capability_tests  # noqa: E402


def _write_config(tmp_path: Path) -> Path:
    presets = tmp_path / "presets.d"
    presets.mkdir(exist_ok=True)
    for source in (ROOT / "presets").glob("*.toml"):
        shutil.copy(source, presets / source.name)
    path = tmp_path / "config.toml"
    path.write_text(
        f'root = "{tmp_path / "media"}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        f'runtime_dir = "{tmp_path / "run"}"\n'
        "dry_run = true\npause_on_battery = false\ntemp_sensor = \"none\"\n",
        encoding="utf-8",
    )
    return path


def _install_env(tmp_path: Path, *, host_only: bool) -> dict:
    env = dict(os.environ)
    env.update({
        "MEDIAFLOW_INSTALL_TEST": "1",
        "MEDIAFLOW_INSTALL_HOST_ONLY": "1" if host_only else "0",
        "MEDIAFLOW_INSTALL_PREFIX": str(tmp_path / "opt" / "mediaflow"),
        "MEDIAFLOW_INSTALL_CONFIG_DIR": str(tmp_path / "etc" / "mediaflow"),
        "MEDIAFLOW_INSTALL_ROOT": str(tmp_path / "media"),
        "MEDIAFLOW_INSTALL_STATE_DIR": str(tmp_path / "var" / "lib" / "mediaflow"),
        "MEDIAFLOW_INSTALL_SYSTEMD_DIR": str(tmp_path / "systemd"),
        "MEDIAFLOW_INSTALL_DOC_DIR": str(tmp_path / "doc" / "mediaflow"),
    })
    env.pop("MEDIAFLOW_ROOT", None)
    return env


def _install(tmp_path: Path, *, host_only: bool):
    return subprocess.run(
        ["bash", str(ROOT / "install.sh")], capture_output=True, text=True,
        env=_install_env(tmp_path, host_only=host_only), timeout=60,
    )


def test_host_only_install_does_not_touch_media_or_service_state(tmp_path):
    proc = _install(tmp_path, host_only=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not (tmp_path / "media").exists()
    assert "no media roots" in proc.stdout
    assert "service left stopped and disabled" in proc.stdout
    assert "systemctl enable" not in proc.stdout
    assert (tmp_path / "etc" / "mediaflow" / "config.toml").is_file()
    assert (tmp_path / "var" / "lib" / "mediaflow").is_dir()


def test_full_install_still_creates_media_when_explicitly_requested(tmp_path):
    proc = _install(tmp_path, host_only=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (tmp_path / "media" / "movies").is_dir()
    assert "systemctl enable" in proc.stdout


def test_engine_start_and_scan_do_not_initialize_media_paths(tmp_path):
    from tests.test_engine import DummyConfig
    from mediaflow.db import Store
    from mediaflow.engine import Engine

    cfg = DummyConfig(tmp_path / "root")
    for path in (cfg.state_dir, cfg.log_dir):
        path.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "state.db")
    store.initialize()
    eng = Engine(cfg, store, {})
    try:
        eng._ensure_dirs()
        eng.scan()
        assert not cfg.movies.exists()
        assert not cfg.pipeline.exists()
        assert cfg.log_dir.is_dir()
    finally:
        store.close()


def test_config_ensure_directories_can_preserve_existing_library_metadata(tmp_path):
    path = _write_config(tmp_path)
    cfg = load_config(path, environ={})
    movies = cfg.movies
    movies.mkdir(parents=True, exist_ok=True)
    os.chmod(movies, 0o755)
    before = stat.S_IMODE(movies.stat().st_mode)

    cfg.ensure_directories(normalize_existing=False)

    assert stat.S_IMODE(movies.stat().st_mode) == before
    assert cfg.work.is_dir()  # scratch/state paths are still initialized explicitly


def test_capability_probes_default_to_hardware_only(tmp_path):
    cfg = load_config(_write_config(tmp_path), environ={})
    with pytest.raises(ValueError, match="software"):
        run_capability_tests(cfg, backends=("qsv", "software"))
    with pytest.raises(ValueError, match="at least one"):
        run_capability_tests(cfg, backends=())


def test_cli_doctor_default_is_non_encoding_and_rejects_software(tmp_path, monkeypatch, capsys):
    path = _write_config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    calls = []
    monkeypatch.setattr("mediaflow.cli.run_doctor", lambda *a, **k: calls.append(k) or _FakeReport())
    monkeypatch.setattr("mediaflow.cli.format_doctor", lambda report, short=False: "doctor ok")

    assert main(["doctor"]) == 0
    assert calls[-1]["run_capabilities"] is False
    assert calls[-1]["capability_backends"] is None

    assert main(["doctor", "--backends", "software", "--capabilities"]) == 2
    assert "software capability probing is not supported" in capsys.readouterr().err

    assert main(["doctor", "--capabilities", "--backends", "qsv,vaapi"]) == 0
    assert calls[-1]["run_capabilities"] is True
    assert calls[-1]["capability_backends"] == ("qsv", "vaapi")


class _FakeReport:
    ok = True
    exit_code = 0

    def as_dict(self):
        return {"ok": True, "exit_code": 0, "checks": []}


def test_automatic_ffmpeg_retest_never_enables_software_probing(tmp_path, monkeypatch):
    from mediaflow.daemon import Daemon
    from mediaflow.db import Store

    path = _write_config(tmp_path)
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    (cfg.capabilities_path).parent.mkdir(parents=True, exist_ok=True)
    (cfg.capabilities_path).write_text(json.dumps({
        "capabilities_schema": 2, "ffmpeg_version": "old",
    }), encoding="utf-8")
    store = Store(cfg.db_path)
    store.initialize()
    calls = []

    class Completed:
        stdout = "ffmpeg version new\n"

    monkeypatch.setattr("mediaflow.daemon.subprocess.run", lambda *a, **k: Completed())
    monkeypatch.setattr(
        "mediaflow.doctor.run_capability_tests",
        lambda *a, **k: calls.append(k) or {"status": "deferred", "completed": False},
    )
    try:
        daemon = Daemon(cfg, store, {}, config_path=path)
        daemon._check_ffmpeg()
        assert calls
        assert calls[0]["backends"] == ("qsv", "vaapi")
        assert calls[0]["allow_software"] is False
        assert any(r.startswith("ffmpeg-changed") for r in daemon.engine.pause_reasons)
    finally:
        store.close()


@pytest.mark.parametrize("command", [
    ["status"], ["queue"], ["review", "list"], ["library", "report"],
])
def test_observational_commands_do_not_create_state(tmp_path, monkeypatch, command):
    path = _write_config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    assert main(command) == 0
    assert not (tmp_path / "state" / "state.db").exists()
