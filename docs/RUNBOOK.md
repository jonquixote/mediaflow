# mediaflow runbook

This is the human operator's guide. The normative requirements are in
`docs/SPEC.md`; the safety subset is generated into `docs/INVARIANTS.md`.

## 1. What mediaflow does

A movie is placed under `_pipeline/convert/movies/<preset>/`. The daemon
stabilises it, probes and plans it, encodes or remuxes it, verifies it, resolves
the title and year, atomically publishes `Title (Year)/Title (Year).<ext>` into
`movies/`, and moves the original into `trash/`. Jellyfin reads only `movies/`.

Nothing in `movies/` is ever edited in place or deleted. Publish is one
`rename()`; a duplicate destination always becomes a REVIEW item.

## 2. First install

Review `install.sh` before running it. It must be run by john as root; the
coding agent never runs it and never has sudo.

```sh
sudo ./install.sh
sudo systemctl start mediaflow.service
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor
sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml status
```

Fresh installs use `dry_run = true`. Leave it on until:

- `doctor` has no failures;
- every unchecked item in `docs/QUESTIONS.md` has been tested and ticked;
- you have inspected several real plans with `mediaflow plan <path>`.

Enabling real movement is one config edit and a daemon reload:

```toml
dry_run = false
```

The daemon reloads config on mtime change and logs the flip. A job that was
planned in dry-run is re-planned once and then proceeds.

## 3. Secrets

Only these files hold secrets, both `root:mediaflow 0640`:

- `/etc/mediaflow/tmdb.env` → `TMDB_READ_TOKEN=...`
- `/etc/mediaflow/notify.env` → `NOTIFY_URL=...`

They are never placed in `config.toml`, command output, logs, or notifications.
TMDb is optional: without a token, mediaflow uses parse-only naming and REVIEWs
anything unclear. Notifications are optional; `status.json`, `progress.json`
and `status --short` always work.

If using public ntfy.sh, use a long random topic and treat the URL as a secret.
Payloads contain counts and job ids only, never titles.

## 4. Daily operation

1. Download to `_pipeline/inbox/`.
2. Move the movie folder into the folder for the desired preset:
   `_pipeline/convert/movies/keep-remux-mp4/`, `1080p-balanced-mp4/`, etc.
   That move is the trigger. Do not put files directly in `work/`.
3. Watch `mediaflow status --short` or `queue --short`.
4. If a review appears, `mediaflow review list` prints the reason, age and the
   command that clears it.

Phone-friendly commands:

```sh
mediaflow status --short
mediaflow queue --short
mediaflow review list --short
mediaflow review accept <id> --candidate 1
mediaflow review move <id> --preset keep-remux-mkv
mediaflow review reject <id>
mediaflow estimate <path> --short
```

All commands also accept `--json`. `doctor` exits non-zero on failures.

## 5. Presets

| Preset | What it does |
|---|---|
| `keep-remux-mp4` | Copy H.264/HEVC video into MP4, transcode audio only where MP4 requires it. |
| `keep-remux-mkv` | Copy any video, audio, subtitles and attachments into MKV. The home for HDR/10-bit/exotic sources. |
| `1080p-balanced-mp4` | Hardware HEVC, 1080p cap, everyday default. |
| `1080p-small-mp4` | Hardware HEVC, higher quantiser, smaller files. |
| `720p-small-mp4` | Hardware HEVC, 720p cap. |
| `1080p-best-x265-mp4` | DISABLED experimental software x265. Do not use without calibration and an overnight window. |

Presets are data files in `/etc/mediaflow/presets.d`. The folder name must
equal the preset id. Unknown keys and invalid combinations fail at load time;
the previous good config stays active and `doctor` explains the problem. The
experimental preset file lives outside `presets.d` and `install.sh` does not
create its convert folder.

Quality numbers are placeholders until you run:

```sh
mediaflow calibrate /path/to/real/clip.mkv --seconds 90
```

The table always has quality, size, time and a score (VMAF or SSIM, otherwise
`n/a`). Choose values and edit the preset files. Real encodes also refine the
speed table.

`calibrate` measures the *production* operation: it builds the same plan an
encode would use and runs the same command builder and encoder over the
requested sample interval, so the number describes the real job. Read the
report before trusting it:

- The header names the preset, the encoder, the target geometry and the
  scope. The scope is `video-encode-only`: audio and subtitle handling are
  not part of the measurement.
