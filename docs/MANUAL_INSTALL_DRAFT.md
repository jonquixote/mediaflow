# Manual installation plan — DRAFT

**DRAFT — NOT APPROVED FOR EXECUTION. This document does not authorize
installation, service activation, media initialization, or host changes.**

| Field | Value |
|---|---|
| Status | Draft for review; **unapproved**; no stage may be executed |
| Starting reference | `44a3b6042ee292c79fac68a14fa1fa0ac1169b5c` (branch `pass-d-install-lock`) |
| SPEC | v1.0 (see `docs/SPEC.md`; safety subset in generated `docs/INVARIANTS.md`) |
| Installer state | `install.sh` is sandbox-only (locked); this draft does **not** unlock it |
| Every command below | **NOT AUTHORIZED** — shown for review of intent only |

## A. Scope and current state

### A.1 What this draft is and is not

- `install.sh` is a sandbox-only create-only rehearsal tool. Without
  `MEDIAFLOW_INSTALL_TEST` exactly equal to `1` it exits 3 immediately after
  the shell safety options, before directory resolution, external commands,
  service queries, account operations, destination writes, or EXIT-trap setup
  (`install.sh` lines 11–20, "unconditional production lock"). There is no
  production override.
- This draft is **not** an alternative way to unlock or run that installer. It
  proposes a separately reviewed, manually executed, stage-gated installation
  that would — if ever approved — perform the same create-only layout the
  installer rehearses, by hand, with explicit approval checkpoints.
- Nothing in this draft authorizes execution. Approval of the *plan* would be
  required first; approval of *each stage* would be required separately, at
  the checkpoints in section D.

### A.2 Evidence classes used in this document

1. **Repository-verified** — read directly from this checkout at the starting
   reference. Cited as `path:symbol` or `path:lines`. This is the strongest
   class used here.
2. **Builder-reported evidence** — results of runs the builder executed in the
   development sandbox, or claims recorded in earlier handoff documents
   (`docs/VERIFIED.md`, `docs/DEPLOYMENT_SAFETY_HANDOFF.md`) at other
   revisions. Reproducible but tied to the sandbox, not to the target host.
3. **Historical host observations** — statements about the real machine
   ("john-server", its GPU, CPU, `/srv/media`, users) that appear in repository
   documents or git history. **Not re-verified and not current verified
   state.** Treated as assumptions to confirm (section C).
4. **Unresolved questions** — open decisions that block approval.

### A.3 Repository-verified facts (material claims)

- **Lock.** `install.sh:11-22` — unconditional production lock, exit 3,
  applies to root and non-root, no override.
- **Rehearsal-only execution path.** With `MEDIAFLOW_INSTALL_TEST=1`, the
  installer requires `MEDIAFLOW_INSTALL_SCRATCH_ROOT` validated by
  `tools/scratch.py:validate_scratch` (under `/tmp`, name
  `mediaflow-rehearsal.*`, `.mediaflow-scratch` marker `created-by-rehearsal`,
  symlink rejection) and resolves the validator from the executing script's
  directory (`install.sh:51-69`). Every destination must resolve strictly
  inside that validated scratch root (`install.sh:72-117`), including derived
  targets (venv, manifest, state subdirectories, preset subdirectories, media
  layout).
- **Production media root refusal.** `install.sh:42-45` — in test mode the
  media destination `/srv/media` (or anything under it) exits 2.
- **Preflight order** (`install.sh:189-262`): repo/provenance checks → full
  revision determination → `MEDIAFLOW_INSTALL_EXPECTED_SHA` match → dirty
  tracked-checkout check (`git status --porcelain --untracked-files=no`) →
  service-state gate → create-only conflicting-destination loop (including
  masked-unit preservation) → `PREFIX`/`DOC_DIR`/`CONFIG_DIR` existence
  refusals → existing-manifest refusal → state-directory precondition.
