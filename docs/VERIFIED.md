# Verification evidence

This file records what has actually been run, where, and with which result.
It is evidence, not a promise about john-server. On-hardware items still open
are tracked as unchecked lines in `docs/QUESTIONS.md`; `doctor` warns while any
remain.

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

Result: `696 passed in 20.83s`. `scripts/check.sh` first runs
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

These used tricked sample files under `/tmp/opencode/` and a sandbox media root:

- H.264 MKV through `keep-remux-mp4`: stream copy, `hvc1` not applicable,
  `+faststart`, atomic publish, manifest and marker present, original retired
  to trash.
- HEVC MKV through `1080p-balanced-mp4` with the software encoder: encode,
  full decode verification, `hvc1`, publish and retire.
- HEVC MPEG-TS through a remux-to-MP4 preset: valid `hvc1`/`hvcC`, full decode
  verification, publish.

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
