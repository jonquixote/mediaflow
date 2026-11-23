# Hardware and deployment verification checks (generated)

Generated from `docs/hardware_checks.toml`; do not edit by hand.
The registry is authoritative for status. `docs/QUESTIONS.md` lists the
open items, `docs/VERIFIED.md` records evidence, and `docs/RUNBOOK.md`
gives the ordered procedure. The sandbox agent never marks an item
`verified-on-hardware`.

- unverified: 44
- verified-sandbox: 0
- verified-on-hardware: 0

| ID | Class | Runner | Status | Supports | Title |
|---|---|---|---|---|---|
| HW-001 | required | doctor-automatable | unverified | DEPLOY-1, DEPLOY-2, DEPLOY-3 | real QSV HEVC 8-bit encode and the actual rate-control mode (ICQ vs CQP vs hybrid) on the Kaby Lake GPU; the s |
| HW-002 | required | doctor-automatable | unverified | DEPLOY-1, DEPLOY-2 | real VAAPI HEVC 8-bit fallback and its rate-control mode |
| HW-003 | feature-conditional | doctor-automatable | unverified | DEPLOY-1, DEPLOY-2 | Kaby Lake HEVC 10-bit hardware encode: disputed in public sources and deliberately never assumed |
| HW-004 | required | manual-by-John | unverified | DEPLOY-1, DEPLOY-3 | whether ffmpeg debug output exposes the engaged rate-control mode as expected; if it does not |
| HW-005 | required | manual-by-John | unverified | DEPLOY-1 | software-decode + hardware-upload fallback for source codecs without a hardware decoder. Run one real non-QSV- |
| HW-006 | informational | doctor-automatable | unverified | DEPLOY-1, DEPLOY-2, SAF-9 | `IOSchedulingClass=idle` effect under the I/O scheduler in use (check `/sys/block/*/queue/scheduler`) |
| HW-007 | feature-conditional | manual-by-John | unverified | DEPLOY-1, SAF-9 | `MemoryHigh=2G`/`MemoryMax=3G` versus peak RSS for software decode plus HEVC encode and for a short x265 `slow |
| HW-008 | informational | manual-by-John | unverified | DEPLOY-1, SAF-9 | `CPUQuota=300%` on this 2-core/4-thread CPU |
| HW-009 | required | doctor-automatable | unverified | DEPLOY-1, DEPLOY-2, DEPLOY-3, SAF-9 | `DeviceAllow=/dev/dri/renderD128 rw` and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` do not break QSV/V |
| HW-010 | required | manual-by-John | unverified | DEPLOY-1, SAF-9 | SIGSTOP/SIGCONT of a real QSV/VAAPI encoder with an open GPU context finishes with a valid file |
| HW-011 | required | manual-by-John | unverified | DEPLOY-1, SAF-9 | thermal pause/resume and the 60-second sustain threshold on the real hwmon sensor/fan profile |
| HW-012 | required | manual-by-John | unverified | DEPLOY-1, SAF-9 | AC-loss pause and resume during a real encode |
| HW-013 | required | manual-by-John | unverified | DEPLOY-1, SAF-9 | whether the fan/PROCHOT profile is adequate while the lid is closed; mediaflow never changes it. Record the ac |
| HW-014 | required | manual-by-John | unverified | DEPLOY-1 | pulled-plug encode resumes with no corrupt or duplicate output on real hardware |
| HW-015 | required | manual-by-John | unverified | DEPLOY-1 | real phone Jellyfin direct-play of an encoded MP4 and an HEVC remux (including `hvc1`) |
| HW-016 | required | manual-by-John | unverified | DEPLOY-1 | Jellyfin ignores the hidden `.mediaflow-<job-id>` marker |
| HW-017 | feature-conditional | manual-by-John | unverified | DEPLOY-1 | optional `[tmdbid-N]` Jellyfin provider-ID folder syntax against current Jellyfin docs |
| HW-018 | informational | manual-by-John | unverified | DEPLOY-1 | `calibrate` on one animation |
| HW-019 | informational | manual-by-John | unverified | DEPLOY-1, SAF-14 | `calibrate` on the real machine reports the same backend the plan would select (`doctor` backend |
| HW-020 | feature-conditional | manual-by-John | unverified | DEPLOY-1, SAF-14 | the calibration quality score is meaningful on the real machine: the sandbox ffmpeg build has no `libvmaf` |
| HW-021 | informational | manual-by-John | unverified | DEPLOY-1, SAF-14 | the speed-table seed is found again by `estimate` after a calibration |
| HW-022 | informational | manual-by-John | unverified | DEPLOY-1 | speed-table estimates within 30% of real encode time on the sample clips |
| HW-023 | feature-conditional | manual-by-John | unverified | DEPLOY-1 | the experimental x265 preset estimate versus the overnight window |
| HW-024 | required | manual-by-John | unverified | DEPLOY-1, SAF-2 | the QSV and VAAPI encode paths carry the source's stated colour description (primaries |
| HW-025 | required | manual-by-John | unverified | DEPLOY-1, SAF-2 | an MP4 output written by the hardware encoders contains a complete `colr` box: the MP4 `nclx` entry needs prim |
| HW-026 | required | manual-by-John | unverified | DEPLOY-1, SAF-2 | the planner-selected backend and the engine speed-table key now use the same backend and canonical target-heig |
| HW-027 | informational | manual-by-John | unverified | DEPLOY-1, DEPLOY-3, SAF-2 | the bits-per-pixel floor default (`min_bpp = 0.01` bits per pixel per frame |
| HW-028 | informational | manual-by-John | unverified | DEPLOY-1, SAF-2 | the presented-frame-count basis for variable-frame-rate sources (container count |
| HW-029 | required | manual-by-John | unverified | DEPLOY-1, SAF-2 | the MP4 muxer marks the first track of a type as default even when the plan says otherwise |
| HW-030 | required | doctor-automatable | unverified | DEPLOY-1, DEPLOY-2, SAF-8 | if an external media drive is added later |
| HW-031 | required | manual-by-John | unverified | DEPLOY-1, SAF-9 | Jellyfin-transcoding detection with the real `jellyfin` user and a real transcode |
| HW-032 | feature-conditional | manual-by-John | unverified | DEPLOY-1, DEPLOY-3 | TMDb reachability and one real title lookup |
| HW-033 | feature-conditional | manual-by-John | unverified | DEPLOY-1, SAF-10 | optional notification delivery and payload redaction with the real notify service |
| HW-034 | required | manual-by-John | unverified | DEPLOY-1, SUP-3 | controlled `SIGTERM` during a real encode and during verification |
| HW-035 | required | manual-by-John | unverified | DEPLOY-1, SUP-1 | reordered and variable-frame-rate packet presentation on real media |
| HW-036 | required | manual-by-John | unverified | DEPLOY-1, SUP-1 | bounded verification memory on a large packet-dense file |
| HW-037 | required | manual-by-John | unverified | DEPLOY-1, SUP-3 | an interrupted or deferred capability probe |
| HW-038 | required | doctor-automatable | unverified | COMPAT-1, DEPLOY-1, DEPLOY-2, DEPLOY-3 | migration of the real `state.db` before enabling live writes |
| HW-039 | required | manual-by-John | unverified | CONFIG-1, DEPLOY-1, DEPLOY-3 | live reload during a real encode |
| HW-040 | required | manual-by-John | unverified | CONFIG-1, DEPLOY-1, DEPLOY-3 | TMDb/notify secret rotation on the installed service |
| HW-041 | required | manual-by-John | unverified | DEPLOY-1, STATE-1 | restore and re-drop a real title |
| HW-042 | required | manual-by-John | unverified | DEPLOY-1, SAF-3 | staged publish on the real shared filesystem |
| HW-043 | required | manual-by-John | unverified | CONFIG-1, DEPLOY-1, DEPLOY-3 | thermal input reload on the installed service |
| HW-044 | required | manual-by-John | unverified | DEPLOY-1, SAF-8 | publish disk boundary with real sidecars |

## Details

### HW-001 — real QSV HEVC 8-bit encode and the actual rate-control mode (ICQ vs CQP vs hybrid) on the Kaby Lake GPU; the s

- Claim: [ ] Section 5.5, real QSV HEVC 8-bit encode and the actual rate-control mode (ICQ vs CQP vs hybrid) on the Kaby Lake GPU; the sandbox agent cannot open `/dev/dri/renderD128`, section 7. Run `sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor` after install and paste the QSV/rate-control result into `docs/VERIFIED.md`.
- Class: required; runner: doctor-automatable
- Supports: DEPLOY-1, DEPLOY-2, DEPLOY-3
- Procedure: Run `sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor` after install and paste the QSV/rate-control result into `docs/VERIFIED.md`.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-001/`
- Status: unverified

### HW-002 — real VAAPI HEVC 8-bit fallback and its rate-control mode

- Claim: [ ] Section 5.5, real VAAPI HEVC 8-bit fallback and its rate-control mode, section 7. Run the same `doctor`; if VAAPI works while QSV fails, verify the planner chooses VAAPI and logs it.
- Class: required; runner: doctor-automatable
- Supports: DEPLOY-1, DEPLOY-2
- Procedure: Run the same `doctor`; if VAAPI works while QSV fails, verify the planner chooses VAAPI and logs it.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-002/`
- Status: unverified

### HW-003 — Kaby Lake HEVC 10-bit hardware encode: disputed in public sources and deliberately never assumed

- Claim: [ ] Section 5.5, Kaby Lake HEVC 10-bit hardware encode: disputed in public sources and deliberately never assumed, section 7. Run `doctor`; confirm the separate 10-bit result and exercise a 10-bit source through `keep-remux-mkv` and, if supported, `1080p-balanced-mp4`.
- Class: feature-conditional; runner: doctor-automatable
- Supports: DEPLOY-1, DEPLOY-2
- Procedure: Run `doctor`; confirm the separate 10-bit result and exercise a 10-bit source through `keep-remux-mkv` and, if supported, `1080p-balanced-mp4`.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-003/`
- Status: unverified

### HW-004 — whether ffmpeg debug output exposes the engaged rate-control mode as expected; if it does not

- Claim: [ ] Section 5.5, whether ffmpeg debug output exposes the engaged rate-control mode as expected; if it does not, inspect `ffmpeg -v debug` for your installed `jellyfin-ffmpeg` and record the stable detector output in `docs/VERIFIED.md`.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, DEPLOY-3
- Procedure: [ ] Section 5.5, whether ffmpeg debug output exposes the engaged rate-control mode as expected; if it does not, inspect `ffmpeg -v debug` for your installed `jellyfin-ffmpeg` and record the stable detector output in `docs/VERIFIED.md`.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-004/`
- Status: unverified

### HW-005 — software-decode + hardware-upload fallback for source codecs without a hardware decoder. Run one real non-QSV-

- Claim: [ ] Section 5.5, software-decode + hardware-upload fallback for source codecs without a hardware decoder. Run one real non-QSV-decodable source through an encode preset and confirm the log says software decode + upload rather than failing.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: Run one real non-QSV-decodable source through an encode preset and confirm the log says software decode + upload rather than failing.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-005/`
- Status: unverified

### HW-006 — `IOSchedulingClass=idle` effect under the I/O scheduler in use (check `/sys/block/*/queue/scheduler`)

- Claim: [ ] Section 7/SAF-9, `IOSchedulingClass=idle` effect under the I/O scheduler in use (check `/sys/block/*/queue/scheduler`), section 7. Run a real encode and inspect `ionice`/latency; record the scheduler and result.
- Class: informational; runner: doctor-automatable
- Supports: DEPLOY-1, DEPLOY-2, SAF-9
- Procedure: Run a real encode and inspect `ionice`/latency; record the scheduler and result.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-006/`
- Status: unverified

### HW-007 — `MemoryHigh=2G`/`MemoryMax=3G` versus peak RSS for software decode plus HEVC encode and for a short x265 `slow

- Claim: [ ] Section 7/SAF-9, `MemoryHigh=2G`/`MemoryMax=3G` versus peak RSS for software decode plus HEVC encode and for a short x265 `slow` sample, section 7. Use `/usr/bin/time -v` or systemd accounting; adjust the unit if needed.
- Class: feature-conditional; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: [ ] Section 7/SAF-9, `MemoryHigh=2G`/`MemoryMax=3G` versus peak RSS for software decode plus HEVC encode and for a short x265 `slow` sample, section 7. Use `/usr/bin/time -v` or systemd accounting; adjust the unit if needed.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-007/`
- Status: unverified

### HW-008 — `CPUQuota=300%` on this 2-core/4-thread CPU

- Claim: [ ] Section 7/SAF-9, `CPUQuota=300%` on this 2-core/4-thread CPU, section 7. Start a real encode with the unit and confirm the effective quota and temperature behaviour.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: Start a real encode with the unit and confirm the effective quota and temperature behaviour.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-008/`
- Status: unverified

### HW-009 — `DeviceAllow=/dev/dri/renderD128 rw` and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` do not break QSV/V

- Claim: [ ] Section 7/SAF-9, `DeviceAllow=/dev/dri/renderD128 rw` and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` do not break QSV/VAAPI or TMDb HTTPS, section 7. Run `doctor` and one real encode inside the installed unit; if broken, record the minimal change and update the unit.
- Class: required; runner: doctor-automatable
- Supports: DEPLOY-1, DEPLOY-2, DEPLOY-3, SAF-9
- Procedure: Run `doctor` and one real encode inside the installed unit; if broken, record the minimal change and update the unit.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-009/`
- Status: unverified

### HW-010 — SIGSTOP/SIGCONT of a real QSV/VAAPI encoder with an open GPU context finishes with a valid file

- Claim: [ ] Section 5.5/SAF-9, SIGSTOP/SIGCONT of a real QSV/VAAPI encoder with an open GPU context finishes with a valid file, section 7. Start a real encode, `kill -STOP` then `kill -CONT` the ffmpeg pid, and verify the final file with `ffprobe` and the decode test. If it hangs or corrupts, set `pause_method = "idle_after_job"` and repeat thermal/AC tests.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: Start a real encode, `kill -STOP` then `kill -CONT` the ffmpeg pid, and verify the final file with `ffprobe` and the decode test. If it hangs or corrupts, set `pause_method = "idle_after_job"` and repeat thermal/AC tests.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-010/`
- Status: unverified

### HW-011 — thermal pause/resume and the 60-second sustain threshold on the real hwmon sensor/fan profile

- Claim: [ ] Section 7/SAF-9, thermal pause/resume and the 60-second sustain threshold on the real hwmon sensor/fan profile, section 7. Close the lid, run under load, and record package temperature plus every `encode-paused`/`encode-resumed` event. Re-run the 1-hour test after any fan change.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: Close the lid, run under load, and record package temperature plus every `encode-paused`/`encode-resumed` event. Re-run the 1-hour test after any fan change.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-011/`
- Status: unverified

### HW-012 — AC-loss pause and resume during a real encode

- Claim: [ ] Section 7/SAF-9, AC-loss pause and resume during a real encode, section 7. Unplug the charger mid-encode, confirm SIGSTOP/paused status, replug, and verify the output passes verification.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: Unplug the charger mid-encode, confirm SIGSTOP/paused status, replug, and verify the output passes verification.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-012/`
- Status: unverified

### HW-013 — whether the fan/PROCHOT profile is adequate while the lid is closed; mediaflow never changes it. Record the ac

- Claim: [ ] Section 5.5/SAF-9, whether the fan/PROCHOT profile is adequate while the lid is closed; mediaflow never changes it. Record the active fan settings with the thermal test.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: [ ] Section 5.5/SAF-9, whether the fan/PROCHOT profile is adequate while the lid is closed; mediaflow never changes it. Record the active fan settings with the thermal test.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-013/`
- Status: unverified

### HW-014 — pulled-plug encode resumes with no corrupt or duplicate output on real hardware

- Claim: [ ] Section 12, pulled-plug encode resumes with no corrupt or duplicate output on real hardware, section 7. With `dry_run=false`, pull power mid-encode, reboot, and verify final state, manifest, hashes, and no duplicate in `movies/`.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: With `dry_run=false`, pull power mid-encode, reboot, and verify final state, manifest, hashes, and no duplicate in `movies/`.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-014/`
- Status: unverified

### HW-015 — real phone Jellyfin direct-play of an encoded MP4 and an HEVC remux (including `hvc1`)

- Claim: [ ] Section 12, real phone Jellyfin direct-play of an encoded MP4 and an HEVC remux (including `hvc1`), section 8. Play both on the Jellyfin phone app and an iPhone; confirm Direct Play with no transcode.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: Play both on the Jellyfin phone app and an iPhone; confirm Direct Play with no transcode.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-015/`
- Status: unverified

### HW-016 — Jellyfin ignores the hidden `.mediaflow-<job-id>` marker

- Claim: [ ] Section 12, Jellyfin ignores the hidden `.mediaflow-<job-id>` marker, section 7. Confirm the movie still appears once and no marker warning appears in Jellyfin logs.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: Confirm the movie still appears once and no marker warning appears in Jellyfin logs.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-016/`
- Status: unverified

### HW-017 — optional `[tmdbid-N]` Jellyfin provider-ID folder syntax against current Jellyfin docs

- Claim: [ ] Section 8, optional `[tmdbid-N]` Jellyfin provider-ID folder syntax against current Jellyfin docs, section 8. Leave `append_tmdbid=false` until tested; if enabling, verify provider matching for one title.
- Class: feature-conditional; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: [ ] Section 8, optional `[tmdbid-N]` Jellyfin provider-ID folder syntax against current Jellyfin docs, section 8. Leave `append_tmdbid=false` until tested; if enabling, verify provider matching for one title.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-017/`
- Status: unverified

### HW-018 — `calibrate` on one animation

- Claim: [ ] Section 5.4, `calibrate` on one animation, one dark live-action and one high-grain film, section 5.4. Run with 90 seconds, record size/time/VMAF or SSIM, and write the chosen quality values into the preset files.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: Run with 90 seconds, record size/time/VMAF or SSIM, and write the chosen quality values into the preset files.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-018/`
- Status: unverified

### HW-019 — `calibrate` on the real machine reports the same backend the plan would select (`doctor` backend

- Claim: [ ] Section 5.4/SAF-14, `calibrate` on the real machine reports the same backend the plan would select (`doctor` backend, not a silent software fallback), section 5.4. Run `mediaflow calibrate <clip> --preset 1080p-balanced-mp4 --json` and check that `backend`/`encoder` match the hardware the planner picks.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1, SAF-14
- Procedure: Run `mediaflow calibrate <clip> --preset 1080p-balanced-mp4 --json` and check that `backend`/`encoder` match the hardware the planner picks.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-019/`
- Status: unverified

### HW-020 — the calibration quality score is meaningful on the real machine: the sandbox ffmpeg build has no `libvmaf`

- Claim: [ ] Section 5.4/SAF-14, the calibration quality score is meaningful on the real machine: the sandbox ffmpeg build has no `libvmaf`, so only SSIM was exercised, section 5.4. Run the same clip with an ffmpeg build that has `libvmaf` and record both numbers next to the chosen quality.
- Class: feature-conditional; runner: manual-by-John
- Supports: DEPLOY-1, SAF-14
- Procedure: Run the same clip with an ffmpeg build that has `libvmaf` and record both numbers next to the chosen quality.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-020/`
- Status: unverified

### HW-021 — the speed-table seed is found again by `estimate` after a calibration

- Claim: [ ] Section 5.4/SAF-14, the speed-table seed is found again by `estimate` after a calibration, section 12. Calibrate, then run `mediaflow estimate <path> --preset <id>` and confirm the factor matches the seeded one.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1, SAF-14
- Procedure: [ ] Section 5.4/SAF-14, the speed-table seed is found again by `estimate` after a calibration, section 12. Calibrate, then run `mediaflow estimate <path> --preset <id>` and confirm the factor matches the seeded one.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-021/`
- Status: unverified

### HW-022 — speed-table estimates within 30% of real encode time on the sample clips

- Claim: [ ] Section 5.4, speed-table estimates within 30% of real encode time on the sample clips, section 12. Compare `estimate` before and real encode wall time after; record both.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: Compare `estimate` before and real encode wall time after; record both.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-022/`
- Status: unverified

### HW-023 — the experimental x265 preset estimate versus the overnight window

- Claim: [ ] Section 5.4, the experimental x265 preset estimate versus the overnight window, section 5.1. Leave it disabled; if testing, calibrate first, copy the preset into `presets.d`, set `enabled=true`, create its convert folder, and confirm the schedule gate refuses jobs longer than the window.
- Class: feature-conditional; runner: manual-by-John
- Supports: DEPLOY-1
- Procedure: [ ] Section 5.4, the experimental x265 preset estimate versus the overnight window, section 5.1. Leave it disabled; if testing, calibrate first, copy the preset into `presets.d`, set `enabled=true`, create its convert folder, and confirm the schedule gate refuses jobs longer than the window.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-023/`
- Status: unverified

### HW-024 — the QSV and VAAPI encode paths carry the source's stated colour description (primaries

- Claim: [ ] Section 7/SAF-2, the QSV and VAAPI encode paths carry the source's stated colour description (primaries, transfer, matrix, range) into the output; the sandbox could only verify the software path, section 7. Encode one tagged bt709 and one tagged bt2020/smpte2084 clip with `1080p-balanced-mp4` and check `ffprobe -show_streams` for `color_primaries`, `color_transfer`, `color_space` and `color_range`.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-2
- Procedure: Encode one tagged bt709 and one tagged bt2020/smpte2084 clip with `1080p-balanced-mp4` and check `ffprobe -show_streams` for `color_primaries`, `color_transfer`, `color_space` and `color_range`.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-024/`
- Status: unverified

### HW-025 — an MP4 output written by the hardware encoders contains a complete `colr` box: the MP4 `nclx` entry needs prim

- Claim: [ ] Section 7/SAF-2, an MP4 output written by the hardware encoders contains a complete `colr` box: the MP4 `nclx` entry needs primaries, transfer and matrix, and a range-only description cannot be carried, section 7. Inspect the box of a hardware-encoded MP4 (`ffprobe -show_streams` plus a hex look for `colr`) and record which fields survive.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-2
- Procedure: Inspect the box of a hardware-encoded MP4 (`ffprobe -show_streams` plus a hex look for `colr`) and record which fields survive.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-025/`
- Status: unverified

### HW-026 — the planner-selected backend and the engine speed-table key now use the same backend and canonical target-heig

- Claim: [ ] Section 5.5/SAF-2, the planner-selected backend and the engine speed-table key now use the same backend and canonical target-height bucket in the sandbox, section 5.5. On the real machine, compare the backend in `mediaflow plan --json` with the backend in `mediaflow estimate --json` for the same source, including a scaled source, and confirm they agree with the backend that actually ran.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-2
- Procedure: [ ] Section 5.5/SAF-2, the planner-selected backend and the engine speed-table key now use the same backend and canonical target-height bucket in the sandbox, section 5.5. On the real machine, compare the backend in `mediaflow plan --json` with the backend in `mediaflow estimate --json` for the same source, including a scaled source, and confirm they agree with the backend that actually ran.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-026/`
- Status: unverified

### HW-027 — the bits-per-pixel floor default (`min_bpp = 0.01` bits per pixel per frame

- Claim: [ ] Section 7/SAF-2, the bits-per-pixel floor default (`min_bpp = 0.01` bits per pixel per frame, about 498 kbps at 1080p24) is a placeholder and is not empirically calibrated; the metric changed units in this pass, section 7. Run the calibration on the three sample clips, record the measured bits per pixel per frame, and set the default from that evidence.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1, DEPLOY-3, SAF-2
- Procedure: Run the calibration on the three sample clips, record the measured bits per pixel per frame, and set the default from that evidence.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-027/`
- Status: unverified

### HW-028 — the presented-frame-count basis for variable-frame-rate sources (container count

- Claim: [ ] Section 7/SAF-2, the presented-frame-count basis for variable-frame-rate sources (container count, then decoded count, then a labelled estimate), section 7. Take a VFR sample, compare `ffprobe -count_frames` with the container count, and record which basis the verifier used.
- Class: informational; runner: manual-by-John
- Supports: DEPLOY-1, SAF-2
- Procedure: [ ] Section 7/SAF-2, the presented-frame-count basis for variable-frame-rate sources (container count, then decoded count, then a labelled estimate), section 7. Take a VFR sample, compare `ffprobe -count_frames` with the container count, and record which basis the verifier used.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-028/`
- Status: unverified

### HW-029 — the MP4 muxer marks the first track of a type as default even when the plan says otherwise

- Claim: [ ] Section 7/SAF-2, the MP4 muxer marks the first track of a type as default even when the plan says otherwise, while MKV keeps the disposition exactly; the plan always marks the first selected audio track as default, section 7. Confirm on the real machine that a multi-track source keeps its intended default track after an MP4 encode.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-2
- Procedure: Confirm on the real machine that a multi-track source keeps its intended default track after an MP4 encode.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-029/`
- Status: unverified

### HW-030 — if an external media drive is added later

- Claim: [ ] Section 7/SAF-8, if an external media drive is added later, every pipeline path and `movies/` still share one filesystem and the systemd `ReadWritePaths`/mount paths are updated, section 7. Run `doctor`; it must fail if `st_dev` differs.
- Class: required; runner: doctor-automatable
- Supports: DEPLOY-1, DEPLOY-2, SAF-8
- Procedure: Run `doctor`; it must fail if `st_dev` differs.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-030/`
- Status: unverified

### HW-031 — Jellyfin-transcoding detection with the real `jellyfin` user and a real transcode

- Claim: [ ] Section 7/SAF-9, Jellyfin-transcoding detection with the real `jellyfin` user and a real transcode, section 7. Start a transcode and confirm mediaflow refuses to start/queues a job.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-9
- Procedure: Start a transcode and confirm mediaflow refuses to start/queues a job.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-031/`
- Status: unverified

### HW-032 — TMDb reachability and one real title lookup

- Claim: [ ] Section 8, TMDb reachability and one real title lookup, section 8. Install a token, run one dry-run plan, confirm cached response reuse and that the token never appears in process output or logs.
- Class: feature-conditional; runner: manual-by-John
- Supports: DEPLOY-1, DEPLOY-3
- Procedure: [ ] Section 8, TMDb reachability and one real title lookup, section 8. Install a token, run one dry-run plan, confirm cached response reuse and that the token never appears in process output or logs.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-032/`
- Status: unverified

### HW-033 — optional notification delivery and payload redaction with the real notify service

- Claim: [ ] Section 9.1/SAF-10, optional notification delivery and payload redaction with the real notify service, section 9.1. Trigger a REVIEW and confirm the payload contains only counts/job ids; verify cooldown.
- Class: feature-conditional; runner: manual-by-John
- Supports: DEPLOY-1, SAF-10
- Procedure: [ ] Section 9.1/SAF-10, optional notification delivery and payload redaction with the real notify service, section 9.1. Trigger a REVIEW and confirm the payload contains only counts/job ids; verify cooldown.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-033/`
- Status: unverified

### HW-034 — controlled `SIGTERM` during a real encode and during verification

- Claim: [ ] Section 5.5/SUP-3, controlled `SIGTERM` during a real encode and during verification, section 7. Run `sudo systemctl stop mediaflow.service` in each phase; confirm the ffmpeg child is gone, the original is intact, no partial file is published, the encode recovers at `PLANNED` without increasing `restarts`, and verification remains unverified until it completes.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SUP-3
- Procedure: Run `sudo systemctl stop mediaflow.service` in each phase; confirm the ffmpeg child is gone, the original is intact, no partial file is published, the encode recovers at `PLANNED` without increasing `restarts`, and verification remains unverified until it completes.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-034/`
- Status: unverified

### HW-035 — reordered and variable-frame-rate packet presentation on real media

- Claim: [ ] Section 5.5/SUP-1, reordered and variable-frame-rate packet presentation on real media, section 5.5. Compare `ffprobe -show_packets` with the container duration; confirm mediaflow uses the maximum `pts + duration`, not the final packet in file order, and that packet count and decoded-frame count remain distinct.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SUP-1
- Procedure: Compare `ffprobe -show_packets` with the container duration; confirm mediaflow uses the maximum `pts + duration`, not the final packet in file order, and that packet count and decoded-frame count remain distinct.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-035/`
- Status: unverified

### HW-036 — bounded verification memory on a large packet-dense file

- Claim: [ ] Section 5.5/SUP-1, bounded verification memory on a large packet-dense file, section 5.5. Verify a large VFR file while sampling peak RSS; confirm retained verifier memory does not grow in proportion to packet count and stays within the systemd `MemoryMax` limit.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SUP-1
- Procedure: [ ] Section 5.5/SUP-1, bounded verification memory on a large packet-dense file, section 5.5. Verify a large VFR file while sampling peak RSS; confirm retained verifier memory does not grow in proportion to packet count and stays within the systemd `MemoryMax` limit.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-036/`
- Status: unverified

### HW-037 — an interrupted or deferred capability probe

- Claim: [ ] Section 5.5/SUP-3, an interrupted or deferred capability probe, section 10. Interrupt `doctor` during a backend probe, for example by removing AC while it is running; confirm the result is `unknown` or `deferred`, not `no`, and that the last valid `/var/lib/mediaflow/capabilities.json` is unchanged.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SUP-3
- Procedure: Interrupt `doctor` during a backend probe, for example by removing AC while it is running; confirm the result is `unknown` or `deferred`, not `no`, and that the last valid `/var/lib/mediaflow/capabilities.json` is unchanged.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-037/`
- Status: unverified

### HW-038 — migration of the real `state.db` before enabling live writes

- Claim: [ ] Section 3.1/COMPAT-1, migration of the real `state.db` before enabling live writes, section 3.1. With the service stopped, copy `/var/lib/mediaflow/state.db` to a safe location, start the new version, confirm a pre-migration backup exists in `/var/lib/mediaflow/backup/`, run `doctor`, and verify that all job history and manifests remain visible.
- Class: required; runner: doctor-automatable
- Supports: COMPAT-1, DEPLOY-1, DEPLOY-2, DEPLOY-3
- Procedure: With the service stopped, copy `/var/lib/mediaflow/state.db` to a safe location, start the new version, confirm a pre-migration backup exists in `/var/lib/mediaflow/backup/`, run `doctor`, and verify that all job history and manifests remain visible.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-038/`
- Status: unverified

### HW-039 — live reload during a real encode

- Claim: [ ] Section 10.1/CONFIG-1, live reload during a real encode, section 10.1. Start a long encode, edit a live key such as `min_free_gb`, then a restart-required key such as `poll_seconds`; confirm the encode keeps its original policy, `status --json` shows the new generation and restart hold, and the old `poll_seconds` remains effective until restart.
- Class: required; runner: manual-by-John
- Supports: CONFIG-1, DEPLOY-1, DEPLOY-3
- Procedure: Start a long encode, edit a live key such as `min_free_gb`, then a restart-required key such as `poll_seconds`; confirm the encode keeps its original policy, `status --json` shows the new generation and restart hold, and the old `poll_seconds` remains effective until restart.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-039/`
- Status: unverified

### HW-040 — TMDb/notify secret rotation on the installed service

- Claim: [ ] Section 10.1/CONFIG-1, TMDb/notify secret rotation on the installed service, section 10.1. Replace `tmdb.env` and `notify.env`, reload the daemon, and confirm no secret value appears in status, journal or reload events and that a restart is reported until performed.
- Class: required; runner: manual-by-John
- Supports: CONFIG-1, DEPLOY-1, DEPLOY-3
- Procedure: [ ] Section 10.1/CONFIG-1, TMDb/notify secret rotation on the installed service, section 10.1. Replace `tmdb.env` and `notify.env`, reload the daemon, and confirm no secret value appears in status, journal or reload events and that a restart is reported until performed.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-040/`
- Status: unverified

### HW-041 — restore and re-drop a real title

- Claim: [ ] Section 3.1/STATE-1, restore and re-drop a real title, section 3.1. Complete a job, run `mediaflow restore <id>`, drop the same folder under the same preset again, and confirm a new job id is created while the RESTORED history stays visible.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, STATE-1
- Procedure: [ ] Section 3.1/STATE-1, restore and re-drop a real title, section 3.1. Complete a job, run `mediaflow restore <id>`, drop the same folder under the same preset again, and confirm a new job id is created while the RESTORED history stays visible.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-041/`
- Status: unverified

### HW-042 — staged publish on the real shared filesystem

- Claim: [ ] Section 7/SAF-3, staged publish on the real shared filesystem, section 7. Complete one encode and confirm the verified output is moved into the work-staged folder, the hidden marker is written last, and only one no-replace rename exposes it in `movies/`; interrupt before the rename and confirm no partial folder appears in the library.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-3
- Procedure: [ ] Section 7/SAF-3, staged publish on the real shared filesystem, section 7. Complete one encode and confirm the verified output is moved into the work-staged folder, the hidden marker is written last, and only one no-replace rename exposes it in `movies/`; interrupt before the rename and confirm no partial folder appears in the library.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-042/`
- Status: unverified

### HW-043 — thermal input reload on the installed service

- Claim: [ ] Section 10.1/CONFIG-1, thermal input reload on the installed service, section 10.1. Start an encode, change `temp_sensor` to `none`, reload, and confirm a restart-required hold is shown and the running encode keeps its original thermal sensor until restart.
- Class: required; runner: manual-by-John
- Supports: CONFIG-1, DEPLOY-1, DEPLOY-3
- Procedure: Start an encode, change `temp_sensor` to `none`, reload, and confirm a restart-required hold is shown and the running encode keeps its original thermal sensor until restart.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-043/`
- Status: unverified

### HW-044 — publish disk boundary with real sidecars

- Claim: [ ] Section 7/SAF-8, publish disk boundary with real sidecars, section 7. Construct a source near the free-space threshold with large subtitle sidecars and confirm the publish gate reserves source envelope plus sidecar bytes, with no output-copy reservation.
- Class: required; runner: manual-by-John
- Supports: DEPLOY-1, SAF-8
- Procedure: [ ] Section 7/SAF-8, publish disk boundary with real sidecars, section 7. Construct a source near the free-space threshold with large subtitle sidecars and confirm the publish gate reserves source envelope plus sidecar bytes, with no output-copy reservation.
- Expected: Observed behaviour matches the claim; revision, timestamp and doctor JSON are captured in the evidence bundle.
- Failure: Do not advance real-library enablement for the affected capability. Record the failure with evidence and follow the runbook guidance for this item.
- Risk: Low with a disposable clip in a sandbox root; no real media may be used. Power and thermal drills must be stopped by John if the machine becomes unstable.
- Disposable media: true
- Evidence: `$EVIDENCE/HW-044/`
- Status: unverified

