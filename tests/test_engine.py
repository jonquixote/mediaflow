"""Engine state-machine, review routing and recovery tests using a fake encoder.

[SAF-4] [SAF-6] [SAF-7] [SAF-11] [REV-1]
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaflow.db import Store
from mediaflow.engine import CommandError, Engine, RestorePurged, ReviewNeeded
from mediaflow.safety import FileSafety


class DummyConfig:
    def __init__(self, root: Path, dry_run=False):
        self.root = root
        self.movies = root / "movies"
        self.pipeline = root / "_pipeline"
        self.inbox = self.pipeline / "inbox"
        self.convert = self.pipeline / "convert"
        self.work = self.pipeline / "work"
        self.review = self.pipeline / "review"
        self.failed = self.pipeline / "failed"
        self.trash = self.pipeline / "trash"
        self.state_dir = root / "_state"
        self.log_dir = self.state_dir / "logs"
        self.status_path = self.state_dir / "status.json"
        self.progress_path = self.state_dir / "progress.json"
        self.capabilities_path = self.state_dir / "capabilities.json"
        self.dry_run = dry_run
        self.stable_seconds = 0
        self.poll_seconds = 30
        self.max_restarts = 3
        self.progress_seconds = 30
        self.min_output_kbps = 300
        self.min_bpp = 0.01
        self.trash_retention_days = 14
        self.failed_retention_days = 30
        self.ffmpeg = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
        self.ffprobe = "/usr/lib/jellyfin-ffmpeg/ffprobe"
        self.append_tmdbid = False
        self.secrets = None
        self.guards = None
        self.db_path = self.state_dir / "state.db"


class FakeEncoder:
    def __init__(self):
        self.calls = []

    def encode(self, plan, resume_from=None, progress_cb=None):
        assert resume_from is None
        self.calls.append(dict(plan))
        if progress_cb:
            progress_cb({"percent": 50.0, "speed": 2.0, "eta_seconds": 10, "out_time_seconds": 5})
        out = Path(plan["working_output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake encoded output" * 100)
        return SimpleNamespace(output=out)


def fake_verifier(plan, output):
    from mediaflow.util import sha256_file
    return SimpleNamespace(ok=True, failures=[], quality_floor_breach=False,
                           sha256=sha256_file(output), output_size=output.stat().st_size,
                           output_video_bytes=100, output_duration=60.0)


@pytest.fixture
def env(tmp_path):
    cfg = DummyConfig(tmp_path / "root")
    for p in (cfg.movies, cfg.inbox, cfg.convert / "movies" / "test-preset", cfg.work, cfg.review,
              cfg.failed, cfg.trash, cfg.state_dir / "logs"):
        p.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "state.db")
    store.initialize()
    presets = {
        "test-preset": {
            "id": "test-preset", "container": "mp4", "meta": {"enabled": True, "experimental": False},
            "video": {"action": "encode", "codec": "hevc", "backend_order": ["qsv"], "allow_software": False,
                      "quality": 24, "look_ahead": True, "max_height": 1080, "max_width": 1920,
                      "ten_bit_source": "keep", "tag": "hvc1", "hdr": "review", "interlaced": "review"},
            "audio": {}, "subtitles": {}, "skip": {"min_savings_pct": 15, "already_efficient": False},
            "schedule": {"window": ""},
        },
        "other-preset": {
            "id": "other-preset", "container": "mkv", "meta": {"enabled": True},
            "video": {"action": "remux", "codec": "hevc", "backend_order": [], "allow_software": False,
                      "quality": 24, "max_height": 1080, "max_width": 1920, "hdr": "keep",
                      "interlaced": "review"},
            "audio": {}, "subtitles": {}, "skip": {}, "schedule": {"window": ""},
        },
    }
    encoder = FakeEncoder()
    class AlwaysOk:
        def start_gate(self, **kwargs):
            return SimpleNamespace(ok=True, reasons=(), messages=())
        def check_space(self, **kwargs):
            return None, SimpleNamespace(ok=True, reasons=(), messages=())
    eng = Engine(cfg, store, presets, encoder=encoder, verifier=fake_verifier, guards=AlwaysOk())
    yield cfg, store, eng, encoder
    store.close()


def _source(cfg, name="Film (2001)", preset="test-preset", content=b"reel-data" * 10000):
    entry = cfg.convert / "movies" / preset / name
    entry.mkdir(parents=True)
    (entry / f"{name}.mkv").write_bytes(content)
    return entry


def _fake_inspect(engine, *, main=None, review=None):
    def fn(job):
        entry = Path(job.get("original_path") or job.get("source_path"))
        if job.get("parked_path"):
            entry = Path(job["parked_path"])
        if review:
            raise ReviewNeeded(review, {"test": True})
        main = next(entry.glob("*.mkv"), entry if entry.is_file() else None)
        assert main
        probe = SimpleNamespace(duration=60.0, size=main.stat().st_size, video_bytes=main.stat().st_size,
                                raw={"format": {"format_name": "matroska"}, "streams": []},
                                main_video={"height": 1080, "width": 1920})
        return SimpleNamespace(entry=entry, kind="dir", main=main, probe=probe,
                               source_size=main.stat().st_size, video_candidates=[], warnings=[])
    engine._inspect_source = fn


def _fake_plan(engine, *, action="encode", counter=None):
    def fn(job, inspection, preset, identity):
        if counter is not None:
            counter.append(1)
        ext = "mp4" if preset.get("container") == "mp4" else "mkv"
        plan = {
            "schema": 1, "preset": job["preset"], "action": action, "container": preset["container"],
            "plan_version": 1, "fallback_from": None,
            "source": {"path": str(inspection.main), "format": "matroska,webm", "duration": 60.0,
                       "size_bytes": inspection.source_size, "video_bytes": inspection.source_size,
                       "main_video_index": 0, "mtime": time.time()},
            "video": {"index": 0, "codec": "hevc", "width": 1920, "height": 1080,
                      "target_width": 1920, "target_height": 1080, "pix_fmt": "yuv420p",
                      "bit_depth": 8, "sar": "1:1", "frame_rate": "24/1", "hdr": False,
                      "backend": "qsv", "encoder": "hevc_qsv", "quality": 24, "tag": "hvc1"},
            "audio": [], "subtitles": [], "sidecars": [], "chapters": True, "attachments": False,
            "name": identity, "ext": ext,
            "destination_rel": f"{identity['title']} ({identity['year']})/{identity['title']} ({identity['year']}).{ext}",
            "dry_run": False, "warnings": [], "dropped": [], "estimated_seconds": 30.0,
            "working_output": str(engine.work / job["id"] / f"out.{ext}"),
        }
        return plan
    engine._build_plan = fn


def _auto_identity(engine):
    engine._resolve_identity = lambda job, plan: {
        "status": "auto", "title": "Film", "year": 2001, "tmdbid": None, "candidates": []}


def _prepare(engine):
    _fake_inspect(engine)
    _fake_plan(engine)
    _auto_identity(engine)


def test_full_path_publishes_and_retires(env):
    """[SAF-4] Happy path: publish atomically, retire original to trash with manifest."""
    cfg, store, eng, encoder = env
    entry = _source(cfg)
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "DONE", store.events(job["id"])
    lib = cfg.movies / "Film (2001)" / "Film (2001).mp4"
    assert lib.exists()
    marker = cfg.movies / "Film (2001)" / f".mediaflow-{job['id']}"
    data = json.loads(marker.read_text())
    assert data["job_id"] == job["id"] and data["output_sha256"] == job["output_sha256"]
    assert not entry.exists()
    trash = cfg.trash / time.strftime("%Y-%m-%d") / job["id"]
    manifest = json.loads((trash / "manifest.json").read_text())
    assert manifest["output_sha256"] == job["output_sha256"]
    assert (trash / "Film (2001).mkv").exists()
    assert not (cfg.work / job["id"]).exists()
    assert len(encoder.calls) == 1


def test_dry_run_moves_nothing_and_replans_once(env):
    """[SAF-11] Dry-run leaves PLANNED, moves nothing, and re-plans once on flip."""
    cfg, store, eng, encoder = env
    cfg.dry_run = True
    eng.dry_run = True
    entry = _source(cfg)
    counts = []
    _fake_inspect(eng)
    _fake_plan(eng, counter=counts)
    _auto_identity(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "PLANNED" and job["dry_run"] is True
    assert entry.exists()
    assert not (cfg.movies / "Film (2001)").exists()
    assert encoder.calls == []
    assert len(counts) == 1
    eng.tick()
    assert len(counts) == 1  # not re-planned each poll
    eng.dry_run = False
    eng.tick()
    job = store.get_job(job["id"])
    assert len(counts) == 2
    assert job["state"] == "DONE"


def test_review_parks_original_and_reject_leaves_it(env):
    """[REV-1] A review parks the source; reject closes without deleting it."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _fake_inspect(eng, review="hdr")
    _fake_plan(eng)
    _auto_identity(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "REVIEW" and job["review_reason"] == "hdr"
    parked = Path(job["parked_path"])
    assert parked.exists() and not entry.exists()
    eng.review_reject(job["id"])
    assert store.get_job(job["id"])["state"] == "FAILED"
    assert parked.exists()


def test_review_park_window_is_reconciled_after_crash(env):
    """[SAF-6]/[REV-1] A park rename before its DB update is adopted on recovery."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _fake_inspect(eng, review="hdr")
    _fake_plan(eng)
    _auto_identity(eng)
    eng.tick()
    job = store.list_jobs()[0]
    parked = Path(job["parked_path"])
    # Simulate SIGKILL after review/<id>/ rename but before parked_path update.
    store.update_job(job["id"], parked_path=None, source_path=str(entry))
    eng.recover()
    recovered = store.get_job(job["id"])
    assert recovered["state"] == "REVIEW"
    assert Path(recovered["parked_path"]) == parked
    assert parked.exists() and not entry.exists()


def test_review_unpark_window_is_reconciled_after_crash(env):
    """[SAF-6]/[REV-1] A review_move rename before its DB update is adopted."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _fake_inspect(eng, review="hdr")
    _fake_plan(eng)
    _auto_identity(eng)
    eng.tick()
    job = store.list_jobs()[0]
    target = cfg.convert / "movies" / "other-preset" / entry.name
    target.parent.mkdir(parents=True, exist_ok=True)
    eng._set_setting_json(f"unpark:{job['id']}", {"target": str(target), "preset": "other-preset"})
    Path(job["parked_path"]).rename(target)  # simulate the rename, then SIGKILL
    eng.recover()
    recovered = store.get_job(job["id"])
    assert recovered["state"] == "REVIEW"
    assert recovered["parked_path"] is None
    assert recovered["preset"] == "other-preset"
    assert Path(recovered["source_path"]) == target
    assert target.exists()


def test_review_move_restores_source_and_replans(env):
    """[REV-1] Moving a review to an MKV preset restores the source under that preset."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _fake_inspect(eng, review="hdr")
    _fake_plan(eng)
    _auto_identity(eng)
    eng.tick()
    job = store.list_jobs()[0]
    eng.review_move(job["id"], "other-preset")
    job = store.get_job(job["id"])
    assert job["state"] == "PLANNED"
    assert job["preset"] == "other-preset"
    target = cfg.convert / "movies" / "other-preset" / entry.name
    assert target.exists()
    assert not (cfg.review / job["id"] / entry.name).exists()


def test_duplicate_then_replace_moves_old_copy_to_trash(env):
    """[SAF-4] Existing destination -> REVIEW duplicate; --replace retires it first."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    existing = cfg.movies / "Film (2001)" / "Film (2001).mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old library copy")
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "REVIEW" and job["review_reason"] == "duplicate"
    assert entry.exists() is False  # parked with the review
    assert existing.read_bytes() == b"old library copy"
    eng.review_accept(job["id"], replace=True)
    job = store.get_job(job["id"])
    eng.tick()
    job = store.get_job(job["id"])
    assert job["state"] == "DONE", store.events(job["id"])
    new_existing = cfg.movies / "Film (2001)" / "Film (2001).mp4"
    assert new_existing.exists()
    assert new_existing.read_bytes() != b"old library copy"
    replaced = list(cfg.trash.glob(f"*/{job['id']}-replaced"))
    assert replaced and (replaced[0] / "manifest.json").exists()
    assert (replaced[0] / "Film (2001).mp4").read_bytes() == b"old library copy"


def test_size_fallback_replans_remux_and_reuses_audio(env):
    """[SIZE-2] Not-smaller encode re-plans as a remux at plan_version 2."""
    cfg, store, eng, encoder = env
    _source(cfg)
    _fake_inspect(eng)
    planned = []

    def plan_fn(job, inspection, preset, identity):
        plan = {
            "schema": 1, "preset": job["preset"], "action": "encode", "container": "mp4",
            "plan_version": 1, "fallback_from": None,
            "source": {"path": str(inspection.main), "duration": 60.0, "size_bytes": 1000,
                       "video_bytes": 1},  # conservative source video size
            "video": {"index": 0, "codec": "hevc", "width": 1920, "height": 1080,
                      "target_width": 1920, "target_height": 1080, "pix_fmt": "yuv420p",
                      "backend": "qsv", "encoder": "hevc_qsv", "quality": 24, "tag": "hvc1"},
            "audio": [{"index": 1, "source_codec": "dts", "language": "eng", "channels": 6,
                       "action": "transcode", "codec": "eac3", "bitrate": 448, "default": True}],
            "subtitles": [], "sidecars": [], "chapters": True, "attachments": False,
            "name": identity, "ext": "mp4",
            "destination_rel": f"{identity['title']} ({identity['year']})/{identity['title']} ({identity['year']}).mp4",
            "warnings": [], "dropped": [], "estimated_seconds": 30.0,
            "working_output": str(eng.work / job["id"] / "out.mp4"),
        }
        planned.append(plan["action"])
        return plan

    eng._build_plan = plan_fn
    _auto_identity(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "DONE", store.events(job["id"])
    assert planned == ["encode"]  # the fallback reuses the existing plan
    assert len(encoder.calls) == 2
    assert encoder.calls[1]["action"] == "remux"
    assert job["plan_version"] == 2
    assert job["plan"]["action"] == "remux"
    assert job["plan"]["fallback_from"] == "encode"
    assert job["plan"]["audio"][0]["codec"] == "eac3"


def test_restart_limit_preserves_original(env):
    """[SAF-12] Repeated encoder failures end FAILED with the original intact in failed/."""
    cfg, store, eng, _ = env
    cfg.max_restarts = 2
    _source(cfg)
    _prepare(eng)

    class Broken:
        def encode(self, plan, resume_from=None, progress_cb=None):
            raise RuntimeError("simulated encoder crash")

    eng.encoder = Broken()
    for _ in range(5):
        eng.tick()
        if store.list_jobs()[0]["state"] == "FAILED":
            break
    job = store.list_jobs()[0]
    assert job["state"] == "FAILED"
    assert job["restarts"] >= 2
    failed_src = Path(job["source_path"])
    assert failed_src.exists()
    assert str(failed_src).startswith(str(cfg.failed))


def test_nonzero_encoder_result_obeys_restart_limit(env):
    """[SAF-12] A returned ffmpeg failure is retried before the job FAILs."""
    cfg, store, eng, _ = env
    cfg.max_restarts = 2
    _source(cfg)
    _prepare(eng)

    class Failing:
        def encode(self, plan, resume_from=None, progress_cb=None):
            return SimpleNamespace(ok=False, returncode=1, stderr_text="synthetic failure")

    eng.encoder = Failing()
    for _ in range(6):
        eng.tick()
        if store.list_jobs()[0]["state"] == "FAILED":
            break
    job = store.list_jobs()[0]
    assert job["state"] == "FAILED"
    assert job["restarts"] >= 2
    failed_src = Path(job["source_path"])
    assert failed_src.exists()
    assert str(failed_src).startswith(str(cfg.failed))


def test_cancel_then_retry_restores_source(env):
    """[SAF-12]/review commands: cancel preserves the original; retry resets from PROBED."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _prepare(eng)
    # Hold the job at NAMED by blocking free space, then cancel.
    eng._space_gate = lambda job: SimpleNamespace(ok=False, reasons=["space-low"], messages=())
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "NAMED"
    eng.cancel(job["id"])
    job = store.get_job(job["id"])
    assert job["state"] == "FAILED"
    assert "cancelled" in (job.get("error") or "")
    assert Path(job["source_path"]).exists()
    eng.retry(job["id"])
    job = store.get_job(job["id"])
    assert job["state"] in {"PROBED", "PLANNED", "ENCODING", "DONE"}
    assert job["restarts"] == 0


def test_empty_trash_directory_is_not_adopted_as_retired(env):
    """[SAF-6] A crash between mkdir(trash/job) and the file rename is completed."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "DONE"
    original_name = entry.name
    source_file = Path(job["trash_path"]) / f"{original_name}.mkv"
    assert source_file.exists()
    # Put the original back and replace the trash job directory with the
    # empty directory that the interrupted file-entry retire would leave.
    entry.mkdir(parents=True)
    source_file.rename(entry / f"{original_name}.mkv")
    for child in Path(job["trash_path"]).iterdir():
        child.unlink()
    Path(job["trash_path"]).rmdir()
    Path(job["trash_path"]).mkdir()
    store.update_job(job["id"], state="PUBLISHED", trash_path=None, manifest_path=None,
                     source_path=str(entry), finished_at=None)
    eng.tick()
    job = store.get_job(job["id"])
    assert job["state"] == "DONE", store.events(job["id"])
    assert (Path(job["trash_path"]) / f"{original_name}.mkv").read_bytes() == b"reel-data" * 10000
    assert (Path(job["trash_path"]) / "manifest.json").exists()


def test_replace_intent_manifest_is_repaired_after_crash(env, monkeypatch):
    """[SAF-6]/[SAF-4] A crash after the replace rename still leaves a manifest."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    existing = cfg.movies / "Film (2001)" / "Film (2001).mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old library copy")
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "REVIEW" and job["review_reason"] == "duplicate"

    import mediaflow.engine as engine_module
    original = engine_module.atomic_write_json
    calls = {"n": 0}

    def fail_manifest_once(path, payload):
        if Path(path).name == "manifest.json" and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated power loss after rename")
        return original(path, payload)

    monkeypatch.setattr(engine_module, "atomic_write_json", fail_manifest_once)
    with pytest.raises(OSError):
        eng.review_accept(job["id"], replace=True)
    replaced = list(cfg.trash.glob(f"*/{job['id']}-replaced"))
    assert replaced and not (replaced[0] / "manifest.json").exists()
    eng.recover()
    assert (replaced[0] / "manifest.json").exists()
    job = store.get_job(job["id"])
    assert job["state"] in {"NAMED", "PUBLISHED", "DONE"}


def test_fail_job_without_plan_does_not_crash(env):
    """[SAF-6] A source that vanishes before planning still reaches FAILED."""
    cfg, store, eng, _ = env
    missing = cfg.convert / "movies" / "test-preset" / "Gone (2001)"
    job = store.create_job(preset="test-preset", source_rel=missing.name,
                           source_path=str(missing), original_path=str(missing),
                           entry_kind="dir")
    eng._fail_job(job["id"], "source-vanished")
    assert store.get_job(job["id"])["state"] == "FAILED"


def test_restore_round_trip_and_checksum_refusal(env):
    """[SAF-5]/restore: output hash verified, published copy goes to trash, original to inbox."""
    cfg, store, eng, _ = env
    _source(cfg)
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    output = Path(job["output_path"])
    output.write_bytes(b"tampered")
    with pytest.raises(CommandError):
        eng.restore(job["id"])
    # restore the correct bytes by rebuilding? A job cannot be restored after tamper.
    # Use a second job for the successful path.
    cfg2 = DummyConfig(cfg.root / "second")
    for p in (cfg2.movies, cfg2.inbox, cfg2.convert / "movies" / "test-preset", cfg2.work,
              cfg2.review, cfg2.failed, cfg2.trash, cfg2.state_dir / "logs"):
        p.mkdir(parents=True, exist_ok=True)
    store2 = Store(cfg2.state_dir / "state.db"); store2.initialize()
    class AlwaysOk:
        def start_gate(self, **k): return SimpleNamespace(ok=True, reasons=(), messages=())
        def check_space(self, **k): return None, SimpleNamespace(ok=True, reasons=(), messages=())
    enc2 = FakeEncoder()
    eng2 = Engine(cfg2, store2, eng.presets, encoder=enc2, verifier=fake_verifier, guards=AlwaysOk())
    _source(cfg2)
    _prepare(eng2)
    eng2.tick()
    job2 = store2.list_jobs()[0]
    out2 = Path(job2["output_path"])
    assert out2.exists()
    eng2.restore(job2["id"])
    job2 = store2.get_job(job2["id"])
    assert job2["state"] == "RESTORED"
    assert not out2.exists()
    assert any(cfg2.inbox.iterdir())
    assert list(cfg2.trash.glob(f"*/{job2['id']}-published"))
    store2.close()


def test_restore_purged_returns_exit_code_3(env):
    cfg, store, eng, _ = env
    _source(cfg)
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    for p in cfg.trash.rglob(f"{job['id']}/*"):
        p.unlink()
    from mediaflow.util import is_real_dir
    for d in sorted(cfg.trash.rglob(f"{job['id']}"), reverse=True):
        if d.is_dir():
            d.rmdir()
    with pytest.raises(RestorePurged) as exc:
        eng.restore(job["id"])
    assert exc.value.exit_code == 3


def test_recovery_adopts_publish_window(env):
    """[SAF-6] A killed publish with matching marker is adopted, never duplicate/re-publish."""
    cfg, store, eng, encoder = env
    entry = _source(cfg)
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    # Put the job back in NAMED as if the DB commit after rename was lost.
    store.update_job(job["id"], state="NAMED", output_path=job["output_path"], adopted=False)
    calls = len(encoder.calls)
    eng.recover()
    job = store.get_job(job["id"])
    assert job["state"] in {"PUBLISHED", "RETIRED"}  # adoption; tick will finish
    eng.tick()
    assert store.get_job(job["id"])["state"] == "DONE"
    assert len(encoder.calls) == calls
    assert len(list(cfg.movies.iterdir())) == 1


def test_recovery_adopts_retire_window(env):
    """[SAF-6] A crash after the trash move but before RETIRED adopts the manifest."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    _prepare(eng)
    eng.tick()
    job = store.list_jobs()[0]
    # simulate DB still PUBLISHED while trash already contains the job
    store.transition(job["id"], "PUBLISHED", expect_from="DONE")
    store.update_job(job["id"], manifest_path=None)
    # DONE work was cleaned; create a fake working output for recovery inspection
    work = cfg.work / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "out.mp4").write_bytes(b"fake encoded output" * 100)
    eng.recover()
    job = store.get_job(job["id"])
    assert job["state"] == "RETIRED"
    assert Path(job["manifest_path"]).exists()
    eng.tick()
    assert store.get_job(job["id"])["state"] == "DONE"


def test_snapshot_does_not_override_db(env):
    """[SAF-6] DB wins when job.json disagrees; recovery rewrites the snapshot."""
    cfg, store, eng, _ = env
    entry = _source(cfg)
    eng.tick()  # creates DETECTED, may be stable
    job = store.list_jobs()[0]
    work = cfg.work / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "job.json").write_text(json.dumps({"state": "PUBLISHED", "id": job["id"]}))
    store.update_job(job["id"], state="PROBED")
    eng.recover()
    snap = json.loads((work / "job.json").read_text())
    assert snap["state"] == "PROBED"


