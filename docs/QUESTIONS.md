# Open questions and on-hardware VERIFY items

Anything still unchecked here is an assumption about the real machine or a
tool that the sandbox cannot settle. `mediaflow doctor` warns while this file
contains unchecked lines. Do not set `dry_run = false` on a fresh install until
the list is clear.

Format: `- [ ] section, what, how john can test`.

## Hardware encode and rate control

- [ ] Section 5.5, real QSV HEVC 8-bit encode and the actual rate-control mode (ICQ vs CQP vs hybrid) on the Kaby Lake GPU; the sandbox agent cannot open `/dev/dri/renderD128`, section 7. Run `sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor` after install and paste the QSV/rate-control result into `docs/VERIFIED.md`.
- [ ] Section 5.5, real VAAPI HEVC 8-bit fallback and its rate-control mode, section 7. Run the same `doctor`; if VAAPI works while QSV fails, verify the planner chooses VAAPI and logs it.
- [ ] Section 5.5, Kaby Lake HEVC 10-bit hardware encode: disputed in public sources and deliberately never assumed, section 7. Run `doctor`; confirm the separate 10-bit result and exercise a 10-bit source through `keep-remux-mkv` and, if supported, `1080p-balanced-mp4`.
- [ ] Section 5.5, whether ffmpeg debug output exposes the engaged rate-control mode as expected; if it does not, inspect `ffmpeg -v debug` for your installed `jellyfin-ffmpeg` and record the stable detector output in `docs/VERIFIED.md`.
- [ ] Section 5.5, software-decode + hardware-upload fallback for source codecs without a hardware decoder. Run one real non-QSV-decodable source through an encode preset and confirm the log says software decode + upload rather than failing.

## Machine protection and systemd

