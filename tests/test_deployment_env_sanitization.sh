#!/usr/bin/env bash
# Deployment settings are for the harness, not its self-hosted test subprocesses.
# Run the existing shell suites under representative exported settings: before the
# suite-level sanitizers this changes hook verdicts and redirects orchestration
# worktrees to TARGET_DIR.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

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
