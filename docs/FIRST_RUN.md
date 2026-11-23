# First-run kit for john-server

Ordered, copy-pasteable drills for the first controlled real-machine run. The
agent has run **none** of these. Every on-box step names the registry item it
updates. `docs/hardware_checks.toml` is the authority for status; the generated
`docs/HARDWARE_CHECKS.md` and `docs/FIRST_RUN_CHECKLIST.md` must not be edited by
hand.

Set a bundle root outside all media roots:

```sh
export EVIDENCE=/var/lib/mediaflow/evidence/$(date -u +%Y%m%dT%H%M%S)
mkdir -p "$EVIDENCE"
export MF=/opt/mediaflow/venv/bin/mediaflow
export CFG=/etc/mediaflow/config.toml
```

The evidence helper records revision, UTC timestamp, redacted command output
and exit code; it never records tokens, URLs with credentials or likely media
titles. A bundle is accepted only when the command exited zero and its outputs
are complete and unredacted.

## 0. Preflight — HW-001 to HW-044 as applicable

```sh
git -C /srv/agent-work/mediaflow rev-parse HEAD | tee "$EVIDENCE/revision.txt"
sudo -u mediaflow -g media "$MF" --config "$CFG" doctor --json > "$EVIDENCE/doctor-before.json"
sudo -u mediaflow -g media "$MF" --config "$CFG" doctor; echo "exit=$?"
```

Success: exit `0`; required checks pass, feature-conditional failures appear
only for disabled features, unknowns are listed honestly. Exit `2` is a
required failure, `3` an enabled-feature failure, `4` a required unknown. Do
not continue past a required failure. Record the exact output under the
registry item you are addressing.

## 1. Device and capability checks — HW-001 to HW-005, HW-020, HW-021

Run doctor as the service user, then capture one item at a time:

```sh
scripts/capture_evidence.sh "$EVIDENCE" HW-001 -- \
  sudo -u mediaflow -g media "$MF" --config "$CFG" doctor --json
scripts/verify_hardware_item.sh "$EVIDENCE/HW-001"
```

Check render-node access with a read-only `stat` as the service user; do not
change permissions. Interpret `unknown` from a deferred or interrupted probe
as unknown, never as unsupported (HW-021).

## 2. Synthetic dry-run rehearsal — HW-001 to HW-005, HW-019, HW-029 to HW-032

Generate disposable clips in a sandbox root, never a real library:

```sh
scripts/make_test_clips.sh /tmp/mediaflow-first-run/clips
sudo -u mediaflow -g media "$MF" --config "$CFG" \
  plan /tmp/mediaflow-first-run/clips/sdr-h264.mkv --json
```

Success: the plan records the planner-selected backend, explicit stream maps,
target geometry and no titles in status/notification payloads. Keep `dry_run =
true`. Use the VFR and 10-bit clips for HW-030 and HW-003 if available.

## 3. Single real encode in a scratch library — HW-007, HW-008, HW-010, HW-011, HW-033 to HW-040

Use a scratch root, one disposable clip and `dry_run=false` only for that job.
Inspect the published output with `ffprobe`, confirm `hvc1`, planned audio and
subtitle streams, naming, sidecars and no overwrite. Verify the staged publish
and marker order (HW-034); interrupt before the rename on a second disposable
clip and confirm no partial folder appears in `movies/`.

## 4. Jellyfin direct-play — HW-009, HW-012, HW-013

Play the published synthetic clips on the real client devices. Record which
client played Direct Play and which transcoded. Do not use real media.

## 5. Interruption drills — HW-015, HW-022, HW-031, HW-035, HW-036

Run each drill on a disposable clip and capture evidence per item:

- `sudo systemctl stop mediaflow.service` during encode and verification (HW-022).
- Protection pause/resume without touching system configuration (HW-015).
- `kill -9` of the daemon during encode and during publish (HW-035, HW-036).
- After each kill, restart, run recovery, then run recovery a second time and
  confirm no new recovery events and no duplicate output.

Do not pull power unless John explicitly accepts the machine risk (HW-014).

## 6. State drills — HW-037, HW-038, HW-041 to HW-044

Back up and restore a copy of `state.db` while stopped. Rehearse a config
reload with a live key and a restart-required key, then restart and confirm the
hold clears. Run a schema migration on a **copy** of the database, never the
live file. Confirm restore/re-drop creates a fresh job (HW-044).

## 7. Calibration and thresholds — HW-016 to HW-019, HW-039

Run calibration on the synthetic clips under supervision. Read size, time,
backend and score; leave `quality`, `min_bpp` and speed values unchanged until
results are reviewed. A missing VMAF score is `n/a`, not a pass (HW-018).

## 8. Gradual enablement

Only after every **required** item is verified: enable one preset path, drop
one small real title, review the plan and output, then expand. Any regression
returns to the previous section and the rollback procedure in
`docs/DEPLOYMENT.md`.

## Updating status

```sh
scripts/verify_hardware_item.sh "$EVIDENCE/HW-001"
.venv/bin/python tools/gen_hardware_checks.py --check
```

The updater refuses incomplete, failed or unredacted bundles. The agent never
sets `verified-on-hardware`; only John's recorded evidence does.
