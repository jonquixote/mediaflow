#!/usr/bin/env python3
"""Identity-checked scratch-root helpers for rehearsals."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

PREFIX = "mediaflow-rehearsal."


def validate_scratch(path: Path) -> Path:
    path = Path(path)
    resolved = path.resolve()
    tmp_root = Path("/tmp").resolve()
    if tmp_root != resolved and tmp_root not in resolved.parents:
        raise ValueError(f"scratch root is not under /tmp: {resolved}")
    if not resolved.name.startswith(PREFIX) or len(resolved.name) <= len(PREFIX):
        raise ValueError(f"scratch root lacks the expected identity: {resolved}")
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
