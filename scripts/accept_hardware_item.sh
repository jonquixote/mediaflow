#!/usr/bin/env bash
# Explicit John attestation for one validated on-box evidence bundle.
set -euo pipefail
BUNDLE="${1:?bundle dir required}"
ACCEPTED_BY="${2:?accepted-by required}"
NOTES="${3:?notes required}"
REVISION="${4:?expected full revision required}"
exec python3 "$(dirname "${BASH_SOURCE[0]}")/../tools/evidence.py" accept "$BUNDLE" \
  --accepted-by "$ACCEPTED_BY" --notes "$NOTES" --expected-revision "$REVISION"
