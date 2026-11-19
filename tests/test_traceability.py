"""Traceability meta-test: registry, SPEC and every test name its IDs."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "requirements.toml"
SPEC = ROOT / "docs" / "SPEC.md"
INVARIANTS = ROOT / "docs" / "INVARIANTS.md"
TESTS = ROOT / "tests"

ID_RE = re.compile(r"\b(?:SAF|SCHED|ENC|REV|MP4|MKV|SIZE)-\d+\b")


def _registry() -> dict:
    with REGISTRY.open("rb") as fh:
        return tomllib.load(fh)


def _ids(text: str) -> set[str]:
    return set(ID_RE.findall(text))


def test_registry_is_well_formed():
    data = _registry()
    reqs = data["requirements"]
    ids = [r["id"] for r in reqs]
    assert len(ids) == len(set(ids)), "duplicate requirement id"
    for req in reqs:
        assert req["text"].strip(), f"empty text: {req['id']}"
        assert req["title"].strip()
        assert isinstance(req["safety"], bool)
        assert req["section"]
        assert ID_RE.fullmatch(req["id"]), req["id"]


def test_spec_and_registry_match_exactly():
    data = _registry()
    ids = {r["id"] for r in data["requirements"]}
    assert SPEC.exists(), "docs/SPEC.md is required"
    spec_ids = _ids(SPEC.read_text(encoding="utf-8"))
    assert spec_ids == ids, (
        f"only in registry: {sorted(ids - spec_ids)}; only in SPEC: {sorted(spec_ids - ids)}"
    )


def test_invariants_are_generated_and_current():
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_invariants.py"),
                           str(ROOT), "--check"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    rendered = subprocess.run([sys.executable, "-c", (
        "import sys; sys.path.insert(0, %r);"
        "import tomllib, importlib.util;"
        "spec=importlib.util.spec_from_file_location('gi', %r);"
        "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(m.render(m.load(__import__('pathlib').Path(%r))), end='')"
    ) % (str(ROOT), str(ROOT / "tools" / "gen_invariants.py"), str(REGISTRY))]
        , capture_output=True, text=True)
    if rendered.returncode == 0:
        assert INVARIANTS.read_text(encoding="utf-8") == rendered.stdout
    unsafe = [r["id"] for r in _registry()["requirements"] if not r["safety"]]
    bullet_ids = set()
    for line in INVARIANTS.read_text(encoding="utf-8").splitlines():
        if line.startswith("- **["):
            bullet_ids.add(line.split("**[", 1)[1].split("]", 1)[0])
    safety_ids = {r["id"] for r in _registry()["requirements"] if r["safety"]}
    assert bullet_ids == safety_ids
    assert not (set(unsafe) & bullet_ids)


def test_every_requirement_has_a_test_marker():
    data = _registry()
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in TESTS.glob("test_*.py")
                       if p.name != "test_traceability.py")
    missing = [r["id"] for r in data["requirements"] if r["id"] not in corpus]
    assert not missing, "requirements with no test marker: " + ", ".join(missing)


def test_declared_test_files_exist_and_name_the_requirement():
    """The registry's test path must be real and mention its own ID."""
    data = _registry()
    problems = []
    for req in data["requirements"]:
        path = ROOT / req["test"]
        if not path.exists():
            problems.append(f"{req['id']}: missing {req['test']}")
        elif req["id"] not in path.read_text(encoding="utf-8"):
            problems.append(f"{req['id']}: not named in {req['test']}")
    assert not problems, "; ".join(problems)


def test_no_orphan_markers_in_tests():
    """Every ID mentioned in tests must be in the registry (catches typos)."""
    registry = {r["id"] for r in _registry()["requirements"]}
    for path in TESTS.glob("test_*.py"):
        if path.name == "test_traceability.py":
            continue
        for needed in _ids(path.read_text(encoding="utf-8")):
            assert needed in registry, f"unknown requirement marker {needed} in {path.name}"
