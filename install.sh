#!/usr/bin/env bash
# mediaflow installer: create-only host installation.
# Run as root after reviewing this file. It never touches Jellyfin config,
# SSH, ufw, Tailscale, the vault, real media paths, or existing artifacts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="${MEDIAFLOW_INSTALL_REPO_DIR:-$SCRIPT_DIR}"
REPO_DIR="$(cd "$REPO_DIR" && pwd -P)"
PREFIX="${MEDIAFLOW_INSTALL_PREFIX:-/opt/mediaflow}"
CONFIG_DIR="${MEDIAFLOW_INSTALL_CONFIG_DIR:-/etc/mediaflow}"
ROOT="${MEDIAFLOW_INSTALL_ROOT:-/srv/media}"
STATE_DIR="${MEDIAFLOW_INSTALL_STATE_DIR:-/var/lib/mediaflow}"
SYSTEMD_DIR="${MEDIAFLOW_INSTALL_SYSTEMD_DIR:-/etc/systemd/system}"
DOC_DIR="${MEDIAFLOW_INSTALL_DOC_DIR:-/usr/share/doc/mediaflow}"
TEST_MODE="${MEDIAFLOW_INSTALL_TEST:-0}"
HOST_ONLY="${MEDIAFLOW_INSTALL_HOST_ONLY:-0}"
EXPECTED_SHA="${MEDIAFLOW_INSTALL_EXPECTED_SHA:-}"
VENV="$PREFIX/venv"
MANIFEST_PATH="$STATE_DIR/install-manifest.json"
CREATED_FILES=()
PRESERVED_PATHS=()

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
run_priv() {
  if [[ "$TEST_MODE" == "1" ]]; then
    printf '+ [rehearsal] '; printf '%q ' "$@"; printf '\n'
  else
    run "$@"
  fi
}
install_dir() {
  local mode="$1" owner="$2" group="$3" path="$4"
  if [[ -e "$path" ]]; then return 0; fi
  if [[ "$TEST_MODE" == "1" ]]; then
    run install -d -m "$mode" "$path"
  else
    run install -d -o "$owner" -g "$group" -m "$mode" "$path"
  fi
  CREATED_FILES+=("DIR:$path")
}
install_file() {
  local mode="$1" owner="$2" group="$3" src="$4" dest="$5"
  if [[ "$TEST_MODE" == "1" ]]; then
    run install -m "$mode" "$src" "$dest"
  else
    run install -o "$owner" -g "$group" -m "$mode" "$src" "$dest"
  fi
  CREATED_FILES+=("FILE:$dest")
}

finalize_manifest() {
  local rc=$?
  [[ -f "$MANIFEST_PATH" ]] || return $rc
  MF_MANIFEST_PATH="$MANIFEST_PATH" python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
manifest = Path(os.environ["MF_MANIFEST_PATH"])
data = json.loads(manifest.read_text())
known = {item["path"] for item in data.get("created_files", [])}
for raw in os.environ.get("MF_CREATED_FINAL", "").splitlines():
    if not raw or ":" not in raw:
        continue
    kind, path = raw.split(":", 1)
    if path not in known:
        data["created_files"].append({"kind": kind, "path": path, "sha256": None})
        known.add(path)
for item in data.get("created_files", []):
    path = Path(item["path"])
    if path.is_file():
        item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        item["sha256"] = None
manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  return $rc
}
trap finalize_manifest EXIT

# --- fail-closed preflight --------------------------------------------------
if [[ "$TEST_MODE" == "1" ]]; then
  [[ "$REPO_DIR" == "$SCRIPT_DIR" || "${MEDIAFLOW_INSTALL_ALLOW_REPO_OVERRIDE:-0}" == "1" ]] || {
    echo "preflight: test-mode repo override not permitted" >&2; exit 3; }
else
  [[ "$REPO_DIR" == "$SCRIPT_DIR" ]] || {
    echo "preflight: source checkout differs from executing script" >&2; exit 3; }
  [[ -n "$EXPECTED_SHA" ]] || {
    echo "preflight: MEDIAFLOW_INSTALL_EXPECTED_SHA is required" >&2; exit 3; }
fi

