#!/usr/bin/env bash
# Shared, STATE-DERIVED gate checks used by BOTH check-pr.sh (gh pr create) and
# check-push.sh (git push to an open PR). These derive truth from git + files in
# the worktree cwd — they do NOT depend on any command argv or PR body. The
# body/marker/adversarial checks live ONLY in check-pr.sh (validated at PR
# creation; a push carries no body).
#
# Source this AFTER defining a `fail()` that prints a reason and exits non-zero.
# Callers set: base (base branch), branch (current), and the TWO ranges below
# (see prc_resolve_base_ref) — `diff_range` and `log_range` are NOT
# interchangeable, which is what review M6 caught:
#
#   diff_range = "<baseref>...HEAD"   THREE dots. For `git diff`, three dots
#                                     means "base..merge-base..HEAD", i.e. only
#                                     what THIS branch changed. Correct.
#   log_range  = "<baseref>..HEAD"    TWO dots. For `git log`, THREE dots is the
#                                     SYMMETRIC difference — it also lists the
#                                     commits that are on the BASE and not on
#                                     the branch. The gate reused one
#                                     three-dot `range` var for both, so as soon
#                                     as the base branch advanced (any merge into
#                                     dev), prc_check_commit_log saw base-only
#                                     subjects like "Merge pull request #7 …",
#                                     which carry no allowed prefix, and BLOCKED
#                                     legitimate PRs and pushes on the codex path
#                                     — permanently, for every branch, until the
#                                     branch was rebased. The Python gate
#                                     (adapters/worktree.py:build_gate_context)
#                                     always used `..` for the log and `...` for
#                                     the diff; tests/test_gate_parity.py now
#                                     pins the two stacks together.
#
# bash 3.2-safe: no mapfile, no process substitution — here-strings only.

# Shared file-name pattern for "this looks like a test file".
PR_TEST_FILE_RE='(e2e|integration|regression|test|spec).*\.(spec|test)\.|(^|/)test_.*\.py|.*_test\.(py|go|ts|js|rs)|tests?/.*\.(py|rs)'

# Allowed commit-subject prefix: a bare harness prefix (`red: `) OR a conventional
# commit optionally carrying the phase as a scope (`test(red): `). The scoped form
# lets the harness run on a target with a commitlint commit-msg hook
# (@commitlint/config-conventional) that rejects a bare `red:`. Kept in sync with
# domain/gate.py VALID_COMMIT_PREFIX.
PR_VALID_PREFIX_RE='^(plan|red|green|evidence|docs|chore|refactor|test|fix|feat|build|ci|perf|revert|style)(\([^)]*\))?: '

# TDD/lifecycle phase (red|green|plan|evidence) from a bare or conventional-scoped
# subject; '' if none. Single source of truth for phase detection in the bash gate,
# mirroring domain/gate.py:commit_phase().
commit_phase() {
  case "$1" in
    plan:*|red:*|green:*|evidence:*) echo "${1%%:*}" ;;
    *\(red\):*)      echo red ;;
    *\(green\):*)    echo green ;;
    *\(plan\):*)     echo plan ;;
    *\(evidence\):*) echo evidence ;;
    *) echo "" ;;
  esac
}

# Resolve the test command to run. PREFER the OPERATOR's $TEST_CMD; fall back to
# the agent-declared, per-PR `.harness/test-cmd` only when the operator set none.
# Echoes the resolved command (empty if neither is set).
#
# The precedence used to be the other way round, and that made the gate
# self-defeating (review P1R-18): prc_check_tests is the push gate's only real
# teeth, the gated agent WRITES `.harness/test-cmd`, and it runs under
# bypassPermissions — so `echo true > .harness/test-cmd` disarmed §1 AND §10 (the
# anti-tautological-test check) in one line, without tripping any bypass
# detection, because the governor only inspects git/gh shapes.
#
# The old order was not accidental: AGENT.md asks for a SCOPED command so the
# revert-and-rerun is fast and hits the changed package in a monorepo. That
# intent survives — the marker is still the whole answer on a repo where the
# operator has no TEST_CMD (the test-discovery path that writes it exists for
# exactly those) — but where an operator command exists it is the authority. The
# cost is a slower gate run; the alternative is a one-line disarm.
#
# Byte-for-byte the same order as domain/gate.py:resolve_test_cmd(). The two
# stacks disagreed on this for one wave (the Python side flipped in P1R-18, the
# bash did not), which is exactly the drift tests/test_gate_parity.py exists to
# make impossible.
prc_resolve_test_cmd() {
  local m=".harness/test-cmd" c="${TEST_CMD:-}"
  # A TRACKED marker is repo history, not this session's claim (keifu
  # #167→#156: a committed marker retargeted the gate repo-wide) — ignore it.
  if [ -z "$c" ] && [ -f "$m" ] \
      && ! git ls-files --error-unmatch "$m" >/dev/null 2>&1; then
    c="$(tr -d '\r' < "$m" | sed '/^[[:space:]]*$/d' | head -1)"
  fi
  printf '%s' "$c"
}

