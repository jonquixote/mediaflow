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
- **Create-only conflict model.** `install.sh:226-239` refuses if any of
  `$CONFIG_DIR/config.toml`, `tmdb.env`, `notify.env`, the three unit files
  the loop lists (`mediaflow.service`, `mediaflow-trash.timer`,
  `mediaflow-doctor.timer`), or `MANIFEST_PATH` exists (preserving masked
  units — symlink to `/dev/null`),
  and refuses existing `PREFIX`, `DOC_DIR`, `CONFIG_DIR`. Re-running over an
  existing installation is a conflict, not an upgrade.
- **Manifest before writes.** `install.sh:266-306` writes
  `$STATE_DIR/install-manifest.json` (schema 2: revision, repo, host_only,
  test_mode, created_files, preserved, principals) before any installation
  write; an EXIT trap finalizes SHA-256 hashes (`install.sh:160-187`,
  `finalize_manifest`).
- **Rehearsal privilege suppression.** `install.sh:127-133` (`run_priv`):
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
| 15 | Media root `/srv/media`, `movies/`, and the pipeline tree | mode 2775 `mediaflow:media` | **out of scope for host-only**; create only at the separately approved media-initialization stage | The installer's own media block creates `/srv/media`, `movies/`, and `_pipeline/convert/movies/<5 preset dirs>` (`install.sh:371-375`); the documented gated initializer `tools/media_init.py` creates a **fuller** set — `_pipeline`, `_pipeline/inbox`, `_pipeline/convert`, `_pipeline/convert/movies/<5 presets>`, `_pipeline/work`, `_pipeline/review`, `_pipeline/failed`, `_pipeline/trash`, all mode 2775 (`tools/media_init.py:12-26`). It refuses `/srv/media` without `MEDIAFLOW_MEDIA_INIT_APPROVED=1` (`:52-53`), refuses symlink components (`:29-39, 76-77`), and **fails closed without `MEDIAFLOW_MEDIA_INIT_UID`/`_GID` under `--apply`** (`:65-71`). Unresolved: `/srv/media` exists, its current owner/mode, one-filesystem requirement (`docs/QUESTIONS.md` HW-030); which of the two layouts an approved initialization would use. |
| 16 | Docs `/usr/share/doc/mediaflow` | mode 0755 `root:root`, contents of repo `docs/` | create (only if absent) | `install.sh:346-347`. Existing dir ⇒ conflict. |
| 17 | systemd units (5 files) | `systemd/mediaflow*.service|timer` → `/etc/systemd/system/`, mode 0644 | create (only if absent) | `install.sh:379-388`. Existing files are refused as conflicts; a masked unit (symlink to `/dev/null`) on the host blocks the install at the service-state gate before any preservation logic applies (see C.2). Unresolved: do any of these names exist on the host now? |
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
  upgrades (`install.sh:226-239`; `docs/DEPLOYMENT_SAFETY_HANDOFF.md`
  "chosen model"). Resolution options (keep existing, archive+remove with a
  new decision, or abandon) must be decided per artifact — never implied.
- `/var/lib/mediaflow` and its `evidence/` tree are preserved untouched,
  including metadata (`tests/test_deployment_safety_round3.py::test_installer_preserves_evidence_and_parent_metadata`).
- Existing state.db, backups, logs, and any existing manifest are preserved;
  an existing manifest refuses a new install run (`install.sh:250-255`).
- Masked units: on a real host a masked mediaflow unit **blocks the install**.
  The service-state gate (`install.sh:215-226`) runs before the conflict loop
  and refuses any `LoadState` other than `not-found` — including
  `LoadState=masked` (`tools/service_state.py:classify`). The masked-preserve
  branch inside the conflict loop (`install.sh:229-231`) is reachable only
  where the gate passes or is stubbed — scratch rehearsals and
  `tests/test_deployment_safety_round3.py::test_installer_preserves_masked_unit_symlink` —
  not on a host whose real unit is masked.
- Secrets (`tmdb.env`, `notify.env`) never live in `config.toml`
  (`config/config.toml.example` header).

### C.3 Outstanding ownership/permission decisions (open, ranked by consequence)

