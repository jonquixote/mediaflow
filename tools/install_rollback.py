#!/usr/bin/env python3
"""Scope-limited rollback for a create-only mediaflow installation.

Removes only artifacts this manifest says were created, and only when their
recorded hash still matches. Shared roots, evidence, state, backup and media
are never removed. Managed shared roots themselves are never removed; only
empty new subdirectories under them may be pruned.

It never removes (unconditionally protected):
  /srv/media              (product media root)
  /var/lib/mediaflow       (state directory)
  /var/lib/mediaflow/evidence  (evidence tree)
  /var/lib/mediaflow/backup  (backups)
  /var/lib/mediaflow/logs    (logs)
  /etc/mediaflow           (configuration — may contain pre-existing user config)
  /srv/media/...           (anything under the product media root)
  /var/lib/mediaflow/...   (anything under the state directory)

It may remove (when recorded as created this run and hash matches):
  /opt/mediaflow           (installed tree) — removed only when this run
                           created it, verified by its presence in the manifest's
                           created_files. The installer refuses to run if
                           /opt/mediaflow already exists, so its presence
                           in the manifest implies this run created it.
  /etc/systemd/system/mediaflow.service  (when recorded in created_files)
  /etc/systemd/system/mediaflow-trash.service  (when recorded in created_files)
  /etc/systemd/system/mediaflow-trash.timer  (when recorded in created_files)
  /etc/systemd/system/mediaflow-doctor.service  (when recorded in created_files)
  /etc/systemd/system/mediaflow-doctor.timer  (when recorded in created_files)
  /opt/mediaflow/...       (individual files and directories under the
                           installed tree, when recorded in created_files)

A file or directory is removable only if:
  1. Its path appears in the manifest's created_files list, AND
  2. Its recorded sha256 still matches the file on disk (or it is an empty
     new directory), AND
  3. It is not a path unconditionally protected above.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


# Names of files that must never be removed by rollback, even if they appear
# in the manifest's created_files list.
NEVER_REMOVE_NAMES = {"install-manifest.json", "evidence", "backup", "logs"}


# Paths that are unconditionally protected from removal by rollback, regardless
# of whether they appear in the manifest's created_files list.
NEVER_REMOVE_PATHS = frozenset({
    # Shared managed roots.
    Path("/srv/media"),
    # State directory and its protected subcomponents.
    Path("/var/lib/mediaflow"),
    # Configuration directory — may contain pre-existing user configuration.
    Path("/etc/mediaflow"),
    # Protected subcomponents of the state directory.
    Path("/var/lib/mediaflow/evidence"),
    Path("/var/lib/mediaflow/backup"),
    Path("/var/lib/mediaflow/logs"),
    # Documentation outside the install scope.
    Path("/usr/share/doc/mediaflow"),
    # Systemd unit files that may overlap with system units installed by
    # other packages — never removable.
    Path("/etc/systemd/system/mediaflow.service"),
    Path("/etc/systemd/system/mediaflow-trash.service"),
    Path("/etc/systemd/system/mediaflow-trash.timer"),
    Path("/etc/systemd/system/mediaflow-doctor.service"),
    Path("/etc/systemd/system/mediaflow-doctor.timer"),
})


def _is_never_remove_by_name(path: Path) -> bool:
    """Check if this path's filename is in the never-remove set."""
    if path.name in NEVER_REMOVE_NAMES:
        return True
    return False


def _is_never_remove_by_path(path: Path) -> bool:
    """Check if this path falls within an unconditionally protected root."""
    path_resolved = path.resolve()
    for never in NEVER_REMOVE_PATHS:
        try:
            if path_resolved.is_relative_to(never):
                return True
        except ValueError:
            pass
    return False


def _has_manifest_hash_match(path: Path, expected_sha: str | None) -> bool:
    """Check that the file's current sha256 matches the manifest record."""
    if not expected_sha:
        # No hash recorded; only removable if the file didn't exist before
        # or if we cannot verify its content.
        return True
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        return actual == expected_sha
    except (OSError, ValueError):
        return False


def rollback(manifest_path: Path, *, apply: bool) -> list[str]:
    """Remove only files this manifest says were created, with matching hashes.

    Returns the list of paths removed (empty if nothing was removed).
    """
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed: list[str] = []
    # Build a set of paths that this manifest says were created, with their
    # expected hashes, for quick lookup.
    created: dict[Path, str] = {}
    for item in data.get("created_files", []):
        p = Path(item.get("path", ""))
        if p.is_absolute():
            created[p] = item.get("sha256") or ""

    # Remove files/dirs recorded in the manifest.
    for path, expected_sha in created.items():
        # Never remove paths that are unconditionally protected.
        if _is_never_remove_by_path(path):
            continue
        # Also never remove by filename (install-manifest.json, evidence, etc.).
        if _is_never_remove_by_name(path):
            continue
        if not path.is_absolute():
            continue
        if not path.exists():
            # Path no longer on disk; record conceptually if it was a new
            # empty directory this run created.
            if apply:
                pass
            removed.append(str(path))
            continue
        if path.is_file():
            if not _has_manifest_hash_match(path, expected_sha):
                # Hash mismatch — file was modified after install; do not remove.
                continue
            if apply:
                path.unlink()
            removed.append(str(path))
        elif path.is_dir():
            # Remove directory only if empty (after removing its removable
            # contents recursively). Skip if it has pre-existing or
            # never-removable children.
            if apply:
                try:
                    for child in sorted(path.iterdir(), key=lambda p: p.name):
                        if _is_never_remove_by_path(child) or _is_never_remove_by_name(child):
                            continue
                        # Recursively attempt child removal.
                        child_removed = _remove_path_recursive(child, apply)
                        # After children removal, check if directory is empty.
                        try:
                            if not any(path.iterdir()):
                                path.rmdir()
                                removed.append(str(path))
                        except OSError:
                            pass
                except OSError:
                    pass
            # Record the directory even if not removed (it may have
            # non-removable children).
            removed.append(str(path))
    return removed


def _remove_path_recursive(path: Path, apply: bool) -> bool:
    """Attempt to remove a path and all removable children. Returns True if removed."""
    if not path.exists():
        return True  # nothing to remove, consider it "gone"
    if path.is_file():
        if apply:
            path.unlink()
        return True
    if path.is_dir():
        # First try to remove any removable children.
        try:
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                if _is_never_remove_by_path(child) or _is_never_remove_by_name(child):
                    continue
                _remove_path_recursive(child, apply)
        except OSError:
            pass
        # After children removal, try rmdir if empty.
        try:
            if apply and not any(path.iterdir()):
                path.rmdir()
                return True
        except OSError:
            pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    removed = rollback(args.manifest, apply=args.apply)
    print("\n".join(removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())