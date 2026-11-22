"""Doctor checks and fail-closed behaviour."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaflow.config import load_config
from mediaflow.doctor import (
    DoctorReport,
    Check,
    format_doctor,
    run_capability_tests,
    run_doctor,
)
from mediaflow.supervision import (
    OperationState,
    ProcessResult,
    ProtectionAction,
    ProtectionDecision,
    SlotBusy,
    processing_slot,
    slot_path,
)


def _setup(tmp_path):
    etc = tmp_path / "etc"
    (etc / "presets.d").mkdir(parents=True)
    for p in (Path(__file__).resolve().parents[1] / "presets").glob("*.toml"):
        shutil.copy(p, etc / "presets.d" / p.name)
    path = etc / "config.toml"
    path.write_text(f'''root = "{tmp_path / "media"}"
state_dir = "{tmp_path / "state"}"
runtime_dir = "{tmp_path / "run"}"
dry_run = true
pause_on_battery = false
temp_sensor = "none"
min_free_gb = 1
''')
    cfg = load_config(path, environ={})
    cfg.ensure_directories()
    for preset in ("keep-remux-mp4", "keep-remux-mkv", "1080p-balanced-mp4",
                   "1080p-small-mp4", "720p-small-mp4"):
        (cfg.convert / preset).mkdir(parents=True, exist_ok=True)
    from mediaflow.db import Store
    store = Store(cfg.db_path)
    store.initialize()
    backup = store.backup(cfg.backup_dir)
    store.close()
    return path, cfg


def test_doctor_reports_capabilities_and_rate_control(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    caps = {
        "ok": True, "ffmpeg_version": "ffmpeg test", "libvmaf": True,
        "backends": {
            "qsv": {"available": True, "hevc8": True, "hevc10": False,
                    "rate_control": "ICQ", "error": None, "error_10bit": "unsupported"},
            "vaapi": {"available": True, "hevc8": True, "hevc10": True,
                      "rate_control": "CQP", "error": None, "error_10bit": None},
        },
        "qsv": {"hevc8": True, "hevc10": False}, "vaapi": {"hevc8": True, "hevc10": True},
    }
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests", lambda *a, **k: caps)
    monkeypatch.setattr("mediaflow.doctor.os.access", lambda *a, **k: True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("- [x] resolved\n")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    assert isinstance(report, DoctorReport)
    names = {c.name: c for c in report.checks}
    assert names["encode-qsv"].status == "ok"
    assert "10-bit=no" in names["encode-qsv"].detail
    assert "ICQ" in names["encode-qsv"].detail
    assert names["encode-vaapi"].status == "ok"
    assert report.ok, [c for c in report.checks if c.status == "fail"]
    assert json.loads(cfg.capabilities_path.read_text())["backends"]["vaapi"]["hevc10"] is True
    short = format_doctor(report, short=True)
    assert len(short.splitlines()) <= 12
    assert all(len(line) <= 60 for line in short.splitlines())


def test_doctor_does_not_call_software_10bit_hardware(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    caps = {
        "ok": True, "ffmpeg_version": "ffmpeg test", "libvmaf": False,
        "backends": {
            "qsv": {"available": False, "hevc8": False, "hevc10": False,
                    "rate_control": "unknown", "error": "no device"},
            "vaapi": {"available": False, "hevc8": False, "hevc10": False,
                      "rate_control": "unknown", "error": "no device"},
            "software": {"available": True, "hevc8": True, "hevc10": True,
                         "rate_control": "unknown"},
        },
        "software": {"hevc8": True, "hevc10": True},
    }
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests", lambda *a, **k: caps)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    names = {c.name: c for c in report.checks}
    assert names["hevc-10bit"].status == "warn"
    assert names["hardware-encode"].status == "warn"


def test_doctor_fails_closed_without_temp_sensor(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    # Force the sensor path back to auto and simulate a missing coretemp hwmon.
    object.__setattr__(cfg, "temp_sensor", "auto")
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests",
                        lambda *a, **k: {"ok": True, "backends": {}, "ffmpeg_version": "x"})
    monkeypatch.setattr("mediaflow.guards.discover_temp_sensor", lambda cfg: None)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    sensor = next(c for c in report.checks if c.name == "temp-sensor")
    assert sensor.status == "fail"
    assert not report.ok


def test_doctor_flags_unknown_preset_folder(tmp_path, monkeypatch):
    path, cfg = _setup(tmp_path)
    (cfg.convert / "mystery-preset").mkdir()
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests",
                        lambda *a, **k: {"ok": True, "backends": {}, "ffmpeg_version": "x"})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))
    report = run_doctor(cfg, config_path=path, presets_dir=path.parent / "presets.d")
    assert any(c.name == "unknown-preset-mystery-preset" and c.status == "fail"
               for c in report.checks)


# ---------------------------------------------------------------------------
# bounded capability supervision and tri-state facts
# ---------------------------------------------------------------------------


def _assert_slot_busy(config) -> None:
    with pytest.raises(SlotBusy):
        with processing_slot(slot_path(config), timeout=0):
            pass


def _successful_process(stdout: str = "") -> ProcessResult:
    return ProcessResult(
        state=OperationState.SUCCEEDED,
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def test_machine_protection_defers_capability_tests_without_probing(
    tmp_path, monkeypatch
):
    """SUP-3: an incomplete capability result never becomes unsupported."""

    _path, config = _setup(tmp_path)
    monkeypatch.setattr(
        "mediaflow.doctor._ffmpeg_version_process",
        lambda *a, **k: pytest.fail("version was probed after protection deferral"),
    )
    cancel = threading.Event()
    pause = threading.Event()
    monitor = lambda: ProtectionDecision(
        ProtectionAction.DEFER,
        ("on-battery", "thermal"),
        detail="machine protection deferred capability tests",
    )

    result = run_capability_tests(
        config,
        cancel_event=cancel,
        pause_event=pause,
        monitor=monitor,
    )

    assert result["status"] == "deferred"
    assert result["state"] == OperationState.DEFERRED.value
    assert result["completed"] is False
    assert result["ok"] is False
    assert result["reasons"] == ["on-battery", "thermal"]
    assert result["backends"] == {}
    assert not cancel.is_set(), "capability tests must not mutate caller cancellation"
    assert not pause.is_set(), "capability tests must not mutate caller pause state"


def test_a_busy_processing_slot_prevents_every_capability_probe(
    tmp_path, monkeypatch
):
    """SUP-2: a busy slot is a deferral, never a false unsupported result."""

    _path, config = _setup(tmp_path)
    monkeypatch.setattr(
        "mediaflow.doctor._ffmpeg_version_process",
        lambda *a, **k: pytest.fail("version was probed while the slot was busy"),
    )
    monkeypatch.setattr(
        "mediaflow.doctor._encode_probe",
        lambda *a, **k: pytest.fail("backend was probed while the slot was busy"),
    )

    with processing_slot(slot_path(config), timeout=0):
        result = run_capability_tests(config, backends=("software",))

    assert result["status"] == "deferred"
    assert result["state"] == OperationState.DEFERRED.value
    assert result["completed"] is False
    assert result["reasons"] == ["processing-slot-busy"]
    assert result["backends"] == {}


def test_capability_probes_and_filter_check_share_the_processing_slot(
    tmp_path, monkeypatch
):
    """SUP-2: the slot covers version, backend trials and filter-list probe."""

    _path, config = _setup(tmp_path)
    phases: list[str] = []

    def require_slot(phase: str) -> None:
        _assert_slot_busy(config)
        phases.append(phase)

    def fake_version(*args, **kwargs):
        require_slot("version")
        return _successful_process("ffmpeg test\n"), "ffmpeg test", None

    def fake_encode(_ffmpeg, _backend, ten_bit, _output, **_kwargs):
        require_slot(f"encode-{10 if ten_bit else 8}")
        return {
            "available": True,
            "hevc8": not ten_bit,
            "hevc10": ten_bit,
            "rate_control": "CQP",
            "error": None,
            "state": OperationState.SUCCEEDED.value,
            "reasons": [],
            "timeout_type": None,
            "spawn_failed": False,
            "incomplete": False,
            "command": ["stub"],
        }

    def fake_filters(*args, **kwargs):
        require_slot("filters")
        return _successful_process("... libvmaf ...\n")

    monkeypatch.setattr("mediaflow.doctor._ffmpeg_version_process", fake_version)
    monkeypatch.setattr("mediaflow.doctor._encode_probe", fake_encode)
    monkeypatch.setattr("mediaflow.doctor._run_supervised", fake_filters)

    result = run_capability_tests(config, backends=("software",))

    assert result["completed"] is True
    assert result["libvmaf"] is True
    assert phases == ["version", "encode-8", "encode-10", "filters"]


def test_incomplete_first_probe_does_not_start_the_second_probe(
    tmp_path, monkeypatch
):
    """An unknown 8-bit fact cannot justify a later 10-bit conclusion."""

    _path, config = _setup(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(
        "mediaflow.doctor._ffmpeg_version_process",
        lambda *a, **k: (_successful_process("ffmpeg test\n"), "ffmpeg test", None),
    )

    def fake_encode(_ffmpeg, _backend, ten_bit, _output, **_kwargs):
        calls.append(ten_bit)
        return {
            "available": None,
            "hevc8": None,
            "hevc10": None,
            "rate_control": "unknown",
            "error": "probe timed out",
            "state": OperationState.TIMED_OUT.value,
            "reasons": ["capability-timeout"],
            "timeout_type": "overall",
            "spawn_failed": False,
            "incomplete": True,
            "command": ["stub"],
        }

    monkeypatch.setattr("mediaflow.doctor._encode_probe", fake_encode)

    result = run_capability_tests(config, backends=("software",))

    assert calls == [False], "the 10-bit probe must not follow an incomplete 8-bit probe"
    assert result["status"] == "timed-out"
    assert result["state"] == OperationState.TIMED_OUT.value
    assert result["timeout_type"] == "overall"
    assert result["completed"] is False
    assert result["backends"]["software"]["hevc8"] is None
    assert result["backends"]["software"]["hevc10"] is None


def test_completed_eight_bit_fact_survives_a_later_timeout(
    tmp_path, monkeypatch
):
    """A completed fact stays valid when the following probe is incomplete."""

    _path, config = _setup(tmp_path)
    monkeypatch.setattr(
        "mediaflow.doctor._ffmpeg_version_process",
        lambda *a, **k: (_successful_process("ffmpeg test\n"), "ffmpeg test", None),
    )

    def fake_encode(_ffmpeg, _backend, ten_bit, _output, **_kwargs):
        if ten_bit:
            return {
                "available": None,
                "hevc8": None,
                "hevc10": None,
                "rate_control": "unknown",
                "error": "10-bit probe timed out",
                "state": OperationState.TIMED_OUT.value,
                "reasons": ["capability-timeout"],
                "timeout_type": "overall",
                "spawn_failed": False,
                "incomplete": True,
                "command": ["stub"],
            }
        return {
            "available": True,
            "hevc8": True,
            "hevc10": False,
            "rate_control": "CQP",
            "error": None,
            "state": OperationState.SUCCEEDED.value,
            "reasons": [],
            "timeout_type": None,
            "spawn_failed": False,
            "incomplete": False,
            "command": ["stub"],
        }

    monkeypatch.setattr("mediaflow.doctor._encode_probe", fake_encode)

    result = run_capability_tests(config, backends=("software",))

    assert result["status"] == "timed-out"
    assert result["timeout_type"] == "overall"
    assert result["completed"] is False
    assert result["backends"]["software"]["hevc8"] is True
    assert result["backends"]["software"]["hevc10"] is None
    assert result["backends"]["software"]["available"] is True


def test_spawn_failure_leaves_capability_facts_unknown(
    tmp_path, monkeypatch
):
    """A process that cannot start is unknown, not unsupported."""

    _path, config = _setup(tmp_path)
    monkeypatch.setattr(
        "mediaflow.doctor._ffmpeg_version_process",
        lambda *a, **k: (_successful_process("ffmpeg test\n"), "ffmpeg test", None),
    )
    monkeypatch.setattr(
        "mediaflow.doctor._encode_probe",
        lambda *a, **k: {
            "available": None,
            "hevc8": None,
            "hevc10": None,
            "rate_control": "unknown",
            "error": "could not start process",
            "state": OperationState.FAILED.value,
            "reasons": [],
            "timeout_type": None,
            "spawn_failed": True,
            "incomplete": True,
            "command": ["stub"],
        },
    )

    result = run_capability_tests(config, backends=("software",))

    assert result["status"] == "failed"
    assert result["completed"] is False
    assert result["backends"]["software"]["hevc8"] is None
    assert result["backends"]["software"]["hevc10"] is None
    assert result["backends"]["software"]["available"] is None


def test_incomplete_capability_report_does_not_overwrite_valid_evidence(
    tmp_path, monkeypatch
):
    """A deferred/timed-out report cannot replace a valid capability file."""

    path, config = _setup(tmp_path)
    valid = {
        "ffmpeg_version": "known good version",
        "libvmaf": True,
        "backends": {"qsv": {"hevc8": True, "hevc10": False}},
    }
    config.capabilities_path.write_text(json.dumps(valid), encoding="utf-8")
    before = config.capabilities_path.read_bytes()
    incomplete = {
        "status": "timed-out",
        "state": OperationState.TIMED_OUT.value,
        "completed": False,
        "ok": False,
        "deferred": False,
        "reasons": ["capability-timeout"],
        "timeout_type": "overall",
        "ffmpeg_version": None,
        "version_error": "probe timed out",
        "libvmaf": None,
        "backends": {
            "qsv": {
                "available": None,
                "hevc8": None,
                "hevc10": None,
                "rate_control": "unknown",
            }
        },
    }
    monkeypatch.setattr("mediaflow.doctor.run_capability_tests", lambda *a, **k: incomplete)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUESTIONS.md").write_text("")
    monkeypatch.setenv("MEDIAFLOW_DOCS", str(docs))

    report = run_doctor(config, config_path=path, presets_dir=path.parent / "presets.d")

    assert config.capabilities_path.read_bytes() == before
    assert json.loads(config.capabilities_path.read_text()) == valid
    state = next(c for c in report.checks if c.name == "capability-state")
    assert state.status == "warn"
    assert state.meta["state"] == OperationState.TIMED_OUT.value
