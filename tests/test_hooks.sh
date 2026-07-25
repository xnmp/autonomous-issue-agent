#!/usr/bin/env bash
# Tests the check-*.sh hooks against real temporary git repos.
# Pure bash mini-framework (no bats dependency).
#
# Run: bash tests/test_hooks.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKS="$ROOT/plugin/hooks/checks"
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
no()   { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }

# assert_exit <expected_rc> <desc> -- <command...>
assert_exit() {
  local want="$1" desc="$2"; shift 2; [ "$1" = "--" ] && shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want" ]; then ok "$desc"; else no "$desc" "want rc=$want got rc=$rc; out: ${out:0:200}"; fi
}

# Make a throwaway git repo with a base branch + one base commit.
fresh_repo() {
  local d; d="$(mktemp -d)"
  (
    cd "$d"
    git init -q -b dev
    git config user.email t@t; git config user.name t
    git config commit.gpgsign false
    echo seed > seed.txt
    git add seed.txt
    git -c core.hooksPath=/dev/null commit -qm "chore: seed"
  )
  echo "$d"
}

echo "== check-branch-name.sh =="
assert_exit 0 "accepts fix/42-foo"        -- bash "$CHECKS/check-branch-name.sh" switch -c fix/42-foo
assert_exit 0 "accepts feat/3-bar"        -- bash "$CHECKS/check-branch-name.sh" checkout -b feat/3-bar
assert_exit 0 "accepts chore/5-deps"      -- bash "$CHECKS/check-branch-name.sh" switch -c chore/5-deps
assert_exit 0 "accepts refactor/8-split"  -- bash "$CHECKS/check-branch-name.sh" checkout -b refactor/8-split
assert_exit 1 "rejects bad name"          -- bash "$CHECKS/check-branch-name.sh" switch -c my-branch
assert_exit 1 "rejects unknown prefix"    -- bash "$CHECKS/check-branch-name.sh" switch -c wip/5-x
assert_exit 1 "rejects missing issue num" -- bash "$CHECKS/check-branch-name.sh" checkout -b fix/foo
assert_exit 0 "ignores non-create switch" -- bash "$CHECKS/check-branch-name.sh" switch main

