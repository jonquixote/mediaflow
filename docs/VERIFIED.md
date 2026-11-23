# Verification evidence

This file records what has actually been run, where, and with which result.
It is evidence, not a promise about john-server. On-hardware items still open
are tracked as unchecked lines in `docs/QUESTIONS.md`; `doctor` warns while any
remain.

## Review record and sandbox verification - 2026-11-22 (Pass C follow-up)

Verified starting state: `main`/`origin/main` at
`7bdbc79f0e99b20851acda6b880294a65fd0e51f`, `pass-c-integration` at
`8b5d067057ac830741cc435aaa91054a46a0a5c5`, and `scripts/check.sh` reporting
`6 passed`, `818 passed` and `818 passed`. No skips were reported. Python
3.14.4, pytest 9.2.0 and Jellyfin ffmpeg 8.1.3-Jellyfin were used;
`/dev/dri/renderD128` was not accessible and no hardware test ran.

### Review record (in place of a pull request)

Branch: `pass-c-followup`. Commits: `d0d2336` (publish path and remaining
findings), `3ad94c9` (configuration classification) and this documentation
commit. The branch was pushed; `gh auth status` reported no authenticated
GitHub host, so **no PR was opened**. The merge into `main` was performed
locally with `--no-ff` and pushed.

Behavioural changes:

- Publish now stages the final folder in `work/`, moves the verified output
  into it, writes the marker last and performs one no-replace rename. Output
  hard links and byte-copy fallbacks were removed.
- Publish disk accounting reserves the SAF-8 source envelope plus staged
  sidecar bytes only.
- SIZE-2 fallback builds a genuine remux plan from source codec/geometry.
- Speed EMA updates are marked per job and cannot replay after a crash.
- Remaining-work estimates are bounded and documented.
- `build_plan` ignores encode speed factors for remuxes.
- `temp_sensor` became restart-required; `poll_seconds` and notifier
  cooldown/reminder keys became live and rebind at a reload boundary.

Tests covering these changes: `tests/test_engine.py` (publish rename-only,
staged recovery, empty destination no-overwrite, speed replay, remaining-work
boundaries, SIZE-2 fallback), `tests/test_plan.py` (planner remux factor),
`tests/test_config_lifecycle.py` (classification and live rebinding), and four
new real-SIGKILL points in `tests/crash_worker.py`/`tests/test_crash.py`
(publish/speed before and after their irreversible step).

Open risks: none of this was exercised on installed hardware or with a real
ffmpeg-side process; the publish kill points are in the engine's staged-folder
path using the test harness. No PR review occurred because the CLI was not
authenticated.

### Commands actually run

```sh
cd /srv/agent-work/mediaflow
.venv/bin/python tools/gen_invariants.py
.venv/bin/python -m pytest -q
scripts/check.sh
```

Exact results:

- generator: `wrote docs/INVARIANTS.md`
- direct full suite: `834 passed in 46.28s`
- `scripts/check.sh` traceability: `6 passed in 0.21s`
- `scripts/check.sh` unit/integration: `834 passed in 45.19s`
- `scripts/check.sh` full suite: `834 passed in 45.09s`

The count differs from the 818-test baseline by +16 follow-up regression
tests. No skips were reported.

## Sandbox verification - 2026-11-22 (Pass C: state and configuration coherence)

Baseline verified before changes: `main`/`origin/main` at
`f4c77c8f38a60a101536e8082918a2e93e2a979c`, `pass-b-integration` at
`639b523ffc3b84eac226ab3a64743bd31618e285`, and `scripts/check.sh` reporting
`6 passed`, `782 passed` and `782 passed`. Python 3.14.4, pytest 9.2.0 and
Jellyfin ffmpeg 8.1.3-Jellyfin were used; `/dev/dri/renderD128` was not
accessible and no hardware test ran.

Final command:

```sh
cd /srv/agent-work/mediaflow
scripts/check.sh
```

Exact results:

- traceability and generated invariants: `6 passed in 0.22s`
- unit and integration: `818 passed in 42.85s`
- full suite including slow/crash/chaos: `818 passed in 41.96s`

A pre-merge run produced the same counts. The delta from the verified
782-test baseline is 36 regression tests.