# The base ref the gate compares against: prefer the remote-tracking `origin/<base>`
# (what the PR will actually merge into), fall back to a local `<base>`. Echoes
# '' when neither resolves. Single source of truth so check-pr.sh, check-push.sh
# and prc_check_red_fails cannot drift apart on which ref they mean.
prc_resolve_base_ref() {
  local ref
  for ref in "origin/$base" "$base"; do
    if git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
      printf '%s' "$ref"; return 0
    fi
  done
  printf ''
}

# Is running untested (no resolvable test command) explicitly permitted? This is
# an OPERATOR decision made in the repo overlay (config/repos/<name>.env), never
# an agent one: the checker inherits the daemon's environment, so an inline
# `ALLOW_UNTESTED=1 gh pr create …` from the agent does NOT reach here.
# Keep in sync with domain/gate.py (the `allow_untested` flag threaded through
# check_tests/check_red_fails).
prc_allow_untested() { [ "${ALLOW_UNTESTED:-0}" = 1 ]; }

# ── Guarded execution of TARGET-OWNED commands ────────────────────────────────
# The command is configured by the harness (or written by the agent via
# .harness/test-cmd), but is owned by the target. Every eval of such a command
# MUST go through prc_run_test_cmd — the bash twin of
# adapters/governor.py:run_target_command. Two defenses (#63 fork bomb):
#   1. env scrub — deployment knobs must not flow into the target's tests;
#   2. re-entrancy bound — HARNESS_GATE_DEPTH is stamped into every child and
#      executing at depth ≥ 2 is refused with rc 213. Call sites treat 213 as
#      "configuration defect, fail CLOSED" — a different verdict than a red
#      suite. Legitimate nesting never executes at depth ≥ 2 (0 = a real
#      gate/probe run, 1 = a suite fixture's own eval).
# HARNESS_GATE_DEPTH is deliberately NOT in the -u list and NOT unset by the
# test-suite sanitizers: clearing it re-arms the bomb. The counter lives in the
# env, so a TEST_CMD/marker that wraps itself in `env -i`/`env -u
# HARNESS_GATE_DEPTH` erases it — doctor warns about such configs; that
# documented edge is the price of keeping this mechanism simple.

PRC_GATE_RECURSION_RC=213
PRC_TEST_TIMEOUT_RC=124

# Static floor of knobs to scrub, mirrored in governor.TARGET_CMD_SCRUB_KNOBS
# and point-at's RTC_SCRUB_STATIC (drift-checked by
# tests/test_deployment_env_sanitization.sh). The deployment's FULL knob set
# arrives dynamically via HARNESS_DEPLOY_KNOBS (written by compose-env.sh), so
# a newly added knob is scrubbed without touching these lists.
PRC_SCRUB_STATIC="TEST_CMD LINT_CMD TYPECHECK_CMD EDIT_CHECK_CMD ALLOW_UNTESTED RED_MUST_FAIL VERIFY_RED_AT_COMMIT BASE_BRANCH GATE_TEST_TIMEOUT_S GATE_TEST_FAILURE_EXIT_CODES GATE_GH_RETRY_SLEEP HARNESS_SCREENSHOTS_READ GH_APP_ID GH_APP_KEY_FILE GH_APP_INSTALL_ID TARGET_DIR AGENT_ENV ISSUE_AGENT_DEPLOYMENT_ID"

