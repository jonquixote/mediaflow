"""[DEPLOY-1] Deployment robustness leads that can be exercised in sandbox."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mediaflow.guards import GIB, GuardConfig, check_space
from mediaflow.plan import PlanError, ReviewRequired, build_plan
from mediaflow.supervision import ProgressTracker, SupervisionPolicy
from tests.test_plan import make_preset, make_probe


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_stall_tracker_uses_monotonic_clock_not_wall_clock(monkeypatch):
    clock = _Clock()
    tracker = ProgressTracker(policy=SupervisionPolicy(stall_timeout=10),
                              clock=clock)
    tracker.observe({"frame": 1})
    real_time = time.time

    monkeypatch.setattr(time, "time", lambda: real_time() + 10_000)
    clock.advance(9)
    assert not tracker.stalled()
    clock.advance(2)
    assert tracker.stalled()
    monkeypatch.setattr(time, "time", real_time)


def test_missing_hardware_backend_refuses_deterministically(tmp_path: Path):
    probe = make_probe(tmp_path, video={"codec_name": "hevc", "pix_fmt": "yuv420p"})
    preset = make_preset(
        video={"allow_software": False, "backend_order": []},
        schedule={"window": ""},
    )
    with pytest.raises((PlanError, ReviewRequired)) as exc:
        build_plan(
            source_path=probe.path, probe=probe, preset=preset, config={"dry_run": True},
            identity={"title": "Test", "year": 2001}, capabilities={}, speed_factor=None,
        )
    assert "no capable hardware encoder" in str(exc.value) or "10bit-no-hw" in str(exc.value)


def test_full_disk_at_publish_gate_fails_closed(tmp_path: Path):
    cfg = GuardConfig(
        min_free_gb=10, root_min_free_gb=1,
        filesystem_root=tmp_path, db_dir=tmp_path,
        trash=tmp_path / "trash", pipeline=tmp_path / "pipeline",
        library=tmp_path / "library",
    )
    stats = lambda path: type("Usage", (), {"total": GIB, "used": GIB, "free": 1})()
    _status, decision = check_space(cfg, source_size=2 * GIB, disk_usage=stats)
    assert not decision.ok
    assert "space-low" in decision.reasons
