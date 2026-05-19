#!/usr/bin/env bash
# Proof 4B — Testability-actually-works.
#
# Run the seamed deploy.sh end-to-end against a throwaway target via the
# DEPLOY_* seams + a programmable fake-systemctl shim. Prove:
#
#   (1) Real production state is NOT mutated:
#       - real /opt/coworker/current symlink unchanged (or unaffected if
#         it doesn't exist on this host)
#       - real /etc/systemd/system contents hash unchanged
#       - real `coworker` DB never queried or dumped (we point
#         DEPLOY_DB_NAME at a throwaway name and use shimmed pg_dump/psql,
#         so the real DB is untouched by definition)
#
#   (2) Bug C (§3 ERR trap firing on unit-start failure) IS catchable
#       without real systemd: program fake-systemctl to exit non-zero on
#       `restart coworker-subscribe.service`, run the script, assert the
#       ERR trap fired and the manual-rollback paste-block printed.
#
# Two contextual behaviours by design (per DECISION PAUSE 2 notes):
#
#   (a) The §3 manual-rollback block in this test prints the shim paths
#       (e.g. ".../shims/fake-sudo", ".../shims/fake-systemctl") rather
#       than literal `sudo`/`systemctl`. That is CORRECT for the test
#       context, not a discrepancy — it shows the seam took effect.
#       Proof 4A is the production-transparency gate; 4B is the
#       testability gate.
#
#   (b) DEPLOY_CURRENT_SYMLINK is staged as a non-existent path. `readlink
#       -f` on a non-existent path returns the path string itself (not
#       an error). So PREV_RELEASE in the test context equals the
#       throwaway symlink path. Assertions expect that and do not flag
#       it as failure.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
DEPLOY_SH="$REPO_ROOT/backend/scripts/deploy.sh"

cd "$REPO_ROOT"  # deploy.sh requires invocation from repo root

if [[ ! -x "$DEPLOY_SH" ]]; then
  echo "FATAL: $DEPLOY_SH not found or not executable" >&2
  exit 2
fi

# ---------------------------------------------------------------------
# Record real production state BEFORE the test, for invariance check.
# ---------------------------------------------------------------------
REAL_SYMLINK_TARGET_BEFORE="<not-present>"
if [[ -L /opt/coworker/current ]]; then
  REAL_SYMLINK_TARGET_BEFORE=$(readlink /opt/coworker/current)
fi

REAL_ETC_SYSTEMD_HASH_BEFORE="<not-readable>"
if [[ -r /etc/systemd/system ]]; then
  REAL_ETC_SYSTEMD_HASH_BEFORE=$(
    find /etc/systemd/system -maxdepth 1 -printf '%f %m %s\n' 2>/dev/null |
    sort | sha256sum | awk '{print $1}'
  )
fi

echo "=================================================================="
echo "4B — Throwaway-target harness"
echo "=================================================================="
echo ""
echo "Real production state recorded BEFORE the test:"
echo "  /opt/coworker/current → $REAL_SYMLINK_TARGET_BEFORE"
echo "  /etc/systemd/system content hash → $REAL_ETC_SYSTEMD_HASH_BEFORE"
echo ""

# ---------------------------------------------------------------------
# Build throwaway environment + shims
# ---------------------------------------------------------------------
TMP=$(mktemp -d -t deploy-4b-harness.XXXXXX)
echo "Throwaway target: $TMP"
echo ""

cleanup() {
  # Clean tmpdir + any /tmp/pre-deploy-backup-* this harness created.
  # The script hardcodes /tmp/pre-deploy-backup-... (not parameterized
  # in this scope); the harness records which file was created and
  # removes only that one.
  if [[ -n "${HARNESS_BACKUP_FILE:-}" && -f "$HARNESS_BACKUP_FILE" ]]; then
    rm -f "$HARNESS_BACKUP_FILE"
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

mkdir -p "$TMP/releases" "$TMP/systemd" "$TMP/credentials" "$TMP/shims"

# --- fake-sudo: strip leading `-u <user>` then exec the rest ---
cat > "$TMP/shims/fake-sudo" <<'SHIM'
#!/usr/bin/env bash
args=("$@")
if [[ "${args[0]:-}" == "-u" ]]; then
  args=("${args[@]:2}")
fi
exec "${args[@]}"
SHIM
chmod +x "$TMP/shims/fake-sudo"

# --- fake-systemctl: log invocations; programmable failure pattern ---
cat > "$TMP/shims/fake-systemctl" <<SHIM
#!/usr/bin/env bash
# Log every invocation (one line per call) to a file the harness reads.
echo "fake-systemctl \$*" >> "$TMP/systemctl.log"

# Programmable failure: if SYSTEMCTL_FAIL_ON is set and the joined-args
# string contains it, exit non-zero. Used to trigger the §3 ERR trap.
if [[ -n "\${SYSTEMCTL_FAIL_ON:-}" ]] && [[ "\$*" == *"\${SYSTEMCTL_FAIL_ON}"* ]]; then
  echo "fake-systemctl: deliberately failing on '\$*'" >&2
  exit 1
fi

# Read-only query subcommands need deterministic output.
case "\${1:-}" in
  is-enabled) echo "enabled" ;;
  is-active)
    if [[ "\${2:-}" == "--quiet" ]]; then exit 0; fi
    echo "active"
    ;;
  show) echo "success" ;;
  list-units|list-timers) ;;   # empty output → no failed units
  *) ;;                         # daemon-reload, enable, restart: just succeed
