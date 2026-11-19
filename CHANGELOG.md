# Changelog

All notable changes to mediaflow. This project is pre-release until John
completes the on-hardware checklist in `docs/RUNBOOK.md`.

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
- Maintenance deletions outside the media pipeline are contained and logged.

### Known limitations

- HEVC 10-bit hardware encode on Kaby Lake is not assumed; it is tested by
  `doctor` and routed through REVIEW unless available.
- QSV/VAAPI, thermal, AC, power-loss and Jellyfin direct-play behavior remain
  on-hardware VERIFY items in `docs/QUESTIONS.md`.
- TV, downloads, AV1, tone mapping, subtitle OCR/burn-in, Atmos preservation,
  deinterlacing, a web UI, Docker and segmented encoding are out of scope for
  v1.