- **Service-state gate.** `install.sh:215-226` always calls
  `python3 "$REPO_DIR/tools/service_state.py"` for the five units. The helper
  (`tools/service_state.py:classify`) passes only when every unit reports
  `LoadState=not-found`; loaded units in any state, nonzero exits, timeouts,
  and unparseable output all refuse. There is no override seam: an inherited
  `MEDIAFLOW_INSTALL_SERVICE_STATE` is ignored (regression tests:
  `tests/test_service_state_gate.py::test_inherited_override_cannot_convert_rejecting_stub_to_success`,
  `::test_inherited_override_cannot_mask_query_failure`).
- **Create-only conflict model.** `install.sh:229-241` refuses if any of
  `$CONFIG_DIR/config.toml`, `tmdb.env`, `notify.env`, the five unit files, or
  `MANIFEST_PATH` exists (preserving masked units — symlink to `/dev/null`),
  and refuses existing `PREFIX`, `DOC_DIR`, `CONFIG_DIR`. Re-running over an
  existing installation is a conflict, not an upgrade.
- **Manifest before writes.** `install.sh:266-306` writes
  `$STATE_DIR/install-manifest.json` (schema 2: revision, repo, host_only,
  test_mode, created_files, preserved, principals) before any installation
  write; an EXIT trap finalizes SHA-256 hashes (`install.sh:160-187`,
  `finalize_manifest`).
- **Rehearsal privilege suppression.** `install.sh:127-134` (`run_priv`):
  account and systemctl operations are only echoed (`+ [rehearsal] ...`) in
  test mode.
- **Runtime dependencies.** `requirements.lock` pins five packages
  (babelfish, guessit, python-dateutil, rebulk, tzdata) with hashes; the
  (currently unreachable) install path would create `$PREFIX/venv` and run
  `pip install --require-hashes --only-binary=:all: -r requirements.lock`
  plus `pip install --no-deps "$PREFIX"` (`install.sh:349-353`).
- **Sandbox suite.** At this revision the builder ran, in the development
  sandbox: `scripts/check.sh` → traceability `6 passed`; unit/integration
  `990 passed`; all-tests `990 passed`; exit 0 (excluded the unrelated
  untracked `tests/test_scratch_lock.py` via `PYTEST_ADDOPTS`). This is
  builder-reported evidence about the sandbox only.

### A.4 Historical host observations (NOT verified; do not act on them)

- A target machine "john-server" with a Kaby Lake GPU, a 2-core/4-thread CPU,
  `/dev/dri/renderD128`, `/usr/lib/jellyfin-ffmpeg/ffmpeg`, and a production
  media root `/srv/media` is described in `docs/QUESTIONS.md` (HW items),
  `systemd/mediaflow.service` comments, and `config/config.toml.example`.
- Git history contains an older installer message addressing user `john`
  (removed at `27130be`).
- `docs/DEPLOYMENT_SAFETY_HANDOFF.md` and `docs/VERIFIED.md` record that
  `/dev/dri/renderD128` was inaccessible **from the sandbox** — a sandbox
  observation, not a host measurement.
- None of these have been re-verified on the host by the builder. Every host
  fact above must be treated as **unverified** until checked at checkpoint 0
  (section D).

### A.5 Unresolved questions (top-level)

See the ranked register in section C.3; the canonical on-hardware list is
`docs/QUESTIONS.md` (44 unchecked `[HW-xxx]` items at this revision).

## B. Proposed installation inventory

Source revision: `44a3b6042ee292c79fac68a14fa1fa0ac1169b5c` (must be re-pinned
at approval time; the installer's own rehearsal flow pins `EXPECTED_SHA` to
the checkout head).

Action labels: **create** (new artifact), **preserve** (must not be modified),
**proposed change** (mutates something that may exist), **unresolved**
(decision or host fact missing).

