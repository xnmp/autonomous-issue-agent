#!/usr/bin/env bash
# Phase 5 acceptance gate — run the PYTHON harness end-to-end against the demo
# target with the REAL Claude Agent SDK + real gh, and confirm one full
# issue→PR→review→merge cycle. This is the gate the unit/integration suites
# cannot prove (it exercises the live SDK query inside a real worktree + real gh
# writes).
#
#   . bin/compose-env.sh demo
#   DEMO_REPO=you/calc-demo bash bin/demo-gate.sh
#
# Requires: gh authenticated; Anthropic creds for the SDK; AGENT_ENV pointing at
# config/.active.<name>.env (from bin/compose-env.sh) with REPO=$DEMO_REPO,
# FIX_BOT, BASE_BRANCH=dev, TARGET_DIR pointing at the
# demo clone; the three-identity App config if you want the review leg too.
#
# Steps:
#   1. Stand up / refresh the demo repo + seed an ai-ready issue (init-demo.sh).
#   2. Run ONE fix-daemon tick in the foreground (python -m issue_agent.app.main
#      fix, bounded) and watch it: pick the issue → resolve via the SDK → open a
#      PR → label ai-pr-open.
#   3. Assert the PR exists and the issue is ai-pr-open. (Review/merge legs follow
#      the same pattern; enable the review daemon to drive them.)
set -uo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${DEMO_REPO:?set DEMO_REPO=owner/repo}"
: "${AGENT_ENV:?AGENT_ENV is unset or empty; source bin/compose-env.sh <name> first}"
[ -f "$AGENT_ENV" ] || { echo "AGENT_ENV does not name a file: $AGENT_ENV" >&2; exit 1; }

echo "== 1. init demo target =="
DEMO_REPO="$DEMO_REPO" bash "$HARNESS_ROOT/examples/demo-target/init-demo.sh"
DEMO_REPO="$DEMO_REPO" bash "$HARNESS_ROOT/examples/demo-target/seed-issues.sh" 2>/dev/null || true

echo "== 2. one fix-daemon tick (real SDK) =="
# ISSUE_AGENT_MAX_TICKS bounds run_forever for the gate (see app/main.py override
# below); without it the daemon loops forever.
ISSUE_AGENT_MAX_TICKS="${ISSUE_AGENT_MAX_TICKS:-1}" \
  uv run --extra sdk --with tree-sitter --with tree-sitter-bash \
  python -m issue_agent.app.main fix

echo "== 3. assert a PR opened against dev =="
open_prs="$(gh pr list --repo "$DEMO_REPO" --label ai-pr-open --json number --jq 'length')"
if [ "${open_prs:-0}" -ge 1 ]; then
  echo "✓ GATE PASS: $open_prs PR(s) labelled ai-pr-open on $DEMO_REPO"
else
  echo "✗ GATE FAIL: no ai-pr-open PR — inspect the daemon log above"
  exit 1
fi