- `status: refused` means nothing was measured - usually no hardware encoder
  is available for that preset. The reason line says which. Nothing is
  seeded into the speed table in that case.
- `status: not-applicable` means the preset does not measure anything (a
  remux preset).
- A row with `n/a` in the score column still has a valid size and time. The
  reason (`scoring failed`, `frame count mismatch`, ...) is printed under the
  row. A missing score never blocks the size/time measurement.
- Only a valid trial seeds the speed table, under the backend that actually
  ran. Trial files are written under the configured work area
  (`_pipeline/work/calibration/`) and removed when the run ends.

## 6. MP4 and MKV in the same library

`movies/` can contain both containers; Jellyfin handles this. An MP4 cannot
hold PGS/VobSub/DVB bitmap subtitles. With the default `bitmap = "mkv_fallback"`,
the affected title is published as MKV and the log/manifest says
`container: mkv (bitmap subs)`. PGS is cheap to store and serve, but a client
that cannot render PGS will ask Jellyfin to burn it in, which is expensive on
this CPU.

Options:

- `mkv_policy = "bitmap_only"` (default): MKV only when bitmap subtitles force it.
- `"any_subs"`: MKV whenever any subtitle is kept; most consistent library.
- `"never"`: never MKV; bitmap subtitles are dropped and recorded in the manifest.

`mediaflow library report` shows the container/codec/resolution mix.

## 7. Safety and recovery

- The original is never deleted at publish; it is moved to
  `trash/<date>/<job-id>/` with `manifest.json`.
- Retention defaults to 14 days. The daily timer runs DB backup, failed-item
  retention and trash purge. `trash purge` purges only expired items; nothing
  purges early automatically.
- A failed job's original is kept in `failed/<job-id>/`. A rejected review's
  original stays in `review/`. These move to trash after
  `failed_retention_days` (default 30).
- `mediaflow restore <job-id>` checks that the original still exists, verifies
  the published file against the manifest hash, records an intent, moves the
  published folder into trash, and moves the original back to `inbox/`. If the
  original was purged, it exits 3 with `original already purged; nothing to restore`.
- A power cut during encoding resumes from the beginning; the partial output
  is removed. A cut in the publish or retire window is adopted using the hidden
  marker and trash layout, not duplicated.
- If `state.db` fails its integrity check, the daemon refuses to start.
  Restore the newest file from `/var/lib/mediaflow/backup/` while the service
  is stopped, then run `doctor`. Do not delete the corrupt file until a copy is
  preserved for inspection.

Never move files from `movies/` by hand while the daemon is running. Use
`restore` or a REVIEW command so the database and marker stay consistent.

### Operation outcomes and recovery

| Outcome | Operator meaning |
|---|---|
| `succeeded` | The uninterrupted operation completed; publication or seeding may continue. |
| `failed` | Process or content failure; nothing is published and the original remains preserved. |
| `cancelled` | User cancellation; partial output is discarded and the job becomes cancelled/FAILED. |
| `stalled` | No real work progress; an encode consumes one restart. |
| `timed-out` | Overall deadline reached; this is distinct from a content failure. |
| `interrupted` | Controlled protection or daemon shutdown; not an unexplained encoder crash. |
| `deferred` | The exclusive slot was busy or a bounded probe was protection-deferred; retry later. |
| `paused` | Nonterminal intentional pause; the stall clock is suspended. |

Recovery follows the outcome, not just the state:

- **Daemon shutdown during encoding:** the owned child is stopped and reaped,
  the partial is removed, and the job is recoverable at `PLANNED` without
  increasing `restarts`.
- **Protection interruption during encoding:** same controlled recovery as
  daemon shutdown, with the reason recorded in the job events.
- **Encode stall, encode timeout, encoder failure, or genuine interruption:**
  one restart is consumed and the job returns to `PROBED`.
- **Controlled interruption during verification:** the job remains `ENCODED`,
  receives no output hash, and cannot advance to `VERIFIED`; verification is
  retried.
- **Verification stall or timeout:** the job fails and the original is preserved.
- **Busy processing slot:** `processing-slot-busy` is a deferral, not evidence
  that hardware is unsupported.
- **Non-cooperative legacy worker:** the exclusive slot and job work directory
  remain held until that worker exits. Do not manually remove its work while
  it is alive.

## 8. Heat, power and schedules

