#!/usr/bin/env bash
# Local test runner. There is deliberately no hosted CI: hardware tests only
# mean anything on john-server. Refuses to run against the production root.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export MEDIAFLOW_ROOT="${MEDIAFLOW_ROOT:-/tmp/mediaflow-test}"
case "$(realpath -m "$MEDIAFLOW_ROOT")" in
  /srv/media|/srv/media/*)
    echo "refusing: MEDIAFLOW_ROOT resolves to production /srv/media" >&2
    exit 1
    ;;
esac

PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  echo "missing venv python: $PY (create .venv and install requirements-dev.txt)" >&2
  exit 1
fi

echo "== traceability and generated invariants =="
"$PY" tools/gen_invariants.py --check
"$PY" -m pytest tests/test_traceability.py -q

echo "== unit and integration =="
"$PY" -m pytest -q -m "not slow"

echo "== all tests (includes slow/crash) =="
"$PY" -m pytest -q
