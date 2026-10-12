#!/usr/bin/env bash
# Deployment settings are for the harness, not its self-hosted test subprocesses.
# Run the adapter suite under representative exported settings: before the
# suite-level sanitizer this redirected orchestration worktrees to TARGET_DIR.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# ── Hostile-env pytest leg ────────────────────────────────────────────────────
# tests/conftest.py must neutralize an operator's exported deployment values.
# Reuse the normal adapter runner so CI's bounded worker pool also applies
# here; a serial replay of the real SQLite histories dominated this drill.
env \
  BASE_BRANCH=wrong-base \
  GH_APP_ID='<your-app-id>' GH_APP_KEY_FILE="$scratch/missing-key" \
  GH_APP_INSTALL_ID=123 TARGET_DIR="$ROOT" AGENT_ENV="$scratch/outer.env" \
  STATE_DIR="$scratch/state" REVIEW_STATE_DIR="$scratch/review-state" \
  HARNESS_DEPLOY_KNOBS="STATE_DIR REVIEW_STATE_DIR CUSTOM_DEPLOY_KNOB" \
  CUSTOM_DEPLOY_KNOB="leaks-unless-manifest-scrubbed" \
  sh -c "cd '$ROOT' && make --no-print-directory test-adapters"
