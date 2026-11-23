#!/usr/bin/env python3
"""Create only approved media paths; never touch music or existing metadata."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

PIPELINE = Path("_pipeline")
REQUIRED = (
    PIPELINE,
    PIPELINE / "inbox",
    PIPELINE / "convert",
    PIPELINE / "convert" / "movies",
    PIPELINE / "work",
    PIPELINE / "review",
    PIPELINE / "failed",
    PIPELINE / "trash",
)
PRESETS = (
    "keep-remux-mp4", "keep-remux-mkv", "1080p-balanced-mp4",
    "1080p-small-mp4", "720p-small-mp4",
)
MODE = 0o2775


def _preflight(root: Path, paths: list[Path]) -> None:
    """Refuse symlinks at any existing component of an intended new path."""

    for path in [root, *paths]:
        current = path
        while current != current.parent:
            if current.is_symlink():
                raise ValueError(f"refusing symlink in media path: {current}")
            if current.exists():
                break
            current = current.parent


def initialize(root: Path, *, apply: bool, uid: int | None = None,
               gid: int | None = None) -> list[str]:
    root = Path(root).resolve()
    if root == Path("/srv/media") and os.environ.get("MEDIAFLOW_MEDIA_INIT_APPROVED") != "1":
        raise ValueError("production media initialization needs explicit approval")
    all_paths = list(REQUIRED) + [
        PIPELINE / "convert" / "movies" / preset for preset in PRESETS
    ]
    _preflight(root, all_paths)
    created: list[str] = []
    for relative in all_paths:
        path = root / relative
        if path.is_symlink():
            raise ValueError(f"refusing symlink media path: {path}")
        if path.is_dir():
            continue
        if path.exists():
            raise ValueError(f"media path exists and is not a directory: {path}")
        if apply:
            path.mkdir(parents=False, exist_ok=False)
            os.chmod(path, MODE)
            if uid is not None and gid is not None:
                os.chown(path, uid, gid)
        created.append(str(path))
    return created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and os.environ.get("MEDIAFLOW_MEDIA_INIT_APPROVED") != "1":
        raise SystemExit("MEDIAFLOW_MEDIA_INIT_APPROVED=1 is required for --apply")
    uid = os.environ.get("MEDIAFLOW_MEDIA_INIT_UID")
    gid = os.environ.get("MEDIAFLOW_MEDIA_INIT_GID")
    created = initialize(
        args.root, apply=args.apply,
        uid=int(uid) if uid else None, gid=int(gid) if gid else None,
    )
    print("\n".join(created))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
