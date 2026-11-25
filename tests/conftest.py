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
