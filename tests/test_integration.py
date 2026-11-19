"""End-to-end integration with real jellyfin-ffmpeg on synthetic clips.

These test the real planner/encoder/verifier/publish pipeline, not encoder speed
or heat. Clips are tiny and generated locally.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaflow.config import load_config
from mediaflow.db import Store
from mediaflow.engine import Engine
from mediaflow.presets import load_presets, load_preset_file

FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"

pytestmark = pytest.mark.integration

if not Path(FFMPEG).exists():
    pytest.skip("jellyfin-ffmpeg unavailable", allow_module_level=True)


class AlwaysOk:
    def start_gate(self, **kwargs):
        return SimpleNamespace(ok=True, reasons=(), messages=())

    def check_space(self, **kwargs):
        return None, SimpleNamespace(ok=True, reasons=(), messages=())


def _gen(path: Path, *, duration: float = 1.5, size: str = "320x180", audio: str = "aac",
         fps: str = "24") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [FFMPEG, "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", audio, "-shortest",
            "-f", "matroska", str(path)]
    subprocess.run(argv, check=True, capture_output=True, timeout=120)


def _config(tmp_path: Path, *, preset: str, min_kbps: int = 100) -> tuple:
    etc = tmp_path / "etc"
    (etc / "presets.d").mkdir(parents=True)
    from pathlib import Path as P
    repo_presets = P(__file__).resolve().parents[1] / "presets"
    for src in repo_presets.glob("*.toml"):
        shutil.copy(src, etc / "presets.d" / src.name)
    cfg_path = etc / "config.toml"
    cfg_path.write_text(f'''root = "{tmp_path / "media"}"
state_dir = "{tmp_path / "state"}"
runtime_dir = "{tmp_path / "run"}"
dry_run = false
stable_seconds = 0
poll_seconds = 1
min_output_kbps = {min_kbps}
min_bpp = 0
ffmpeg = "{FFMPEG}"
ffprobe = "{FFPROBE}"
''')
    cfg = load_config(cfg_path, environ={})
    cfg.ensure_directories()
    for pid in ("keep-remux-mp4", "keep-remux-mkv", "1080p-balanced-mp4", "1080p-small-mp4",
                "720p-small-mp4"):
        (cfg.convert / pid).mkdir(parents=True, exist_ok=True)
    presets = load_presets(etc / "presets.d")
    store = Store(cfg.db_path)
    store.initialize()
    engine = Engine(cfg, store, presets, guards=AlwaysOk())
    engine.start()
    return cfg, store, engine


def _probe(path: Path) -> dict:
    proc = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                           str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_remux_end_to_end_publish_retire_manifest(tmp_path):
    """[SAF-3]/[SAF-6]/[MP4-1]/[MP4-8] Real H.264 remux, marker, manifest and trash."""
    cfg, store, engine = _config(tmp_path, preset="keep-remux-mp4")
    name = "Remux Movie (2001)"
    src = cfg.convert / "keep-remux-mp4" / name / f"{name}.mkv"
    _gen(src)
    (src.parent / f"{name}.en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    try:
        engine.tick()
        job = store.list_jobs()[0]
        assert job["state"] == "DONE", store.events(job["id"])
        out = cfg.movies / name / f"{name}.mp4"
        assert out.exists()
        assert (cfg.movies / name / f".mediaflow-{job['id']}").exists()
        sidecar = cfg.movies / name / f"{name}.en.srt"
        assert sidecar.exists()
        probe = _probe(out)
        assert [s["codec_type"] for s in probe["streams"]].count("video") == 1
        assert [s["codec_name"] for s in probe["streams"] if s["codec_type"] == "audio"] == ["aac"]
        manifest = json.loads((cfg.trash / job["trash_path"].split("/")[-2] /
                               job["trash_path"].split("/")[-1] / "manifest.json").read_text())
        assert manifest["output_sha256"] == job["output_sha256"]
        assert Path(manifest["original_path"]) == Path(job["original_path"])
        assert not src.parent.exists()
        assert not (cfg.work / job["id"]).exists()
    finally:
        store.close()


def test_real_software_encode_end_to_end_hvc1(tmp_path):
    """[MP4-1]/[SAF-2]/[SIZE-4] Real HEVC encode via software test preset, verified and published."""
    etc = tmp_path / "etc"
    (etc / "presets.d").mkdir(parents=True)
    from pathlib import Path as P
    for src in (P(__file__).resolve().parents[1] / "presets").glob("*.toml"):
        shutil.copy(src, etc / "presets.d" / src.name)
    preset_path = etc / "presets.d" / "software-test.toml"
    preset_path.write_text("""id = "software-test"
