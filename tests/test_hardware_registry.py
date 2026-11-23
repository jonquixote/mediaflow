"""[DEPLOY-1] Hardware verification registry integrity and generated view."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import gen_hardware_checks as generator  # noqa: E402


def _registry():
    return tomllib.loads((ROOT / "docs" / "hardware_checks.toml").read_text())


def test_registry_is_valid():
    data = _registry()
    generator.validate(data)
    assert len(data["checks"]) >= 40
    ids = [c["id"] for c in data["checks"]]
    assert ids == [f"HW-{i:03d}" for i in range(1, len(ids) + 1)]


def test_generated_view_is_fresh():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_hardware_checks.py"), "--check"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_deploy_requirements_have_registry_coverage():
    data = _registry()
    supported = {rid for check in data["checks"] for rid in check["supports"]}
    for rid in ("DEPLOY-1", "DEPLOY-2", "DEPLOY-3"):
        assert rid in supported


def test_every_open_question_has_a_registry_id():
    text = (ROOT / "docs" / "QUESTIONS.md").read_text()
    ids = {c["id"] for c in _registry()["checks"]}
    open_lines = [line for line in text.splitlines() if line.strip().startswith("- [ ]")]
    assert open_lines
    for line in open_lines:
        found = [token for token in ids if f"[{token}]" in line]
        assert len(found) == 1, line


def test_first_run_references_only_known_registry_ids():
    import re

    ids = {c["id"] for c in _registry()["checks"]}
    text = (ROOT / "docs" / "FIRST_RUN.md").read_text()
    found = set(re.findall(r"HW-\d{3}", text))
    assert found
    assert not found - ids


def test_hardware_verification_requires_evidence(monkeypatch):
    data = _registry()
    check = dict(data["checks"][0])
    check["status"] = "verified-on-hardware"
    requirements = generator.load_requirements()
    with pytest.raises(AssertionError, match="date"):
        generator.validate({"checks": [check]}, requirements)
    check["verified_at"] = "2026-11-22"
    with pytest.raises(AssertionError, match="revision"):
        generator.validate({"checks": [check]}, requirements)
