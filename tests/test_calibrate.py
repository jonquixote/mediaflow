"""Calibration tests (SAF-14/A1/A2).

The tests never touch real media: a tiny synthetic clip is generated with
ffmpeg into a temporary directory.  Hardware backends are exercised through
the planner's capability report, never through a real GPU encode.
"""

from __future__ import annotations

import json
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import mediaflow.calibrate as calibrate_mod
from mediaflow.calibrate import calibrate, format_calibration
from mediaflow.ffmpeg import FFMPEG, FFPROBE, build_command
from mediaflow.plan import ProbeResult
from mediaflow.presets import Audio, Meta, Preset, Schedule, Skip, Subtitles, Video
from mediaflow.supervision import (
    OperationState,
    ProtectionAction,
    ProtectionDecision,
    SlotBusy,
    processing_slot,
    slot_path,
)

pytestmark = pytest.mark.integration

if not Path(FFMPEG).exists():  # pragma: no cover - environment guard
    pytest.skip("jellyfin-ffmpeg unavailable", allow_module_level=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Config:
    """The parts of the configuration the calibration path reads."""

    def __init__(self, root: Path, capabilities: dict | None = None) -> None:
        self.root = Path(root)
        self.ffmpeg = FFMPEG
        self.ffprobe = FFPROBE
        self.state_dir = self.root / "state"
        self.work = self.root / "_pipeline" / "work"
        self.trash = self.root / "_pipeline" / "trash"
        self.capabilities_path = self.state_dir / "capabilities.json"
        self.dry_run = False
        # The synthetic sandbox has no meaningful AC/sensor contract.  Keep
        # the calibration test deterministic; production's default remains
        # fail-closed for unknown AC state.
        self.pause_on_battery = False
        self.temp_sensor = "none"
        self.min_output_kbps = 300
        self.min_bpp = 0.01
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.capabilities_path.write_text(
            json.dumps({"capabilities_schema": 2, **(capabilities or {})}), encoding="utf-8"
        )


class _Store:
    """A speed-table stub that records what calibration seeds."""

    def __init__(self) -> None:
        self.seeds: list[tuple] = []

    def speed_seed(self, preset: str, backend: str, height: int, factor: float) -> None:
        self.seeds.append((preset, backend, height, factor))


def _preset(
    *,
    preset_id: str = "test-encode",
    action: str = "encode",
    allow_software: bool = True,
    window: str = "01:00-05:00",
    max_width: int = 1920,
    max_height: int = 1080,
    quality: int = 24,
    look_ahead: bool = True,
    backend_order: tuple[str, ...] = ("qsv", "vaapi"),
) -> Preset:
    raw = {
        "id": preset_id,
        "container": "mp4",
        "meta": {"enabled": True, "experimental": False},
        "video": {
            "action": action,
            "codec": "hevc",
            "backend_order": list(backend_order),
            "allow_software": allow_software,
            "quality": quality,
            "look_ahead": look_ahead,
            "max_width": max_width,
            "max_height": max_height,
        },
        "audio": {"languages": ["original"], "max_tracks": 1},
        "subtitles": {"languages": [], "include_forced": False},
        "skip": {"already_efficient": False},
        "schedule": {"window": window},
    }
    return Preset(
        id=preset_id,
        description="test preset",
        container="mp4",
        meta=Meta(),
        video=Video(
            action=action,
            codec="hevc",
            backend_order=backend_order,
            allow_software=allow_software,
            quality=quality,
            look_ahead=look_ahead,
            max_width=max_width,
            max_height=max_height,
        ),
        audio=Audio(),
        subtitles=Subtitles(),
        skip=Skip(),
        schedule=Schedule(window=window),
        raw=raw,
    )


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("calibrate-source") / "clip.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path


def _probe_4k(path: Path) -> ProbeResult:
    """A probe that reports a 4K source for the given file."""
    return ProbeResult(
        path=path,
        raw={
            "format": {"format_name": "mov,mp4,m4a", "duration": "2.0", "size": "1"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 3840,
                    "height": 2160,
                    "pix_fmt": "yuv420p",
                    "sample_aspect_ratio": "1:1",
                    "avg_frame_rate": "25/1",
                    "r_frame_rate": "25/1",
                    "duration": "2.0",
                    "disposition": {"default": 1},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "duration": "2.0",
                    "disposition": {"default": 1},
                    "tags": {"language": "eng"},
                },
            ],
        },
    )