1. **State directory ownership/mode.** An existing root:root 0755
   `/var/lib/mediaflow` blocks `state.db` creation; the installer refuses and
   demands "a separately approved metadata decision" (`install.sh:243-249`).
   Observed on john-server 2026-11-26 (section D fact sheet): the directory
   exists in exactly that state, containing only `evidence/`. The draft does
   **not** choose an outcome. Options: change ownership to `mediaflow:media`
   2775, relocate `state_dir`, or stop — consequences per option in the
   decision brief (C.7).
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

### C.6 Open decision: procedure form (unchosen)

If an installation is ever approved, it could be performed either as the
hand-run procedure this draft describes, or by a **separately reviewed helper
script** written for that future approval. Tradeoff, stated without choosing:

- **Hand-run (this draft):** no new code to review or trust; every command is
  individually visible; but it is error-prone in exactly the places rollback
  depends on — manifest transcription, hash fill-in, ordering — and nothing
  validates the result (see the Stage 2 manifest limitation).
- **Reviewed helper script:** deterministic ordering, automatic manifest and
  hash handling, repeatable; but it is new privileged code requiring its own
  review, tests, and approval, and it recreates a subset of `install.sh` —
  with the risk of drifting from the installer or being mistaken for a
  general-purpose installer. It would also need the same scratch/containment
  discipline if it ever ran in rehearsal.

The choice belongs to the reviewer; this draft does not make it. Until it is
made, no implementation work for either form is authorized.

### C.7 Decision brief — C.3 item 1: state directory ownership (resolves nothing)

Inputs read for this brief: `config/config.toml.example`, `src/mediaflow/config.py`,
`src/mediaflow/engine.py`, `src/mediaflow/guards.py`, `src/mediaflow/daemon.py`,
`src/mediaflow/cli.py`, `systemd/mediaflow*.service`, `tools/install_rollback.py`,
`install.sh`, plus the 2026-11-26 fact sheet (section D).

**Observed starting state (host, 2026-11-26):** `/var/lib/mediaflow` is
`root:root 755`, containing only `evidence/` (two subdirectories:
`20261123T042753` `root:root 755`; `20261123T042940` `john:john 750`); no
`state.db`, `backup/`, `logs/`, or manifest. `install.sh:243-249` refuses
exactly this owner/mode.

**What the service must write under `state_dir`** (all derived from the
configured `state_dir`, `src/mediaflow/config.py:257-277`):

- `state.db` (SQLite; read-only opens additionally create `-wal`/`-shm`
  auxiliary files — documented in `docs/DEPLOYMENT_SAFETY_HANDOFF.md`);
- `backup/` — database backups written by the maintenance command
  (`src/mediaflow/cli.py:540-556`; path `config.py:261`);
- `logs/` — daemon log directory (`config.py:265`; `src/mediaflow/engine.py:203`);
- `capabilities.json`, `status.json`, `progress.json` — atomically replaced
  runtime state (`config.py:269-277`; `src/mediaflow/daemon.py:274-298`;
  `src/mediaflow/engine.py:203-207`);
- `evidence/` — **no writer found**: nothing in `src/` or `tools/` writes
  under `state_dir/evidence`. `tools/evidence.py` operates on explicitly
  named bundle directories (`tools/evidence.py:2` and `validate`), and the
  handoff's examples use an `$EVIDENCE` variable. The installer and rollback
  protect that path regardless (`install.sh:254-256`;
  `tools/install_rollback.py:63`). Who created the host's two evidence
  subdirectories could not be verified from the repository.

**Is `state_dir` configurable, and what else assumes `/var/lib/mediaflow`?**

- Configurable: yes — `state_dir` is a config key
  (`config/config.toml.example`; `src/mediaflow/config.py:48`
  `DEFAULT_STATE_DIR = Path("/var/lib/mediaflow")`), classified
  `RESTART_REQUIRED` on change (`config.py:436`). All the paths above follow
  it (`config.py:257-277`), as do the engine's derivations
  (`engine.py:203-207, 429-435, 3384-3390`).
