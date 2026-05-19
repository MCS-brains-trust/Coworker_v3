#!/usr/bin/env bash
# Proof 4A — Production-behaviour-unchanged.
#
# With every DEPLOY_* env var UNSET, demonstrate that deploy.sh's seams
# resolve to the pre-seam literals byte-for-byte AND that the §3
# manual-rollback paste-block produces a runnable, paste-ready command
# set containing literal `sudo`, `systemctl`, and the production symlink
# path `/opt/coworker/current` — byte-identical to the C′-shipped version
# (commit 3051d14).
#
# This proves the seam refactor is transparent to production: a normal
# `./backend/scripts/deploy.sh <sha>` with no DEPLOY_* env set behaves
# byte-for-byte as it did pre-refactor.
#
# Method:
#   4A.1 — extract the seam-declaration block from the live deploy.sh,
#          eval it in this shell with all DEPLOY_* env unset, and assert
#          each resolved variable equals the pre-seam literal.
#   4A.2 — extract the print_manual_rollback_and_exit function from the
#          live deploy.sh (with `exit 1` defanged to `return 0`), invoke
#          it with EXAMPLE placeholders, capture stderr, and assert the
#          printed paste-block contains literal `sudo ln -sfn`,
#          `sudo systemctl ...`, and `/opt/coworker/current` — NOT any
#          `$SUDO` or `$SYSTEMCTL` token that would indicate failed
#          expansion. The deterministic portion of the block is
#          byte-compared against the expected literal.
#
# No production state is read or written. No deploy is performed.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
DEPLOY_SH="$REPO_ROOT/backend/scripts/deploy.sh"

if [[ ! -f "$DEPLOY_SH" ]]; then
  echo "FATAL: $DEPLOY_SH not found" >&2
  exit 2
fi

# Defensive: clear any pre-existing DEPLOY_* that a parent shell might have set.
unset DEPLOY_RELEASES_DIR DEPLOY_CURRENT_SYMLINK DEPLOY_SYSTEMD_DIR \
      DEPLOY_ENV_FILE DEPLOY_DB_NAME DEPLOY_PG_SUPERUSER \
      DEPLOY_DOMAIN DEPLOY_DROPLET_HOSTNAME \
      DEPLOY_SUDO DEPLOY_SYSTEMCTL DEPLOY_SKIP_GIT_CHECK

# ---------------------------------------------------------------------
# 4A.1 — every seam resolves to its pre-seam literal under unset env
# ---------------------------------------------------------------------
echo "=================================================================="
echo "4A.1 — Per-seam unset-env resolution"
echo "=================================================================="
echo ""

# Extract the seam block from deploy.sh verbatim. The block starts at the
# `# ----- deployment-target seams ...` comment and ends at the
# `RELEASE_DIR="$DEPLOY_RELEASES_DIR/$RELEASE"` alias line.
seam_block=$(
  sed -n \
    '/^# ----- deployment-target seams/,/^RELEASE_DIR="\$DEPLOY_RELEASES_DIR\/\$RELEASE"$/p' \
    "$DEPLOY_SH"
)

if [[ -z "$seam_block" ]]; then
  echo "FATAL: failed to extract seam block from $DEPLOY_SH" >&2
  exit 2
fi

# RELEASE_DIR uses $RELEASE; set a placeholder before evaluating the block.
RELEASE="placeholder-sha"

# Eval the extracted block so the variable assignments take effect here.
eval "$seam_block"

declare -A EXPECTED=(
  [DEPLOY_RELEASES_DIR]="/opt/coworker/releases"
  [DEPLOY_CURRENT_SYMLINK]="/opt/coworker/current"
  [DEPLOY_SYSTEMD_DIR]="/etc/systemd/system"
  [DEPLOY_ENV_FILE]="/opt/coworker/shared/credentials/coworker.env"
  [DEPLOY_DB_NAME]="coworker"
  [DEPLOY_PG_SUPERUSER]="postgres"
  [DEPLOY_DOMAIN]="coworker.mcands.com.au"
  [DEPLOY_DROPLET_HOSTNAME]="coworker-v3-prod-syd1"
  [SUDO]="sudo"
  [SYSTEMCTL]="systemctl"
  [DEPLOY_SKIP_GIT_CHECK]="0"
  [DOMAIN]="coworker.mcands.com.au"
  [RELEASE_DIR]="/opt/coworker/releases/placeholder-sha"
)

