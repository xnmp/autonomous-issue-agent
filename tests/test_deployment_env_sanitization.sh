#!/usr/bin/env bash
# Deployment settings are for the harness, not its self-hosted test subprocesses.
# Run the existing shell suites under representative exported settings: before the
# suite-level sanitizers this changes hook verdicts and redirects orchestration
# worktrees to TARGET_DIR.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# ── Scrub-list drift check ────────────────────────────────────────────────────
# One python source of truth (governor.TARGET_CMD_SCRUB_KNOBS, also consumed by
# tests/conftest.py) and the remaining launcher's bash hand-copy. The advisory
# gates now invoke the Python executor directly. The launcher must name the same knobs —
# this repo already learned (gate parity) that hand-kept twins drift silently.
py_list="$(cd "$ROOT" && uv run --quiet python -c \
  'from issue_agent.adapters.governor import TARGET_CMD_SCRUB_KNOBS as K; print("\n".join(sorted(K)))')"
for spec in "bin/point-at.sh:RTC_SCRUB_STATIC"; do
  f="${spec%%:*}"; var="${spec##*:}"
  sh_list="$(sed -n "s/^\(export \)\?${var}=\"\(.*\)\"$/\2/p" "$ROOT/$f" | tr ' ' '\n' | sort -u | grep .)"
  if [ "$py_list" != "$sh_list" ]; then
    echo "SCRUB-LIST DRIFT: $f ($var) disagrees with governor.TARGET_CMD_SCRUB_KNOBS" >&2
    diff <(echo "$py_list") <(echo "$sh_list") >&2 || true
    exit 1
  fi
done
echo "scrub lists in sync (python + remaining bash launcher)"

# ── Hostile-env pytest leg ────────────────────────────────────────────────────
# The adapter tests read LINT_CMD/TYPECHECK_CMD/… from the process env;
# tests/conftest.py must neutralize an operator's exported values.
# Reuse the normal adapter runner so CI's bounded worker pool also applies
# here; a serial replay of the real SQLite histories dominated this drill.
env \
  TEST_CMD=true LINT_CMD=true TYPECHECK_CMD=true EDIT_CHECK_CMD=true \
  ALLOW_UNTESTED=1 RED_MUST_FAIL=0 VERIFY_RED_AT_COMMIT=0 \
  BASE_BRANCH=wrong-base GATE_TEST_TIMEOUT_S=1 \
  GATE_TEST_FAILURE_EXIT_CODES=1 GATE_GH_RETRY_SLEEP=0 \
  GH_APP_ID='<your-app-id>' GH_APP_KEY_FILE="$scratch/missing-key" \
  GH_APP_INSTALL_ID=123 TARGET_DIR="$ROOT" AGENT_ENV="$scratch/outer.env" \
  STATE_DIR="$scratch/state" REVIEW_STATE_DIR="$scratch/review-state" \
  STEWARD_STATE_DIR="$scratch/steward-state" \
  HARNESS_DEPLOY_KNOBS="STATE_DIR REVIEW_STATE_DIR STEWARD_STATE_DIR CUSTOM_DEPLOY_KNOB" \
  CUSTOM_DEPLOY_KNOB="leaks-unless-manifest-scrubbed" \
  sh -c "cd '$ROOT' && make --no-print-directory test-adapters"

env \
  TEST_CMD=true \
  LINT_CMD=true \
  TYPECHECK_CMD=true \
  ALLOW_UNTESTED=1 \
  RED_MUST_FAIL=0 \
  VERIFY_RED_AT_COMMIT=0 \
  BASE_BRANCH=wrong-base \
  GATE_TEST_TIMEOUT_S=1 \
  GATE_TEST_FAILURE_EXIT_CODES=1 \
  GATE_GH_RETRY_SLEEP=0 \
  GH_APP_ID='<your-app-id>' \
  GH_APP_KEY_FILE="$scratch/missing-key" \
  GH_APP_INSTALL_ID=123 \
  TARGET_DIR="$ROOT" \
  AGENT_ENV="$scratch/outer.env" \
  STATE_DIR="$scratch/state" \
  REVIEW_STATE_DIR="$scratch/review-state" \
  STEWARD_STATE_DIR="$scratch/steward-state" \
  HARNESS_DEPLOY_KNOBS="STATE_DIR REVIEW_STATE_DIR STEWARD_STATE_DIR CUSTOM_DEPLOY_KNOB" \
  CUSTOM_DEPLOY_KNOB="leaks-unless-manifest-scrubbed" \
  bash "$ROOT/tests/test_hooks.sh"