- Hardcoded assumptions on the old path remain in:
  - the three service units: `ReadWritePaths=/srv/media /var/lib/mediaflow /run/mediaflow`
    (`systemd/mediaflow.service:48`, `mediaflow-trash.service:19`,
    `mediaflow-doctor.service:17`). Under `ProtectSystem=strict` (all three
    units) a relocated state dir would be **read-only** to the service until
    the units are edited;
  - `tools/install_rollback.py:59-66`: the never-remove path list is
    hardcoded to `/var/lib/mediaflow` (+ `evidence/`, `backup/`, `logs/`);
    a relocated state dir would lose that protection (the by-name list at
    `:50` would still shield `install-manifest.json`, `evidence`, `backup`,
    `logs` at any depth);
  - `src/mediaflow/guards.py:162` defaults `db_dir` to
    `/var/lib/mediaflow` (overridable via the `db_dir` key,
    `guards.py:51-58`, applied at `:169/:191`), and `:531` falls back to the
    same path for the db-space check. Whether the runtime wiring propagates a
    relocated `state_dir` into `db_dir` was **not traced further**;
  - `install.sh` itself: `STATE_DIR` default, precondition, manifest path,
    evidence preservation (`install.sh:29, 243-256, 36`).
  - Nothing in the observed host tree (no `state.db`/`backup`/`logs` yet)
    would be stranded by a relocation today.

**If the top-level directory becomes `mediaflow:media 2775` while the
existing evidence subdirectories keep their owners (`root:root 755`,
`john:john 750`):**

- The service can create and write `state.db`, `capabilities.json`,
  `status.json`, `progress.json`, and populate `backup/` — that is the point
  of the mode (`install.sh:243-249` refuses 755 root:root precisely because
  the service user otherwise cannot create `state.db`).
- The service can **rename or move the `evidence/` directory entry**: it
  would own the parent (`/var/lib/mediaflow`, mode 2775, no sticky bit), and
  POSIX rename/unlink of an entry requires write+execute on the parent
  directory, not ownership of the entry. `ReadWritePaths=/var/lib/mediaflow`
  (all three units) permits the write; `ProtectSystem=strict` does not cover
  `/var/lib`. The moved tree stays intact but the protected path no longer
  contains it, and a same-named replacement could be created in its place.
- The service **cannot** rename/delete entries *inside* `evidence/` (that
  needs write on `evidence/` itself, `root:root 755`), cannot modify
  `20261123T042753` (755 root:root), and has no access at all to
  `20261123T042940` (`750 john:john`; `mediaflow` is not user or group
  `john`).
- Rollback protection is ownership-independent: `NEVER_REMOVE_PATHS`
  (`tools/install_rollback.py:59-66`) shields the paths regardless of who
  owns them; the exposure above is the *service's* runtime capability, not a
  rollback behavior.
- Verified by reasoning over the observed modes plus the unit hardening;
  **not** exercised (no host action). POSIX ACLs beyond mode bits, and any
  `ProtectPaths`-style restriction beyond the listed directives, were not
  verifiable from the repository.

**Consequences per option:**

- **Change ownership** (`chown mediaflow:media`, mode 2775, top level only):
  unblocks `state.db` creation; no config or unit edits (paths unchanged;
  `state_dir` reload class irrelevant since the value does not change);
  rollback protections unchanged; the service gains the rename/move
  capability over the `evidence/` entry described above; the existing
  evidence subdirectories keep their owners and modes, and whether to change
  *their* metadata is a separate sub-decision. A metadata change to an
  existing directory is a **proposed change** in inventory terms and must
  never be performed silently.
- **Relocate `state_dir`** (config change to a new path, owned correctly from
  the start): avoids touching the observed tree; but requires editing
  `ReadWritePaths` in all three units (a **proposed change** to reviewed
  units), loses the rollback tool's hardcoded path protection for the new
  location (relying only on the by-name list), leaves `guards.py`'s `db_dir`
  default pointing at the old path unless the key is set, leaves the existing
  `evidence/` tree behind at the old path (or requires a separate move
  decision), and is a `RESTART_REQUIRED` config key (`config.py:436`).
- **Stop** (leave `root:root 755`; no host-only install yet): the installer
  refuses (`install.sh:243-249`), so no create-only installation can proceed
  until one of the other options is approved; the rehearsal path is
  unaffected (scratch-only); the evidence tree is untouched. Nothing else in
  the host-only inventory is blocked by this decision except items that
  derive from it.