REVISION="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
[[ ${#REVISION} -eq 40 ]] || {
  echo "preflight: cannot determine a full checkout revision" >&2; exit 3; }
if [[ -n "$EXPECTED_SHA" ]]; then
  [[ "$REVISION" == "$EXPECTED_SHA" ]] || {
    echo "preflight: expected revision $EXPECTED_SHA does not match $REVISION" >&2; exit 3; }
fi
DIRTY="$(git -C "$REPO_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)"
if [[ -n "$DIRTY" && ( "$TEST_MODE" != "1" || "${MEDIAFLOW_INSTALL_FORCE_DIRTY_CHECK:-0}" == "1" ) ]]; then
  echo "preflight: tracked checkout is dirty; refuse before any writes" >&2
  printf '%s\n' "$DIRTY" >&2
  exit 3
fi

UNIT_NAMES=(mediaflow.service mediaflow-trash.timer mediaflow-doctor.timer)
if [[ -n "${MEDIAFLOW_INSTALL_SERVICE_STATE:-}" ]]; then
  [[ "$TEST_MODE" == "1" ]] || {
    echo "preflight: service-state override is test-only" >&2; exit 3; }
  STATE_STRING="$MEDIAFLOW_INSTALL_SERVICE_STATE"
else
  STATE_STRING=""
  for unit in "${UNIT_NAMES[@]}"; do
    active="$(systemctl is-active "$unit" 2>/dev/null || printf 'query-error')"
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || printf 'query-error')"
    STATE_STRING+="$unit=$active/$enabled;"
  done
fi
if [[ "$TEST_MODE" == "1" ]]; then
  # Single override string applies to every affected unit.
  case "$STATE_STRING" in
    "inactive disabled"|"inactive masked") ;;
    *)
      echo "preflight: non-passive service state ($STATE_STRING); refuse before writes" >&2
      echo "a separately approved stop/disable/unmask transition is required first" >&2
      exit 3
      ;;
  esac
else
  case "$STATE_STRING" in
    *query-error*|*=inactive/disabled\;*inactive/disabled\;*inactive/disabled\;)
      [[ "$STATE_STRING" != *query-error* ]] || {
        echo "preflight: service query failed; refuse before writes" >&2; exit 3; }
      ;;
    *)
      echo "preflight: non-passive service/timer state ($STATE_STRING); refuse before writes" >&2
      exit 3
      ;;
  esac
fi

for path in "$CONFIG_DIR/config.toml" "$CONFIG_DIR/tmdb.env" "$CONFIG_DIR/notify.env" \
            "$SYSTEMD_DIR/mediaflow.service" "$SYSTEMD_DIR/mediaflow-trash.timer" \
            "$SYSTEMD_DIR/mediaflow-doctor.timer" "$MANIFEST_PATH"; do
  if [[ -L "$path" && "$path" == "$SYSTEMD_DIR/"* && "$(readlink "$path")" == "/dev/null" ]]; then
    continue  # masked unit: preserve and never write it
  fi
  if [[ -L "$path" || -e "$path" ]]; then
    echo "preflight: conflicting destination: $path" >&2
    exit 3
  fi
done
[[ ! -e "$PREFIX" ]] || { echo "preflight: conflicting destination: $PREFIX" >&2; exit 3; }
[[ ! -e "$DOC_DIR" ]] || { echo "preflight: conflicting destination: $DOC_DIR" >&2; exit 3; }
[[ ! -e "$CONFIG_DIR" ]] || { echo "preflight: conflicting destination: $CONFIG_DIR" >&2; exit 3; }

# Preserve explicitly allowed shared state and evidence, including metadata.
if [[ -e "$STATE_DIR" ]]; then
  PRESERVED_PATHS+=("$STATE_DIR")
  [[ ! -e "$MANIFEST_PATH" ]] || { echo "preflight: existing manifest; refuse overwrite" >&2; exit 3; }
fi
if [[ -e "$STATE_DIR/evidence" ]]; then
  PRESERVED_PATHS+=("$STATE_DIR/evidence")
fi

echo "mediaflow installer (create-only)"
echo "  repo:   $REPO_DIR"
echo "  rev:    $REVISION"
echo "  prefix: $PREFIX"
echo "  config: $CONFIG_DIR"
echo "  media:  $ROOT"

# Write the manifest before any installation writes.
install_dir 2775 mediaflow media "$STATE_DIR"
MF_MANIFEST_PATH="$MANIFEST_PATH" MF_REPO="$REPO_DIR" MF_REVISION="$REVISION" \
MF_HOST_ONLY="$HOST_ONLY" MF_TEST_MODE="$TEST_MODE" \
MF_CREATED="$(printf '%s\n' "${CREATED_FILES[@]}")" \
MF_PRESERVED="$(printf '%s\n' "${PRESERVED_PATHS[@]}")" \
python3 - <<'PY'
import json, os, stat
from pathlib import Path
preserved = []
for raw in os.environ["MF_PRESERVED"].splitlines():
    if not raw:
        continue
    path = Path(raw)
    if not path.exists():
        continue
    info = path.stat()
    preserved.append({
        "path": str(path), "uid": info.st_uid, "gid": info.st_gid,
        "mode": oct(stat.S_IMODE(info.st_mode)),
    })
