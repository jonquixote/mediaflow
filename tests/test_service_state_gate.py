"""Tests for the create-only installer's service-state gate.

These run the real helper (tools/service_state.py) against a stub systemctl,
and the real install.sh preflight up to its first refusal.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "tools" / "service_state.py"
INSTALL = REPO / "install.sh"
UNITS = [
    "mediaflow.service",
    "mediaflow-trash.service",
    "mediaflow-trash.timer",
    "mediaflow-doctor.service",
    "mediaflow-doctor.timer",
]

STUB = r"""#!/usr/bin/env bash
unit="${@: -1}"
case "${STUB_MODE:-}" in
  exit1) exit 1;;
  garbage) echo hello; exit 0;;
  slow) sleep 30;;
esac
state=""
IFS=',' read -ra pairs <<< "${STUB_SCENARIO:-}"
for p in "${pairs[@]}"; do
  if [[ "${p%%=*}" == "$unit" ]]; then state="${p#*=}"; fi
done
state="${state:-absent}"
echo "Id=$unit"
case "$state" in
  absent) echo "LoadState=not-found"; echo "ActiveState=inactive"; echo "UnitFileState=";;
  inactive-disabled) echo "LoadState=loaded"; echo "ActiveState=inactive"; echo "UnitFileState=disabled";;
  active-enabled) echo "LoadState=loaded"; echo "ActiveState=active"; echo "UnitFileState=enabled";;
  masked) echo "LoadState=masked"; echo "ActiveState=inactive"; echo "UnitFileState=masked";;
  failed) echo "LoadState=loaded"; echo "ActiveState=failed"; echo "UnitFileState=disabled";;
  activating) echo "LoadState=loaded"; echo "ActiveState=activating"; echo "UnitFileState=disabled";;
  deactivating) echo "LoadState=loaded"; echo "ActiveState=deactivating"; echo "UnitFileState=enabled";;
  static) echo "LoadState=loaded"; echo "ActiveState=inactive"; echo "UnitFileState=static";;
  indirect) echo "LoadState=loaded"; echo "ActiveState=inactive"; echo "UnitFileState=indirect";;
  linked) echo "LoadState=loaded"; echo "ActiveState=inactive"; echo "UnitFileState=linked";;
esac
"""


@pytest.fixture
def stub(tmp_path):
    path = tmp_path / "systemctl"
    path.write_text(STUB)
    path.chmod(0o755)
    return path


def run_helper(stub, test_mode=True, **extra):
    env = {"PATH": os.environ.get("PATH", "")}
    if test_mode:
        env["MEDIAFLOW_INSTALL_TEST"] = "1"
        env["MEDIAFLOW_SYSTEMCTL"] = str(stub)
    env.update(extra)
    return subprocess.run(
        [sys.executable, str(HELPER), *UNITS],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_all_units_absent_passes(stub):
    result = run_helper(stub)
    assert result.returncode == 0, result.stdout
    assert result.stdout.count(": ok absent") == len(UNITS)


@pytest.mark.parametrize("unit", UNITS)
@pytest.mark.parametrize("state", [
    "inactive-disabled", "active-enabled", "masked", "failed",
    "activating", "deactivating", "static", "indirect", "linked",
])
def test_any_loaded_unit_refuses_and_names_it(stub, unit, state):
    result = run_helper(stub, STUB_SCENARIO=f"{unit}={state}")
    assert result.returncode == 3, result.stdout
    assert f"{unit}: REFUSE" in result.stdout


def test_systemctl_nonzero_exit_refuses(stub):
    result = run_helper(stub, STUB_MODE="exit1")
    assert result.returncode == 3
    assert "query-error" in result.stdout


def test_garbage_output_refuses(stub):
    result = run_helper(stub, STUB_MODE="garbage")
    assert result.returncode == 3
    assert "unparseable" in result.stdout


def test_override_is_ignored_outside_test_mode(stub, tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    env = {"PATH": str(empty), "MEDIAFLOW_SYSTEMCTL": str(stub)}
    result = subprocess.run(
        [sys.executable, str(HELPER), *UNITS],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 3
    assert "systemctl not found" in result.stdout


def test_timeout_refuses(stub, monkeypatch):
    spec = importlib.util.spec_from_file_location("service_state_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MEDIAFLOW_INSTALL_TEST", "1")
    monkeypatch.setenv("MEDIAFLOW_SYSTEMCTL", str(stub))
    monkeypatch.setenv("STUB_MODE", "slow")
    monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.5)
    values, error = module.query(UNITS[0])
    assert values is None
    assert "timed out" in error


def test_install_script_is_valid_bash_and_uses_the_helper():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0
    text = INSTALL.read_text()
    assert '"$REPO_DIR/tools/service_state.py"' in text
    assert "/opt/mediaflow/venv/bin/python3" not in text
    assert "systemctl is-active" not in text
    assert "systemctl is-enabled" not in text


@pytest.mark.parametrize("state", ["active-enabled", "inactive-disabled", "masked"])
def test_installer_refuses_before_any_write_when_a_unit_is_loaded(stub, tmp_path, state):
    dirs = {name: tmp_path / name for name in
            ("prefix", "config", "state", "systemd", "doc", "media")}
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "MEDIAFLOW_INSTALL_TEST": "1",
        "MEDIAFLOW_SYSTEMCTL": str(stub),
        "STUB_SCENARIO": f"mediaflow.service={state}",
        "MEDIAFLOW_INSTALL_HOST_ONLY": "1",
        "MEDIAFLOW_INSTALL_PREFIX": str(dirs["prefix"]),
        "MEDIAFLOW_INSTALL_CONFIG_DIR": str(dirs["config"]),
        "MEDIAFLOW_INSTALL_STATE_DIR": str(dirs["state"]),
        "MEDIAFLOW_INSTALL_SYSTEMD_DIR": str(dirs["systemd"]),
        "MEDIAFLOW_INSTALL_DOC_DIR": str(dirs["doc"]),
        "MEDIAFLOW_INSTALL_ROOT": str(dirs["media"]),
    }
    result = subprocess.run(
        ["bash", str(INSTALL)], capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 3, result.stderr
    assert "refuse before writes" in result.stderr
    assert not any(path.exists() for path in dirs.values())
