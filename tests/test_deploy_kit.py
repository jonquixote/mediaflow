"""[DEPLOY-3] Installer rehearsal and evidence-bundle tooling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import evidence  # noqa: E402

INSTALL = ROOT / "install.sh"
EVIDENCE = ROOT / "tools" / "evidence.py"
CLIPS = ROOT / "scripts" / "make_test_clips.sh"
pytestmark = pytest.mark.integration


def _install_env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "MEDIAFLOW_INSTALL_TEST": "1",
        "MEDIAFLOW_INSTALL_ALLOW_DIRTY": "1",
        "MEDIAFLOW_INSTALL_PREFIX": str(tmp_path / "opt" / "mediaflow"),
        "MEDIAFLOW_INSTALL_CONFIG_DIR": str(tmp_path / "etc" / "mediaflow"),
        "MEDIAFLOW_INSTALL_ROOT": str(tmp_path / "media"),
        "MEDIAFLOW_INSTALL_STATE_DIR": str(tmp_path / "var" / "lib" / "mediaflow"),
        "MEDIAFLOW_INSTALL_SYSTEMD_DIR": str(tmp_path / "systemd"),
        "MEDIAFLOW_INSTALL_DOC_DIR": str(tmp_path / "doc" / "mediaflow"),
    })
    env.pop("MEDIAFLOW_ROOT", None)
    return env


def _install(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL)], capture_output=True, text=True,
        env=_install_env(tmp_path), timeout=60,
    )


def test_installer_rehearsal_fresh_and_repeat(tmp_path):
    first = _install(tmp_path)
    assert first.returncode == 0, first.stderr + first.stdout
    config = tmp_path / "etc" / "mediaflow" / "config.toml"
    media = tmp_path / "media"
    assert config.is_file()
    assert (media / "movies").is_dir()
    assert (tmp_path / "systemd" / "mediaflow.service").is_file()

    config.write_text("dry_run = false  # locally edited\n", encoding="utf-8")
    second = _install(tmp_path)
    assert second.returncode == 0, second.stderr + second.stdout
    assert config.read_text() == "dry_run = false  # locally edited\n"


def test_installer_rehearsal_preserves_state_and_secrets(tmp_path):
    _install(tmp_path)
    state = tmp_path / "var" / "lib" / "mediaflow"
    (state / "state.db").write_bytes(b"existing state")
    secrets = tmp_path / "etc" / "mediaflow" / "tmdb.env"
    secrets.write_text("TMDB_READ_TOKEN=keep-me\n", encoding="utf-8")

    assert _install(tmp_path).returncode == 0
    assert (state / "state.db").read_bytes() == b"existing state"
    assert secrets.read_text() == "TMDB_READ_TOKEN=keep-me\n"


def test_rehearsal_refuses_production_media_root(tmp_path):
    env = _install_env(tmp_path)
    env["MEDIAFLOW_INSTALL_ROOT"] = "/srv/media"
    proc = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True,
                          env=env, timeout=30)
    assert proc.returncode == 2
    assert "production media root" in proc.stderr


def _bundle(tmp_path: Path, *, item="HW-001", exit_code=0, outputs=True, valid=True):
    directory = tmp_path / "bundle" / item
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 2, "item_id": item, "timestamp": "2026-11-22T00:00:00+00:00",
        "revision": "a" * 40, "command": ["mediaflow", "doctor", "--json"],
        "exit_code": exit_code,
        "outputs": [str(directory / "stdout.txt")] if outputs else [],
    }
    if valid:
        (directory / "stdout.txt").write_text('{"ok": true}\n', encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_evidence_bundle_validates_and_updates_registry(tmp_path):
    bundle = _bundle(tmp_path)
    assert evidence.validate(bundle)["item_id"] == "HW-001"
    registry = tmp_path / "hardware_checks.toml"
    registry.write_text((ROOT / "docs" / "hardware_checks.toml").read_text())
    evidence.accept(bundle, accepted_by="John", notes="reviewed",
                     expected_revision="a" * 40)
    assert evidence.update_registry(
        bundle, registry, expected_revision="a" * 40,
    ) == "HW-001"
    data = tomllib.loads(registry.read_text())
    item = next(c for c in data["checks"] if c["id"] == "HW-001")
    assert item["status"] == "verified-on-hardware"
    assert item["verified_revision"] == "a" * 40


def test_evidence_validator_rejects_incomplete_and_tampered_bundles(tmp_path):
    with pytest.raises(ValueError):
        evidence.validate(_bundle(tmp_path, outputs=False))
    with pytest.raises(ValueError):
        evidence.validate(_bundle(tmp_path, exit_code=1))
    tampered = _bundle(tmp_path)
    (tampered / "stdout.txt").write_text("TMDB_READ_TOKEN=secret-value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unredacted"):
        evidence.validate(tampered)


def test_redaction_removes_tokens_urls_and_titles():
    text = (
        "TMDB_READ_TOKEN=abc123\nBearer abcdef\n"
        "http://user:pass@example.invalid/topic\nFilm (2001)\n"
    )
    redacted = evidence.redact_text(text)
    assert "abc123" not in redacted
    assert "Bearer abcdef" not in redacted
    assert "user:pass" not in redacted
    assert "Film (2001)" not in redacted
    assert "[TITLE]" in redacted


def test_synthetic_clip_generator(tmp_path):
    ffmpeg = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
    if not Path(ffmpeg).is_file():
        pytest.skip("ffmpeg genuinely unavailable")
    out = tmp_path / "clips"
    proc = subprocess.run(["bash", str(CLIPS), str(out)], capture_output=True,
                          text=True, env={**os.environ, "MEDIAFLOW_FFMPEG": ffmpeg},
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert (out / "sdr-h264.mkv").stat().st_size > 0
    assert (out / "vfr-h264.mkv").stat().st_size > 0
    assert (out / "synthetic.srt").stat().st_size > 0
