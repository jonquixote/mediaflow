#!/usr/bin/env bash
# Capture a redacted evidence bundle for one hardware registry item.
# Usage: capture_evidence.sh BUNDLE_DIR HW-xxx -- command [args...]
set -euo pipefail
BUNDLE="${1:?bundle dir required}"
ITEM="${2:?registry item id required}"
shift 2
if [[ "${1:-}" != "--" ]]; then
  echo "expected -- before the command" >&2
  exit 2
fi
shift
exec python3 "$(dirname "${BASH_SOURCE[0]}")/../tools/evidence.py" capture \
  --bundle "$BUNDLE" --item "$ITEM" -- "$@"
