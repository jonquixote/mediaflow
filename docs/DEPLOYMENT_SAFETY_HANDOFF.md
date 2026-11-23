# Deployment safety handoff

Reviewed baseline: `f058bb34f9e73f1c292ab3104f56b414ad20eb99` on
`main`/`origin/main`. The reviewed base and the current remote default were
identical when this branch was created; no integration drift existed.

Implementation branch: `pass-d-safety-fixes`.
Tested implementation revision: `4d8a12d`.
Scope: repository investigation, implementation and sandbox tests only. No
command in this document was executed against john-server or any production
root. This handoff is not deployment authorization.

## 1. Disposition of the advisor findings

| # | Class | Disposition and references |
|---|---|---|
| 1 | Confirmed safety-contract conflict | `install.sh` created/chowned media paths and enabled the service. Added `MEDIAFLOW_INSTALL_HOST_ONLY=1`: media dirs and convert folders are skipped, systemctl enable/daemon-reload is skipped, service stays stopped/disabled. Test: `tests/test_deployment_safety.py::test_host_only_install_does_not_touch_media_or_service_state`. |
| 2 | Confirmed implementation bug | `Engine._ensure_dirs()` previously mkdir+chmod every media path on start/scan. It now ensures only state/log paths by default; media creation requires explicit `manage_media_dirs=True` for scratch installs. `Config.ensure_directories(normalize_existing=False)` preserves existing modes. Tests: `test_engine_start_and_scan_do_not_initialize_media_paths`, `test_config_ensure_directories_can_preserve_existing_library_metadata`. |
| 3 | Confirmed diagnostic-policy conflict | `doctor` defaulted to capability probes including software/libx265; daemon automatic ffmpeg-change retest called it without backend selection. `run_doctor()` now defaults `run_capabilities=False`; CLI default is non-encoding, `--capabilities --backends qsv,vaapi` is required for probes, software is rejected, and automatic retests pass hardware-only backends with `allow_software=False`. Skipped/deferred probes remain unknown. Tests: `test_capability_probes_default_to_hardware_only`, `test_cli_doctor_default_is_non_encoding_and_rejects_software`, `test_automatic_ffmpeg_retest_never_enables_software_probing`. |
| 4 | Confirmed observational side effect | `status`, `queue`, `plan`, `estimate`, `library report` and `review list` used `_open_store()` which called `Store.initialize()`, creating/migrating state. Added `Store(read_only=True)` using immutable SQLite, `_open_store(..., observation=True)`, and missing-DB paths print an empty result without creating state. Tests: `test_observational_commands_do_not_create_state` and existing CLI tests. |
| 5 | Safety-contract constraint / unverified on server | No access changes were made. The proposed least-privilege model below preserves `john:agentwork` on movies and leaves music untouched. This is a proposal only until John verifies ACLs, symlinks and service-user access. |
| 6 | Documentation defects | Corrected `FIRST_RUN.md` staged publish to `HW-042` and restore/re-drop to `HW-041`; corrected QUESTIONS `poll_seconds` (live) to a restart-required example `temp_sensor`; documented doctor exit codes 0/2/3/4. Registry remains authoritative. |
| 7 | Confirmed rehearsal-isolation concern | `MEDIAFLOW_INSTALL_*` seams existed but media paths were still created and systemctl enable ran in the ordinary path. Host-only mode now isolates host-file installation; rehearsal uses fake roots under `/tmp`, skips privileged subprocesses and refuses `/srv/media`. Tests cover fresh/repeat, config/state/secrets preservation, refusal on production root. |
| 8 | Confirmed evidence-helper ambiguity | `scripts/verify_hardware_item.sh` calls `update-registry`, a write. `tools/evidence.py validate` is read-only and separate. Evidence capture redacts token assignments, bearer values, credential URLs, likely `Title (Year)` strings and `/srv/media` paths; validator rejects unredacted output. Tests: `test_evidence_validator_rejects_incomplete_and_tampered_bundles`, `test_redaction_removes_tokens_urls_and_titles`. |
| Still unverified | On-hardware questions | Backend behavior, thermal/AC, systemd protections, direct play, real migration and access checks remain unverified. Sandbox tests never set a hardware registry item to verified. |

## 2. CLI side-effect matrix

