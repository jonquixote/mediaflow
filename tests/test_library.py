"""Aggregate library scan/report tests, including MP4-7 mixed containers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mediaflow.db import Store
from mediaflow.library import (
    CONTAINERS,
    MEDIA_EXTENSIONS,
    RESOLUTION_BUCKETS,
    format_report,
    library_report,
    scan_library,
)
from mediaflow.probe import ProbeFailed, ProbeResult


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state.db")
    value.initialize()
    yield value
    value.close()


def video(codec: str = "h264", height: int = 1080) -> dict:
    return {
        "format": {"format_name": "matroska,webm"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": codec, "height": height},
        ],
    }


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_probe(calls: list[str], *, broken: str = "broken") -> callable:
    def probe(path: Path):
        calls.append(path.name)
        if broken in path.name.lower():
            raise ProbeFailed("synthetic corrupt file")
        if path.suffix.lower() in {".mkv", ".webm"}:
            return video("hevc", 2160)
        if path.name.startswith("small"):
            return video("h264", 480)
        return video("h264", 1080)

    return probe


def build_library(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    root = tmp_path / "movies"
    sizes = {}
    sizes["a.mp4"] = len(b"a" * 1000)
    write(root / "a.mp4", b"a" * 1000)
    sizes["b.mkv"] = len(b"b" * 2000)
    write(root / "b.mkv", b"b" * 2000)
    sizes["small.mp4"] = len(b"c" * 3000)
    write(root / "sub" / "small.mp4", b"c" * 3000)
    sizes["broken.mp4"] = len(b"d" * 4000)
    write(root / "broken.mp4", b"d" * 4000)
    return root, sizes


def test_mp4_7_mixed_container_report(store, tmp_path):
    root, sizes = build_library(tmp_path)
    calls: list[str] = []
    report = library_report(root, store, ffprobe=make_probe(calls), cache=True)

    assert report["count"] == report["total_count"] == report["total_files"] == 4
    expected_bytes = sum(sizes.values())
    assert report["bytes"] == report["total_bytes"] == report["total_size"] == expected_bytes
    assert report["totals"] == {"count": 4, "bytes": expected_bytes}

    # MP4-7: both containers appear in the aggregate mix.
    assert report["containers"]["mp4"]["count"] == 3
    assert report["containers"]["mkv"]["count"] == 1
    assert report["containers"]["other"]["count"] == 0
    assert report["container_counts"] == {"mp4": 3, "mkv": 1, "other": 0}
    assert report["container_mix"] == {"mp4": 3, "mkv": 1, "other": 0}
    assert report["containers"]["mp4"]["bytes"] == sizes["a.mp4"] + sizes["small.mp4"] + sizes["broken.mp4"]
    assert report["containers"]["mkv"]["bytes"] == sizes["b.mkv"]

    assert report["codecs"]["h264"]["count"] == 2
    assert report["codecs"]["hevc"]["count"] == 1
    assert report["codecs"]["unknown"]["count"] == 1
    assert report["codec_counts"] == {"h264": 2, "hevc": 1, "unknown": 1}

    assert report["resolutions"]["<=720"]["count"] == 1
    assert report["resolutions"]["<=1080"]["count"] == 1
    assert report["resolutions"][">1080"]["count"] == 1
    assert report["resolutions"]["unknown"]["count"] == 1
    assert report["resolution_counts"] == {
        "<=720": 1,
        "<=1080": 1,
        ">1080": 1,
        "unknown": 1,
    }
    assert report["unreadable"] == 1
    assert report["probed"] == 4
    assert report["cached"] == 0
    assert sorted(calls) == ["a.mp4", "b.mkv", "broken.mp4", "small.mp4"]

    # The report is JSON-safe and aggregate-only: no filenames or titles.
    encoded = json.dumps(report, sort_keys=True)
    for filename in ("a.mp4", "b.mkv", "small.mp4", "broken.mp4"):
        assert filename not in encoded
    assert "title" not in encoded.lower()

    text = format_report(report)
    assert "mp4" in text and "mkv" in text
    assert "h264" in text and "hevc" in text
    assert "a.mp4" not in text
    assert "MP4-7" in library_report.__doc__


def test_hidden_files_hidden_dirs_symlinks_and_non_media_are_ignored(store, tmp_path):
    root = tmp_path / "movies"
    good = write(root / "visible.mp4", b"x" * 10)
    write(root / ".hidden.mp4", b"x" * 10)
    write(root / ".mediaflow-marker", b"marker")
    write(root / ".hidden-dir" / "inside.mkv", b"x" * 10)
    write(root / "notes.txt", b"not a movie")
    write(root / "cover.jpg", b"image")
    write(root / "no-extension", b"x")
    (root / "symlink.mkv").symlink_to(good)
    (root / "linkdir").symlink_to(root / "sub", target_is_directory=True)
    write(root / "sub" / "nested.mp4", b"y" * 20)
    # Outside the movies root is never scanned.
    write(tmp_path / "outside.mp4", b"z" * 30)

    scanned = list(scan_library(root))
    assert {path.name for path in scanned} == {"visible.mp4", "nested.mp4"}
    assert all(not path.is_symlink() for path in scanned)

    report = library_report(root, store, ffprobe=make_probe([]), cache=True)
    assert report["count"] == 2
    assert report["bytes"] == 30
    assert report["containers"]["mp4"]["count"] == 2
    assert report["containers"]["mkv"]["count"] == 0


def test_cache_reuse_avoids_reprobe_and_detects_changes(store, tmp_path):
    root = tmp_path / "movies"
    first = write(root / "one.mp4", b"1" * 100)
    write(root / "two.mkv", b"2" * 200)
    calls: list[str] = []
    probe = make_probe(calls)

    first_report = library_report(root, store, ffprobe=probe, cache=True)
    assert first_report["probed"] == 2
    assert first_report["cached"] == 0
    assert len(calls) == 2

    second_report = library_report(root, store, ffprobe=probe, cache=True)
    assert second_report["probed"] == 0
    assert second_report["cached"] == 2
    assert second_report["cache_hits"] == 2
    assert len(calls) == 2
    assert second_report["count"] == first_report["count"]
    assert second_report["containers"] == first_report["containers"]
    assert second_report["codecs"] == first_report["codecs"]

    # A changed size/mtime invalidates only that file's cache entry.
    first.write_bytes(b"1" * 101)
    assert first.stat().st_size == 101
    third = library_report(root, store, ffprobe=probe, cache=True)
    assert third["probed"] == 1
    assert third["cached"] == 1
    assert len(calls) == 3
    assert calls[-1] == "one.mp4"


def test_cache_disabled_probes_every_time(store, tmp_path):
    root = tmp_path / "movies"
    write(root / "one.mp4", b"1" * 10)
    calls: list[str] = []
    probe = make_probe(calls)
    library_report(root, store, ffprobe=probe, cache=False)
    library_report(root, store, ffprobe=probe, cache=False)
    assert len(calls) == 2


def test_missing_movies_path_is_a_zero_report(store, tmp_path):
    report = library_report(tmp_path / "does-not-exist", store)
    assert report["count"] == 0
    assert report["bytes"] == 0
    assert report["unreadable"] == 0
    assert report["probed"] == 0
    assert report["containers"] == {name: {"count": 0, "bytes": 0} for name in CONTAINERS}
    assert report["resolutions"] == {
        name: {"count": 0, "bytes": 0} for name in RESOLUTION_BUCKETS
    }
    json.dumps(report)
    text = format_report(report, short=True)
    assert "0 files" in text


def test_probe_result_and_precomputed_cache_payload_are_supported(store, tmp_path):
    root = tmp_path / "movies"
    path = write(root / "result.mp4", b"r" * 50)
    probe_result = ProbeResult(
        path=path,
        raw={"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
             "streams": [{"codec_type": "video", "codec_name": "avc1", "height": 720}]},
    )
    report = library_report(root, store, probe=lambda candidate: probe_result, cache=True)
    assert report["codecs"]["h264"]["count"] == 1
    assert report["resolutions"]["<=720"]["count"] == 1

    # A cache hit may be a small derived summary rather than full ffprobe JSON.
    store.put_library_cache(str(path.resolve()), path.stat().st_size, path.stat().st_mtime,
                            {"codec_name": "hevc", "height": 1080})
    cached = library_report(root, store, probe=lambda candidate: pytest.fail("probed"), cache=True)
    assert cached["codecs"]["hevc"]["count"] == 1
    assert cached["resolutions"]["<=1080"]["count"] == 1
    assert cached["cached"] == 1


def test_short_report_is_phone_sized_and_normal_report_is_plain(store, tmp_path):
    root, _ = build_library(tmp_path)
    report = library_report(root, store, ffprobe=make_probe([]), cache=True)

    short = format_report(report, short=True)
    lines = short.splitlines()
    assert 0 < len(lines) <= 12
    assert all(len(line) <= 60 for line in lines)
    assert "mp4" in short and "mkv" in short
    assert "\x1b" not in short

    # Force a long codec list through the short renderer; still phone-width.
    crowded = dict(report)
    crowded["codecs"] = {
        f"codec-{index}": {"count": index + 1, "bytes": index * 100}
        for index in range(12)
    }
    crowded_short = format_report(crowded, short=True)
    assert all(len(line) <= 60 for line in crowded_short.splitlines())
    assert len(crowded_short.splitlines()) <= 12

    normal = format_report(report)
    assert "Containers" in normal
    assert "MP4" in normal and "MKV" in normal
    assert "mp4" in normal and "mkv" in normal
    assert "Probe cache:" in normal
    assert "\x1b" not in normal


def test_media_extensions_cover_mp4_and_mkv():
    assert ".mp4" in MEDIA_EXTENSIONS
    assert ".mkv" in MEDIA_EXTENSIONS
    assert ".txt" not in MEDIA_EXTENSIONS
    assert "MP4-7" in format_report.__doc__