esac
exit 0
SHIM
chmod +x "$TMP/shims/fake-systemctl"

# --- install shim: ignore -o/-g flags (need real root for chown), pass
# rest to /usr/bin/install. Placed FIRST on PATH so deploy.sh's plain
# `install` invocations resolve here. ---
cat > "$TMP/shims/install" <<'SHIM'
#!/usr/bin/env bash
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/install "${args[@]}"
SHIM
chmod +x "$TMP/shims/install"

# --- pg_dump shim: emit deterministic bytes to stdout (script redirects
# to BACKUP_FILE and then ls -lh / sha256sum it). Never touches a real DB. ---
cat > "$TMP/shims/pg_dump" <<'SHIM'
#!/usr/bin/env bash
printf 'fake-pg-dump-content\n'
SHIM
chmod +x "$TMP/shims/pg_dump"

# --- psql shim: return empty for the alembic_version select. DB_HEAD
# ends up empty; combined with the empty alembic-heads output (next),
# this makes DB_HEAD == RELEASE_HEAD and the migration guard passes. ---
cat > "$TMP/shims/psql" <<'SHIM'
#!/usr/bin/env bash
printf ''
SHIM
chmod +x "$TMP/shims/psql"

# --- uv shim: no-op. The script's §2a `uv sync` is bypassed; the
# release's .venv/bin/alembic is pre-staged below. ---
cat > "$TMP/shims/uv" <<'SHIM'
#!/usr/bin/env bash
exit 0
SHIM
chmod +x "$TMP/shims/uv"

# ---------------------------------------------------------------------
# Pre-stage the release dir's .venv/bin/alembic (no-op).
# §1 `git archive | tar -x` does not include `.venv` (gitignored), so
# our pre-staged stub survives the extraction.
# ---------------------------------------------------------------------
RELEASE_SHA=$(git rev-parse --short HEAD)
RELEASE_DIR="$TMP/releases/$RELEASE_SHA"
mkdir -p "$RELEASE_DIR/.venv/bin"
cat > "$RELEASE_DIR/.venv/bin/alembic" <<'SHIM'
#!/usr/bin/env bash
# no-op alembic stub: returns empty for `heads`, exits 0 for everything
exit 0
SHIM
chmod +x "$RELEASE_DIR/.venv/bin/alembic"

# Stage an env file (content irrelevant; the script just copies it).
cat > "$TMP/credentials/coworker.env" <<'EOF'
TEST_HARNESS=1
EOF
chmod 0640 "$TMP/credentials/coworker.env"

# Note: $TMP/current is DELIBERATELY not created. readlink -f on a
# non-existent path returns the path itself; PREV_RELEASE will equal
# "$TMP/current" — see contextual behaviour (b) above.

# ---------------------------------------------------------------------
# Export DEPLOY_* seams pointing at the throwaway + shims.
# ---------------------------------------------------------------------
export DEPLOY_RELEASES_DIR="$TMP/releases"
export DEPLOY_CURRENT_SYMLINK="$TMP/current"
export DEPLOY_SYSTEMD_DIR="$TMP/systemd"
export DEPLOY_ENV_FILE="$TMP/credentials/coworker.env"
export DEPLOY_DB_NAME="coworker_test_deploy_4b"   # never queried — psql is shimmed
export DEPLOY_PG_SUPERUSER="$(whoami)"
export DEPLOY_DOMAIN="localhost"
export DEPLOY_DROPLET_HOSTNAME="$(hostname)"
export DEPLOY_SUDO="$TMP/shims/fake-sudo"
export DEPLOY_SYSTEMCTL="$TMP/shims/fake-systemctl"
export DEPLOY_SKIP_GIT_CHECK=1

