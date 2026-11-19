"""Filesystem safety tests. [SAF-1] [SAF-3] [SAF-5]"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from mediaflow.safety import (
    CrossDeviceError,
    FileSafety,
    PathDenied,
    SafetyError,
    atomic_write_bytes,
    check_one_filesystem,
)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    work = root / "_pipeline" / "work"
    trash = root / "_pipeline" / "trash"
    movies = root / "movies"
    for p in (work, trash, movies, root / "_pipeline" / "inbox"):
        p.mkdir(parents=True)
    return root, work, trash, movies


def test_delete_refuses_outside_roots(tree):
    """[SAF-5] Only work/trash/job scratch are deletable."""
    root, work, trash, movies = tree
    victim = movies / "precious.mkv"
    victim.write_text("x")
    fs = FileSafety(root, work, trash)
    with pytest.raises(PathDenied):
        fs.remove(victim, "test")
    assert victim.exists()


def test_delete_refuses_parent_traversal(tree):
    """[SAF-5] A path using .. cannot escape work."""
    root, work, trash, movies = tree
    victim = movies / "precious.mkv"
    victim.write_text("x")
    fs = FileSafety(root, work, trash)
    sneaky = work / "job" / ".." / ".." / ".." / "movies" / "precious.mkv"
    with pytest.raises(PathDenied):
        fs.remove(sneaky, "test")
    assert victim.exists()


def test_delete_refuses_symlink_anywhere(tree):
    """[SAF-5] Symlinks, including below an allowed directory, are refused."""
    root, work, trash, movies = tree
    outside = root / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    job = work / "job1"
    job.mkdir()
    (job / "link").symlink_to(outside)
    fs = FileSafety(root, work, trash)
    with pytest.raises(PathDenied):
        fs.remove(job / "link", "test")
    with pytest.raises(PathDenied):
        fs.remove(job, "test")
    assert (outside / "keep").exists()
    assert (job / "link").is_symlink()


def test_delete_refuses_short_path(tree):
    root, work, trash, _ = tree
    fs = FileSafety(root, work, trash)
    with pytest.raises(PathDenied):
        fs.remove(Path("/x"), "test")


def test_delete_logs_and_removes_inside_work(tree, caplog):
    root, work, trash, _ = tree
    job = work / "job1"
    job.mkdir()
    (job / "out.mp4.partial").write_text("partial")
    fs = FileSafety(root, work, trash)
    fs.remove(job / "out.mp4.partial", "test-cleanup")
    assert not (job / "out.mp4.partial").exists()
    assert "test-cleanup" in caplog.text


def test_retire_move_moves_never_deletes(tree):
    """[SAF-1] A retired original remains byte-identical in trash."""
    root, work, trash, _ = tree
    src = root / "_pipeline" / "convert" / "movies" / "preset" / "Film"
    src.mkdir(parents=True)
    (src / "Film.mkv").write_bytes(b"reel")
    fs = FileSafety(root, work, trash)
    dest = fs.retire_move(src, trash, "JOB123", date="2026-01-02")
    assert dest == trash / "2026-01-02" / "JOB123"
    assert (dest / "Film.mkv").read_bytes() == b"reel"
    assert not src.exists()


def test_retire_move_refuses_overwrite(tree):
    root, work, trash, _ = tree
    a = root / "_pipeline" / "convert" / "movies" / "p" / "a.mkv"
    a.parent.mkdir(parents=True)
    a.write_text("a")
    existing = trash / "2026-01-02" / "JOB"
    existing.mkdir(parents=True)
    fs = FileSafety(root, work, trash)
    dest = fs.retire_move(a, trash, "JOB", date="2026-01-02")
    assert dest.name.startswith("JOB-")
    assert a.exists() is False


def test_atomic_write_is_complete(tmp_path):
    target = tmp_path / "x.json"
    atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert not list(tmp_path.glob(".*tmp*"))


def test_one_filesystem(tree):
    root, work, trash, movies = tree
    devices = check_one_filesystem([work, trash, movies])
    assert len(set(devices.values())) == 1


def test_one_filesystem_missing(tree):
    root, work, trash, movies = tree
    with pytest.raises(SafetyError):
        check_one_filesystem([work, root / "nope"])


def test_publish_noreplace_refuses_existing_destination(tree):
    """[SAF-4] Publish never overwrites an existing library item."""
    root, work, trash, movies = tree
    src = work / "pub" / "Title"
    src.mkdir(parents=True)
    (src / "Title.mp4").write_text("new")
    dest = movies / "Title"
    dest.mkdir()
    (dest / "Title.mp4").write_text("old")
    fs = FileSafety(root, work, trash)
    with pytest.raises(FileExistsError):
        fs.rename_noreplace(src, dest)
    assert (dest / "Title.mp4").read_text() == "old"
    assert (src / "Title.mp4").read_text() == "new"


def test_publish_fails_closed_without_noreplace(tree, monkeypatch):
    """[SAF-3]/[SAF-4] No unsafe check-then-rename fallback is allowed."""
    root, work, trash, movies = tree
    src = work / "pub" / "Title"
    src.mkdir(parents=True)
    (src / "Title.mp4").write_text("new")
    fs = FileSafety(root, work, trash)
    monkeypatch.setattr("mediaflow.safety.platform.system", lambda: "Darwin")
    with pytest.raises(SafetyError, match="no-replace"):
        fs.rename_noreplace(src, movies / "Title")
    assert not (movies / "Title").exists()


def test_rename_refuses_cross_device(tree, monkeypatch):
    root, work, trash, _ = tree
    src = work / "job" / "file"
    src.parent.mkdir()
    src.write_text("x")
    fs = FileSafety(root, work, trash)
    monkeypatch.setattr("mediaflow.safety._same_device", lambda a, b: False)
    with pytest.raises(CrossDeviceError):
        fs.rename(src, root / "movies" / "file")


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30))
@settings(max_examples=40, deadline=None)
def test_path_guard_fuzz(tmp_path_factory, name):
    """[SAF-5] Fuzz: arbitrary text can never become an accepted outside path."""
    base = tmp_path_factory.mktemp("guard")
    root = base / "root"
    work = root / "work"
    trash = root / "trash"
    work.mkdir(parents=True)
    trash.mkdir(parents=True)
    fs = FileSafety(root, work, trash)
    target = root / "movies" / name
    try:
        fs.check_deletable(target)
    except PathDenied:
        pass
    else:
        pytest.fail(f"outside path accepted: {target!r}")