def test_publish_free_space_gate(env):
    """[SAF-8] A low-space guard leaves NAMED and publishes nothing."""
    cfg, store, eng, _ = env
    _source(cfg)
    _prepare(eng)

    def block_space(*a, **k):
        return SimpleNamespace(ok=False, reasons=("space-low",), messages=("low",))

    eng._space_gate = block_space
    eng.tick()
    job = store.list_jobs()[0]
    assert job["state"] == "NAMED"
    assert not cfg.movies.exists() or not any(cfg.movies.iterdir())


def test_stall_detector_restarts_then_fails(env):
    """[SAF-12] A live but stalled encoder is cancelled and counted as a restart."""
    cfg, store, eng, _ = env
    cfg.stall_minutes = 0.01
    cfg.max_restarts = 0
    _source(cfg)
    _prepare(eng)

    class Hung:
        def encode(self, plan, resume_from=None, progress_cb=None, cancel_event=None,
                   pause_event=None):
            if progress_cb:
                progress_cb({"percent": 1.0, "out_time_seconds": 1})
            for _ in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    return SimpleNamespace(ok=False, returncode=143)
                time.sleep(0.02)
            return SimpleNamespace(ok=False, returncode=1)

    eng.encoder = Hung()
    started = time.monotonic()
    eng.tick()
    assert time.monotonic() - started < 10
    job = store.list_jobs()[0]
    assert job["state"] == "FAILED"
    assert "stall" in (job.get("error") or "")
    assert any(e["kind"] == "stalled" for e in store.events(job["id"]))
    assert Path(job["source_path"]).exists()


