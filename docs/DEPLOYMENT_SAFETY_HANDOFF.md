# Deployment safety handoff — reviewer round 2

Reviewed baseline: `947c8f9f19fc090371d9fcf9a508cc342113aa41`
(branch `pass-d-safety-fixes` at review time). Current remote state recorded
separately: `origin/main` was `f058bb34f9e73f1c292ab3104f56b414ad20eb99`;
`origin/pass-d-safety-fixes` was the reviewed `947c8f9`. There was no hidden
main advancement to substitute.

Tested implementation revision: `fad57ef`.
Current branch: `pass-d-safety-round2`. No merge or deployment is authorized.

## 1. Disposition of the advisor findings

| Area | Class | Disposition |
|---|---|---|
| 1. Installer preflight / service / artifacts | Confirmed defects | `install.sh` now refuses a dirty tracked checkout, active/enabled service state, symlink destinations and unexpected media ownership **before any writes**. It prints full revision and absolute checkout, writes `$STATE_DIR/install-manifest.json`, and host-only mode still creates no media paths and changes no service state. `tools/install_rollback.py` removes only positively identified empty new directories and never files/shared roots. Tests: `test_installer_refuses_dirty_checkout_before_writes`, `test_installer_refuses_existing_active_or_enabled_service`, `test_installer_refuses_symlink_destination_and_preserves_target`, `test_installer_writes_artifact_manifest_and_rollback_is_scoped`. |
| 2. Doctor acceptance policy | Confirmed defects | `apply_backend_acceptance()` implements `none`, `qsv-required`, `vaapi-required`, `either`; CLI flag `--accept-backends` is separate from `--capabilities/--backends`. `DoctorReport.ok` is false when exit is 4. `_classify_checks` now uses enabled backends and the explicit policy. Default doctor is **non-encoding but not subprocess-free**: it runs a bounded, supervised `ffmpeg -version`; this is documented in check `subprocess-audit` and below. Tests: `test_ok_is_false_when_exit_code_is_four`, `test_acceptance_policies_and_disabled_backend`, `test_default_doctor_documents_its_subprocesses`. |
| 3. Observational DB / CLI truthfulness | Confirmed defects | `Store.check_schema()` validates compatibility without migration. Missing, corrupt, older and newer databases are distinguished. CLI `DatabaseError` exits 3. Read-only stores use WAL-aware `mode=ro` (not `immutable`), so live reads see uncheckpointed commits; SQLite may create `-shm` auxiliary files, documented here. `plan`/`estimate`/`library report` now fail with exit 3 when no state exists instead of claiming success. Status fallback says `unknown`, not running. `plan --refresh` is a mutation and is marked as such in the matrix. Tests: `test_read_only_store_rejects_newer_schema`, `test_read_only_store_sees_active_wal_writer`, `test_plan_and_estimate_fail_truthfully_without_state`, `test_status_fallback_does_not_claim_running`, `test_corrupt_state_is_not_treated_as_absence`. |
| 4. Fully isolated rehearsal | Confirmed gap | `scripts/rehearse_install.sh` clears inherited `MEDIAFLOW_*` and `PYTHONPATH`, writes scratch config with scratch paths, uses scratch docs/secrets/presets/state/runtime, checks a sentinel outside the scratch root, and cleans through identity-checked `tools/scratch.py`. Test mode skips venv/pip, so this rehearses **file layout only**, not dependency installation. Test: `test_rehearsal_isolates_hostile_inherited_configuration`. |
| 5. Initialization vs installation | Confirmed gap | `Engine.start()` now refuses missing required media paths with an actionable error and does not create them. `tools/media_init.py` creates only approved pipeline paths, preserves existing ownership/modes, never touches music, and requires explicit approval for production root. Scratch initialization and production access validation remain separate. Tests: `test_engine_start_refuses_missing_media_paths`, `test_media_init_creates_only_approved_paths_and_preserves_modes`. |
| 6. Evidence helper | Confirmed defects | CLI dispatch conflict fixed (`action` vs `cmd`), `sys.argv[1:]` used, outputs resolved relative to bundle with traversal/symlink rejection, redacted `token=***` accepted, full 40-hex revision required, and registry mutation requires explicit `accept` plus `--expected-revision`. Bundle integrity, claim attestation, John's acceptance and registry mutation are separate. Tests: end-to-end CLI capture/validate from another cwd, wrapper/failure, tampering/traversal, revision/acceptance. |
| 7. Documentation / command matrix | Documentation defects | This document, DEPLOYMENT.md, FIRST_RUN.md, QUESTIONS.md, generated registry views, requirements and invariants are reconciled. `ffmpeg -version` is described accurately. No blanket “always safe” claim remains. |
| Still unverified | On-hardware questions | All 44 registry items remain `unverified`. Sandbox software-encoder fixtures/tests remain test-only and are never hardware evidence. |

## 2. Complete command matrix

