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
- [ ] Section 5.4, speed-table estimates within 30% of real encode time on the sample clips, section 12. Compare `estimate` before and real encode wall time after; record both.
- [ ] Section 5.4, the experimental x265 preset estimate versus the overnight window, section 5.1. Leave it disabled; if testing, calibrate first, copy the preset into `presets.d`, set `enabled=true`, create its convert folder, and confirm the schedule gate refuses jobs longer than the window.

## Environment-specific checks

- [ ] Section 7/SAF-8, if an external media drive is added later, every pipeline path and `movies/` still share one filesystem and the systemd `ReadWritePaths`/mount paths are updated, section 7. Run `doctor`; it must fail if `st_dev` differs.
- [ ] Section 7/SAF-9, Jellyfin-transcoding detection with the real `jellyfin` user and a real transcode, section 7. Start a transcode and confirm mediaflow refuses to start/queues a job.
- [ ] Section 8, TMDb reachability and one real title lookup, section 8. Install a token, run one dry-run plan, confirm cached response reuse and that the token never appears in process output or logs.
- [ ] Section 9.1/SAF-10, optional notification delivery and payload redaction with the real notify service, section 9.1. Trigger a REVIEW and confirm the payload contains only counts/job ids; verify cooldown.