prc_run_test_cmd() {  # → rc; 213=recursion, 124=timeout
  local _depth="${HARNESS_GATE_DEPTH:-0}" _v _timeout="${GATE_TEST_TIMEOUT_S:-300}"
  case "$_depth" in ''|*[!0-9]*) _depth=0 ;; esac
  if [ "$_depth" -ge 2 ]; then
    echo "GATE RECURSION: refusing to run a test command at gate depth $_depth — the resolved command re-entered the harness gate (self-referential TEST_CMD/.harness/test-cmd, see #63): $1" >&2
    return "$PRC_GATE_RECURSION_RC"
  fi
  local _scrub=()
  for _v in $PRC_SCRUB_STATIC ${HARNESS_DEPLOY_KNOBS:-} HARNESS_DEPLOY_KNOBS; do
    case "$_v" in [A-Za-z_]*) _scrub+=(-u "$_v") ;; esac
  done
  case "$_timeout" in ''|*[!0-9]*) _timeout=300 ;; esac
  [ "$_timeout" -gt 0 ] || _timeout=300
  if command -v timeout >/dev/null 2>&1; then
    env "${_scrub[@]}" \
      HARNESS_GATE_DEPTH="$((_depth + 1))" \
      timeout --signal=TERM --kill-after=5 "$_timeout" bash -c "$1"
  else
    # macOS has no coreutils timeout by default. The Python/SDK verifier remains
    # authoritative there; this advisory stack refuses rather than claiming a
    # potentially unbounded command produced trusted evidence.
    echo "red-check: timeout command unavailable; refusing target command" >&2
    return "$PRC_TEST_TIMEOUT_RC"
  fi
}

# Does this process status mean the configured test runner observed an
# assertion failure? The operator owns this contract; the target command never
# inherits the knob. Invalid declarations fail closed. Timeout/spawn/signal
# statuses are reserved and can never be opted into as red evidence.
prc_is_confirmed_test_failure_rc() {
  local wanted="$1" token raw="${GATE_TEST_FAILURE_EXIT_CODES:-1}" found=1
  raw="${raw//,/ }"
  for token in $raw; do
    case "$token" in ''|*[!0-9]*) return 1 ;; esac
    [ "$token" -gt 0 ] && [ "$token" -lt 124 ] || return 1
    [ "$token" -eq "$wanted" ] && found=0
  done
  return "$found"
}

# §1 — local test suite must pass (CI re-runs authoritatively, when present).
#
# With no test command this check AND the red-fail check (§10) have no teeth, so
# a tautological/wrong-layer test (the #39 class) sails through. Warning to
# stderr was not enough: "no test command" then looked EXACTLY like "tests
# passed" to everything downstream (the gate exits 0 either way), which is a
# silent fail-open on any target that never set TEST_CMD. So DENY by default and
# make the operator opt out deliberately with ALLOW_UNTESTED=1 (then we keep the
# old warn-and-pass, because a genuinely test-less repo does exist).
prc_check_tests() {
  local tcmd _trc; tcmd="$(prc_resolve_test_cmd)"
  if [ -n "$tcmd" ]; then
    prc_run_test_cmd "$tcmd"; _trc=$?
    if [ "$_trc" -eq "$PRC_GATE_RECURSION_RC" ]; then
      fail "GATE RECURSION: the test command re-entered the harness gate and was refused (self-referential TEST_CMD/.harness/test-cmd — see #63); this is a test-command configuration defect, NOT a failing suite: $tcmd"
    fi
    [ "$_trc" -eq 0 ] || fail "local test suite failed: $tcmd"
  elif prc_allow_untested; then
    echo "GATE WARNING: no test command (neither .harness/test-cmd nor TEST_CMD) — the test gate AND the red-fail (anti-tautological-test) check are DISABLED by ALLOW_UNTESTED=1. A test that passes without the change will not be caught." >&2
  else
    fail "no test command resolved (neither .harness/test-cmd nor TEST_CMD) — the test gate and the red-fail (anti-tautological-test) check cannot run, so the gate fails CLOSED instead of passing you silently. Write the exact command you used to run your tests into .harness/test-cmd (one line, non-watch, must exit), or have the operator set TEST_CMD. If this repo genuinely has no test suite, the operator sets ALLOW_UNTESTED=1 in its overlay config."
  fi
}

