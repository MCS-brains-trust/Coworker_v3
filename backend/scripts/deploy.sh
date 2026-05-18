#!/usr/bin/env bash
set -euo pipefail

# Deploy MC & S CoWorker v3.
#
# Two paths, selected by run-location:
#
#   LOCAL  — runs ON the droplet (coworker-v3-prod-syd1) from a local
#            git checkout. Builds the release via `git archive` (no
#            rsync, no ssh). This is the default since 2026-05-18.
#
#   PUSH   — legacy workstation-push path. Preserved as an escape
#            hatch; runs over ssh + rsync. Used when this script is
#            invoked from a developer machine (not the droplet).
#
# Usage:  ./backend/scripts/deploy.sh [git-sha-or-tag]
# Default: current HEAD short SHA.
#
# See docs/ENVIRONMENT_AND_DEPLOY.md for the full deploy spec and
# rollback procedure.

# ----- guard: must run from repo root -----
if [[ ! -d ./backend ]] || [[ ! -d ./infra/systemd ]]; then
  echo "Error: deploy.sh must be run from the repo root."
  echo "Could not find ./backend or ./infra/systemd at $(pwd)."
  exit 1
fi

RELEASE="${1:-$(git rev-parse --short HEAD)}"
DOMAIN="coworker.mcands.com.au"
RELEASE_DIR="/opt/coworker/releases/$RELEASE"

# Unit-list constants. Shared by both deploy paths.
#   ALWAYS_ON_SERVICES        — long-running; reload-or-restart / restart
#   TIMER_ACTIVATED_SERVICES  — oneshots; plain restart (one round of
#                               work, doubles as a sanity check)
#   TIMERS                    — enable --now
ALWAYS_ON_SERVICES=(
  coworker-api.service
  coworker-worker.service
)
TIMER_ACTIVATED_SERVICES=(
  coworker-dispatch.service
  coworker-scheduler.service
  coworker-subscribe.service
  coworker-backfill.service
  coworker-delivery-confirm.service
)
TIMERS=(
  coworker-dispatch.timer
  coworker-scheduler.timer
  coworker-subscribe.timer
  coworker-backfill.timer
  coworker-delivery-confirm.timer
)

# ----- run-location detection -----
# LOCAL path triggers when both are true:
#   1. hostname matches the production droplet
#   2. /opt/coworker/releases exists (droplet was provisioned)
# Otherwise fall back to legacy workstation-push.
#
# Hostname verified 2026-05-18 via `hostname` and `hostnamectl`:
#   coworker-v3-prod-syd1
DROPLET_HOSTNAME="coworker-v3-prod-syd1"
if [[ "$(hostname)" == "$DROPLET_HOSTNAME" ]] && [[ -d /opt/coworker/releases ]]; then
  DEPLOY_MODE="local"
else
  DEPLOY_MODE="push"
fi

echo "Deploying $RELEASE (mode: $DEPLOY_MODE) to $DOMAIN..."