# Stable iteration order for the printed table.
ordered_vars=(
  DEPLOY_RELEASES_DIR
  DEPLOY_CURRENT_SYMLINK
  DEPLOY_SYSTEMD_DIR
  DEPLOY_ENV_FILE
  DEPLOY_DB_NAME
  DEPLOY_PG_SUPERUSER
  DEPLOY_DOMAIN
  DEPLOY_DROPLET_HOSTNAME
  SUDO
  SYSTEMCTL
  DEPLOY_SKIP_GIT_CHECK
  DOMAIN
  RELEASE_DIR
)

fail_count=0
for var in "${ordered_vars[@]}"; do
  actual="${!var}"
  expected="${EXPECTED[$var]}"
  if [[ "$actual" == "$expected" ]]; then
    printf "  PASS  %-26s = %s\n" "$var" "$actual"
  else
    printf "  FAIL  %-26s = %s  (expected: %s)\n" "$var" "$actual" "$expected"
    fail_count=$((fail_count + 1))
  fi
done

echo ""
if (( fail_count > 0 )); then
  echo "4A.1 FAILED — $fail_count seam(s) drifted from pre-seam literal."
  exit 1
fi
echo "4A.1 PASSED — every seam resolves to its pre-seam literal under unset env."
echo ""

# ---------------------------------------------------------------------
# 4A.2 — §3 manual-rollback block expands to runnable production text
# ---------------------------------------------------------------------
echo "=================================================================="
echo "4A.2 — §3 manual-rollback block under unset env"
echo "=================================================================="
echo ""

# Extract the function definition verbatim, dedent by 2 spaces (it's
# indented because it lives inside `if [[ "$DEPLOY_MODE" == "local" ]];
# then ... fi`), and replace `exit 1` with `return 0` so we can call it
# without terminating this proof script.
fn_def=$(
  awk '/^  print_manual_rollback_and_exit\(\) \{$/,/^  \}$/' "$DEPLOY_SH" |
    sed -e 's/^  //' -e 's/^  exit 1$/  return 0/'
)

# Defensive: assert the defang actually took effect, so a future sed
# pattern drift cannot silently let the function exit 1 mid-proof.
if ! grep -q '^  return 0$' <<<"$fn_def"; then
  echo "FATAL: failed to defang 'exit 1' in extracted function body" >&2
  exit 2
fi
if grep -qE '^[[:space:]]*exit 1$' <<<"$fn_def"; then
  echo "FATAL: stray 'exit 1' remains in extracted function body" >&2
  exit 2
fi

if [[ -z "$fn_def" ]]; then
  echo "FATAL: failed to extract print_manual_rollback_and_exit from $DEPLOY_SH" >&2
  exit 2
fi

eval "$fn_def"

# Stage the runtime state the function reads.
PREV_RELEASE="/opt/coworker/releases/prev-sha-EXAMPLE"
RELEASE_DIR="/opt/coworker/releases/new-sha-EXAMPLE"
BACKUP_FILE="/tmp/pre-deploy-backup-EXAMPLE.dump"

# Arrays — exact copies of the ones declared at the top of deploy.sh.
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

# PRE_DEPLOY_ENABLED is associative — iteration order is not deterministic,
# so we compare only the deterministic prefix of the captured output (up
# to and including the "Sibling units' pre-deploy is-enabled state:" line).
declare -A PRE_DEPLOY_ENABLED=(
  [coworker-api.service]=enabled
  [coworker-worker.service]=enabled
)

captured_stderr=$(mktemp)
trap 'rm -f "$captured_stderr"' EXIT

print_manual_rollback_and_exit 2>"$captured_stderr" >/dev/null

echo "----- BEGIN captured stderr from print_manual_rollback_and_exit -----"
cat "$captured_stderr"
echo "-----  END captured stderr from print_manual_rollback_and_exit  -----"
echo ""

# Hard assertions on the captured output.
captured="$(cat "$captured_stderr")"

assert_contains() {
  local needle="$1"
  if [[ "$captured" == *"$needle"* ]]; then
    printf "  PASS  contains: %s\n" "$needle"
  else
    printf "  FAIL  missing : %s\n" "$needle"
    fail_count=$((fail_count + 1))
  fi
}

assert_not_contains() {
  local needle="$1"
  if [[ "$captured" != *"$needle"* ]]; then
    printf "  PASS  absent  : %s\n" "$needle"
  else
    printf "  FAIL  present : %s\n" "$needle"
    fail_count=$((fail_count + 1))
  fi
}