**Could not be verified from the repository:** who/what created the two
evidence subdirectories; whether any component writes `state.db` WAL sidecars
under a different path; whether `guards.py`'s `db_dir` is wired from
`state_dir` at runtime; actual ACLs, mount flags, or additional systemd
restrictions on the host; whether the service self-heals a missing `logs/`
subdirectory (`engine.py:286-292` uses `mkdir(parents=True, exist_ok=True)`
for some paths — which paths, unverified).

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
stat -c '%U:%G %a %n' /opt/mediaflow /etc/mediaflow /var/lib/mediaflow /srv/media /usr/share/doc/mediaflow 2>/dev/null
ls -la /usr/lib/jellyfin-ffmpeg/ 2>/dev/null
ls -l /dev/dri/renderD128 2>/dev/null
```

Read-only replication of the installer's service-state gate — the same
`systemctl show` form and properties `tools/service_state.py` uses
(`PROPS` at `tools/service_state.py:11`, command build `:23-27`):

```sh
# read-only: gate replication (tools/service_state.py contract)
for u in mediaflow.service mediaflow-trash.service mediaflow-trash.timer \
         mediaflow-doctor.service mediaflow-doctor.timer; do
  systemctl show -p Id -p LoadState -p ActiveState -p UnitFileState "$u"
done
```

Contract (from `tools/service_state.py:29-57`): the installation could only
proceed if **every** unit reports `LoadState=not-found`. A loaded or masked
unit in any state refuses; so does any **query failure** — nonzero
`systemctl` exit (`:38-39`), timeout (`:34-35`), `systemctl` not found
(`:32-33`), or output lacking `LoadState`/`ActiveState` ("unparseable",
`:45-46`). Anything other than five `LoadState=not-found` answers means the
installer's gate would refuse: stop and record the fact.

Read-only replication of the installer's conflict and precondition checks
(`install.sh:229-262`):

```sh
# read-only: conflict paths the installer refuses on (install.sh:226-236 —
# exactly seven paths). The two oneshot service files below are a deliberate,
# stricter addition by this draft: the installer's loop does not check them,
# but Stage 3 would create them, so detecting pre-existing copies early is
# useful. Read-only either way.
for p in /etc/mediaflow/config.toml /etc/mediaflow/tmdb.env /etc/mediaflow/notify.env \
         /etc/systemd/system/mediaflow.service /etc/systemd/system/mediaflow-trash.timer \
         /etc/systemd/system/mediaflow-doctor.timer /var/lib/mediaflow/install-manifest.json \
         /etc/systemd/system/mediaflow-trash.service /etc/systemd/system/mediaflow-doctor.service; do
  if [ -L "$p" ]; then echo "symlink $p -> $(readlink "$p")";
  elif [ -e "$p" ]; then echo "exists: $p"; fi
done
# NOTE on masked units: a masked mediaflow unit on the host is NOT a
# preserved-exception at install time. The service-state gate runs first
# (install.sh:215-226) and refuses any LoadState other than not-found —
# including LoadState=masked — so a masked unit blocks the install before the
# conflict loop's masked-preserve branch (install.sh:229-231) is ever reached.
# That preserve branch applies only where the gate passes or is stubbed
# (scratch rehearsals; tests/test_deployment_safety_round3.py::test_installer_preserves_masked_unit_symlink).
# read-only: create-only directory conflicts (install.sh:237-239)
for d in /opt/mediaflow /usr/share/doc/mediaflow /etc/mediaflow; do
  [ -e "$d" ] && echo "conflict: $d"