| Command | Reads | Writes / destinations | External calls | Encoding/probing | Privilege | Safe phase |
|---|---|---|---|---|---|---|
| `doctor` (default) | config, presets, secrets file metadata, state DB immutable, capability file, systemd unit text, filesystem stats | none; no capability file writes | none; no network | none | service user | host-only preflight |
| `doctor --capabilities --backends qsv,vaapi` | same as default | temporary ffmpeg probe output in system temp; processing slot; no capability file write by doctor | supervised ffmpeg qsv/vaapi probes | hardware-only encode probes; never software/x265 | service user with render access | explicitly approved capability phase |
| `status` / `queue` | status.json, progress.json if present, immutable state DB | none | none | none | any user with config read | any phase |
| `plan` / `estimate` | source path, immutable state DB when present, speed table | none | ffprobe supervised read of source | no encoding | any user with source read | dry-run phase |
| `library report` | movies tree, immutable state DB, ffprobe on library files | none | supervised ffprobe metadata reads | no encoding | service user/library read | dry-run or approved access-validation phase |
| `review list` | immutable state DB | none | none | none | any user with config read | any phase |
| `run` / `maintenance` / mutation commands | config, state, source/output paths | state DB, job snapshots, status/progress, work/trash/backup, library on publish | ffmpeg/ffprobe/TMDb/notify as configured | production encode/remux when jobs advance | service user, single writer | only after approved media initialization |
| `calibrate` | source, config, state | work scratch, speed seed in DB, calibration report | supervised ffmpeg/ffprobe | explicit encode trials | service user | only after approved capability phase |
| `capture_evidence` | command output | new bundle under chosen bundle dir | runs given command | depends on command | John | any approved drill |
| `verify_hardware_item` | evidence bundle | writes `docs/hardware_checks.toml` | none | none | repository maintainer | only after valid on-box evidence |

## 3. Supported host-only install sequence

Every block contains exactly one command. Each command is a write unless
stated otherwise and needs John's approval.

Record the reviewed revision (read):

```sh
git -C /srv/agent-work/mediaflow rev-parse HEAD
```

Install host files only; effects: `/opt/mediaflow`, `/etc/mediaflow`,
`/var/lib/mediaflow`, docs and systemd unit files; no `/srv/media` paths,
no systemctl enable/start/timer changes:

```sh
sudo MEDIAFLOW_INSTALL_HOST_ONLY=1 ./install.sh
```

Expected exit: `0`. Verify with one read-only command per check:

```sh
sudo systemctl is-enabled mediaflow.service
```

Expected: `disabled` or `masked`; never `enabled` after this phase.

```sh
sudo systemctl is-active mediaflow.service
```

Expected: `inactive`.

```sh
stat -c '%U:%G %a %n' /srv/media/movies /srv/media/music
```

Expected: unchanged `john:agentwork 2775` for both; agent must compare with
John's survey, not assume.

Rollback: remove only host files created by that install under
`/opt/mediaflow`, `/etc/mediaflow`, `/var/lib/mediaflow` and
`/usr/share/doc/mediaflow`; never delete existing config, secrets, state or
anything under `/srv/media`.

**Stop point:** do not create pipeline directories, chmod/chown media paths,
enable services/timers, or run real jobs without John's separate explicit
approval.

## 4. Builder-tested isolated rehearsal

This sequence creates files in a scratch root only. Running it on john-server
still needs approval because it is a write.

Create scratch root (write; cleanup confined to this directory):

```sh
SCRATCH=$(mktemp -d /tmp/mediaflow-rehearsal.XXXXXX)
```

Run rehearsal with fake roots and privileged subprocesses skipped:

```sh
MEDIAFLOW_INSTALL_TEST=1 MEDIAFLOW_INSTALL_HOST_ONLY=1 MEDIAFLOW_INSTALL_PREFIX="$SCRATCH/opt/mediaflow" MEDIAFLOW_INSTALL_CONFIG_DIR="$SCRATCH/etc/mediaflow" MEDIAFLOW_INSTALL_ROOT="$SCRATCH/media" MEDIAFLOW_INSTALL_STATE_DIR="$SCRATCH/var/lib/mediaflow" MEDIAFLOW_INSTALL_SYSTEMD_DIR="$SCRATCH/systemd" MEDIAFLOW_INSTALL_DOC_DIR="$SCRATCH/doc/mediaflow" bash ./install.sh
```

