#!/usr/bin/env python3
"""Child process used by crash tests. Not a pytest module.

It builds a sandbox with the fake encoder/verifier and kills itself at a chosen
state transition. The parent test restarts it and checks the final invariants.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediaflow.db import Store  # noqa: E402
from mediaflow.engine import Engine  # noqa: E402
from tests.test_engine import (  # noqa: E402
    DummyConfig, FakeEncoder, fake_verifier, _fake_inspect, _fake_plan,
)


class SlowEncoder(FakeEncoder):
    def encode(self, plan, resume_from=None, progress_cb=None, cancel_event=None,
               pause_event=None):
        if progress_cb:
            progress_cb({"percent": 25.0})
        time.sleep(0.08)
        return super().encode(plan, resume_from=resume_from, progress_cb=progress_cb)


class AlwaysOk:
    def start_gate(self, **kwargs):
        return SimpleNamespace(ok=True, reasons=(), messages=())

    def check_space(self, **kwargs):
        return None, SimpleNamespace(ok=True, reasons=(), messages=())


PRESETS = {
    "test-preset": {
        "id": "test-preset", "container": "mp4", "meta": {"enabled": True, "experimental": False},
        "video": {"action": "encode", "codec": "hevc", "backend_order": ["qsv"],
                  "allow_software": False, "quality": 24, "look_ahead": True,
                  "max_height": 1080, "max_width": 1920, "ten_bit_source": "keep",
                  "tag": "hvc1", "hdr": "review", "interlaced": "review"},
        "audio": {}, "subtitles": {}, "skip": {"min_savings_pct": 15,
                                              "already_efficient": False},
        "schedule": {"window": ""},
    }
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--kill-state")
    ap.add_argument("--kill-mode", choices=["before-commit", "after-commit", "after-snapshot"])
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--slow", action="store_true")
    args = ap.parse_args()
    cfg = DummyConfig(args.root)
    for p in (cfg.movies, cfg.inbox, cfg.convert / "movies" / "test-preset", cfg.work,
              cfg.review, cfg.failed, cfg.trash, cfg.state_dir / "logs"):
        p.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "state.db")
    store.initialize()
    jobs_existing = store.list_jobs(include_done=True)
    if not jobs_existing:
        for i in range(args.jobs):
            entry = cfg.convert / "movies" / "test-preset" / f"Crash Film {i + 1} (200{i + 1})"
            entry.mkdir(parents=True, exist_ok=True)
            (entry / f"{entry.name}.mkv").write_bytes(b"reel-data" * 10000)
    encoder = SlowEncoder() if args.slow else FakeEncoder()
    eng = Engine(cfg, store, PRESETS, encoder=encoder, verifier=fake_verifier, guards=AlwaysOk())
    _fake_inspect(eng)
    _fake_plan(eng)

    def identity(job, plan):
        name = Path(job.get("original_path") or job.get("source_path")).name
        parsed = name
        title = name.rsplit(" (", 1)[0]
        year = int(name.rsplit("(", 1)[1].rstrip(")").split(")")[0])
        return {"status": "auto", "title": title, "year": year, "tmdbid": None, "candidates": []}

    eng._resolve_identity = identity

    killed = False
    if args.kill_state:
        def kill():
            nonlocal killed
            if killed:
                return
            killed = True
            os.kill(os.getpid(), signal.SIGKILL)

        if args.kill_mode in {"before-commit", "after-commit"}:
            original = store.transition

            def transition(job_id, to_state, **kwargs):
                if to_state == args.kill_state and args.kill_mode == "before-commit":
                    kill()
                ok = original(job_id, to_state, **kwargs)
                if to_state == args.kill_state and args.kill_mode == "after-commit":
                    kill()
                return ok

            store.transition = transition
        else:
            original_snapshot = eng._write_snapshot

            def snapshot(job_id):
                original_snapshot(job_id)
                job = store.get_job(job_id)
                if job and job["state"] == args.kill_state:
                    kill()

            eng._write_snapshot = snapshot
    try:
        eng.start()
        if args.loop:
            for _ in range(200):
                eng.tick()
                time.sleep(0.01)
                if all(j["state"] in {"DONE", "FAILED"} for j in store.list_jobs()):
                    break
            else:
                return 9
        else:
            eng.tick()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
