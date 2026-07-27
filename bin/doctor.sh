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

# review C1/P1R-6: the ONE place doctor compares GitHub logins. Mirrors
# domain/identity.py's normalize_login (strip whitespace, casefold, then
# strip ONE trailing `[bot]` suffix) — a raw `!=` lets `fix-bot` vs
# `fix-bot[bot]` pass doctor even though they're the SAME GitHub identity in
# two API surface forms, and GitHub then 422s the self-approval on every PR,
# forever. The whitespace strip MUST run first: `FIX_BOT='fix-bot '` (a
# trailing space, e.g. round-tripped through a shell env file) has to lose
# the space BEFORE the `[bot]`-suffix check, or the two never normalize to
# the same string and doctor prints "two distinct identities" over a pair
# that GitHub-side is really the SAME bot with padding (review P1R-6 — this
# used to defeat the check entirely, reproducing the original C1 symptom).
_normalize_login() {
  local l="$1"
  # trim leading/trailing whitespace (standard bash idiom — no external tools)
  l="${l#"${l%%[![:space:]]*}"}"
  l="${l%"${l##*[![:space:]]}"}"
  l="${l,,}"
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

echo "== config snapshot (config/.active.<name>.env) =="
# review P0R-1 fold-in: `.active.env` is a SNAPSHOT. A machine whose snapshot was
# composed by the pre-P0R-1 bin/compose-env.sh (whose PRE/POST diff silently
# dropped any var the files assign to the value the launching shell already had)
# keeps that broken file until somebody re-runs point-at — and `install-systemd.sh`
# copies it, so the omission survives reboots. The observed symptom was total:
# TEST_CMD missing → the gate resolves no test command → every push and every
# `gh pr create` denied, forever, with nothing anywhere saying why.
#
# The check recomposes the two SOURCE files in a subshell and compares the
# variable NAMES against the snapshot. That expectation is derived independently
# of compose-env.sh's own logic (re-review §5: "assert set equality against an
# independently-derived expectation, never membership of the happy-path
# examples"), and there is no hand-maintained knob list to fall out of date.
# Sourcing — rather than only scanning assignment lines — is what keeps it quiet
# about a name the overlay assigns and then `unset`s (R2-11): absent from both
# sides, so not a discrepancy.
# Per-name snapshots (config/.active.<name>.env) plus the legacy fixed
# config/.active.env a pre-per-name compose may have left behind: check ALL of
# them — with concurrent deployments any one being stale is a real finding.
shopt -s nullglob
_ACTIVE_FILES=("$HARNESS_ROOT"/config/.active*.env)
shopt -u nullglob
if [ ${#_ACTIVE_FILES[@]} -eq 0 ]; then
  ok "no composed config/.active.<name>.env yet — nothing to compare (point-at writes it)"
fi
for _ACTIVE_ENV in "${_ACTIVE_FILES[@]}"; do
  _ACTIVE_BASE="${_ACTIVE_ENV##*/}"
  _SHARED_ENV="$HARNESS_ROOT/config/shared.env"
  # compose-env.sh stamps the composing overlay into the first line.
  _ACTIVE_NAME="$(sed -n '1s|.*repos/\([A-Za-z0-9_.-]*\)\.env.*|\1|p' "$_ACTIVE_ENV")"
  _ACTIVE_OVERLAY="$HARNESS_ROOT/config/repos/$_ACTIVE_NAME.env"
  if [ -z "$_ACTIVE_NAME" ] || [ ! -f "$_ACTIVE_OVERLAY" ]; then
    warn "cannot tell which overlay composed config/$_ACTIVE_BASE (missing/edited AUTO-GENERATED header) — skipping the staleness check; re-run 'make point-at REPO=<name>' to regenerate it"
  else
    # Names the two files assign AND that survive sourcing them.
    _expected_names="$(
      set +u
      set -a
      # shellcheck disable=SC1090
      [ -f "$_SHARED_ENV" ] && . "$_SHARED_ENV"
      # shellcheck disable=SC1090
      . "$_ACTIVE_OVERLAY"
      set +a
      grep -hoE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' \
        "$_SHARED_ENV" "$_ACTIVE_OVERLAY" 2>/dev/null \
        | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' \
        | sort -u \
        | while IFS= read -r _n; do
            [ -n "$_n" ] || continue
            [ -n "${!_n+set}" ] && printf '%s\n' "$_n"
          done
    )"
    _snapshot_names="$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$_ACTIVE_ENV" \
      | sed 's/=$//' | sort -u)"
    _missing_names="$(comm -23 \
      <(printf '%s\n' "$_expected_names" | sed '/^$/d' | sort -u) \
      <(printf '%s\n' "$_snapshot_names" | sed '/^$/d' | sort -u) | tr '\n' ' ')"
    _missing_names="${_missing_names%"${_missing_names##*[![:space:]]}"}"
    if [ -n "$_missing_names" ]; then
      bad "config/$_ACTIVE_BASE is STALE — it omits $(printf '%s' "$_missing_names" | wc -w | tr -d ' ') variable(s) that config/shared.env or config/repos/$_ACTIVE_NAME.env assign: $_missing_names — regenerate it with 'make point-at REPO=$_ACTIVE_NAME' (or 'issue-agent start'). A snapshot written by the pre-P0R-1 compose-env.sh drops assigned vars silently; if TEST_CMD is among them the gate denies every push and 'gh pr create' until this is fixed."
    else
      ok "config/$_ACTIVE_BASE matches config/shared.env + repos/$_ACTIVE_NAME.env"
    fi
  fi
done

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
  # Mirrors app/config.py::_repo_slug — "owner/name" -> "owner__name"
  # (casefolded), blank/unset REPO -> the fixed "unknown" slug.
  #
  # R2-10: the separator changed from "-" to "__". A single "-" collided
  # whenever EITHER segment already contained a hyphen (`acme/app-b` and
  # `acme-app/b` both -> `acme-app-b`), since "-" is legal inside both a
  # GitHub owner login and a repo name. "_" is NOT legal in a GitHub owner
  # login at all, so splitting on "__" can never be ambiguous. Casefolded too,
  # so case-variant spellings of the same repo share one state dir.
  _repo_slug() {
    local r
    r="$(printf '%s' "${1:-}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -z "$r" ]; then
      printf 'unknown\n'
      return
    fi
    r="${r//\//__}"
    printf '%s\n' "${r,,}"
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
_reported_bad=""   # vars already failed by the placeholder/example scan below
_is_bad_var() { case " $_reported_bad " in *" $1 "*) return 0 ;; esac; return 1; }
# P1R-6: check the NORMALIZED value, not the raw one — a whitespace-only
# FIX_BOT ("   ") is non-empty by `-n` but carries no real identity.


# An UNEDITED template value passes every emptiness check — `FIX_BOT` left as
# the literal `<your-gh-login>` is non-empty, so doctor used to green-light it.
# The fixer would then compare every PR author against that literal, match
# nothing, and silently do no work forever: the exact silent-identity-mismatch
# class of review C1, reintroduced through config rather than code. Any value
# still wrapped in <angle brackets> is a placeholder nobody filled in.
_placeholder_vars="REPO FIX_BOT TEST_CMD TARGET_DIR BASE_BRANCH"
# Scope the identity fields to the path actually in use, or doctor fails over
# fields the operator is CORRECT to leave untouched:
#   solo                     → no second identity at all; none of these are read
#   three + REVIEW_BOT_TOKEN → static-token path; the GH_APP_* block is unused
#   three, no token          → GitHub App path; the App fields are required
# (app/main.py `_app_identity` resolves App token → static token → ambient.)
if [ "${IDENTITY_MODE:-three}" = "three" ]; then
  _placeholder_vars="$_placeholder_vars REVIEW_BOT STEWARD_BOT"
  if [ -z "${REVIEW_BOT_TOKEN:-}" ]; then
    _placeholder_vars="$_placeholder_vars GH_APP_ID GH_APP_INSTALL_ID GH_APP_KEY_FILE"
  fi
fi
for _var in $_placeholder_vars; do
  case "${!_var:-}" in
    *"<"*">"*)
      bad "$_var is still the template placeholder (${!_var}) — edit config/shared.env or the repo overlay"
      _reported_bad="$_reported_bad $_var"
      continue ;;
  esac
  # The `<angle bracket>` form is not the only unedited value that ships: the
  # examples in config/agent.env.example are PLAUSIBLE-looking (owner/repo,
  # fix-bot, review-bot) and so sailed through the bracket check, letting
  # doctor report "✓ three distinct identities (fix-bot / review-bot /
  # steward-bot)" for three accounts that do not exist. A confident green on a
  # config nobody edited is worse than no check at all.
  case "$_var=${!_var:-}" in
    "REPO=owner/repo"|"REPO=owner/name"|"FIX_BOT=fix-bot"|"REVIEW_BOT=review-bot"|"STEWARD_BOT=steward-bot")
      bad "$_var is still the shipped example value (${!_var}) — this config was never edited"
      _reported_bad="$_reported_bad $_var" ;;
  esac
