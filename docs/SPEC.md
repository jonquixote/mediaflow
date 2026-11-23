# mediaflow - Media Optimisation and Organisation Funnel

**Spec v1.0** for john-server (Ubuntu Server 26.04.1 LTS, MacBookPro14,1, Intel i5-7360U, Iris Plus 640).
Default output container: **MP4**. Owner: john. Builder: the coding agent (`agent` user), reviewed by john.

The machine-readable requirement registry is `docs/requirements.toml`; it is
authoritative for requirement IDs, wording and safety flags. `docs/INVARIANTS.md`
is generated from its safety-flagged entries and is never edited by hand.
`docs/PLAN_SCHEMA.md` is authoritative for persisted plan fields and their
meaning. `docs/hardware_checks.toml` is authoritative for unverified hardware
and deployment items and their status; its markdown views are generated.
`docs/DEPLOYMENT.md` is authoritative for install/upgrade/rollback procedure,
`docs/FIRST_RUN.md` for the ordered on-box drills, `docs/RUNBOOK.md` for daily
operation, and `docs/VERIFIED.md` records only commands that were actually run.
This SPEC and its detailed tables are explanatory; if they disagree with the
registry, schema or code, those authorities win.

## 0. Reading this document

MUST/MUST NOT are hard; SHOULD is the default unless a reason is documented.
`safe` requirements outrank features. **VERIFY** items are assumptions about
the real machine and are settled on the box or listed in `docs/QUESTIONS.md`.
In a conflict, section 7 and the state machine win.

## 1. Goal and non-goals

Drop a movie into a folder named after a preset. mediaflow stabilises it,
probes and plans it, encodes or remuxes it, verifies it, names it
`Title (Year)`, atomically publishes it under `/srv/media/movies/`, and retires
the original to trash. Jellyfin direct-plays the result on phones.

v1 excludes TV, downloading, AV1, HDR-to-SDR tone mapping, subtitle OCR/burn-in,
Dolby Atmos preservation, deinterlacing, a web UI, Docker, and segmented
encoding. The encoder interface is reserved for phase 2.

## 2. Environment

- 2 cores / 4 threads: software x265 is only for the disabled experimental preset.
- Kaby Lake QSV hardware HEVC is the default. HEVC 10-bit hardware encode is
  disputed and tested separately, never assumed. The engaged rate-control mode
  is recorded.
- Use Jellyfin's ffmpeg path, configurable; generic ffmpeg is insufficient.
- Closed-lid laptop: AC-only start gates and a thermal guard are mandatory.
- Power can fail: every step is crash-safe and resumable. Interrupted encodes
  restart from the beginning in v1.
- The builder has no sudo; `install.sh` is for john.
- Pipeline and library share one filesystem so all moves are single renames.
- Phone is the primary console: every command has a short output mode.

## 3. Layout and state

The root defaults to `/srv/media`; tests override it. `movies/` is the Jellyfin
library. `_pipeline/` contains `inbox/`, `convert/movies/<preset>/`, `work/`,
`review/`, `failed/`, and `trash/<date>/<job-id>/`. Config is under
`/etc/mediaflow/`; state, logs and backups under `/var/lib/mediaflow/`; runtime
locks under `/run/mediaflow/`. Directories are setgid `2775` and the service
uses `UMask=0002`. A human moving an entry into a preset folder is the trigger.

### 3.1 One state model and persisted generations

`mediaflow.state` is the only definition of job states, review reasons, intent
kinds, command statuses and legal transitions. Active, terminal, resumable,
review and user-visible classifications, and the SQL placeholders used by the
database, are generated from that model. `RESTORED` is terminal for scanning
and queueing; its manifest, trash copies and history remain visible to status,
listings and retention (STATE-1).

`state.db` carries an enforced schema version. Older databases are backed up
and migrated by ordered, idempotent, transactional steps; newer databases are
refused before modification. Plans, speed rows and capability files carry
explicit semantic generations rather than relying on unchanged JSON shapes
(COMPAT-1).

