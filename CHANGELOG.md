# Changelog

All notable changes to mediaflow. This project is pre-release until John
completes the on-hardware checklist in `docs/RUNBOOK.md`.

## [Unreleased]

### Added

- One shared supervision boundary for encode, verify, calibration trials,
  capability probes and ffmpeg-change retests, with typed outcomes,
  real-progress liveness, bounded startup/finalization allowances and one
  exclusive expensive-operation slot.
- Bounded, cancellable verification scans that aggregate packet and frame
  records incrementally and use the maximum packet `pts + duration` as the
  presentation tail.
- Regressions for shutdown/cancellation precedence, controlled recovery,
  non-cooperative worker ownership, capability unknowns, protection deferral,
  net remaining-work space accounting and the canonical backend/speed key.

### Changed

- Mid-encode free-space checks reserve net remaining work plus an additive
  emergency low-water allowance; published outputs reserve the copy size.
- Calibration and capability probes hold the processing slot from their first
  probe through cleanup and report incomplete results as unknown, never as a
  false `False`.
- Existing encoder, verifier, probe and guard call shapes remain compatible;
  new supervision controls are passed only when accepted.

### Fixed

- Cancellation or shutdown now wins over a late successful result, and a
  daemon shutdown is no longer relabelled as a user cancellation.
- A controlled protection/shutdown encode interruption recovers at `PLANNED`
  without consuming a restart; stalls and timeouts still do.
- Verification interruptions, deferrals and shutdowns can no longer advance a
  job to `VERIFIED`.
- Remuxes use an explicit copy estimate instead of borrowing an encode
  backend's speed-table row.

### Deployment advisor round 3

- Host installation is now create-only: exact SHA/provenance enforcement, exact passive service/timer parsing, conflict refusal, evidence preservation and hash-bound rollback manifest.
- Doctor backend acceptance is aggregate with explicit unknown/missing semantics; observational DB and cache paths are truthful and WAL-aware.
- Rehearsal provenance, doctor crash handling, sentinel comparison and identity-bound cleanup were tightened.
- Scratch DB initialization, media-init modes/ancestors, and content-bound evidence acceptance were completed.

### Deployment advisor round 2

- Installer preflight refuses dirty checkouts, active/enabled services, symlink destinations and unexpected media ownership before writes; artifact manifest and scoped rollback added.
- Doctor acceptance policy is explicit and separate from probing; report.ok now matches exit-code 4.
- Read-only state validation distinguishes missing/corrupt/older/newer databases; observational commands fail truthfully instead of faking success.
- Rehearsal, media initialization, cleanup identity checks and evidence acceptance are isolated and end-to-end tested.

### Deployment safety corrections

- Added host-only install mode that leaves media roots and service state untouched.
- Runtime startup/scan no longer creates or normalizes library directories.
- Observational CLI commands use immutable read-only state and never create a DB.
- Default doctor is non-encoding; hardware capability probes are explicit, hardware-only and never use software/x265.
- Corrected registry references and documented doctor exit semantics in a durable handoff.

### Pass D (deployment readiness)

- Added the authoritative hardware-check registry with generated markdown and
  checklist views; the agent cannot mark on-box items verified.
- Doctor is now a read-only preflight with pass/fail/unknown outcomes,
  class-aware exit codes and no capability writes or network calls.
- Install.sh gained sandbox rehearsal seams; install, repeat, preservation and
  refusal paths are tested against a fake root.
- Added synthetic clip generation, redacted evidence bundles and a validating
  registry updater for John's first-run drills.

### Pass C follow-up

- Publish stages the final folder in `work/` and moves the verified output
  into it; the only published transition is one no-replace rename. Output hard
  links and byte-copy fallbacks were removed.
- Publish disk accounting keeps the source-size envelope and adds only staged
  sidecar bytes.
- SIZE-2 fallback builds a real remux plan from the source codec and geometry
  and drops encode-only fields.
- Post-encode speed updates are marked atomically so a crash replay cannot
  double-count one encode; remaining-work estimates are bounded and documented.