| # | Item | Source / target | Action | Notes and citations |
|---|---|---|---|---|
| 1 | System group `mediaflow` | host account database | proposed change | `system` group; `install.sh:309`. Unresolved: does it already exist on the host? Never silently reuse or recreate. |
| 2 | System group `media` | host account database | proposed change | `install.sh:310`. Unresolved: existence and current members. |
| 3 | System user `mediaflow` | host account database | proposed change | `--system --gid mediaflow --home-dir /opt/mediaflow --no-create-home --shell /usr/sbin/nologin` (`install.sh:312-314`). Unresolved: existence, UID collisions. |
| 4 | Supplementary memberships | `mediaflow` → `media`, `render`, `video` | proposed change | `install.sh:315-317`; required by `systemd/mediaflow.service` (`SupplementaryGroups=render video`). Unresolved: do `render`/`video` groups exist? |
| 5 | Code tree `/opt/mediaflow` | `pyproject.toml`, `requirements.lock`, `README.md`, `src/`, `tools/`, `docs/` from the pinned revision | create (only if absent) | mode 0755 `root:root` (`install.sh:338`). Existing tree ⇒ conflict, not upgrade (`install.sh:237`). |
| 6 | Virtualenv `/opt/mediaflow/venv` | created from host `python3` | create | `python3 -m venv` + hashed installs (`install.sh:349-353`). Unresolved: host `python3` version; whether hashed wheels are available offline or a network window is needed. |
| 7 | Package install | `pip install --no-deps /opt/mediaflow` | create | `install.sh:353`. Depends on item 6. |
| 8 | Config dir `/etc/mediaflow` | mode 0750 `root:mediaflow` | create (only if absent) | `install.sh:333`. Existing dir ⇒ conflict (`install.sh:239`); existing content must never be normalized. |
| 9 | `config.toml` | from `config/config.toml.example`, mode 0644 | create | `install.sh:357`. Example ships `root = "/srv/media"`, `dry_run = true` (`config/config.toml.example`). **`dry_run` must stay `true`** until the `docs/QUESTIONS.md` list is clear. |
| 10 | Secret files `tmdb.env`, `notify.env` | from `config/*.env.example`, mode 0640 `root:mediaflow` | create + unresolved content | `install.sh:358-361`. Real tokens are a John-supplied input; the example files are placeholders. |
| 11 | Preset dirs `presets.d/`, `experimental/` | mode 0750 `root:mediaflow` | create | `install.sh:334-335`; presets 0644 from `presets/*.toml` (`install.sh:362-368`). |
| 12 | State dir `/var/lib/mediaflow` | mode 2775 `mediaflow:media` | preserve-if-existing / create-if-absent | `install.sh:266, 242-255`: existing state and `evidence/` are preserved untouched; an existing **root:root 0755** state dir is refused pending a separately approved ownership decision; an existing manifest refuses overwrite (`install.sh:252`). |
| 13 | State subdirs `backup/`, `logs/` | mode 2775 `mediaflow:media` | create | `install.sh:336-337`. |
| 14 | Install manifest `/var/lib/mediaflow/install-manifest.json` | written by the installing run | create | Schema 2 (`install.sh:267-306`). Must exist after any approved install run; never removed by rollback. |
| 15 | Media root `/srv/media`, `movies/`, `_pipeline/convert/movies/<5 preset dirs>` | mode 2775 `mediaflow:media` | **out of scope for host-only**; create only at the separately approved media-initialization stage | `install.sh:371-375`; the gated initializer is `tools/media_init.py` (`MEDIAFLOW_MEDIA_INIT_APPROVED=1`, refuses `/srv/media` without it, `tools/media_init.py:52`). Unresolved: `/srv/media` exists, its current owner/mode, one-filesystem requirement (`docs/QUESTIONS.md` HW-030). |
| 16 | Docs `/usr/share/doc/mediaflow` | mode 0755 `root:root`, contents of repo `docs/` | create (only if absent) | `install.sh:346-347`. Existing dir ⇒ conflict. |
| 17 | systemd units (5 files) | `systemd/mediaflow*.service|timer` → `/etc/systemd/system/`, mode 0644 | create (only if absent) | `install.sh:379-388`. Existing/masked units are preserved, never overwritten (masked = symlink to `/dev/null`, `install.sh:229-232, 382-385`). Unresolved: do any of these names exist on the host now? |
| 18 | Runtime dir `/run/mediaflow` | mode 2775, group `mediaflow` | create by systemd, not by the install | `RuntimeDirectory=mediaflow`, `RuntimeDirectoryMode=2775` (`systemd/mediaflow.service`). Exists only while the unit machinery is active; behavior before first activation is unresolved (verify `doctor` pre-activation). |
| 19 | systemd daemon state | `daemon-reload`, `enable` | **not performed in host-only** | `install.sh:393-394` runs these only in the full (non-host-only) path; the host-only path leaves units stopped/disabled (`install.sh:390-391`). Both remain behind CHECKPOINT 2. |
| 20 | Existing media, Jellyfin config, SSH, firewall, Tailscale, vault | — | **preserve** | Out of scope entirely (`install.sh:2-9` header; `docs/DEPLOYMENT_SAFETY_HANDOFF.md`). |
| 21 | Hardware capability registry | `docs/hardware_checks.toml` (44 items) | preserve / unresolved | Only recorded on-box evidence can set `verified-on-hardware` (`docs/QUESTIONS.md` header). |

