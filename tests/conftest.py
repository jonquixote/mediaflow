"""Shared test fixtures.

Installer tests must run install.sh against a disposable scratch root: a
directory named mediaflow-rehearsal.* that carries the creation marker checked
by tools/scratch.py. For the modules listed below, pytest's tmp_path is such a
root and MEDIAFLOW_INSTALL_SCRATCH_ROOT names it. Other modules are unaffected.
"""
from __future__ import annotations

import pytest

INSTALLER_MODULES = frozenset({
    "test_deploy_kit",
    "test_deployment_safety",
    "test_deployment_safety_round2",
    "test_deployment_safety_round3",
    "test_service_state_gate",
})


def _is_installer_module(request) -> bool:
    return request.module.__name__.rsplit(".", 1)[-1] in INSTALLER_MODULES


@pytest.fixture
def tmp_path(tmp_path, request):
    if not _is_installer_module(request):
        return tmp_path
    scratch = tmp_path / "mediaflow-rehearsal.test"
    scratch.mkdir()
    (scratch / ".mediaflow-scratch").write_text(
        "created-by-rehearsal\n", encoding="utf-8")
    return scratch


@pytest.fixture(autouse=True)
def _installer_scratch_root(request, monkeypatch):
    if _is_installer_module(request):
        scratch = request.getfixturevalue("tmp_path")
        monkeypatch.setenv("MEDIAFLOW_INSTALL_SCRATCH_ROOT", str(scratch))


# Canonical sandbox systemctl stub. Same behavior contract as the installer
# gate: answers `systemctl show -p <prop> <unit>` for the five mediaflow units,
# absent by default, with STUB_SCENARIO (per-unit states) and STUB_MODE
# (exit1/garbage/slow) hooks for refusing scenarios. The real service manager
# is never queried in rehearsal.
SYSTEMCTL_STUB = r"""#!/usr/bin/env bash
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


@pytest.fixture(autouse=True)
def _installer_systemctl_stub(request, monkeypatch):
    """Rehearsals decide the service-state gate from a sandbox stub instead of
    the removed MEDIAFLOW_INSTALL_SERVICE_STATE override string."""
    if not _is_installer_module(request):
        return
    scratch = request.getfixturevalue("tmp_path")
    stub = scratch.parent / "systemctl-stub"
    stub.write_text(SYSTEMCTL_STUB)
    stub.chmod(0o755)
    monkeypatch.setenv("MEDIAFLOW_SYSTEMCTL", str(stub))
