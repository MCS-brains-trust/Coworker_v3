# Environment and Deploy

Last reconciled: 2026-05-18.

This document is the operational source of truth for how to ship
code to the MC & S CoWorker v3 production droplet
(`coworker-v3-prod-syd1`, SYD1) and what the environment looks like
once code is there. Read this before invoking
`backend/scripts/deploy.sh`.

The architecture doc (`MCS_CoWorker_v3_Architecture.md`, kept
outside the repo) describes the intended end-state design. This
document describes what is actually wired today. Where the two
disagree, an ADR in `docs/decisions/` records the deliberate
deviation and supersedes the architecture doc until a future ADR
reverses it.

## 1. Environment shape

- **Droplet:** `coworker-v3-prod-syd1`, DigitalOcean SYD1,
  Ubuntu 24.04. Hostname verified 2026-05-18.
- **Domain:** `coworker.mcands.com.au` (Caddy → 127.0.0.1:8001).
- **Application user:** `coworker` (and group `coworker`).
- **Code root:** `/opt/coworker/current` (symlink) →
  `/opt/coworker/releases/<sha>/`.
- **Postgres 16 + pgvector** (local socket, role `coworker`).
- **Redis 7** (local socket).

### 1.1 Release directory layout

A release directory is the **extracted contents of the repo at a
specific git ref, with no `.git`**:

```
/opt/coworker/releases/<sha>/
├── pyproject.toml          ← repo ROOT
├── uv.lock
├── .env.example
├── .gitignore
├── README.md
├── .venv/                  ← built by `uv sync` at release root
├── backend/
│   ├── alembic.ini
│   ├── migrations/
│   └── coworker/
├── infra/
├── frontend/
└── docs/
```

Shape constraints downstream code must respect:

- **`pyproject.toml` is at the release ROOT, not under `backend/`.**
  All `uv sync` invocations must run from
  `/opt/coworker/releases/<sha>/`. This is the deviation that
  broke the rolled-back deploy: the prior `deploy.sh` ran
  `cd .../backend && uv sync`, which fails when pyproject lives
  one level up.
- **The release directory has no `.git`.** The LOCAL deploy path
  uses `git archive` to extract a tracked-files snapshot. Nothing
  in the deployed code may assume `.git` is present at runtime
  (e.g. no `git rev-parse` at runtime).
- **`.venv/` lives at the release root**, not under `backend/`.
  The systemd units invoke its interpreter directly:
  `/opt/coworker/current/.venv/bin/python -m coworker.workers.<module>`.

### 1.2 Credentials

All `coworker-*` units read environment variables from a single
plaintext file:

```
-rw-r----- root:coworker /opt/coworker/shared/credentials/coworker.env
                         (mode 0640)
```

This is **plain `EnvironmentFile=`**, not age-encrypted. The
architecture doc §2.3 prescribes an age-encrypted-env design which
is the intended future target, deferred until Phase 14 key-backup
tooling exists. See
**[ADR-0002: production runs plain EnvironmentFile, not age (deferred)](decisions/0002-plain-env-file-deferred-age.md)**
for the full rationale and revisit triggers.

### 1.3 The systemd fleet

Twelve units in total — 7 services + 5 timers (5 of the services
have an associated timer):

| Unit | Type | Purpose | Cadence |
|---|---|---|---|
| `coworker-api.service` | simple | FastAPI / uvicorn on `127.0.0.1:8001` | always on |
| `coworker-worker.service` | simple | plugin event worker | always on |
| `coworker-dispatch.service` | oneshot | approval dispatch | timer (1 min) |
| `coworker-scheduler.service` | oneshot | scheduled-trigger sweep | timer (1 min) |
| `coworker-subscribe.service` | oneshot | Graph subscription sweep | timer (30 min) |
| `coworker-backfill.service` | oneshot | missed-notification backfill | timer (5 min) |
| `coworker-delivery-confirm.service` | oneshot | delivery-status confirmation sweep | timer (30 min) |
| `coworker-dispatch.timer` | timer | drives `coworker-dispatch.service` | — |
| `coworker-scheduler.timer` | timer | drives `coworker-scheduler.service` | — |
| `coworker-subscribe.timer` | timer | drives `coworker-subscribe.service` | — |
| `coworker-backfill.timer` | timer | drives `coworker-backfill.service` | — |
| `coworker-delivery-confirm.timer` | timer | drives `coworker-delivery-confirm.service` | — |

All units share the same canonical block:

```ini
[Service]
User=coworker
Group=coworker
WorkingDirectory=/opt/coworker/current/backend
EnvironmentFile=/opt/coworker/shared/credentials/coworker.env
ExecStart=/opt/coworker/current/.venv/bin/<binary> …
```

