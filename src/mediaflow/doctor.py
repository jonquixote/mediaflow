"""Doctor: explicit, fail-loud checks of this machine and installation."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .guards import MachineProtectionMonitor
from .safety import SafetyError, check_one_filesystem
from .supervision import (
    OperationState,
    ProcessResult,
    ProtectionAction,
    ProtectionDecision,
    SlotBusy,
    SupervisionPolicy,
    processing_slot,
    run_process,
    slot_path,
)

FFMPEG_DEFAULT = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_DEFAULT = "/usr/lib/jellyfin-ffmpeg/ffprobe"
RENDER_NODE = Path("/dev/dri/renderD128")

#: Capability files gained an explicit schema in Pass C.  Older files may be
#: read for diagnostics but must not establish backend facts.
CAPABILITIES_SCHEMA = 2


def capabilities_are_current(data: Any) -> bool:
    """Whether a capability file was produced by tri-state probing."""

    if not isinstance(data, dict):
        return False
    return data.get("capabilities_schema") in (CAPABILITIES_SCHEMA, str(CAPABILITIES_SCHEMA))


def read_capabilities(path: str | Path) -> dict:
    """Read a capability file, treating pre-version records as unknown."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if capabilities_are_current(data) else {}


@dataclass
class Check:
    """One preflight check.

    ``status`` is retained for older callers. ``outcome`` is the truthful
    three-way result: pass, fail or unknown. ``check_class`` decides whether a
    result affects the exit code.
    """

    name: str
    status: str  # ok | warn | fail (legacy view)
    detail: str
    meta: dict[str, Any] = field(default_factory=dict)
    check_class: str = "required"  # required | feature-conditional | informational
    registry_id: str | None = None

    @property
    def outcome(self) -> str:
        return {"ok": "pass", "warn": "unknown", "fail": "fail"}[self.status]


@dataclass
class DoctorReport:
    checks: list[Check]

    @property
    def required_failures(self) -> list[Check]:
        return [c for c in self.checks
                if c.status == "fail" and c.check_class == "required"]

    @property
    def feature_failures(self) -> list[Check]:
        return [c for c in self.checks
                if c.status == "fail" and c.check_class == "feature-conditional"]

    @property
    def required_unknowns(self) -> list[Check]:
        return [c for c in self.checks
                if c.status == "warn" and c.check_class == "required"]

    @property
    def unknowns(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def ok(self) -> bool:
        return not self.required_failures and not self.feature_failures \
            and not self.required_unknowns

    @property
    def warnings(self) -> int:
        return sum(c.status == "warn" for c in self.checks)

    @property
    def exit_code(self) -> int:
        """0 pass; 2 required failure; 3 enabled-feature failure; 4 required unknown."""

        if self.required_failures:
            return 2
        if self.feature_failures:
            return 3
        if self.required_unknowns:
            return 4
        return 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "summary": {
                "pass": sum(c.outcome == "pass" for c in self.checks),
                "fail": sum(c.outcome == "fail" for c in self.checks),
                "unknown": sum(c.outcome == "unknown" for c in self.checks),
                "required_failures": len(self.required_failures),
                "feature_failures": len(self.feature_failures),
                "required_unknowns": len(self.required_unknowns),
                "informational": sum(c.check_class == "informational" for c in self.checks),
            },
            "warnings": self.warnings,
            "checks": [{
                "name": c.name, "status": c.status, "outcome": c.outcome,
                "class": c.check_class, "registry_id": c.registry_id,
                "detail": c.detail, "meta": c.meta,
            } for c in self.checks],
        }


