#!/usr/bin/env python3
"""Scope-limited rollback helper for a recorded mediaflow install.

Removes only empty directories that the manifest says this installation
created. It never removes files, shared directories, state, config, secrets,
evidence, backups or anything under /srv/media.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

PROTECTED_PREFIXES = (Path("/srv/media"), Path("/etc/mediaflow"),
                      Path("/var/lib/mediaflow"), Path("/run/mediaflow"))


def rollback(manifest_path: Path, *, apply: bool) -> list[str]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed: list[str] = []
    for raw in data.get("created", []):
        path = Path(raw)
        resolved = path.resolve() if path.exists() else path.absolute()
        if not resolved.is_absolute():
            raise ValueError(f"created path is not absolute: {raw}")
        if any(resolved == prefix or prefix in resolved.parents for prefix in PROTECTED_PREFIXES):
            # Shared managed roots are never removed.
            continue
        if not path.is_dir():
            continue
        try:
            entries = list(path.iterdir())
        except OSError:
            continue
        if entries:
            continue
        if apply:
            path.rmdir()
        removed.append(str(path))
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
