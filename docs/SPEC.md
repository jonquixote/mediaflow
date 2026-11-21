# mediaflow - Media Optimisation and Organisation Funnel

**Spec v1.0** for john-server (Ubuntu Server 26.04.1 LTS, MacBookPro14,1, Intel i5-7360U, Iris Plus 640).
Default output container: **MP4**. Owner: john. Builder: the coding agent (`agent` user), reviewed by john.

The machine-readable requirement registry is `docs/requirements.toml`. Every
normative ID lives there. `docs/INVARIANTS.md` is generated from the
safety-flagged entries and is never edited by hand.

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
   any state -> REVIEW | FAILED
```

The poller scans every 30 seconds. STABLE waits for unchanged size and mtime
and no temp markers. PROBED validates the container allowlist, finds the main
video, and reviews multiple videos or multi-disc entries. PLANNED records the
full stream plan, target dimensions, name, path and estimate. Dry-run leaves
the job PLANNED and moves nothing; flipping dry-run off re-plans once. ENCODING
obeys start gates and writes partial output. VERIFIED hashes and runs all
safety checks. NAMED resolves identity. PUBLISHED builds a folder in work and
renames it into the library with a hidden marker. RETIRED moves the original to
trash and writes a manifest. DONE archives logs, updates stats and the speed
table and cleans work.

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

Verification compares the output with the plan. The video codec must match
(the encoder family for an encode, the copied source codec for a remux) and
every HEVC stream in an MP4 output must carry `hvc1`. Resolution, pixel
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
- **SAF-3** atomic publish, one filesystem, every move one rename.
- **SAF-4** never overwrite; duplicate goes to REVIEW; replace records an intent and moves the old copy to trash first.
- **SAF-5** one guarded delete function and one retire_move; no shell.
- **SAF-6** crash-safe, idempotent recovery in every window, including marker adoption and intents.
- **SAF-7** one daemon and one writer via flock; one encode at a time.
- **SAF-8** free-space guards before/during encode and before publish; trash counts; never early purge.
- **SAF-9** layered machine protection: systemd limits, start gates, thermal stop, hwmon discovery, fail closed, never touch fans/PROCHOT.
- **SAF-10** unprivileged service; secrets only in env files, bearer header, redacted everywhere.
- **SAF-11** fresh install dry-run; explicit logged disable; nothing moves in dry-run.
- **SAF-12** liveness: restart limit, stall detector, ffmpeg-version change pauses until capability tests pass.
- **SAF-13** untrusted input: protocol whitelist, format allowlist, regular files only.
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