echo "Required substrings (paste-block contains literal sudo / systemctl / prod path):"
assert_contains '-----8<----- BEGIN MANUAL ROLLBACK -----8<-----'
assert_contains '------8<----- END MANUAL ROLLBACK -----8<------'
assert_contains 'sudo ln -sfn "/opt/coworker/releases/prev-sha-EXAMPLE" /opt/coworker/current'
assert_contains 'sudo systemctl reload-or-restart coworker-api.service'
assert_contains 'sudo systemctl stop coworker-worker.service'
assert_contains 'sudo systemctl stop coworker-dispatch.service'
assert_contains 'sudo systemctl stop coworker-scheduler.service'
assert_contains 'sudo systemctl stop coworker-subscribe.service'
assert_contains 'sudo systemctl stop coworker-backfill.service'
assert_contains 'sudo systemctl stop coworker-delivery-confirm.service'
assert_contains 'sudo systemctl stop coworker-dispatch.timer'
assert_contains 'sudo systemctl stop coworker-scheduler.timer'
assert_contains 'sudo systemctl stop coworker-subscribe.timer'
assert_contains 'sudo systemctl stop coworker-backfill.timer'
assert_contains 'sudo systemctl stop coworker-delivery-confirm.timer'
assert_contains 'The §3c symlink swap completed; /opt/coworker/current now'
assert_contains 'points at /opt/coworker/releases/new-sha-EXAMPLE.'
assert_contains 'Pre-deploy DB backup retained at: /tmp/pre-deploy-backup-EXAMPLE.dump'
echo ""

echo "Forbidden substrings (no unexpanded shell tokens — would mean broken seam):"
assert_not_contains '$SUDO'
assert_not_contains '$SYSTEMCTL'
assert_not_contains '$DEPLOY_CURRENT_SYMLINK'
assert_not_contains '$RELEASE_DIR'
assert_not_contains '$PREV_RELEASE'
echo ""

# Byte-identity check on the deterministic prefix (everything from start
# through the "Sibling units' pre-deploy is-enabled state:" line).
end_marker="Sibling units' pre-deploy is-enabled state:"
captured_prefix="${captured%%${end_marker}*}${end_marker}"

expected_prefix=$(cat <<'EOF'

=== Deploy failed during §3 unit-start sequence ===

The §3c symlink swap completed; /opt/coworker/current now
points at /opt/coworker/releases/new-sha-EXAMPLE. A systemd unit start in §3d/§3e/§3f
returned non-zero, so the script stopped before the §4/§5/§6
verification gates ran. No automatic rollback has been
performed — start-sequence failures are typically config/env
issues an operator should diagnose on the half-started fleet
before reverting.

Inspect first:
  systemctl list-units --failed --type=service,timer 'coworker-*'
  journalctl -u <unit> --no-pager -n 50

When ready to roll back, paste the block below as-is. All
paths and unit names are fully resolved; no substitutions
needed.

-----8<----- BEGIN MANUAL ROLLBACK -----8<-----
sudo ln -sfn "/opt/coworker/releases/prev-sha-EXAMPLE" /opt/coworker/current
sudo systemctl reload-or-restart coworker-api.service
sudo systemctl stop coworker-worker.service
sudo systemctl stop coworker-dispatch.service
sudo systemctl stop coworker-scheduler.service
sudo systemctl stop coworker-subscribe.service
sudo systemctl stop coworker-backfill.service
sudo systemctl stop coworker-delivery-confirm.service
sudo systemctl stop coworker-dispatch.timer
sudo systemctl stop coworker-scheduler.timer
sudo systemctl stop coworker-subscribe.timer
sudo systemctl stop coworker-backfill.timer
sudo systemctl stop coworker-delivery-confirm.timer
------8<----- END MANUAL ROLLBACK -----8<------

Pre-deploy DB backup retained at: /tmp/pre-deploy-backup-EXAMPLE.dump

Sibling units' pre-deploy is-enabled state:
EOF
)

echo "Byte-identity check on deterministic block prefix:"
if [[ "$captured_prefix" == "$expected_prefix" ]]; then
  echo "  PASS  captured prefix is byte-identical to expected C′-shipped literal block."
else
  echo "  FAIL  captured prefix DIFFERS from expected C′-shipped literal block:"
  diff <(printf '%s' "$expected_prefix") <(printf '%s' "$captured_prefix") || true
  fail_count=$((fail_count + 1))
fi
echo ""

if (( fail_count > 0 )); then
  echo "4A.2 FAILED — $fail_count check(s) failed."
  exit 1
fi
echo "4A.2 PASSED — printed paste-block is byte-identical to the C′-shipped"
echo "             version and is paste-ready & runnable on the production droplet."
echo ""

echo "=================================================================="
echo "4A — OVERALL: PASSED"
echo "=================================================================="