def _run_supervised(
    argv: list[str],
    timeout: float = 60.0,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> ProcessResult:
    """Run one command through the shared process owner."""

    return run_process(
        argv,
        policy=SupervisionPolicy(
            stall_timeout=None,
            startup_grace=min(30.0, max(0.0, float(timeout))),
            finalization_grace=min(30.0, max(0.0, float(timeout))),
            overall_timeout=timeout if timeout and timeout > 0 else None,
            monitor_interval=0.2,
            terminate_grace=5.0,
        ),
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )


def _run(argv: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Compatibility subprocess result backed by the shared process owner."""

    process = _run_supervised(argv, timeout)
    if process.state is OperationState.TIMED_OUT:
        raise subprocess.TimeoutExpired(argv, timeout)
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        process.stdout,
        process.stderr,
    )


def _ffmpeg_version_process(
    ffmpeg: str,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> tuple[ProcessResult, str | None, str | None]:
    """Return the typed version probe and its parsed value/error."""

    process = _run_supervised(
        [ffmpeg, "-version"],
        timeout=20,
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    if process.state is not OperationState.SUCCEEDED:
        detail = (process.reason or process.stderr or "ffmpeg version did not complete")[-300:]
        return process, None, detail
    lines = (process.stdout or "").splitlines()
    return process, (lines[0] if lines else "unknown"), None


def _ffmpeg_version(ffmpeg: str) -> tuple[str | None, str | None]:
    process, version, error = _ffmpeg_version_process(ffmpeg)
    if process.state is not OperationState.SUCCEEDED:
        return None, error
    return version, None


def _rate_control(stderr: str) -> str:
    text = stderr.lower()
    for name in ("icq", "cqp", "vbr", "cbr", "la_icq", "la_vbr"):
        if name in text:
            return name.upper()
    if "ratecontrolmethod" in text:
        line = next((l for l in stderr.splitlines() if "ratecontrolmethod" in l.lower()), "")
        return line.strip()[:120]
    return "unknown"


def _encode_probe(
    ffmpeg: str,
    backend: str,
    ten_bit: bool,
    output: Path,
    quality: int = 24,
    timeout: float = 90.0,
    *,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> dict:
    common = [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "debug",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3",
        "-an", "-sn", "-dn", "-y",
    ]
    if backend == "qsv":
        argv = common + [
            "-c:v", "hevc_qsv", "-global_quality", str(quality), "-look_ahead", "1",
        ]
        if ten_bit:
            argv += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        argv += ["-tag:v", "hvc1", "-f", "mp4", str(output)]
    elif backend == "vaapi":
        argv = common + [
            "-vaapi_device", str(RENDER_NODE),
            "-vf", "format=p010le,hwupload" if ten_bit else "format=nv12,hwupload",
            "-c:v", "hevc_vaapi", "-qp", str(quality),
        ]
        if ten_bit:
            argv += ["-profile:v", "main10"]
        argv += ["-f", "mp4", str(output)]
    elif backend == "software":
        argv = common + [
            "-c:v", "libx265", "-preset", "slow", "-crf", str(quality),
        ]
        if ten_bit:
            argv += ["-pix_fmt", "yuv420p10le"]
        argv += ["-tag:v", "hvc1", "-f", "mp4", str(output)]
    else:
        raise ValueError(backend)
    process = run_process(
        argv,
        policy=SupervisionPolicy(
            stall_timeout=None,
            overall_timeout=timeout if timeout and timeout > 0 else None,
            monitor_interval=0.2,
            terminate_grace=5.0,
        ),
        cancel_event=cancel_event,
        pause_event=pause_event,
        monitor=monitor,
    )
    if process.state not in {OperationState.SUCCEEDED, OperationState.FAILED}:
        return {
            "available": None,
            "hevc8": None,
            "hevc10": None,
            "rate_control": "unknown",
            "error": (process.reason or process.stderr or "probe did not complete")[-500:],
            "state": process.state.value,
            "reasons": list(process.reasons),
            "timeout_type": process.timeout_type,
            "spawn_failed": False,
            "incomplete": True,
            "command": [a for a in argv if str(output) not in a][:4] + ["..."],
        }
    if process.state is OperationState.FAILED and process.spawn_failed:
        return {
            "available": None,
            "hevc8": None,
            "hevc10": None,
            "rate_control": "unknown",
            "error": (process.reason or process.stderr or "probe could not start")[-500:],
            "state": process.state.value,
            "reasons": list(process.reasons),
            "timeout_type": process.timeout_type,
            "spawn_failed": True,
            "incomplete": True,
            "command": [a for a in argv if str(output) not in a][:4] + ["..."],
        }
    ok = (
        process.state is OperationState.SUCCEEDED
        and output.exists()
        and output.stat().st_size > 1000
    )
    return {
        "available": ok,
        "hevc8": ok and not ten_bit,
        "hevc10": ok and ten_bit,
        "rate_control": _rate_control(process.stderr or ""),
        "error": None if ok else (process.stderr or "")[-500:],
        "state": process.state.value,
        "reasons": list(process.reasons),
        "timeout_type": process.timeout_type,
        "spawn_failed": False,
        "incomplete": False,
        "command": [a for a in argv if str(output) not in a][:4] + ["..."],
    }


def _event_is_set(event: Any) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except Exception:
        return False


def _capability_decision(results: dict[str, Any], decision: Any) -> dict[str, Any]:
    """Map a machine decision to an exact capability outcome."""

    action = ProtectionAction(decision.action)
    reasons = list(getattr(decision, "reasons", ()) or ())
    detail = str(getattr(decision, "reason_text", "") or "")
    if action is ProtectionAction.PAUSE:
        results.update(
            status="paused",
            state=OperationState.PAUSED.value,
            deferred=False,
            completed=False,
            reasons=reasons,
            ok=False,
            reason=detail or "capability tests are paused",
        )
    elif action is ProtectionAction.DEFER:
        results.update(
            status="deferred",
            state=OperationState.DEFERRED.value,
            deferred=True,
            completed=False,
            reasons=reasons,
            ok=False,
            reason=detail or "machine protection deferred capability tests",
        )
    elif action is ProtectionAction.TERMINATE:
        cancelled = str(getattr(decision, "kind", "")) == "cancelled"
        results.update(
            status="cancelled" if cancelled else "interrupted",
            state=(OperationState.CANCELLED if cancelled else OperationState.INTERRUPTED).value,
            deferred=False,
            completed=False,
            reasons=reasons,
            ok=False,
            reason=detail or ("operation cancelled" if cancelled else "protection interrupted capability tests"),
        )
    return results


def _incomplete_capability(
    results: dict[str, Any],
    state: OperationState,
    *,
    reasons: Iterable[str] = (),
    timeout_type: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record an incomplete result without inventing unsupported facts."""

    status = {
        OperationState.CANCELLED: "cancelled",
        OperationState.INTERRUPTED: "interrupted",
        OperationState.DEFERRED: "deferred",
        OperationState.TIMED_OUT: "timed-out",
        OperationState.STALLED: "stalled",
        OperationState.FAILED: "failed",
    }.get(state, "failed")
    merged = list(dict.fromkeys([
        *(str(item) for item in results.get("reasons", []) or []),
        *(str(item) for item in reasons),
    ]))
    results.update(
        status=status,
        state=state.value,
        deferred=state is OperationState.DEFERRED,
        completed=False,
        reasons=merged,
        timeout_type=timeout_type,
        ok=False,
        reason=reason or "capability probe did not complete",
    )
    return results


def run_capability_tests(
    config: Any,
    *,
    ffmpeg: str | None = None,
    backends: Iterable[str] = ("qsv", "vaapi"),
    allow_software: bool = False,
    timeout: float = 90.0,
    cancel_event: Any = None,
    pause_event: Any = None,
    monitor: Any = None,
) -> dict:
    """Run capability probes under one slot and shared supervision.

    Capability facts are tri-state.  A completed probe may establish
    ``True`` or ``False``; an incomplete, deferred or spawn-failed probe is
    ``None``.  The second bit-depth probe is not started after an incomplete
    first probe, while a completed 8-bit fact remains available if the
    10-bit probe later times out.  Incomplete reports are never persisted over
    a valid capability file.

    Software/libx265 probing is disabled by default. The initial deployment
    workflow and automatic retests must never encode with x265.
    """

    backends = tuple(str(name) for name in backends)
    if "software" in backends and not allow_software:
        raise ValueError("software capability probes require explicit allow_software=True")
    unknown = sorted(set(backends) - {"qsv", "vaapi", "software"})
    if unknown:
        raise ValueError(f"unknown capability backend(s): {', '.join(unknown)}")
    if not backends:
        raise ValueError("at least one capability backend is required")

    ffmpeg = ffmpeg or str(getattr(config, "ffmpeg", FFMPEG_DEFAULT))
    results: dict[str, Any] = {
        "capabilities_schema": CAPABILITIES_SCHEMA,
        "backends_requested": list(backends),
        "software_probing": "software" in backends,
        "status": "ok",
        "state": OperationState.SUCCEEDED.value,
        "deferred": False,
        "completed": False,
        "reasons": [],
        "timeout_type": None,
        "ok": False,
        "reason": None,
        "ffmpeg_version": None,
        "version_error": None,
        "tested_at": time.time(),
        "render_node": str(RENDER_NODE),
        "backends": {},
        "libvmaf": None,
    }

    cancel = cancel_event if cancel_event is not None else threading.Event()
    pause = pause_event if pause_event is not None else threading.Event()
    protection = monitor if monitor is not None else MachineProtectionMonitor(
        config,
        cancel_event=cancel,
        pause_event=pause,
        defer_on_protection=True,
    )
    try:
        initial = protection()
    except Exception as exc:
        return _incomplete_capability(
            results,
            OperationState.FAILED,
            reasons=["protection-monitor-error"],
            reason=f"protection monitor failed: {exc}",
        )
    if initial.action is not ProtectionAction.CONTINUE:
        return _capability_decision(results, initial)
    if _event_is_set(pause):
        return _capability_decision(
            results,
            ProtectionDecision(
                ProtectionAction.PAUSE,
                ("caller-paused",),
                detail="capability tests are paused by the caller",
            ),
        )
    if _event_is_set(cancel):
        return _capability_decision(
            results,
            ProtectionDecision(
                ProtectionAction.TERMINATE,
                ("user-cancelled",),
                detail="operation cancelled",
                kind="cancelled",
            ),
        )

    slot = processing_slot(slot_path(config), timeout=0)
    try:
        slot.__enter__()
    except SlotBusy:
        return _incomplete_capability(
            results,
            OperationState.DEFERRED,
            reasons=["processing-slot-busy"],
            reason="the expensive-operation slot is busy",
        )
    except OSError as exc:
        return _incomplete_capability(
            results,
            OperationState.FAILED,
            reasons=["processing-slot-unavailable"],
            reason=f"the expensive-operation slot is unavailable: {exc}",
        )

    try:
        version_process, version, version_error = _ffmpeg_version_process(
            ffmpeg,
            cancel_event=cancel,
            pause_event=pause,
            monitor=protection,
        )
        results["ffmpeg_version"] = version
        results["version_error"] = version_error
        if version_process.state is not OperationState.SUCCEEDED:
            state = version_process.state
            if state is OperationState.FAILED and not version_process.spawn_failed:
                state = OperationState.FAILED
            return _incomplete_capability(
                results,
                state,
                reasons=version_process.reasons,
                timeout_type=version_process.timeout_type,
                reason=version_error or "ffmpeg version probe did not complete",
            )

        with tempfile.TemporaryDirectory(prefix="mediaflow-caps-") as td:
            for backend in backends:
                eight = _encode_probe(
                    ffmpeg,
                    backend,
                    False,
                    Path(td) / f"{backend}-8.mp4",
                    timeout=timeout,
                    cancel_event=cancel,
                    pause_event=pause,
                    monitor=protection,
                )
                ten: dict[str, Any] | None = None
                if not eight.get("incomplete"):
                    ten = _encode_probe(
                        ffmpeg,
                        backend,
                        True,
                        Path(td) / f"{backend}-10.mp4",
                        timeout=timeout,
                        cancel_event=cancel,
                        pause_event=pause,
                        monitor=protection,
                    )

                incomplete = eight if eight.get("incomplete") else (ten if ten and ten.get("incomplete") else None)
                if incomplete is not None:
                    state = OperationState(incomplete.get("state") or OperationState.FAILED.value)
                    hevc8 = None if eight.get("incomplete") else bool(eight.get("available"))
                    hevc10 = None if ten is None or ten.get("incomplete") else bool(ten.get("available"))
                    known_available = True if hevc8 is True or hevc10 is True else None
                    results["backends"][backend] = {
                        "available": known_available,
                        "hevc8": hevc8,
                        "hevc10": hevc10,
                        "rate_control": eight.get("rate_control") or "unknown",
                        "rate_control_10bit": (ten or {}).get("rate_control") or "unknown",
                        "error": eight.get("error"),
                        "error_10bit": (ten or {}).get("error"),
                        "state": state.value,
                        "timeout_type": incomplete.get("timeout_type"),
                    }
                    results[backend] = {
                        "hevc8": hevc8,
                        "hevc10": hevc10,
                        "rate_control": results["backends"][backend]["rate_control"],
                    }
                    return _incomplete_capability(
                        results,
                        state,
                        reasons=incomplete.get("reasons") or (),
                        timeout_type=incomplete.get("timeout_type"),
                        reason="capability probe did not complete",
                    )

                eight_available = bool(eight.get("available"))
                ten_available = bool((ten or {}).get("available"))
                results["backends"][backend] = {
                    "available": eight_available or ten_available,
                    "hevc8": eight_available,
                    "hevc10": ten_available,
                    "rate_control": (
                        eight.get("rate_control")
                        if eight_available
                        else (ten or {}).get("rate_control")
                    ),
                    "rate_control_10bit": (ten or {}).get("rate_control"),
                    "error": eight.get("error") if not eight_available else None,
                    "error_10bit": (ten or {}).get("error") if not ten_available else None,
                    "state": OperationState.SUCCEEDED.value,
                }
                results[backend] = {
                    "hevc8": eight_available,
                    "hevc10": ten_available,
                    "rate_control": results["backends"][backend]["rate_control"],
                }

            filters = _run_supervised(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-protocol_whitelist",
                    "file,crypto,data",
                    "-filters",
                ],
                timeout=20,
                cancel_event=cancel,
                pause_event=pause,
                monitor=protection,
            )
            if filters.state is not OperationState.SUCCEEDED:
                results["libvmaf"] = None
                return _incomplete_capability(
                    results,
                    filters.state,
                    reasons=filters.reasons,
                    timeout_type=filters.timeout_type,
                    reason=filters.reason or "libvmaf filter probe did not complete",
                )
            results["libvmaf"] = any(
                "libvmaf" in line for line in (filters.stdout or "").splitlines()
            )
    finally:
        try:
            slot.__exit__(None, None, None)
        except Exception:
            pass

    results["status"] = "ok"
    results["state"] = OperationState.SUCCEEDED.value
    results["completed"] = True
    results["deferred"] = False
    results["ok"] = any(
        value.get("hevc8") is True for value in results["backends"].values()
    )
    return results


def _write_capabilities(config: Any, caps: dict) -> None:
    path = Path(getattr(config, "capabilities_path", Path(getattr(config, "state_dir", "/var/lib/mediaflow")) / "capabilities.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(caps, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _docs_dir() -> Path:
    env = os.environ.get("MEDIAFLOW_DOCS")
    candidates = [Path(env)] if env else []
    candidates += [Path(__file__).resolve().parents[2] / "docs",
                   Path("/usr/share/mediaflow/docs"), Path("/usr/share/doc/mediaflow")]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0] if candidates else Path("docs")


def _unchecked_questions() -> list[str]:
    path = _docs_dir() / "QUESTIONS.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip().startswith("- [ ]")]


def _read_state_settings(db_path: Path) -> dict[str, str]:
    """Read persisted holds through the same validated WAL-aware read path."""

    from .db import DatabaseError, Store

    path = Path(db_path)
    if not path.exists():
        return {}
    store = Store(path, read_only=True)
    store.check_schema()
    try:
        rows = store._conn.execute("SELECT key,value FROM settings").fetchall()
        return {str(key): str(value) for key, value in rows}
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot read persisted holds: {exc}") from exc
    finally:
        store.close()


def _config_has_secrets(path: Path | None) -> bool:
    if not path or not Path(path).exists():
        return False
    try:
        import tomllib
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return False

    def walk(value, prefix=()):
        if isinstance(value, dict):
            for k, v in value.items():
                yield from walk(v, prefix + (str(k),))
        else:
            yield prefix

    for keys in walk(data):
        lowered = [k.lower() for k in keys]
        if any(k in {"tmdb_token", "tmdb_read_token", "notify_url", "token", "url"} for k in lowered):
            return True
    return False


_FEATURE_CHECKS = {
    "encode-qsv", "encode-vaapi", "hevc-10bit", "hardware-encode", "libvmaf",
}
_INFORMATIONAL_CHECKS = {
    "verify-items", "db-backup", "last-migration", "trash-size", "ac-power",
    "capability-file", "experimental-folder", "tmdb-config", "notification-config",
}


def apply_backend_acceptance(report: DoctorReport, policy: str, *,
                             enabled_backends: frozenset[str] | None = None) -> DoctorReport:
    """Apply an explicit backend acceptance policy to feature checks.

    This is separate from requesting probes. Probing two backends never means
    both are required. ``none`` makes backend results informational.
    """

    if policy not in {"none", "qsv-required", "vaapi-required", "either"}:
        raise ValueError(f"unknown backend acceptance policy: {policy!r}")
    # An empty enabled set means no backend is enabled, not "both".
    enabled = frozenset(enabled_backends or ())
    checks = [replace(check, meta=dict(check.meta)) for check in report.checks]
    backend_checks = {c.name: c for c in checks
                      if c.name in {"encode-qsv", "encode-vaapi"}}
    for check in backend_checks.values():
        check.check_class = "informational"

    if policy == "none" or not enabled:
        status, cls, detail = "ok", "informational", "no backend acceptance policy"
    else:
        required = {
            "qsv-required": {"qsv"},
            "vaapi-required": {"vaapi"},
            "either": {"qsv", "vaapi"},
        }[policy] & enabled
        observed = {
            name.removeprefix("encode-"): check.status
            for name, check in backend_checks.items()
            if name.removeprefix("encode-") in required
        }
        states = [observed.get(backend, "missing") for backend in sorted(required)]
        if not required:
            status, cls, detail = "ok", "informational", "no eligible backend"
        elif all(state == "ok" for state in states):
            status, cls, detail = "ok", "informational", "required backend passed"
        elif any(state == "ok" for state in states) and policy == "either" \
                and "missing" not in states:
            status, cls, detail = "ok", "informational", "one eligible backend passed"
        elif any(state != "ok" for state in states) and all(
            state in {"ok", "fail"} for state in states
        ) and policy == "either":
            status, cls, detail = "fail", "feature-conditional", "no eligible backend passed"
        elif all(state == "fail" for state in states):
            status, cls, detail = "fail", "feature-conditional", "required backend failed"
        else:
            # Unknown, missing or a mix of failure and unknown is not proof of
            # unsupported hardware; it is an unmet acceptance requirement.
            status, cls, detail = "warn", "required", "backend acceptance unknown"
    checks.append(Check(
        "backend-acceptance", status, detail,
        {"policy": policy, "enabled": sorted(enabled)}, check_class=cls,
    ))
    return DoctorReport(checks)


def _classify_checks(report: DoctorReport, config: Any, acceptance: str = "none") -> DoctorReport:
    """Attach outcome classes so exit codes reflect feature and info results."""

    enabled = {str(getattr(value, "video", getattr(value, "backend", ""))).lower()
               for value in (getattr(config, "presets", {}) or {}).values()
               if getattr(getattr(value, "video", None), "action", "") == "encode"
               or getattr(value, "action", "") == "encode"} if hasattr(config, "presets") else set()
    enabled = {name for name in enabled if name in {"qsv", "vaapi", "software"}}
    for check in report.checks:
        if check.name == "temp-sensor" and str(
            getattr(config, "temp_sensor", "auto")
        ).lower() == "none":
            check.check_class = "informational"
        elif check.name in _INFORMATIONAL_CHECKS:
            check.check_class = "informational"
        elif check.name in _FEATURE_CHECKS:
            check.check_class = "feature-conditional"
        elif check.name.startswith("encode-"):
            check.check_class = "feature-conditional"
    return apply_backend_acceptance(report, acceptance, enabled_backends=frozenset(enabled))


def run_doctor(config: Any, *, config_path: Path | None = None, presets_dir: Path | None = None,
               secrets: Any = None, run_capabilities: bool = False,
               capability_backends: tuple[str, ...] | None = None,
                backend_acceptance: str = "none",
                ffmpeg_runner: Callable[..., Any] | None = None) -> DoctorReport:
    checks: list[Check] = []
    ffmpeg = str(getattr(config, "ffmpeg", FFMPEG_DEFAULT))
    ffprobe = str(getattr(config, "ffprobe", FFPROBE_DEFAULT))
    root = Path(getattr(config, "root", "/srv/media"))

    # binaries
    for label, exe in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        path = Path(exe)
        if not path.is_file() or not os.access(path, os.X_OK):
            checks.append(Check(f"{label}-binary", "fail", f"not executable: {exe}"))
        else:
            checks.append(Check(f"{label}-binary", "ok", exe))
    # Capability probes own their ffmpeg version subprocess.  This avoids a
    # second version probe outside the processing slot and keeps a busy slot
    # from starting any capability-related process.
    backends = capability_backends or ("qsv", "vaapi")
    caps = run_capability_tests(
        config, ffmpeg=ffmpeg, backends=backends, allow_software=False,
    ) if run_capabilities else {}
    if run_capabilities:
        version = caps.get("ffmpeg_version")
        verr = caps.get("version_error")
        version_status = "ok" if version else ("warn" if caps else "fail")
    else:
        version, verr = _ffmpeg_version(ffmpeg)
        version_status = "ok" if version else "fail"
    if version:
        checks.append(Check("ffmpeg-version", version_status, version, {"version": version}))
    else:
        checks.append(Check("ffmpeg-version", version_status, verr or "unknown"))

    # render node
    if RENDER_NODE.exists() and os.access(RENDER_NODE, os.R_OK | os.W_OK):
        checks.append(Check("render-node", "ok", str(RENDER_NODE)))
    elif RENDER_NODE.exists():
        checks.append(Check("render-node", "fail", f"not readable/writable by this user: {RENDER_NODE}"))
    else:
        checks.append(Check("render-node", "warn", f"missing {RENDER_NODE}; hardware encode unavailable"))

    # capability tests
    if caps:
        capability_status = str(caps.get("status") or "ok")
        completed = (
            bool(caps.get("completed"))
            if "completed" in caps
            else capability_status in {"ok", "completed"}
        )
        if not completed:
            checks.append(Check(
                "capability-state",
                "warn",
                caps.get("reason") or f"capability tests {capability_status}",
                {
                    "status": capability_status,
                    "state": caps.get("state"),
                    "reasons": caps.get("reasons") or [],
                },
            ))
        for backend, result in (caps.get("backends") or {}).items():
            hevc8 = result.get("hevc8")
            hevc10 = result.get("hevc10")
            if hevc8 is None and hevc10 is None:
                checks.append(Check(
                    f"encode-{backend}", "warn",
                    "capability is unknown because the probe did not complete",
                    result,
                ))
                continue
            status = "ok" if hevc8 is True else "warn"
            eight_text = "yes" if hevc8 is True else ("no" if hevc8 is False else "unknown")
            ten_text = "yes" if hevc10 is True else ("no" if hevc10 is False else "unknown")
            checks.append(Check(
                f"encode-{backend}", status,
                f"HEVC 8-bit={eight_text}, 10-bit={ten_text}, "
                f"rate-control={result.get('rate_control') or 'unknown'}",
                result,
            ))
        hardware = {name: result
                    for name, result in (caps.get("backends") or {}).items()
                    if name in {"qsv", "vaapi"}}
        hardware_8bit = any(r.get("hevc8") is True for r in hardware.values())
        hardware_10bit = any(r.get("hevc10") is True for r in hardware.values())
        hardware_8_unknown = any(r.get("hevc8") is None for r in hardware.values())
        if not hardware_8bit:
            detail = (
                "hardware HEVC 8-bit capability is unknown"
                if hardware_8_unknown
                else "no hardware HEVC 8-bit encoder; only MKV remuxes will work"
            )
            checks.append(Check("hardware-encode", "warn", detail))
        # Preflight is read-only.  Capability facts are written by the daemon
        # after a deliberate retest, never by doctor.
        try:
            existing = read_capabilities(getattr(config, "capabilities_path", ""))
            if not existing:
                checks.append(Check(
                    "capability-file", "warn",
                    "no current tri-state capability file; doctor does not write one",
                    check_class="informational",
                ))
            else:
                age = time.time() - float(existing.get("tested_at") or 0)
                checks.append(Check(
                    "capability-file", "ok" if age < 30 * 86400 else "warn",
                    f"capability file age {max(0, age) / 86400:.1f}d",
                    check_class="informational",
                ))
        except Exception as exc:
            checks.append(Check("capability-file", "warn", str(exc),
                                check_class="informational"))
        if hardware_10bit:
            ten_detail = "hardware 10-bit HEVC available"
        elif any(r.get("hevc10") is None for r in hardware.values()):
            ten_detail = "hardware 10-bit HEVC capability is unknown"
        else:
            ten_detail = "hardware 10-bit HEVC unavailable; 10bit-no-hw REVIEW path active"
        checks.append(Check("hevc-10bit", "ok" if hardware_10bit else "warn", ten_detail))

    # temperature sensor
    try:
        from .guards import GuardConfig, discover_temp_sensor, read_package_temp
        gc = GuardConfig.from_config(config)
        sensor = discover_temp_sensor(gc)
        if sensor and sensor.kind != "none":
            temp = read_package_temp(sensor)
            checks.append(Check("temp-sensor", "ok", f"{sensor.name}: {sensor.path} ({temp} C)",
                                sensor.to_json()))
        elif str(getattr(config, "temp_sensor", "auto")) == "none":
            checks.append(Check("temp-sensor", "warn", "disabled explicitly; encodes are not thermally gated"))
        else:
            checks.append(Check("temp-sensor", "fail",
                                "no coretemp package sensor; encodes fail closed (SAF-9)"))
    except Exception as exc:
        checks.append(Check("temp-sensor", "fail", f"guard check failed: {exc}"))

    # AC power
    try:
        from .guards import GuardConfig, read_ac_state
        gc = gc if "gc" in locals() else GuardConfig.from_config(config)
        ac = read_ac_state(gc)
        checks.append(Check("ac-power", "ok" if ac is True else "warn",
                            "AC online" if ac is True else "on battery or no AC source"))
    except Exception as exc:
        checks.append(Check("ac-power", "warn", f"could not read power supply: {exc}"))

    # one filesystem
    try:
        check_one_filesystem([Path(getattr(config, n)) for n in
                              ("inbox", "convert", "work", "review", "failed", "trash", "movies")])
        checks.append(Check("one-filesystem", "ok", "pipeline and library share one st_dev"))
    except (SafetyError, FileNotFoundError, AttributeError) as exc:
        checks.append(Check("one-filesystem", "fail", str(exc)))

    # free space and trash
    try:
        free = shutil.disk_usage(root).free
        min_free = float(getattr(config, "min_free_gb", 40)) * 1024**3
        status = "ok" if free >= min_free else "fail"
        checks.append(Check("free-space", status, f"{free / 1024**3:.1f} GiB free (need {min_free / 1024**3:.0f} GiB)"))
    except OSError as exc:
        checks.append(Check("free-space", "fail", str(exc)))
    try:
        trash = Path(getattr(config, "trash"))
        trash_bytes = sum(p.stat().st_size for p in trash.rglob("*") if p.is_file() and not p.is_symlink())
        over = trash_bytes > float(getattr(config, "max_trash_gb", 120)) * 1024**3
        checks.append(Check("trash-size", "warn" if over else "ok",
                            f"{trash_bytes / 1024**3:.1f} GiB in trash"))
    except Exception as exc:
        checks.append(Check("trash-size", "warn", str(exc)))

    # permissions
    for name in ("pipeline", "movies", "work", "convert", "trash"):
        try:
            p = Path(getattr(config, name))
            mode = p.stat().st_mode
            setgid = bool(mode & 0o2000)
            writable = os.access(p, os.W_OK)
            checks.append(Check(f"perm-{name}", "ok" if (setgid and writable) else "fail",
                                f"{oct(mode & 0o7777)} setgid={setgid} writable={writable}"))
        except Exception as exc:
            checks.append(Check(f"perm-{name}", "fail", str(exc)))

    # DB and backup: strictly read-only.  Doctor must never migrate or create.
    try:
        from .db import SCHEMA_VERSION
        db_path = Path(getattr(config, "db_path"))
        if not db_path.is_file():
            checks.append(Check(
                "db-integrity", "warn", "state.db absent; no migration performed",
                check_class="informational",
            ))
            checks.append(Check("db-schema", "warn", "schema unknown; database absent",
                                check_class="informational"))
        else:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=1.0)
            try:
                rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
                if rows != ["ok"]:
                    raise RuntimeError("integrity_check=" + "; ".join(rows))
                version_row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                migration_row = conn.execute(
                    "SELECT value FROM meta WHERE key='last_migration_backup'"
                ).fetchone()
            finally:
                conn.close()
            current_version = int(version_row[0]) if version_row else 0
            checks.append(Check(
                "db-schema",
                "ok" if current_version == SCHEMA_VERSION else "warn",
                f"schema {current_version} (supported {SCHEMA_VERSION}); doctor does not migrate",
            ))
            checks.append(Check(
                "last-migration",
                "ok" if migration_row else "warn",
                f"backup {migration_row[0]}" if migration_row else "no schema migration performed",
                check_class="informational",
            ))
            checks.append(Check("db-integrity", "ok", "integrity_check=ok (read-only)"))
        backup_dir = Path(getattr(config, "backup_dir"))
        backups = sorted(backup_dir.glob("state-*.db")) if backup_dir.is_dir() else []
        if backups:
            age = time.time() - backups[-1].stat().st_mtime
            checks.append(Check("db-backup", "ok" if age < 48 * 3600 else "warn",
                                f"newest backup {age / 3600:.1f}h old",
                                check_class="informational"))
        else:
            checks.append(Check("db-backup", "warn", "no backup yet",
                                check_class="informational"))
    except Exception as exc:
        checks.append(Check("db-integrity", "fail", f"refusing to trust state: {exc}"))

    # configuration generation and reload classification
    from .config import KEY_CLASS, ReloadClass
    restart_keys = sorted(
        key for key, value in KEY_CLASS.items()
        if value is ReloadClass.RESTART_REQUIRED
    )
    checks.append(Check(
        "config-generation", "ok",
        f"generation {int(getattr(config, 'generation', 1) or 1)}",
        {"generation": int(getattr(config, "generation", 1) or 1)},
    ))
    checks.append(Check(
        "config-reload", "ok",
        f"{len(KEY_CLASS)} keys classified; {len(restart_keys)} require restart",
        {"restart_required": restart_keys},
    ))

    # secrets in config and permissions
    if _config_has_secrets(config_path):
        checks.append(Check("secrets", "fail", "secret-like key found in config.toml (SAF-10)"))
    else:
        checks.append(Check("secrets", "ok", "no secret-like keys in config.toml"))
    if secrets is not None:
        warns = list(getattr(secrets, "warnings", ()) or ())
        checks.append(Check("secret-files", "warn" if warns else "ok",
                            "; ".join(warns) if warns else "env files missing or permissions acceptable"))

    # presets
    if presets_dir is None:
        presets_dir = Path(os.environ.get("MEDIAFLOW_PRESETS", "/etc/mediaflow/presets.d"))
    try:
        from .presets import load_presets
        presets = load_presets(Path(presets_dir), experimental_dir=Path(presets_dir).parent / "experimental" / "presets",
                               include_experimental=False)
        enabled = [p.id for p in presets.values() if p.enabled]
        exp = [p.id for p in presets.values() if p.experimental and p.enabled]
        checks.append(Check("presets", "warn" if exp else "ok",
                            f"{len(enabled)} enabled" + (f"; experimental enabled: {', '.join(exp)}" if exp else "")))
        for p in presets.values():
            folder = Path(getattr(config, "convert")) / p.id
            if p.enabled and not folder.exists():
                checks.append(Check(f"preset-folder-{p.id}", "fail", f"missing {folder}"))
        convert_movies = Path(getattr(config, "convert"))
        if convert_movies.exists():
            known = set(presets)
            for child in sorted(convert_movies.iterdir()):
                if child.is_dir() and not child.is_symlink() and child.name not in known:
                    checks.append(Check(f"unknown-preset-{child.name}", "fail",
                                        f"unknown preset folder is silently ignored: {child}"))
    except Exception as exc:
        checks.append(Check("presets", "fail", str(exc)))

    # questions / VERIFY items
    unchecked = _unchecked_questions()
    if unchecked:
        checks.append(Check("verify-items", "warn",
                            f"{len(unchecked)} unchecked on-hardware item(s) in QUESTIONS.md"))
    else:
        checks.append(Check("verify-items", "ok", "no unchecked VERIFY items"))

    # TMDb is optional and doctor never contacts the network.  A token only
    # changes which optional feature is available; reachability is an on-box
    # verification item, not a preflight result.
    token = getattr(secrets, "tmdb_token", None) if secrets is not None else None
    checks.append(Check(
        "tmdb-config", "ok" if token else "warn",
        "TMDb token configured" if token else "no TMDb token; parse-only fallback",
        check_class="informational",
    ))
    checks.append(Check(
        "notification-config", "ok" if getattr(secrets, "notify_url", None) else "warn",
        "notification URL configured" if getattr(secrets, "notify_url", None)
        else "no notification URL; notifications disabled",
        check_class="informational",
    ))

    # experimental folder not created by install
    exp_folder = Path(getattr(config, "convert")) / "1080p-best-x265-mp4"
    if exp_folder.exists():
        checks.append(Check("experimental-folder", "warn",
                            "experimental preset folder exists; ensure it is intentional",
                            check_class="informational"))

    import sys
    checks.append(Check(
        "python-version", "ok" if sys.version_info >= (3, 12) else "fail",
        f"Python {sys.version_info.major}.{sys.version_info.minor} (need >= 3.12)",
    ))

    state_settings = _read_state_settings(Path(getattr(config, "db_path", "")))
    holds = sorted(key for key, value in state_settings.items()
                   if value and key.endswith("_hold"))
    restart_pending = state_settings.get("restart_required", "")
    checks.append(Check(
        "restart-holds", "ok" if not restart_pending and not holds else "warn",
        f"restart_required={restart_pending or 'none'}; holds={','.join(holds) or 'none'}",
        {"restart_required": restart_pending, "holds": holds},
        check_class="informational",
    ))
    checks.append(Check(
        "ffmpeg-change", "ok" if not state_settings.get("ffmpeg_changed_hold", "") else "warn",
        state_settings.get("ffmpeg_changed_hold", "") or "no ffmpeg-change hold",
        check_class="informational",
    ))

    movies = Path(getattr(config, "movies", root / "movies"))
    pipeline = Path(getattr(config, "pipeline", root / "_pipeline"))
    checks.append(Check(
        "library-roots",
        "ok" if movies.exists() and movies.resolve() != pipeline.resolve() else "warn",
        f"movies={movies}; pipeline={pipeline}",
        check_class="informational" if not movies.exists() else "required",
    ))

    unit_paths = [Path("/etc/systemd/system/mediaflow.service"),
                  Path(__file__).resolve().parents[2] / "systemd" / "mediaflow.service"]
    unit_text = ""
    for unit in unit_paths:
        try:
            unit_text = unit.read_text(encoding="utf-8")
            if unit_text:
                break
        except OSError:
            continue
    needed = ("KillMode=", "TimeoutStopSec=", "DeviceAllow=/dev/dri/renderD128 rw",
              "SupplementaryGroups=", "ReadWritePaths=")
    missing = [token for token in needed if token not in unit_text]
    checks.append(Check(
        "systemd-hardening",
        "ok" if unit_text and not missing else ("warn" if not unit_text else "fail"),
        f"unit reviewed; missing={','.join(missing) or 'none'}",
        {"missing": missing},
        check_class="informational" if not unit_text else "required",
    ))

    checks.append(Check(
        "subprocess-audit", "ok",
        "default doctor runs supervised ffmpeg -version only; capability mode adds supervised ffmpeg probes",
        check_class="informational",
    ))
    return _classify_checks(DoctorReport(checks), config, backend_acceptance)


def format_doctor(report: DoctorReport, *, short: bool = False) -> str:
    if short:
        fails = report.required_failures
        unknowns = report.unknowns
        lines = [f"mediaflow doctor: {'FAIL' if fails else 'OK'}",
                 f"checks {len(report.checks)}, unknown {len(unknowns)}"]
        for c in fails[:6]:
            lines.append(f"FAIL {c.name}: {c.detail}"[:60])
        for c in unknowns[:4]:
            lines.append(f"UNKN {c.name}: {c.detail}"[:60])
        return "\n".join(lines[:12])
    marks = {"ok": "PASS", "warn": "UNKN", "fail": "FAIL"}
    out = [f"[{marks[c.status]}] {c.check_class[:4]} {c.name}: {c.detail}"
           for c in report.checks]
    out.append("")
    out.append(
        "doctor: " + ("ok" if report.ok else "problems found")
        + f" (exit {report.exit_code}, {len(report.checks)} checks, "
        f"{len(report.required_failures)} required failures, "
        f"{len(report.feature_failures)} feature failures, "
        f"{len(report.unknowns)} unknown)"
    )
    return "\n".join(out)