SQLite `state.db` is authoritative. WAL, `synchronous=FULL` and
`busy_timeout=5000`; integrity check at startup; nightly backup keeping three
generations. The fsync chain is: write and fsync file, fsync directory, commit
DB, atomically write/fsync/rename `job.json`, fsync directory. Jobs have ULIDs
and are keyed by preset, relative source path and first-seen time. Mutating CLI
commands go through the daemon's command table, or the CLI takes the daemon
lock when the daemon is down. Secrets live only in env files.

## 4. Workflow

```
DETECTED -> STABLE -> PROBED -> PLANNED -> ENCODING -> ENCODED -> VERIFIED
         -> NAMED -> PUBLISHED -> RETIRED -> DONE
   ENCODED -> PLANNED (SIZE-2 fallback)
   PUBLISHED -> RETIRED -> DONE -> RESTORED
   any active state -> REVIEW | FAILED
```

A `RESTORED` job is terminal history: the same preset/source path may be
dropped again and creates a fresh job. Active and REVIEW jobs still block a
fresh drop.

The poller scans every 30 seconds. STABLE waits for unchanged size and mtime
and no temp markers. PROBED validates the container allowlist, finds the main
video, and reviews multiple videos or multi-disc entries. PLANNED records the
full stream plan, target dimensions, name, path and estimate. Dry-run leaves
the job PLANNED and moves nothing; flipping dry-run off re-plans once. ENCODING
obeys start gates and writes partial output. VERIFIED hashes and runs all
safety checks. NAMED resolves identity. PUBLISHED builds the final folder in
work on the library filesystem, writes its hidden marker last, and publishes
with one atomic no-replace rename. The verified output is moved into that
folder; no output hard link or byte copy is used. RETIRED moves the original to
trash and writes a manifest. DONE archives logs, updates stats and the speed
table (once per job) and cleans work.

Review reasons and resume states are defined in the review table. Clearing a
review never skips planning, encoding or verification.

## 5. Presets and media rules

Presets are TOML data. The folder name equals the preset id. Shipped:
`keep-remux-mp4`, `keep-remux-mkv`, `1080p-balanced-mp4`, `1080p-small-mp4`,
`720p-small-mp4`, plus a disabled experimental
`1080p-best-x265-mp4`. Encoding never upscales; max dimensions preserve
display aspect ratio using SAR and explicit even numbers.

Every HEVC MP4 output carries `hvc1` and faststart. MP4 remux accepts only
H.264/HEVC video. MP4 audio copies AAC/AC-3/E-AC-3 and transcodes other codecs
by channels per the audio table. Audio selection normalises language tags,
drops commentary, honours original/default and unknown-language rules, and
never emits zero audio if the source had audio. MP4 text subtitles become
`mov_text`; bitmap subtitles cause an MKV fallback or are dropped. Sidecars are
carried. Chapters are kept; MP4 drops attachments/data. HDR, Dolby Vision and
interlacing go to REVIEW unless the preset explicitly retains HDR. Explicit
`-map` is mandatory. MKV presets are remux-only and keep attachments.

SIZE-1 remuxes already-efficient HEVC. SIZE-2 compares video-stream bytes
after an encode and re-plans as remux at `plan_version + 1`, reusing the audio
and subtitle selection. SIZE-3 never publishes a larger encode; remuxes are
exempt from must-be-smaller. SIZE-4 preserves aspect, SAR and frame rate.
SIZE-5 computes explicit even target dimensions. Quality values are
placeholders calibrated by john; speed estimates are keyed by preset, backend
and resolution bucket and updated by real encodes.