Plus the standard hardening block: `NoNewPrivileges=true`,
`PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`,
`ReadWritePaths=/var/lib/coworker /var/log/coworker`,
`ProtectKernelTunables=true`, `ProtectKernelModules=true`,
`ProtectControlGroups=true`,
`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
`RestrictNamespaces=true`, `LockPersonality=true`,
`RestrictRealtime=true`, `SystemCallArchitectures=native`.

## 2. Deploy paths

`backend/scripts/deploy.sh` selects between two paths based on the
host it's invoked from:

| Path | Trigger | Mechanism |
|---|---|---|
| **LOCAL** (default since 2026-05-18) | `hostname == coworker-v3-prod-syd1` AND `/opt/coworker/releases/` exists | builds release locally via `git archive`; no ssh, no rsync |
| **PUSH** (legacy escape hatch) | anything else | rsync-over-ssh to `coworker-v3` alias, ssh heredocs for build/install |

Both paths converge on the same on-droplet end state: a fresh
release directory, the `current` symlink swung, units enabled and
running.

### 2.1 LOCAL path — droplet-native (preferred)

Run from `~/code/mcs-coworker-v3/` on the droplet itself:

```bash
./backend/scripts/deploy.sh                  # deploys HEAD short SHA
./backend/scripts/deploy.sh <sha-or-tag>     # deploys a specific ref
```

What it does, in order:

1. **Refuse on dirty working tree.** `git diff-index --quiet HEAD --`
   — only committed code may ship.
2. **Build release.** `git archive --format=tar <sha> | tar -x -C
   /opt/coworker/releases/<sha>/`. Produces a no-`.git` snapshot
   matching the existing `releases/a81083a/` shape.
3. **Build venv at release root.** `cd <release> && uv sync
   --python python3.12` — produces `<release>/.venv/`.
4. **Pre-deploy DB backup.** `pg_dump -Fc -d coworker >
   /tmp/pre-deploy-backup-<timestamp>.dump`, sha256sum logged.
   Taken **before** any DB or symlink mutation — covers both the
   migration and the symlink-swap rollback windows.
5. **Migration guard.** Capture the live DB alembic head (from
   `alembic_version`) and the release's alembic head (from
   `<release>/.venv/bin/alembic heads`). Print both.
   - If they are **equal**, the upgrade is provably a no-op:
     proceed silently. This is the default case for any deploy
     whose only schema changes were already applied by a prior
     run.
   - If they **differ**, the deploy refuses unless the operator
     has set `DEPLOY_ALLOW_MIGRATION=1`. On refusal the pending
     revision range is printed (`alembic history -r <db>:<rel>`)
     followed by:
     `refusing to migrate live DB while previous code is still
     active; re-run with DEPLOY_ALLOW_MIGRATION=1 to acknowledge
     the migration is backward-compatible with the running
     release.` The script exits 1 having performed no DB or
     symlink mutation (the §2b pg_dump is read-only on the DB).
   - The gate enforces — rather than trusts — the backward-
     compatibility invariant the architecture doc and the script
     header already declare: a migration must coexist safely
     with the previous release's code for the window between
     §2c (migrate) and §3c (symlink swap).
6. **Migrations.** `cd <release>/backend &&
   <release>/.venv/bin/alembic upgrade head`. Uses the explicit
   venv binary (matches the venv-resolution mechanism the systemd
   units use). Must be a no-op when the DB is already at head;
   alembic does not error on "already at head".
7. **Install systemd units.** `install -m 0644 -o root -g root -C
   <release>/infra/systemd/coworker-*.{service,timer}
   /etc/systemd/system/`. The `-C` flag skips unchanged files so
   identical-mtime no-op deploys stay quiet.
8. **`daemon-reload`.** Running services keep their current
   `ExecStart` until restarted in §11.
9. **Capture rollback target.** `PREV_RELEASE=$(readlink -f
   /opt/coworker/current)`.
10. **Atomic symlink swap.** `ln -sfn <release>
    /opt/coworker/current`.
11. **Restart services:** `coworker-api` (reload-or-restart),
    `coworker-worker` (restart), each timer-activated oneshot
    (enable + restart — one round of work fires immediately as a
    sanity check), each timer (enable + start).
12. **Negative failure-scan.** `systemctl list-units --failed
    --type=service,timer 'coworker-*'` — if any failed, dump 30
    lines of `journalctl -u <unit>` per failure and roll back.
13. **Positive `is-active` assertions** (new in this rework — the
    fleet-verify from the deploy reconciliation task §4.2#8):
    - Each always-on service must be `active`.
    - Each timer-activated oneshot must have `Result=success`
      (oneshots return `inactive` post-completion; the truthful
      success signal is `Result`, not `is-active`).
    - Each timer must be `active`.
    - On any failure, dump 50 lines of `journalctl -u <unit>` per
      failure and roll back.
14. **Health smoke test.** `curl -fsS
    https://coworker.mcands.com.au/health`, assert presence of the
    `"version"` key. Roll back on failure.
