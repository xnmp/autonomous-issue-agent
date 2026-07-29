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
# tests/conftest.py) and two bash hand-copies. They must name the same knobs —
# this repo already learned (gate parity) that hand-kept twins drift silently.
py_list="$(cd "$ROOT" && uv run --quiet python -c \
  'from issue_agent.adapters.governor import TARGET_CMD_SCRUB_KNOBS as K; print("\n".join(sorted(K)))')"
for f in plugin/hooks/checks/_pr_common.sh bin/point-at.sh; do
  sh_list="$(grep -o '\-u [A-Z_][A-Z_]*' "$ROOT/$f" | sed 's/-u //' | sort -u)"
  if [ "$py_list" != "$sh_list" ]; then
    echo "SCRUB-LIST DRIFT: $f disagrees with governor.TARGET_CMD_SCRUB_KNOBS" >&2
    diff <(echo "$py_list") <(echo "$sh_list") >&2 || true
    exit 1
  fi
done
echo "scrub lists in sync (python + 2 bash copies)"

# ── Hostile-env pytest leg ────────────────────────────────────────────────────
# The adapter tests read LINT_CMD/TYPECHECK_CMD/… from the process env;
# tests/conftest.py must neutralize an operator's exported values.
env \
  TEST_CMD=true LINT_CMD=true TYPECHECK_CMD=true EDIT_CHECK_CMD=true \
  ALLOW_UNTESTED=1 RED_MUST_FAIL=0 VERIFY_RED_AT_COMMIT=0 \
  BASE_BRANCH=wrong-base GATE_TEST_TIMEOUT_S=1 GATE_GH_RETRY_SLEEP=0 \
  GH_APP_ID='<your-app-id>' GH_APP_PRIVATE_KEY="$scratch/missing-key" \
  GH_APP_INSTALLATION_ID=123 TARGET_DIR="$ROOT" AGENT_ENV="$scratch/outer.env" \
  sh -c "cd '$ROOT' && uv run --quiet pytest tests/adapters -q"

env \
  TEST_CMD=true \
  LINT_CMD=true \
  TYPECHECK_CMD=true \
  ALLOW_UNTESTED=1 \
  RED_MUST_FAIL=0 \
  VERIFY_RED_AT_COMMIT=0 \
  BASE_BRANCH=wrong-base \
  GATE_TEST_TIMEOUT_S=1 \
  GATE_GH_RETRY_SLEEP=0 \
  GH_APP_ID='<your-app-id>' \
  GH_APP_PRIVATE_KEY="$scratch/missing-key" \
  GH_APP_INSTALLATION_ID=123 \
  TARGET_DIR="$ROOT" \
  AGENT_ENV="$scratch/outer.env" \
  bash "$ROOT/tests/test_hooks.sh"

env \
  TEST_CMD=true \
  LINT_CMD=true \
  TYPECHECK_CMD=true \
  ALLOW_UNTESTED=1 \
  RED_MUST_FAIL=0 \
  VERIFY_RED_AT_COMMIT=0 \
  BASE_BRANCH=wrong-base \
  GATE_TEST_TIMEOUT_S=1 \
  GATE_GH_RETRY_SLEEP=0 \
  GH_APP_ID='<your-app-id>' \
  GH_APP_PRIVATE_KEY="$scratch/missing-key" \
  GH_APP_INSTALLATION_ID=123 \
  TARGET_DIR="$ROOT" \
  AGENT_ENV="$scratch/outer.env" \
  bash "$ROOT/tests/test_orchestration.sh"
