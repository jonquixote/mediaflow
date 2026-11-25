"""[LOCK-1] install.sh is a sandbox-only rehearsal tool.

These tests run the real install.sh. Destinations are always throwaway
directories under pytest's tmp_path; no test points the installer at a real
production path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
MARKER = ".mediaflow-scratch"
DESTINATIONS = {
    "prefix": "MEDIAFLOW_INSTALL_PREFIX",
    "config": "MEDIAFLOW_INSTALL_CONFIG_DIR",
    "state": "MEDIAFLOW_INSTALL_STATE_DIR",
    "systemd": "MEDIAFLOW_INSTALL_SYSTEMD_DIR",
    "doc": "MEDIAFLOW_INSTALL_DOC_DIR",
    "media": "MEDIAFLOW_INSTALL_ROOT",
}
LAYOUT = {
    "prefix": ("opt", "mediaflow"),
    "config": ("etc", "mediaflow"),
    "state": ("var", "lib", "mediaflow"),
    "systemd": ("systemd",),
    "doc": ("doc", "mediaflow"),
    "media": ("media",),
}


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _scratch(parent: Path, name: str = "mediaflow-rehearsal.lock",
             marker: bool = True) -> Path:
    root = parent / name
    root.mkdir()
    if marker:
        (root / MARKER).write_text("created-by-rehearsal\n", encoding="utf-8")
    return root


def _env(scratch, base, *, test_mode=True, **moved) -> dict:
    """Environment for install.sh with every destination under *base*.

    `moved` maps a destination key (see DESTINATIONS) to a different base.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEDIAFLOW_")}
    env.update({
        "MEDIAFLOW_INSTALL_HOST_ONLY": "1",
        "MEDIAFLOW_INSTALL_ALLOW_DIRTY": "1",
        "MEDIAFLOW_INSTALL_SERVICE_STATE": "inactive disabled",
        "MEDIAFLOW_INSTALL_EXPECTED_SHA": _head(),
    })
    if test_mode:
        env["MEDIAFLOW_INSTALL_TEST"] = "1"
    if scratch is not None:
        env["MEDIAFLOW_INSTALL_SCRATCH_ROOT"] = str(scratch)
    for key, var in DESTINATIONS.items():
        env[var] = str(Path(moved.get(key, base)).joinpath(*LAYOUT[key]))
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL)], capture_output=True, text=True, env=env,
        timeout=120,
    )


def _created(base: Path) -> list:
    return sorted(p.name for p in Path(base).iterdir() if p.name != MARKER)


def test_valid_scratch_root_still_installs(tmp_path):
    root = _scratch(tmp_path)
    result = _run(_env(root, root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "opt" / "mediaflow").is_dir()


def test_without_test_mode_refuses_before_anything(tmp_path):
    root = _scratch(tmp_path)
    result = _run(_env(root, root, test_mode=False))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not approved" in result.stderr
    assert result.stdout == ""
    assert _created(root) == []


@pytest.mark.parametrize("kind", ["missing", "unmarked", "misnamed"])
def test_invalid_scratch_designation_refuses(tmp_path, kind):
    if kind == "missing":
        root = _scratch(tmp_path)
        env = _env(None, root)
    elif kind == "unmarked":
        root = _scratch(tmp_path, marker=False)
        env = _env(root, root)
    else:
        root = _scratch(tmp_path, name="not-a-rehearsal-root")
        env = _env(root, root)
    result = _run(env)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "scratch root" in result.stderr.lower()
    assert _created(root) == []


@pytest.mark.parametrize("key", sorted(DESTINATIONS))
def test_destination_outside_scratch_root_refuses(tmp_path, key):
    root = _scratch(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    result = _run(_env(root, root, **{key: outside}))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "scratch root" in result.stderr.lower()
    assert _created(root) == []
    assert list(outside.iterdir()) == []


def test_symlink_escape_refuses(tmp_path):
    root = _scratch(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    result = _run(_env(root, root, prefix=root / "link"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert list(outside.iterdir()) == []


def test_sibling_with_shared_name_prefix_refuses(tmp_path):
    root = _scratch(tmp_path, name="mediaflow-rehearsal.a")
    evil = tmp_path / "mediaflow-rehearsal.a-evil"
    evil.mkdir()
    result = _run(_env(root, root, config=evil))
    assert result.returncode == 3, result.stdout + result.stderr
    assert list(evil.iterdir()) == []


def test_refusal_leaves_scratch_files_unchanged(tmp_path):
    root = _scratch(tmp_path)
    sentinel = root / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    marker_before = (root / MARKER).read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    result = _run(_env(root, root, state=outside))
    assert result.returncode == 3, result.stdout + result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert (root / MARKER).read_bytes() == marker_before
    assert sorted(p.name for p in root.iterdir()) == [MARKER, "sentinel"]