No ownership, access, or existing-artifact conflict is silently resolved: any
"already exists" outcome is a stop-and-decide, not an overwrite.

## C. Preconditions and decision register

### C.1 Evidence required before an installation could be approved

1. Pinned source revision re-verified at approval time (`git rev-parse` of the
   branch head; matches the reviewed SHA; checkout clean of tracked changes).
2. Current sandbox suite green at the pinned revision (builder-reported:
   990/990 at `44a3b60`; re-run at approval time).
3. Host fact sheet produced from **read-only** checks (checkpoint 0, section
   D): OS/release, `python3 --version`, systemd version, existence/state of
   the five unit names, existence of users/groups in items 1–4, existence and
   mode/owner of `/opt/mediaflow`, `/etc/mediaflow`, `/var/lib/mediaflow`,
   `/srv/media`, `/usr/share/doc/mediaflow`, `/run/mediaflow`,
   `/usr/lib/jellyfin-ffmpeg/{ffmpeg,ffprobe}`, `/dev/dri/renderD128`.
4. Explicit John approval recorded for: the inventory (B), the chosen account
   plan (items 1–4), the state-directory ownership decision (C.3), and each
   subsequent stage.

### C.2 Conflicts and preservation requirements

- Any pre-existing `/opt/mediaflow`, `/etc/mediaflow`, `/usr/share/doc/mediaflow`,
  or unit file is a **conflict**: the create-only model refuses rather than
  upgrades (`install.sh:229-241`; `docs/DEPLOYMENT_SAFETY_HANDOFF.md`
  "chosen model"). Resolution options (keep existing, archive+remove with a
  new decision, or abandon) must be decided per artifact — never implied.
- `/var/lib/mediaflow` and its `evidence/` tree are preserved untouched,
  including metadata (`tests/test_deployment_safety_round3.py::test_installer_preserves_evidence_and_parent_metadata`).
- Existing state.db, backups, logs, and any existing manifest are preserved;
  an existing manifest refuses a new install run (`install.sh:250-255`).
- Masked units (symlink to `/dev/null`) are preserved and never written
  (`install.sh:229-232`; `tests/test_deployment_safety_round3.py::test_installer_preserves_masked_unit_symlink`).
- Secrets (`tmdb.env`, `notify.env`) never live in `config.toml`
  (`config/config.toml.example` header).

### C.3 Outstanding ownership/permission decisions (open, ranked by consequence)

1. **State directory ownership/mode.** An existing root:root 0755
   `/var/lib/mediaflow` blocks `state.db` creation; the installer refuses and
   demands "a separately approved metadata decision" (`install.sh:243-249`).
   The draft does **not** choose an outcome. Options: chown to
   `mediaflow:media` 2775, leave and relocate state_dir, or abandon — each
   with its own approval.
2. **Account creation vs reuse** (items 1–4): creating system accounts,
   reusing existing ones, or aborting — unresolved host facts.
3. **Media root** (`/srv/media`): existence, owner, mode, free space, single
   filesystem with every pipeline path (SAF-8; `docs/QUESTIONS.md` HW-030) —
   unresolved and out of host-only scope anyway.
4. **Secret provisioning**: who supplies real TMDb/notify tokens, and when
   (`/etc/mediaflow/*.env`, 0640 `root:mediaflow`).
