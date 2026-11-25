"""[DEPLOY-6] Reviewer round-3 deployment safety regressions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from mediaflow.cli import main  # noqa: E402
from mediaflow.db import DatabaseError, Store  # noqa: E402
from mediaflow.doctor import (  # noqa: E402
    Check,
    DoctorReport,
    _read_state_settings,
    apply_backend_acceptance,
    run_doctor,
)
from mediaflow.config import load_config  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> Path:
    presets = tmp_path / "presets.d"
    presets.mkdir(exist_ok=True)
    for source in (ROOT / "presets").glob("*.toml"):
        shutil.copy(source, presets / source.name)
    path = tmp_path / "config.toml"
    path.write_text(
        f'root = "{tmp_path / "media"}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        f'runtime_dir = "{tmp_path / "run"}"\n'
        'dry_run = true\npause_on_battery = false\ntemp_sensor = "none"\n',
        encoding="utf-8",
    )
    return path


def _install_env(tmp_path: Path, **overrides) -> dict:
    env = dict(os.environ)
    current_sha = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    env.update({
        "MEDIAFLOW_INSTALL_TEST": "1",
        "MEDIAFLOW_INSTALL_ALLOW_DIRTY": "1",
        "MEDIAFLOW_INSTALL_HOST_ONLY": "1",
        "MEDIAFLOW_INSTALL_PREFIX": str(tmp_path / "opt" / "mediaflow"),
        "MEDIAFLOW_INSTALL_CONFIG_DIR": str(tmp_path / "etc" / "mediaflow"),
        "MEDIAFLOW_INSTALL_ROOT": str(tmp_path / "media"),
        "MEDIAFLOW_INSTALL_STATE_DIR": str(tmp_path / "var" / "lib" / "mediaflow"),
        "MEDIAFLOW_INSTALL_SYSTEMD_DIR": str(tmp_path / "systemd"),
        "MEDIAFLOW_INSTALL_DOC_DIR": str(tmp_path / "doc" / "mediaflow"),
        "MEDIAFLOW_INSTALL_EXPECTED_SHA": current_sha,
    })
    for key in ("MEDIAFLOW_ROOT", "MEDIAFLOW_CONFIG", "MEDIAFLOW_ENV_DIR",
                "MEDIAFLOW_PRESETS", "MEDIAFLOW_DOCS", "PYTHONPATH",
                "MEDIAFLOW_INSTALL_SERVICE_STATE"):
        env.pop(key, None)
    env.update(overrides)
    return env


def _install(tmp_path: Path, **overrides):
    return subprocess.run(
        ["bash", str(ROOT / "install.sh")], capture_output=True, text=True,
        env=_install_env(tmp_path, **overrides), timeout=60,
    )


# ---------------------------------------------------------------------------
# 1. installer
# ---------------------------------------------------------------------------


def test_installer_enforces_expected_sha(tmp_path):
    proc = _install(tmp_path, MEDIAFLOW_INSTALL_EXPECTED_SHA="b" * 40)
    assert proc.returncode == 3
    assert "expected revision" in (proc.stderr + proc.stdout).lower()
    assert not (tmp_path / "etc").exists()


def test_installer_proceeds_when_stub_reports_all_units_absent(tmp_path):
    proc = _install(tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "create-only host installation complete" in proc.stdout


@pytest.mark.parametrize("overrides", [
    {"STUB_SCENARIO": "mediaflow.service=inactive-disabled"},
    {"STUB_SCENARIO": "mediaflow.service=active-enabled"},
    {"STUB_SCENARIO": "mediaflow.service=masked"},
    {"STUB_SCENARIO": "mediaflow.service=failed"},
    {"STUB_SCENARIO": "mediaflow.service=activating"},
    {"STUB_SCENARIO": "mediaflow.service=deactivating"},
    {"STUB_SCENARIO": "mediaflow.service=static"},
    {"STUB_SCENARIO": "mediaflow.service=indirect"},
    {"STUB_SCENARIO": "mediaflow.service=linked"},
    {"STUB_MODE": "exit1"},
    {"STUB_MODE": "garbage"},
], ids=["loaded-inactive", "loaded-active", "masked", "failed", "activating",
        "deactivating", "static", "indirect", "linked", "query-error", "garbage"])
def test_installer_refuses_loaded_units_and_query_problems_at_the_real_gate(
        tmp_path, overrides):
    target = tmp_path / "target"
    proc = _install(target, **overrides)
    output = proc.stderr + proc.stdout
    assert proc.returncode == 3, (overrides, proc.stdout, proc.stderr)
    # the refusal must come from the real service-state gate, not an earlier guard
    assert "service or timer state conflicts" in output, overrides
    assert "refuse before writes" in output
    if "STUB_MODE" in overrides:
        assert "query-error" in output, overrides
    assert not (target / "etc").exists(), overrides


def test_installer_preserves_masked_unit_symlink(tmp_path):
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    unit = systemd / "mediaflow.service"
    unit.symlink_to("/dev/null")
    proc = _install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert unit.is_symlink() and os.readlink(unit) == "/dev/null"


def test_installer_create_only_refuses_conflicting_destination(tmp_path):
    config = tmp_path / "etc" / "mediaflow"
    config.mkdir(parents=True)
    (config / "config.toml").write_text("keep\n")
    proc = _install(tmp_path)
    assert proc.returncode == 3
    assert "conflict" in (proc.stderr + proc.stdout).lower()
    assert (config / "config.toml").read_text() == "keep\n"
    assert not (tmp_path / "opt").exists()


def test_installer_preserves_evidence_and_parent_metadata(tmp_path):
    state = tmp_path / "var" / "lib" / "mediaflow"
    evidence = state / "evidence" / "20261123T042940"
    evidence.mkdir(parents=True)
    marker = evidence / "record.txt"
    marker.write_text("keep evidence\n")
    before = stat.S_IMODE(state.stat().st_mode)

    proc = _install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "keep evidence\n"
    assert stat.S_IMODE(state.stat().st_mode) == before


def test_installer_manifest_and_rollback_cover_created_files(tmp_path):
    proc = _install(tmp_path,
                    MEDIAFLOW_INSTALL_FAIL_AFTER="config")
    assert proc.returncode == 9
    manifest = tmp_path / "var" / "lib" / "mediaflow" / "install-manifest.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data["created_files"], data
    assert all("sha256" in item for item in data["created_files"])

    rollback = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "install_rollback.py"),
         str(manifest), "--apply"], capture_output=True, text=True,
    )
    assert rollback.returncode == 0, rollback.stderr
    assert not (tmp_path / "etc" / "mediaflow" / "config.toml").exists()
    assert manifest.exists()  # recovery record preserved


# ---------------------------------------------------------------------------
# 2. doctor and observational commands
# ---------------------------------------------------------------------------


def test_aggregate_either_acceptance_matrix():
    def report(qsv, vaapi, enabled=("qsv", "vaapi")):
        checks = []
        for name, status in (("encode-qsv", qsv), ("encode-vaapi", vaapi)):
            if status == "missing":
                continue
            checks.append(Check(name, status, name, check_class="feature-conditional"))
        result = apply_backend_acceptance(DoctorReport(checks), "either",
                                          enabled_backends=frozenset(enabled))
        return result.exit_code, result.ok

    assert report("ok", "fail")[0] == 0
    assert report("ok", "warn")[0] == 0
    assert report("fail", "fail")[0] == 3
    assert report("fail", "warn")[0] == 4
    assert report("warn", "warn")[0] == 4
    assert report("ok", "fail", enabled=("vaapi",))[0] == 3  # qsv disabled
    assert report("missing", "ok")[0] == 4  # qsv required but absent


def test_doctor_state_reads_are_schema_truthful_and_wal_aware(tmp_path):
    path = _config(tmp_path)
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    writer = Store(cfg.db_path)
    writer.initialize()
    writer.set_setting("ffmpeg_changed_hold", "ffmpeg-changed")
    # keep writer open so WAL is uncheckpointed
    settings = _read_state_settings(cfg.db_path)
    assert settings.get("ffmpeg_changed_hold") == "ffmpeg-changed"
    writer.close()

    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not sqlite")
    with pytest.raises(DatabaseError):
        _read_state_settings(broken)


def test_read_only_open_error_is_database_error(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"not sqlite")
    with pytest.raises(DatabaseError):
        Store(db, read_only=True)


def test_library_report_does_not_write_cache_for_observational_run(tmp_path, monkeypatch):
    path = _config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    store = Store(cfg.db_path)
    store.initialize()
    store.close()
    monkeypatch.setattr("mediaflow.db.Store.put_library_cache",
                        lambda *a, **k: pytest.fail("cache write in observational command"))
    assert main(["library", "report"]) == 0


# ---------------------------------------------------------------------------
# 3. rehearsal and scratch DB initialization
# ---------------------------------------------------------------------------


def test_rehearsal_does_not_hide_doctor_crash(tmp_path):
    env = dict(os.environ)
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\nexit 9\n")
    fake_python.chmod(0o755)
    env.update({
        "MEDIAFLOW_REHEARSAL_PYTHON": str(fake_python),
        "SCRATCH_PARENT": str(tmp_path),
        "KEEP_SCRATCH": "1",
    })
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "rehearse_install.sh")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode != 0


def test_rehearsal_checks_sentinel_contents(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("original\n")
    env = dict(os.environ)
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(f"#!/bin/sh\nprintf changed > {sentinel}\nexit 0\n")
    fake_python.chmod(0o755)
    env.update({
        "MEDIAFLOW_REHEARSAL_PYTHON": str(fake_python),
        "SENTINEL": str(sentinel), "SCRATCH_PARENT": str(tmp_path), "KEEP_SCRATCH": "1",
    })
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "rehearse_install.sh")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode != 0


def test_scratch_cleanup_rejects_substitution_and_symlink(tmp_path):
    target = tmp_path / "mediaflow-rehearsal.fake"
    target.mkdir()
    marker = subprocess.run([sys.executable, str(ROOT / "tools" / "scratch.py"),
                             "validate", str(target)], capture_output=True, text=True)
    assert marker.returncode != 0
    marker_file = target / ".mediaflow-scratch"
    marker_file.write_text("created-by-rehearsal\n")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "scratch.py"),
                           "validate", str(target)]).returncode == 0
    link = tmp_path / "link-rehearsal.abc"
    link.symlink_to(target, target_is_directory=True)
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "scratch.py"),
                           "validate", str(link)]).returncode != 0


def test_scratch_db_initialization_enables_first_plan(tmp_path, monkeypatch):
    path = _config(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "scratch_db.py"), str(path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    cfg = load_config(path, environ={})
    assert cfg.db_path.is_file()
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    ffmpeg = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
    if not Path(ffmpeg).is_file():
        pytest.skip("ffmpeg genuinely unavailable for synthetic fixture")
    clip = cfg.convert / "keep-remux-mp4" / "sample.mkv"
    clip.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
        check=True, capture_output=True, timeout=60,
    )
    assert main(["plan", str(clip), "--preset", "keep-remux-mp4"]) in (0, 2)


# ---------------------------------------------------------------------------
# 4. initialization helper
# ---------------------------------------------------------------------------


def test_media_init_documented_command_and_symlink_ancestors(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "_pipeline").symlink_to(outside, target_is_directory=True)
    env = dict(os.environ)
    env["MEDIAFLOW_MEDIA_INIT_APPROVED"] = "1"
    env["MEDIAFLOW_MEDIA_INIT_UID"] = "1001"
    env["MEDIAFLOW_MEDIA_INIT_GID"] = "1001"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "media_init.py"),
         "--root", str(root), "--apply"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode != 0
    assert "symlink" in proc.stderr.lower()


def test_media_init_sets_expected_mode_on_new_paths(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    env = dict(os.environ)
    env["MEDIAFLOW_MEDIA_INIT_APPROVED"] = "1"
    env["MEDIAFLOW_MEDIA_INIT_UID"] = "1001"
    env["MEDIAFLOW_MEDIA_INIT_GID"] = "1001"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "media_init.py"),
         "--root", str(root), "--apply"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    pipeline = root / "_pipeline"
    assert stat.S_IMODE(pipeline.stat().st_mode) == 0o2775


# ---------------------------------------------------------------------------
# 5. evidence acceptance binding
# ---------------------------------------------------------------------------


def _sha() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _evidence(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "evidence.py"), *args],
        capture_output=True, text=True,
    )


def test_acceptance_is_bound_to_bundle_contents(tmp_path):
    bundle = tmp_path / "HW-001"
    assert _evidence("capture", "--bundle", str(bundle), "--item", "HW-001",
                     "--", sys.executable, "-c", "print('ok')").returncode == 0
    assert _evidence("validate", str(bundle)).returncode == 0
    assert _evidence("accept", str(bundle), "--accepted-by", "John",
                     "--notes", "reviewed", "--expected-revision", _sha()).returncode == 0
    output = next(bundle.glob("*stdout.txt"))
    output.write_text("different harmless output\n")
    registry = tmp_path / "hardware_checks.toml"
    registry.write_text((ROOT / "docs" / "hardware_checks.toml").read_text())
    mutated = _evidence("update-registry", str(bundle), "--expected-revision", _sha(),
                        "--registry", str(registry))
    assert mutated.returncode != 0
    assert "changed after acceptance" in mutated.stderr


def test_redaction_is_idempotent_for_media_paths():
    import evidence

    once = evidence.redact_text("path /srv/media/movies/Film (2001)\n")
    twice = evidence.redact_text(once)
    assert once == twice


def test_recapture_invalidates_previous_acceptance(tmp_path):
    bundle = tmp_path / "HW-001"
    assert _evidence("capture", "--bundle", str(bundle), "--item", "HW-001",
                     "--", sys.executable, "-c", "print('first')").returncode == 0
    assert _evidence("accept", str(bundle), "--accepted-by", "John",
                     "--notes", "first", "--expected-revision", _sha()).returncode == 0
    assert _evidence("capture", "--bundle", str(bundle), "--item", "HW-001",
                     "--", sys.executable, "-c", "print('second')").returncode == 0
    assert not (bundle / "acceptance.json").exists()