- Planner-created remuxes ignore encode speed factors.
- `temp_sensor` is restart-required; `poll_seconds`, `review_reminder_days` and
  `notify_cooldown_hours` are live and rebind at a reload boundary.

### Added

- One shared job-state vocabulary (`mediaflow.state`) used by database queries,
  engine transitions, CLI/status, notifications and retention. RESTORED is
  terminal history and no longer blocks a re-drop.
- Authoritative configuration reload classification with atomic generations,
  pending restart holds and plan generation/preset binding.
- Enforced database schema versioning with backed-up, ordered, idempotent
  migrations and explicit semantic versions for plans, speed rows and
  capability files.

### Changed

- Publish space checks keep the SAF-8 source-size envelope and add staged
  sidecar bytes instead of replacing the source reservation.
- Legacy plans are deliberately re-planned; legacy speed rows are retained but
  ignored until re-seeded; unversioned capability files are unknown.
- `status --json` reports config generation, pending generation and restart
  holds; `doctor` reports schema version and last migration backup.

### Fixed

- A restored job could reappear as an active job and silently block a new drop.
- Invalid-config holds were not cleared by a later valid reload.
- Mixed configuration generations could leave paths, guards, secrets and the
  encoder on different values.
- An old database could be silently restamped as current and a newer database
  downgraded by an older binary.

## [1.0.0] - 2026-11-18

### Added

- Preset-driven conversion funnel for `/srv/media`: stabilise, probe, plan,
  encode or remux, verify, name, atomically publish, and retire the original
  to trash.
- Shipped presets: `keep-remux-mp4`, `keep-remux-mkv`, `1080p-balanced-mp4`,
  `1080p-small-mp4`, `720p-small-mp4`, plus a disabled experimental x265
  preset.
- Crash-safe SQLite state machine with recovery, orphan work cleanup, trash
  purge, review actions and library duplicate handling.
- Safety layer with one-filesystem checks, atomic rename, no symlink
  following, verified-output-before-retire, protocol/format allowlists and
  secret redaction.
- Machine guards: AC state, thermal pause/resume, Jellyfin transcode
  detection, free-space watchdogs, schedule windows, SIGSTOP/SIGCONT encoder
  pause and stall detection.
- CLI with short phone-friendly output, `doctor`, `calibrate`, `estimate`,
  `status`, queue/review/library commands, and JSON output.
- Native systemd units with hardening and timers for trash purge and optional
  weekly doctor runs.
- Idempotent `install.sh` for john (never run by the build agent).
- Requirement registry, generated `docs/INVARIANTS.md`, traceability
  meta-test, plan schema, runbook, on-hardware questions and verification
  evidence.
- Unit, integration, crash and chaos test suites using sandbox roots only;
  `scripts/check.sh` refuses to run against production `/srv/media`.

### Fixed

- Crash windows around review parking, unparking, empty trash-directory
  adoption and duplicate replacement now persist intent before the rename and
  reconcile it on startup.
- Planless jobs can reach FAILED without a `working_output` path.
- Publish fails closed when `renameat2(RENAME_NOREPLACE)` is unavailable;
  an existing library item is never overwritten by the fallback.
- SAF-2 is no longer configurable: the full decode is mandatory and covers
  all audio as well as video.
- Encoder failures that return non-zero now obey `max_restarts` instead of
  failing immediately.
- A job waiting at a start gate is no longer mistaken for an interrupted
  encode on daemon restart; only a real encoder start consumes a restart.
- Maintenance deletions outside the media pipeline are contained and logged.

### Known limitations

- HEVC 10-bit hardware encode on Kaby Lake is not assumed; it is tested by
  `doctor` and routed through REVIEW unless available.
- QSV/VAAPI, thermal, AC, power-loss and Jellyfin direct-play behavior remain
  on-hardware VERIFY items in `docs/QUESTIONS.md`.
- TV, downloads, AV1, tone mapping, subtitle OCR/burn-in, Atmos preservation,
  deinterlacing, a web UI, Docker and segmented encoding are out of scope for
  v1.
