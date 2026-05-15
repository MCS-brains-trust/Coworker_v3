#!/usr/bin/env bash
set -euo pipefail

# Deploy MC & S CoWorker v3 to the production droplet.
#
# Installs and enables all systemd units in infra/systemd/. Idempotent —
# safe to run repeatedly; only changed files transfer, and only changed
# units pick up new content (daemon-reload is unconditional, but running
# services only re-read their ExecStart on restart).
#
# Run from the repo root on your local workstation.
# Usage: ./backend/scripts/deploy.sh [git-sha-or-tag]
# Defaults to the current HEAD SHA when no arg is given.

if [[ ! -d ./backend ]] || [[ ! -d ./infra/systemd ]]; then
  echo "Error: deploy.sh must be run from the repo root."
  echo "Could not find ./backend or ./infra/systemd at $(pwd)."
  exit 1
fi

RELEASE="${1:-$(git rev-parse --short HEAD)}"
HOST="coworker-v3"
SSH_PORT=2202
RELEASE_DIR="/opt/coworker/releases/$RELEASE"
DOMAIN="coworker.mcands.com.au"

# The coworker-* unit fleet. Split drives restart semantics:
#   ALWAYS_ON_SERVICES       — long-running; explicit reload-or-restart / restart
#   TIMER_ACTIVATED_SERVICES — oneshots; plain restart (fires one round of work
#                              immediately, doubles as a sanity check)
#   TIMERS                   — enable + start; no restart concept
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

echo "Deploying $RELEASE to $HOST..."

# ---------------------------------------------------------------------------
# 1. Sync code + infra to the release directory.
# ---------------------------------------------------------------------------
ssh -p $SSH_PORT "$HOST" \
  "sudo -u coworker mkdir -p $RELEASE_DIR/backend $RELEASE_DIR/infra"

rsync -az --delete -e "ssh -p $SSH_PORT" \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude '__pycache__' --exclude '*.pyc' \
  ./backend/ "$HOST:$RELEASE_DIR/backend/"

rsync -az --delete -e "ssh -p $SSH_PORT" \
  ./infra/ "$HOST:$RELEASE_DIR/infra/"

# ---------------------------------------------------------------------------
# 2. Build deps + run DB migrations (against the still-old running code).
#    Migrations must stay backwards-compatible with the previous release
#    (additive columns with defaults, no destructive renames) so the old
#    code surviving until step 3d coexists safely with the new schema.
# ---------------------------------------------------------------------------
ssh -p $SSH_PORT "$HOST" "sudo -u coworker bash" <<INNER
  set -euo pipefail
  cd $RELEASE_DIR/backend
  uv sync --python python3.12
  uv run alembic upgrade head
INNER

# ---------------------------------------------------------------------------
# 3. Install/refresh systemd unit files, swap the current symlink, then
#    bring units to their target state. Single SSH session to minimise
#    round-trips; set -e fails fast on any step.
# ---------------------------------------------------------------------------
ssh -p $SSH_PORT "$HOST" bash <<EOF
  set -euo pipefail

  # 3a. Install (or refresh) unit files. -C skips identical files so
  #     mtimes are stable across no-op deploys.
  sudo install -m 0644 -o root -g root -C \\
    $RELEASE_DIR/infra/systemd/coworker-*.service \\
    $RELEASE_DIR/infra/systemd/coworker-*.timer \\
    /etc/systemd/system/

  # 3b. Reload systemd's view of unit files. Running services continue
  #     with their current ExecStart until they're restarted below.
  sudo systemctl daemon-reload

  # 3c. Atomic symlink swap: /opt/coworker/current now points to the
  #     new release. The service restarts in 3d pick this up because
  #     each unit's ExecStart resolves /opt/coworker/current/ fresh.
  sudo ln -sfn $RELEASE_DIR /opt/coworker/current

  # 3d. Always-on services. enable (no --now) then reload-or-restart /
  #     restart so the refresh happens exactly once via the explicit
  #     refresh step, regardless of whether the service was already
  #     running. (restart starts a stopped unit, so no double-start
  #     edge case on a fresh droplet.)
  sudo systemctl enable coworker-api.service
  sudo systemctl reload-or-restart coworker-api.service

  sudo systemctl enable coworker-worker.service
  sudo systemctl restart coworker-worker.service

  # 3e. Timer-activated oneshots: enable + restart. Each restart fires
  #     one round of work immediately against the new release, doubling
  #     as a wiring sanity check.
  for unit in ${TIMER_ACTIVATED_SERVICES[@]}; do
    sudo systemctl enable "\$unit"
    sudo systemctl restart "\$unit"
  done

  # 3f. Timers: enable + start so they begin their cadence.
  for unit in ${TIMERS[@]}; do
    sudo systemctl enable --now "\$unit"
  done
EOF

# ---------------------------------------------------------------------------
# 4. Failure gate. Any coworker-* unit in failed state blocks the deploy
#    from declaring success; we dump the journal for each failure to make
#    the next step obvious.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 5. /health smoke test (only reached if the failure gate passed).
# ---------------------------------------------------------------------------
sleep 3
echo ""
echo "Smoke-testing https://${DOMAIN}/health ..."
curl -fsS "https://${DOMAIN}/health" | python3 -m json.tool

# ---------------------------------------------------------------------------
# 6. Final state printout. Two views: current status of every coworker-*
#    unit, and the upcoming timer schedule.
# ---------------------------------------------------------------------------
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