done
# read-only: state-directory precondition (install.sh:243-249) — a
# root:root 0755 state dir would refuse; decision C.3 item 1 applies
stat -c '%a %U:%G %n' /var/lib/mediaflow 2>/dev/null
# read-only: runtime directory (systemd RuntimeDirectory; config runtime_dir)
ls -ld /run/mediaflow 2>/dev/null || echo "/run/mediaflow absent"
```

#### Stage 0 fact sheet (2026-11-26) — partial: `/var/lib/mediaflow`

Read-only output **run by John on john-server on 2026-11-26**; not verified
by the builder. Pasted verbatim as received (a prose summary; the raw command
transcript was not provided):

```
Observed state of /var/lib/mediaflow: root:root 755, created Nov 22, contents
only evidence/ with two subdirectories (20261123T042753 root:root 755;
20261123T042940 john:john 750), no state.db, backup, logs or manifest, 20K total.
```

What this replaces with an observation (previously assumption/unverified):

- `/var/lib/mediaflow` **exists** on the host, owner `root:root`, mode `755`
  (created Nov 22). This is exactly the precondition `install.sh:243-249`
  refuses on; decision C.3 item 1 now starts from a confirmed state (see the
  brief in C.7).
- The `evidence/` tree exists with two subdirectories: `20261123T042753`
  (`root:root 755`) and `20261123T042940` (`john:john 750`). No `state.db`,
  `backup/`, `logs/`, or `install-manifest.json` exist yet — so a create-only
  install would find no manifest conflict (`install.sh:252`) and no state
  subdirectory conflicts.

What it does **not** cover (still unverified — every other Stage 0 check and
A.4 assumption stands): `/srv/media` and its contents, `/opt/mediaflow`,
`/etc/mediaflow`, `/usr/share/doc/mediaflow`, `/run/mediaflow`, the five
systemd unit names, the existence of the `mediaflow`/`media`/`render`/`video`
accounts, python/systemd versions, `/usr/lib/jellyfin-ffmpeg/`,
`/dev/dri/renderD128`, GPU/CPU facts, and the `docs/QUESTIONS.md` hardware
items. No conclusions beyond the observed listing are drawn here.

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
# one usermod per group, each only if that group exists — the same shape as
# the installer's loop (install.sh:315-317: "for grp in media render video;
# if getent group \"$grp\"; then usermod -aG \"$grp\" mediaflow; fi").
# -aG takes a single comma-separated group list; a two-group form such as
# "-aG render video mediaflow" would misparse, so these are separate calls:
usermod -aG media mediaflow        # only if group media exists
usermod -aG render mediaflow       # <VERIFY: group render exists>
usermod -aG video mediaflow        # <VERIFY: group video exists>
```

Effects: system account(s) exist. Reversal: see section E (accounts are not
reversed by the manifest rollback).

### Stage 2 — directories, code, venv, configuration (mutating; NOT AUTHORIZED)

Commands follow the installer's own write sequence (`install.sh:266-368`);
media directories are deliberately absent here (Stage 5 only).

```sh
# proposed, NOT AUTHORIZED; every target must have been confirmed absent at Stage 0
# install -d semantics: the installer skips existing paths entirely
# (install.sh:140-142: "if [[ -e \"$path\" ]]; then return 0; fi") and never
# resets owner/mode on them. GNU install -d on an EXISTING directory would
# apply the given owner/mode — so every install -d below is "only if absent";
# the state directory and its subdirectories are preserve-if-existing.
# File installs mirror install_file's real (non-rehearsal) form with explicit
# -o/-g (install.sh:150-158); the rehearsal form omits them, this draft does not.
# (1) state directory first: the manifest's parent (install.sh:266) —
#     preserve-if-existing (inventory item 12; decision C.3 item 1 first)
install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow   # only if absent; <VERIFY: C.3 decision 1 first>
# (2) manifest next — see "Manifest ordering" below (install.sh:267-306)
# (3) configuration directories (install.sh:333-335) — only if absent
install -d -o root -g mediaflow -m 0750 /etc/mediaflow
install -d -o root -g mediaflow -m 0750 /etc/mediaflow/presets.d /etc/mediaflow/experimental
# (4) state subdirectories (install.sh:336-337) — only if absent; preserve-if-existing
install -d -o mediaflow -g media -m 2775 /var/lib/mediaflow/backup /var/lib/mediaflow/logs
# (5) code tree (install.sh:338-345) — only if absent (existing tree = conflict)
install -d -o root -g root -m 0755 /opt/mediaflow
cp -R <repo>/pyproject.toml <repo>/requirements.lock <repo>/README.md <repo>/src <repo>/tools <repo>/docs /opt/mediaflow/
# (6) documentation (install.sh:346-347) — only if absent (existing dir = conflict)
install -d -o root -g root -m 0755 /usr/share/doc/mediaflow && cp -R <repo>/docs/. /usr/share/doc/mediaflow/
# (7) venv and package (install.sh:349-353)
python3 -m venv /opt/mediaflow/venv
/opt/mediaflow/venv/bin/pip install --require-hashes --only-binary=:all: -r /opt/mediaflow/requirements.lock
/opt/mediaflow/venv/bin/pip install --no-deps /opt/mediaflow
# (8) configuration files (install.sh:357-368) — file targets must be absent
#     (the Stage 0 conflict check refuses any existing file)
install -o root -g root -m 0644 <repo>/config/config.toml.example /etc/mediaflow/config.toml   # dry_run stays true
install -o root -g mediaflow -m 0640 <repo>/config/tmdb.env.example /etc/mediaflow/tmdb.env    # placeholder contents; real tokens are a John-supplied input
install -o root -g mediaflow -m 0640 <repo>/config/notify.env.example /etc/mediaflow/notify.env # placeholder contents; real tokens are a John-supplied input
install -o root -g root -m 0644 <repo>/presets/*.toml /etc/mediaflow/presets.d/                # glob matches the five top-level presets only; presets/experimental/ is a directory and is not matched
install -o root -g root -m 0644 <repo>/presets/experimental/1080p-best-x265-mp4.toml /etc/mediaflow/experimental/1080p-best-x265-mp4.toml
```