Cases exercised:

- Shared state classification, exhaustive guard against unclassified states,
  transition validation and recovery edges including ENCODED/VERIFIED restart.
- RESTORED re-drop, active-job blocking, status/history agreement and no
  terminal work-directory churn.
- Config key classification completeness, live/restart behaviour, secret
  redaction, successful reload clearing only `config-invalid`, active-operation
  deferral and same-second fingerprint detection.
- Plan generation/preset binding, deliberate re-plan without retry consumption,
  disabled-preset hold and dry-run flips in both directions.
- v1 database migration with backup preservation, idempotence, failure
  rollback and retry, forward-version refusal, backup restore, legacy plan
  fixtures, legacy speed-key isolation and unversioned capability handling.
- Persistent machine holds after recovery, recovery idempotence, atomic command
  claim and the SAF-8 publish copy reserve.

Not verified here:

- Real migration of the production database and real daemon restart with the
  new generation model.
- Hardware encode, SIGTERM/systemd recovery, VMAF and large-file memory
  behavior remain on-hardware items in `docs/QUESTIONS.md`.

## Sandbox verification - 2026-11-21 (Pass B: shared supervision and bounded verification)

Environment: Linux x86_64, Python 3.14.4, pytest 9.2.0 and
`/usr/lib/jellyfin-ffmpeg/ffmpeg` 8.1.3-Jellyfin in the same sandbox.
`/dev/dri/renderD128` is not readable/writable by the build user, so no
hardware encode ran. Production `/srv/media` and system configuration were not
touched; every run used a sandbox root under `/tmp`.

### Test suite

Command:

```sh
cd /srv/agent-work/mediaflow
scripts/check.sh
```

Exact results:

- traceability and generated invariants: `6 passed in 0.21s`
- unit and integration: `782 passed in 41.86s`
- full suite including slow, crash and chaos tests: `782 passed in 41.35s`

The same command was repeated after the no-ff merge to `main` with identical
counts.

The previous Pass A baseline was 731 tests; this pass adds 51 regression tests.

### Cases exercised

- Real-progress liveness: repeated callbacks, malformed/regressing values,
  independent frame/time/byte dimensions, bounded startup and finalization
  allowances, and suspension during pause.
- Process ownership: cancellation of a real sleeping subprocess, cancellation
  of a stopped child, termination escalation, reaping, bounded output tails and
  lossless progress-line reads.
- Typed outcomes: cancellation, protection interruption, stall, timeout,
  deferral and success remain distinct; internally detected stops do not set
  the caller's cancellation event.
- Verification: reordered packet aggregation, maximum `pts + duration` tail,
  packet/frame distinction, cancellable hashing and scans, full video+audio
  decode, decode timeout, protection interruption and shell-out argument
  safety.
- Engine shutdown and cancellation: daemon-shutdown recovery without consuming
  a restart, a stale user cancellation yielding to shutdown at the next
  operation boundary, late-success suppression, commit-time cancellation,
  safe direct cancellation of an unrelated job, deferred slot/work cleanup
  until a non-cooperative worker exits, and generation-protected cleanup after
  retry.
- Resource ownership: the single expensive-operation slot is held by
  calibration and capability probes through every phase; a busy slot is a
  typed deferral. Non-cooperative workers retain the slot and work until exit.
- Capability honesty: tri-state facts, no second bit-depth probe after an
  incomplete first probe, completed facts surviving later failures,
  spawn-failure unknowns and incomplete reports not overwriting valid evidence.
- Disk and speed-table chain: net remaining work, additive emergency reserve,
  monotonic watchdog cadence, publish-copy reserve, planned backend and
  canonical target-height lookup, remux copy estimate and post-encode update.

### Not verified here

- The four new `docs/QUESTIONS.md` items for Pass B.
- Any QSV or VAAPI behavior, real `SIGTERM`/systemd recovery, large-file
  verifier RSS, reordered/VFR presentation on real media, or interrupted
  capability probing on the installed service.
- The VMAF path remains unverified because this ffmpeg build has no `libvmaf`.

## Sandbox verification - 2026-11-21 (Pass A: execution semantics)