5. **Network policy for the venv build** (item 6): online window vs fully
   offline wheel supply — unresolved.

### C.4 Unverified hardware and deployment prerequisites

- All 44 `docs/QUESTIONS.md` items are unchecked at this revision, including
  QSV/VAAPI encode and rate control (HW-001..005), systemd resource limits
  versus real encodes (HW-006..013), power-loss behavior (HW-014), and
  Jellyfin direct-play (HW-015). These gate **service activation and real
  media processing**, not host-only installation.
- The doctor's default run is non-encoding but not subprocess-free (one
  bounded `ffmpeg -version`) — `docs/DEPLOYMENT_SAFETY_HANDOFF.md` (Priority 2
  note). Hardware capability probes are separate and need their own approval.

### C.5 Prerequisite split

| Stage family | Prerequisites |
|---|---|
| Host-only installation (files, accounts, config, units staged inactive) | C.1 items 1–4; C.2 conflicts resolved; C.3 decisions 1–2, 5; **no** hardware evidence required |
| Service activation (`daemon-reload`, `enable`, `start`) | host-only accepted (F.2); CHECKPOINT 2 approval; HW-006..013 relevant; secrets provisioned; `dry_run` still `true` |
| Media initialization and real processing | CHECKPOINT 3 approval; `/srv/media` facts and SAF-8 single-filesystem check; HW-001..005, 014, 015, 018..030; `dry_run=false` only after `docs/QUESTIONS.md` is clear |

## D. Reviewable execution stages

Rules for every stage: one stage per approval; commands run individually by a
human, **never** as a scripted sequence; every mutating command below is
**NOT AUTHORIZED** and shown only to define what an approved stage would do;
`<VERIFY:...>` placeholders mark host facts that have not been confirmed.

### Stage 0 — read-only verification (no host changes)

Read-only commands (still **NOT AUTHORIZED** for the builder; for John on the
host):

```sh
# read-only: host facts for the C.1 fact sheet
uname -a; python3 --version; systemctl --version
getent group mediaflow; getent group media; getent passwd mediaflow
systemctl status mediaflow.service mediaflow-trash.timer mediaflow-doctor.timer  # read-only
stat -c '%U:%G %a %n' /opt/mediaflow /etc/mediaflow /var/lib/mediaflow /srv/media /usr/share/doc/mediaflow 2>/dev/null
ls -la /usr/lib/jellyfin-ffmpeg/ 2>/dev/null
ls -l /dev/dri/renderD128 2>/dev/null
```

Expected outcome: the C.1 fact sheet, with every `<VERIFY:...>` placeholder in
this document replaced by an observed value, and every B-inventory conflict
listed explicitly.

**STOP / CHECKPOINT 1 — approval to perform any write (Stage 1+).** Requires:
fact sheet reviewed; C.3 decisions 1, 2 and 5 taken; inventory conflicts
resolved per artifact.

### Stage 1 — accounts and groups (mutating; NOT AUTHORIZED)

```sh
# proposed, NOT AUTHORIZED; only for names confirmed absent at Stage 0
groupadd --system mediaflow
groupadd --system media
useradd --system --gid mediaflow --home-dir /opt/mediaflow --no-create-home --shell /usr/sbin/nologin mediaflow
usermod -aG media mediaflow
usermod -aG render video mediaflow   # <VERIFY: render/video groups exist>
```

Effects: system account(s) exist. Reversal: see section E (accounts are not
reversed by the manifest rollback).

### Stage 2 — directories, code, venv, configuration (mutating; NOT AUTHORIZED)

