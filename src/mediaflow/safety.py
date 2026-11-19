"""Filesystem safety primitives.

All destructive and publish moves go through this module.  Nothing else in
mediaflow may call os.remove, os.unlink, shutil.rmtree, or os.rename directly.

The guards here are deliberately conservative: a symlink anywhere below an
allowed root is refused, as is a path with fewer than four absolute
components.  The only deletion roots are the pipeline work directory and the
trash directory; even trash deletion is explicit (`trash purge`) and never
automatic.
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import platform
import stat
import time
from pathlib import Path
from typing import Iterable, Mapping

log = logging.getLogger(__name__)

MIN_COMPONENTS = 4
RENAME_NOREPLACE = 1


class SafetyError(RuntimeError):
    """A filesystem operation violated a safety invariant."""


class PathDenied(SafetyError):
    """A path failed the deletion or move guard."""


class CrossDeviceError(SafetyError):
    """A move would cross filesystems and therefore not be atomic."""


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o664) -> None:
    """Write + fsync + rename + fsync directory, safe against page-cache loss."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), mode)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Mapping | list) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_bytes(path, data)


def _assert_no_symlink(path: Path) -> None:
    """Refuse if any existing component at or below the absolute root is a symlink."""
    for candidate in [path, *path.parents]:
        if candidate == Path(candidate.anchor):
            break
        try:
            st = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise PathDenied(f"refusing symlink path: {candidate}")


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return path != base
    except ValueError:
        return False


def _same_device(path: Path, base: Path) -> bool:
    try:
        return path.stat().st_dev == base.stat().st_dev
    except FileNotFoundError:
        return True