Verification compares the output with the plan. Output duration must match
source duration within `max(1 s, 0.1%)`, and the video presentation tail (the
maximum packet `pts + duration`) must match output container duration within
that tolerance; packet order is not presentation order. The video codec must
match (the encoder family for an encode, the copied source codec for a remux)
and every HEVC stream in an MP4 output must carry `hvc1`. Resolution, pixel
format, SAR and frame rate must match. Every colour property the source
*states* must be present and equal in the output; a property the source does
not state is never invented and creates no requirement. Every planned audio
track must match codec, channel count, channel layout, language and default
disposition, and every planned subtitle track must match codec, language,
forced and default disposition. The advisory quality metric is **bits per
pixel per frame** (video payload bits / (presented frames x width x height));
its frame-count basis (container count, decoded count or a labelled estimate)
is reported with it, and a metric that cannot be evaluated is reported as
unavailable rather than silently skipped. A breach of the floor is a REVIEW,
not a failure.

Calibration measures that same operation. It builds the production plan,
encodes an explicitly requested source interval with the production command
builder and encoder, and compares the trial with the same interval of the
source scaled to the planned geometry, so both sides cover the same frames.
It refuses - with a reason - when no eligible backend exists and never
substitutes one silently; a failed comparison or a frame-count mismatch is
`n/a`, never a number. Only a valid trial seeds the speed table, and the
seed is keyed by the backend that actually ran. Trial artifacts live in the
configured work area and are removed through the approved cleanup path.

### 5.5 Shared execution and liveness

The encoder, verifier, calibration trials and capability probes use one
supervision boundary and one exclusive operation slot. A progress callback is
visibility, not proof of work: liveness advances only for a finite, monotonic
frame count, media timestamp or byte count. Intentional pause time does not
count toward a stall or overall-timeout limit.

- **SUP-1** Production verification uses bounded packet and frame aggregates,
  sums video packet bytes, keeps packet count distinct from decoded-frame
  count, and uses the maximum packet `pts + duration` as the presentation end.
- **SUP-2** Cancellation or protection termination suppresses a late success;
  partial results are discarded, owned children are reaped, and the exclusive
  slot and scratch remain held until a non-cooperative worker exits.
- **SUP-3** Success, failure, cancellation, stall, timeout, interruption and
  deferral are distinct. Only uninterrupted success may publish or seed.
  Controlled encode interruptions recover without consuming a restart; encode
  stalls, encode timeouts and genuine interruptions consume one.

Existing encoder and verifier call shapes remain supported; the engine passes
new supervision controls only when the callable accepts them.

The in-flight disk reserve is a bounded heuristic: the gross budget is the
planned source video bytes (falling back to total source bytes); with no real
media progress the full gross budget is reserved; with progress it is the
larger of `gross * (1 - progress)` and a linear projection from partial bytes,
capped by the `1.2 * source_size` admission envelope. It is clamped to
`[0, cap]` and is not a guaranteed upper bound on the final output; the
additive emergency low-water reserve covers nonlinear growth. A missing gross
budget returns no estimate and the guard keeps its full source-size fallback.

MP4 rules: MP4-1, MP4-2, MP4-3, MP4-4, MP4-5, MP4-6, MP4-7, MP4-8, MP4-9,
MP4-10, MP4-11, MP4-12. MKV-1. Size rules: SIZE-1, SIZE-2, SIZE-3, SIZE-4,
SIZE-5. Backend tests run 8-bit and 10-bit HEVC with the exact preset options
and log the rate-control mode.

## 6. Preset validation

Reject at load time: `mkv_policy=never` with `bitmap=mkv_fallback`; MKV with
encode; MP4 with `copy_codecs=["*"]`; HDR keep on encode without 10-bit keep;
`to8` with HDR keep; `ten_bit_source` on remux; software allowed without a
schedule window. Warn when an experimental preset is enabled without a speed
entry. Reject unknown keys/ids and out-of-range values.

## 7. Safety invariants

