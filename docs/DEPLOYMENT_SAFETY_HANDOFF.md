# Deployment safety handoff — reviewer round 3

Reviewed baseline: `a31cd16640731e417462e8b55875f18bf396b670`
(`pass-d-safety-round2`). Current remote state recorded separately:
`origin/main` was `f058bb34f9e73f1c292ab3104f56b414ad20eb99`;
`origin/pass-d-safety-round2` was the reviewed `a31cd16`. No newer main was
substituted.

Tested implementation revision: `0bbd74f`.
Branch: `pass-d-safety-round3`. No merge, PR or deployment is authorized.

## Priority 1 — host-only installation blockers

### Chosen model: fresh-install/create-only

The installer now creates a host installation only when every target artifact
is absent. It refuses conflicting destinations before any write. It does not
overwrite code, docs, units, config, presets or secrets; it does not normalize
existing directory metadata. Explicitly existing shared state directories
(`/var/lib/mediaflow` and its evidence tree) are preserved untouched.

Consequences and limitations:

- Re-running the installer over an existing installation is a **conflict**, not
  an upgrade. This is intentional and avoids a general recovery journal.
- Rollback removes only files this run created and only when their SHA-256
  still matches the manifest. It removes newly created empty directories, then
  preserves all originals, evidence and shared roots.
- A failure before the manifest write cannot occur: the manifest is written
  before installation writes. A trap finalizes hashes on partial failure.
- This does not provide upgrade-in-place behavior; a separately approved
  upgrade procedure remains out of scope.

### Findings and fixes

| Finding | Status | Reference / test |
|---|---|---|
| Revision printed but not enforced | Fixed | `install.sh` requires `MEDIAFLOW_INSTALL_EXPECTED_SHA`, rejects mismatch, and requires source/script provenance. `test_installer_enforces_expected_sha`. |
| Dirty override available in production | Fixed | Dirty check is production-only; test seams are explicit (`ALLOW_DIRTY`, `FORCE_DIRTY_CHECK`, `ALLOW_REPO_OVERRIDE`). `test_installer_refuses_dirty_checkout_before_writes`. |
| `inactive` matched as active | Fixed | Exact passive state parser; `inactive disabled` passes, active/enabled/unknown/missing/transitioning states refuse. `test_installer_accepts_only_exact_passive_service_states`. |
| Query failures treated as absence | Fixed | Failed queries become `query-error` and refuse. Same test. |
| Only main service inspected | Fixed | `mediaflow.service`, `mediaflow-trash.timer`, `mediaflow-doctor.timer` all inspected. |
| Service override available in production | Fixed | `MEDIAFLOW_INSTALL_SERVICE_STATE` is test-only. |
| Masked unit overwritten | Fixed | Symlink to `/dev/null` is preserved and skipped. `test_installer_preserves_masked_unit_symlink`. |
| Conflicting destination overwritten | Fixed | Create-only conflict refusal before writes. `test_installer_create_only_refuses_conflicting_destination`. |
| Evidence/parent metadata changed | Fixed | Existing state/evidence and metadata preserved. `test_installer_preserves_evidence_and_parent_metadata`. |
| Manifest written after writes / incomplete | Fixed | Manifest written before writes, finalizer adds hashes on success or partial failure. `test_installer_manifest_and_rollback_cover_created_files`. |
| Empty-directory rollback called complete | Fixed | `tools/install_rollback.py` removes only hash-matched created files and identified empty new dirs; never originals or evidence. |

Priority 1 remains a **proposal until John/advisor approval**. No host
installation command is approved by this document alone.

## Priority 2 — verification workflow correctness

| Finding | Status | Reference / test |
|---|---|---|
| Doctor aggregate acceptance wrong | Fixed | `apply_backend_acceptance()` emits a dedicated `backend-acceptance` result. Pass/fail/unknown/missing/disabled matrix tested. `test_aggregate_either_acceptance_matrix`. |
| Doctor state reads used immutable DB and hid errors | Fixed | `_read_state_settings()` uses validated WAL-aware read store; corrupt DB raises. `test_doctor_state_reads_are_schema_truthful_and_wal_aware`. |
| Read-only open errors escaped | Fixed | `Store.__init__` wraps read-only integrity failures in `DatabaseError`. `test_read_only_open_error_is_database_error`. |
| Observational library report wrote cache | Fixed | CLI disables cache writes for read-only store. `test_library_report_does_not_write_cache_for_observational_run`. |
| Rehearsal hid doctor crashes | Fixed | Doctor exit is captured; 0/2/3/4 are valid preflight outcomes, others fail. `test_rehearsal_does_not_hide_doctor_crash`. |
| Rehearsal import provenance unverified | Fixed | Rehearsal asserts imported `mediaflow` path belongs to the checkout. |
| Sentinel existence check insufficient | Fixed | Content before/after compared. `test_rehearsal_checks_sentinel_contents`. |
| Cleanup identity weak | Fixed | Marker, prefix, parent, symlink and device checks. `test_scratch_cleanup_rejects_substitution_and_symlink`. |
| Missing-state planning had no supported path | Fixed | `tools/scratch_db.py` initializes an isolated DB and layout; `test_scratch_db_initialization_enables_first_plan`. |
| Media init command refused as documented | Fixed | Single `MEDIAFLOW_MEDIA_INIT_APPROVED=1` gate; explicit mode, ancestor symlink preflight. `test_media_init_documented_command_and_symlink_ancestors`, `test_media_init_sets_expected_mode_on_new_paths`. |
| Acceptance not bound to bundle bytes | Fixed | `accept()` stores canonical bundle digest; registry mutation recomputes and rejects changes. `test_acceptance_is_bound_to_bundle_contents`. |
| Redaction not idempotent | Fixed | `/srv/media/[REDACTED]` is stable. `test_redaction_is_idempotent_for_media_paths`. |
| Recapture left stale acceptance | Fixed | Capture removes existing acceptance. `test_recapture_invalidates_previous_acceptance`. |

