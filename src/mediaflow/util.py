"""Small shared helpers with no pipeline policy."""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterator

TEMP_MARKERS = (".part", ".!qB", ".crdownload", ".aria2", ".tmp", ".filepart")


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024,
                progress: Callable[[int, int], None] | None = None) -> str:
    total = Path(path).stat().st_size
    done = 0
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
            done += len(block)
            if progress:
                progress(done, total)
    return h.hexdigest()


def tree_stats(path: Path) -> tuple[int, float]:
    """Sum regular file sizes and max mtime without following symlinks."""
    path = Path(path)
    if path.is_file() and not path.is_symlink():
        st = path.stat()
        return st.st_size, st.st_mtime
    total = 0
    newest = 0.0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            p = Path(root) / name
            if p.is_symlink():
                continue
            st = p.stat()
            total += st.st_size
            newest = max(newest, st.st_mtime)
        for name in dirs:
            p = Path(root) / name
            if not p.is_symlink():
                try:
                    newest = max(newest, p.stat().st_mtime)
                except FileNotFoundError:
                    pass
    return total, newest


def has_temp_marker(path: Path) -> str | None:
    """Return the first temp-marker path in or adjacent to a candidate entry."""
    path = Path(path)
    candidates = [path, *path.parent.iterdir()] if path.parent.exists() else [path]
    for p in candidates:
        if any(p.name.endswith(m) for m in TEMP_MARKERS):
            return str(p)
    if path.is_dir() and not path.is_symlink():
        for entry in path.rglob("*"):
            if entry.is_symlink():
                continue
            if any(entry.name.endswith(m) for m in TEMP_MARKERS):
                return str(entry)
    return None


def gzip_bytes(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    with open(target, "rb") as fh:
        os.fsync(fh.fileno())


def gzip_text(text: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wb") as dst:
        dst.write(text.encode("utf-8"))
    with open(target, "rb") as fh:
        os.fsync(fh.fileno())


def is_regular_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)


def is_real_dir(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(st.st_mode)


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PiB"


def short_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
