#!/usr/bin/env python3
"""Redacted evidence bundles, integrity validation and explicit acceptance.

Bundle integrity, a claim review and registry mutation are separate steps.
A successful command or a syntactically valid bundle is not proof that a
hardware claim was tested, and the agent never marks on-box items verified.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "hardware_checks.toml"
MANIFEST = "manifest.json"
ACCEPTANCE = "acceptance.json"

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(token|secret|password|authorization|api[_-]?key|notify[_-]?url|tmdb[_-]?read[_-]?token)"
    r"\s*[=:]\s*(?!\*\*\*)\S+"
)
_BEARER = re.compile(r"(?i)bearer\s+(?!\*\*\*)[A-Za-z0-9._\-]+")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@(?P<host>[^/\s]+)")
_TITLE = re.compile(r"(?<![\w])[A-Z][\w .'\-]{1,80}\(\d{4}\)(?:\[[^\]]+\])?")
_MEDIA_PATH = re.compile(r"/srv/media(?!/\[REDACTED\])")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ITEM_ID = re.compile(r"HW-\d{3}")


def redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer ***", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=***", text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}***@{m.group('host')}/…", text)
    text = _TITLE.sub("[TITLE]", text)
    return _MEDIA_PATH.sub("/srv/media/[REDACTED]", text)


def _revision() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def capture(bundle: Path, command: list[str], *, item_id: str, cwd: Path | None = None,
            extra: dict | None = None) -> dict:
    bundle = Path(bundle)
    bundle.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    proc = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    outputs = {}
    for name, text in (("stdout.txt", proc.stdout), ("stderr.txt", proc.stderr)):
        filename = f"{item_id}-{stamp}-{name}"
        target = bundle / filename
        target.write_text(redact_text(text), encoding="utf-8")
        outputs[filename] = str(target)
    manifest = {
        "schema": 2,
        "item_id": item_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "revision": _revision(),
        "command": [redact_text(part) for part in command],
        "exit_code": proc.returncode,
        "outputs": outputs,
        "claim_demonstrated": False,
        "extra": redact_text(json.dumps(extra or {}, sort_keys=True)),
    }
    (bundle / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (bundle / ACCEPTANCE).unlink(missing_ok=True)
    return manifest


def _resolve_output(directory: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = directory / path
    resolved = path.resolve()
    root = directory.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"output escapes bundle: {raw}")
    if path.is_symlink():
        raise ValueError(f"output is a symlink: {raw}")
    return resolved


def validate(bundle: Path, *, expected_revision: str | None = None) -> dict:
    bundle = Path(bundle)
    path = bundle / MANIFEST
    if not path.is_file():
        raise ValueError(f"missing {MANIFEST}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable manifest: {exc}") from exc
    required = {"schema", "item_id", "timestamp", "revision", "command",
                "exit_code", "outputs"}
    if not required <= set(manifest):
        raise ValueError(f"incomplete manifest: {sorted(required - set(manifest))}")
    if manifest["schema"] != 2:
        raise ValueError("unsupported bundle schema")
    if not _ITEM_ID.fullmatch(str(manifest["item_id"])):
        raise ValueError("invalid item_id")
    if not _REVISION.fullmatch(str(manifest["revision"])):
        raise ValueError("revision must be a full 40-character SHA")
    if expected_revision and str(manifest["revision"]) != str(expected_revision):
        raise ValueError("bundle revision does not match expected revision")
    if not manifest["command"]:
        raise ValueError("empty command")
    if manifest["exit_code"] != 0:
        raise ValueError(f"command failed with {manifest['exit_code']}")
    if not manifest["outputs"]:
        raise ValueError("no output evidence")
    for raw in manifest["outputs"]:
        output = _resolve_output(path.parent, str(raw))
        if not output.is_file():
            raise ValueError(f"missing evidence file: {raw}")
        text = output.read_text(encoding="utf-8", errors="replace")
        if redact_text(text) != text:
            raise ValueError(f"unredacted content in {output}")
    return manifest


def bundle_digest(bundle: Path, manifest: dict | None = None) -> str:
    """Digest the exact reviewed manifest fields and output bytes."""

    bundle = Path(bundle)
    manifest = manifest or validate(bundle)
    import hashlib

    digest = hashlib.sha256()
    canonical = {key: value for key, value in manifest.items()
                 if key not in {"claim_demonstrated", "extra"}}
    digest.update(json.dumps(canonical, sort_keys=True).encode("utf-8"))
    for raw in sorted(manifest["outputs"]):
        digest.update(str(raw).encode("utf-8"))
        digest.update(_resolve_output(bundle, str(raw)).read_bytes())
    return digest.hexdigest()


def accept(bundle: Path, *, accepted_by: str, notes: str, expected_revision: str) -> dict:
    manifest = validate(bundle, expected_revision=expected_revision)
    path = Path(bundle) / MANIFEST
    attestation = {
        "schema": 1,
        "item_id": manifest["item_id"],
        "accepted_by": accepted_by,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "expected_revision": expected_revision,
        "bundle_digest": bundle_digest(bundle, manifest),
        "claim_verified": True,
        "notes": redact_text(notes),
    }
    (path.parent / ACCEPTANCE).write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    return attestation


def update_registry(bundle: Path, registry: Path = REGISTRY,
                    expected_revision: str | None = None) -> str:
    if not expected_revision:
        raise ValueError("--expected-revision is required for registry mutation")
    manifest = validate(bundle, expected_revision=expected_revision)
    acceptance_path = Path(bundle) / ACCEPTANCE
    if not acceptance_path.is_file():
        raise ValueError("explicit acceptance is required before registry mutation")
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    if acceptance.get("item_id") != manifest["item_id"]:
        raise ValueError("acceptance item mismatch")
    if expected_revision and acceptance.get("expected_revision") != expected_revision:
        raise ValueError("acceptance revision mismatch")
    if not acceptance.get("claim_verified"):
        raise ValueError("claim was not explicitly verified")
    if acceptance.get("bundle_digest") != bundle_digest(bundle, manifest):
        raise ValueError("bundle changed after acceptance; re-accept required")

    registry = Path(registry)
    text = registry.read_text(encoding="utf-8")
    blocks = text.split("[[checks]]")
    changed = False
    for index, block in enumerate(blocks):
        if f'id = "{manifest["item_id"]}"' not in block:
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
        raise ValueError(f"registry item not found: {manifest['item_id']}")
    registry.write_text("[[checks]]".join(blocks), encoding="utf-8")
    return str(manifest["item_id"])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    capture_p = sub.add_parser("capture")
    capture_p.add_argument("--bundle", required=True, type=Path)
    capture_p.add_argument("--item", required=True)
    capture_p.add_argument("--cwd", type=Path)
    capture_p.add_argument("cmd", nargs=argparse.REMAINDER)
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("bundle", type=Path)
    validate_p.add_argument("--expected-revision")
    accept_p = sub.add_parser("accept")
    accept_p.add_argument("bundle", type=Path)
    accept_p.add_argument("--accepted-by", required=True)
    accept_p.add_argument("--notes", required=True)
    accept_p.add_argument("--expected-revision", required=True)
    update_p = sub.add_parser("update-registry")
    update_p.add_argument("bundle", type=Path)
    update_p.add_argument("--expected-revision")
    update_p.add_argument("--registry", type=Path, default=REGISTRY)
    args = parser.parse_args(argv)

    if args.action == "capture":
        if not args.cmd or args.cmd[0] == "--":
            args.cmd = args.cmd[1:]
        if not args.cmd:
            raise SystemExit("no command supplied to capture")
        manifest = capture(args.bundle, args.cmd, item_id=args.item, cwd=args.cwd)
        print(json.dumps(manifest, indent=2))
        return 0
    if args.action == "validate":
        validate(args.bundle, expected_revision=args.expected_revision)
        print("bundle integrity valid")
        return 0
    if args.action == "accept":
        attestation = accept(args.bundle, accepted_by=args.accepted_by,
                             notes=args.notes, expected_revision=args.expected_revision)
        print(json.dumps(attestation, indent=2))
        return 0
    update_registry(args.bundle, registry=args.registry,
                    expected_revision=args.expected_revision)
    print("registry updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