# Shims at front of PATH so plain `install`, `pg_dump`, `psql`, `uv`
# invocations resolve to our shims. /usr/bin/install is invoked by
# absolute path inside our `install` shim, so we don't shadow it
# downstream. Other PATH-resolved binaries (curl, awk, tr, sed, etc.)
# resolve to the real system binaries.
export PATH="$TMP/shims:$PATH"

# Program the fake systemctl to fail on `restart coworker-subscribe.service`
# — this is the §3e step that crashed in incident 09fad28.
export SYSTEMCTL_FAIL_ON="restart coworker-subscribe.service"

echo "DEPLOY_* env exported. Running deploy.sh against the throwaway..."
echo ""

# ---------------------------------------------------------------------
# Run deploy.sh.  Capture stdout + stderr separately.
# We expect the script to exit non-zero because of the §3 ERR trap.
# ---------------------------------------------------------------------
DEPLOY_STDOUT="$TMP/deploy.stdout"
DEPLOY_STDERR="$TMP/deploy.stderr"

set +e
"$DEPLOY_SH" "$RELEASE_SHA" >"$DEPLOY_STDOUT" 2>"$DEPLOY_STDERR"
deploy_exit=$?
set -e

echo "deploy.sh exit code: $deploy_exit (expected: 1 — §3 ERR trap fired)"
echo ""

# Record the backup file the script created in /tmp (for cleanup).
HARNESS_BACKUP_FILE=$(grep -oE '/tmp/pre-deploy-backup-[0-9-]+\.dump' "$DEPLOY_STDOUT" | head -1 || true)

# ---------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------
fail_count=0
pass() { printf "  PASS  %s\n" "$1"; }
fail() { printf "  FAIL  %s\n" "$1"; fail_count=$((fail_count + 1)); }

echo "=================================================================="
echo "4B.1 — Real production state invariance"
echo "=================================================================="

REAL_SYMLINK_TARGET_AFTER="<not-present>"
if [[ -L /opt/coworker/current ]]; then
  REAL_SYMLINK_TARGET_AFTER=$(readlink /opt/coworker/current)
fi

REAL_ETC_SYSTEMD_HASH_AFTER="<not-readable>"
if [[ -r /etc/systemd/system ]]; then
  REAL_ETC_SYSTEMD_HASH_AFTER=$(
    find /etc/systemd/system -maxdepth 1 -printf '%f %m %s\n' 2>/dev/null |
    sort | sha256sum | awk '{print $1}'
  )
fi

if [[ "$REAL_SYMLINK_TARGET_BEFORE" == "$REAL_SYMLINK_TARGET_AFTER" ]]; then
  pass "real /opt/coworker/current unchanged ($REAL_SYMLINK_TARGET_BEFORE → $REAL_SYMLINK_TARGET_AFTER)"
else
  fail "real /opt/coworker/current MUTATED ($REAL_SYMLINK_TARGET_BEFORE → $REAL_SYMLINK_TARGET_AFTER)"
fi

if [[ "$REAL_ETC_SYSTEMD_HASH_BEFORE" == "$REAL_ETC_SYSTEMD_HASH_AFTER" ]]; then
  pass "real /etc/systemd/system contents hash unchanged"
else
  fail "real /etc/systemd/system contents hash MUTATED"
fi

# Throwaway-side proof: the script DID write to the seam'd paths.
if [[ -L "$DEPLOY_CURRENT_SYMLINK" ]]; then
  target=$(readlink "$DEPLOY_CURRENT_SYMLINK")
  if [[ "$target" == "$RELEASE_DIR" ]]; then
    pass "throwaway $DEPLOY_CURRENT_SYMLINK → $target (script's §3c swap landed in throwaway, not prod)"
  else
    fail "throwaway $DEPLOY_CURRENT_SYMLINK points at $target (expected $RELEASE_DIR)"
  fi
else
  fail "throwaway $DEPLOY_CURRENT_SYMLINK was not created — script didn't reach §3c?"
fi

unit_files_in_throwaway=$(ls "$DEPLOY_SYSTEMD_DIR/" 2>/dev/null | wc -l)
if (( unit_files_in_throwaway > 0 )); then
  pass "throwaway $DEPLOY_SYSTEMD_DIR/ contains $unit_files_in_throwaway unit file(s) (script's §3a install landed in throwaway, not prod)"