def test_ac_loss_pauses_and_resumes_encoder(env):
    """[SAF-9] Mid-encode AC loss pauses the encoder and resumes on AC."""
    cfg, store, eng, _ = env
    _source(cfg)
    _prepare(eng)
    calls = {"mid": 0}
    seen = {"paused": False}

    class FlakyPower:
        def start_gate(self, **kw):
            return SimpleNamespace(ok=True, reasons=(), messages=())

        def check_space(self, **kw):
            return None, SimpleNamespace(ok=True, reasons=(), messages=())

        def mid_encode_gate(self, **kw):
            calls["mid"] += 1
            if calls["mid"] in {1, 2}:
                return SimpleNamespace(ok=False, reasons=("on-battery",), messages=())
            return SimpleNamespace(ok=True, reasons=(), messages=())

    class Pausable:
        def encode(self, plan, resume_from=None, progress_cb=None, cancel_event=None,
                   pause_event=None):
            for i in range(100):
                if pause_event is not None and pause_event.is_set():
                    seen["paused"] = True
                if cancel_event is not None and cancel_event.is_set():
                    return SimpleNamespace(ok=False, returncode=143)
                if progress_cb:
                    progress_cb({"percent": i, "out_time_seconds": i / 10})
                time.sleep(0.03)
            out = Path(plan["working_output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x" * 200000)
            return SimpleNamespace(ok=True, returncode=0)

    eng.guards = FlakyPower()
    eng.encoder = Pausable()
    eng.tick()
    job = store.list_jobs()[0]
    assert seen["paused"] is True
    kinds = {e["kind"] for e in store.events(job["id"])}
    assert "encode-paused" in kinds and "encode-resumed" in kinds
    assert job["state"] == "DONE"


def test_cancel_command_during_encode(env):
    """[SAF-12]/CLI: a cancel command is serviced while the encoder is running."""
    cfg, store, eng, _ = env
    _source(cfg)
    _prepare(eng)

    class Long:
        def encode(self, plan, resume_from=None, progress_cb=None, cancel_event=None,
                   pause_event=None):
            for i in range(500):
                if cancel_event is not None and cancel_event.is_set():
                    return SimpleNamespace(ok=False, returncode=143)
                if progress_cb:
                    progress_cb({"percent": i / 5, "out_time_seconds": i / 10})
                time.sleep(0.02)
            return SimpleNamespace(ok=False, returncode=1)

    eng.encoder = Long()
    import threading
    t = threading.Thread(target=eng.tick)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        jobs = store.list_jobs()
        if jobs and jobs[0]["state"] == "ENCODING":
            break
        time.sleep(0.02)
    store.enqueue_command("cancel", {"job_id": jobs[0]["id"]})
    t.join(10)
    assert not t.is_alive()
    job = store.get_job(jobs[0]["id"])
    assert job["state"] == "FAILED"
    assert "cancelled" in (job.get("error") or "")
    assert all(c["status"] != "pending" for c in store.pending_commands())
