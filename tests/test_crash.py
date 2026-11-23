"""Real SIGKILL crash and chaos tests using a child harness.

[SAF-6] Kills happen before/after DB commits, in the publish and retire
windows, and between DB commit and job.json snapshots.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mediaflow.db import Store

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "crash_worker.py"
pytestmark = [pytest.mark.crash, pytest.mark.integration]


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
    return env


def _run(root: Path, *extra: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(WORKER), "--root", str(root), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env())


def _assert_safe_final(root: Path, expected_jobs: int) -> None:
    state = root / "_state" / "state.db"
    store = Store(state)
    store.initialize()
    try:
        jobs = store.list_jobs(include_done=True)
        assert len(jobs) == expected_jobs
        for job in jobs:
            assert job["state"] == "DONE", (job["state"], job.get("error"), store.events(job["id"]))
            output = Path(job["output_path"])
            assert output.exists() and output.stat().st_size > 0
            marker = output.parent / f".mediaflow-{job['id']}"
            data = json.loads(marker.read_text())
            assert data["job_id"] == job["id"] and data["output_sha256"] == job["output_sha256"]
            trash = Path(job["trash_path"])
            manifest = json.loads((trash / "manifest.json").read_text())
            assert manifest["output_sha256"] == job["output_sha256"]
            assert manifest["source_sha256"]
        movies = root / "movies"
        outputs = [p for p in movies.rglob("*") if p.is_file() and not p.name.startswith(".mediaflow-")]
        assert len(outputs) == expected_jobs
        assert not list(movies.rglob("*.partial"))
        assert len({p.parent for p in outputs}) == expected_jobs
        work = root / "_pipeline" / "work"
        leftovers = [p for p in work.rglob("*") if p.name != "job.json"]
        assert leftovers == []
        for job in jobs:
            assert not (work / job["id"]).exists()
    finally:
        store.close()


@pytest.mark.parametrize("state,mode", [
    ("PLANNED", "after-commit"),
    ("PLANNED", "after-snapshot"),
    ("ENCODING", "after-commit"),
    ("PUBLISHED", "before-commit"),   # publish rename happened, DB commit lost
    ("RETIRED", "before-commit"),     # original in trash, RETIRED commit lost
    ("DONE", "after-commit"),         # DONE committed, snapshot/cleanup window
    ("NAMED", "after-snapshot"),
])
def test_sigkill_recovery(tmp_path, state, mode):
    """[SAF-6] A kill at a state boundary always recovers to a safe DONE."""
    root = tmp_path / state.lower() / mode
    killed = _run(root, "--kill-state", state, "--kill-mode", mode)
    assert killed.returncode == -signal.SIGKILL, (
        f"worker was not killed at {state}/{mode}: rc={killed.returncode}\n"
        f"stdout={killed.stdout}\nstderr={killed.stderr}"
    )
    resumed = _run(root, "--loop")
    assert resumed.returncode == 0, f"{resumed.stdout}\n{resumed.stderr}"
    _assert_safe_final(root, 1)


@pytest.mark.parametrize("phase", [
    "publish-before-rename",
    "publish-after-rename",
    "speed-before-update",
    "speed-after-update",
])
def test_sigkill_publish_and_speed_kill_points(tmp_path, phase):
    """[SAF-6] Publish and speed-update kill points recover idempotently."""

    root = tmp_path / phase
    killed = _run(root, "--kill-phase", phase)
    assert killed.returncode == -signal.SIGKILL, (
        f"worker was not killed at {phase}: rc={killed.returncode}\n"
        f"stdout={killed.stdout}\nstderr={killed.stderr}"
    )
    assert not list((root / "movies").rglob("*.partial"))

    resumed = _run(root, "--loop")
    assert resumed.returncode == 0, f"{resumed.stdout}\n{resumed.stderr}"
    before = _recovery_event_count(root)
    _assert_safe_final(root, 1)

    again = _run(root, "--loop")
    assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"
    assert _recovery_event_count(root) == before


def _recovery_event_count(root: Path) -> int:
    store = Store(root / "_state" / "state.db")
    store.initialize()
    try:
        return sum(
            1 for job in store.list_jobs(include_done=True)
            for event in store.events(job["id"])
            if event["kind"] == "recovery"
        )
    finally:
        store.close()


def test_chaos_loop(tmp_path):
    """[SAF-6] Random SIGKILLs across a batch never leave duplicates or partials."""
    root = tmp_path / "chaos"
    killed_count = 0
    rng = random.Random(20261118)
    for _ in range(12):
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), "--root", str(root), "--jobs", "3", "--loop", "--slow"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(),
        )
        time.sleep(rng.uniform(0.02, 0.35))
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
            killed_count += 1
            continue
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"{out}\n{err}"
        break
    else:
        # If random timing never managed to kill, force a final recovery pass.
        pass
    final = _run(root, "--loop", timeout=30)
    assert final.returncode == 0, f"{final.stdout}\n{final.stderr}"
    assert killed_count >= 1, "chaos loop did not exercise a SIGKILL"
    _assert_safe_final(root, 3)