The service is AC-only by default. It will not start while Jellyfin is
transcoding, on battery, or when the package is already hot. It pauses when AC
drops, disk space gets low, or the package reaches the thermal threshold for
60 seconds. It signals ffmpeg with SIGSTOP/SIGCONT (default) or, if configured,
finishes the current user-paused job then idles
(`pause_method = "idle_after_job"`). That setting defers only a deliberate
user pause: AC loss, thermal conditions, low space, Jellyfin transcoding and
an ffmpeg-change hold still interrupt the current expensive operation. The
mid-encode space check reserves net remaining work plus an additive emergency
reserve; it never subtracts already-written bytes from that net value a second
time.

The fan curve is controlled outside mediaflow. mediaflow never writes fan or
PROCHOT settings. Record the active fan profile whenever you run the one-hour
thermal test.

A `[schedule].window` such as `"01:00-07:00"` is a **start gate**: a job starts
only if its estimate fits before the window closes. Once started it may finish.
`run --force <id>` is the only way to override the cold-start/estimate gates.
It does not override AC, temperature, viewer, or space safety gates.

## 9. Timers and backups

- `mediaflow.service`: daemon, one encode at a time.
- `mediaflow-trash.timer`: daily at 04:45 (with jitter): DB `.backup` keeping
  three copies, failed/rejected retention sweep, trash purge. Enable only after
  reviewing retention.
- `mediaflow-doctor.timer`: weekly report. Enable if you want it in the journal.

To inspect without changing anything:

```sh
mediaflow trash list --short
mediaflow library report --short
mediaflow status --json | jq .
cat /var/lib/mediaflow/status.json
cat /var/lib/mediaflow/progress.json
```

## 10. Updating jellyfin-ffmpeg

An apt upgrade can silently change encoder behaviour. On daemon start and when
the ffmpeg version changes, mediaflow pauses the queue, re-runs capability
tests, and resumes only if 8-bit hardware encode still works. 10-bit results are
recorded separately. If the queue stays paused with `ffmpeg-changed`, read the
journal and run `doctor`; fix the capability problem or pin/downgrade ffmpeg.

Capability probes are tri-state. A completed probe may establish yes or no. A
timeout, deferral, cancellation, or failure to start the probe is unknown, not
unsupported. An unknown or incomplete report does not overwrite the last valid
`/var/lib/mediaflow/capabilities.json`. During an ffmpeg version change, the
queue remains paused until a complete capability run succeeds.

## 11. On-hardware acceptance checklist

Do these on john-server, with the sandbox root for tests and no other media
workloads:

- [ ] `doctor` saved to `docs/VERIFIED.md`; QSV/VAAPI 8-bit and 10-bit, rate
      control, temperature sensor.
- [ ] `calibrate` on animation, dark live-action and high-grain clips.
- [ ] One real encode with `dry_run=false`, direct-play on the Jellyfin phone
      app and an iPhone.
- [ ] One HEVC remux, direct-play on Apple devices (`hvc1`).
- [ ] One hour with the lid closed; record temperature and fan profile.
- [ ] Pull the power during an encode; confirm clean resume and no duplicate.
- [ ] SIGSTOP/SIGCONT a running QSV encode; if it fails, set
      `pause_method = "idle_after_job"` and repeat thermal/AC tests.
- [ ] Unplug the charger mid-encode; confirm pause and resume.
- [ ] Measure peak memory for software decode + HEVC encode and for a short
      x265 slow sample; adjust `MemoryHigh`/`MemoryMax`.
- [ ] Confirm Jellyfin ignores `.mediaflow-<job-id>`.
- [ ] Tick every item in `docs/QUESTIONS.md` and re-run `doctor`.

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| Queue paused | `status --json` → `paused_reasons`; `doctor`; journal |
| Review count growing | `review list`, run the printed command |
| Job stays DETECTED | temp markers still present or file still growing; wait for `stable_seconds` |
| `estimate-exceeds-window` | no speed entry or estimate cannot fit; `run --force <id>` only after deciding it is safe |
| `10bit-no-hw` | route to `keep-remux-mkv`, or explicitly choose `ten_bit_source="to8"` (lossy decision) |
| `hdr` | `review move <id> --preset keep-remux-mkv` |
| duplicate | compare sizes/durations; `review accept <id> --replace` moves the old library copy to trash |
| `ffmpeg-changed` | rerun `doctor`; capability tests must pass |
| No notification | `notify.env` missing, cooldown, or service unreachable; check journal (URL is redacted) |
