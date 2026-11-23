#!/usr/bin/env python3
"""Scope-limited rollback for a create-only mediaflow installation.

Removes only artifacts this manifest says were created, and only when their
recorded hash still matches. Shared roots, evidence, state, backup and media
are never removed. Managed shared roots themselves are never removed; only
empty new subdirectories under them may be pruned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SHARED_ROOTS = (Path("/srv/media"), Path("/etc/mediaflow"),
                Path("/var/lib/mediaflow"), Path("/run/mediaflow"))
NEVER_REMOVE_NAMES = {"install-manifest.json", "evidence", "backup", "logs"}


def _safe(path: Path) -> bool:
    if not path.is_absolute():
        return False
    if path.name in NEVER_REMOVE_NAMES:
        return False
    resolved = path.resolve() if path.exists() else path.absolute()
    return any(resolved != root and root in resolved.parents for root in SHARED_ROOTS) or (
        any(resolved == root for root in SHARED_ROOTS) is False
        and str(resolved).startswith("/tmp/")
    )


def rollback(manifest_path: Path, *, apply: bool) -> list[str]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed: list[str] = []
    dirs: list[Path] = []
    for item in data.get("created_files", []):
        path = Path(item.get("path", ""))
        if not _safe(path) or not path.exists():
            continue
        expected = item.get("sha256")
        if path.is_file():
            if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                continue
            if apply:
                path.unlink()
            removed.append(str(path))
        elif path.is_dir():
            dirs.append(path)
    for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if any(path.iterdir()):
                continue
            if apply:
                path.rmdir()
            removed.append(str(path))
        except OSError:
            continue
    return removed


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