# §6 — every commit subject on the branch uses an allowed prefix, and no `green:`
#       precedes a `red:` (re-derived from the git LOG, closes the -F bypass).
#
# Uses `log_range` (TWO dots) — the commits THIS branch adds. It used to share the
# diff's three-dot `range`, which for `git log` is the symmetric difference and
# therefore also fed it the base branch's own commits (review M6). See the header.
prc_check_commit_log() {
  local seen_red=0 green_before_red=0 s log_subjects phase
  # Commits already on the observed remote PR branch are published history the
  # agent cannot rewrite (the broker requires them as ancestors), so their
  # subjects are exempt from the prefix rule — GitHub's own "Update branch"
  # merge commit ("Merge branch '<base>' into <branch>") wedged keifu PR #182
  # (2026-08-03) between the broker and this gate. A red: among them still
  # counts for ordering; each published subject exempts ONE occurrence (twin
  # of domain/gate.py:check_commit_log — keep the two in lockstep).
  local branch published_ref="" published_subjects=""
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [ -n "$branch" ] && [ "$branch" != "HEAD" ] \
    && git rev-parse -q --verify "refs/remotes/origin/$branch" >/dev/null 2>&1; then
    published_ref="refs/remotes/origin/$branch"
    published_subjects=$(git log "${log_range%%..*}..$published_ref" --reverse --format='%s' 2>/dev/null || true)
  fi
  # Plain array, not declare -A: macOS deployments may run bash 3.2.
  local -a published_pool=()
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    published_pool+=("$s")
    if [ "$(commit_phase "$s")" = red ]; then seen_red=1; fi
  done <<< "$published_subjects"
  log_subjects=$(git log "$log_range" --reverse --format='%s' 2>/dev/null || true)
  local matched i
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    matched=0
    for i in "${!published_pool[@]}"; do
      if [ "${published_pool[$i]}" = "$s" ]; then
        unset "published_pool[$i]"
        matched=1
        break
      fi
    done
    if [ "$matched" = 1 ]; then continue; fi
    [[ "$s" =~ $PR_VALID_PREFIX_RE ]] \
      || fail "commit '${s:0:60}' lacks an allowed prefix (plan|red|green|evidence|docs|chore|refactor|test|fix|feat:, bare or scoped e.g. test(red):)"
    phase="$(commit_phase "$s")"
    case "$phase" in
      red)   seen_red=1 ;;
      green) [ "$seen_red" = 1 ] || green_before_red=1 ;;
    esac
  done <<< "$log_subjects"
  [ "$green_before_red" = 0 ] \
    || fail "a 'green:' commit appears before any 'red:' commit on the branch (TDD ordering)"
}

# §7 — evidence/ may contain ONLY committed screenshot images (visual proof that
# renders in the PR's Files-changed tab). Any non-image path under evidence/ stays
# gitignored and is rejected from the diff.
prc_check_no_evidence() {  # prc_check_no_evidence "<changed-file-list>"
  local bad
  bad=$(printf '%s\n' "$1" | grep '^evidence/' \
    | grep -ivE '\.(png|jpg|jpeg|gif|webp)$' || true)
  [ -z "$bad" ] \
    || fail "evidence/ may only contain screenshot images (.png/.jpg/.jpeg/.gif/.webp); non-image evidence stays gitignored: $bad"
  return 0
}

# .harness/ is harness-local control state and must never become repo content:
# a committed .harness/test-cmd (keifu #167 → #156, 2026-08-01) retargeted the
# trusted red-fail gate for EVERY downstream worktree to another issue's test.
prc_check_no_harness_state() {  # prc_check_no_harness_state "<changed-file-list>"
  local bad
  bad=$(printf '%s\n' "$1" | grep -E '^\.harness(/|$)' || true)
  [ -z "$bad" ] \
    || fail ".harness/ files are harness-local control state and are never committed — remove from the diff (git rm --cached): $bad"
  return 0
}