#### Manifest ordering (what a manual run would have to reproduce)

Grounded in `install.sh:266-306` (write) and `install.sh:160-187`
(`finalize_manifest`, EXIT trap). The installer's actual order is:

1. Create the manifest's parent `/var/lib/mediaflow` (mode 2775) — the only
   write that happens before the manifest exists (`install.sh:266`).
2. Write `/var/lib/mediaflow/install-manifest.json` **before every other
   write**: schema 2 with the pinned revision, repo, `host_only`, `test_mode`,
   `model: "create-only"`, `replaced: []`, `preserved` (path/uid/gid/mode of
   any pre-existing state/evidence), and `principals`
   (`install.sh:267-306`). At this point `created_files` contains **only
   paths created so far — just the state-directory `DIR` entry** — because
   `CREATED_FILES` is populated progressively by `mark_created` as writes
   happen (`install.sh:269` renders the array as it stands at write time).
3. Perform the remaining writes, appending each created path to
   `CREATED_FILES` (`install.sh:134-138, 339-345`).
4. Near the **end** of the script — after the units are written — export the
   accumulated list (`export MF_CREATED_FINAL`, `install.sh:397`).
5. At process exit, the trap (`install.sh:187`) appends any
   `MF_CREATED_FINAL` entries not already recorded and computes the hashes:
   for every `created_files` entry, `sha256` of the file bytes if it is a
   file, `null` otherwise, then writes the manifest back
   (`install.sh:160-186`).

So the *installer* does **not** write the full created_files list up front;
the complete list reaches the manifest only via `finalize_manifest` from
`MF_CREATED_FINAL`, which is exported only after all writes
(`install.sh:397`). A manual run that writes the **full planned**
`created_files` list in the initial manifest is a **deliberate difference**
from the installer: it defines the rollback scope before any write (closer to
the manifest-first intent) at the cost of diverging from `install.sh`'s
progressive behavior. For a hand-run Stage 2/3 the order otherwise applies:
state directory → manifest → all other writes → hash fill-in at the end. What
`tools/install_rollback.py` consumes is exactly `created_files[].path`
(absolute) and `created_files[].sha256`
(`tools/install_rollback.py:115-124`); its never-remove filters and its hash
gate (`:97-107`) then apply.

**Limitation (not repaired here):** a hand-written manifest *can* match what
the rollback tool expects — the shape above is sufficient — but nothing
generates or validates it. A missed or misspelled path silently shrinks the
rollback scope, and a file entry left with an empty/`null` hash silently
disables the hash-mismatch protection (`tools/install_rollback.py:97-107`
treats a missing hash as matching). Any approved run would need the manifest
verified afterwards by an independent read-only listing against the recorded
`created_files`. No helper script is proposed or provided by this draft (that
choice is registered in C.6).

#### Stage-2 reconciliation table (inventory item → creating stage/command)

