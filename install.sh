#!/usr/bin/env bash
# mediaflow installer. Run as root, after reviewing this file. Idempotent.
# It never touches Jellyfin configuration, SSH, ufw, Tailscale, or the vault.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# These paths are also baked into the systemd units and config example.
# Keep them fixed so the installer cannot produce a half-relocated setup.
PREFIX="/opt/mediaflow"
CONFIG_DIR="/etc/mediaflow"
ROOT="/srv/media"
STATE_DIR="/var/lib/mediaflow"
VENV="$PREFIX/venv"

if [[ ${EUID} -ne 0 ]]; then
  echo "install.sh must run as root (john). The coding agent must never run it." >&2
  exit 1
fi

run() {
  printf '+ '; printf '%q ' "$@"; printf '\n'
  "$@"
}

echo "mediaflow installer"
echo "  repo:   $REPO_DIR"
echo "  prefix: $PREFIX"
echo "  config: $CONFIG_DIR"
echo "  media:  $ROOT"
echo
echo "This installer does not modify Jellyfin, SSH, ufw, Tailscale, or the vault."
echo "Fresh installs start in dry_run=true. Do not disable dry-run until doctor"
echo "is green and docs/QUESTIONS.md has no unchecked items."
echo

# --- users and groups -------------------------------------------------------
if ! getent group mediaflow >/dev/null; then run groupadd --system mediaflow; fi
if ! getent group media >/dev/null; then run groupadd --system media; fi
if ! id -u mediaflow >/dev/null 2>&1; then
  run useradd --system --gid mediaflow --home-dir "$PREFIX" --no-create-home \
      --shell /usr/sbin/nologin mediaflow
fi
for grp in media render video; do
  if getent group "$grp" >/dev/null; then
    run usermod -aG "$grp" mediaflow
  fi
done

# --- directories ------------------------------------------------------------
run install -d -o root -g mediaflow -m 0750 "$CONFIG_DIR"
run install -d -o root -g mediaflow -m 0750 "$CONFIG_DIR/presets.d"
run install -d -o root -g mediaflow -m 0750 "$CONFIG_DIR/experimental"
run install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow
run install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow/backup
run install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow/logs
run install -d -o mediaflow -g media -m 2775 "$ROOT"
run install -d -o mediaflow -g media -m 2775 "$ROOT/movies"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/inbox"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/convert"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/convert/movies"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/work"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/review"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/failed"
run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/trash"
# Deliberately do NOT create the experimental preset folder.

# --- code and venv ----------------------------------------------------------
run install -d -o root -g root -m 0755 "$PREFIX"
for item in pyproject.toml requirements.lock README.md src tools docs; do
  run cp -R "$REPO_DIR/$item" "$PREFIX/"
done
run install -d -o root -g root -m 0755 /usr/share/doc/mediaflow
run cp -R "$REPO_DIR/docs/." /usr/share/doc/mediaflow/
if [[ ! -x "$VENV/bin/python" ]]; then
  run python3 -m venv "$VENV"
fi
run "$VENV/bin/pip" install --require-hashes --only-binary=:all: -r "$PREFIX/requirements.lock"
run "$VENV/bin/pip" install --no-deps "$PREFIX"

# --- configuration (never overwrite existing) -------------------------------
if [[ ! -e "$CONFIG_DIR/config.toml" ]]; then
  run install -o root -g root -m 0644 "$REPO_DIR/config/config.toml.example" "$CONFIG_DIR/config.toml"
else
  echo "keeping existing $CONFIG_DIR/config.toml"
fi
for f in tmdb.env notify.env; do
  if [[ ! -e "$CONFIG_DIR/$f" ]]; then
    run install -o root -g mediaflow -m 0640 "$REPO_DIR/config/$f.example" "$CONFIG_DIR/$f"
  else
    run chown root:mediaflow "$CONFIG_DIR/$f"
    run chmod 0640 "$CONFIG_DIR/$f"
  fi
done

# Copy presets without clobbering local edits. Presets are data.
for preset in "$REPO_DIR"/presets/*.toml; do
  name="$(basename "$preset")"
  if [[ ! -e "$CONFIG_DIR/presets.d/$name" ]]; then
    run install -o root -g root -m 0644 "$preset" "$CONFIG_DIR/presets.d/$name"
  else
    echo "keeping existing preset $CONFIG_DIR/presets.d/$name"
  fi
done
if [[ -f "$REPO_DIR/presets/experimental/1080p-best-x265-mp4.toml" && \
      ! -e "$CONFIG_DIR/experimental/1080p-best-x265-mp4.toml" ]]; then
  run install -o root -g root -m 0644 \
    "$REPO_DIR/presets/experimental/1080p-best-x265-mp4.toml" \
    "$CONFIG_DIR/experimental/1080p-best-x265-mp4.toml"
fi

# Create one convert folder per enabled shipped preset.
for preset_dir in keep-remux-mp4 keep-remux-mkv 1080p-balanced-mp4 1080p-small-mp4 720p-small-mp4; do
  run install -d -o mediaflow -g media -m 2775 "$ROOT/_pipeline/convert/movies/$preset_dir"
done

# --- systemd ----------------------------------------------------------------
run install -o root -g root -m 0644 "$REPO_DIR/systemd/mediaflow.service" /etc/systemd/system/mediaflow.service
run install -o root -g root -m 0644 "$REPO_DIR/systemd/mediaflow-trash.service" /etc/systemd/system/mediaflow-trash.service
run install -o root -g root -m 0644 "$REPO_DIR/systemd/mediaflow-trash.timer" /etc/systemd/system/mediaflow-trash.timer
run install -o root -g root -m 0644 "$REPO_DIR/systemd/mediaflow-doctor.service" /etc/systemd/system/mediaflow-doctor.service
run install -o root -g root -m 0644 "$REPO_DIR/systemd/mediaflow-doctor.timer" /etc/systemd/system/mediaflow-doctor.timer
run systemctl daemon-reload
run systemctl enable mediaflow.service

cat <<EOF

Installed. Next steps (see /usr/share/doc/mediaflow/RUNBOOK.md):
  1. systemctl start mediaflow.service
  2. sudo -u mediaflow -g media $VENV/bin/mediaflow --config $CONFIG_DIR/config.toml doctor
  3. Put test folders in $ROOT/_pipeline/convert/movies/<preset>/ and inspect:
       $VENV/bin/mediaflow --config $CONFIG_DIR/config.toml plan <path>
  4. Leave dry_run=true until doctor is green and docs/QUESTIONS.md is clear.
  5. Enable mediaflow-trash.timer only after reviewing retention.
EOF