Default doctor is non-encoding but **not subprocess-free**: it runs a bounded,
supervised `ffmpeg -version`. Hardware capability checks are separate and
explicit. Sandbox software-encoder fixtures remain test-only and do not
demonstrate hardware encode or VMAF behavior.

## Priority 3 — documentation

- This document, `DEPLOYMENT.md`, `FIRST_RUN.md`, `QUESTIONS.md`,
  `requirements.toml`, invariants and generated views were audited against the
  final diff.
- All `HW-xxx` references in `FIRST_RUN.md` and `QUESTIONS.md` resolve in the
  registry; a test now locks this in.
- WAL-aware read-only reads are documented as potentially creating `-wal`/`-shm`
  auxiliary files; they are not described as filesystem-write-free.
- Capture/validate/accept examples use one bundle directory consistently.
- Test accounting: round 2 was `863 + 19 new - 1 removed parameter case = 881`.
  Round 3 adds 19 focused tests and one registry-reference test:
  `881 + 19 + 1 = 901`.

## Supported commands and approval boundaries

Record revision (read):

```sh
git -C /srv/agent-work/mediaflow rev-parse HEAD
```

Host-only install (write; requires explicit approval and `EXPECTED_SHA`):

```sh
sudo MEDIAFLOW_INSTALL_HOST_ONLY=1 MEDIAFLOW_INSTALL_EXPECTED_SHA=<approved-full-sha> /srv/agent-work/mediaflow/install.sh
```

Expected exit: `0` on fresh create-only install, `3` on conflict/preflight
refusal. The daemon and timers remain stopped/disabled.

Verify service state (read):

```sh
sudo systemctl is-active mediaflow.service
```

Verify enablement (read):

```sh
sudo systemctl is-enabled mediaflow.service
```

Verify media metadata (read):

```sh
stat -c '%U:%G %a %n' /srv/media/movies /srv/media/music
```

Builder rehearsal (writes only to its own scratch root):

```sh
bash /srv/agent-work/mediaflow/scripts/rehearse_install.sh
```

The script owns scratch creation and cleanup. If `KEEP_SCRATCH=1` is used,
retain exactly the printed root and validate it with `tools/scratch.py` before
later cleanup.

Non-encoding diagnostics:

```sh
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --json
```

Hardware capability probes require separate approval:

```sh
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --capabilities --backends qsv,vaapi --accept-backends either --json
```

Scratch DB initialization (isolated config only):

```sh
python3 tools/scratch_db.py /tmp/path/to/scratch-config.toml
```

Evidence validation is read-only:

```sh
python3 tools/evidence.py validate "$EVIDENCE/HW-001"
```

Registry mutation requires John's separate acceptance and an expected revision:

```sh
python3 tools/evidence.py update-registry "$EVIDENCE/HW-001" --expected-revision <full-revision>
```

The agent never performs the last two on-box stages or registry mutation.

## Actual test results

Commands run at implementation revision `0bbd74f`:

```sh
.venv/bin/python tools/gen_invariants.py
.venv/bin/python tools/gen_hardware_checks.py
.venv/bin/python -m pytest -q tests/test_deployment_safety_round3.py
scripts/check.sh
```

Results are in `docs/VERIFIED.md`: focused 19 passed; full suite 901 passed;
no skips. `/dev/dri/renderD128` remained inaccessible.

## Unresolved gaps

- All 44 hardware registry items remain unverified.
- Privileged ownership, ACLs, systemd effects and real service states cannot be
  faithfully exercised in this sandbox; injected tests only cover seams.
- Host installation is unapproved. No production-media initialization,
  Jellyfin change, real hardware probe or registry verification occurred.

## Stop point

Stop before any production install, media initialization, access change,
service action, Jellyfin change or hardware verification. A green suite proves
sandbox behavior only.