description = "integration-only software HEVC"
container = "mp4"
[meta]
experimental = false
enabled = true
[video]
action = "encode"
codec = "hevc"
backend_order = ["qsv", "vaapi"]
allow_software = true
quality = 35
look_ahead = true
max_height = 1080
max_width = 1920
ten_bit_source = "keep"
tag = "hvc1"
hdr = "review"
interlaced = "review"
[audio]
languages = ["original"]
max_tracks = 3
copy_codecs = ["aac", "ac3", "eac3"]
exclude_commentary = true
unknown_language = "keep_first"
[subtitles]
languages = ["eng"]
include_forced = true
text = "mov_text"
bitmap = "mkv_fallback"
mkv_policy = "bitmap_only"
[skip]
already_efficient = false
efficient_kbps_1080p = 4000
min_savings_pct = 0
[schedule]
window = "00:00-23:59"
""")
    cfg_path = etc / "config.toml"
    cfg_path.write_text(f'''root = "{tmp_path / "media"}"
state_dir = "{tmp_path / "state"}"
runtime_dir = "{tmp_path / "run"}"
dry_run = false
stable_seconds = 0
min_output_kbps = 20
min_bpp = 0
ffmpeg = "{FFMPEG}"
ffprobe = "{FFPROBE}"
''')
    cfg = load_config(cfg_path, environ={})
    cfg.ensure_directories()
    (cfg.convert / "software-test").mkdir(parents=True)
    presets = load_presets(etc / "presets.d")
    store = Store(cfg.db_path)
    store.initialize()
    engine = Engine(cfg, store, presets, guards=AlwaysOk())
    engine.start()
    name = "Encoded Movie (2002)"
    src = cfg.convert / "software-test" / name / f"{name}.mkv"
    _gen(src, size="160x90", duration=1.0)
    try:
        engine.tick()
        job = store.list_jobs()[0]
        assert job["state"] == "DONE", store.events(job["id"])
        out = cfg.movies / name / f"{name}.mp4"
        probe = _probe(out)
        video = [s for s in probe["streams"] if s["codec_type"] == "video"][0]
        assert video["codec_name"] == "hevc"
        assert video["codec_tag_string"] == "hvc1"
        assert (video["width"], video["height"]) == (160, 90)
        assert job["output_sha256"]
        assert Path(job["trash_path"]).exists()
    finally:
        store.close()


def test_mpegts_hevc_remux_builds_hvcc(tmp_path):
    """[MP4-2] HEVC from MPEG-TS remuxes into MP4 with a valid hvc1/hvcC tag."""
    cfg, store, engine = _config(tmp_path, preset="keep-remux-mp4", min_kbps=10)
    name = "Transit (2003)"
    src = cfg.convert / "keep-remux-mp4" / name / f"{name}.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=1",
                    "-c:v", "libx265", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
                    "-f", "mpegts", str(src)], check=True, capture_output=True, timeout=120)
    try:
        engine.tick()
        job = store.list_jobs()[0]
        assert job["state"] == "DONE", store.events(job["id"])
        out = cfg.movies / name / f"{name}.mp4"
        probe = _probe(out)
        video = [s for s in probe["streams"] if s["codec_type"] == "video"][0]
        assert video["codec_name"] == "hevc"
        assert video["codec_tag_string"] == "hvc1"
        # ffprobe can open and decode the final hvcC sample.
        decode = subprocess.run([FFMPEG, "-v", "error", "-xerror", "-i", str(out),
                                 "-map", "0:v:0", "-f", "null", "-"],
                                capture_output=True, text=True, timeout=120)
        assert decode.returncode == 0 and not decode.stderr.strip()
    finally:
        store.close()
