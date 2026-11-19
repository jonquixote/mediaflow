#!/usr/bin/env python3
"""Generate docs/INVARIANTS.md from docs/requirements.toml.

Only safety=true requirements are emitted. The output is deterministic;
tests compare it byte-for-byte with a fresh render.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

HEADER = """# mediaflow - INVARIANTS (generated from docs/requirements.toml; do not edit)

Normative, safety-critical subset of SPEC v1.0. Re-read before every build step. If a feature conflicts with a rule here, the feature loses.
"""


def load(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def render(registry: dict) -> str:
    lines = [HEADER.rstrip(), ""]
    for req in registry.get("requirements", []):
        if not req.get("safety"):
            continue
        lines.append(f"- **[{req['id']}]** {req['text']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    source = root / "docs" / "requirements.toml"
    target = root / "docs" / "INVARIANTS.md"
    rendered = render(load(source))
    if "--check" in argv:
        actual = target.read_text(encoding="utf-8") if target.exists() else ""
        if actual != rendered:
            print("docs/INVARIANTS.md is stale; run tools/gen_invariants.py", file=sys.stderr)
            return 1
        return 0
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
