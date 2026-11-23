#!/usr/bin/env bash
# Builder rehearsal: fake-root, layout-only install. No sudo/services/media writes.
set -euo pipefail
ROOT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRATCH="$(mktemp -d "${SCRATCH_PARENT:-/tmp}/mediaflow-rehearsal.XXXXXX")"
export SCRATCH

cat > "$SCRATCH/config.toml" <<EOF
root = "$SCRATCH/media"
state_dir = "$SCRATCH/var/lib/mediaflow"
runtime_dir = "$SCRATCH/run"
ffmpeg = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
ffprobe = "/usr/lib/jellyfin-ffmpeg/ffprobe"
dry_run = true
pause_on_battery = false
temp_sensor = "none"
min_free_gb = 1
EOF

env -u MEDIAFLOW_ROOT -u MEDIAFLOW_CONFIG -u MEDIAFLOW_ENV_DIR \
    -u MEDIAFLOW_PRESETS -u MEDIAFLOW_DOCS -u PYTHONPATH \
    MEDIAFLOW_INSTALL_TEST=1 MEDIAFLOW_INSTALL_HOST_ONLY=1 MEDIAFLOW_INSTALL_ALLOW_DIRTY=1 \
    MEDIAFLOW_INSTALL_REPO_DIR="$ROOT_REPO" \
    MEDIAFLOW_INSTALL_PREFIX="$SCRATCH/opt/mediaflow" \
    MEDIAFLOW_INSTALL_CONFIG_DIR="$SCRATCH/etc/mediaflow" \
    MEDIAFLOW_INSTALL_ROOT="$SCRATCH/media" \
    MEDIAFLOW_INSTALL_STATE_DIR="$SCRATCH/var/lib/mediaflow" \
    MEDIAFLOW_INSTALL_SYSTEMD_DIR="$SCRATCH/systemd" \
    MEDIAFLOW_INSTALL_DOC_DIR="$SCRATCH/doc/mediaflow" \
    bash "$ROOT_REPO/install.sh"

cp "$SCRATCH/config.toml" "$SCRATCH/etc/mediaflow/config.toml"

env -u MEDIAFLOW_ROOT -u PYTHONPATH \
    MEDIAFLOW_CONFIG="$SCRATCH/etc/mediaflow/config.toml" \
    MEDIAFLOW_ENV_DIR="$SCRATCH/etc/mediaflow" \
    MEDIAFLOW_PRESETS="$SCRATCH/etc/mediaflow/presets.d" \
    MEDIAFLOW_DOCS="$SCRATCH/doc/mediaflow" \
    "$ROOT_REPO/.venv/bin/python" -m mediaflow doctor --json > "$SCRATCH/doctor.json" || true

if [[ -n "${SENTINEL:-}" && ! -e "$SENTINEL" ]]; then
  echo "rehearsal sentinel was modified" >&2
  exit 4
fi

echo "layout-only rehearsal complete: $SCRATCH"
if [[ "${KEEP_SCRATCH:-0}" != "1" ]]; then
  python3 "$ROOT_REPO/tools/scratch.py" cleanup "$SCRATCH"
fi