Environment: the same sandbox as the 2026-11-18 run below - Linux x86_64,
Python 3.14.4, pytest 9.2.0, `/usr/lib/jellyfin-ffmpeg/ffmpeg` 8.1.3-Jellyfin.
`/dev/dri/renderD128` is still not readable by the build user, so no hardware
encode ran. Production `/srv/media` and the system configuration were not
touched; every run used a sandbox root under `/tmp`.

### Test suite

Command:

```sh
cd /srv/agent-work/mediaflow
scripts/check.sh
```

Result: `6 passed in 0.20s` for the traceability and generated-invariants
step, `731 passed in 31.13s` for the unit/integration suite and
`731 passed in 31.26s` for the full suite (including slow, crash and chaos
tests). The baseline before this pass was 698 passed; the pass adds 33
regression tests (14 calibration, 12 verifier, 2 planner, 1 encoder, plus
the extended colour and stream-contract cases).

### What the new checks were exercised against

All of these ran on real, tiny files produced with the sandbox ffmpeg:

- Video codec contract: a planned HEVC encode rejects an H.264 output, a
  planned remux accepts the copied H.264 codec, and an MP4 HEVC output
  without the `hvc1` sample entry is rejected while one with it passes.
- Audio contract: codec, channel count, speaker layout, language and the
  default disposition are each checked; a wrong value fails and a downmix is
  judged by its channel count only.
- Subtitle contract: a `mov_text` output passes, a copied text subtitle fails.
- Colour contract: a stated colour property that disappears from the output
  fails, a conflicting value fails, an unstated property creates no
  requirement, and the documented `limited`/`tv` alias compares equal.  The
  planner no longer turns ffprobe's inferred `color_range=tv` into a
  requirement, which is what a plain MKV-to-MP4 remux of an untagged source
  needs.
- Quality metric: the value is bits per pixel per frame (a factor of 25 away
  from the old per-second formula on a 25 fps clip), the frame-count basis is
  reported (`container`, `decoded` or a labelled `estimated` value), a breach
  of the floor is a REVIEW and not a failure, and an unknown frame count is
  reported instead of being skipped.
- Calibration: the trial command is the production command (the planner's
  encoder, quality handling, target scaling, sample window and video-only
  mapping), a 4K source is measured at the planned 1080p geometry, a preset
  without an eligible backend is refused with a reason, a remux preset is
  reported as not applicable, a failed scoring run is `n/a` with a reason, a
  failed trial never seeds the speed table, and a valid trial seeds it under
  the backend that actually ran.  Trial artifacts stayed inside the
  configured work area and were removed afterwards.

### Not verified here

- Every QSV and VAAPI claim (backend selection, look-ahead, 10-bit, colour
  round-trip, rate control) is still hardware-only and is listed in
  `docs/QUESTIONS.md`.
- The sandbox ffmpeg build has no `libvmaf` filter, so only the SSIM path of
  the calibration score was exercised; the VMAF path is unverified.
- The bits-per-pixel floor default (0.01 bits per pixel per frame) is a
  placeholder and was not calibrated against real content.

## Sandbox verification - 2026-11-18

Environment:

- Linux x86_64 sandbox, Python 3.14.4, pytest 9.2.0.
- `/usr/lib/jellyfin-ffmpeg/ffmpeg` 8.1.3-Jellyfin.
- Sandbox media root `/tmp/opencode/mf-doctor/root`; production `/srv/media`
  was never used.
- `/dev/dri/renderD128` exists but is not readable/writable by the build user,
  so QSV/VAAPI encode and the on-hardware checks cannot run here.

### Test suite

Command:

```sh
cd /srv/agent-work/mediaflow
scripts/check.sh
```

Result: `698 passed in 20.85s`. `scripts/check.sh` first runs
`tools/gen_invariants.py --check` and the traceability meta-test, then the
unit/integration suite and the full suite including slow and crash/chaos tests.
The runner refuses a `MEDIAFLOW_ROOT` under `/srv/media`.

### Doctor (sandbox configuration)

Command:

```sh
.venv/bin/python -m mediaflow \
  --config /tmp/opencode/mf-doctor/etc/config.toml doctor
```