| B item | Created in | Command |
|---|---|---|
| 1 group `mediaflow` | Stage 1 | `groupadd --system mediaflow` (existence-gated) |
| 2 group `media` | Stage 1 | `groupadd --system media` (existence-gated) |
| 3 user `mediaflow` | Stage 1 | `useradd --system ...` (existence-gated) |
| 4 memberships | Stage 1 | three separate `usermod -aG <grp> mediaflow` (existence-gated) |
| 5 code tree `/opt/mediaflow` | Stage 2 (5) | `install -d` 0755 `root:root` (only if absent) + `cp -R` |
| 6 venv | Stage 2 (7) | `python3 -m venv` |
| 7 package install | Stage 2 (7) | `pip install --no-deps /opt/mediaflow` |
| 8 `/etc/mediaflow` | Stage 2 (3) | `install -d` 0750 `root:mediaflow` (only if absent) |
| 9 `config.toml` | Stage 2 (8) | `install -o root -g root -m 0644` from example |
| 10 `tmdb.env`, `notify.env` | Stage 2 (8) | `install -o root -g mediaflow -m 0640` from examples; real contents John-supplied |
| 11 `presets.d/`, `experimental/` + presets | Stage 2 (3, 8) | dirs 0750 `root:mediaflow` (only if absent); five presets + experimental preset via `install -o root -g root -m 0644` |
| 12 `/var/lib/mediaflow` | Stage 2 (1) | `install -d` 2775 `mediaflow:media` (only if absent; **preserve-if-existing**; decision C.3 item 1 first) |
| 13 `backup/`, `logs/` | Stage 2 (4) | `install -d` 2775 `mediaflow:media` (only if absent; **preserve-if-existing**) |
| 14 install manifest | Stage 2 (2, hash fill-in at end) | see "Manifest ordering" |
| 15 media root set | Stage 5 | `tools/media_init.py` behind CHECKPOINT 3 — not a Stage 2 command |
| 16 `/usr/share/doc/mediaflow` | Stage 2 (6) | `install -d` 0755 `root:root` (only if absent) + `cp -R docs/.` |
| 17 five systemd units | Stage 3 | `install -o root -g root -m 0644` (Stage 3; targets must be absent) |
| 18 `/run/mediaflow` | systemd | `RuntimeDirectory=mediaflow` at activation; no manual command |
| 19 daemon-reload/enable | deferred | behind CHECKPOINT 2 |
| 20 preserved artifacts | — | no command; conflicts refuse |
| 21 hardware registry | — | no command; evidence-only |

### Stage 3 — systemd units staged inactive (mutating; NOT AUTHORIZED)

```sh
# proposed, NOT AUTHORIZED; each target must have been confirmed absent at
# Stage 0 (the installer's conflict loop checks the service and both timers,
# install.sh:226-236; this draft also checks the two oneshot service files)
# -o/-g match install_file's real form (install.sh:150-158). /etc/systemd/system
# itself exists on any systemd host (installer: install.sh:379, skip-if-existing);
# do not create or re-mode it.
install -o root -g root -m 0644 <repo>/systemd/mediaflow.service      /etc/systemd/system/
install -o root -g root -m 0644 <repo>/systemd/mediaflow-trash.service /etc/systemd/system/
install -o root -g root -m 0644 <repo>/systemd/mediaflow-trash.timer   /etc/systemd/system/
install -o root -g root -m 0644 <repo>/systemd/mediaflow-doctor.service /etc/systemd/system/
install -o root -g root -m 0644 <repo>/systemd/mediaflow-doctor.timer   /etc/systemd/system/
```

Units are copied but **not** enabled, started, or daemon-reloaded here. They
remain inactive until CHECKPOINT 2.

**STOP / CHECKPOINT 2 — approval for service activation** (separate future
document; includes `systemctl daemon-reload`, `enable`, `start`, and the HW-006..013
verification plan). Not part of host-only installation.

### Stage 4 — host-only acceptance (read-only verification; see F.2)

### Stage 5 — media initialization and processing

Out of scope for this draft's approval. Requires **STOP / CHECKPOINT 3**.
The gated initializer is `tools/media_init.py`: it refuses `/srv/media`
without `MEDIAFLOW_MEDIA_INIT_APPROVED=1` (`tools/media_init.py:52-53`),
refuses symlink components, creates the pipeline tree listed in inventory
item 15 at mode 2775, and fails closed when `--apply` is used without
explicit `MEDIAFLOW_MEDIA_INIT_UID`/`MEDIAFLOW_MEDIA_INIT_GID` (`:65-71`).
The `dry_run=false` decision is a separate approval; see F.3. Note the open
layout question in item 15 (installer block vs initializer set).