echo "== check-commit.sh (prefix) =="
assert_exit 0 "accepts green:"   -- bash "$CHECKS/check-commit.sh" commit -m "green: implement x"
assert_exit 0 "accepts red:"     -- bash "$CHECKS/check-commit.sh" commit -m "red: failing test"
assert_exit 1 "rejects bad pfx"  -- bash "$CHECKS/check-commit.sh" commit -m "wip: stuff"
assert_exit 0 "allows -F (file)" -- bash "$CHECKS/check-commit.sh" commit -F /tmp/msg
assert_exit 0 "handles -m=glued" -- bash "$CHECKS/check-commit.sh" commit "-mgreen: glued"
# LINT_CMD wiring
out=$(LINT_CMD='false' bash "$CHECKS/check-commit.sh" commit -m "green: x" 2>&1); rc=$?
[ "$rc" -eq 1 ] && ok "LINT_CMD failure blocks" || no "LINT_CMD failure blocks" "rc=$rc"
out=$(LINT_CMD='true' bash "$CHECKS/check-commit.sh" commit -m "green: x" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "LINT_CMD success passes" || no "LINT_CMD success passes" "rc=$rc"
# verify-red-at-commit: a red: commit whose tests PASS is blocked; failing tests pass.
out=$(TEST_CMD='true'  bash "$CHECKS/check-commit.sh" commit -m "red: t" 2>&1); rc=$?
[ "$rc" -eq 1 ] && ok "red: with passing tests blocked" || no "red: with passing tests blocked" "rc=$rc"
out=$(TEST_CMD='false' bash "$CHECKS/check-commit.sh" commit -m "red: t" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "red: with failing tests allowed" || no "red: with failing tests allowed" "rc=$rc"
# green: is never subject to the red-check, even if tests pass.
out=$(TEST_CMD='true'  bash "$CHECKS/check-commit.sh" commit -m "green: x" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "green: not red-checked" || no "green: not red-checked" "rc=$rc"
# opt-out disables it.
out=$(VERIFY_RED_AT_COMMIT=0 TEST_CMD='true' bash "$CHECKS/check-commit.sh" commit -m "red: t" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "VERIFY_RED_AT_COMMIT=0 disables" || no "VERIFY_RED_AT_COMMIT=0 disables" "rc=$rc"

echo "== check-commit-order.sh (red before green) =="
# Case A: green with NO prior red on the branch → blocked.
d=$(fresh_repo)
( cd "$d" && git switch -qc fix/1-x && echo a>a && git add a && git -c core.hooksPath=/dev/null commit -qm "plan: types" )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit -m "green: impl" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "green without prior red is blocked" || no "green without prior red is blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Case B: green WITH a prior red on the branch → allowed.
d=$(fresh_repo)
( cd "$d" && git switch -qc fix/1-x \
    && echo a>a && git add a && git -c core.hooksPath=/dev/null commit -qm "red: failing test" )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit -m "green: impl" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "green with prior red is allowed" || no "green with prior red is allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Case C: a red: commit itself is never ordering-constrained.
d=$(fresh_repo)
( cd "$d" && git switch -qc fix/1-x )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit -m "red: first test" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "red: is unconstrained" || no "red: is unconstrained" "rc=$rc"
rm -rf "$d"

# Case D: a NON-green --amend (re-editing a tip) is allowed.
d=$(fresh_repo)
( cd "$d" && git switch -qc fix/1-x )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit --amend -m "docs: tidy" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "non-green --amend allowed" || no "non-green --amend allowed" "rc=$rc"
rm -rf "$d"

# Case D2: --amend to a green: with NO prior red → still BLOCKED (closed bypass).
d=$(fresh_repo)
( cd "$d" && git switch -qc fix/1-x \
    && echo a>a && git add a && git -c core.hooksPath=/dev/null commit -qm "chore: benign" )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit --amend -m "green: sneaky" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "amend→green without red is blocked" || no "amend→green without red is blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Case E: unresolvable base → fail-open (allow).
d=$(mktemp -d); ( cd "$d" && git init -q -b main && git config user.email t@t && git config user.name t )
out=$( cd "$d" && BASE_BRANCH=nonexistent bash "$CHECKS/check-commit-order.sh" commit -m "green: x" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "unresolvable base fails open" || no "unresolvable base fails open" "rc=$rc"
rm -rf "$d"

echo "== check-pr.sh (full gate) =="
# Build a compliant fix/ branch in a temp repo, then probe each gate.
setup_pr_repo() {
  local d; d=$(fresh_repo)
  (
    cd "$d"
    mkdir -p .claude .harness tests docs/adr
    git switch -qc fix/7-token
    # red then green, with a regression test file in the diff
    echo "def test_token(): assert True" > tests/test_token_regression.py
    git add tests/test_token_regression.py
    git -c core.hooksPath=/dev/null commit -qm "red: regression test for token"
    echo "def token(): return 1" > token.py
    git add token.py
    git -c core.hooksPath=/dev/null commit -qm "green: implement token"
    : > .harness/ac-posted; : > .harness/test-plan-posted
    echo "PASS looks good" > .harness/adversarial-verdict
    # A GENUINE test command: passes only WITH the impl, so §1 is green and §7c
    # goes red on revert. The gate now fails CLOSED without one (ALLOW_UNTESTED=1
    # is the opt-out), so every case below would otherwise be blocked at §1 —
    # i.e. "blocked" would stop proving anything about the rule under test.
    echo 'test -f token.py' > .harness/test-cmd
  )
  echo "$d"
}

PR_BODY_OK=$'## Acceptance Criteria\n- token works\n\nFixes #7'
PR_BODY_NO_AC=$'Fixes #7\nsome text'
PR_BODY_NO_LINK=$'## Acceptance Criteria\n- token works'

d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "compliant PR passes" || no "compliant PR passes" "rc=$rc out=${out:0:200}"
rm -rf "$d"

d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_NO_AC" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "missing AC blocked" || no "missing AC blocked" "rc=$rc"
rm -rf "$d"

d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_NO_LINK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "missing Fixes #n blocked" || no "missing Fixes #n blocked" "rc=$rc"
rm -rf "$d"

# Missing test-plan marker → blocked.
d=$(setup_pr_repo); rm -f "$d/.harness/test-plan-posted"
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "missing test-plan marker blocked" || no "missing test-plan marker blocked" "rc=$rc"
rm -rf "$d"

# Missing AC marker → blocked (AC-posted-before-code enforcement).
d=$(setup_pr_repo); rm -f "$d/.harness/ac-posted"
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "missing AC marker blocked" || no "missing AC marker blocked" "rc=$rc"
rm -rf "$d"

# Failing adversarial verdict → blocked.
d=$(setup_pr_repo); echo "FAIL nope" > "$d/.harness/adversarial-verdict"
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "FAIL verdict blocked" || no "FAIL verdict blocked" "rc=$rc"
rm -rf "$d"

# fix/ branch with NO test file in the diff → blocked.
d=$(fresh_repo)
( cd "$d" && mkdir -p .claude .harness && git switch -qc fix/9-notest \
    && echo x>x.py && git add x.py && git -c core.hooksPath=/dev/null commit -qm "red: placeholder" \
    && echo y>y.py && git add y.py && git -c core.hooksPath=/dev/null commit -qm "green: impl" \
    && : > .harness/ac-posted; : > .harness/test-plan-posted && echo PASS > .harness/adversarial-verdict )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'regression test'; } \
  && ok "fix/ without regression test blocked" || no "fix/ without regression test blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# evidence/ NON-IMAGE committed → blocked.
d=$(setup_pr_repo)
( cd "$d" && mkdir -p evidence && echo shot > evidence/x.txt && git add evidence/x.txt \
    && git -c core.hooksPath=/dev/null commit -qm "chore: oops evidence" )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "committed non-image evidence/ blocked" || no "committed non-image evidence/ blocked" "rc=$rc"
rm -rf "$d"

# evidence/ IMAGE committed → allowed (visual proof renders in the PR).
d=$(setup_pr_repo)
( cd "$d" && mkdir -p evidence && printf '\x89PNG' > evidence/shot.png && git add evidence/shot.png \
    && git -c core.hooksPath=/dev/null commit -qm "evidence: screenshot" )
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
# rc may be non-zero for OTHER reasons in this fixture; assert specifically that the
# evidence check did NOT fire (no "evidence/" complaint in the output).
printf '%s' "$out" | grep -qi 'evidence/' \
  && no "committed evidence image allowed" "evidence complaint: ${out:0:160}" \
  || ok "committed evidence image allowed"
rm -rf "$d"

# green-without-red detected at PR gate too (log re-check). Build with only green.
d=$(fresh_repo)
( cd "$d" && mkdir -p .claude .harness && git switch -qc fix/11-x \
    && echo "def test_x(): pass" > test_x_regression.py && git add test_x_regression.py \
    && echo z>z.py && git add z.py && git -c core.hooksPath=/dev/null commit -qm "green: impl no red" \
    && : > .harness/ac-posted; : > .harness/test-plan-posted && echo PASS > .harness/adversarial-verdict )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'TDD ordering'; } \
  && ok "PR gate catches green-without-red" || no "PR gate catches green-without-red" "rc=$rc out=${out:0:160}"
rm -rf "$d"

echo "== check-pr.sh §0b (PR base must equal BASE_BRANCH) =="
# No --base at all → blocked (gh would default to the repo's default branch).
d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "PR without --base blocked" || no "PR without --base blocked" "rc=$rc out=${out:0:160}"
printf '%s' "$out" | grep -q 'no --base' && ok "missing-base message is precise" || no "missing-base message is precise" "out=${out:0:160}"
rm -rf "$d"

# --base main when BASE_BRANCH=dev → blocked (the #49 bug).
d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base main --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "PR base=main blocked when BASE_BRANCH=dev" || no "PR base=main blocked when BASE_BRANCH=dev" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# --base dev with BASE_BRANCH=dev → allowed (proven by the compliant case above, re-assert via -B alias).
d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create -B dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "PR base via -B dev allowed" || no "PR base via -B dev allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

echo "== check-pr.sh (adversarial-review regressions) =="
# --web is rejected.
d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --web --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "--web blocked" || no "--web blocked" "rc=$rc"
rm -rf "$d"

# A commit with a bad prefix made via -F (file message) is caught by the PR-gate log re-check.
d=$(fresh_repo)
( cd "$d" && mkdir -p .claude .harness && git switch -qc fix/12-fmsg \
    && echo "def test_a(): pass" > test_a_regression.py && git add test_a_regression.py \
    && git -c core.hooksPath=/dev/null commit -qm "red: a" \
    && echo impl>impl.py && git add impl.py \
    && printf 'wip arbitrary message no prefix\n' > /tmp/cm.txt \
    && git -c core.hooksPath=/dev/null commit -qF /tmp/cm.txt \
    && : > .harness/ac-posted; : > .harness/test-plan-posted && echo PASS > .harness/adversarial-verdict )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'allowed prefix'; } \
  && ok "PR gate catches -F bad-prefix commit" || no "PR gate catches -F bad-prefix commit" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Modifying an EXISTING test file is blocked (only new test files allowed).
d=$(fresh_repo)
( cd "$d"
  # seed an existing test on dev
  mkdir -p tests && echo "def test_keep(): assert True" > tests/test_keep.py
  git add tests/test_keep.py && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  mkdir -p .claude .harness && git switch -qc fix/13-modtest
  echo "def test_new_regression(): assert True" > tests/test_new_regression.py
  git add tests/test_new_regression.py && git -c core.hooksPath=/dev/null commit -qm "red: new"
  # now MODIFY the pre-existing test
  echo "def test_keep(): assert False  # weakened" > tests/test_keep.py
  echo impl>impl.py; git add tests/test_keep.py impl.py
  git -c core.hooksPath=/dev/null commit -qm "green: impl + weaken existing test"
  : > .harness/ac-posted; : > .harness/test-plan-posted; echo PASS > .harness/adversarial-verdict )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'existing test file'; } \
  && ok "modifying existing test blocked" || no "modifying existing test blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# feat/ branch accepts a conventionally-named test (test_*.py), not just e2e/integration.
d=$(fresh_repo)
( cd "$d" && mkdir -p .claude .harness tests && git switch -qc feat/14-modulo \
    && echo "def test_modulo_integration(): assert True" > tests/test_modulo.py && git add tests/test_modulo.py \
    && git -c core.hooksPath=/dev/null commit -qm "red: modulo test" \
    && echo "def modulo(a,b): return a%b" > mod.py && git add mod.py \
    && git -c core.hooksPath=/dev/null commit -qm "green: modulo" \
    && : > .harness/ac-posted; : > .harness/test-plan-posted && echo PASS > .harness/adversarial-verdict )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f mod.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body '## Acceptance Criteria
- modulo

Fixes #14' 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "feat/ accepts conventional test name" || no "feat/ accepts conventional test name" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo "== check-pr.sh §7d (one screenshot per AC item) =="
# Fixture: a compliant fix/ PR that ALSO declares a visual acceptance criterion
# and commits <n> screenshots, all recorded as read by the agent.
setup_visual_repo() {  # setup_visual_repo <n-screenshots> [<n-read>]
  local d n read_n i; d=$(setup_pr_repo); n="$1"; read_n="${2:-$1}"
  ( cd "$d"
    mkdir -p evidence
    : > .harness/visual-ac
    : > .harness/screenshots-read
    i=1
    while [ "$i" -le "$n" ]; do
      printf '\x89PNG' > "evidence/ac-$i.png"
      [ "$i" -le "$read_n" ] && echo "ac-$i.png" >> .harness/screenshots-read
      i=$((i + 1))
    done
    if [ "$n" -gt 0 ]; then
      git add evidence
      git -c core.hooksPath=/dev/null commit -qm "evidence: screenshots"
    fi )
  echo "$d"
}
AC3_LINKED=$'## Acceptance Criteria\n- [ ] one\n  ![AC 1](evidence/ac-1.png)\n- [ ] two\n  ![AC 2](evidence/ac-2.png)\n- [ ] three\n  ![AC 3](evidence/ac-3.png)\n\nFixes #7'
AC3_ONE_LINK=$'## Acceptance Criteria\n- [ ] one\n- [ ] two\n- [ ] three\n\n![only one](evidence/ac-1.png)\n\nFixes #7'

# 3 AC items but only 2 screenshots → blocked, message names both counts.
d=$(setup_visual_repo 2)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q '3 AC items but only 2'; } \
  && ok "3 AC items with 2 screenshots blocked (counts named)" \
  || no "per-AC screenshot count enforced" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# 3 AC items, 3 read+committed screenshots, each referenced → allowed.
