"""Tests for the fail-closed machine and space protection gates.

Covers [SAF-8] free space and trash, [SAF-9] sensor/AC/Jellyfin/thermal
protection, and [SCHED-1] schedule window start gates.  Every test uses fake
sysfs, fake procfs, injected disk usage, and explicit dates; none reads the
host's real sensors or relies on the host's power state.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaflow import guards
from mediaflow.guards import (
    GIB,
    GuardConfig,
    GuardDecision,
    ac_should_pause,
    ac_should_resume,
    check_space,
    discover_temp_sensor,
    estimate_fits,
    jellyfin_transcoding,
    mid_encode_gate,
    read_ac_state,
    read_package_temp,
    start_gate,
    thermal_should_pause,
    thermal_should_resume,
    window_close,
    window_contains,
)


class FakeDisk:
    """Inject disk free bytes per path; records every path queried."""

    def __init__(self, free: int = 100 * GIB, overrides: dict[Path, int] | None = None):
        self.free = free
        self.overrides = {Path(path): value for path, value in (overrides or {}).items()}
        self.calls: list[Path] = []

    def __call__(self, path):
        path = Path(path)
        self.calls.append(path)
        value = self.overrides.get(path, self.free)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(total=value, used=0, free=value)


@pytest.fixture
def cfg(tmp_path: Path) -> GuardConfig:
    sysfs = tmp_path / "sys"
    (sysfs / "class" / "hwmon").mkdir(parents=True)
    (sysfs / "class" / "power_supply").mkdir(parents=True)
    proc = tmp_path / "proc"
    proc.mkdir()
    return GuardConfig(
        min_free_gb=40.0,
        root_min_free_gb=2.0,
        max_trash_gb=120.0,
        pause_when_jellyfin_transcoding=True,
        pause_on_battery=True,
        temp_sensor="none",
        start_if_temp_below_c=65.0,
        thermal_pause_c=85.0,
        thermal_resume_c=75.0,
        thermal_sustain_seconds=60.0,
        jellyfin_user="1000",
        sysfs_root=sysfs,
        proc_root=proc,
        filesystem_root=tmp_path,
        db_dir=tmp_path / "db",
        trash=tmp_path / "trash",
        pipeline=tmp_path / "pipeline",
        library=tmp_path / "library",
        run_dir=tmp_path / "run",
    )


def set_ac(cfg: GuardConfig, *states: int) -> None:
    base = cfg.sysfs_root / "class" / "power_supply"
    for index, state in enumerate(states):
        supply = base / f"AC{index}"
        supply.mkdir(parents=True, exist_ok=True)
        (supply / "online").write_text(f"{state}\n")


def add_hwmon(
    cfg: GuardConfig,
    index: int,
    name: str,
    inputs: dict[int, int | float | str],
    labels: dict[int, str] | None = None,
) -> Path:
    hwmon = cfg.sysfs_root / "class" / "hwmon" / f"hwmon{index}"
    hwmon.mkdir(parents=True)
    (hwmon / "name").write_text(f"{name}\n")
    for channel, value in inputs.items():
        (hwmon / f"temp{channel}_input").write_text(f"{value}\n")
    for channel, label in (labels or {}).items():
        (hwmon / f"temp{channel}_label").write_text(f"{label}\n")
    return hwmon


def add_process(
    cfg: GuardConfig,
    pid: int,
    uid: int,
    argv: list[str],
    *,
    name: str = "ffmpeg",
) -> None:
    process = cfg.proc_root / str(pid)
    process.mkdir()
    (process / "status").write_text(
        f"Name:\t{name}\nState:\tS\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
    )
    (process / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


# ---------------------------------------------------------------------------
# Decisions and config


def test_decision_normalises_and_adapter_keeps_defaults():
    decision = GuardDecision(ok=False, reasons=["space-low"], messages=["no space"])
    assert decision.ok is False
    assert decision.reasons == ("space-low",)
    assert decision.messages == ("no space",)

    adapted = GuardConfig.from_config(
        {
            "min_free_gb": "55",
            "temp_sensor": "auto",
            "trash": "/tmp/mediaflow-trash",
            "paths": {"pipeline": "/tmp/mediaflow-pipeline"},
        }
    )
    assert adapted.min_free_gb == 55.0
    assert adapted.temp_sensor == "auto"
    assert adapted.trash == Path("/tmp/mediaflow-trash")
    assert adapted.pipeline == Path("/tmp/mediaflow-pipeline")
    assert adapted.root_min_free_gb == 2.0
    assert adapted.sysfs_root == Path("/sys")

    duck = GuardConfig.from_config(
        SimpleNamespace(min_free_gb=17, paths=SimpleNamespace(run_dir="/tmp/run"))
    )
    assert duck.min_free_gb == 17.0
    assert duck.run_dir == Path("/tmp/run")
    assert duck.proc_root == Path("/proc")

    class ProductionShape:
        root = Path("/srv/media")
        state_dir = Path("/srv/state")
        runtime_dir = Path("/srv/run")
        min_free_gb = 33.0

        @property
        def db_path(self) -> Path:
            return self.state_dir / "state.db"

        @property
        def pipeline(self) -> Path:
            return self.root / "_pipeline"

        @property
        def trash(self) -> Path:
            return self.pipeline / "trash"

        @property
        def library(self) -> Path:
            return self.root / "movies"

    production = GuardConfig.from_config(ProductionShape())
    assert production.min_free_gb == 33.0
    assert production.db_dir == Path("/srv/state")
    assert production.run_dir == Path("/srv/run")
    assert production.pipeline == Path("/srv/media/_pipeline")
    assert production.trash == Path("/srv/media/_pipeline/trash")
    assert production.library == Path("/srv/media/movies")


# ---------------------------------------------------------------------------
# [SAF-8] Space and trash


def test_space_reports_values_and_counts_trash_without_following_links(cfg: GuardConfig):
    cfg.trash.mkdir()
    (cfg.trash / "a").mkdir()
    (cfg.trash / "a" / "one.bin").write_bytes(b"12345")
    (cfg.trash / "two.bin").write_bytes(b"1234567")
    precious = cfg.trash.parent / "precious.bin"
    precious.write_bytes(b"x" * 1000)
    (cfg.trash / "link-to-precious").symlink_to(precious)

    disk = FakeDisk(
        overrides={
            cfg.pipeline: 100 * GIB,
            cfg.filesystem_root: 50 * GIB,
            cfg.db_dir: 50 * GIB,
        }
    )
    status, decision = check_space(cfg, disk_usage=disk)

    assert decision.ok is True
    assert decision.reasons == ()
    assert status.free_bytes == 100 * GIB
    assert status.required_bytes == 40 * GIB
    assert status.trash_bytes == 12  # the symlink target is not followed
    assert status.root_free_bytes == 50 * GIB
    assert status.db_free_bytes == 50 * GIB
    assert status.decision is decision
    assert (cfg.trash / "a" / "one.bin").exists()
    assert (cfg.trash / "two.bin").exists()
    assert precious.exists()


def test_source_size_dominates_required_space(cfg: GuardConfig):
    required = math.ceil(1.2 * (100 * GIB))
    disk = FakeDisk(
        free=100 * GIB,
        overrides={cfg.pipeline: required - 1},
    )
    status, decision = check_space(cfg, source_size=100 * GIB, disk_usage=disk)
    assert status.required_bytes == required
    assert "space-low" in decision.reasons
    assert decision.ok is False

    disk_ok = FakeDisk(free=100 * GIB, overrides={cfg.pipeline: required})
    _status_ok, decision_ok = check_space(
        cfg, source_size=100 * GIB, disk_usage=disk_ok
    )
    assert decision_ok.ok is True


def test_mid_encode_uses_half_the_minimum_reserve(cfg: GuardConfig):
    disk = FakeDisk(
        free=100 * GIB,
        overrides={cfg.pipeline: 20 * GIB},
    )
    before_status, before = check_space(cfg, disk_usage=disk)
    mid_status, mid = check_space(cfg, mid_encode=True, disk_usage=disk)

    assert before_status.required_bytes == 40 * GIB
    assert before.reasons == ("space-low",)
    assert mid_status.required_bytes == 20 * GIB
    assert mid.ok is True


def test_root_and_db_filesystems_are_checked(cfg: GuardConfig):
    other_db = cfg.db_dir.parent / "explicit-db"
    disk = FakeDisk(
        free=100 * GIB,
        overrides={
            cfg.pipeline: 100 * GIB,
            cfg.filesystem_root: 1 * GIB,
            other_db: 0,
        },
    )
    status, decision = check_space(cfg, db_path=other_db, disk_usage=disk)

    assert decision.reasons == ("root-space-low", "db-space-low")
    assert status.root_free_bytes == 1 * GIB
    assert status.db_free_bytes == 0
    assert other_db in disk.calls


def test_trash_over_limit_pauses_and_is_never_purged(cfg: GuardConfig):
    cfg.max_trash_gb = 0.000001  # about 1 KiB
    cfg.trash.mkdir()
    big = cfg.trash / "big.bin"
    big.write_bytes(b"x" * 4096)

    disk = FakeDisk()
    status, decision = check_space(cfg, disk_usage=disk)

    assert "trash-too-big" in decision.reasons
    assert decision.ok is False
    assert status.trash_bytes == 4096
    assert big.exists()
    assert big.read_bytes() == b"x" * 4096


def test_unknown_disk_usage_fails_closed(cfg: GuardConfig):
    def broken(_path):
        raise OSError("cannot stat")

    status, decision = check_space(cfg, disk_usage=broken)
    assert decision.reasons == ("space-low", "root-space-low", "db-space-low")
    assert status.free_bytes == 0
    assert status.root_free_bytes == 0
    assert status.db_free_bytes == 0


def test_disk_usage_may_be_a_plain_tuple(cfg: GuardConfig):
    def usage(_path):
        return (100 * GIB, 0, 100 * GIB)

    _status, decision = check_space(cfg, disk_usage=usage)
    assert decision.ok is True, decision.reasons


def test_unmeasurable_trash_fails_closed(cfg: GuardConfig, monkeypatch):
    cfg.trash.mkdir()
    (cfg.trash / "content.bin").write_bytes(b"x")

    def broken_scandir(_path):
        raise OSError("cannot read trash")

    monkeypatch.setattr(guards.os, "scandir", broken_scandir)
    _status, decision = check_space(cfg, disk_usage=FakeDisk())
    assert "trash-too-big" in decision.reasons
    assert decision.ok is False


# ---------------------------------------------------------------------------
# [SAF-9] Temperature discovery and reads


def test_sensor_discovered_by_hwmon_name_not_index(cfg: GuardConfig):
    cfg.temp_sensor = "auto"
    # A different driver at the first index must not win.
    add_hwmon(
        cfg,
        0,
        "k10temp",
        {1: 99000},
        {1: "Package id 0"},
    )
    add_hwmon(
        cfg,
        3,
        "coretemp",
        {1: 70000, 2: 55000},
        {1: "Package id 1", 2: "Package id 0"},
    )

    sensor = discover_temp_sensor(cfg)

    assert sensor is not None
    assert sensor.name == "coretemp"
    assert sensor.hwmon_dir.name == "hwmon3"
    assert sensor.temp_input is not None
    assert sensor.temp_input.name == "temp2_input"
    assert sensor.label == "Package id 0"
    assert sensor.kind == "coretemp"
    assert read_package_temp(sensor) == pytest.approx(55.0)
    assert Path(sensor.to_json()["temp_input"]) == sensor.temp_input


def test_sensor_fallback_to_temp1_warns(cfg: GuardConfig, caplog):
    cfg.temp_sensor = "auto"
    add_hwmon(cfg, 4, "coretemp", {1: 62000})
    caplog.set_level(logging.WARNING, logger="mediaflow.guards")

    sensor = discover_temp_sensor(cfg)

    assert sensor is not None
    assert sensor.temp_input is not None
    assert sensor.temp_input.name == "temp1_input"
    assert read_package_temp(sensor) == pytest.approx(62.0)
    assert any("no Package label" in record.message for record in caplog.records)


def test_missing_sensor_fails_closed(cfg: GuardConfig):
    cfg.temp_sensor = "auto"
    assert discover_temp_sensor(cfg) is None

    decision = start_gate(cfg, source_size=0, disk_usage=FakeDisk())
    assert "temp-sensor-missing" in decision.reasons
    assert decision.ok is False
    assert len(decision.messages) == len(decision.reasons)


def test_temp_sensor_none_is_explicitly_disabled(cfg: GuardConfig):
    disabled = discover_temp_sensor(cfg)
    assert disabled is not None
    assert disabled.disabled is True
    assert disabled.kind == "none"
    set_ac(cfg, 1)

    decision = start_gate(cfg, source_size=0, disk_usage=FakeDisk())
    assert decision.ok is True, decision.reasons
    assert decision.reasons == ()

    # An explicitly disabled SensorInfo is also an explicit opt-out, even if
    # the config would otherwise say auto.
    cfg.temp_sensor = "auto"
    assert start_gate(
        cfg, source_size=0, temp_sensor=disabled, disk_usage=FakeDisk()
    ).ok is True


@pytest.mark.parametrize(
    ("millidegrees", "expected_ok"),
    [(64999, True), (65000, False)],
)
def test_start_temperature_threshold(
    cfg: GuardConfig, millidegrees: int, expected_ok: bool
):
    cfg.temp_sensor = "auto"
    add_hwmon(cfg, 1, "coretemp", {1: millidegrees}, {1: "Package id 0"})
    set_ac(cfg, 1)
    sensor = discover_temp_sensor(cfg)
    assert sensor is not None

    decision = start_gate(
        cfg, source_size=0, temp_sensor=sensor, disk_usage=FakeDisk()
    )
    assert decision.ok is expected_ok
    if expected_ok:
        assert decision.reasons == ()
    else:
        assert decision.reasons == ("too-hot",)


def test_invalid_temperature_is_treated_as_missing(cfg: GuardConfig):
    path = cfg.sysfs_root / "class" / "hwmon" / "hwmon9"
    path.mkdir(parents=True)
    (path / "name").write_text("coretemp\n")
    (path / "temp1_input").write_text("not-a-number\n")
    cfg.temp_sensor = "auto"
    sensor = discover_temp_sensor(cfg)
    assert sensor is not None
    assert read_package_temp(sensor) is None
    assert read_package_temp(None) is None


# ---------------------------------------------------------------------------
# [SAF-9] AC power


def test_no_power_supply_fails_closed(cfg: GuardConfig):
    assert read_ac_state(cfg) is None
    decision = start_gate(cfg, source_size=0, disk_usage=FakeDisk())
    assert decision.reasons == ("no-ac-source",)


def test_any_online_supply_counts_as_ac(cfg: GuardConfig):
    set_ac(cfg, 0, 1)
    assert read_ac_state(cfg) is True
    assert start_gate(cfg, source_size=0, disk_usage=FakeDisk()).ok is True


def test_battery_pauses(cfg: GuardConfig):
    set_ac(cfg, 0)
    assert read_ac_state(cfg) is False
    decision = start_gate(cfg, source_size=0, disk_usage=FakeDisk())
    assert decision.reasons == ("on-battery",)


def test_pause_on_battery_can_be_disabled(cfg: GuardConfig):
    cfg.pause_on_battery = False
    assert read_ac_state(cfg) is None
    assert start_gate(cfg, source_size=0, disk_usage=FakeDisk()).ok is True
    assert mid_encode_gate(cfg, source_size=0, disk_usage=FakeDisk()).ok is True


def test_ac_helpers_and_accept_both_argument_orders(cfg: GuardConfig):
    assert ac_should_pause(False, cfg) is True
    assert ac_should_pause(None, cfg) is True
    assert ac_should_pause(True, cfg) is False
    assert ac_should_pause(1, cfg) is False
    assert ac_should_pause("0", cfg) is True
    assert ac_should_pause(cfg, False) is True
    assert ac_should_resume(True, cfg) is True
    assert ac_should_resume(False, cfg) is False

    cfg.pause_on_battery = False
    assert ac_should_pause(None, cfg) is False
    assert ac_should_resume(None, cfg) is True


# ---------------------------------------------------------------------------
# [SAF-9] Jellyfin transcode detection


def test_jellyfin_ffmpeg_process_is_detected(cfg: GuardConfig):
    add_process(
        cfg,
        123,
        1000,
        ["/usr/lib/jellyfin-ffmpeg/ffmpeg", "-i", "/media/movie.mkv"],
    )
    assert jellyfin_transcoding(cfg) is True

    set_ac(cfg, 1)
    decision = start_gate(cfg, source_size=0, disk_usage=FakeDisk())
    assert decision.reasons == ("jellyfin-transcoding",)


def test_jellyfin_without_ffmpeg_is_not_a_transcode(cfg: GuardConfig):
    add_process(cfg, 124, 1000, ["/usr/bin/jellyfin", "--datadir", "/var/lib/jellyfin"])
    assert jellyfin_transcoding(cfg) is False


def test_ffmpeg_owned_by_another_user_is_ignored(cfg: GuardConfig):
    add_process(cfg, 125, 1001, ["/usr/bin/ffmpeg", "-i", "input.mkv"])
    assert jellyfin_transcoding(cfg) is False


def test_jellyfin_username_is_resolved(cfg: GuardConfig, monkeypatch):
    cfg.jellyfin_user = "jellyfin"
    add_process(cfg, 127, 4242, ["/usr/bin/ffmpeg", "-i", "input.mkv"])
    monkeypatch.setattr(
        guards.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=4242)
    )
    assert jellyfin_transcoding(cfg) is True


def test_proc_unavailable_fails_closed_unless_disabled(cfg: GuardConfig):
    cfg.proc_root = cfg.proc_root.parent / "missing-proc"
    assert jellyfin_transcoding(cfg) is True

    cfg.pause_when_jellyfin_transcoding = False
    assert jellyfin_transcoding(cfg) is False


def test_permission_error_on_status_is_ignored(cfg: GuardConfig, monkeypatch):
    add_process(cfg, 126, 1000, ["/usr/bin/ffmpeg", "-i", "input.mkv"])
    original = Path.read_text

    def deny_status(self, *args, **kwargs):
        if self.name == "status":
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_status)
    assert jellyfin_transcoding(cfg) is False


# ---------------------------------------------------------------------------
# [SCHED-1] Windows and estimates


def test_window_contains_daytime():
    window = ("09:00", "17:00")
    assert window_contains(window, datetime(2026, 1, 5, 9, 0)) is True
    assert window_contains(window, datetime(2026, 1, 5, 16, 59)) is True
    assert window_contains(window, datetime(2026, 1, 5, 8, 59)) is False
    assert window_contains(window, datetime(2026, 1, 5, 17, 0)) is False
    assert window_contains(None, datetime(2026, 1, 5, 3, 0)) is True


def test_window_contains_overnight():
    window = {"start": time(22, 0), "end": time(7, 0)}
    assert window_contains(window, datetime(2026, 1, 5, 23, 0)) is True
    assert window_contains(window, datetime(2026, 1, 6, 2, 0)) is True
    assert window_contains(window, datetime(2026, 1, 6, 6, 59)) is True
    assert window_contains(window, datetime(2026, 1, 6, 7, 0)) is False
    assert window_contains(window, datetime(2026, 1, 6, 12, 0)) is False
    assert window_contains("22:00-07:00", "23:30") is True


def test_window_accepts_preset_minutes_and_equal_endpoints_never_open():
    # presets.parse_window() returns minutes since midnight.
    window = (60, 420)  # 01:00-07:00
    assert window_contains(window, datetime(2026, 1, 5, 2, 0)) is True
    assert window_contains(window, datetime(2026, 1, 5, 7, 0)) is False
    assert window_contains((420, 420), datetime(2026, 1, 5, 7, 0)) is False
    assert window_contains((420, 420), datetime(2026, 1, 5, 12, 0)) is False


def test_window_close_handles_overnight():
    window = ("22:00", "07:00")
    assert window_close(window, datetime(2026, 1, 5, 23, 30)) == datetime(
        2026, 1, 6, 7, 0
    )
    assert window_close(window, datetime(2026, 1, 6, 2, 0)) == datetime(
        2026, 1, 6, 7, 0
    )
    assert window_close(None, datetime(2026, 1, 5, 23, 30)) is None


def test_estimate_fits_before_window_close():
    window = ("22:00", "07:00")
    now = datetime(2026, 1, 5, 22, 0)
    assert estimate_fits(8 * 3600, window, now) is True
    assert estimate_fits(9 * 3600, window, now) is False
    assert estimate_fits(None, window, now) is False
    assert estimate_fits(None, None, now) is True


def test_start_gate_accepts_preset_window_string(cfg: GuardConfig):
    set_ac(cfg, 1)
    decision = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=60,
        window="01:00-07:00",
        now=datetime(2026, 1, 5, 2, 0),
        disk_usage=FakeDisk(),
    )
    assert decision.ok is True, decision.reasons


def test_start_gate_schedule_cold_start_exceeds_and_outside(cfg: GuardConfig):
    set_ac(cfg, 1)
    disk = FakeDisk()
    window = ("22:00", "07:00")

    outside = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=60,
        window=window,
        now=datetime(2026, 1, 6, 12, 0),
        disk_usage=disk,
    )
    assert outside.reasons == ("outside-window",)

    cold = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=60,
        window=window,
        now=datetime(2026, 1, 5, 23, 0),
        speed_known=False,
        disk_usage=disk,
    )
    assert cold.reasons == ("estimate-cold-start",)

    too_long = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=9 * 3600,
        window=window,
        now=datetime(2026, 1, 5, 23, 0),
        disk_usage=disk,
    )
    assert too_long.reasons == ("estimate-exceeds-window",)

    fits = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=7 * 3600,
        window=window,
        now=datetime(2026, 1, 5, 23, 0),
        disk_usage=disk,
    )
    assert fits.ok is True, fits.reasons

    unscheduled = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=None,
        window=None,
        speed_known=False,
        disk_usage=disk,
    )
    assert unscheduled.ok is True


# ---------------------------------------------------------------------------
# [SAF-9] Thermal hysteresis and no process signalling


def test_thermal_pause_is_sustained_and_resume_uses_hysteresis(cfg: GuardConfig):
    assert thermal_should_pause(84.9, cfg, above_since=3600) is False
    assert thermal_should_pause(85.0, cfg, above_since=59) is False
    assert thermal_should_pause(85.0, cfg, above_since=timedelta(seconds=59)) is False
    assert thermal_should_pause(85.0, cfg, above_since=60) is True
    assert thermal_should_pause(90.0, cfg, above_since=timedelta(seconds=61)) is True
    assert thermal_should_pause(90.0, cfg, above_since=None) is False
    assert thermal_should_pause(None, cfg, above_since=3600) is False

    assert thermal_should_resume(75.0, cfg) is True
    assert thermal_should_resume(75.1, cfg) is False
    assert thermal_should_resume(None, cfg) is False


def test_thermal_helpers_never_signal_processes(cfg: GuardConfig, monkeypatch):
    calls: list[tuple] = []

    def fail_kill(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("thermal helpers must never signal a process")

    monkeypatch.setattr(os, "kill", fail_kill)
    assert thermal_should_pause(90.0, cfg, above_since=3600) is True
    assert thermal_should_resume(70.0, cfg) is True
    assert calls == []


# ---------------------------------------------------------------------------
# Combined gates


def test_start_gate_reason_order_is_stable_and_all_gates_apply(cfg: GuardConfig):
    cfg.temp_sensor = "auto"  # no hwmon fixture -> missing
    add_process(cfg, 200, 1000, ["/usr/bin/ffmpeg", "-i", "movie.mkv"])
    # No power supply files -> no-ac-source.
    decision = start_gate(
        cfg,
        source_size=0,
        estimate_seconds=60,
        window=("09:00", "17:00"),
        now=datetime(2026, 1, 5, 20, 0),
        speed_known=False,
        user_paused=True,
        extra_reasons=("ffmpeg-changed",),
        disk_usage=FakeDisk(free=0),
    )

    assert decision.ok is False
    assert decision.reasons == (
        "user-paused",
        "jellyfin-transcoding",
        "no-ac-source",
        "temp-sensor-missing",
        "space-low",
        "root-space-low",
        "db-space-low",
        "ffmpeg-changed",
        "estimate-cold-start",
        "outside-window",
    )
    assert len(decision.messages) == len(decision.reasons)


def test_mid_encode_gate_checks_space_and_ac(cfg: GuardConfig):
    set_ac(cfg, 0)
    disk = FakeDisk(
        free=100 * GIB,
        overrides={cfg.pipeline: 19 * GIB},
    )
    decision = mid_encode_gate(cfg, source_size=0, disk_usage=disk)
    assert decision.reasons == ("space-low", "on-battery")


def test_mid_encode_gate_passes_and_ignores_start_only_gates(cfg: GuardConfig):
    # Hot sensor and a Jellyfin transcode are start gates; the mid-encode gate
    # leaves thermal handling to the monitor and does not re-check Jellyfin.
    add_hwmon(cfg, 2, "coretemp", {1: 99000}, {1: "Package id 0"})
    add_process(cfg, 201, 1000, ["/usr/bin/ffmpeg"])
    cfg.temp_sensor = "auto"
    set_ac(cfg, 1)
    disk = FakeDisk(
        free=100 * GIB,
        overrides={cfg.pipeline: 21 * GIB},
    )

    decision = mid_encode_gate(cfg, source_size=0, disk_usage=disk)
    assert decision.ok is True, decision.reasons


def test_mid_encode_gate_fails_closed_without_ac(cfg: GuardConfig):
    decision = mid_encode_gate(cfg, source_size=0, disk_usage=FakeDisk())
    assert decision.reasons == ("no-ac-source",)