Expected: exit `0`; no `$SCRATCH/media`; config/state/systemd/doc files exist;
output says host-only and service disabled. Verify:

```sh
test ! -e "$SCRATCH/media"
```

Run non-encoding diagnostics against a scratch config (write to scratch only):

```sh
MEDIAFLOW_CONFIG="$SCRATCH/etc/mediaflow/config.toml" .venv/bin/mediaflow doctor --json
```

Clean up only the newly created scratch root:

```sh
rm -rf "$SCRATCH"
```

No sudo, service action, `/srv/media` write or network access occurs in this
sequence. The rehearsal copies repository docs/tools/src into the scratch
prefix and writes fake config, secrets, presets, state and unit files there.

## 5. Non-encoding and hardware-only sequences

Non-encoding diagnostics are always safe first:

```sh
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --json
```

This does not encode, probe backends, write capabilities or contact TMDb.
Only after separate approval and verified render access:

```sh
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --capabilities --backends qsv,vaapi --json
```

Software/x265 is not available through this CLI. A deferred/incomplete result
is unknown; never record it as accepted capability evidence.

## 6. Proposed least-privilege access model (not yet verified)

Facts from John's survey, not test passes:

- `/srv/media/movies` and `/srv/media/music` are `john:agentwork 2775`.
- Jellyfin runs as `jellyfin:jellyfin` with supplementary `video` and `render`.
- `/dev/dri/renderD128` is `root:render 0660`.

Proposals requiring separate approval and on-box verification:

- Keep `john:agentwork` ownership and mode `2775` on movies; do not chmod or
  chown it during installation or startup.
- Leave music untouched; no mediaflow supplementary group grant for music.
- If service-user write access to movies is required for publish/restore,
  prefer a narrowly scoped ACL on the specific pipeline/library roots after
  John reviews inherited permissions; do not rely solely on a supplementary
  group.
- Verify effective access with John's own read-only checks before changing
  any unit, config, ACL or ownership. No production unit/config change was
  made by the agent.

## 7. Evidence helper and registry semantics

- `tools/evidence.py capture` runs a command and writes a redacted bundle.
- `tools/evidence.py validate BUNDLE` is read-only and checks completeness,
  exit status, revision and unredacted content.
- `tools/evidence.py update-registry BUNDLE` is a separate write to
  `docs/hardware_checks.toml`; it refuses incomplete or inconsistent bundles.
- `scripts/verify_hardware_item.sh` calls `update-registry` and must not be
  described as validation-only.
- Redaction is best-effort pattern removal, not a proof of absence. Manual
  review of the bundle by John is still required before registry mutation.
- The agent never marks an on-hardware item verified.

## 8. Doctor exit and feature semantics

- `0`: no required or enabled-feature failures. Unknowns may remain.
- `2`: required failure; blocks progression.
- `3`: failure in a feature-conditional check.
- `4`: a required check is unknown; investigation needed before acceptance.
- Informational items never change the exit code.
- Default doctor has no backend/encode checks, so feature-conditional failures
  can only arise when capability checks are explicitly requested. This matches
  the documented semantics.

## 9. Registry ids and documentation authority

`docs/hardware_checks.toml` is authoritative for on-box status; generated
views are `docs/HARDWARE_CHECKS.md` and `docs/FIRST_RUN_CHECKLIST.md`.
Known corrected references: staged publish is `HW-042`; restore/re-drop is
`HW-041`; `poll_seconds` is live, `temp_sensor` is restart-required.
Requirements and generated invariants were regenerated with the tools.

## 10. Actual sandbox results and stop point

Commands actually run from the repository:

```sh
.venv/bin/python tools/gen_invariants.py
.venv/bin/python tools/gen_hardware_checks.py
.venv/bin/python -m pytest -q tests/test_deployment_safety.py
scripts/check.sh
```

Results at implementation revision `4d8a12d`:

- traceability: `6 passed in 0.24s`
- unit/integration: `863 passed in 46.90s`
- full suite: `863 passed in 46.08s`
- focused deployment-safety tests: `11 passed`

No skips were reported. `/dev/dri/renderD128` was inaccessible; no hardware
encode, systemd action, production config/state access, real-media operation
or Jellyfin change occurred.

**Stop before production-media initialization, ownership/mode changes, service
enablement, timers, real media, Jellyfin configuration or hardware verification.
All require John's separate explicit approval.**