done

if _is_bad_var REPO; then :                       # already reported, no ✓
elif [ -n "$REPO" ]; then ok "REPO=$REPO"
else bad "REPO unset"; fi
if _is_bad_var FIX_BOT; then :
elif [ -n "$(_normalize_login "$FIX_BOT")" ]; then ok "FIX_BOT=$FIX_BOT"
else bad "FIX_BOT unset"; fi

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
  # Distinctness is meaningless if the logins are unedited examples — three
  # DIFFERENT fake names are still "distinct". Suppress the ✓ so the scan's ✗
  # is the only verdict on this line.
  if _is_bad_var FIX_BOT || _is_bad_var REVIEW_BOT || _is_bad_var STEWARD_BOT; then
    :
  elif [ -n "$REVIEW_BOT" ] && [ -n "$STEWARD_BOT" ] \
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

    # Everything above ran as the FIXER (ambient gh auth). The reviewer uses a
    # different identity, and nothing checked it could reach the repo at all —
    # a bot account whose collaborator invite was never accepted authenticates
    # fine and then 404s on every PR, which surfaces as "the reviewer silently
    # does nothing" rather than as an error. Verify the token's identity AND
    # its access, since a PAT pasted from the wrong account is the same class
    # of silent identity mismatch as review C1.
    if [ -n "${REVIEW_BOT_TOKEN:-}" ]; then
      # gh prints its error BODY to stdout on a bad token, so an unguarded
      # capture yields `{ "message": ...` and reports it as a login. A GitHub
      # login is [A-Za-z0-9-] only, so charset-validating the first line both
      # sanitises the value and detects the failure.
      _bot_login=$(GH_TOKEN="$REVIEW_BOT_TOKEN" gh api user --jq '.login' 2>/dev/null \
                   | head -1 | tr -d '[:space:]')
      case "$_bot_login" in
        *[!A-Za-z0-9-]*) _bot_login="" ;;
      esac
      if [ -z "$_bot_login" ]; then
        bad "REVIEW_BOT_TOKEN is invalid, expired, or lacks read:user (gh api user failed)"
      elif [ "$(_normalize_login "$_bot_login")" != "$(_normalize_login "${REVIEW_BOT:-}")" ]; then
        bad "REVIEW_BOT_TOKEN belongs to '$_bot_login' but REVIEW_BOT says '${REVIEW_BOT:-unset}' — GitHub would 422 every approval"
      elif ! GH_TOKEN="$REVIEW_BOT_TOKEN" gh repo view "$REPO" --json name >/dev/null 2>&1; then
        bad "review identity '$_bot_login' cannot access $REPO — invite it as a collaborator (write) and ACCEPT the invite from that account"
      else
        # Read access is NOT enough: submitting a review is a write. A bot that
        # can read but not push sails through this section and then 403s on
        # POST /pulls/N/reviews — after a full, paid review session has already
        # run and produced a verdict that can never be posted. Observed exactly
        # that way on the first real deployment (403 "Resource not accessible by
        # personal access token"). So probe the push bit here, not at approve time.
        _perm="$(GH_TOKEN="$REVIEW_BOT_TOKEN" gh api "repos/$REPO" --jq '.permissions.push' 2>/dev/null)"
        if [ "$_perm" = "true" ]; then
          ok "review identity '$_bot_login' authenticates and has WRITE access to $REPO"
          # `.permissions.push` reports the COLLABORATOR ROLE. A fine-grained PAT
          # carries a second, independent permission set that this (or any other
          # read-only call) cannot introspect — so push=true still 403s the
          # approve if the token was minted without Pull requests: Read+Write.
          # That is exactly how the first real deployment failed: label writes
          # succeeded (Issues: write) while POST /pulls/N/reviews came back
          # "Resource not accessible by personal access token". Say so, because a
          # bare ✓ here reads as "approvals will work" and it cannot promise that.
          case "$REVIEW_BOT_TOKEN" in
            github_pat_*)
              warn "REVIEW_BOT_TOKEN is a fine-grained PAT — its per-scope grants are not readable via the API. If approvals 403 with 'Resource not accessible by personal access token', re-mint it with Pull requests: Read+Write (Issues: Read+Write, Contents: Read)" ;;
          esac
        else
          bad "review identity '$_bot_login' can READ $REPO but not write (permissions.push=${_perm:-unknown}) — every approval will 403 AFTER the review is paid for. Invite it as a collaborator with WRITE and accept the invite from that account; a fine-grained PAT additionally needs Pull requests: Read+Write, Issues: Read+Write, Contents: Read"
        fi
      fi
    fi
  fi