# Harness scaffolding may share a filename with a target's tracked instructions.
# Reject only our marker, leaving legitimate target-owned AGENTS*.md edits valid.
prc_check_no_generated_instructions() {  # "<changed-file-list>"
  local f
  for f in AGENTS.md AGENTS.override.md; do
    printf '%s\n' "$1" | grep -qxF "$f" || continue
    if git show "HEAD:$f" 2>/dev/null \
        | grep -qF '<!-- issue-agent generated instructions: start -->'; then
      fail "$f contains issue-agent generated instructions — restore the target-owned file"
    fi
  done
}

# §7b — existing test files must not be modified/deleted; only NEW tests added.
# #124: reviewer-granted gate exemptions. The fixer DECLARES via a
# `Gate-Exemption: <rule> <subject>` commit trailer (tracked history — visible
# in the PR, nothing left in the repo after merge); the gate lets a DECLARED
# violation through so the reviewer can judge it, and the merge side withholds
# auto-merge until REVIEW_BOT grants each declaration against the current
# patch id. Twin of domain/exemptions.py — keep the rule ids, the exemptable
# set (tests-immutable, required-test) and the tests-immutable cap of 8 in
# lockstep (raised from 3 after tauri #589, 2026-08-09: a shared interaction
# change forced mechanical path updates in four e2e specs; per-file grants are
# the authorization, the count only bounds the reviewer's hazard list). Unknown
# rules are inert.
prc_declared_exemption_subjects() {  # $1 rule → prints declared subjects, deduped
  # Subject charset/bound mirrors domain/exemptions.py _SUBJECT_TOKEN: `*` or
  # a bounded path-ish token — never arbitrary non-space text (it reaches the
  # reviewer prompt).
  git log "$log_range" --format='%B' 2>/dev/null \
    | sed -nE "s#^[ \t]*Gate-Exemption:[ \t]+$1[ \t]+(\*|[A-Za-z0-9._/+-]{1,200})[ \t]*\$#\1#p" \
    | sort -u
}

# Lines that LOOK like Gate-Exemption trailers but don't parse (typo'd rule,
# out-of-charset subject like `@scope/pkg`). Rightly inert, but silently so —
# the denial hint keeps a typo from recreating the #126 comply-how loop.
# Twin of domain/exemptions.py count_malformed_declarations.
prc_malformed_exemption_hint() {  # prints a hint suffix, or nothing
  local n
  # [[:blank:]] not [ \t]: grep -E does not expand \t inside a bracket
  # expression (the class would be {space, backslash, t} — round-3 MINOR 8),
  # while the sed twin above DOES expand it. [[:blank:]] is space+tab in both.
  n=$(git log "$log_range" --format='%B' 2>/dev/null \
    | grep -E '^[[:blank:]]*Gate-Exemption:' \
    | grep -cvE "^[[:blank:]]*Gate-Exemption:[[:blank:]]+(tests-immutable|required-test)[[:blank:]]+(\*|[A-Za-z0-9._/+-]{1,200})[[:blank:]]*\$" || true)
  [ "${n:-0}" -gt 0 ] || return 0
  printf '%s' " NOTE: $n Gate-Exemption line(s) in your commit messages did not parse and were ignored — the format is exactly 'Gate-Exemption: <rule> <subject>' where <rule> is an exemptable rule (tests-immutable, required-test) and <subject> is a plain path of [A-Za-z0-9._/+-] (or * for required-test)."
}