- **SAF-1** originals only move to trash; no early automatic purge.
- **SAF-2** verify before publish; failures leave the original untouched.
- **SAF-3** atomic publish from a staged work folder with one no-replace rename; no output hard link or byte copy.
- **SAF-4** never overwrite; duplicate goes to REVIEW; replace records an intent and moves the old copy to trash first.
- **SAF-5** one guarded delete function and one retire_move; no shell.
- **SAF-6** crash-safe, idempotent recovery in every window, including marker adoption and intents.
- **SAF-7** one daemon and one writer via flock; one encode at a time.
- **SAF-8** source-size admission envelope before encode and publish; publish adds only staged sidecar bytes; mid-encode uses bounded net remaining work plus emergency reserve; trash counts; never early purge.
- **SAF-9** layered machine protection: systemd limits, start gates, thermal stop, hwmon discovery, fail closed, never touch fans/PROCHOT.
- **SAF-10** unprivileged service; secrets only in env files, bearer header, redacted everywhere.
- **SAF-11** fresh install dry-run; explicit logged disable; nothing moves in dry-run.
- **SAF-12** liveness: restart limit, stall detector, ffmpeg-version change pauses until capability tests pass.
- **SAF-13** untrusted input: protocol whitelist, format allowlist, regular files only.
- **SAF-14** calibration measures the production operation; no silent backend substitution; only valid trials seed the speed table.
- **DEPLOY-1** every unverified claim has one registry item; only recorded on-box evidence can verify it.
- **DEPLOY-4** host-only install and runtime startup do not touch library metadata; observational commands do not create state; no software/x265 probing in the initial workflow or automatic retests.
- **DEPLOY-2** doctor is a read-only preflight with pass/fail/unknown outcomes and class-aware exit codes.
- **DEPLOY-3** installer/upgrade rehearsal and redacted evidence bundles are tested; no on-box item is marked verified by the agent.
- **STATE-1** one shared job-state vocabulary; RESTORED is terminal history and does not block a re-drop.
- **CONFIG-1** keyed reload classification, atomic generations and plan binding; restart-required changes stay visibly pending.
- **COMPAT-1** enforced schema versions, backed-up ordered migrations and explicit semantic generations for plans/speed/capabilities.
- **SUP-1** bounded verification scans; packet bytes and counts are aggregated without retaining packet records; presentation end is the maximum packet pts + duration.
- **SUP-2** cancellation/protection wins over late success; partial results are discarded; children and exclusive resources remain owned until the worker exits.
- **SUP-3** typed terminal outcomes; only uninterrupted success may publish or seed; controlled encode recovery does not consume a restart.
- **SCHED-1** schedule window is a start gate only; estimates must fit; force is the only override.
- **REV-1** each REVIEW reason has a fixed resume state.
- **ENC-1** encode interface accepts `resume_from`, reserved for phase 2.

## 8. Identification

Use `guessit` for parse, TMDb for identity via a bearer token, with cached
responses. Auto-accept only one candidate with title similarity >= 0.90 and an
exact year (or the documented missing-year case). Ties and uncertainty go to
REVIEW with the top three candidates. Names are `Title (Year)`, sanitised per
the filename rules, NFC normalised and capped at 200 UTF-8 bytes. Multi-disc
and extras are handled or ignored as specified.

## 9. CLI

Commands: `doctor`, `status`, `queue`, `plan`, `run`, `pause`, `resume`,
`cancel`, `review list|accept|move|reject`, `retry`, `calibrate`, `estimate`,
`library report`, `trash list|purge`, `restore`. All support `--short`
(12 lines, 60 columns) and `--json`. Mutations follow the single-writer rule.
Restore requires the original in trash, verifies the published hash, records
an intent, moves the published copy to trash and the original to inbox.

Status/progress JSON are atomically written and contain no titles. Optional
notifications contain counts and job ids only, respect a cooldown, and are
sent for failures, lingering reviews, paused queues and purge summaries.

## 10. Configuration

`/etc/mediaflow/config.toml` holds the settings from the shipped example.
Secrets live only in `tmdb.env` and `notify.env`, mode 0640 root:mediaflow.
Invalid reloads keep the previous good config. A dry-run flip is logged and
re-plans PLANNED jobs once.

#### 10.1 Reload classification

The machine-readable authority is `KEY_CLASS` in `mediaflow.config`. The table
below is its human-readable mirror; the code is authoritative if they differ.

