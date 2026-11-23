#!/usr/bin/env python3
"""Initialize a scratch state database and media layout without a daemon."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediaflow.config import load_config  # noqa: E402
from mediaflow.db import Store  # noqa: E402
from mediaflow.supervision import slot_path  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: scratch_db.py CONFIG")
    config_path = Path(sys.argv[1])
    if not config_path.is_file():
        raise SystemExit(f"config not found: {config_path}")
    cfg = load_config(config_path, environ={})
    if str(cfg.root) == "/srv/media":
        raise SystemExit("refusing production media root")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    slot_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    for path in (cfg.movies, cfg.inbox, cfg.convert, cfg.work,
                 cfg.review, cfg.failed, cfg.trash):
        path.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.initialize()
    store.close()
    print(f"scratch database initialized: {cfg.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
