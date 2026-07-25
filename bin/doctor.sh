#!/usr/bin/env bash
# Preflight: validate tooling, config, identities, and repo access before the
# daemons run. Exit non-zero if anything that would silently break is wrong.
set -uo pipefail
# review C5/D7: honour a pre-set HARNESS_ROOT (tests point this at a scratch
# config/repos/ tree to exercise the collision check below in isolation);
# normal invocations never set it, so this resolves exactly as before.
HARNESS_ROOT="${HARNESS_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
WARN=0; ERR=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { WARN=$((WARN+1)); printf '  \033[33m⚠\033[0m %s\n' "$1"; }
bad()  { ERR=$((ERR+1));  printf '  \033[31m✗\033[0m %s\n' "$1"; }

# review C1: the ONE place doctor compares GitHub logins. Mirrors
# domain/identity.py's normalize_login (casefold, then strip ONE trailing
# `[bot]` suffix) — a raw `!=` lets `fix-bot` vs `fix-bot[bot]` pass doctor
# even though they're the SAME GitHub identity in two API surface forms,
# and GitHub then 422s the self-approval on every PR, forever.
_normalize_login() {
  local l="${1,,}"
  printf '%s\n' "${l%\[bot\]}"
}

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

echo "== per-repo state-dir collisions (config/repos/*.env) =="
# review M5/D7: default STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR are now
# per-repo (app/config.py::_default_state_dir), but nothing stopped two
# overlays from setting (or defaulting to) the SAME dir — a second repo's fix
# daemon would then resume the first repo's issue-N.session/.phase transcript.
# Scan every config/repos/*.env overlay, resolve each role's EFFECTIVE state
# dir (explicit STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR if set, else the
# same per-repo default config.py computes), and fail on any (role, dir) pair
# two different overlays resolve to.
_REPOS_DIR="$HARNESS_ROOT/config/repos"
if [ -d "$_REPOS_DIR" ] && compgen -G "$_REPOS_DIR/*.env" >/dev/null 2>&1; then
  # Mirrors app/config.py::_repo_slug — "owner/name" -> "owner-name",
  # blank/unset REPO -> the fixed "unknown" slug.
  _repo_slug() {
    local r
    r="$(printf '%s' "${1:-}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    r="${r//\//-}"
    printf '%s\n' "${r:-unknown}"
  }
  # Mirrors app/config.py::_default_state_dir.
  _default_state_dir() {
    printf '%s/.local/state/%s/%s\n' "$HOME" "$2" "$(_repo_slug "$1")"
  }

  declare -A _seen_dir  # "<role>:<dir>" -> overlay name that claimed it first
  _collision=0
  for _ov in "$_REPOS_DIR"/*.env; do
    _name="$(basename "$_ov" .env)"
    # Compose the SAME precedence bin/compose-env.sh uses (shared then
    # per-repo overlay) in an isolated subshell, so nothing here leaks into
    # doctor's own environment or the next overlay's resolution.
    _vals="$(
      set -a
      [ -f "$HARNESS_ROOT/config/shared.env" ] && . "$HARNESS_ROOT/config/shared.env"
      . "$_ov"
      set +a
      printf '%s\n%s\n%s\n%s\n' "${REPO:-}" "${STATE_DIR:-}" "${REVIEW_STATE_DIR:-}" "${STEWARD_STATE_DIR:-}"
    )"
    _repo="$(sed -n '1p' <<<"$_vals")"
    _sd="$(sed -n '2p' <<<"$_vals")"
    _rd="$(sed -n '3p' <<<"$_vals")"
    _wd="$(sed -n '4p' <<<"$_vals")"
    [ -n "$_sd" ] || _sd="$(_default_state_dir "$_repo" issue-agent)"
    [ -n "$_rd" ] || _rd="$(_default_state_dir "$_repo" review-agent)"
    [ -n "$_wd" ] || _wd="$(_default_state_dir "$_repo" steward-agent)"

    for _pair in "fix:$_sd" "review:$_rd" "steward:$_wd"; do
      _role="${_pair%%:*}"; _dir="${_pair#*:}"
      _key="$_role:$_dir"
      if [ -n "${_seen_dir[$_key]:-}" ] && [ "${_seen_dir[$_key]}" != "$_name" ]; then
        bad "state-dir collision: overlays '${_seen_dir[$_key]}' and '$_name' both resolve $_role state to $_dir (review M5/D7 — the second repo would resume the first's session/phase)"
        _collision=1
      else
        _seen_dir[$_key]="$_name"
      fi
    done
  done
  [ "$_collision" -eq 0 ] && ok "no state-dir collisions across $(ls "$_REPOS_DIR"/*.env | wc -l | tr -d ' ') overlay(s)"
else
  ok "no config/repos/*.env overlays found — nothing to check (single-repo layout)"
fi

: "${REPO:=}"; : "${IDENTITY_MODE:=three}"; : "${FIX_BOT:=}"
[ -n "$REPO" ] && ok "REPO=$REPO" || bad "REPO unset"
[ -n "$FIX_BOT" ] && ok "FIX_BOT=$FIX_BOT" || bad "FIX_BOT unset"

if [ "$IDENTITY_MODE" = "three" ]; then
  : "${REVIEW_BOT:=}"; : "${STEWARD_BOT:=}"
  # FIX must differ from REVIEW (GitHub 422 self-approval) and from STEWARD.
  # REVIEW and STEWARD MAY share an account: the steward never approves/merges
  # PRs, so it triggers no self-approval conflict. Compared NORMALIZED (review
  # C1) — a raw `!=` would let fix-bot vs fix-bot[bot] (the SAME identity)
  # through.
  _nfix="$(_normalize_login "$FIX_BOT")"
  _nreview="$(_normalize_login "$REVIEW_BOT")"
  _nsteward="$(_normalize_login "$STEWARD_BOT")"
  if [ -n "$REVIEW_BOT" ] && [ -n "$STEWARD_BOT" ] \
     && [ "$_nfix" != "$_nreview" ] && [ "$_nfix" != "$_nsteward" ]; then
    if [ "$_nreview" = "$_nsteward" ]; then
      ok "two distinct identities (fix=$FIX_BOT, review+steward=$REVIEW_BOT)"
    else
      ok "three distinct identities ($FIX_BOT / $REVIEW_BOT / $STEWARD_BOT)"
    fi
  else
    bad "IDENTITY_MODE=three needs REVIEW_BOT+STEWARD_BOT set and FIX_BOT distinct from both after normalizing ([bot]-suffix-insensitive, case-insensitive) — review+steward may coincide"
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
    # The push gate (wave P0-B/B6) probes `gh pr list` per gated push and FAILS
    # CLOSED on error — an unauthed/broken gh here means every governed-branch
    # push is denied forever, not silently allowed. Assert the probe works now.
    gh pr list -R "$REPO" --state open --limit 1 --json number >/dev/null 2>&1 \
      && ok "gh pr list works ($REPO) — push-gate probe healthy" \
      || bad "gh pr list failed for $REPO — the fail-closed push gate would deny every governed push (fix gh auth/network first)"
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