prc_check_tests_immutable() {  # prc_check_tests_immutable "<changed-file-list>"
  # AUTO-DECLARED (ADR 0014 revised 2026-08-03): every modified/deleted
  # existing test file is itself the exemption request — no trailer. The gate
  # only bounds SCOPE (>8 overflows the reviewer's per-file hazard list); the
  # merge side withholds until the reviewer explicitly grants each file. Twin
  # of gate.check_tests_immutable.
  # OLD-side paths (--diff-filter excludes additions; -M off so a renamed-away
  # test still reports its old path). The python twin derives the same set from
  # `diff --git` records, which likewise survive renames, binary files and
  # `.gitattributes -diff` — deriving from the NEW-side name list alone let a
  # renamed-away test escape entirely (round-4 adversarial review).
  #
  # THREE-dot `diff_range`, never the two-dot `log_range` (#178). For `git diff`
  # two dots means the ENDPOINT diff `base HEAD`, so every test file the BASE
  # advanced past this branch's merge-base counted as "modified by this branch",
  # in reverse. tauri PR #614 (2026-08-08) was blocked with a net diff of two
  # modified test files because dev had merged other PRs touching five more:
  # the endpoint diff reported seven, the branch's own diff two. Same class as
  # the review-M6 two-dot/three-dot mix-up documented in this file's header,
  # here in the opposite direction. The python twin counts from the three-dot
  # `changed` list (gate.check_tests_immutable), which is what $1 carries.
  local f n_modified=0 mdrt_range="${diff_range:-}"
  [ -n "$mdrt_range" ] || mdrt_range="${log_range%%..*}...HEAD"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '%s' "$f" | grep -qE "$PR_TEST_FILE_RE" || continue
    n_modified=$((n_modified + 1))
    printf '%s' "$f" | grep -qE '^[A-Za-z0-9._/+-]{1,200}$' \
      || fail "existing test file '$f' was modified, but its path cannot be carried in a reviewer grant (allowed: letters, digits, and . _ / + - up to 200 characters). Restore that file and put the change in a test file whose path is within the charset, or rename the file in a separate PR"
  done <<< "$(git diff --no-renames --diff-filter=MDRT --name-only "$mdrt_range" 2>/dev/null || printf '%s' "$1")"
  [ "$n_modified" -le 8 ] \
    || fail "more than 8 existing test files modified/deleted — changing existing tests is allowed (the reviewer must explicitly approve each one before merge), but this many reads as a rewrite, not a correction; reduce the change's scope (or split it) so at most 8 existing test files are touched, and prefer ADDING tests in new files wherever the old expectation is still right"
}

