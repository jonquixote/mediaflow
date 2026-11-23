#!/usr/bin/env python3
"""Redacted on-box evidence bundles and registry status updates.

The sandbox agent never runs on-box checks and never marks an item
verified-on-hardware. This tool is for John: it records command output with
secrets and titles removed, validates a completed bundle, and only then writes
the verified status into docs/hardware_checks.toml.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "hardware_checks.toml"

MANIFEST = "manifest.json"

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(token|secret|password|authorization|api[_-]?key|notify[_-]?url|tmdb[_-]?read[_-]?token)"
    r"\s*[=:]\s*\S+"
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@(?P<host>[^/\s]+)")
_TITLE = re.compile(r"(?<![\w])[A-Z][\w .'\-]{1,80}\(\d{4}\)(?:\[[^\]]+\])?")
_MEDIA_PATH = re.compile(r"/srv/media\b")


def redact_text(text: str) -> str:
    """Remove secrets, credentials and likely media titles from captured text."""

    text = _BEARER.sub("Bearer ***", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=***", text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}***@{m.group('host')}/…", text)
    text = _TITLE.sub("[TITLE]", text)
    text = _MEDIA_PATH.sub("/srv/media/[REDACTED]", text)
    return text


def _revision() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def capture(bundle: Path, command: list[str], *, item_id: str, cwd: Path | None = None,
            extra: dict | None = None) -> dict:
    """Run a command and store its redacted output in a timestamped bundle."""

    bundle = Path(bundle)
    directory = bundle / item_id / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    directory.mkdir(parents=True, exist_ok=False)
    proc = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    outputs = {}
    for name, text in (("stdout.txt", proc.stdout), ("stderr.txt", proc.stderr)):
        (directory / name).write_text(redact_text(text), encoding="utf-8")
        outputs[name] = str(directory / name)
    manifest = {
        "schema": 1,
        "item_id": item_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "revision": _revision(),
        "command": [redact_text(part) for part in command],
        "exit_code": proc.returncode,
        "outputs": outputs,
        "extra": redact_text(json.dumps(extra or {}, sort_keys=True)),
    }
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def validate(bundle: Path) -> dict:
    """Return the manifest if the bundle is complete, consistent and safe."""

    bundle = Path(bundle)
    path = bundle / MANIFEST
    if not path.is_file():
        raise ValueError(f"missing {MANIFEST}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable manifest: {exc}") from exc
    required = {"schema", "item_id", "timestamp", "revision", "command", "exit_code", "outputs"}
    if not required <= set(manifest):
        raise ValueError(f"incomplete manifest: {sorted(required - set(manifest))}")
    if manifest["schema"] != 1:
        raise ValueError("unsupported bundle schema")
    if not manifest["item_id"] or not re.fullmatch(r"HW-\d{3}", str(manifest["item_id"])):
        raise ValueError("invalid item_id")
    if not manifest["command"]:
        raise ValueError("empty command")
    if manifest["exit_code"] != 0:
        raise ValueError(f"command failed with {manifest['exit_code']}")
    if not manifest["revision"] or len(str(manifest["revision"])) < 7:
        raise ValueError("missing revision")
    if not manifest["outputs"]:
        raise ValueError("no output evidence")
    for name in manifest["outputs"]:
        output = Path(name)
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError(f"missing or empty evidence file: {name}")
        text = output.read_text(encoding="utf-8", errors="replace")
        if _BEARER.search(text) or _SECRET_ASSIGNMENT.search(text) or _TITLE.search(text):
            raise ValueError(f"unredacted content in {output}")
    return manifest


def update_registry(bundle: Path, registry: Path = REGISTRY) -> str:
    """Mark one item verified only after its evidence bundle validates."""

    manifest = validate(bundle)
    item_id = str(manifest["item_id"])
    registry = Path(registry)
    text = registry.read_text(encoding="utf-8")
    blocks = text.split("[[checks]]")
    changed = False
    for index, block in enumerate(blocks):
        if f'id = "{item_id}"' not in block:
            continue
        block = re.sub(r'status = "[^"]*"', 'status = "verified-on-hardware"', block)
        block = re.sub(r'verified_at = "[^"]*"',
                       f'verified_at = "{manifest["timestamp"][:10]}"', block)
        block = re.sub(r'verified_revision = "[^"]*"',
                       f'verified_revision = "{manifest["revision"]}"', block)
        blocks[index] = block
        changed = True
        break
    if not changed:
        raise ValueError(f"registry item not found: {item_id}")
    registry.write_text("[[checks]]".join(blocks), encoding="utf-8")
    return item_id


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    capture_p = sub.add_parser("capture")
    capture_p.add_argument("--bundle", required=True, type=Path)
    capture_p.add_argument("--item", required=True)
    capture_p.add_argument("--cwd", type=Path)
    capture_p.add_argument("command", nargs=argparse.REMAINDER)
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("bundle", type=Path)
    update_p = sub.add_parser("update-registry")
    update_p.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)

    if args.command == "capture":
        manifest = capture(args.bundle, args.command, item_id=args.item, cwd=args.cwd)
        print(json.dumps(manifest, indent=2))
        return 0
    if args.command == "validate":
        validate(args.bundle)
        print("bundle valid")
        return 0
    update_registry(args.bundle)
    print("registry updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