```sh
# proposed, NOT AUTHORIZED; every target must have been confirmed absent at Stage 0
install -d -o root -g root -m 0755 /opt/mediaflow
cp -R <repo>/pyproject.toml <repo>/requirements.lock <repo>/README.md <repo>/src <repo>/tools <repo>/docs /opt/mediaflow/
python3 -m venv /opt/mediaflow/venv
/opt/mediaflow/venv/bin/pip install --require-hashes --only-binary=:all: -r /opt/mediaflow/requirements.lock
/opt/mediaflow/venv/bin/pip install --no-deps /opt/mediaflow
install -d -o root -g mediaflow -m 0750 /etc/mediaflow
install -m 0644 <repo>/config/config.toml.example /etc/mediaflow/config.toml   # dry_run stays true
install -d -o root -g mediaflow -m 0750 /etc/mediaflow/presets.d /etc/mediaflow/experimental
install -m 0644 <repo>/presets/*.toml /etc/mediaflow/presets.d/
install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow   # <VERIFY: C.3 decision 1 first>
install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow/backup /var/lib/mediaflow/logs
install -d -o root -g root -m 0755 /usr/share/doc/mediaflow && cp -R <repo>/docs/. /usr/share/doc/mediaflow/
```

Manifest requirement: because this draft replaces the installer run, the
manual equivalent of `install.sh:266-306` must be performed **first within
Stage 2**: write `/var/lib/mediaflow/install-manifest.json` (schema 2) before
the first write of this stage, and record created paths so the section-E
rollback scope exists. (Open item: the installer computes this automatically;
a manual run needs an explicit, reviewed step for it — flagged in C.3 as part
of approval.)

### Stage 3 — systemd units staged inactive (mutating; NOT AUTHORIZED)

```sh
# proposed, NOT AUTHORIZED
install -m 0644 <repo>/systemd/mediaflow.service      /etc/systemd/system/
install -m 0644 <repo>/systemd/mediaflow-trash.service /etc/systemd/system/
install -m 0644 <repo>/systemd/mediaflow-trash.timer   /etc/systemd/system/
install -m 0644 <repo>/systemd/mediaflow-doctor.service /etc/systemd/system/
install -m 0644 <repo>/systemd/mediaflow-doctor.timer   /etc/systemd/system/
```

Units are copied but **not** enabled, started, or daemon-reloaded here. They
remain inactive until CHECKPOINT 2.

**STOP / CHECKPOINT 2 — approval for service activation** (separate future
document; includes `systemctl daemon-reload`, `enable`, `start`, and the HW-006..013
verification plan). Not part of host-only installation.

### Stage 4 — host-only acceptance (read-only verification; see F.2)

### Stage 5 — media initialization and processing

Out of scope for this draft's approval. Requires **STOP / CHECKPOINT 3**;
`tools/media_init.py` (`MEDIAFLOW_MEDIA_INIT_APPROVED=1`) and the
`dry_run=false` decision are separate approvals; see F.3.

There is deliberately **no one-shot installer, script, or command sequence in
this draft** that performs Stage 1→3 unattended; each command is reviewed and
run individually, and the checkpoints cannot be bypassed by construction
(no automation is provided).

## E. Failure handling and rollback

Grounded in a read of `tools/install_rollback.py` (all 213 lines) and
`install.sh:72-98, 266-306` at the starting reference. Passing sandbox tests
(`tests/test_deployment_safety_round3.py::test_installer_manifest_and_rollback_cover_created_files`)
prove the tested scenario only; they do **not** establish that rollback is
complete or safe on a real host.

What rollback can do (by design, `tools/install_rollback.py:110-199`):
remove files recorded in `created_files` whose recorded SHA-256 still matches,
prune empty new directories, and never touch protected roots.

What cannot be reversed, and known gaps (blockers/limitations — **not repaired
in this task**):