d=$(setup_visual_repo 3)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "one screenshot per AC item passes" || no "one screenshot per AC item passes" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Enough screenshots, but the body links only one → blocked (unlinked shots are
# invisible to the reviewer).
d=$(setup_visual_repo 3)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_ONE_LINK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'references only 1'; } \
  && ok "unreferenced screenshots blocked" || no "body must reference each screenshot" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Unparseable (prose) AC section → falls back to the ≥1-screenshot floor: one
# read+committed shot passes even though the body links nothing.
d=$(setup_visual_repo 1)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "prose AC section falls back to the 1-screenshot floor" || no "AC parse miss must not deny" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Floor still holds: visual-ac declared with no screenshot committed → blocked.
d=$(setup_visual_repo 0)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'no screenshot is committed'; } \
  && ok "visual-ac without any screenshot blocked" || no "visual-ac floor (>=1 shot)" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Floor still holds: a committed screenshot the agent never READ → blocked.
d=$(setup_visual_repo 3 2)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'never read/verified'; } \
  && ok "unread screenshot blocked" || no "visual-ac floor (all shots read)" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# No .harness/visual-ac → the whole rule is inert, even with a 3-item checklist.
d=$(setup_pr_repo)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "no visual-ac declared → no screenshot demanded" || no "non-visual PR unaffected" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo "== check-pr.sh §7c (red-must-actually-fail) =="
# Build a feat/ branch whose NEW test genuinely depends on the implementation:
# TEST_CMD passes iff the impl file exists. The gate reverts the impl and re-runs;
# a real test goes red (allowed), a tautological TEST_CMD stays green (blocked).
setup_redfail_repo() {  # setup_redfail_repo <branch>
  local d; d=$(fresh_repo)
  ( cd "$d"
    mkdir -p .claude .harness tests
    git switch -qc "$1"
    echo "def test_add(): assert True" > tests/test_add.py   # NEW test (kept at HEAD)
    git add tests/test_add.py
    git -c core.hooksPath=/dev/null commit -qm "red: add test"
    echo "def add(a,b): return a+b" > add.py                 # NEW impl (reverted to red)
    git add add.py
    git -c core.hooksPath=/dev/null commit -qm "green: add"
    : > .harness/ac-posted; : > .harness/test-plan-posted
    echo "PASS" > .harness/adversarial-verdict )
  echo "$d"
}
RF_BODY=$'## Acceptance Criteria\n- add works\n\nFixes #20'