def _run(source: Path, config: _Config, **kwargs):
    """Run the calibration with a single quality unless the test says otherwise."""
    kwargs.setdefault("qualities", [24])
    return calibrate(source, config, **kwargs)


# ---------------------------------------------------------------------------
# A1 - the trial is the production operation
# ---------------------------------------------------------------------------


def test_hardware_only_preset_refuses_without_a_capable_backend(
    tmp_path: Path, source: Path
) -> None:
    """SAF-14: no silent software substitution when the preset forbids it."""
    config = _Config(tmp_path, capabilities={})
    store = _Store()

    result = _run(
        source,
        config,
        seconds=1,
        preset=_preset(allow_software=False, window=""),
        store=store,
    )

    assert result["status"] == "refused"
    assert "encoder" in result["reason"].lower()
    assert result["rows"] == []
    assert store.seeds == []


def test_software_is_used_only_when_the_preset_allows_it(
    tmp_path: Path, source: Path
) -> None:
    """SAF-14: the software fallback is the planner's, not the calibrator's."""
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True, window="01:00-05:00")

    result = _run(source, config, seconds=1, preset=preset)

    assert result["status"] == "ok", result.get("reason")
    assert result["backend"] == "software"
    assert result["encoder"] == "libx265"
    assert result["scope"] == "video-encode-only"
    assert result["rows"]
    command = result["rows"][0]["command"]
    assert "libx265" in command
    assert not any("qsv" in part or "vaapi" in part for part in command)


def test_trial_uses_the_production_command(tmp_path: Path, source: Path) -> None:
    """SAF-14: the trial command is the production command for the plan."""
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True)

    result = _run(source, config, seconds=1, preset=preset)

    assert result["status"] == "ok", result.get("reason")
    row = result["rows"][0]
    command = row["command"]
    assert "-c:v" in command and command[command.index("-c:v") + 1] == "libx265"
    assert command[command.index("-preset") + 1] == "slow"
    assert "-crf" in command
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    # The trial covers exactly the requested interval and the video stream
    # alone.
    assert "-ss" in command and "-t" in command
    assert "-an" in command and "-sn" in command and "-dn" in command
    start = float(command[command.index("-ss") + 1])
    length = float(command[command.index("-t") + 1])
    assert start == pytest.approx(result["sample_start"], abs=0.002)
    assert length == pytest.approx(result["sample_seconds"], abs=0.002)


