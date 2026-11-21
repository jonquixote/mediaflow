# Verification evidence

This file records what has actually been run, where, and with which result.
It is evidence, not a promise about john-server. On-hardware items still open
are tracked as unchecked lines in `docs/QUESTIONS.md`; `doctor` warns while any
remain.

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