class FileSafety:
    """Centralised delete and move implementation.

    ``roots`` are the configured pipeline paths.  The only deletion roots are
    ``work`` and ``trash``.  Every operation is synchronous and logged.
    """

    def __init__(self, root: Path, work: Path, trash: Path, scratch: Path | None = None):
        self.root = Path(root)
        self.work = Path(work)
        self.trash = Path(trash)
        self.scratch = Path(scratch) if scratch else None

    # -- validation -----------------------------------------------------
    def _absolute(self, path: Path) -> Path:
        p = Path(path)
        if not p.is_absolute():
            raise PathDenied(f"path must be absolute: {p}")
        if len(p.parts) < MIN_COMPONENTS:
            raise PathDenied(f"path has fewer than {MIN_COMPONENTS} components: {p}")
        # Normalise without resolving symlinks.
        try:
            p = Path(os.path.normpath(str(p)))
            _assert_no_symlink(p)
        except (ValueError, OSError) as exc:
            raise PathDenied(f"invalid path {p!r}: {exc}") from exc
        return p

    def _deletion_roots(self, scratch: Path | None = None) -> list[Path]:
        roots = [self.work, self.trash]
        if scratch is not None:
            roots.append(Path(scratch))
        return roots

    def check_deletable(self, path: Path, scratch: Path | None = None) -> Path:
        p = self._absolute(path)
        for base in self._deletion_roots(scratch):
            base = Path(os.path.normpath(str(base)))
            if p != base and _under(p, base):
                return p
        raise PathDenied(f"refusing deletion outside work/trash/scratch: {p}")

    # -- deletion -------------------------------------------------------
    def remove(self, path: Path, reason: str, scratch: Path | None = None) -> None:
        """Remove a file/tree inside work, trash, or a supplied scratch dir.

        Refuses symlinks (the final path and every child of a directory), and
        logs each removed item.  Directories are walked without following
        links, and a pre-scan ensures an operation cannot delete half a tree
        and then discover a symlink.
        """
        p = self.check_deletable(path, scratch=scratch)
        if not p.exists() and not p.is_symlink():
            log.info("remove: already absent path=%s reason=%s", p, reason)
            return
        if p.is_symlink():
            raise PathDenied(f"refusing to remove symlink: {p}")
        self._scan_for_symlinks(p)
        if p.is_dir():
            self._remove_tree(p, reason)
        else:
            p.unlink()
            log.warning("removed file path=%s reason=%s", p, reason)
            fsync_dir(p.parent)
        _prune_empty_dirs(p.parent, stop=self.work if _under(p, self.work) else self.trash)

    def _scan_for_symlinks(self, path: Path) -> None:
        if path.is_symlink():
            raise PathDenied(f"refusing symlink in deletion tree: {path}")
        if path.is_dir():
            for child in path.iterdir():
                if child.is_symlink():
                    raise PathDenied(f"refusing symlink in deletion tree: {child}")
                self._scan_for_symlinks(child)

    def _remove_tree(self, path: Path, reason: str) -> None:
        for child in list(path.iterdir()):
            if child.is_symlink():
                raise PathDenied(f"refusing symlink in deletion tree: {child}")
            if child.is_dir():
                self._remove_tree(child, reason)
            else:
                child.unlink()
                log.warning("removed file path=%s reason=%s", child, reason)
        path.rmdir()
        log.warning("removed directory path=%s reason=%s", path, reason)
        fsync_dir(path.parent)

    # -- moves ----------------------------------------------------------
    def rename(self, src: Path, dest: Path, what: str = "move") -> None:
        """Atomic no-replace rename on the same filesystem."""
        src = self._absolute(src)
        dest = Path(os.path.normpath(str(dest)))
        if not dest.is_absolute():
            raise PathDenied(f"destination must be absolute: {dest}")
        if src == dest:
            return
        if not _under(dest, self.root):
            # Review/failed destinations are under root; this catches typos
            # while still allowing inbox/convert/movies moves.
            raise PathDenied(f"move destination outside root: {dest}")
        _assert_no_symlink(dest.parent)
        if _same_device(src, dest.parent):
            pass
        else:
            raise CrossDeviceError(f"cross-device move refused: {src} -> {dest}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            raise SafetyError(f"destination already exists: {dest}")
        os.rename(src, dest)
        log.warning("%s path=%s -> %s", what, src, dest)
        fsync_dir(src.parent)
        if dest.parent != src.parent:
            fsync_dir(dest.parent)

    def rename_noreplace(self, src: Path, dest: Path, what: str = "publish") -> None:
        """Publish with renameat2(RENAME_NOREPLACE) when available."""
        self._absolute(src)
        dest = Path(os.path.normpath(str(dest)))
        if not dest.is_absolute() or not _under(dest, self.root):
            raise PathDenied(f"publish destination outside root: {dest}")
        if not _under(dest, self.root):
            raise PathDenied(f"publish destination outside root: {dest}")
        _assert_no_symlink(dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(dest)
        if _same_device(src, dest.parent) is False:
            raise CrossDeviceError(f"cross-device publish refused: {src} -> {dest}")
        ok = False
        if platform.system() == "Linux":
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                renameat2 = libc.renameat2
                renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                renameat2.restype = ctypes.c_int
                at_fdcwd = -100
                rc = renameat2(at_fdcwd, os.fsencode(src), at_fdcwd, os.fsencode(dest), RENAME_NOREPLACE)
                if rc == 0:
                    ok = True
                elif ctypes.get_errno() == errno.EEXIST:
                    raise FileExistsError(dest)
            except FileExistsError:
                # FileExistsError is an OSError; never let it fall through to
                # an overwriting rename.
                raise
            except (AttributeError, OSError):
                ok = False
        if not ok:
            # The fallback would be check-then-rename, which can overwrite a
            # destination created by a concurrent writer (SAF-4). Publishing
            # must fail closed instead.
            raise SafetyError(
                "atomic no-replace rename is unavailable; refusing to publish "
                f"over a possible destination: {dest}"
            )
        log.warning("%s path=%s -> %s", what, src, dest)
        fsync_dir(src.parent)
        if dest.parent != src.parent:
            fsync_dir(dest.parent)

    def retire_move(self, src: Path, trash_root: Path, job_id: str, *, date: str | None = None,
                    suffix: str | None = None) -> Path:
        """Move an original or replaced library folder into trash; never delete."""
        src = self._absolute(src)
        date = date or time.strftime("%Y-%m-%d")
        name = job_id if not suffix else f"{job_id}-{suffix}"
        dest = Path(trash_root) / date / name
        n = 1
        while dest.exists():
            n += 1
            dest = Path(trash_root) / date / f"{name}-{n}"
        self.rename(src, dest, what="retire")
        return dest


def check_one_filesystem(paths: Iterable[Path]) -> dict[Path, int]:
    devices: dict[Path, int] = {}
    missing: list[Path] = []
    for p in paths:
        p = Path(p)
        try:
            devices[p] = p.stat().st_dev
        except FileNotFoundError:
            missing.append(p)
    if missing:
        raise SafetyError("paths missing: " + ", ".join(str(p) for p in missing))
    if len(set(devices.values())) != 1:
        detail = ", ".join(f"{p}={d}" for p, d in devices.items())
        raise CrossDeviceError(f"pipeline paths are not on one filesystem: {detail}")
    return devices


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove empty ancestors up to (not including) stop."""
    cur = start
    while _under(cur, stop):
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent
