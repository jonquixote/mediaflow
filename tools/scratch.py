#!/usr/bin/env python3
"""Identity-checked scratch-root helpers for rehearsals."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

PREFIX = "mediaflow-rehearsal."
MARKER = ".mediaflow-scratch"


def validate_scratch(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"scratch root is a symlink: {path}")
    resolved = path.resolve()
    tmp_root = Path("/tmp").resolve()
    if tmp_root != resolved and tmp_root not in resolved.parents:
        raise ValueError(f"scratch root is not under /tmp: {resolved}")
    if not resolved.name.startswith(PREFIX) or len(resolved.name) <= len(PREFIX):
        raise ValueError(f"scratch root lacks the expected identity: {resolved}")
    marker = resolved / MARKER
    if not marker.is_file():
        raise ValueError(f"scratch root lacks its creation marker: {resolved}")
    if marker.read_text(encoding="utf-8").strip() != "created-by-rehearsal":
        raise ValueError(f"scratch marker mismatch: {resolved}")
    if resolved.stat().st_dev != marker.stat().st_dev:
        raise ValueError(f"scratch device changed: {resolved}")
    return resolved


def cleanup(path: Path) -> None:
    shutil.rmtree(validate_scratch(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "cleanup"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    resolved = validate_scratch(args.path)
    if args.action == "cleanup":
        shutil.rmtree(resolved)
    print(resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
