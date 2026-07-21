#!/usr/bin/env bash
# Shared, STATE-DERIVED gate checks used by BOTH check-pr.sh (gh pr create) and
# check-push.sh (git push to an open PR). These derive truth from git + files in
# the worktree cwd — they do NOT depend on any command argv or PR body. The
# body/marker/adversarial checks live ONLY in check-pr.sh (validated at PR
# creation; a push carries no body).
#
# Source this AFTER defining a `fail()` that prints a reason and exits non-zero.
# Callers set: base (base branch), range (origin/base...HEAD), branch (current).
#
# bash 3.2-safe: no mapfile, no process substitution — here-strings only.

# Shared file-name pattern for "this looks like a test file".
PR_TEST_FILE_RE='(e2e|integration|regression|test|spec).*\.(spec|test)\.|(^|/)test_.*\.py|.*_test\.(py|go|ts|js)|tests?/.*\.py'

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

# Resolve the test command to run. PREFER the agent-declared, per-PR command in
# .harness/test-cmd over the static $TEST_CMD. The agent knows what it tested,
# the repo's runner, and which fixtures to avoid (e.g. scoping pytest to one file
# to dodge pytest-docker) — a single static config string cannot track that
# per-issue. Letting the agent choose does NOT weaken the gate: §10 reverts the
# implementation and requires THIS command to go red, so a weak/irrelevant command
# stays green on revert and is blocked. The command's relevance is exactly what
# red-fail proves. Falls back to $TEST_CMD when the marker is absent/blank.
# Echoes the resolved command (empty if neither is set).
prc_resolve_test_cmd() {
  local m=".harness/test-cmd" c=""
  [ -f "$m" ] && c="$(tr -d '\r' < "$m" | sed '/^[[:space:]]*$/d' | head -1)"
  [ -n "$c" ] || c="${TEST_CMD:-}"
  printf '%s' "$c"
}

# §1 — local test suite must pass (CI re-runs authoritatively, when present).
#
# With no TEST_CMD this check AND the red-fail check (§10) silently do nothing —
# the gate fires but has no teeth, so a tautological/wrong-layer test (the #39
# class) sails through. That is a silent degradation on any target that hasn't
# set TEST_CMD, so make it LOUD: warn to stderr (the agent sees it; it lands in
# the hook log) exactly once per process. We still allow — TEST_CMD is genuinely
# optional for some repos — but never silently.
prc_check_tests() {
  local tcmd; tcmd="$(prc_resolve_test_cmd)"
  if [ -n "$tcmd" ]; then
    eval "$tcmd" || fail "local test suite failed: $tcmd"
  else
    echo "GATE WARNING: no test command (neither .harness/test-cmd nor TEST_CMD) — the test gate AND the red-fail (anti-tautological-test) check are DISABLED. A test that passes without the change will not be caught. Have the agent write .harness/test-cmd, or set TEST_CMD." >&2
  fi
}

# §6 — every commit subject on the branch uses an allowed prefix, and no `green:`
#       precedes a `red:` (re-derived from the git LOG, closes the -F bypass).
prc_check_commit_log() {
  local seen_red=0 green_before_red=0 s log_subjects phase
  log_subjects=$(git log "$range" --reverse --format='%s' 2>/dev/null || true)
  while IFS= read -r s; do
    [ -n "$s" ] || continue
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

# §7b — existing test files must not be modified/deleted; only NEW tests added.
prc_check_tests_immutable() {  # prc_check_tests_immutable "<changed-file-list>"
  local f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '%s' "$f" | grep -qE "$PR_TEST_FILE_RE" || continue
    if git cat-file -e "origin/$base:$f" 2>/dev/null || git cat-file -e "$base:$f" 2>/dev/null; then
      fail "existing test file '$f' was modified/deleted — add NEW tests instead of changing existing ones"
    fi
  done <<< "$1"
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
# Skipped when TEST_CMD is empty, RED_MUST_FAIL=0, there are no new tests, or
# there is no implementation to revert (a pure-test diff can't be made red).
# Fail-OPEN on any git/infra error — a degraded simulation must never wedge a
# legitimate PR; it logs and allows.
prc_check_red_fails() {  # prc_check_red_fails "<changed-file-list>"
  [ "${RED_MUST_FAIL:-1}" = 0 ] && return 0
  local tcmd; tcmd="$(prc_resolve_test_cmd)"
  [ -n "$tcmd" ] || return 0

  # Resolve the bare base ref (prefer origin/, fall back to local).
  local baseref="" ref
  for ref in "origin/$base" "$base"; do
    git rev-parse --verify --quiet "$ref" >/dev/null 2>&1 && { baseref="$ref"; break; }
  done
  [ -n "$baseref" ] || { echo "red-check: cannot resolve base '$base' — skipping (fail-open)" >&2; return 0; }

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
  # §1 ran (agent-declared .harness/test-cmd, else $TEST_CMD).
  local passed=0
  eval "$tcmd" >/dev/null 2>&1 && passed=1

  # Restore now (don't wait for EXIT) so a fail message reflects a clean tree.
  git checkout -q HEAD -- "${impl_all[@]}" 2>/dev/null || true
  trap - EXIT

  [ "$passed" = 0 ] || fail "the new test(s) still PASS with the implementation reverted — they do not exercise the change (tautological or wrong-layer test). Assert on the observable behaviour the change produces, not on a constant that is always true. New test file(s): ${new_tests[*]}"
  return 0
}
