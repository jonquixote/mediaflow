#!/usr/bin/env bash
# Validate an evidence bundle and mark its registry item verified.
# Run by John only, after a real on-box check. The agent never runs this for on-box items.
set -euo pipefail
BUNDLE="${1:?bundle dir required}"
exec python3 "$(dirname "${BASH_SOURCE[0]}")/../tools/evidence.py" update-registry "$BUNDLE"
