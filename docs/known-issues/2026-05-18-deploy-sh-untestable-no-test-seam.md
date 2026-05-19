# deploy.sh has 14 hardcoded production references and no test seam

- **Status:** PARTIALLY-RESOLVED (2026-05-19, commit 055e75d) — see Resolution below; Bug B residual remains, tracked separately
- **Discovered:** 2026-05-18 (during v3 deploy-hardening)
- **Severity:** HIGH — root cause of the entire 2-day failure sequence; this is the headline
- **Owner:** Elio (unassigned action)

## Finding
The committed `backend/scripts/deploy.sh` LOCAL path has 14 hardcoded
production references (production symlink path, releases dir prefix,
DB name, `/etc/systemd/system/`, real `daemon-reload`, real unit-name
arrays, etc. — none parameterised). There is no dry-run, no isolation
seam. The script CANNOT be exercised anywhere except against real
production.

## Evidence
Enumerated during the Step-4 proof-rig attempt: items include L31
`RELEASE_DIR="/opt/coworker/releases/$RELEASE"` (prefix literal), L192
`PREV_RELEASE=$(readlink -f /opt/coworker/current)`, L194
`ln -sfn "$RELEASE_DIR" /opt/coworker/current`, L110/131
`-d coworker`, L181-184 install to `/etc/systemd/system/`, L189
`daemon-reload`, L38-55 unit-name arrays. Input-only redirection of
the unmodified script was proven impossible (no
`${CURRENT_SYMLINK:-...}` style seams).

## Root cause
The script was written to do one thing against one host and never
given a test/dry-run mode. Every defect (alembic env-loading, missing
PUBLIC_WEBHOOK_BASE_URL, the §3 rollback gap) therefore could only be
discovered in production — which is exactly what happened, five
times.

## Not yet decided / open question
The fix. This becomes its own deliberate, discovery-first task (the
NEXT task after this findings log). Candidate direction:
parameterise the production references behind env-overrides that
DEFAULT to today's exact literals (so production behaviour is
unchanged) enabling a throwaway-target dry-run. NOT to be designed
here — flagged as the priority task.

## Out of scope for the finding
Implementing the fix. This file establishes WHY it is the
highest-leverage next work.

## Resolution (2026-05-19, commit 055e75d) — PARTIALLY-RESOLVED
Parameterize-with-transparent-defaults seams added in commit 055e75d.
deploy.sh is now verifiable pre-production for **2 of 3** incident-bug
classes:

- **Bug A — alembic running with no env** (the 09fad28 precursor):
  catchable via the throwaway harness.
- **Bug C — §3 ERR trap firing on a unit-start failure** (the rollback
  coverage gap closed by 3051d14): caught by `proof_4b` running the
  exact 09fad28-shape failure against shimmed systemctl without real
  systemd.

Proven by:

- `backend/tests/scripts/proof_4a_production_transparency.sh` —
  extracts the manual-rollback block from deploy.sh itself, asserts
  unset-env resolution to literal `sudo`/`systemctl` with zero
  unexpanded tokens. This is the gate protecting the working v3
  deploy.
- `backend/tests/scripts/proof_4b_throwaway_harness.sh` — runs the
  seamed script end-to-end against a `mktemp` throwaway with shims,
  proves real production state invariant (production symlink,
  `/etc/systemd/system/`, real DB, real units all untouched), and
  catches Bug C.

**Production-transparency invariant (non-negotiable):** a normal
`./backend/scripts/deploy.sh <sha>` with NO `DEPLOY_*` env set is
byte-for-byte identical to pre-seam behaviour. Every seam uses
`${VAR:-<original literal>}` and proof_4a is the textual gate.

**Scope note:** the PUSH path (legacy rsync/ssh workstation-push
escape hatch) was NOT seamed. It is a structurally broken legacy
path and out of scope for this work.

## Residual — explicit, NOT resolved
**Bug B — worker-required-env validation** (e.g. missing
`PUBLIC_WEBHOOK_BASE_URL`) is NOT caught by these seams. The seams
make the deploy script verifiable; they do not validate the
`.env`-file contract against the keys the worker actually requires
at import time. This is the same underlying problem as
[`2026-05-18-env-file-validated-against-wrong-contract.md`](2026-05-18-env-file-validated-against-wrong-contract.md),
which remains **OPEN** and is the natural place a future Bug-B fix
is tracked. Marking this finding fully RESOLVED would overclaim;
status is therefore PARTIALLY-RESOLVED until that cross-referenced
finding is itself closed.
