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

from .config import (
    classify_config_change,
    classify_secret_change,
    config_mtime_fingerprint,
    load_config,
    load_secrets,
)
from .doctor import read_capabilities
from .engine import Engine
from .presets import load_presets
from .safety import fsync_dir

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
                 notifier=None, identity_resolver=None, tick_seconds: float | None = None,
                 secrets: Any = None):
        self.config = config
        self.store = store
        self.presets = presets
        self.secrets = secrets
        self.config_path = Path(config_path) if config_path else None
        self.presets_dir = Path(presets_dir) if presets_dir else None
        self.tick_seconds = float(tick_seconds if tick_seconds is not None else
                                  getattr(config, "poll_seconds", 30))
        self.engine = Engine(config, store, presets, encoder=encoder, verifier=verifier,
                             guards=guards, notifier=notifier, secrets=secrets,
                             identity_resolver=identity_resolver)
        self._stop = threading.Event()
        self._generation_lock = threading.RLock()
        self._config_mtime = self._fingerprint()

    def run(self, *, once: bool = False, force_job: str | None = None) -> int:
        with daemon_lock(self.config, blocking=False):
            self._install_signals()
            # Holds about a previous configuration generation are resolved by
            # a restart: the loaded config is now the effective generation.
            self.store.set_setting("config_invalid", "")
            self.store.set_setting("restart_required", "")
            self.engine.clear_global_pause_reasons("config-invalid:")
            self.engine.clear_global_pause_reasons("restart-required:")
            self.engine._pause_reasons = [
                value for value in self.engine._pause_reasons
                if not value.startswith(("config-invalid:", "restart-required:"))
            ]
            self.engine.set_restart_required(())
            self.engine.start()
            self._check_ffmpeg()
            if once:
                self.engine.tick(once=True, force_job=force_job)
                return 0
            log.info("mediaflow daemon started pid=%s", os.getpid())
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.engine.apply_pending_config()
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
        self.engine.request_shutdown()

    def _install_signals(self) -> None:
        def handler(signum, frame):
            log.info("signal %s received", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass  # non-main thread (tests)

    def _fingerprint(self) -> Any:
        """Fingerprint the complete config and preset tree.

        The shared helper uses nanosecond mtimes and recursive preset scanning;
        the legacy fallback is only for programmatically built test daemons
        without file paths.
        """

        if self.config_path is not None and self.presets_dir is not None:
            return config_mtime_fingerprint(self.config_path, self.presets_dir)
        values = []
        for p in (self.config_path, self.presets_dir):
            if p is None:
                continue
            try:
                values.append(p.stat().st_mtime_ns)
            except OSError:
                values.append(0)
        return tuple(values)

    def _reload_if_changed(self) -> None:
        """Validate and install a complete configuration generation atomically.

        Nothing is applied until config, presets and secret files all load
        successfully.  Restart-required values are retained from the old
        generation and reported as a persistent hold.  A valid later reload
        clears only the invalid-config hold.
        """

        if self.config_path is None:
            return
        current = self._fingerprint()
        if current == self._config_mtime:
            return
        self._config_mtime = current
        try:
            presets_dir = self.presets_dir or Path(
                getattr(self.config, "presets_dir", self.config_path.parent / "presets.d")
            )
            new = load_config(self.config_path, environ=os.environ)
            new_presets = load_presets(presets_dir)
            env_dir = Path(os.environ.get("MEDIAFLOW_ENV_DIR", str(self.config_path.parent)))
            new_secrets = load_secrets(env_dir)
        except Exception as exc:
            # Reason text is produced by the loaders and never contains a
            # secret value; only key paths and TOML coordinates are included.
            reason = f"config-invalid: {exc}"
            log.warning("config reload rejected; previous good config remains: %s", exc)
            with self._generation_lock:
                self.engine.clear_global_pause_reasons("config-invalid:")
                self.engine.add_global_pause_reasons([reason])
                self.engine._pause_reasons = sorted({
                    value for value in self.engine._pause_reasons
                    if not value.startswith("config-invalid:")
                } | {reason})
                self.store.set_setting("config_invalid", reason)
            return

        with self._generation_lock:
            plan = classify_config_change(self.config, new)
            secret_plan = classify_secret_change(self.secrets, new_secrets)
            restart = tuple(sorted({
                *plan.restart_required, *secret_plan.restart_required
            }))
            effective = plan.effective_config
            old_dry = self.engine.dry_run

            # All validation is complete.  Publish one coherent generation.
            self.config = effective
            self.engine.set_restart_required(restart)
            self.engine.clear_global_pause_reasons("config-invalid:")
            self.engine._pause_reasons = [
                value for value in self.engine._pause_reasons
                if not value.startswith("config-invalid:")
            ]
            self.store.set_setting("config_invalid", "")
            if restart:
                self.store.set_setting("restart_required", ",".join(restart))
                self.engine.add_global_pause_reasons([
                    f"restart-required:{key}" for key in restart
                ])
            else:
                self.store.set_setting("restart_required", "")
                self.engine.clear_global_pause_reasons("restart-required:")

            # Secret rotations are restart-required, so the daemon keeps the
            # old object until restart; no value is logged or persisted.
            self.engine.pending_config = effective
            self.engine.pending_presets = new_presets
            self.engine.pending_secrets = self.secrets
            applied = self.engine.apply_pending_config()

            self.store.event(
                None, "config-reload",
                f"generation={effective.generation} applied={applied}"
                + (f" restart-required={','.join(restart)}" if restart else ""),
            )
            if old_dry != self.engine.dry_run:
                self.store.event(None, "dry-run", f"changed to {self.engine.dry_run}")
            log.info(
                "configuration reloaded generation=%s applied=%s restart_required=%s",
                effective.generation, applied, ",".join(restart) or "none",
            )

    @staticmethod
    def _write_capabilities(path: Path, caps: dict) -> None:
        """Atomically replace capabilities.json with fsync semantics."""

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(caps, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)

    def _check_ffmpeg(self) -> None:
        """Detect an unattended jellyfin-ffmpeg upgrade (SAF-12)."""
        ffmpeg = str(getattr(self.config, "ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg"))
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=20)
            version = out.stdout.splitlines()[0] if out.stdout else "unknown"
        except Exception as exc:
            self.engine.clear_global_pause_reasons("ffmpeg-")
            self.engine.add_global_pause_reasons(["ffmpeg-missing"])
            self.store.set_setting("ffmpeg_missing_hold", "ffmpeg-missing")
            log.warning("ffmpeg check failed: %s", exc)
            return
        self.store.set_setting("ffmpeg_missing_hold", "")
        cap_path = Path(getattr(self.config, "capabilities_path", "/var/lib/mediaflow/capabilities.json"))
        caps = read_capabilities(cap_path)
        prior = caps.get("ffmpeg_version")
        if prior:
            self.store.set_setting("capability_unknown_hold", "")
        if prior and prior != version:
            log.warning("ffmpeg version changed; queue paused pending capability tests")
            self.store.event(None, "ffmpeg-changed", "version mismatch")
            self.engine.clear_global_pause_reasons("ffmpeg-changed")
            self.engine.add_global_pause_reasons(["ffmpeg-changed"])
            self.store.set_setting("ffmpeg_changed_hold", "ffmpeg-changed")
            try:
                from .doctor import run_capability_tests
                # The daemon's stop event is the caller-owned cancellation for
                # the retest.  Capability probes still own their own timeouts,
                # AC/thermal monitor and exclusive slot.
                results = run_capability_tests(
                    self.config, cancel_event=self._stop
                )
                capability_status = str(results.get("status") or "ok")
                if capability_status not in {"ok", "completed"}:
                    reason = (
                        f"ffmpeg-changed: capability tests {capability_status}"
                    )
                    self.engine.clear_global_pause_reasons("ffmpeg-changed")
                    self.engine.add_global_pause_reasons([reason])
                    self.store.set_setting("ffmpeg_changed_hold", reason)
                    self.store.event(
                        None,
                        "ffmpeg-changed",
                        f"{reason}; reasons={','.join(results.get('reasons') or [])}",
                    )
                elif (
                    (results.get("completed")
                     if "completed" in results
                     else str(results.get("status") or "") in {"ok", "completed"})
                    and ("libvmaf" not in results or results.get("libvmaf") is not None)
                ):
                    caps.update(results)
                    caps["ffmpeg_version"] = version
                    self._write_capabilities(cap_path, caps)
                    self.engine.clear_global_pause_reasons("ffmpeg-changed")
                    self.store.set_setting("ffmpeg_changed_hold", "")
                    self.store.event(None, "ffmpeg-changed", "capability tests passed")
                else:
                    reason = "ffmpeg-changed: capability tests failed"
                    self.engine.clear_global_pause_reasons("ffmpeg-changed")
                    self.engine.add_global_pause_reasons([reason])
                    self.store.set_setting("ffmpeg_changed_hold", reason)
            except Exception as exc:
                log.warning("capability tests unavailable: %s", exc)
                reason = "ffmpeg-changed: capability tests failed"
                self.engine.clear_global_pause_reasons("ffmpeg-changed")
                self.engine.add_global_pause_reasons([reason])
                self.store.set_setting("ffmpeg_changed_hold", reason)
            return
        if not prior:
            # An unversioned or missing capability file is unknown, not
            # evidence.  Never promote it to the current ffmpeg version.
            self.engine.add_global_pause_reasons(["capability-unknown"])
            self.store.set_setting("capability_unknown_hold", "capability-unknown")
            self.store.event(
                None, "capability-unknown",
                "run doctor to establish tri-state capability facts",
            )
            return
