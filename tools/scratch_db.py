#!/usr/bin/env python3
"""Initialize a scratch state database and media layout without a daemon.

This tool must be run with an explicit configuration that defines a scratch
root, an explicit state_dir, and an explicit runtime_dir all resolved inside
a scratch directory under /tmp/. It rejects any configuration that defaults
to production paths or resolves to production roots.

Python version requirement: 3.12 or newer (verified at runtime).
"""

from __future__ import annotations

import sys
from pathlib import Path
from mediaflow.supervision import slot_path  # noqa: E402

# The project requires Python 3.12 or newer.
PYTHON_VERSION_REQUIRED = (3, 12)


def _check_python_version() -> None:
    """Verify the running Python meets the version requirement."""
    if sys.version_info < PYTHON_VERSION_REQUIRED:
        raise SystemExit(
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            f"is installed; {PYTHON_VERSION_REQUIRED[0]}."
            f"{PYTHON_VERSION_REQUIRED[1]} or newer is required."
        )


def _resolve_and_validate(path: Path, label: str) -> Path:
    """Resolve a path, reject symlinks, and verify it exists under /tmp/."""
    if path.is_symlink():
        raise SystemExit(f"{label} is a symlink; scratch roots must not be symlinks: {path}")
    resolved = path.resolve()
    # The scratch root must be under /tmp/.
    if str(resolved).startswith("/tmp/") is False:
        raise SystemExit(
            f"{label} must resolve to a path under /tmp/, got: {resolved}"
        )
    return resolved


def main() -> int:
    _check_python_version()
    if len(sys.argv) != 2:
        raise SystemExit("usage: scratch_db.py CONFIG")
    config_path = Path(sys.argv[1])
    if not config_path.is_file():
        raise SystemExit(f"config not found: {config_path}")
    from mediaflow.config import load_config  # noqa: E402
    cfg = load_config(config_path, environ={})

    # Validate that root, state_dir, and runtime_dir are explicitly set
    # in the config and do not default to production paths.
    if not cfg.root:
        raise SystemExit("config error: root is not set; explicitly set state_dir and runtime_dir")
    if not cfg.state_dir:
        raise SystemExit("config error: state_dir is not set; explicitly set state_dir")
    if not cfg.runtime_dir:
        raise SystemExit("config error: runtime_dir is not set; explicitly set runtime_dir")

    # Resolve and validate each path.
    root = _resolve_and_validate(Path(cfg.root), "root")
    state_dir = _resolve_and_validate(Path(cfg.state_dir), "state_dir")
    runtime_dir = _resolve_and_validate(Path(cfg.runtime_dir), "runtime_dir")

    # Reject any path that resolves to a production root.
    production_roots = (Path("/srv/media"), Path("/var/lib/mediaflow"),
                        Path("/run/mediaflow"), Path("/etc/mediaflow"))
    for p_name, p_val in [("root", root), ("state_dir", state_dir),
                          ("runtime_dir", runtime_dir)]:
        for prod in production_roots:
            try:
                if p_val.resolve().is_relative_to(prod.resolve()):
                    raise SystemExit(
                        f"{p_name} ({p_val}) resolves to production root {prod}; "
                        "refusing to operate on production paths"
                    )
            except ValueError:
                pass

    # Verify the scratch layout: state_dir and runtime_dir must be under the
    # scratch base (root). They must all resolve under /tmp/ and must not
    # resolve to production roots.
    root_resolved = root.resolve()
    production_roots = (Path("/srv/media"), Path("/var/lib/mediaflow"),
                        Path("/run/mediaflow"), Path("/etc/mediaflow"))
    for p_name, p_val in [("state_dir", state_dir),
                          ("runtime_dir", runtime_dir)]:
        p_resolved = p_val.resolve()
        # Must be under /tmp/.
        if str(p_resolved).startswith("/tmp/") is False:
            raise SystemExit(
                f"{p_name} ({p_val}) must resolve to a path under /tmp/, got: {p_resolved}"
            )
        # Must not be a production root.
        for prod in production_roots:
            try:
                if p_resolved.is_relative_to(prod.resolve()):
                    raise SystemExit(
                        f"{p_name} ({p_val}) resolves to production root {prod}; "
                        "refusing to operate on production paths"
                    )
            except ValueError:
                pass
        # Must be distinct from root.
        if p_resolved == root_resolved:
            raise SystemExit(
                f"{p_name} ({p_val}) must be distinct from root ({root_resolved})"
            )

    # Initialize the database and media layout.
    from mediaflow.db import Store  # noqa: E402
    slot_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    for path in (cfg.movies, cfg.inbox, cfg.convert, cfg.work,
                 cfg.review, cfg.failed, cfg.trash):
        path.mkdir(parents=True, exist_ok=True)
    store = Store(state_dir / "state.db")
    store.initialize()
    store.close()
    print(f"scratch database initialized: {state_dir / 'state.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())