15. **Final-state printout.** `systemctl list-units 'coworker-*'`
    + `list-timers 'coworker-*'`.

#### Rollback semantics

On any failure between the symlink swap (step 10) and the end of
verification (step 14), the script:

1. Re-points `/opt/coworker/current` at `PREV_RELEASE`.
2. `reload-or-restart coworker-api.service` so production traffic
   immediately returns to the previous code.
3. `systemctl stop` each sibling unit (see paragraph below for
   which) so they don't keep running against the rolled-back
   symlink.
4. Logs the pre-deploy DB backup path and each unit's pre-deploy
   `is-enabled` state (captured at the start of the deploy).
5. Exits 1.

Sibling units (`coworker-worker`, the 5 timer-activated oneshots,
and the 5 timers) are **stopped** by the rollback (`systemctl
stop`), but **not** disabled. Their pre-deploy `is-enabled` state
is captured at the start of the deploy and printed for the
operator. Stopping them ensures they don't keep running against
the rolled-back symlink (inspectable-and-inert); leaving them
enabled means `systemctl status <unit>` and `journalctl -u <unit>`
work without further setup.

### 2.2 PUSH path — workstation rsync (legacy)

Triggered automatically when the script runs anywhere other than
the production droplet. Behaviour preserved verbatim from the
pre-2026-05-18 script: `ssh -p 2202 coworker-v3`, two `rsync`
blocks (one for `./backend/`, one for `./infra/`), then heredoc'd
`uv sync` + `alembic upgrade head`, then heredoc'd
`daemon-reload` + symlink swap + restarts, then a remote
fail-scan and smoke test.

Notes for anyone reaching for this path:

- Requires SSH alias `coworker-v3` configured (port 2202, key
  auth) in the operator's `~/.ssh/config`.
- **The legacy heredoc still runs `cd $RELEASE_DIR/backend && uv
  sync`**, which is broken against the current root-`pyproject.toml`
  layout. The PUSH path is kept as a structural escape hatch but
  **should not be invoked against current `main`** without first
  fixing the heredoc to run `uv sync` from `$RELEASE_DIR`. The
  intended future move is to bring the PUSH path's build step
  into line with LOCAL once we trust LOCAL through a few deploys.
- The PUSH path does not implement the positive `is-active`
  fleet-verify (step 13 in LOCAL). It performs only the negative
  fail-scan. If you use this path, run the manual verify in §3
  of this document after it completes.
- The PUSH path does not implement the migration guard (step 5
  in LOCAL). It runs `alembic upgrade head` unconditionally.

## 3. Verifying the fleet by hand

After any deploy (or to audit current state), the verify is:

```bash
# Negative: nothing failed
systemctl list-units --failed --type=service,timer 'coworker-*'

# Positive: each always-on service is active
systemctl is-active coworker-api.service coworker-worker.service

# Positive: each oneshot's last run succeeded
for u in coworker-dispatch coworker-scheduler coworker-subscribe \
         coworker-backfill coworker-delivery-confirm; do
  echo "$u: Result=$(systemctl show -p Result --value ${u}.service)"
done

# Positive: each timer is active and has a next-fire scheduled
systemctl list-timers --all 'coworker-*'

# Application: /health responds 200 with a version
curl -s https://coworker.mcands.com.au/health | python3 -m json.tool
```

The combination of these checks is what `deploy.sh` automates in
§4–§6 of the LOCAL path.

> The form `systemctl --failed 'coworker-*'` (without
> `list-units`) is **not** valid syntax — `--failed` is a flag,
> not a verb. Use `systemctl list-units --failed …` as shown
> above. The deploy script and this doc use the valid form
> throughout.

## 4. Cross-references

- **ADR-0002** — plain `EnvironmentFile=` vs age, deferred.
  `docs/decisions/0002-plain-env-file-deferred-age.md`.
- **Architecture doc §2.3** — age-encrypted env design (intended
  future target; current state is the ADR-0002 plain-env
  decision). Kept outside the repo by design.
- **`infra/systemd/`** — the unit files this doc describes.
- **`backend/scripts/deploy.sh`** — the script this doc
  describes.
