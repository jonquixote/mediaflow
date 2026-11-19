"""mediaflow command line interface (phone-friendly)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Mapping

from .config import Config, ConfigError, load_config, load_secrets
from .daemon import AlreadyRunning, Daemon, daemon_lock
from .db import DatabaseError, Store
from .doctor import format_doctor, run_doctor
from .engine import CommandError, Engine, EngineError, RestorePurged, ReviewError
from .util import human_bytes, short_age

DEFAULT_CONFIG = Path("/etc/mediaflow/config.toml")
MAX_SHORT_LINES = 12
MAX_SHORT_COLS = 60


class CliError(RuntimeError):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _short(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        if len(raw) <= MAX_SHORT_COLS:
            lines.append(raw)
        else:
            lines.extend(textwrap.wrap(raw, MAX_SHORT_COLS, break_long_words=True))
        if len(lines) >= MAX_SHORT_LINES:
            break
    if len(lines) > MAX_SHORT_LINES:
        lines = lines[:MAX_SHORT_LINES]
    return "\n".join(lines)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _config_path(args) -> Path:
    return Path(args.config or os.environ.get("MEDIAFLOW_CONFIG", DEFAULT_CONFIG))


def _presets_dir(config_path: Path) -> Path:
    return Path(os.environ.get("MEDIAFLOW_PRESETS", config_path.parent / "presets.d"))


def _load(args) -> tuple[Config, Any, dict]:
    path = _config_path(args)
    cfg = load_config(path, environ=os.environ)
    from .presets import load_presets
    warnings: list[str] = []
    presets = load_presets(_presets_dir(path), include_experimental=False, warnings=warnings)
    env_dir = Path(os.environ.get("MEDIAFLOW_ENV_DIR", path.parent))
    secrets = load_secrets(env_dir)
    return cfg, secrets, presets


def _open_store(cfg: Config) -> Store:
    store = Store(cfg.db_path)
    store.initialize()
    return store


def _make_engine(cfg: Config, secrets: Any, presets: dict, store: Store) -> Engine:
    engine = Engine(cfg, store, presets)
    engine.secrets = secrets
    try:
        from .notify import Notifier
        engine.notifier = Notifier(cfg, secrets, store)
    except Exception:
        engine.notifier = None
    return engine


def _status_payload(cfg: Config, store: Store | None = None) -> dict:
    status_file = cfg.status_path
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            return data
        except Exception:
            pass
    counts: dict[str, int] = {}
    oldest = None
    jobs = store.list_jobs(include_done=False) if store else []
    for job in jobs:
        counts[job["state"]] = counts.get(job["state"], 0) + 1
        if job["state"] == "REVIEW":
            oldest = max(oldest or 0, time.time() - float(job.get("updated_at") or 0))
    free = None
    try:
        free = shutil.disk_usage(cfg.root).free
    except OSError:
        pass
    return {"counts": counts, "queue": sum(counts.values()), "reviews": counts.get("REVIEW", 0),
            "oldest_review_seconds": oldest, "paused_reasons": [], "free_bytes": free,
            "trash_bytes": 0, "last_success": None, "dry_run": cfg.dry_run,
            "updated_at": time.time()}


def cmd_status(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        data = _status_payload(cfg, store)
    finally:
        store.close()
    if args.json:
        print(_json(data))
        return 0
    lines = [
        f"state {'PAUSED' if data.get('paused_reasons') else 'running'}  dry-run {'on' if data.get('dry_run') else 'off'}",
        f"queue {data.get('queue', 0)}  reviews {data.get('reviews', 0)}",
    ]
    current = None
    try:
        progress = json.loads(cfg.progress_path.read_text(encoding="utf-8"))
        if progress.get("phase") not in (None, "idle"):
            current = progress
    except Exception:
        pass
    if current:
        pct = current.get("percent")
        lines.append(f"job {current.get('job_id')} {current.get('phase')} {pct if pct is not None else '?'}%")
        if current.get("eta_seconds") is not None:
            lines.append(f"eta {short_age(float(current['eta_seconds']))}")
    if data.get("oldest_review_seconds") is not None:
        lines.append(f"oldest review {short_age(float(data['oldest_review_seconds']))}")
    if data.get("paused_reasons"):
        lines.append("paused: " + ",".join(data["paused_reasons"])[:48])
    free = data.get("free_bytes")
    trash = data.get("trash_bytes")
    lines.append(f"free {human_bytes(free)}  trash {human_bytes(trash)}")
    if data.get("last_success"):
        lines.append(f"last success {short_age(time.time() - data['last_success'])} ago")
    out = "\n".join(lines)
    print(_short(out) if args.short else out)
    return 0


def cmd_queue(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        jobs = store.list_jobs(include_done=True)
        if args.state:
            jobs = [j for j in jobs if j["state"] == args.state.upper()]
        if args.json:
            print(_json([{k: j.get(k) for k in ("id", "state", "preset", "source_rel", "review_reason",
                                                 "created_at", "updated_at", "dry_run")} for j in jobs]))
            return 0
        if not jobs:
            print("queue empty")
            return 0
        lines = []
        for j in jobs[:40]:
            flag = f" [{j['review_reason']}]" if j.get("review_reason") else ""
            age = short_age(time.time() - float(j["created_at"]))
            lines.append(f"{j['state']:<9} {j['id'][-8:]} {j['preset']} {age}{flag}")
        out = "\n".join(lines)
        print(_short(out) if args.short else out)
        return 0
    finally:
        store.close()


def _plan_text(plan: Mapping) -> str:
    if plan.get("would_review"):
        return (f"would review: {plan['would_review'].get('reason')}\n"
                f"nothing will move in dry-run")
    src = plan.get("source") or {}
    v = plan.get("video") or {}
    name = plan.get("name") or {}
    lines = [
        f"action {plan.get('action')} -> {plan.get('ext')}",
        f"video {v.get('codec')} {v.get('width')}x{v.get('height')} -> "
        f"{v.get('target_width')}x{v.get('target_height')}",
        f"audio {len(plan.get('audio') or [])}  subs {len(plan.get('subtitles') or [])}",
        f"name {name.get('stem')}",
        f"source {human_bytes(src.get('size_bytes'))} {float(src.get('duration') or 0):.0f}s",
        f"estimate {short_age(float(plan['estimated_seconds'])) if plan.get('estimated_seconds') else 'unknown'}",
    ]
    if plan.get("warnings"):
        lines.append("warn " + "; ".join(plan["warnings"])[:80])
    if plan.get("dropped"):
        lines.append(f"dropped {len(plan['dropped'])} stream(s)")
    return "\n".join(lines)


def cmd_plan(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        engine = _make_engine(cfg, secrets, presets, store)
        path = Path(args.path)
        if args.refresh:
            job = engine._job_for_path(path)
            if job and job["state"] == "PLANNED":
                return _route(cfg, secrets, presets, "plan_refresh", {"job_id": job["id"]},
                              as_json=bool(args.json), short=bool(args.short))
        plan = engine.preview_plan(path, preset_id=args.preset, refresh=args.refresh)
        if args.json:
            print(_json(plan))
            return 0
        text = _plan_text(plan)
        print(_short(text) if args.short else text)
        return 0
    finally:
        store.close()


def _route(cfg: Config, secrets: Any, presets: dict, command: str, payload: Mapping,
           as_json: bool = False, short: bool = False) -> int:
    """Execute a mutation directly (holding the lock) or enqueue it for the daemon."""
    try:
        with daemon_lock(cfg, blocking=False):
            store = _open_store(cfg)
            try:
                engine = _make_engine(cfg, secrets, presets, store)
                engine.start()
                result = engine.execute_command(command, payload)
            finally:
                store.close()
    except AlreadyRunning:
        store = _open_store(cfg)
        try:
            cmd_id = store.enqueue_command(command, payload)
        finally:
            store.close()
        if as_json:
            print(_json({"queued": cmd_id, "command": command}))
        elif short:
            print(_short(f"queued {command} {cmd_id[-8:]}"))
        else:
            print(f"queued {cmd_id}; daemon will execute within one poll")
        return 0
    if result is not None:
        if as_json:
            print(_json(result))
        elif short:
            state = result.get("state") if isinstance(result, Mapping) else None
            job = result.get("id") if isinstance(result, Mapping) else ""
            print(_short(f"ok {command} {str(job)[-8:]} {state or ''}".strip()))
        else:
            print(result if isinstance(result, (str, int)) else _short(str(result)))
    return 0


def _mutate(args, command: str, payload: Mapping) -> int:
    cfg, secrets, presets = _load(args)
    try:
        return _route(cfg, secrets, presets, command, payload,
                      as_json=bool(getattr(args, "json", False)),
                      short=bool(getattr(args, "short", False)))
    except RestorePurged as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (CommandError, ReviewError, EngineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_review(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        if args.review_command == "list":
            reviews = store.list_jobs(("REVIEW",))
            rows = []
            for j in reviews:
                reason = j.get("review_reason") or "?"
                hint = _review_hint(reason)
                age = time.time() - float(j.get("updated_at") or j["created_at"])
                rows.append({"id": j["id"], "reason": reason, "age_seconds": age,
                             "preset": j["preset"], "clear": hint})
            if args.json:
                print(_json(rows))
                return 0
            if not rows:
                print("no reviews")
                return 0
            text = "\n".join(f"{r['id'][-8:]} {r['reason']} {short_age(r['age_seconds'])} {r['clear']}" for r in rows)
            print(_short(text) if args.short else text)
            return 0
        if args.review_command == "accept":
            payload = {"job_id": args.id, "candidate": args.candidate, "title": args.title,
                       "year": args.year, "main": args.main, "replace": args.replace,
                       "publish_anyway": args.publish_anyway}
            return _mutate(args, "review_accept", payload)
        if args.review_command == "move":
            return _mutate(args, "review_move", {"job_id": args.id, "preset": args.preset})
        if args.review_command == "reject":
            return _mutate(args, "review_reject", {"job_id": args.id})
        raise CliError("unknown review command")
    finally:
        store.close()


def _review_hint(reason: str) -> str:
    return {
        "multiple-videos": "review accept --main FILE",
        "unsupported-format": "review reject",
        "hdr": "review move --preset",
        "10bit": "review move --preset",
        "10bit-no-hw": "review move --preset",
        "interlaced": "review move --preset",
        "remux-incompatible-video": "review move --preset",
        "quality-floor": "review accept --publish-anyway",
        "identity-low-confidence": "review accept --candidate N",
        "identity-tie": "review accept --candidate N",
        "duplicate": "review accept --replace",
        "multi-disc": "review reject",
    }.get(reason, "review list")


def cmd_mutate_simple(args, command: str) -> int:
    payload = {}
    for key in ("id", "job_id"):
        if hasattr(args, key) and getattr(args, key) is not None:
            payload["job_id"] = getattr(args, key)
    if command == "pause":
        payload["now"] = bool(args.now)
    if command == "trash_purge":
        payload["older_than_days"] = _parse_days(args.older_than) if args.older_than else None
    return _mutate(args, command, payload)


def _parse_days(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        return int(text)
    except ValueError:
        raise CliError(f"invalid day count: {value}")


def cmd_trash(args) -> int:
    cfg, secrets, presets = _load(args)
    if args.trash_command == "purge":
        days = _parse_days(args.older_than) if args.older_than else None
        return _mutate(args, "trash_purge", {"older_than_days": days})
    entries = []
    if cfg.trash.exists():
        for day in sorted(cfg.trash.iterdir(), reverse=True):
            if not day.is_dir() or day.is_symlink():
                continue
            for job in day.iterdir():
                if job.is_symlink():
                    continue
                size = sum(p.stat().st_size for p in job.rglob("*") if p.is_file() and not p.is_symlink())
                entries.append({"date": day.name, "job_id": job.name,
                                "size": size, "age_seconds": time.time() - job.stat().st_mtime})
    if args.json:
        print(_json(entries))
        return 0
    if not entries:
        print("trash empty")
        return 0
    text = "\n".join(f"{e['date']} {e['job_id'][:8]} {human_bytes(e['size'])}" for e in entries[:30])
    print(_short(text) if args.short else text)
    return 0


def cmd_run(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        try:
            from .notify import Notifier
            notifier = Notifier(cfg, secrets, store)
        except Exception:
            notifier = None
        daemon = Daemon(cfg, store, presets, config_path=_config_path(args),
                        presets_dir=_presets_dir(_config_path(args)),
                        notifier=notifier)
        return daemon.run(once=args.once, force_job=args.force)
    finally:
        store.close()


def cmd_doctor(args) -> int:
    cfg, secrets, presets = _load(args)
    report = run_doctor(cfg, config_path=_config_path(args),
                        presets_dir=_presets_dir(_config_path(args)), secrets=secrets)
    if args.json:
        print(_json(report.as_dict()))
    else:
        print(format_doctor(report, short=args.short))
    return 0 if report.ok else 1


def cmd_calibrate(args) -> int:
    cfg, secrets, presets = _load(args)
    from .calibrate import calibrate, format_calibration
    store = _open_store(cfg)
    try:
        preset_id = args.preset or ("1080p-balanced-mp4" if "1080p-balanced-mp4" in presets else next(iter(presets), None))
        preset = presets.get(preset_id) if preset_id else None
        if preset is None:
            print("no preset available for calibration", file=sys.stderr)
            return 2
        try:
            result = calibrate(Path(args.file), cfg, seconds=args.seconds, preset=preset, store=store)
        except Exception as exc:
            print(f"calibration failed: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(_json(result))
        else:
            print(format_calibration(result, short=args.short))
        return 0
    finally:
        store.close()


def cmd_estimate(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        engine = _make_engine(cfg, secrets, presets, store)
        info = engine.estimate_path(Path(args.path), preset_id=args.preset)
        if args.json:
            print(_json(info))
            return 0
        if info["estimate_seconds"] is not None:
            text = (f"estimate {short_age(float(info['estimate_seconds']))} "
                    f"({info['backend']}, {info['height']}p, factor {info['speed_factor']:.2f}x)")
        else:
            text = "estimate unknown: cold start for this preset/backend/resolution"
        print(_short(text) if args.short else text)
        return 0
    finally:
        store.close()


def cmd_library(args) -> int:
    cfg, secrets, presets = _load(args)
    from .library import format_report, library_report
    store = _open_store(cfg)
    try:
        report = library_report(cfg.movies, store, ffprobe=cfg.ffprobe)
        if args.json:
            print(_json(report))
        else:
            print(format_report(report, short=args.short))
        return 0
    finally:
        store.close()


def cmd_maintenance(args) -> int:
    cfg, secrets, presets = _load(args)
    store = _open_store(cfg)
    try:
        backup = store.backup(cfg.backup_dir, keep=3)
        daemon_up = False
        try:
            with daemon_lock(cfg, blocking=False):
                engine = _make_engine(cfg, secrets, presets, store)
                engine.start()
                purged = engine.trash_purge()
                swept = engine.failed_sweep()
        except AlreadyRunning:
            daemon_up = True
            store.enqueue_command("trash_purge", {"older_than_days": None})
            store.enqueue_command("failed_sweep", {"older_than_days": None})
            purged = swept = None
    finally:
        store.close()
    if args.json:
        print(_json({"backup": str(backup), "purged": purged, "swept": swept,
                     "daemon": "running" if daemon_up else "stopped"}))
    else:
        print(f"backup {backup.name}")
        print("maintenance queued for daemon" if daemon_up else
              f"trash purged {purged}; failed swept {swept}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--short", action="store_true", default=argparse.SUPPRESS,
                        help="phone-width output (12 lines x 60 cols)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable JSON")
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                        help="config.toml path")

    p = argparse.ArgumentParser(prog="mediaflow", description="Media optimisation and organisation funnel")
    p.add_argument("--short", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--config", type=Path, default=None, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", parents=[common]); s.set_defaults(func=cmd_status)
    s = sub.add_parser("queue", parents=[common]); s.add_argument("--state", default=None); s.set_defaults(func=cmd_queue)
    s = sub.add_parser("plan", parents=[common]); s.add_argument("path"); s.add_argument("--refresh", action="store_true"); s.add_argument("--preset"); s.set_defaults(func=cmd_plan)
    s = sub.add_parser("run", parents=[common]); s.add_argument("--once", action="store_true"); s.add_argument("--force", default=None); s.set_defaults(func=cmd_run)
    s = sub.add_parser("pause", parents=[common]); s.add_argument("--now", action="store_true"); s.set_defaults(func=lambda a: cmd_mutate_simple(a, "pause"))
    s = sub.add_parser("resume", parents=[common]); s.set_defaults(func=lambda a: cmd_mutate_simple(a, "resume"))
    s = sub.add_parser("cancel", parents=[common]); s.add_argument("id"); s.set_defaults(func=lambda a: cmd_mutate_simple(a, "cancel"))
    s = sub.add_parser("retry", parents=[common]); s.add_argument("id"); s.set_defaults(func=lambda a: cmd_mutate_simple(a, "retry"))
    s = sub.add_parser("restore", parents=[common]); s.add_argument("id"); s.set_defaults(func=lambda a: cmd_mutate_simple(a, "restore"))
    s = sub.add_parser("calibrate", parents=[common]); s.add_argument("file"); s.add_argument("--seconds", type=int, default=90); s.add_argument("--preset"); s.set_defaults(func=cmd_calibrate)
    s = sub.add_parser("estimate", parents=[common]); s.add_argument("path"); s.add_argument("--preset"); s.set_defaults(func=cmd_estimate)
    s = sub.add_parser("doctor", parents=[common]); s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("maintenance", parents=[common]); s.set_defaults(func=cmd_maintenance)

    review = sub.add_parser("review", parents=[common])
    rsub = review.add_subparsers(dest="review_command", required=True)
    r = rsub.add_parser("list", parents=[common]); r.set_defaults(func=cmd_review)
    r = rsub.add_parser("accept", parents=[common])
    r.add_argument("id"); r.add_argument("--candidate", type=int); r.add_argument("--title")
    r.add_argument("--year", type=int); r.add_argument("--main"); r.add_argument("--replace", action="store_true")
    r.add_argument("--publish-anyway", dest="publish_anyway", action="store_true")
    r.set_defaults(func=cmd_review)
    r = rsub.add_parser("move", parents=[common]); r.add_argument("id"); r.add_argument("--preset", required=True); r.set_defaults(func=cmd_review)
    r = rsub.add_parser("reject", parents=[common]); r.add_argument("id"); r.set_defaults(func=cmd_review)

    library = sub.add_parser("library", parents=[common])
    lsub = library.add_subparsers(dest="library_command", required=True)
    l = lsub.add_parser("report", parents=[common]); l.set_defaults(func=cmd_library)

    trash = sub.add_parser("trash", parents=[common])
    tsub = trash.add_subparsers(dest="trash_command", required=True)
    t = tsub.add_parser("list", parents=[common]); t.set_defaults(func=cmd_trash)
    t = tsub.add_parser("purge", parents=[common]); t.add_argument("--older-than"); t.set_defaults(func=cmd_trash)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, default in (("short", False), ("json", False), ("config", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (ConfigError, DatabaseError) as exc:
        print(f"mediaflow: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