# §10 — the NEW tests must ACTUALLY FAIL without the implementation (real red).
#
# This is the gate that catches a tautological / wrong-layer test: a test that
# passes even when the change is absent provides zero regression protection (the
# #39 `expect('My App').toBe('My App')` class). The commit-ordering rule
# (§6) only checks LABELS — it never proves the `red:` test was genuinely red.
#
# Mechanism (in the agent's already-built worktree, so installed deps survive):
#   1. Split the diff into NEW test files vs implementation files.
#   2. Transiently revert the implementation to the base (new impl files removed,
#      modified impl files checked out at base), keeping the new tests at HEAD.
#   3. Run TEST_CMD. With the implementation gone, the new tests MUST fail
#      (suite → non-zero). If the suite still PASSES, the tests don't exercise
#      the change → block.
#   4. ALWAYS restore the worktree to HEAD (trap), even on early exit/kill.
#
# Deliberately create-only (check-pr.sh), not on every push: it runs TEST_CMD an
# extra time and only the initial diff has a clean red/green split to simulate.
# Skipped when RED_MUST_FAIL=0, there are no new tests, or there is no
# implementation to revert (a pure-test diff can't be made red).
# Fail-OPEN on any git/infra error — a degraded simulation must never wedge a
# legitimate PR; it logs and allows.
#
# NO test command is NOT such an infra error, though: it means this gate never
# ran at all, which is the exact condition #39 slipped through. So it fails
# CLOSED (unless the operator set ALLOW_UNTESTED=1), same rule as §1. §1 runs
# first in check-pr.sh and would already have denied — this is defence in depth,
# so reordering the sections can never silently reopen the hole.
prc_check_red_fails() {  # prc_check_red_fails "<changed-file-list>"
  [ "${RED_MUST_FAIL:-1}" = 0 ] && return 0
  local tcmd; tcmd="$(prc_resolve_test_cmd)"
  if [ -z "$tcmd" ]; then
    prc_allow_untested \
      && { echo "red-check: no test command — anti-tautological-test check DISABLED by ALLOW_UNTESTED=1" >&2; return 0; }
    fail "no test command resolved (neither .harness/test-cmd nor TEST_CMD) — the red-fail (anti-tautological-test) check cannot prove the new tests fail without the implementation. Write the command you used into .harness/test-cmd (one line, non-watch), or have the operator set TEST_CMD / ALLOW_UNTESTED=1."
  fi

  # Resolve the bare base ref (shared resolver — see prc_resolve_base_ref).
  local baseref; baseref="$(prc_resolve_base_ref)"
  [ -n "$baseref" ] || fail "red-check: cannot resolve base '$base' — trusted red-test evidence cannot be produced"

  # Partition changed files: new tests (keep at HEAD) vs implementation (revert).
  local f new_tests=() impl_existing=() impl_all=()
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if printf '%s' "$f" | grep -qE "$PR_TEST_FILE_RE" \
       && ! git cat-file -e "$baseref:$f" 2>/dev/null; then
      new_tests+=("$f"); continue          # a NEW test file → keep it at HEAD
    fi
    impl_all+=("$f")                         # implementation (or non-test change)
    git cat-file -e "$baseref:$f" 2>/dev/null && impl_existing+=("$f")
  done <<< "$1"

  [ "${#new_tests[@]}" -gt 0 ] || return 0   # no new tests → §5 owns "must add a test"
  [ "${#impl_all[@]}"  -gt 0 ] || { echo "red-check: pure-test diff, nothing to revert — skipping" >&2; return 0; }

  # Restore HEAD no matter how we leave. fail() calls exit (RETURN never fires),
  # so guarantee restoration with an EXIT trap; `git checkout HEAD -- <files>`
  # re-materialises both the reverted and the removed-new implementation files.
  # node_modules etc. are gitignored, so we never need `git clean -x`.
  # shellcheck disable=SC2064
  trap "git checkout -q HEAD -- ${impl_all[*]} 2>/dev/null || true" EXIT

  # Build the RED state: drop new impl, revert modified impl to base.
  for f in "${impl_all[@]}"; do
    if printf '%s\n' "${impl_existing[@]+"${impl_existing[@]}"}" | grep -qxF "$f"; then
      git checkout -q "$baseref" -- "$f" 2>/dev/null || { echo "red-check: revert of '$f' failed — skipping (fail-open)" >&2; return 0; }
    else
      rm -f "$f" 2>/dev/null || true
    fi
  done

  # With the implementation absent, the suite MUST be red. Run the SAME command
  # §1 ran (agent-declared .harness/test-cmd, else $TEST_CMD). A recursion
  # refusal (213) is neither red nor green — restore and fail CLOSED.
  local passed=0 _rrc=0
  prc_run_test_cmd "$tcmd" >/dev/null 2>&1; _rrc=$?
  [ "$_rrc" -eq 0 ] && passed=1

  # Restore now (don't wait for EXIT) so a fail message reflects a clean tree.
  git checkout -q HEAD -- "${impl_all[@]}" 2>/dev/null || true
  trap - EXIT

  if [ "$_rrc" -eq "$PRC_GATE_RECURSION_RC" ]; then
    fail "GATE RECURSION: the test command re-entered the harness gate and was refused (see #63) — the red-fail check cannot run. Fix the test-command configuration (self-referential TEST_CMD/.harness/test-cmd)."
  fi
  if [ "$_rrc" -eq "$PRC_TEST_TIMEOUT_RC" ]; then
    fail "red-test verification timed out — timeout is infrastructure failure, not evidence that the new test detects the reverted implementation"
  fi

  [ "$passed" = 0 ] || fail "the new test(s) still PASS with the implementation reverted — they do not exercise the change (tautological or wrong-layer test). Assert on the observable behaviour the change produces, not on a constant that is always true. New test file(s): ${new_tests[*]}"
  prc_is_confirmed_test_failure_rc "$_rrc" \
    || fail "red-test verification exited $_rrc, which is infrastructure/cancellation rather than a confirmed test-failure code. Only GATE_TEST_FAILURE_EXIT_CODES='${GATE_TEST_FAILURE_EXIT_CODES:-1}' may provide red evidence; fix the runner or its trusted exit-code contract."
  return 0
}
