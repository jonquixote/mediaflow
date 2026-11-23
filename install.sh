#!/usr/bin/env bash
# mediaflow installer. Run as root, after reviewing this file. Idempotent.
# It never touches Jellyfin configuration, SSH, ufw, Tailscale, or the vault.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Production paths are fixed by the systemd units and config example. The
# MEDIAFLOW_INSTALL_* overrides exist only for sandbox rehearsals and never
# change production behaviour when unset.
PREFIX="${MEDIAFLOW_INSTALL_PREFIX:-/opt/mediaflow}"
CONFIG_DIR="${MEDIAFLOW_INSTALL_CONFIG_DIR:-/etc/mediaflow}"
ROOT="${MEDIAFLOW_INSTALL_ROOT:-/srv/media}"
STATE_DIR="${MEDIAFLOW_INSTALL_STATE_DIR:-/var/lib/mediaflow}"
SYSTEMD_DIR="${MEDIAFLOW_INSTALL_SYSTEMD_DIR:-/etc/systemd/system}"
DOC_DIR="${MEDIAFLOW_INSTALL_DOC_DIR:-/usr/share/doc/mediaflow}"
TEST_MODE="${MEDIAFLOW_INSTALL_TEST:-0}"
VENV="$PREFIX/venv"

if [[ "$ROOT" == "/srv/media" || "$ROOT" == /srv/media/* ]] && [[ "$TEST_MODE" == "1" ]]; then
  echo "rehearsal mode refuses the production media root" >&2
  exit 2
fi
if [[ ${EUID} -ne 0 && "$TEST_MODE" != "1" ]]; then
  echo "install.sh must run as root (john). The coding agent must never run it." >&2
  exit 1
fi

run() {
  printf '+ '; printf '%q ' "$@"; printf '\n'
  "$@"
}

# Privileged operations are echoed only in rehearsal mode. File operations use
# install_dir/install_file so ownership is skipped when testing as a normal user.
run_priv() {
  if [[ "$TEST_MODE" == "1" ]]; then
    printf '+ [rehearsal] '; printf '%q ' "$@"; printf '\n'
  else
    run "$@"
  fi
}

install_dir() {
  local mode="$1" owner="$2" group="$3" path="$4"
  if [[ "$TEST_MODE" == "1" ]]; then
    run install -d -m "$mode" "$path"
  else
    run install -d -o "$owner" -g "$group" -m "$mode" "$path"
  fi
}

install_file() {
  local mode="$1" owner="$2" group="$3" src="$4" dest="$5"
  if [[ "$TEST_MODE" == "1" ]]; then
    run install -m "$mode" "$src" "$dest"
  else
    run install -o "$owner" -g "$group" -m "$mode" "$src" "$dest"
  fi
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
if ! getent group mediaflow >/dev/null; then run_priv groupadd --system mediaflow; fi
if ! getent group media >/dev/null; then run_priv groupadd --system media; fi
if ! id -u mediaflow >/dev/null 2>&1; then
  run_priv useradd --system --gid mediaflow --home-dir "$PREFIX" --no-create-home \
      --shell /usr/sbin/nologin mediaflow
fi
for grp in media render video; do
  if getent group "$grp" >/dev/null; then
    run_priv usermod -aG "$grp" mediaflow
  fi
done

# --- directories ------------------------------------------------------------
install_dir 0750 root mediaflow "$CONFIG_DIR"
install_dir 0750 root mediaflow "$CONFIG_DIR/presets.d"
install_dir 0750 root mediaflow "$CONFIG_DIR/experimental"
install_dir 2775 mediaflow media "$STATE_DIR"
install_dir 2775 mediaflow media "$STATE_DIR/backup"
install_dir 2775 mediaflow media "$STATE_DIR/logs"
install_dir 2775 mediaflow media "$ROOT"
install_dir 2775 mediaflow media "$ROOT/movies"
install_dir 2775 mediaflow media "$ROOT/_pipeline"
install_dir 2775 mediaflow media "$ROOT/_pipeline/inbox"
install_dir 2775 mediaflow media "$ROOT/_pipeline/convert"
install_dir 2775 mediaflow media "$ROOT/_pipeline/convert/movies"
install_dir 2775 mediaflow media "$ROOT/_pipeline/work"
install_dir 2775 mediaflow media "$ROOT/_pipeline/review"
install_dir 2775 mediaflow media "$ROOT/_pipeline/failed"
install_dir 2775 mediaflow media "$ROOT/_pipeline/trash"
# Deliberately do NOT create the experimental preset folder.

# --- code and venv ----------------------------------------------------------
install_dir 0755 root root "$PREFIX"
for item in pyproject.toml requirements.lock README.md src tools docs; do
  run cp -R "$REPO_DIR/$item" "$PREFIX/"
done
install_dir 0755 root root "$DOC_DIR"
run cp -R "$REPO_DIR/docs/." "$DOC_DIR/"
if [[ ! -x "$VENV/bin/python" ]]; then
  run_priv python3 -m venv "$VENV"
fi
run_priv "$VENV/bin/pip" install --require-hashes --only-binary=:all: -r "$PREFIX/requirements.lock"
run_priv "$VENV/bin/pip" install --no-deps "$PREFIX"

# --- configuration (never overwrite existing) -------------------------------
if [[ ! -e "$CONFIG_DIR/config.toml" ]]; then
  install_file 0644 root root "$REPO_DIR/config/config.toml.example" "$CONFIG_DIR/config.toml"
else
  echo "keeping existing $CONFIG_DIR/config.toml"
fi
for f in tmdb.env notify.env; do
  if [[ ! -e "$CONFIG_DIR/$f" ]]; then
    install_file 0640 root mediaflow "$REPO_DIR/config/$f.example" "$CONFIG_DIR/$f"
  else
    run_priv chown root:mediaflow "$CONFIG_DIR/$f"
    run_priv chmod 0640 "$CONFIG_DIR/$f"
  fi
done

# Copy presets without clobbering local edits. Presets are data.
for preset in "$REPO_DIR"/presets/*.toml; do
  name="$(basename "$preset")"
  if [[ ! -e "$CONFIG_DIR/presets.d/$name" ]]; then
    install_file 0644 root root "$preset" "$CONFIG_DIR/presets.d/$name"
  else
    echo "keeping existing preset $CONFIG_DIR/presets.d/$name"
  fi
done
if [[ -f "$REPO_DIR/presets/experimental/1080p-best-x265-mp4.toml" && \
      ! -e "$CONFIG_DIR/experimental/1080p-best-x265-mp4.toml" ]]; then
  install_file 0644 root root \
    "$REPO_DIR/presets/experimental/1080p-best-x265-mp4.toml" \
    "$CONFIG_DIR/experimental/1080p-best-x265-mp4.toml"
fi

# Create one convert folder per enabled shipped preset.
for preset_dir in keep-remux-mp4 keep-remux-mkv 1080p-balanced-mp4 1080p-small-mp4 720p-small-mp4; do
  install_dir 2775 mediaflow media "$ROOT/_pipeline/convert/movies/$preset_dir"
done

# --- systemd ----------------------------------------------------------------
install_dir 0755 root root "$SYSTEMD_DIR"
install_file 0644 root root "$REPO_DIR/systemd/mediaflow.service" "$SYSTEMD_DIR/mediaflow.service"
install_file 0644 root root "$REPO_DIR/systemd/mediaflow-trash.service" "$SYSTEMD_DIR/mediaflow-trash.service"
install_file 0644 root root "$REPO_DIR/systemd/mediaflow-trash.timer" "$SYSTEMD_DIR/mediaflow-trash.timer"
install_file 0644 root root "$REPO_DIR/systemd/mediaflow-doctor.service" "$SYSTEMD_DIR/mediaflow-doctor.service"
install_file 0644 root root "$REPO_DIR/systemd/mediaflow-doctor.timer" "$SYSTEMD_DIR/mediaflow-doctor.timer"
run_priv systemctl daemon-reload
run_priv systemctl enable mediaflow.service

cat <<EOF

Installed. Next steps (see /usr/share/doc/mediaflow/RUNBOOK.md):
  1. systemctl start mediaflow.service
  2. sudo -u mediaflow -g media $VENV/bin/mediaflow --config $CONFIG_DIR/config.toml doctor
  3. Put test folders in $ROOT/_pipeline/convert/movies/<preset>/ and inspect:
       $VENV/bin/mediaflow --config $CONFIG_DIR/config.toml plan <path>
  4. Leave dry_run=true until doctor is green and docs/QUESTIONS.md is clear.
  5. Enable mediaflow-trash.timer only after reviewing retention.
EOF