- [ ] Section 7/SAF-9, `IOSchedulingClass=idle` effect under the I/O scheduler in use (check `/sys/block/*/queue/scheduler`), section 7. Run a real encode and inspect `ionice`/latency; record the scheduler and result.
- [ ] Section 7/SAF-9, `MemoryHigh=2G`/`MemoryMax=3G` versus peak RSS for software decode plus HEVC encode and for a short x265 `slow` sample, section 7. Use `/usr/bin/time -v` or systemd accounting; adjust the unit if needed.
- [ ] Section 7/SAF-9, `CPUQuota=300%` on this 2-core/4-thread CPU, section 7. Start a real encode with the unit and confirm the effective quota and temperature behaviour.
- [ ] Section 7/SAF-9, `DeviceAllow=/dev/dri/renderD128 rw` and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` do not break QSV/VAAPI or TMDb HTTPS, section 7. Run `doctor` and one real encode inside the installed unit; if broken, record the minimal change and update the unit.
- [ ] Section 5.5/SAF-9, SIGSTOP/SIGCONT of a real QSV/VAAPI encoder with an open GPU context finishes with a valid file, section 7. Start a real encode, `kill -STOP` then `kill -CONT` the ffmpeg pid, and verify the final file with `ffprobe` and the decode test. If it hangs or corrupts, set `pause_method = "idle_after_job"` and repeat thermal/AC tests.
- [ ] Section 7/SAF-9, thermal pause/resume and the 60-second sustain threshold on the real hwmon sensor/fan profile, section 7. Close the lid, run under load, and record package temperature plus every `encode-paused`/`encode-resumed` event. Re-run the 1-hour test after any fan change.
- [ ] Section 7/SAF-9, AC-loss pause and resume during a real encode, section 7. Unplug the charger mid-encode, confirm SIGSTOP/paused status, replug, and verify the output passes verification.
- [ ] Section 5.5/SAF-9, whether the fan/PROCHOT profile is adequate while the lid is closed; mediaflow never changes it. Record the active fan settings with the thermal test.

## Power loss and direct play

- [ ] Section 12, pulled-plug encode resumes with no corrupt or duplicate output on real hardware, section 7. With `dry_run=false`, pull power mid-encode, reboot, and verify final state, manifest, hashes, and no duplicate in `movies/`.
- [ ] Section 12, real phone Jellyfin direct-play of an encoded MP4 and an HEVC remux (including `hvc1`), section 8. Play both on the Jellyfin phone app and an iPhone; confirm Direct Play with no transcode.
- [ ] Section 12, Jellyfin ignores the hidden `.mediaflow-<job-id>` marker, section 7. Confirm the movie still appears once and no marker warning appears in Jellyfin logs.
- [ ] Section 8, optional `[tmdbid-N]` Jellyfin provider-ID folder syntax against current Jellyfin docs, section 8. Leave `append_tmdbid=false` until tested; if enabling, verify provider matching for one title.

## Calibration, memory and scheduling

- [ ] Section 5.4, `calibrate` on one animation, one dark live-action and one high-grain film, section 5.4. Run with 90 seconds, record size/time/VMAF or SSIM, and write the chosen quality values into the preset files.
- [ ] Section 5.4/SAF-14, `calibrate` on the real machine reports the same backend the plan would select (`doctor` backend, not a silent software fallback), section 5.4. Run `mediaflow calibrate <clip> --preset 1080p-balanced-mp4 --json` and check that `backend`/`encoder` match the hardware the planner picks.
- [ ] Section 5.4/SAF-14, the calibration quality score is meaningful on the real machine: the sandbox ffmpeg build has no `libvmaf`, so only SSIM was exercised, section 5.4. Run the same clip with an ffmpeg build that has `libvmaf` and record both numbers next to the chosen quality.
- [ ] Section 5.4/SAF-14, the speed-table seed is found again by `estimate` after a calibration, section 12. Calibrate, then run `mediaflow estimate <path> --preset <id>` and confirm the factor matches the seeded one.
- [ ] Section 5.4, speed-table estimates within 30% of real encode time on the sample clips, section 12. Compare `estimate` before and real encode wall time after; record both.
- [ ] Section 5.4, the experimental x265 preset estimate versus the overnight window, section 5.1. Leave it disabled; if testing, calibrate first, copy the preset into `presets.d`, set `enabled=true`, create its convert folder, and confirm the schedule gate refuses jobs longer than the window.

## Colour metadata, codecs and the quality metric

- [ ] Section 7/SAF-2, the QSV and VAAPI encode paths carry the source's stated colour description (primaries, transfer, matrix, range) into the output; the sandbox could only verify the software path, section 7. Encode one tagged bt709 and one tagged bt2020/smpte2084 clip with `1080p-balanced-mp4` and check `ffprobe -show_streams` for `color_primaries`, `color_transfer`, `color_space` and `color_range`.
- [ ] Section 7/SAF-2, an MP4 output written by the hardware encoders contains a complete `colr` box: the MP4 `nclx` entry needs primaries, transfer and matrix, and a range-only description cannot be carried, section 7. Inspect the box of a hardware-encoded MP4 (`ffprobe -show_streams` plus a hex look for `colr`) and record which fields survive.
- [ ] Section 5.5/SAF-2, the planner-selected backend and the engine speed-table key now use the same backend and canonical target-height bucket in the sandbox, section 5.5. On the real machine, compare the backend in `mediaflow plan --json` with the backend in `mediaflow estimate --json` for the same source, including a scaled source, and confirm they agree with the backend that actually ran.
- [ ] Section 7/SAF-2, the bits-per-pixel floor default (`min_bpp = 0.01` bits per pixel per frame, about 498 kbps at 1080p24) is a placeholder and is not empirically calibrated; the metric changed units in this pass, section 7. Run the calibration on the three sample clips, record the measured bits per pixel per frame, and set the default from that evidence.
- [ ] Section 7/SAF-2, the presented-frame-count basis for variable-frame-rate sources (container count, then decoded count, then a labelled estimate), section 7. Take a VFR sample, compare `ffprobe -count_frames` with the container count, and record which basis the verifier used.
- [ ] Section 7/SAF-2, the MP4 muxer marks the first track of a type as default even when the plan says otherwise, while MKV keeps the disposition exactly; the plan always marks the first selected audio track as default, section 7. Confirm on the real machine that a multi-track source keeps its intended default track after an MP4 encode.

## Environment-specific checks

- [ ] Section 7/SAF-8, if an external media drive is added later, every pipeline path and `movies/` still share one filesystem and the systemd `ReadWritePaths`/mount paths are updated, section 7. Run `doctor`; it must fail if `st_dev` differs.
- [ ] Section 7/SAF-9, Jellyfin-transcoding detection with the real `jellyfin` user and a real transcode, section 7. Start a transcode and confirm mediaflow refuses to start/queues a job.
- [ ] Section 8, TMDb reachability and one real title lookup, section 8. Install a token, run one dry-run plan, confirm cached response reuse and that the token never appears in process output or logs.
- [ ] Section 9.1/SAF-10, optional notification delivery and payload redaction with the real notify service, section 9.1. Trigger a REVIEW and confirm the payload contains only counts/job ids; verify cooldown.

## Shared execution and controlled recovery

- [ ] Section 5.5/SUP-3, controlled `SIGTERM` during a real encode and during verification, section 7. Run `sudo systemctl stop mediaflow.service` in each phase; confirm the ffmpeg child is gone, the original is intact, no partial file is published, the encode recovers at `PLANNED` without increasing `restarts`, and verification remains unverified until it completes.
- [ ] Section 5.5/SUP-1, reordered and variable-frame-rate packet presentation on real media, section 5.5. Compare `ffprobe -show_packets` with the container duration; confirm mediaflow uses the maximum `pts + duration`, not the final packet in file order, and that packet count and decoded-frame count remain distinct.
- [ ] Section 5.5/SUP-1, bounded verification memory on a large packet-dense file, section 5.5. Verify a large VFR file while sampling peak RSS; confirm retained verifier memory does not grow in proportion to packet count and stays within the systemd `MemoryMax` limit.
- [ ] Section 5.5/SUP-3, an interrupted or deferred capability probe, section 10. Interrupt `doctor` during a backend probe, for example by removing AC while it is running; confirm the result is `unknown` or `deferred`, not `no`, and that the last valid `/var/lib/mediaflow/capabilities.json` is unchanged.

## State, configuration and upgrade coherence

- [ ] Section 3.1/COMPAT-1, migration of the real `state.db` before enabling live writes, section 3.1. With the service stopped, copy `/var/lib/mediaflow/state.db` to a safe location, start the new version, confirm a pre-migration backup exists in `/var/lib/mediaflow/backup/`, run `doctor`, and verify that all job history and manifests remain visible.
- [ ] Section 10.1/CONFIG-1, live reload during a real encode, section 10.1. Start a long encode, edit a live key such as `min_free_gb`, then a restart-required key such as `poll_seconds`; confirm the encode keeps its original policy, `status --json` shows the new generation and restart hold, and the old `poll_seconds` remains effective until restart.
- [ ] Section 10.1/CONFIG-1, TMDb/notify secret rotation on the installed service, section 10.1. Replace `tmdb.env` and `notify.env`, reload the daemon, and confirm no secret value appears in status, journal or reload events and that a restart is reported until performed.
- [ ] Section 3.1/STATE-1, restore and re-drop a real title, section 3.1. Complete a job, run `mediaflow restore <id>`, drop the same folder under the same preset again, and confirm a new job id is created while the RESTORED history stays visible.
- [ ] Section 7/SAF-3, staged publish on the real shared filesystem, section 7. Complete one encode and confirm the verified output is moved into the work-staged folder, the hidden marker is written last, and only one no-replace rename exposes it in `movies/`; interrupt before the rename and confirm no partial folder appears in the library.
- [ ] Section 10.1/CONFIG-1, thermal input reload on the installed service, section 10.1. Start an encode, change `temp_sensor` to `none`, reload, and confirm a restart-required hold is shown and the running encode keeps its original thermal sensor until restart.
- [ ] Section 7/SAF-8, publish disk boundary with real sidecars, section 7. Construct a source near the free-space threshold with large subtitle sidecars and confirm the publish gate reserves source envelope plus sidecar bytes, with no output-copy reservation.
