#!/usr/bin/env python3
"""Generate docs/HARDWARE_CHECKS.md from docs/hardware_checks.toml.

Never hand-edit the generated view. The registry is the only place where
verification status may be changed, and only recorded on-box evidence may set
``verified-on-hardware``.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "hardware_checks.toml"
REQUIREMENTS = ROOT / "docs" / "requirements.toml"
TARGET = ROOT / "docs" / "HARDWARE_CHECKS.md"
CHECKLIST = ROOT / "docs" / "FIRST_RUN_CHECKLIST.md"

CLASSES = {"required", "feature-conditional", "informational"}
RUNNERS = {"sandbox-only", "doctor-automatable", "manual-by-John"}
STATUSES = {"unverified", "verified-sandbox", "verified-on-hardware"}


def load(path: Path = REGISTRY) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def validate(data: dict, requirements: dict | None = None) -> None:
    checks = data.get("checks", [])
    assert checks, "hardware registry is empty"
    ids = [c["id"] for c in checks]
    assert len(ids) == len(set(ids)), "duplicate hardware check id"
    requirements = requirements or load_requirements()
    known = {r["id"] for r in requirements.get("requirements", [])}
    for check in checks:
        assert check["class"] in CLASSES, f"bad class for {check['id']}"
        assert check["runner"] in RUNNERS, f"bad runner for {check['id']}"
        assert check["status"] in STATUSES, f"bad status for {check['id']}"
        assert check.get("supports"), f"{check['id']} supports no requirement"
        for rid in check["supports"]:
            assert rid in known, f"{check['id']} references unknown requirement {rid}"
        if check["status"] == "verified-on-hardware":
            assert check.get("verified_at"), f"{check['id']} lacks verification date"
            assert check.get("verified_revision"), f"{check['id']} lacks verification revision"


def load_requirements() -> dict:
    with REQUIREMENTS.open("rb") as fh:
        return tomllib.load(fh)


def render(data: dict) -> str:
    checks = data["checks"]
    lines = [
        "# Hardware and deployment verification checks (generated)",
        "",
        "Generated from `docs/hardware_checks.toml`; do not edit by hand.",
        "The registry is authoritative for status. `docs/QUESTIONS.md` lists the",
        "open items, `docs/VERIFIED.md` records evidence, and `docs/RUNBOOK.md`",
        "gives the ordered procedure. The sandbox agent never marks an item",
        "`verified-on-hardware`.",
        "",
    ]
    for status in ("unverified", "verified-sandbox", "verified-on-hardware"):
        count = sum(1 for c in checks if c["status"] == status)
        lines.append(f"- {status}: {count}")
    lines += ["", "| ID | Class | Runner | Status | Supports | Title |",
              "|---|---|---|---|---|---|"]
    for c in checks:
        supports = ", ".join(c["supports"])
        lines.append(
            f"| {c['id']} | {c['class']} | {c['runner']} | {c['status']} | "
            f"{supports} | {c['title']} |"
        )
    lines += ["", "## Details", ""]
    for c in checks:
        lines += [
            f"### {c['id']} — {c['title']}",
            "",
            f"- Claim: {c['claim']}",
            f"- Class: {c['class']}; runner: {c['runner']}",
            f"- Supports: {', '.join(c['supports'])}",
            f"- Procedure: {c['procedure']}",
            f"- Expected: {c['expected']}",
            f"- Failure: {c['failure_interpretation']}",
            f"- Risk: {c['risk']}",
            f"- Disposable media: {str(c['disposable_media']).lower()}",
            f"- Evidence: `{c['evidence']}`",
            f"- Status: {c['status']}"
            + (f" ({c['verified_at']}, {c['verified_revision']})"
               if c["status"] == "verified-on-hardware" else ""),
            "",
        ]
    return "\n".join(lines) + "\n"


def render_checklist(data: dict) -> str:
    lines = [
        "# First-run checklist (generated)",
        "",
        "Generated from `docs/hardware_checks.toml`; do not edit by hand.",
        "John ticks a line only after a real on-box check and a validated",
        "evidence bundle. The sandbox agent never ticks on-box items.",
        "",
    ]
    for check in data["checks"]:
        lines.append(
            f"- [ ] {check['id']} — {check['title']} "
            f"(class {check['class']}, runner {check['runner']}, "
            f"evidence `{check['evidence']}`)"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    data = load()
    validate(data)
    rendered = render(data)
    checklist = render_checklist(data)
    if check:
        for target, expected in ((TARGET, rendered), (CHECKLIST, checklist)):
            actual = target.read_text(encoding="utf-8") if target.exists() else ""
            if actual != expected:
                print(f"{target} is stale; run tools/gen_hardware_checks.py", file=sys.stderr)
                return 1
        return 0
    TARGET.write_text(rendered, encoding="utf-8")
    CHECKLIST.write_text(checklist, encoding="utf-8")
    print(f"wrote {TARGET} and {CHECKLIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