# (f) genuine red: TEST_CMD passes only WITH the impl → reverting makes it fail → allow.
d=$(setup_redfail_repo feat/20-add)
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f add.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "genuine red test passes the gate" || no "genuine red test passes the gate" "rc=$rc out=${out:0:200}"
# and the worktree was restored (impl back at HEAD)
[ -f "$d/add.py" ] && ok "impl restored after red-check" || no "impl restored after red-check" "add.py missing"
rm -rf "$d"

# (g) tautology: TEST_CMD always passes (even with impl reverted) → block.
d=$(setup_redfail_repo feat/20-add)
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "tautological test is blocked" || no "tautological test is blocked" "rc=$rc out=${out:0:200}"
printf '%s' "$out" | grep -q 'do not exercise the change' && ok "block message names the cause" || no "block message names the cause" "out=${out:0:200}"
[ -f "$d/add.py" ] && ok "impl restored after a blocking red-check" || no "impl restored after a blocking red-check" "add.py missing"
rm -rf "$d"

# (h) RED_MUST_FAIL=0 disables the check → tautology allowed (knob honoured).
d=$(setup_redfail_repo feat/20-add)
out=$( cd "$d" && BASE_BRANCH=dev RED_MUST_FAIL=0 TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "RED_MUST_FAIL=0 disables the check" || no "RED_MUST_FAIL=0 disables the check" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (i) no test command at all → the gate must fail CLOSED. A warning was not
#     enough: with nothing to run, "untested" exited 0 exactly like "tests
#     passed", so §1 AND the anti-tautological §7c were silently disabled on any
#     target that never set TEST_CMD. The message must tell the agent how to fix it.
d=$(setup_redfail_repo feat/20-add)
out=$( cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "no test command resolved"; } \
  && ok "no test command → PR blocked (fail-closed)" \
  || no "no-test-command fails closed" "rc=$rc out=${out:0:200}"
printf '%s' "$out" | grep -q '.harness/test-cmd' \
  && ok "no-test-command message says how to fix it" \
  || no "no-test-command message actionable" "out=${out:0:200}"
rm -rf "$d"

# (j) ALLOW_UNTESTED=1 → the operator's explicit opt-out for a genuinely
#     test-less repo restores the old warn-and-pass behaviour (and still warns,
#     so the degradation is never silent).
d=$(setup_redfail_repo feat/20-add)
out=$( cd "$d" && BASE_BRANCH=dev ALLOW_UNTESTED=1 bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
{ [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "GATE WARNING: no test command"; } \
  && ok "ALLOW_UNTESTED=1 → allowed, but still warns" \
  || no "ALLOW_UNTESTED opt-out" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (j2) the opt-out covers only a MISSING command — a failing one still blocks.
d=$(setup_redfail_repo feat/20-add)
out=$( cd "$d" && BASE_BRANCH=dev ALLOW_UNTESTED=1 TEST_CMD='false' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "ALLOW_UNTESTED does not excuse failing tests" || no "ALLOW_UNTESTED vs failing tests" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (k) .harness/test-cmd is PREFERRED over $TEST_CMD. Agent declares a genuine
#     command (passes only with impl); a DIFFERENT, irrelevant $TEST_CMD='true' is
#     set. The gate must use the marker → revert makes it red → allow. Proves the
#     marker wins (with TEST_CMD='true' alone the tautology check would BLOCK).
d=$(setup_redfail_repo feat/20-add)
( cd "$d" && echo 'test -f add.py' > .harness/test-cmd )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok ".harness/test-cmd preferred over TEST_CMD (genuine red passes)" || no "test-cmd marker preferred" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (l) Agent cannot cheat the gate by declaring a weak command: a tautological
#     .harness/test-cmd ('true') stays green on revert → BLOCKED, even though the
#     repo-wide TEST_CMD is a genuine one. Teeth come from "must fail on revert",
#     regardless of who chose the command.
d=$(setup_redfail_repo feat/20-add)
( cd "$d" && echo 'true' > .harness/test-cmd )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f add.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "weak agent-declared test-cmd still blocked by red-fail (self-policing)" || no "weak test-cmd self-policed" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo "== check-push.sh (push to an open PR) =="
# A stub `gh` on PATH controls the "open PR for this branch?" lookup. GH_STUB_PR
# non-empty → `gh pr list … --jq …` prints that number (PR exists); empty → prints
# nothing (no PR). All other gh calls print nothing.
make_gh_stub() {  # make_gh_stub <dir> <pr-number-or-empty>
  local d="$1" n="$2"
  mkdir -p "$d/bin"
  cat > "$d/bin/gh" <<EOF
#!/usr/bin/env bash
# stub: only answers 'pr list … --head …'
case " \$* " in
  *" pr list "*) printf '%s' "$n" ;;
  *) : ;;
esac
EOF
  chmod +x "$d/bin/gh"
}

# (a) governed branch, NO open PR → allow (pre-PR push is fine).
d=$(setup_pr_repo); make_gh_stub "$d" ""
out=$( cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev bash "$CHECKS/check-push.sh" push origin fix/7-token 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push w/ no open PR allowed" || no "push w/ no open PR allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (b) open PR + failing TEST_CMD → blocked. Drop the fixture's .harness/test-cmd
#     first: the marker deliberately WINS over $TEST_CMD, so leaving it would make
#     this case assert the marker's command, not the failing one.
d=$(setup_pr_repo); make_gh_stub "$d" "7"; rm -f "$d/.harness/test-cmd"
out=$( cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "push w/ open PR + failing tests blocked" || no "push w/ open PR + failing tests blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (c) open PR + passing tests + clean commits → allow.
d=$(setup_pr_repo); make_gh_stub "$d" "7"
out=$( cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push w/ open PR + green allowed" || no "push w/ open PR + green allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (d) open PR + a pre-existing (base-branch) test file modified → blocked.
d=$(fresh_repo); make_gh_stub "$d" "7"
( cd "$d"
  mkdir -p tests && echo "def test_keep(): assert True" > tests/test_keep.py
  git add tests/test_keep.py && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  git switch -qc fix/13-modtest
  echo "def test_keep(): assert False  # weakened" > tests/test_keep.py
  echo impl>impl.py; git add tests/test_keep.py impl.py
  git -c core.hooksPath=/dev/null commit -qm "green: weaken existing test" )
out=$( cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "push modifying existing test blocked" || no "push modifying existing test blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (e) non-governed branch → allow regardless (and never even calls gh).
d=$(fresh_repo); ( cd "$d" && git switch -qc scratch-branch )
out=$( cd "$d" && BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push on non-governed branch allowed" || no "push on non-governed branch allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# A `gh` that FAILS the lookup (network/auth error), logging one line per call so
# the retry is observable. GH_CALL_LOG selects the log file.
make_gh_failing_stub() {  # make_gh_failing_stub <dir> [<attempt-that-succeeds> <pr-number>]
  local d="$1" ok_at="${2:-0}" n="${3:-}"
  mkdir -p "$d/bin"
  cat > "$d/bin/gh" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" pr list "*)
    echo call >> "\$GH_CALL_LOG"
    if [ "$ok_at" != 0 ] && [ "\$(wc -l < "\$GH_CALL_LOG")" -ge "$ok_at" ]; then
      printf '%s' "$n"; exit 0
    fi
    echo "error connecting to api.github.com" >&2; exit 1 ;;
  *) : ;;
esac
EOF
  chmod +x "$d/bin/gh"
}

# (f) the lookup fails on BOTH attempts → the push is BLOCKED (fail-closed).
#     Regression: this used to be `|| echo ""`, i.e. a gh error looked exactly
#     like "no open PR" and skipped the ENTIRE push gate — the gate's own
#     precondition was the bypass.
d=$(setup_pr_repo); make_gh_failing_stub "$d"
out=$( cd "$d" && PATH="$d/bin:$PATH" GH_CALL_LOG="$d/gh-calls" GATE_GH_RETRY_SLEEP=0 \
       BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'could not verify PR state'; } \
  && ok "push blocked when the PR-state lookup fails (fail-closed)" \
  || no "gh lookup failure fails closed" "rc=$rc out=${out:0:200}"
printf '%s' "$out" | grep -qi 'retry the push' \
  && ok "gh-failure message tells the agent to retry" || no "gh-failure message actionable" "out=${out:0:200}"
[ "$(wc -l < "$d/gh-calls")" -eq 2 ] \
  && ok "PR-state lookup is retried once before denying" \
  || no "lookup retried once" "calls=$(wc -l < "$d/gh-calls")"
rm -rf "$d"

# (g) a TRANSIENT failure (first call fails, retry succeeds) must NOT deny — the
#     retry exists so a network blip doesn't cost the agent a turn.
d=$(setup_pr_repo); make_gh_failing_stub "$d" 2 ""      # 2nd call answers "no PR"
out=$( cd "$d" && PATH="$d/bin:$PATH" GH_CALL_LOG="$d/gh-calls" GATE_GH_RETRY_SLEEP=0 \
       BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "transient lookup failure recovers on retry" || no "transient lookup recovers" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (h) retry succeeds and reports an OPEN PR → the shared checks still run
#     (recovering must not skip enforcement).
d=$(setup_pr_repo); make_gh_failing_stub "$d" 2 "7"; rm -f "$d/.harness/test-cmd"
out=$( cd "$d" && PATH="$d/bin:$PATH" GH_CALL_LOG="$d/gh-calls" GATE_GH_RETRY_SLEEP=0 \
       BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'test suite failed'; } \
  && ok "open PR found on retry still enforces the push gate" \
  || no "retry then enforce" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (i) no test command + open PR → the push gate fails CLOSED too (prc_check_tests
#     is shared with check-pr.sh), and ALLOW_UNTESTED=1 opts out.
d=$(setup_pr_repo); make_gh_stub "$d" "7"; rm -f "$d/.harness/test-cmd"
out=$( cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'no test command resolved'; } \
  && ok "push w/ open PR + no test command blocked" || no "push no-test-command fails closed" "rc=$rc out=${out:0:200}"
out=$( cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev ALLOW_UNTESTED=1 bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push w/ ALLOW_UNTESTED=1 allowed" || no "push ALLOW_UNTESTED opt-out" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo ""
printf 'hooks: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