| # | Class | Gap |
|---|---|---|
| E-1 | **Blocker for "clean uninstall" claims** | The five `/etc/systemd/system/mediaflow-*` unit files are in `NEVER_REMOVE_PATHS` (`tools/install_rollback.py:70-74`) even though the module docstring (lines 25–29) says they may be removed when recorded. Rollback leaves the (inactive) units behind; docstring and code disagree. A manual unit-removal decision would be required. |
| E-2 | Limitation | `/etc/mediaflow` is unconditionally protected (`:61`): `config.toml`, presets and **secrets** survive any rollback by design. Removal is a separate decision. |
| E-3 | Limitation | The installer records each copied tree as a single entry (`FILE:$PREFIX/src`, `install.sh:339-345`). Rollback descends into such entries via `_remove_path_recursive` (`:175-199`) **without per-file hash verification** (the hash gate applies to top-level entries; a missing hash counts as matching, `:97-107`). Contrary to the docstring claim ("only when their SHA-256 still matches"), files modified after install under those trees may still be removed. |
| E-4 | Limitation | The `removed` report overstates: nonexistent paths and directories that could not be pruned are still appended to the output (`:135-141, 169-171`). Any future rollback must be verified with independent `find`/`stat` checks, not by trusting the list. |
| E-5 | Limitation | Accounts/groups (Stage 1) are not manifest artifacts — the manifest records principals only as "created-or-existing" (`install.sh:296-301, 319-330`) — and rollback never touches accounts. Reversal is a separate, explicitly approved manual action (`userdel`/`groupdel` **NOT AUTHORIZED** here). |
| E-6 | Limitation | There is no upgrade path. A repeat run over an existing installation is a conflict (`install.sh:229-241`); recovery from a half-approved manual state is a per-artifact decision, not a tool. |
| E-7 | Boundary | Failure before the manifest write = preflight refusal, nothing written, nothing to roll back. Failure after writes = manifest exists with finalized hashes (EXIT trap, `install.sh:72-98`); rollback scope is then the recorded created set. A failure *during Stage 2 manual execution before the manifest step* has no tool support — hence the manifest-first requirement in Stage 2. |

Preserved unconditionally under all failure modes: `/srv/media` and everything
under it, `/var/lib/mediaflow` including `evidence/`, `backup/`, `logs/`,
`/etc/mediaflow` contents, `/usr/share/doc/mediaflow`, the manifest itself
(`NEVER_REMOVE_PATHS`, `NEVER_REMOVE_NAMES`, `tools/install_rollback.py:50-75`).

## F. Acceptance evidence

### F.1 Principle

Each future stage produces evidence **before** the next stage may be
requested. Evidence is recorded in `docs/VERIFIED.md` (style: what ran, where,
revision, result). Sandbox test success — including the 990/990 run reported
in A.3 — proves sandbox behavior only and **does not establish production
readiness** (`docs/DEPLOYMENT_SAFETY_HANDOFF.md` "Stop point").

### F.2 Host-only installation acceptance (this draft's scope)

After an approved Stage 1–3 sequence, host-only acceptance requires:

1. `install-manifest.json` exists at `/var/lib/mediaflow/` with schema 2,
   correct revision, and finalized hashes for created files.
2. Read-only checks (approved, on-host): the five unit files exist under
   `/etc/systemd/system/`; `systemctl is-active` reports inactive and
   `is-enabled` reports disabled/static for each — **no** activation occurred.
3. `sudo -u mediaflow -g media /opt/mediaflow/venv/bin/mediaflow --config /etc/mediaflow/config.toml doctor --json`
   (NOT AUTHORIZED; proposed) exits 0/2/3/4 with no crash, in dry-run, with
   hardware probes still unchecked.
4. Configuration preserved: `dry_run = true`; secrets 0640 `root:mediaflow`.
5. Pre-existing evidence/state metadata unchanged (compare with the Stage 0
   fact sheet).
6. Explicitly **out of acceptance**: service activation, timers, real encoder
   runs, media initialization, registry status changes.

### F.3 Later-stage acceptance (defined, not authorized)

- **Service activation acceptance** (after CHECKPOINT 2): journal shows clean
  start; in-unit `doctor` matches out-of-unit results; HW-006..013 evidence
  recorded; secret redaction confirmed (HW-040); daemon still `dry_run=true`.
- **Media processing acceptance** (after CHECKPOINT 3): `media_init` gate
  evidence; first real encode under HW-001..005/014/015/024/042 checks;
  `docs/QUESTIONS.md` items moved to `verified-on-hardware` only by recorded
  on-box evidence.

## F.4 Explicit non-claims

- This draft does not claim the host facts in A.4 are true.
- It does not claim rollback is complete (see E-1..E-6).
- It does not claim approval for any command it contains.
- It does not claim that passing sandbox tests establishes production
  readiness.