# =========================================================================
# LOCAL path — runs on the droplet, no rsync, no ssh.
# =========================================================================
if [[ "$DEPLOY_MODE" == "local" ]]; then

  # ----- refuse if tracked working tree is dirty -----
  if ! git diff-index --quiet HEAD --; then
    echo "Error: tracked working tree is dirty. Commit or stash before deploy."
    git status --short
    exit 1
  fi

  RESOLVED_SHA=$(git rev-parse "$RELEASE")

  # Capture each unit's pre-deploy enabled state. Printed on
  # rollback so the operator sees what state the units were in
  # pre-deploy. Rollback stops sibling units (`systemctl stop`)
  # but does not change their enabled state.
  declare -A PRE_DEPLOY_ENABLED
  for unit in "${ALWAYS_ON_SERVICES[@]}" "${TIMER_ACTIVATED_SERVICES[@]}" "${TIMERS[@]}"; do
    PRE_DEPLOY_ENABLED["$unit"]=$(systemctl is-enabled "$unit" 2>/dev/null || echo "unknown")
  done

  # ----- §1 build release via `git archive` (no .git, matches a81083a shape) -----
  sudo -u coworker mkdir -p "$RELEASE_DIR"
  git archive --format=tar "$RESOLVED_SHA" | sudo -u coworker tar -x -C "$RELEASE_DIR"

  # ----- §2a build venv at release ROOT -----
  # pyproject.toml + uv.lock live at the repo root, not under backend/.
  # uv sync must run from $RELEASE_DIR (root), producing $RELEASE_DIR/.venv.
  sudo -u coworker bash -c "cd '$RELEASE_DIR' && uv sync --python python3.12"

  # ----- §2b pre-deploy pg_dump insurance -----
  # Taken BEFORE alembic upgrade (a destructive DB operation) and
  # before the symlink swap. Custom-format dump, sha256sum logged.
  BACKUP_FILE="/tmp/pre-deploy-backup-$(date +%Y%m%d-%H%M%S).dump"
  sudo -u postgres pg_dump -Fc -d coworker > "$BACKUP_FILE"
  echo "Pre-deploy DB backup:"
  ls -lh "$BACKUP_FILE"
  sha256sum "$BACKUP_FILE"

  # ----- §2c-pre migration guard -----
  # The symlink swap (§3c) happens AFTER alembic upgrade (§2c), so
  # in the window between the two the still-running OLD code sees
  # the NEW schema. That is safe only when the migration is
  # backward-compatible with the previous release (additive
  # columns with defaults, no destructive renames) — the invariant
  # the architecture doc and this script's header already declare.
  # This guard enforces that invariant instead of trusting it.
  #
  # If the release's alembic head == live DB head, the upgrade is
  # provably a no-op and we proceed silently. Otherwise the deploy
  # refuses unless the operator has explicitly acknowledged the
  # migration is backward-compatible by setting
  # DEPLOY_ALLOW_MIGRATION=1. This gate performs no DB or symlink
  # mutation; on refusal nothing past §2b's read-only pg_dump has
  # touched the system.
  DB_HEAD=$(sudo -u postgres psql -d coworker -tAc "select version_num from alembic_version;" | tr -d '[:space:]' || true)
  RELEASE_HEAD=$(sudo -u coworker bash -c "cd '$RELEASE_DIR/backend' && '$RELEASE_DIR/.venv/bin/alembic' heads" | head -1 | awk '{print $1}')
  echo "Live DB alembic head:    ${DB_HEAD:-<none>}"
  echo "Release alembic head:    ${RELEASE_HEAD:-<none>}"

  if [[ "$DB_HEAD" != "$RELEASE_HEAD" ]]; then
    if [[ "${DEPLOY_ALLOW_MIGRATION:-0}" != "1" ]]; then
      echo ""
      echo "Pending migrations between DB head and release head:"
      sudo -u coworker bash -c "cd '$RELEASE_DIR/backend' && '$RELEASE_DIR/.venv/bin/alembic' history -r '${DB_HEAD:-base}:$RELEASE_HEAD'" || true
      echo ""
      echo "refusing to migrate live DB while previous code is still active;"
      echo "re-run with DEPLOY_ALLOW_MIGRATION=1 to acknowledge the migration"
      echo "is backward-compatible with the running release."
      exit 1
    fi
    echo "DEPLOY_ALLOW_MIGRATION=1 set; proceeding with migration."
  fi

  # ----- §2c alembic upgrade head -----
  # alembic.ini lives in backend/. Use the EXPLICIT venv binary
  # (not `uv run`) to match the venv-resolution mechanism the
  # systemd units use, and to remove any chance `uv run` re-resolves
  # the environment at this irreversible step. For a release whose
  # head matches the live DB head this is a no-op; alembic does not
  # error on "already at head".
  sudo -u coworker bash -c "cd '$RELEASE_DIR/backend' && '$RELEASE_DIR/.venv/bin/alembic' upgrade head"

  # ----- §3a install systemd units -----
  # `-C` skips identical files so mtimes stay stable across no-op deploys.
  sudo install -m 0644 -o root -g root -C \
    "$RELEASE_DIR/infra/systemd/"coworker-*.service \
    "$RELEASE_DIR/infra/systemd/"coworker-*.timer \
    /etc/systemd/system/

  # ----- §3b daemon-reload -----
  # Reloads systemd's view of the unit files. Running services keep
  # their current ExecStart until the restarts in §3d–§3f.
  sudo systemctl daemon-reload

  # ----- §3c capture rollback target, then atomic symlink swap -----
  PREV_RELEASE=$(readlink -f /opt/coworker/current)
  echo "Previous release (rollback target): $PREV_RELEASE"
  sudo ln -sfn "$RELEASE_DIR" /opt/coworker/current

  # Rollback helper — called from any failure gate below.
  #   1. Re-point the symlink at the previous release.
  #   2. Restart api so traffic returns immediately to the previous
  #      code on the previous release.
  #   3. STOP (not disable) the 6 siblings + 5 timers. This is their
  #      first-ever start; leaving them running against the
  #      rolled-back symlink is an inconsistent live state.
  #      Inspectable-and-inert beats inspectable-and-live. The
  #      is-enabled state is left as the operator finds it, so
  #      `systemctl status <unit>` and `journalctl -u <unit>` work
  #      without further setup.
  #   4. Surface diagnostics (backup path, pre-deploy enabled state).
  rollback_to_prev() {
    echo ""
    echo "Rolling back: /opt/coworker/current -> $PREV_RELEASE"
    sudo ln -sfn "$PREV_RELEASE" /opt/coworker/current
    sudo systemctl reload-or-restart coworker-api.service || true

    # Stop (NOT disable) every sibling that this deploy started.
    # Use `|| true` so a unit that never came up doesn't trip set -e.
    for unit in coworker-worker.service "${TIMER_ACTIVATED_SERVICES[@]}" "${TIMERS[@]}"; do
      sudo systemctl stop "$unit" || true
    done

    echo ""
    echo "Pre-deploy DB backup retained at: $BACKUP_FILE"
    echo "Sibling units stopped (still enabled, for inspection)."
    echo "Their pre-deploy is-enabled state was:"
    for unit in "${!PRE_DEPLOY_ENABLED[@]}"; do
      echo "  $unit = ${PRE_DEPLOY_ENABLED[$unit]}"
    done
  }

  # ----- §3d enable + restart always-on services -----
  sudo systemctl enable coworker-api.service
  sudo systemctl reload-or-restart coworker-api.service

  sudo systemctl enable coworker-worker.service
  sudo systemctl restart coworker-worker.service

  # ----- §3e enable + restart timer-activated oneshots -----
  # Each `restart` fires one round of work immediately against the
  # new release, doubling as a wiring sanity check.
  for unit in "${TIMER_ACTIVATED_SERVICES[@]}"; do
    sudo systemctl enable "$unit"
    sudo systemctl restart "$unit"
  done

  # ----- §3f enable + start timers -----
  for unit in "${TIMERS[@]}"; do
    sudo systemctl enable --now "$unit"
  done

  # ----- §4 negative failure-scan -----
  # Catches units that started and crashed loudly. The valid form is
  # `list-units --failed --type=service,timer 'coworker-*'`;
  # `systemctl --failed 'coworker-*'` (no `list-units`) is not.
  FAILED_UNITS=$(systemctl list-units --failed --no-pager --no-legend --plain --type=service,timer 'coworker-*' | awk '{print $1}')
  if [[ -n "$FAILED_UNITS" ]]; then
    echo ""
    echo "Deploy failed: one or more coworker units are in failed state."
    echo ""
    echo "Failed units:"
    echo "$FAILED_UNITS" | sed 's/^/  /'
    echo ""
    while IFS= read -r unit; do
      [[ -z "$unit" ]] && continue
      echo "--- journalctl -u $unit --no-pager -n 30 ---"
      journalctl -u "$unit" --no-pager -n 30 || true
      echo ""
    done <<< "$FAILED_UNITS"
    rollback_to_prev
    exit 1
  fi

  # ----- §5 positive is-active assertions -----
  # The §4 fail-scan catches loud crashes, but the 6 siblings + 5
  # timers have never run before this deploy: "not failed" is not
  # the same as "started and working". Assert positively per unit.
  POSITIVE_FAILURES=()

  # Long-running services: must be active.
  for unit in "${ALWAYS_ON_SERVICES[@]}"; do
    if ! systemctl is-active --quiet "$unit"; then
      state=$(systemctl is-active "$unit" 2>&1 || true)
      POSITIVE_FAILURES+=("$unit (expected active, got $state)")
    fi
  done

  # Timer-activated services are oneshots: after `restart` they may
  # have already exited cleanly (ActiveState=inactive,
  # SubState=dead). `is-active` returns 0 only for currently-running
  # units, so assert Result=success instead.
  for unit in "${TIMER_ACTIVATED_SERVICES[@]}"; do
    result=$(systemctl show -p Result --value "$unit")
    if [[ "$result" != "success" ]]; then
      POSITIVE_FAILURES+=("$unit (expected Result=success, got Result=$result)")
    fi
  done

  # Timers: must be active (loaded + waiting / running).
  for unit in "${TIMERS[@]}"; do
    if ! systemctl is-active --quiet "$unit"; then
      state=$(systemctl is-active "$unit" 2>&1 || true)
      POSITIVE_FAILURES+=("$unit (expected active, got $state)")
    fi
  done

  if [[ ${#POSITIVE_FAILURES[@]} -gt 0 ]]; then
    echo ""
    echo "Deploy failed: one or more coworker units did not come up cleanly."
    echo ""
    for f in "${POSITIVE_FAILURES[@]}"; do
      echo "  $f"
    done
    echo ""
    for f in "${POSITIVE_FAILURES[@]}"; do
      unit="${f%% *}"
      echo "--- journalctl -u $unit --no-pager -n 50 ---"
      journalctl -u "$unit" --no-pager -n 50 || true
      echo ""
    done
    rollback_to_prev
    exit 1
  fi

  # ----- §6 health smoke test -----
  sleep 3
  echo ""
  echo "Smoke-testing https://${DOMAIN}/health ..."
  if ! HEALTH_JSON=$(curl -fsS "https://${DOMAIN}/health"); then
    echo "Health check failed (curl)."
    rollback_to_prev
    exit 1
  fi
  echo "$HEALTH_JSON" | python3 -m json.tool
  if ! echo "$HEALTH_JSON" | grep -q '"version"'; then
    echo "Health response missing 'version' key."
    rollback_to_prev
    exit 1
  fi

  # ----- §7 final state -----
  echo ""
  echo "=== Final state ==="
  systemctl list-units --all --no-pager --no-legend --type=service,timer 'coworker-*'
  echo ""
  echo "=== Timer schedule ==="
  systemctl list-timers --all --no-pager 'coworker-*'
  echo ""
  echo "Deployed $RELEASE successfully."
  exit 0
fi

# =========================================================================
# PUSH path (legacy) — workstation-push via ssh + rsync.
# Preserved as an escape hatch for invocation from a developer machine.
# Not the primary path post-2026-05-18; see docs/ENVIRONMENT_AND_DEPLOY.md.
# Behaviour preserved from the pre-reconciliation script.
# =========================================================================
HOST="coworker-v3"
SSH_PORT=2202

# 1. Sync code + infra to the release directory.
ssh -p $SSH_PORT "$HOST" \
  "sudo -u coworker mkdir -p $RELEASE_DIR/backend $RELEASE_DIR/infra"

rsync -az --delete -e "ssh -p $SSH_PORT" \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude '__pycache__' --exclude '*.pyc' \
  ./backend/ "$HOST:$RELEASE_DIR/backend/"

rsync -az --delete -e "ssh -p $SSH_PORT" \
  ./infra/ "$HOST:$RELEASE_DIR/infra/"

# 2. Build deps + run DB migrations (against the still-old running code).
#    NOTE: legacy heredoc runs `uv sync` from $RELEASE_DIR/backend.
#    The current repo has pyproject.toml at the repo root, not under
#    backend/, so this path is broken against current main and is
#    retained only as a structural escape hatch. Run the LOCAL path
#    from the droplet for current main; see docs/ENVIRONMENT_AND_DEPLOY.md.
ssh -p $SSH_PORT "$HOST" "sudo -u coworker bash" <<INNER
  set -euo pipefail
  cd $RELEASE_DIR/backend
  uv sync --python python3.12
  uv run alembic upgrade head
INNER

# 3. Install/refresh systemd unit files, swap the symlink, restart units.
ssh -p $SSH_PORT "$HOST" bash <<EOF
  set -euo pipefail

  sudo install -m 0644 -o root -g root -C \\
    $RELEASE_DIR/infra/systemd/coworker-*.service \\
    $RELEASE_DIR/infra/systemd/coworker-*.timer \\
    /etc/systemd/system/

  sudo systemctl daemon-reload

  sudo ln -sfn $RELEASE_DIR /opt/coworker/current

  sudo systemctl enable coworker-api.service
  sudo systemctl reload-or-restart coworker-api.service

  sudo systemctl enable coworker-worker.service
  sudo systemctl restart coworker-worker.service

  for unit in ${TIMER_ACTIVATED_SERVICES[@]}; do
    sudo systemctl enable "\$unit"
    sudo systemctl restart "\$unit"
  done

  for unit in ${TIMERS[@]}; do
    sudo systemctl enable --now "\$unit"
  done
EOF

# 4. Failure gate.
FAILED_UNITS=$(ssh -p $SSH_PORT "$HOST" \
  "systemctl list-units --failed --no-pager --no-legend --plain --type=service,timer 'coworker-*' | awk '{print \$1}'" \
  || true)

if [[ -n "$FAILED_UNITS" ]]; then
  echo ""
  echo "Deploy failed: one or more coworker units are in failed state."
  echo ""
  echo "Failed units:"
  echo "$FAILED_UNITS" | sed 's/^/  /'
  echo ""
  while IFS= read -r unit; do
    [[ -z "$unit" ]] && continue
    echo "--- journalctl -u $unit --no-pager -n 20 ---"
    ssh -p $SSH_PORT "$HOST" "journalctl -u $unit --no-pager -n 20" || true
    echo ""
  done <<< "$FAILED_UNITS"
  echo "The new release is at $RELEASE_DIR; the current symlink already"
  echo "points there. Investigate the failed units above; if rollback is"
  echo "needed, re-point /opt/coworker/current at the previous release"
  echo "directory and restart the affected services."
  exit 1
fi

# 5. /health smoke test.
sleep 3
echo ""
echo "Smoke-testing https://${DOMAIN}/health ..."
curl -fsS "https://${DOMAIN}/health" | python3 -m json.tool

# 6. Final state printout.
echo ""
echo "=== Final state on $HOST ==="
ssh -p $SSH_PORT "$HOST" \
  "systemctl list-units --all --no-pager --no-legend --type=service,timer 'coworker-*'"

echo ""
echo "=== Timer schedule on $HOST ==="
ssh -p $SSH_PORT "$HOST" \
  "systemctl list-timers --all --no-pager 'coworker-*'"

echo ""
echo "Deployed $RELEASE successfully."