def test_a_4k_source_is_measured_at_the_planned_geometry(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SAF-14: the trial scales to the planned target, not to the source."""
    monkeypatch.setattr(calibrate_mod, "probe_media", lambda *a, **k: _probe_4k(source))
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True, max_width=1920, max_height=1080)
    store = _Store()

    result = _run(source, config, seconds=1, preset=preset, store=store)

    assert result["status"] == "ok", result.get("reason")
    assert result["source_geometry"] == {"width": 3840, "height": 2160}
    assert result["target_geometry"] == {"width": 1920, "height": 1080}
    assert len(store.seeds) == 1
    seeded_preset, seeded_backend, seeded_height, seeded_factor = store.seeds[0]
    assert seeded_preset == "test-encode"
    assert seeded_backend == "software"
    assert seeded_height == 1080
    assert seeded_factor > 0
    assert result["seeded"]["height"] == 1080
    command = result["rows"][0]["command"]
    vf = command[command.index("-vf") + 1]
    assert "scale=1920:1080" in vf


def test_the_quality_sweep_varies_the_production_quality(
    tmp_path: Path, source: Path
) -> None:
    """SAF-14: every trial is a production encode at another quality."""
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True, quality=24)

    result = _run(
        source, config, seconds=1, preset=preset, qualities=[20, 28]
    )

    assert result["status"] == "ok", result.get("reason")
    crfs = []
    for row in result["rows"]:
        command = row["command"]
        crfs.append(int(command[command.index("-crf") + 1]))
    assert crfs == [20, 28]


def test_qsv_plan_keeps_look_ahead_and_bit_depth(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SAF-14: the trial is built from the planner's production plan."""
    captured: dict = {}

    class _StubEncoder:
        def __init__(self, ffmpeg: str = FFMPEG, **kwargs) -> None:
            self.ffmpeg = ffmpeg

        def encode(self, plan, resume_from=None, **kwargs):
            captured["plan"] = plan
            captured["sample"] = kwargs.get("sample")
            captured["video_only"] = kwargs.get("video_only")
            output = Path(kwargs["output"])
            captured["command"] = build_command(
                plan,
                output,
                ffmpeg=self.ffmpeg,
                sample=kwargs.get("sample"),
                video_only=kwargs.get("video_only"),
            )
            from mediaflow.ffmpeg import EncodeResult

            return EncodeResult(output=output, returncode=1, stderr="stub")

    monkeypatch.setattr(calibrate_mod, "FfmpegEncoder", _StubEncoder)
    monkeypatch.setattr(calibrate_mod, "probe_media", lambda *a, **k: _probe_4k(source))
    config = _Config(tmp_path, capabilities={"qsv": {"hevc8": True, "hevc10": True}})
    preset = _preset(allow_software=False, window="", look_ahead=True)

    result = _run(source, config, seconds=1, preset=preset)

    assert result["status"] == "ok"
    assert result["backend"] == "qsv"
    assert result["encoder"] == "hevc_qsv"
    assert captured["video_only"] is True
    assert captured["sample"] is not None
    command = captured["command"]
    assert command[command.index("-look_ahead") + 1] == "1"
    assert command[command.index("-pix_fmt") + 1] == "nv12"
    assert "vpp_qsv=w=1920:h=1080" in command[command.index("-vf") + 1]


# ---------------------------------------------------------------------------
# A2 - the reference sample
# ---------------------------------------------------------------------------


def test_the_reference_is_the_same_source_interval(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SAF-14: the score compares the trial with the interval it encodes."""
    calls: list[dict] = []

    def fake_score(ffmpeg, source_path, trial, **kwargs):
        calls.append({"source": source_path, "trial": trial, **kwargs})
        return "ssim", 0.99, None

    monkeypatch.setattr(calibrate_mod, "_score", fake_score)
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True)

    result = _run(source, config, seconds=1, preset=preset)

    assert result["status"] == "ok", result.get("reason")
    assert calls
    call = calls[0]
    assert call["source"] == source
    assert call["start"] == pytest.approx(result["sample_start"], abs=0.002)
    assert call["duration"] == pytest.approx(result["sample_seconds"], abs=0.002)
    assert call["width"] == result["target_geometry"]["width"]
    assert call["height"] == result["target_geometry"]["height"]
    assert result["rows"][0]["score"] == pytest.approx(0.99)


def test_a_failed_scoring_run_is_reported_as_not_available(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SAF-14: a failed score never becomes a number."""
    monkeypatch.setattr(
        calibrate_mod,
        "_score",
        lambda *a, **k: ("n/a", None, "scoring failed (rc=1)"),
    )
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True)

    result = _run(source, config, seconds=1, preset=preset)

    assert result["status"] == "ok"
    row = result["rows"][0]
    assert row["score"] is None
    assert row["metric"] == "n/a"
    assert "scoring failed" in row["reason"]
    # The size and the elapsed time stay usable even without a score.
    assert row["size_bytes"] > 0
    assert row["encode_seconds"] > 0
    assert row["valid"] is True


def test_a_frame_count_mismatch_is_not_scored(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SAF-14: two sides that do not cover the same interval are not scored."""
    monkeypatch.setattr(
        calibrate_mod, "_frame_count", lambda *a, **k: 3
    )
    monkeypatch.setattr(
        calibrate_mod,
        "_expected_frames",
        lambda *a, **k: 25,
    )
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True)

    result = _run(source, config, seconds=1, preset=preset)

    row = result["rows"][0]
    assert row["score"] is None
    assert "frame count mismatch" in row["reason"]
    assert row["valid"] is False


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def test_a_failed_trial_is_not_seeded(tmp_path: Path, source: Path, monkeypatch) -> None:
    """SAF-14: only a valid trial may seed the speed table."""

    class _FailingEncoder:
        def __init__(self, ffmpeg: str = FFMPEG, **kwargs) -> None:
            self.ffmpeg = ffmpeg

        def encode(self, plan, resume_from=None, **kwargs):
            from mediaflow.ffmpeg import EncodeResult

            return EncodeResult(
                output=Path(kwargs["output"]), returncode=1, stderr="boom"
            )

    monkeypatch.setattr(calibrate_mod, "FfmpegEncoder", _FailingEncoder)
    config = _Config(tmp_path, capabilities={})
    store = _Store()

    result = _run(
        source,
        config,
        seconds=1,
        preset=_preset(allow_software=True),
        store=store,
    )

    assert result["status"] == "ok"
    assert all(row["valid"] is False for row in result["rows"])
    assert store.seeds == []
    assert result["seeded"] is None


def test_the_seed_key_is_the_backend_that_ran(
    tmp_path: Path, source: Path
) -> None:
    """SAF-14: the speed table is keyed by the backend that actually ran."""
    config = _Config(tmp_path, capabilities={})
    store = _Store()

    result = _run(
        source,
        config,
        seconds=1,
        preset=_preset(preset_id="balanced", allow_software=True),
        store=store,
    )

    assert result["status"] == "ok", result.get("reason")
    assert store.seeds, "the valid trial must seed the speed table"
    preset_id, backend, height, factor = store.seeds[0]
    assert preset_id == "balanced"
    assert backend == "software"
    assert height == result["source_geometry"]["height"]
    assert factor > 0
    assert result["seeded"]["backend"] == "software"


# ---------------------------------------------------------------------------
# scope and housekeeping
# ---------------------------------------------------------------------------


def test_a_remux_preset_is_not_applicable(tmp_path: Path, source: Path) -> None:
    """SAF-14: calibration measures the encode path only."""
    config = _Config(tmp_path, capabilities={})
    store = _Store()

    result = _run(
        source,
        config,
        seconds=1,
        preset=_preset(action="remux", allow_software=True),
        store=store,
    )

    assert result["status"] == "not-applicable"
    assert "remux" in result["reason"]
    assert result["rows"] == []
    assert store.seeds == []


def test_the_trial_artifacts_stay_in_the_work_area(tmp_path: Path, source: Path) -> None:
    """SAF-13: the scratch area is the configured work area, and it is clean."""
    config = _Config(tmp_path, capabilities={})
    preset = _preset(allow_software=True)

    result = _run(source, config, seconds=1, preset=preset)

    assert result["status"] == "ok", result.get("reason")
    assert not [
        path
        for path in config.work.rglob("*")
        if path.name.startswith("calib-")
    ]


def test_the_result_reports_what_it_measured(tmp_path: Path, source: Path) -> None:
    """SAF-14: the report names the preset, encoder, geometry and scope."""
    config = _Config(tmp_path, capabilities={})

    result = _run(
        source, config, seconds=1, preset=_preset(preset_id="balanced")
    )

    assert result["scope"] == "video-encode-only"
    assert result["preset"] == "balanced"
    assert result["encoder"] == "libx265"
    assert result["target_geometry"] == {"width": 320, "height": 240}
    assert result["bit_depth"] == 8
    assert result["sample_seconds"] > 0
    text = format_calibration(result)
    assert "video-encode-only" in text
    assert "balanced" in text


# ---------------------------------------------------------------------------
# bounded expensive-operation supervision
# ---------------------------------------------------------------------------


def test_machine_protection_defers_before_a_source_probe(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SUP-2/SAF-14: protection defers calibration instead of probing."""

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("the source was probed despite protection deferral")

    monkeypatch.setattr(calibrate_mod, "probe_media", unexpected_probe)
    cancel = threading.Event()
    pause = threading.Event()
    monitor = lambda: ProtectionDecision(
        ProtectionAction.DEFER,
        ("on-battery",),
        detail="machine protection deferred calibration",
    )

    result = _run(
        source,
        _Config(tmp_path),
        seconds=1,
        preset=_preset(allow_software=True),
        cancel_event=cancel,
        pause_event=pause,
        monitor=monitor,
    )

    assert result["status"] == "deferred"
    assert result["state"] == OperationState.DEFERRED.value
    assert result["deferred"] is True
    assert result["reasons"] == ["on-battery"]
    assert result["rows"] == []
    assert result["seeded"] is None
    assert not cancel.is_set(), "calibration must not mutate caller cancellation"
    assert not pause.is_set(), "calibration must not mutate caller pause state"


def test_a_busy_processing_slot_defers_without_probing(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SUP-2/SAF-14: a busy operation slot prevents every calibration probe."""

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("calibration probed while the operation slot was busy")

    monkeypatch.setattr(calibrate_mod, "probe_media", unexpected_probe)
    config = _Config(tmp_path)

    with processing_slot(slot_path(config), timeout=0):
        result = _run(
            source,
            config,
            seconds=1,
            preset=_preset(allow_software=True),
        )

    assert result["status"] == "deferred"
    assert result["state"] == OperationState.DEFERRED.value
    assert result["reasons"] == ["processing-slot-busy"]
    assert result["rows"] == []


def test_calibration_holds_the_slot_through_every_expensive_phase(
    tmp_path: Path, source: Path, monkeypatch
) -> None:
    """SUP-2/SAF-14: probe, filters, trials and cleanup share one slot."""

    active = {"value": False}
    phases: list[str] = []

    @contextmanager
    def tracked_slot(path, **_kwargs):
        active["value"] = True
        try:
            yield path
        finally:
            active["value"] = False

    def require_slot(phase: str) -> None:
        assert active["value"], f"{phase} ran outside the processing slot"
        phases.append(phase)

    def fake_probe(*args, **kwargs):
        require_slot("probe")
        return _probe_4k(source)

    def fake_filter(*args, **kwargs):
        require_slot("filter")
        return True

    def fake_frames(*args, **kwargs):
        require_slot("frame-count")
        return None

    def fake_trial(**kwargs):
        require_slot("trial")
        return {
            "quality": int(kwargs["quality"]),
            "size_bytes": 1024,
            "encode_seconds": 1.0,
            "speed_factor": 1.0,
            "score": None,
            "metric": "n/a",
            "frames": None,
            "valid": True,
            "reason": None,
            "command": ["stub"],
        }

    def fake_cleanup(_config, _path):
        require_slot("cleanup")

    monkeypatch.setattr(calibrate_mod, "processing_slot", tracked_slot)
    monkeypatch.setattr(calibrate_mod, "probe_media", fake_probe)
    monkeypatch.setattr(calibrate_mod, "_has_filter", fake_filter)
    monkeypatch.setattr(calibrate_mod, "_expected_frames", fake_frames)
    monkeypatch.setattr(calibrate_mod, "_run_trial", fake_trial)
    monkeypatch.setattr(calibrate_mod, "_remove_scratch", fake_cleanup)

    result = _run(
        source,
        _Config(tmp_path),
        seconds=1,
        preset=_preset(allow_software=True),
    )

    assert result["status"] == "ok", result.get("reason")
    assert phases == ["probe", "filter", "frame-count", "trial", "cleanup"]
    assert active["value"] is False