data = {
    "schema": 2,
    "revision": os.environ["MF_REVISION"],
    "repo": os.environ["MF_REPO"],
    "host_only": os.environ["MF_HOST_ONLY"] == "1",
    "test_mode": os.environ["MF_TEST_MODE"] == "1",
    "model": "create-only",
    "created_files": [
        {"kind": item.split(":", 1)[0], "path": item.split(":", 1)[1], "sha256": None}
        for item in os.environ["MF_CREATED"].splitlines() if item
    ],
    "replaced": [],
    "preserved": preserved,
    "principals": {"mediaflow_group": "created-or-existing",
                   "media_group": "created-or-existing",
                   "mediaflow_user": "created-or-existing"},
}
Path(os.environ["MF_MANIFEST_PATH"]).write_text(
    json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

# --- users and groups -------------------------------------------------------
if ! getent group mediaflow >/dev/null; then run_priv groupadd --system mediaflow; fi
if ! getent group media >/dev/null; then run_priv groupadd --system media; fi
if ! id -u mediaflow >/dev/null 2>&1; then
  run_priv useradd --system --gid mediaflow --home-dir "$PREFIX" --no-create-home \
      --shell /usr/sbin/nologin mediaflow
fi
for grp in media render video; do
  if getent group "$grp" >/dev/null; then run_priv usermod -aG "$grp" mediaflow; fi
done

# --- directories and code ---------------------------------------------------
install_dir 0750 root mediaflow "$CONFIG_DIR"
install_dir 0750 root mediaflow "$CONFIG_DIR/presets.d"
install_dir 0750 root mediaflow "$CONFIG_DIR/experimental"
install_dir 2775 mediaflow media "$STATE_DIR/backup"
install_dir 2775 mediaflow media "$STATE_DIR/logs"
install_dir 0755 root root "$PREFIX"
for item in pyproject.toml requirements.lock README.md src tools docs; do
  run cp -R "$REPO_DIR/$item" "$PREFIX/"
  CREATED_FILES+=("FILE:$PREFIX/$item")
done
install_dir 0755 root root "$DOC_DIR"
run cp -R "$REPO_DIR/docs/." "$DOC_DIR/"
if [[ ! -x "$VENV/bin/python" ]] && [[ "$TEST_MODE" != "1" ]]; then
  run_priv python3 -m venv "$VENV"
fi
if [[ "$TEST_MODE" != "1" ]]; then
  run_priv "$VENV/bin/pip" install --require-hashes --only-binary=:all: -r "$PREFIX/requirements.lock"
  run_priv "$VENV/bin/pip" install --no-deps "$PREFIX"
fi

# --- configuration ----------------------------------------------------------
install_file 0644 root root "$REPO_DIR/config/config.toml.example" "$CONFIG_DIR/config.toml"
for f in tmdb.env notify.env; do
  [[ -f "$REPO_DIR/config/$f.example" ]] && \
    install_file 0640 root mediaflow "$REPO_DIR/config/$f.example" "$CONFIG_DIR/$f"
done
for preset in "$REPO_DIR"/presets/*.toml; do
  name="$(basename "$preset")"
  install_file 0644 root root "$preset" "$CONFIG_DIR/presets.d/$name"
done
install_file 0644 root root \
  "$REPO_DIR/presets/experimental/1080p-best-x265-mp4.toml" \
  "$CONFIG_DIR/experimental/1080p-best-x265-mp4.toml"

if [[ "$HOST_ONLY" != "1" ]]; then
  install_dir 2775 mediaflow media "$ROOT"
  install_dir 2775 mediaflow media "$ROOT/movies"
  for preset_dir in keep-remux-mp4 keep-remux-mkv 1080p-balanced-mp4 1080p-small-mp4 720p-small-mp4; do
    install_dir 2775 mediaflow media "$ROOT/_pipeline/convert/movies/$preset_dir"
  done
fi

# --- systemd units ----------------------------------------------------------
install_dir 0755 root root "$SYSTEMD_DIR"
for unit in mediaflow.service mediaflow-trash.service mediaflow-trash.timer \
            mediaflow-doctor.service mediaflow-doctor.timer; do
  target="$SYSTEMD_DIR/$unit"
  if [[ -L "$target" && "$(readlink "$target")" == "/dev/null" ]]; then
    echo "preserving masked unit $target"
    continue
  fi
  install_file 0644 root root "$REPO_DIR/systemd/$unit" "$target"
done

if [[ "$HOST_ONLY" == "1" ]]; then
  echo "host-only: service left stopped and disabled; timers untouched"
else
  run_priv systemctl daemon-reload
  run_priv systemctl enable mediaflow.service
fi

export MF_CREATED_FINAL="$(printf '%s\n' "${CREATED_FILES[@]}")"
if [[ "${MEDIAFLOW_INSTALL_FAIL_AFTER:-}" == "config" && "$TEST_MODE" == "1" ]]; then
  echo "injected failure after config" >&2
  exit 9
fi

echo "create-only host installation complete"
