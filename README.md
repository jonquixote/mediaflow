# mediaflow

Media optimisation and organisation funnel for John's Jellyfin server.

A file placed in `_pipeline/convert/movies/<preset>/` is stabilised, probed,
planned, encoded or remuxed, verified, named, atomically published and the
original retired to trash. The daemon is a native systemd service; the CLI is
phone-friendly.

## Safety first

Read `docs/INVARIANTS.md` before changing pipeline behaviour. It is generated
from `docs/requirements.toml` by `tools/gen_invariants.py`; never edit it by
hand.

Nothing in this repository has been run against `/srv/media`. Tests use a
sandbox root and refuse `/srv/media`.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
scripts/check.sh
```

## Install

`install.sh` is for John to review and run. It is idempotent, prints commands
before running them, and never touches Jellyfin configuration, SSH, ufw,
Tailscale, or the vault. See `docs/RUNBOOK.md`.
