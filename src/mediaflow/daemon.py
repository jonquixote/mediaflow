"""Daemon lifecycle, process locking and config reload."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .engine import Engine

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


@contextlib.contextmanager
def daemon_lock(config: Any, *, blocking: bool = False) -> Iterator[int]:
    """flock on /run/mediaflow/daemon.lock (RuntimeDirectory in production)."""
    runtime = Path(getattr(config, "runtime_dir", "/run/mediaflow"))
    runtime.mkdir(parents=True, exist_ok=True)
    path = Path(getattr(config, "lock_path", runtime / "daemon.lock"))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o664)
    try:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise AlreadyRunning(str(path)) from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def daemon_running(config: Any) -> bool:
    try:
        with daemon_lock(config, blocking=False):
            return False
    except AlreadyRunning:
        return True


class Daemon:
    def __init__(self, config: Any, store: Any, presets: dict, *, config_path: Path | None = None,
                 presets_dir: Path | None = None, encoder=None, verifier=None, guards=None,
                 notifier=None, identity_resolver=None, tick_seconds: float | None = None):
        self.config = config
        self.store = store
        self.presets = presets
        self.config_path = Path(config_path) if config_path else None
        self.presets_dir = Path(presets_dir) if presets_dir else None
        self.tick_seconds = float(tick_seconds if tick_seconds is not None else
                                  getattr(config, "poll_seconds", 30))
        self.engine = Engine(config, store, presets, encoder=encoder, verifier=verifier,
                             guards=guards, notifier=notifier,
                             identity_resolver=identity_resolver)
        self._stop = threading.Event()
        self._config_mtime = self._fingerprint()

    def run(self, *, once: bool = False, force_job: str | None = None) -> int:
        with daemon_lock(self.config, blocking=False):
            self._install_signals()
            self.engine.start()
            self._check_ffmpeg()
            if once:
                self.engine.tick(once=True, force_job=force_job)
                return 0
            log.info("mediaflow daemon started pid=%s", os.getpid())
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self._reload_if_changed()
                    self.engine.tick(force_job=force_job)
                    self._check_ffmpeg()
                except Exception:
                    log.exception("daemon tick failed")
                    self.engine.write_status()
                # The full poller cadence stays poll_seconds, but queued CLI
                # commands are serviced at least every second (SPEC 3.1).
                deadline = started + self.tick_seconds
                while not self._stop.is_set() and time.monotonic() < deadline:
                    self._stop.wait(min(1.0, max(0.0, deadline - time.monotonic())))
                    if self._stop.is_set():
                        break
                    try:
                        self.engine.process_commands()
                    except Exception:
                        log.exception("command servicing failed")
            log.info("mediaflow daemon stopping")
            self.engine.write_status()
            return 0

    def stop(self) -> None:
        self._stop.set()

    def _install_signals(self) -> None:
        def handler(signum, frame):
            log.info("signal %s received", signum)
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass  # non-main thread (tests)

    def _fingerprint(self) -> tuple[float, ...]:
        values = []
        for p in (self.config_path, self.presets_dir):
            if p is None:
                continue
            try:
                if p.is_dir():
                    values.extend(sorted(f.stat().st_mtime for f in p.glob("*.toml")))
                    values.append(p.stat().st_mtime)
                else:
                    values.append(p.stat().st_mtime)
            except OSError:
                values.append(0.0)
        return tuple(values)

    def _reload_if_changed(self) -> None:
        if self.config_path is None:
            return
        current = self._fingerprint()
        if current == self._config_mtime:
            return
        self._config_mtime = current
        try:
            from .config import load_config
            from .presets import load_presets
            new = load_config(self.config_path, environ=os.environ)
            presets = load_presets(Path(getattr(new, "presets_dir", self.presets_dir or "")))
            old_dry = self.engine.dry_run
            self.config = new
            self.engine.cfg = new
            self.engine.dry_run = bool(getattr(new, "dry_run", True))
            self.engine.presets = dict(presets)
            self.engine._pause_reasons = []
            self.store.event(None, "config-reload", "loaded")
            if old_dry != self.engine.dry_run:
                self.store.event(None, "dry-run", f"changed to {self.engine.dry_run}")
            log.info("configuration reloaded")
        except Exception as exc:
            log.warning("config reload rejected; previous good config remains: %s", exc)
            self.engine._pause_reasons = [f"config-invalid: {exc}"]

    def _check_ffmpeg(self) -> None:
        """Detect an unattended jellyfin-ffmpeg upgrade (SAF-12)."""
        ffmpeg = str(getattr(self.config, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg"))
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=20)
            version = out.stdout.splitlines()[0] if out.stdout else "unknown"
        except Exception as exc:
            self.engine._pause_reasons = ["ffmpeg-missing"]
            log.warning("ffmpeg check failed: %s", exc)
            return
        cap_path = Path(getattr(self.config, "capabilities_path", "/var/lib/mediaflow/capabilities.json"))
        try:
            caps = json.loads(cap_path.read_text(encoding="utf-8"))
        except Exception:
            caps = {}
        prior = caps.get("ffmpeg_version")
        if prior and prior != version:
            log.warning("ffmpeg version changed; queue paused pending capability tests")
            self.store.event(None, "ffmpeg-changed", "version mismatch")
            self.engine._pause_reasons = ["ffmpeg-changed"]
            try:
                from .doctor import run_capability_tests
                results = run_capability_tests(self.config)
                if results.get("ok"):
                    caps.update(results)
                    caps["ffmpeg_version"] = version
                    tmp = cap_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(caps, indent=2), encoding="utf-8")
                    os.replace(tmp, cap_path)
                    self.engine._pause_reasons = []
                    self.store.event(None, "ffmpeg-changed", "capability tests passed")
                else:
                    self.engine._pause_reasons = ["ffmpeg-changed: capability tests failed"]
            except Exception as exc:
                log.warning("capability tests unavailable: %s", exc)
                self.engine._pause_reasons = ["ffmpeg-changed: capability tests failed"]
            return
        if not prior:
            caps["ffmpeg_version"] = version
            try:
                cap_path.parent.mkdir(parents=True, exist_ok=True)
                cap_path.write_text(json.dumps(caps, indent=2), encoding="utf-8")
            except OSError:
                pass
