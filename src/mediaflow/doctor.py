"""Doctor: explicit, fail-loud checks of this machine and installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .safety import SafetyError, check_one_filesystem

FFMPEG_DEFAULT = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_DEFAULT = "/usr/lib/jellyfin-ffmpeg/ffprobe"
RENDER_NODE = Path("/dev/dri/renderD128")


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def warnings(self) -> int:
        return sum(c.status == "warn" for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "warnings": self.warnings,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail, "meta": c.meta}
                       for c in self.checks],
        }


def _run(argv: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _ffmpeg_version(ffmpeg: str) -> tuple[str | None, str | None]:
    try:
        proc = _run([ffmpeg, "-version"], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()[:300]
    lines = (proc.stdout or "").splitlines()
    return (lines[0] if lines else "unknown"), None


def _rate_control(stderr: str) -> str:
    text = stderr.lower()
    for name in ("icq", "cqp", "vbr", "cbr", "la_icq", "la_vbr"):
        if name in text:
            return name.upper()
    if "ratecontrolmethod" in text:
        line = next((l for l in stderr.splitlines() if "ratecontrolmethod" in l.lower()), "")
        return line.strip()[:120]
    return "unknown"


def _encode_probe(ffmpeg: str, backend: str, ten_bit: bool, output: Path,
                  quality: int = 24, timeout: float = 90.0) -> dict:
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
    try:
        proc = _run(argv, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "hevc8": False, "hevc10": False,
                "rate_control": "unknown", "error": str(exc)[:300]}
    ok = proc.returncode == 0 and output.exists() and output.stat().st_size > 1000
    return {
        "available": ok,
        "hevc8": ok and not ten_bit,
        "hevc10": ok and ten_bit,
        "rate_control": _rate_control(proc.stderr or ""),
        "error": None if ok else (proc.stderr or "")[-500:],
        "command": [a for a in argv if str(output) not in a][:4] + ["..."],
    }


def run_capability_tests(config: Any, *, ffmpeg: str | None = None,
                         backends: Iterable[str] = ("qsv", "vaapi", "software"),
                         timeout: float = 90.0) -> dict:
    """Run the 8-bit and 10-bit tests and return capabilities.json content.

    The exact shipped-preset option set is represented by quality=24 and
    look-ahead for QSV.  A 10-bit failure is a capability result, not a crash.
    """
    ffmpeg = ffmpeg or str(getattr(config, "ffmpeg", FFMPEG_DEFAULT))
    version, version_error = _ffmpeg_version(ffmpeg)
    results: dict[str, Any] = {
        "ffmpeg_version": version, "version_error": version_error,
        "tested_at": time.time(), "render_node": str(RENDER_NODE),
        "backends": {},
    }
    with tempfile.TemporaryDirectory(prefix="mediaflow-caps-") as td:
        for backend in backends:
            eight = _encode_probe(ffmpeg, backend, False, Path(td) / f"{backend}-8.mp4",
                                  timeout=timeout)
            ten = _encode_probe(ffmpeg, backend, True, Path(td) / f"{backend}-10.mp4",
                                timeout=timeout)
            results["backends"][backend] = {
                "available": bool(eight.get("available") or ten.get("available")),
                "hevc8": bool(eight.get("available")),
                "hevc10": bool(ten.get("available")),
                "rate_control": eight.get("rate_control") if eight.get("available") else ten.get("rate_control"),
                "rate_control_10bit": ten.get("rate_control"),
                "error": eight.get("error") if not eight.get("available") else None,
                "error_10bit": ten.get("error") if not ten.get("available") else None,
            }
            # Keep planner-friendly flat keys used by Engine._choose_backend.
            results[backend] = {
                "hevc8": bool(eight.get("available")),
                "hevc10": bool(ten.get("available")),
                "rate_control": results["backends"][backend]["rate_control"],
            }
    results["ok"] = any(v["hevc8"] for v in results["backends"].values())
    try:
        results["libvmaf"] = any("libvmaf" in line for line in
                                 _run([ffmpeg, "-filters"], timeout=20).stdout.splitlines())
    except Exception:
        results["libvmaf"] = False
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


def run_doctor(config: Any, *, config_path: Path | None = None, presets_dir: Path | None = None,
               secrets: Any = None, run_capabilities: bool = True,
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
    version, verr = _ffmpeg_version(ffmpeg)
    if version:
        checks.append(Check("ffmpeg-version", "ok", version, {"version": version}))
    else:
        checks.append(Check("ffmpeg-version", "fail", verr or "unknown"))

    # render node
    if RENDER_NODE.exists() and os.access(RENDER_NODE, os.R_OK | os.W_OK):
        checks.append(Check("render-node", "ok", str(RENDER_NODE)))
    elif RENDER_NODE.exists():
        checks.append(Check("render-node", "fail", f"not readable/writable by this user: {RENDER_NODE}"))
    else:
        checks.append(Check("render-node", "warn", f"missing {RENDER_NODE}; hardware encode unavailable"))

    # capability tests
    caps = run_capability_tests(config, ffmpeg=ffmpeg) if run_capabilities else {}
    if caps:
        for backend, result in (caps.get("backends") or {}).items():
            status = "ok" if result.get("hevc8") else "warn"
            checks.append(Check(
                f"encode-{backend}", status,
                f"HEVC 8-bit={'yes' if result.get('hevc8') else 'no'}, "
                f"10-bit={'yes' if result.get('hevc10') else 'no'}, "
                f"rate-control={result.get('rate_control') or 'unknown'}",
                result,
            ))
        hardware = {name: result
                    for name, result in (caps.get("backends") or {}).items()
                    if name in {"qsv", "vaapi"}}
        hardware_8bit = any(r.get("hevc8") for r in hardware.values())
        hardware_10bit = any(r.get("hevc10") for r in hardware.values())
        if not hardware_8bit:
            checks.append(Check("hardware-encode", "warn",
                                "no hardware HEVC 8-bit encoder; only MKV remuxes will work"))
        try:
            _write_capabilities(config, caps)
        except OSError:
            checks.append(Check("capabilities-write", "warn", "could not write capabilities.json"))
        checks.append(Check("hevc-10bit", "ok" if hardware_10bit else "warn",
                            "hardware 10-bit HEVC available" if hardware_10bit else
                            "hardware 10-bit HEVC unavailable; 10bit-no-hw REVIEW path active"))

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

    # DB and backup
    try:
        from .db import Store
        store = Store(Path(getattr(config, "db_path")))
        store.integrity_check()
        backup = store.newest_backup(Path(getattr(config, "backup_dir")))
        store.close()
        if backup:
            age = time.time() - backup.stat().st_mtime
            status = "ok" if age < 48 * 3600 else "warn"
            checks.append(Check("db-integrity", "ok", "integrity_check=ok"))
            checks.append(Check("db-backup", status, f"newest backup {age / 3600:.1f}h old"))
        else:
            checks.append(Check("db-integrity", "ok", "integrity_check=ok"))
            checks.append(Check("db-backup", "warn", "no backup yet"))
    except Exception as exc:
        checks.append(Check("db-integrity", "fail", f"refusing to trust state: {exc}"))

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

    # TMDb reachability (optional; token only in header)
    token = getattr(secrets, "tmdb_token", None) if secrets is not None else None
    if not token:
        checks.append(Check("tmdb", "ok", "optional: no token configured; parse-only fallback"))
    else:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.themoviedb.org/3/search/movie?query=test&year=2000",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = 200 <= resp.status < 300
            checks.append(Check("tmdb", "ok" if ok else "warn", "TMDb reachable"))
        except Exception:
            checks.append(Check("tmdb", "warn", "TMDb unreachable; cached/parse-only fallback will be used"))

    # experimental folder not created by install
    exp_folder = Path(getattr(config, "convert")) / "1080p-best-x265-mp4"
    if exp_folder.exists():
        checks.append(Check("experimental-folder", "warn",
                            "experimental preset folder exists; ensure it is intentional"))

    return DoctorReport(checks)


def format_doctor(report: DoctorReport, *, short: bool = False) -> str:
    if short:
        fails = [c for c in report.checks if c.status == "fail"]
        warns = [c for c in report.checks if c.status == "warn"]
        lines = [f"mediaflow doctor: {'FAIL' if fails else 'OK'}",
                 f"checks {len(report.checks)}, warnings {len(warns)}"]
        for c in fails[:6]:
            lines.append(f"FAIL {c.name}: {c.detail}"[:60])
        for c in warns[:4]:
            lines.append(f"WARN {c.name}: {c.detail}"[:60])
        return "\n".join(lines[:12])
    out = []
    for c in report.checks:
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[c.status]
        out.append(f"[{mark}] {c.name}: {c.detail}")
    out.append("")
    out.append("doctor: " + ("ok" if report.ok else "problems found") +
               f" ({len(report.checks)} checks, {report.warnings} warnings)")
    return "\n".join(out)