| Class | Keys | Effect |
|---|---|---|
| restart-required | `root`, `state_dir`, `runtime_dir`, `ffmpeg`, `temp_sensor` | Old effective value stays until restart; status shows `restart-required:*`. `temp_sensor` is protection policy: changing it under a live operation could make thermal protection blind. |
| future-jobs | `append_tmdbid`, `stall_minutes` | Captured per operation/job; new operations use the new value. |
| live | `ffprobe`, `dry_run`, `poll_seconds`, `review_reminder_days`, `notify_cooldown_hours`, `stable_seconds`, `trash_retention_days`, `failed_retention_days`, `min_free_gb`, `root_min_free_gb`, `max_trash_gb`, `space_watchdog_seconds`, `pause_when_jellyfin_transcoding`, `pause_on_battery`, `pause_method`, `start_if_temp_below_c`, `thermal_pause_c`, `thermal_resume_c`, `progress_seconds`, `max_restarts`, `min_output_kbps`, `min_bpp`, `jellyfin_user` | Applied between operations; `poll_seconds` updates the daemon loop and notifier keys rebind the notifier at the reload boundary. |
| rejected | any secret-like or unknown key | `config.toml` never holds secrets; unknown keys are errors. |

`dry_run = true` is a start gate: PLANNED jobs re-plan once in either
direction. Work already past PLANNING completes under its recorded plan; no
new move starts while dry-run is on. Secret files are reloaded only on daemon
restart, and their values never appear in reload events, status or diffs.

No new configuration key is required for shared supervision: the existing
`stall_minutes`, `space_watchdog_seconds`, `min_free_gb`, `min_free_gb / 2`
emergency reserve, `pause_on_battery`, `pause_method`, `temp_sensor` and
`max_restarts` settings retain their meanings. Existing plans remain valid;
`plan_version`, `fallback_from` and `working_output` keep their contract, and
remux plans carry an explicit `copy-estimate` instead of borrowing an encode
backend row. New interruption markers are additive settings: a job with no
marker follows the historical crash-recovery path, while a controlled encode
or verification marker takes precedence and is recoverable. Older encoder,
verifier, probe and guard call shapes continue to work; new supervision
controls are supplied only when the callable accepts them.

## 17. Deployment readiness

Pass D does not deploy. It provides a hardware-check registry, a read-only
doctor preflight, an installer rehearsal, an ordered first-run kit and a
redacted evidence-bundle workflow. Host-only installation must not initialize
media roots or change service state, and default doctor/diagnostics must not
run encoding probes. All on-box items remain unverified until John supplies
recorded evidence. See `docs/hardware_checks.toml`,
`docs/DEPLOYMENT_SAFETY_HANDOFF.md`, `docs/DEPLOYMENT.md`, `docs/FIRST_RUN.md`
and `docs/VERIFIED.md`.

## 11-12. Deployment and tests

Python 3.12+ venv at `/opt/mediaflow/venv`, hashed dependencies, `guessit`
only at runtime. Native systemd units with hardening, a daily maintenance timer
and an optional weekly doctor timer. `install.sh` is idempotent and prints
commands before running them. Tests use a sandbox root, refuse `/srv/media`,
cover unit, integration, crash and chaos suites, and include traceability. The
chaos loop kills the daemon between state transitions and asserts originals are
intact or in trash with valid manifests and no partial output in the library.

## 13-16. Rollout, working agreement, risks and references

Build order B0-B5. John installs, runs doctor, stays in dry-run, checks a real
plan, processes one movie and verifies phone direct-play, then batches. The
agent commits small, never edits generated invariants, writes
`docs/QUESTIONS.md` for uncertainty, and pastes test/doctor output into
`docs/VERIFIED.md`. Risk register covers SIGSTOP with a GPU context, memory
limits, ffmpeg upgrades, 10-bit support, hangs, crash windows, TMDb outages,
power cuts, heat and notification privacy. References are the Jellyfin
hardware-acceleration and naming docs, FFmpeg QSV documentation, Kaby Lake
media capability notes and the Swiftfin HEVC tag issue.