else
  fail "throwaway $DEPLOY_SYSTEMD_DIR/ is empty — script didn't reach §3a?"
fi

echo ""

echo "=================================================================="
echo "4B.2 — Bug C: §3 ERR trap caught at the unit-start failure"
echo "=================================================================="

if (( deploy_exit == 1 )); then
  pass "deploy.sh exited 1 (expected: §3 ERR trap fires after unit-start failure)"
else
  fail "deploy.sh exited $deploy_exit (expected: 1)"
fi

assert_stderr_contains() {
  local needle="$1"
  if grep -qF -- "$needle" "$DEPLOY_STDERR"; then
    pass "stderr contains: $needle"
  else
    fail "stderr missing : $needle"
  fi
}

assert_stderr_contains '=== Deploy failed during §3 unit-start sequence ==='
assert_stderr_contains '-----8<----- BEGIN MANUAL ROLLBACK -----8<-----'
assert_stderr_contains '------8<----- END MANUAL ROLLBACK -----8<------'
# In TEST context the printed block contains the shim paths, not literal
# sudo/systemctl. This is correct-for-test per contextual behaviour (a).
assert_stderr_contains "$TMP/shims/fake-sudo"
assert_stderr_contains "$TMP/shims/fake-systemctl"
# And the throwaway symlink path — proves DEPLOY_CURRENT_SYMLINK was
# threaded through to the print function.
assert_stderr_contains "$TMP/current"

# Confirm the failure ORIGINATED at the right unit (the bug-C signature).
if grep -qF "deliberately failing on 'restart coworker-subscribe.service'" "$DEPLOY_STDERR"; then
  pass "failure originated at: restart coworker-subscribe.service (Bug C class)"
else
  fail "failure did not originate at the programmed unit"
fi

# Confirm the manual-rollback block stopped systemctl invocations after
# the failed restart — i.e. the §3 ERR trap actually fired BEFORE §3f
# could enable timers. We assert by checking the systemctl.log:
echo ""
echo "Fake systemctl invocation log (each line = one invocation deploy.sh made):"
nl -ba "$TMP/systemctl.log" 2>/dev/null || echo "(no invocations logged?)"
echo ""

# The script invokes systemctl in order: is-enabled (×many), daemon-reload,
# enable api, reload-or-restart api, enable worker, restart worker, then
# the timer-activated loop where coworker-subscribe.service is item 3.
# It should NEVER reach `enable --now coworker-*.timer` (§3f).
if grep -qE 'enable --now coworker-' "$TMP/systemctl.log"; then
  fail "fake-systemctl saw '${grep_line:-enable --now}' — ERR trap did NOT short-circuit §3f"
else
  pass "fake-systemctl did NOT see any 'enable --now' calls — §3 ERR trap fired before §3f"
fi

# Contextual behaviour (b): PREV_RELEASE should be the throwaway symlink
# path (because readlink -f on non-existent returns the path itself).
if grep -qF "ln -sfn \"$TMP/current\" $TMP/current" "$DEPLOY_STDERR"; then
  pass "PREV_RELEASE == \$DEPLOY_CURRENT_SYMLINK in test context (readlink-f-on-nonexistent behaviour, expected per design)"
else
  # Not necessarily a failure — depending on whether readlink behaves
  # exactly that way; surface for review.
  pass_or_inform=$(grep -F 'ln -sfn' "$DEPLOY_STDERR" || true)
  printf "  INFO  PREV_RELEASE expansion in test context: %s\n" "$pass_or_inform"
fi

echo ""

# ---------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------
echo "=================================================================="
if (( fail_count == 0 )); then
  echo "4B — OVERALL: PASSED"
  echo ""
  echo "Real production state untouched. The script ran end-to-end against"
  echo "the throwaway; §3 ERR trap fired at the programmed unit-start"
  echo "failure (Bug C class) and the manual-rollback block printed."
  echo "Deferred residual: Bug B (worker-required-env validation) is NOT"
  echo "exercised by this proof — that is the documented out-of-scope item"
  echo "in docs/known-issues/2026-05-18-env-file-validated-against-wrong-contract.md."
  echo "=================================================================="
  exit 0
else
  echo "4B — OVERALL: FAILED ($fail_count assertion failure(s))"
  echo "=================================================================="
  echo ""
  echo "deploy.sh stdout was:"
  echo "-----"
  cat "$DEPLOY_STDOUT" || true
  echo "-----"
  echo ""
  echo "deploy.sh stderr was:"
  echo "-----"
  cat "$DEPLOY_STDERR" || true
  echo "-----"
  exit 1
fi