There is deliberately **no one-shot installer, script, or command sequence in
this draft** that performs Stage 1→3 unattended; each command is reviewed and
run individually, and the checkpoints cannot be bypassed by construction
(no automation is provided).

## E. Failure handling and rollback

Grounded in a read of `tools/install_rollback.py` (all 213 lines) and
`install.sh:160-187, 266-306` at the starting reference. Passing sandbox tests
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
| E-2b | Limitation | `/usr/share/doc/mediaflow` is also in `NEVER_REMOVE_PATHS` (`tools/install_rollback.py:67`, "Documentation outside the install scope") although a create-only install **creates** it (`install.sh:346-347`). Rollback therefore leaves the installed documentation tree behind; its removal would be a separate manual decision. |
| E-3 | Limitation | The installer records each copied tree as a single entry (`FILE:$PREFIX/src`, `install.sh:339-345`). Rollback descends into such entries via `_remove_path_recursive` (`:175-199`) **without per-file hash verification** (the hash gate applies to top-level entries; a missing hash counts as matching, `:97-107`). Contrary to the docstring claim ("only when their SHA-256 still matches"), files modified after install under those trees may still be removed. |
| E-4 | Limitation | The `removed` report overstates: nonexistent paths and directories that could not be pruned are still appended to the output (`:135-141, 169-171`). Any future rollback must be verified with independent `find`/`stat` checks, not by trusting the list. |
| E-5 | Limitation | Accounts/groups (Stage 1) are not manifest artifacts — the manifest records principals only as "created-or-existing" (`install.sh:296-301, 319-330`) — and rollback never touches accounts. Reversal is a separate, explicitly approved manual action (`userdel`/`groupdel` **NOT AUTHORIZED** here). |
| E-6 | Limitation | There is no upgrade path. A repeat run over an existing installation is a conflict (`install.sh:226-239`); recovery from a half-approved manual state is a per-artifact decision, not a tool. |
| E-7 | Boundary (corrected 2026-11-26) | Failure **before** the manifest write = preflight refusal, nothing written, nothing to roll back. Failure **after the manifest write but before `export MF_CREATED_FINAL`** (`install.sh:397`) = the manifest contains **only the state-directory `DIR` entry with a null hash**: paths created in that window (config dirs, `backup/`/`logs/`, the code tree, docs, config files, units) are not in `created_files` and are therefore **outside the rollback tool's scope**, and no hashes exist for them (`finalize_manifest` can only iterate recorded entries; `install.sh:160-186`). Any command failure in that window aborts into it under `set -e` (e.g., a failed `cp -R` at `install.sh:339-345`). Failure **after the export** = `finalize_manifest` appends the complete list and hashes it. The existing test injects `MEDIAFLOW_INSTALL_FAIL_AFTER="config"` (`tests/test_deployment_safety_round3.py:169-182`), which fires **after** the export (`install.sh:398-401`), so it covers only the post-export case: it asserts each `created_files` item *has* a `sha256` key (`:177` — key presence, not non-null values) and does **not** cover the pre-export window. A failure during a Stage 2 manual execution before the manifest step has no tool support at all. Not repaired here. |
| E-8 | Checked, no gap found at this revision | `NEVER_REMOVE_NAMES` (`tools/install_rollback.py:50`: `install-manifest.json`, `evidence`, `backup`, `logs`) applies by file name at every depth, including inside `/opt/mediaflow`. The copied trees (`pyproject.toml`, `requirements.lock`, `README.md`, `src/`, `tools/`, `docs/`) were scanned at the starting reference: **no path matches any of those names**, so no copied artifact is spuriously protected. This is revision-specific: a future tree containing a directory named `logs`, `backup`, or `evidence` under the prefix would be shielded from rollback. |

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
- It does not claim rollback is complete (see E-1..E-8).
- It does not claim approval for any command it contains.
- It does not claim that passing sandbox tests establishes production
  readiness.
