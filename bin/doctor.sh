#!/usr/bin/env bash
# Preflight: validate tooling, config, identities, and repo access before the
# daemons run. Exit non-zero if anything that would silently break is wrong.
set -uo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
WARN=0; ERR=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { WARN=$((WARN+1)); printf '  \033[33m⚠\033[0m %s\n' "$1"; }
bad()  { ERR=$((ERR+1));  printf '  \033[31m✗\033[0m %s\n' "$1"; }

echo "== tooling =="
for c in claude gh jq git uv; do
  if command -v "$c" >/dev/null 2>&1; then ok "$c: $($c --version 2>/dev/null | head -1)"; else bad "$c not found on PATH"; fi
done

echo "== governor self-test =="
if command -v uv >/dev/null 2>&1; then
  out=$(printf '{"tool_input":{"command":"git commit --no-verify -m x"}}' | uv run --quiet "$HARNESS_ROOT/plugin/hooks/bash_governor.py" 2>&1); rc=$?
  if [ "$rc" -eq 2 ]; then ok "governor blocks --no-verify (exit 2)"; else bad "governor did NOT block bypass (rc=$rc)"; fi
else
  warn "uv missing → governor degraded (fails open)"
fi

echo "== config =="
if [ -f "$CONFIG" ]; then
  ok "config: $CONFIG"
  # shellcheck disable=SC1090
  set -a; . "$CONFIG"; set +a
else
  bad "config not found: $CONFIG (copy config/agent.env.example → config/agent.env)"
fi

: "${REPO:=}"; : "${IDENTITY_MODE:=three}"; : "${FIX_BOT:=}"
[ -n "$REPO" ] && ok "REPO=$REPO" || bad "REPO unset"
[ -n "$FIX_BOT" ] && ok "FIX_BOT=$FIX_BOT" || bad "FIX_BOT unset"

if [ "$IDENTITY_MODE" = "three" ]; then
  : "${REVIEW_BOT:=}"; : "${STEWARD_BOT:=}"
  # FIX must differ from REVIEW (GitHub 422 self-approval) and from STEWARD.
  # REVIEW and STEWARD MAY share an account: the steward never approves/merges
  # PRs, so it triggers no self-approval conflict.
  if [ -n "$REVIEW_BOT" ] && [ -n "$STEWARD_BOT" ] \
     && [ "$FIX_BOT" != "$REVIEW_BOT" ] && [ "$FIX_BOT" != "$STEWARD_BOT" ]; then
    if [ "$REVIEW_BOT" = "$STEWARD_BOT" ]; then
      ok "two distinct identities (fix=$FIX_BOT, review+steward=$REVIEW_BOT)"
    else
      ok "three distinct identities ($FIX_BOT / $REVIEW_BOT / $STEWARD_BOT)"
    fi
  else
    bad "IDENTITY_MODE=three needs REVIEW_BOT+STEWARD_BOT set and FIX_BOT distinct from both (review+steward may coincide)"
  fi
else
  ok "solo identity mode (human reviews; auto-merge stays human)"
  [ "${AUTO_MERGE:-1}" = "1" ] && warn "AUTO_MERGE=1 has no effect in solo mode (no second approver) — merges stay human"
fi

echo "== github access =="
if command -v gh >/dev/null 2>&1; then
  who=$(gh api user --jq '.login' 2>/dev/null || echo "")
  [ -n "$who" ] && ok "gh authenticated as $who" || bad "gh not authenticated (run: gh auth login)"
  if [ -n "$REPO" ]; then
    gh repo view "$REPO" --json name >/dev/null 2>&1 && ok "can read $REPO" || bad "cannot access repo $REPO"
  fi
else
  bad "gh missing"
fi

echo "== project commands =="
if [ -n "${TEST_CMD:-}" ]; then
  ok "TEST_CMD=$TEST_CMD"
  [ "${RED_MUST_FAIL:-1}" = 0 ] \
    && warn "RED_MUST_FAIL=0 — the anti-tautological-test (red-fail) gate is OFF; a test that passes without the change won't be caught"
else
  # The bigger consequence than "no tests": the red-fail gate that catches
  # tautological / wrong-layer tests (the #39 class) is disabled with it.
  warn "TEST_CMD unset → PR gate + reviewer run NO tests AND the anti-tautological-test (red-fail) gate is DISABLED. The hook still fires but has no teeth. Set TEST_CMD to arm it."
fi
[ -n "${LINT_CMD:-}" ] && ok "LINT_CMD set" || warn "LINT_CMD unset (commit hook won't lint)"

echo ""
printf 'doctor: %d error(s), %d warning(s)\n' "$ERR" "$WARN"
[ "$ERR" -eq 0 ]
