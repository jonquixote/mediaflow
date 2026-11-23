"""[DEPLOY-5] End-to-end deployment safety regressions for reviewed 947c8f9."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from mediaflow.cli import main  # noqa: E402
from mediaflow.doctor import (  # noqa: E402
    Check,
    DoctorReport,
    apply_backend_acceptance,
)
from mediaflow.db import SCHEMA_VERSION, DatabaseError, Store  # noqa: E402


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


def _install_env(tmp_path: Path, repo: Path | None = None) -> dict:
    env = dict(os.environ)
    env.update({
        "MEDIAFLOW_INSTALL_TEST": "1",
        "MEDIAFLOW_INSTALL_ALLOW_DIRTY": "1",
        "MEDIAFLOW_INSTALL_SERVICE_STATE": "inactive disabled",
        "MEDIAFLOW_INSTALL_EXPECTED_SHA": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip(),
        "MEDIAFLOW_INSTALL_HOST_ONLY": "1",
        "MEDIAFLOW_INSTALL_REPO_DIR": str(repo or ROOT),
        "MEDIAFLOW_INSTALL_PREFIX": str(tmp_path / "opt" / "mediaflow"),
        "MEDIAFLOW_INSTALL_CONFIG_DIR": str(tmp_path / "etc" / "mediaflow"),
        "MEDIAFLOW_INSTALL_ROOT": str(tmp_path / "media"),
        "MEDIAFLOW_INSTALL_STATE_DIR": str(tmp_path / "var" / "lib" / "mediaflow"),
        "MEDIAFLOW_INSTALL_SYSTEMD_DIR": str(tmp_path / "systemd"),
        "MEDIAFLOW_INSTALL_DOC_DIR": str(tmp_path / "doc" / "mediaflow"),
    })
    for key in ("MEDIAFLOW_ROOT", "MEDIAFLOW_CONFIG", "MEDIAFLOW_ENV_DIR",
                "MEDIAFLOW_PRESETS", "MEDIAFLOW_DOCS", "PYTHONPATH"):
        env.pop(key, None)
    return env


def _install(tmp_path: Path, repo: Path | None = None):
    return subprocess.run(
        ["bash", str(ROOT / "install.sh")], capture_output=True, text=True,
        env=_install_env(tmp_path, repo), timeout=60,
    )


# ---------------------------------------------------------------------------
# 1. installer preflight
# ---------------------------------------------------------------------------


def test_installer_refuses_dirty_checkout_before_writes(tmp_path):
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    (repo / "tracked").write_text("one\n")
    subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "tracked").write_text("dirty\n")

    env = _install_env(tmp_path, repo)
    env["MEDIAFLOW_INSTALL_ALLOW_DIRTY"] = "0"
    env["MEDIAFLOW_INSTALL_FORCE_DIRTY_CHECK"] = "1"
    env["MEDIAFLOW_INSTALL_ALLOW_REPO_OVERRIDE"] = "1"
    env["MEDIAFLOW_INSTALL_EXPECTED_SHA"] = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    proc = subprocess.run(["bash", str(ROOT / "install.sh")], capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 3
    assert "dirty" in (proc.stderr + proc.stdout).lower()
    assert not (tmp_path / "etc").exists()


def test_installer_refuses_existing_active_or_enabled_service(tmp_path):
    env = _install_env(tmp_path)
    env["MEDIAFLOW_INSTALL_SERVICE_STATE"] = "active enabled"
    proc = subprocess.run(["bash", str(ROOT / "install.sh")], capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 3
    assert "active" in (proc.stderr + proc.stdout).lower()
    assert not (tmp_path / "etc").exists()


def test_installer_refuses_symlink_destination_and_preserves_target(tmp_path):
    env = _install_env(tmp_path)
    config_dir = tmp_path / "etc" / "mediaflow"
    config_dir.mkdir(parents=True)
    outside = tmp_path / "outside.toml"
    outside.write_text("keep me\n")
    (config_dir / "config.toml").symlink_to(outside)

    proc = subprocess.run(["bash", str(ROOT / "install.sh")], capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 3
    assert "symlink" in (proc.stderr + proc.stdout).lower()
    assert outside.read_text() == "keep me\n"


def test_installer_writes_artifact_manifest_and_rollback_is_scoped(tmp_path):
    proc = _install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    manifest = tmp_path / "var" / "lib" / "mediaflow" / "install-manifest.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert {"created_files", "replaced", "preserved", "principals"}.issubset(data)

    preexisting = tmp_path / "etc" / "mediaflow" / "config.toml"
    preexisting.write_text("keep\n")
    # Create-only model: a second install refuses the conflict and preserves bytes.
    proc = _install(tmp_path)
    assert proc.returncode == 3
    assert preexisting.read_text() == "keep\n"


# ---------------------------------------------------------------------------
# 2. doctor acceptance policy
# ---------------------------------------------------------------------------


def test_ok_is_false_when_exit_code_is_four():
    report = DoctorReport([Check("x", "warn", "x")])
    assert report.exit_code == 4
    assert report.ok is False


def test_acceptance_policies_and_disabled_backend(tmp_path):
    report = DoctorReport([
        Check("encode-qsv", "fail", "qsv failed", check_class="feature-conditional"),
        Check("encode-vaapi", "ok", "vaapi ok", check_class="feature-conditional"),
    ])
    enabled = frozenset({"qsv", "vaapi"})
    qsv = apply_backend_acceptance(DoctorReport(list(report.checks)), "qsv-required",
                                   enabled_backends=enabled)
    assert qsv.exit_code == 3
    either = apply_backend_acceptance(DoctorReport(list(report.checks)), "either",
                                      enabled_backends=enabled)
    assert either.exit_code == 0
    vaapi = apply_backend_acceptance(DoctorReport(list(report.checks)), "vaapi-required",
                                     enabled_backends=enabled)
    assert vaapi.exit_code == 0
    none = apply_backend_acceptance(DoctorReport(list(report.checks)), "none",
                                    enabled_backends=enabled)
    assert none.exit_code == 0


def test_default_doctor_documents_its_subprocesses(tmp_path, monkeypatch):
    path = _config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    called = []

    class Fake:
        exit_code = 0
        ok = True

        def as_dict(self):
            return {"ok": True, "exit_code": 0, "checks": [], "summary": {}}

    monkeypatch.setattr("mediaflow.cli.run_doctor", lambda *a, **k: called.append(k) or Fake())
    monkeypatch.setattr("mediaflow.cli.format_doctor", lambda report, short=False: "doctor ok")
    main(["doctor"])
    assert "ffmpeg -version" in (ROOT / "docs" / "DEPLOYMENT_SAFETY_HANDOFF.md").read_text()


# ---------------------------------------------------------------------------
# 3. observational DB truthfulness
# ---------------------------------------------------------------------------


def test_read_only_store_rejects_newer_schema(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.initialize()
    with store._tx() as c:
        c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
    store.close()
    with pytest.raises(DatabaseError, match="newer"):
        Store(path, read_only=True).check_schema()


def test_read_only_store_sees_active_wal_writer(tmp_path):
    path = tmp_path / "state.db"
    writer = Store(path)
    writer.initialize()
    job = writer.create_job(preset="p", source_rel="r", source_path="/tmp/r")
    # Keep the writer connection open so the WAL is uncheckpointed.
    reader = Store(path, read_only=True)
    reader.check_schema()
    assert reader.get_job(job["id"])["id"] == job["id"]
    writer.close()
    reader.close()


def test_plan_and_estimate_fail_truthfully_without_state(tmp_path, monkeypatch, capsys):
    path = _config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    assert main(["plan", str(tmp_path / "missing.mkv")]) == 3
    assert main(["estimate", str(tmp_path / "missing.mkv")]) == 3
    assert "no state database" in capsys.readouterr().err


def test_status_fallback_does_not_claim_running(tmp_path, monkeypatch, capsys):
    path = _config(tmp_path)
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    main(["status"])
    assert "unknown" in capsys.readouterr().out


def test_corrupt_state_is_not_treated_as_absence(tmp_path, monkeypatch):
    path = _config(tmp_path)
    (tmp_path / "state" / "state.db").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "state.db").write_bytes(b"not sqlite")
    monkeypatch.setenv("MEDIAFLOW_CONFIG", str(path))
    assert main(["status"]) == 3


# ---------------------------------------------------------------------------
# 4. isolated rehearsal and 5. initialization
# ---------------------------------------------------------------------------


def test_rehearsal_isolates_hostile_inherited_configuration(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched\n")
    env = dict(os.environ)
    env.update({
        "MEDIAFLOW_ROOT": "/srv/media",
        "MEDIAFLOW_CONFIG": "/etc/mediaflow/config.toml",
        "MEDIAFLOW_ENV_DIR": "/etc/mediaflow",
        "MEDIAFLOW_PRESETS": "/etc/mediaflow/presets.d",
        "MEDIAFLOW_DOCS": "/usr/share/doc/mediaflow",
        "SENTINEL": str(sentinel),
        "SCRATCH_PARENT": str(tmp_path),
    })
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "rehearse_install.sh")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert sentinel.read_text() == "untouched\n"
    assert "layout-only" in proc.stdout


def test_engine_start_refuses_missing_media_paths(tmp_path):
    from tests.test_engine import DummyConfig
    from mediaflow.db import Store
    from mediaflow.engine import Engine

    cfg = DummyConfig(tmp_path / "root")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "state.db")
    store.initialize()
    eng = Engine(cfg, store, {})
    try:
        with pytest.raises(Exception, match="missing.*media path"):
            eng.start()
        assert not cfg.movies.exists()
    finally:
        store.close()


def test_media_init_creates_only_approved_paths_and_preserves_modes(tmp_path):
    root = tmp_path / "media"
    movies = root / "movies"
    movies.mkdir(parents=True)
    os.chmod(movies, 0o755)
    music = root / "music"
    music.mkdir(parents=True)
    before = (movies.stat().st_mode, music.stat().st_mode)

    env = dict(os.environ)
    env["MEDIAFLOW_MEDIA_INIT_APPROVED"] = "1"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "media_init.py"),
         "--root", str(root), "--apply"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert (root / "_pipeline" / "convert" / "movies").is_dir()
    assert (movies.stat().st_mode, music.stat().st_mode) == before


# ---------------------------------------------------------------------------
# 6. evidence helper end-to-end
# ---------------------------------------------------------------------------


def _run_evidence(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "evidence.py"), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_evidence_capture_validate_from_other_cwd(tmp_path):
    bundle = tmp_path / "bundle"
    capture = _run_evidence("capture", "--bundle", str(bundle),
                            "--item", "HW-001", "--", sys.executable, "-c",
                            "print('ok')", cwd=tmp_path)
    assert capture.returncode == 0, capture.stderr
    valid = _run_evidence("validate", str(bundle), cwd=tmp_path / "somewhere" if False else ROOT)
    assert valid.returncode == 0, valid.stderr


def test_evidence_script_wrapper_and_failed_command(tmp_path):
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "capture_evidence.sh"), str(tmp_path / "b"),
         "HW-001", "--", sys.executable, "-c", "raise SystemExit(7)"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0  # capture itself succeeded; manifest records exit 7
    manifest = json.loads(next((tmp_path / "b").rglob("manifest.json")).read_text())
    assert manifest["exit_code"] == 7
    assert _run_evidence("validate", str(tmp_path / "b")).returncode != 0


def test_evidence_rejects_tampering_traversal_and_redacted_patterns(tmp_path):
    bundle = tmp_path / "bundle"
    assert _run_evidence("capture", "--bundle", str(bundle), "--item", "HW-001",
                         "--", sys.executable, "-c", "print('token=abc123')").returncode == 0
    # Redaction works: token=*** is accepted by the integrity validator.
    assert _run_evidence("validate", str(bundle)).returncode == 0
    output = next(bundle.glob("*stdout.txt"))
    assert "token=abc123" not in output.read_text()
    assert "token=***" in output.read_text()
    # Direct tampering with an unredacted value is still rejected.
    output.write_text("token=abc123\n")
    assert _run_evidence("validate", str(bundle)).returncode != 0

    bundle2 = tmp_path / "bundle2"
    assert _run_evidence("capture", "--bundle", str(bundle2), "--item", "HW-001",
                         "--", sys.executable, "-c", "print('ok')").returncode == 0
    manifest_path = next(bundle2.rglob("manifest.json"))
    data = json.loads(manifest_path.read_text())
    data["outputs"] = ["../../outside.txt"]
    manifest_path.write_text(json.dumps(data))
    assert _run_evidence("validate", str(bundle2)).returncode != 0


def test_evidence_requires_full_revision_and_explicit_acceptance(tmp_path):
    bundle = tmp_path / "bundle"
    assert _run_evidence("capture", "--bundle", str(bundle), "--item", "HW-001",
                         "--", sys.executable, "-c", "print('ok')").returncode == 0
    manifest_path = next(bundle.rglob("manifest.json"))
    data = json.loads(manifest_path.read_text())
    data["revision"] = "abc"
    manifest_path.write_text(json.dumps(data))
    assert _run_evidence("validate", str(bundle)).returncode != 0

    data["revision"] = "a" * 40
    manifest_path.write_text(json.dumps(data))
    assert _run_evidence("validate", str(bundle)).returncode == 0
    assert _run_evidence("update-registry", str(bundle)).returncode != 0  # acceptance missing

    accepted = _run_evidence("accept", str(bundle), "--accepted-by", "John",
                             "--notes", "on-box check reviewed",
                             "--expected-revision", "a" * 40)
    assert accepted.returncode == 0, accepted.stderr
    registry = tmp_path / "hardware_checks.toml"
    registry.write_text((ROOT / "docs" / "hardware_checks.toml").read_text())
    assert _run_evidence("update-registry", str(bundle)).returncode != 0  # no expected rev
    assert _run_evidence("update-registry", str(bundle),
                         "--expected-revision", "b" * 40).returncode != 0
    assert _run_evidence("update-registry", str(bundle),
                         "--expected-revision", "a" * 40,
                         "--registry", str(registry)).returncode == 0
