#!/usr/bin/env python3
"""Create only approved media paths; never touch music or existing metadata."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PIPELINE = Path("_pipeline")
REQUIRED = (
    PIPELINE / "inbox",
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


def initialize(root: Path, *, apply: bool) -> list[str]:
    root = Path(root).resolve()
    if root == Path("/srv/media") and os.environ.get("MEDIAFLOW_ALLOW_PRODUCTION_MEDIA") != "1":
        raise ValueError("production media initialization needs explicit approval")
    created: list[str] = []
    for relative in REQUIRED:
        path = root / relative
        if path.is_symlink():
            raise ValueError(f"refusing symlink media path: {path}")
        if path.is_dir():
            continue
        if path.exists():
            raise ValueError(f"media path exists and is not a directory: {path}")
        if apply:
            path.mkdir(parents=True, exist_ok=False, mode=0o2775)
        created.append(str(path))
    for preset in PRESETS:
        path = root / PIPELINE / "convert" / "movies" / preset
        if path.is_dir():
            continue
        if path.exists():
            raise ValueError(f"preset path exists and is not a directory: {path}")
        if apply:
            path.mkdir(parents=True, exist_ok=False, mode=0o2775)
        created.append(str(path))
    return created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and os.environ.get("MEDIAFLOW_MEDIA_INIT_APPROVED") != "1":
        raise SystemExit("MEDIAFLOW_MEDIA_INIT_APPROVED=1 is required for --apply")
    created = initialize(args.root, apply=args.apply)
    print("\n".join(created))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