else
  bad "gh missing"
fi

echo "== project commands =="
if [ -n "${TEST_CMD:-}" ]; then
  ok "TEST_CMD=$TEST_CMD (the gate runs THIS; it wins over the agent's .harness/test-cmd — review P1R-18)"
  [ "${RED_MUST_FAIL:-1}" = 0 ] \
    && warn "RED_MUST_FAIL=0 — the anti-tautological-test (red-fail) gate is OFF; a test that passes without the change won't be caught"
else
  # Corrected claim (review M6/§4): with no TEST_CMD the gate does NOT "run no
  # tests" — it falls back to the agent's own .harness/test-cmd, and if that is
  # missing too it fails CLOSED (denies every push and `gh pr create`) unless
  # ALLOW_UNTESTED=1. Both are worse than they sound: the fallback is a command
  # the gated agent chose, and the fail-closed path halts the harness.
  if [ "${ALLOW_UNTESTED:-0}" = 1 ]; then
    warn "TEST_CMD unset + ALLOW_UNTESTED=1 → if the agent writes no .harness/test-cmd, the test gate AND the anti-tautological-test (red-fail) gate are BOTH skipped. Set TEST_CMD to arm them."
  else
    warn "TEST_CMD unset → the gate falls back to the agent-written .harness/test-cmd, and denies every push / 'gh pr create' when that is missing too (fail-closed; ALLOW_UNTESTED=1 is the opt-out). Set TEST_CMD so the command the gate runs is yours, not the agent's."
  fi
fi
# Corrected claim (review M6/§4): this used to print "✓ LINT_CMD set" while the
# rule ran on the codex path ONLY — the default SDK path had no equivalent, so
# the ✓ described a command nothing executed. It is now enforced by both
# (domain/gate.py:check_project_commands), which is what makes the ✓ true.
[ -n "${LINT_CMD:-}" ] \
  && ok "LINT_CMD=$LINT_CMD (blocks a commit on failure)" \
  || warn "LINT_CMD unset (commit gate won't lint)"
[ -n "${TYPECHECK_CMD:-}" ] \
  && ok "TYPECHECK_CMD=$TYPECHECK_CMD (blocks a commit on failure)" \
  || warn "TYPECHECK_CMD unset (commit gate won't typecheck)"

echo ""
printf 'doctor: %d error(s), %d warning(s)\n' "$ERR" "$WARN"
[ "$ERR" -eq 0 ]
