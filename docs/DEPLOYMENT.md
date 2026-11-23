# Deployment, upgrade and rollback (v1.0)

This document is authoritative for **installer and upgrade procedure**. The
runbook is authoritative for daily operation, `docs/requirements.toml` for
normative IDs, `docs/PLAN_SCHEMA.md` for persisted plans, and
`docs/hardware_checks.toml` for on-box verification status. Nothing here has
been executed on john-server by the agent.

## Safety

John runs every command below. The agent never runs `install.sh`, `sudo`,
`systemctl`, or any command against the real media or state roots. All rehearsal
tests use `MEDIAFLOW_INSTALL_TEST=1` and fake roots under `/tmp`.

## Fresh install

1. Record the revision: `git -C /srv/agent-work/mediaflow rev-parse HEAD`.
2. Review `install.sh`, `systemd/*.service`, `config/config.toml.example` and
   this document.
3. Run `sudo ./install.sh` from the repository. It is idempotent: existing
   `config.toml`, `tmdb.env`, `notify.env`, presets, state and media are never
   overwritten. It creates users/groups, directories, code, venv, units and
   convert folders; it does not create the experimental preset folder.
4. Run `sudo systemctl daemon-reload` only if step 3 did not (it does).
5. Run `sudo systemctl start mediaflow.service` and immediately
   `mediaflow --config /etc/mediaflow/config.toml doctor --json`.
6. Keep `dry_run = true` until every **required** registry item is verified.

## Upgrade over an older release

1. Stop writes: `sudo systemctl stop mediaflow.service`.
2. Record revisions and doctor output into an evidence bundle (see
   `docs/FIRST_RUN.md`).
3. Back up the live database with `mediaflow maintenance` or copy
   `/var/lib/mediaflow/state.db` while stopped. Keep at least three backups in
   `/var/lib/mediaflow/backup/`.
4. Copy the new release over `/opt/mediaflow` using `install.sh`. It preserves
   config, secrets, presets and state.
5. Start the daemon. On first `Store.initialize()` the ordered, transactional
   migration runs. The pre-migration backup is recorded in `meta` as
   `last_migration_backup`. A newer database is refused without modification.
6. Run `doctor --json`. Confirm `db-schema` is `ok`, `last-migration` names the
   backup, and no restart-required hold remains after the restart.
7. Only then resume real-library processing.

## Rollback

1. Stop the daemon.
2. Restore the previous code revision in `/opt/mediaflow` or reinstall the
   previous release.
3. If the schema must be rolled back, stop all mediaflow units, preserve the
   current `state.db` as `state.db.corrupt-<timestamp>`, copy the newest
   pre-upgrade backup from `/var/lib/mediaflow/backup/` to
   `/var/lib/mediaflow/state.db`, then start the old release. Job history is
   never discarded; the preserved file remains for inspection.
4. Run `doctor` and inspect a small dry-run plan before re-enabling writes.

## Systemd review (audited, not executed)

`systemd/mediaflow.service` uses `KillMode=mixed` and `TimeoutStopSec=120`.
Pass B shutdown terminates and reaps the child within the 5-second supervision
grace, so 120 seconds is sufficient; a stall timeout is not part of shutdown.
`Restart=on-failure` plus `RestartSec=15` can restart the daemon after a crash,
but global holds and the job restart budget are persisted and re-applied on
recovery, so a restart does not silently reset safety state.

`SupplementaryGroups=render video` and
`DeviceAllow=/dev/dri/renderD128 rw` are required for hardware encode and are
feature-conditional until John verifies them (HW-023, HW-024). `ReadWritePaths`
must include `/srv/media`, `/var/lib/mediaflow` and `/run/mediaflow`; any
network or additional media mount must be added by John and re-checked with
doctor (HW-038). The units never invoke `systemctl` from the agent.

Doctor reads the unit text and reports missing hardening keys without invoking
systemd. On the installed machine it reads `/etc/systemd/system/mediaflow.service`;
in a sandbox it reads the repository copy and labels the check informational.

## Log and state growth

Logs go to journald (no file log rotation by mediaflow). `status.json` and
`progress.json` are atomically replaced, not appended. The database WAL is
checkpointed by SQLite; the nightly maintenance unit keeps three backup
generations. Verify disk growth on the box (HW-006, HW-038).