Result: exit 1, only because the sandbox user cannot open the GPU render node
and unresolved on-hardware items remain. Full output:

```text
[OK  ] ffmpeg-binary: /usr/lib/jellyfin-ffmpeg/ffmpeg
[OK  ] ffprobe-binary: /usr/lib/jellyfin-ffmpeg/ffprobe
[OK  ] ffmpeg-version: ffmpeg version 8.1.3-Jellyfin Copyright (c) 2000-2026 the FFmpeg developers
[FAIL] render-node: not readable/writable by this user: /dev/dri/renderD128
[WARN] encode-qsv: HEVC 8-bit=no, 10-bit=no, rate-control=VBR
[WARN] encode-vaapi: HEVC 8-bit=no, 10-bit=no, rate-control=unknown
[OK  ] encode-software: HEVC 8-bit=yes, 10-bit=yes, rate-control=unknown
[WARN] hardware-encode: no hardware HEVC 8-bit encoder; only MKV remuxes will work
[WARN] hevc-10bit: hardware 10-bit HEVC unavailable; 10bit-no-hw REVIEW path active
[OK  ] temp-sensor: coretemp: /sys/class/hwmon/hwmon1/temp1_input (80.0 C)
[OK  ] ac-power: AC online
[OK  ] one-filesystem: pipeline and library share one st_dev
[OK  ] free-space: 7.4 GiB free (need 1 GiB)
[OK  ] trash-size: 0.0 GiB in trash
[OK  ] perm-pipeline: 0o2775 setgid=True writable=True
[OK  ] perm-movies: 0o2775 setgid=True writable=True
[OK  ] perm-work: 0o2775 setgid=True writable=True
[OK  ] perm-convert: 0o2775 setgid=True writable=True
[OK  ] perm-trash: 0o2775 setgid=True writable=True
[OK  ] db-integrity: integrity_check=ok
[WARN] db-backup: no backup yet
[OK  ] secrets: no secret-like keys in config.toml
[OK  ] secret-files: env files missing or permissions acceptable
[OK  ] presets: 5 enabled
[WARN] verify-items: 24 unchecked on-hardware item(s) in QUESTIONS.md
[OK  ] tmdb: optional: no token configured; parse-only fallback

doctor: problems found (26 checks, 6 warnings)
```

The `hevc-10bit` warning is deliberate: software libx265 10-bit works, but
hardware 10-bit is separately reported and never inferred from software.

### Sandbox end-to-end checks

These used generated sample files under `/tmp/opencode/mf-final2` and a sandbox
media root. The sandbox start gates were relaxed for temperature/space so the
runs could proceed; verification was not relaxed.

- H.264 MKV through `keep-remux-mp4`: stream copy, `+faststart`, atomic
  publish, manifest and marker present, original retired to trash, and the
  published file passed the mandatory video-plus-audio full decode.
- HEVC MKV through a software-enabled copy of `1080p-balanced-mp4`: encoded
  MP4 with `hvc1`, mandatory full decode, publish and retire.
- HEVC MPEG-TS through `keep-remux-mp4`: valid `hvc1`/`hvcC`, mandatory full
  decode, publish and retire.
- A job left in ENCODING by a start gate across process restarts returned to
  PLANNED without consuming a restart; a genuinely interrupted encode still
  consumes one. This is covered by `tests/test_engine.py`.

Crash/chaos tests kill workers between state transitions and assert that the
original is intact or in trash, the manifest is valid, and the library contains
no partial output. See `tests/test_crash.py` and `tests/crash_worker.py`.

## Still requires john-server

The following cannot be settled in this sandbox and remain unchecked in
`docs/QUESTIONS.md`:

- Real QSV and VAAPI HEVC 8-bit/10-bit behavior and the engaged rate-control
  mode on the Kaby Lake GPU.
- SIGSTOP/SIGCONT with a real GPU context, thermal/AC pause-resume, fan profile
  with the lid closed, and memory/CPU quota behavior under systemd.
- Pull-the-plug recovery on real hardware.
- Jellyfin direct-play on phones, marker invisibility, TMDb and notification
  delivery with real services.

Do not set `dry_run = false` until `doctor` is green and that list is clear.