| Command | Reads | Writes / destinations | External calls / encoding | Privileges | Safe phase |
|---|---|---|---|---|---|
| `mediaflow doctor` | config, presets, secret metadata, immutable state, capability file, systemd text, filesystem stats | none; no capability file | bounded supervised `ffmpeg -version`; no encode | service user | host-only preflight |
| `mediaflow doctor --capabilities --backends qsv,vaapi --accept-backends either` | same as default | scratch temp probe output; processing slot | supervised qsv/vaapi encode probes; no software/x265 | service user with render access | explicitly approved capability phase |
| `mediaflow status` / `queue` / `review list` | status/progress files if present, WAL-aware read-only DB | none | none | config read | any phase; liveness unknown without status file |
| `mediaflow plan` / `estimate` | source, read-only DB (required), speed table | none | supervised ffprobe metadata read | source read | dry-run phase after DB exists |
| `mediaflow library report` | movies tree, read-only DB (required) | none | supervised ffprobe metadata reads | library read | approved access-validation phase |
| `mediaflow plan --refresh` | source and DB | mutates persisted plan | supervised probe | daemon/CLI single-writer | mutation phase, not observational |
| `mediaflow run` / `maintenance` | config, state, media paths | DB, job snapshots, status/progress, work/trash/backup, library | ffmpeg/ffprobe/TMDb/notify as configured | service user | only after approved media initialization |
| `bash scripts/rehearse_install.sh` | repo checkout | new scratch root under `/tmp` only | supervised doctor subprocess; no sudo/services | builder user | sandbox rehearsal |
| `python tools/evidence.py validate BUNDLE` | bundle | none | none | John | integrity check only |
| `python tools/evidence.py accept BUNDLE ...` | validated bundle | writes `acceptance.json` in bundle | none | John | explicit claim review |
| `python tools/evidence.py update-registry BUNDLE --expected-revision ...` | bundle + acceptance | writes `docs/hardware_checks.toml` | none | maintainer | after John's acceptance only |

## 3. Supported host-only install (each block needs John's approval)

Record revision (read):

```sh
git -C /srv/agent-work/mediaflow rev-parse HEAD
```

Host-file install (write; no media roots, no service/timer state changes):

```sh
sudo MEDIAFLOW_INSTALL_HOST_ONLY=1 ./install.sh
```

Expected exit: `0`. Refusal exit: `3`. Verify service remains inactive (read):

```sh
sudo systemctl is-active mediaflow.service
```

Verify not enabled (read):

```sh
sudo systemctl is-enabled mediaflow.service
```

Verify media ownership unchanged (read):

```sh
stat -c '%U:%G %a %n' /srv/media/movies /srv/media/music
```

Rollback: remove only host files this command created under `/opt/mediaflow`,
`/etc/mediaflow`, `/var/lib/mediaflow` and `/usr/share/doc/mediaflow`; never
delete existing config/secrets/state/evidence or anything under `/srv/media`.
Use `python tools/install_rollback.py MANIFEST` to list safe empty new
directories; `--apply` removes only those.

**Stop before media initialization, ACL/unit changes, service enablement,
real media, Jellyfin changes or hardware verification.**

## 4. Isolated rehearsal (write to scratch only)

Create scratch (write):

```sh
SCRATCH=$(mktemp -d /tmp/mediaflow-rehearsal.XXXXXX)
```

Run rehearsal (write in scratch; layout-only):

```sh
bash scripts/rehearse_install.sh
```

Expected exit: `0`; output contains `layout-only`; hostile inherited paths are
cleared; outside sentinel is unchanged. Cleanup validates identity and
containment before removing the scratch root:

```sh
python3 tools/scratch.py cleanup "$SCRATCH"
```

The rehearsal skips venv/pip execution; it does not prove dependency install.

## 5. Non-encoding vs hardware capability sequences

Default preflight (non-encoding, supervised `ffmpeg -version`):

```sh
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --json
```

Explicit hardware capability check (requires separate approval):

```sh
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --capabilities --backends qsv,vaapi --accept-backends either --json
```

Probing two backends does not require both. `none` makes backend results
informational; `qsv-required`/`vaapi-required` make only that backend decisive;
`either` fails only when no backend works. Exit 0 never implies full
hardware-registry acceptance.

## 6. Initialization and access model

Run media initialization only with explicit approval:

```sh
sudo MEDIAFLOW_MEDIA_INIT_APPROVED=1 python3 /opt/mediaflow/tools/media_init.py --root /srv/media --apply
```

It creates only approved `_pipeline` directories, preserves existing modes,
leaves music untouched, and never falls back to the full installer.
The least-privilege access proposal remains: preserve `john:agentwork` on
movies/music, no broad agentwork membership, no ACL/unit changes without John's
verification and approval. These are proposals, not verified server facts.

## 7. Evidence helper lifecycle

Capture (write to bundle):

```sh
bash scripts/capture_evidence.sh "$EVIDENCE" HW-001 -- <approved command>
```

Validate integrity only (no writes):

```sh
python3 tools/evidence.py validate "$EVIDENCE/HW-001"
```

John's explicit attestation (writes acceptance.json):

```sh
bash scripts/accept_hardware_item.sh "$EVIDENCE/HW-001" John "reviewed on-box output" <full-revision>
```

Registry mutation (separate, requires acceptance and full revision):

```sh
python3 tools/evidence.py update-registry "$EVIDENCE/HW-001" --expected-revision <full-revision>
```

The agent never performs the acceptance or update steps. A bundle proves
integrity only, not cryptographic authenticity or a hardware claim.

## 8. Actual sandbox results

Commands run at `fad57ef` implementation tree:

```sh
.venv/bin/python tools/gen_invariants.py
.venv/bin/python tools/gen_hardware_checks.py
.venv/bin/python -m pytest -q tests/test_deployment_safety_round2.py
scripts/check.sh
```

Results are recorded in `docs/VERIFIED.md`; no skips were reported. The
reviewed baseline suite was `863 passed`; the added end-to-end regressions
increase the count. `/dev/dri/renderD128` was inaccessible and no hardware
check ran.

## 9. Unresolved questions

- Effective systemd protections, ACLs, symlinks and service-user access are
  still unverified on the server.
- All 44 hardware registry items remain unverified.
- John/advisor must approve the staged access and media initialization plan
  before any production-media step.

## 10. Stop point

Stop before production-media initialization, ownership/mode changes, ACL or
unit changes, service/timer enablement, real media processing, Jellyfin
changes or hardware verification. A green sandbox suite is not production or
hardware readiness.
