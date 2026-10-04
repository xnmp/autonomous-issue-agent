#!/usr/bin/env bash
# Tests the check-*.sh hooks against real temporary git repos.
# Pure bash mini-framework (no bats dependency).
#
# Run: bash tests/test_hooks.sh
set -uo pipefail
# Git's repository, object, index, and config selectors can redirect fixture
# writes into the caller's checkout before a fixture has been established.
for git_knob in "${!GIT_@}"; do
  unset "$git_knob" || { printf 'hook fixture setup failed: cannot clear %s\n' "$git_knob" >&2; exit 2; }
done
export GIT_ALLOW_PROTOCOL=file
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

# Test fixtures choose their own gate controls. Never inherit a self-hosted
# deployment's controls, which can recursively run this suite through TEST_CMD.
for gate_knob in \
  TEST_CMD LINT_CMD TYPECHECK_CMD EDIT_CHECK_CMD ALLOW_UNTESTED RED_MUST_FAIL \
  VERIFY_RED_AT_COMMIT BASE_BRANCH GATE_TEST_TIMEOUT_S \
  GATE_TEST_FAILURE_EXIT_CODES GATE_GH_RETRY_SLEEP \
  GH_APP_ID GH_APP_KEY_FILE GH_APP_INSTALL_ID TARGET_DIR AGENT_ENV \
  HARNESS_SCREENSHOTS_READ ${HARNESS_DEPLOY_KNOBS:-} HARNESS_DEPLOY_KNOBS; do
  unset "$gate_knob"
done
# NOTE: HARNESS_GATE_DEPTH is deliberately not in the list — it is the #63
# re-entrancy counter; unsetting it inside a gate-launched run re-arms the bomb.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 2
CHECKS="$ROOT/plugin/hooks/checks"
PASS=0
FAIL=0

fixture_die() { printf 'hook fixture setup failed: %s\n' "$1" >&2; exit 2; }
FIXTURE_TMP_BASE="${TMPDIR:-/tmp}"
[ -d "$FIXTURE_TMP_BASE" ] || fixture_die "temp directory is unavailable: $FIXTURE_TMP_BASE"
FIXTURE_TMP_BASE="$(cd "$FIXTURE_TMP_BASE" && pwd -P)" || fixture_die "cannot enter temp directory"
case "$FIXTURE_TMP_BASE/" in "$ROOT/"*) fixture_die "temp directory is inside the source checkout" ;; esac
INVOKING_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || :)"
if [ -n "$INVOKING_ROOT" ]; then
  case "$FIXTURE_TMP_BASE/" in "$INVOKING_ROOT/"*) fixture_die "temp directory is inside the invoking checkout" ;; esac
fi

fixture_require() {
  local candidate="${1:-}" canonical
  case "$candidate" in "$FIXTURE_TMP_BASE"/hooks-fixture.*) ;; *) fixture_die "invalid fixture path: $candidate" ;; esac
  [ -d "$candidate" ] && [ ! -L "$candidate" ] || fixture_die "fixture directory is unavailable: $candidate"
  canonical="$(cd "$candidate" && pwd -P)" || fixture_die "cannot resolve fixture directory: $candidate"
  [ "$canonical" = "$candidate" ] || fixture_die "fixture directory changed: $candidate"
}

fixture_dir() {
  local candidate
  candidate="$(mktemp -d "$FIXTURE_TMP_BASE/hooks-fixture.XXXXXXXX")" || fixture_die "mktemp failed"
  fixture_require "$candidate"
  printf '%s\n' "$candidate"
}

fixture_cd() {
  fixture_require "$1"
  builtin cd -- "$1" || fixture_die "cannot enter fixture directory: $1"
}

GH_SHIM_ROOT="$(fixture_dir)" || fixture_die "gh isolation fixture allocation failed"
cat > "$GH_SHIM_ROOT/gh" <<'SH'
#!/usr/bin/env bash
printf 'unexpected gh call in hook fixture\n' >&2
exit 75
SH
[ "$?" -eq 0 ] || fixture_die "cannot write gh isolation shim"
chmod +x "$GH_SHIM_ROOT/gh" || fixture_die "cannot make gh isolation shim executable"
export PATH="$GH_SHIM_ROOT:$PATH"
trap 'rm -rf -- "$GH_SHIM_ROOT"' EXIT

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
  local d; d="$(fixture_dir)" || fixture_die "fresh fixture allocation failed"
  (
    fixture_cd "$d"
    git init -q -b dev &&
    git config user.email t@t && git config user.name t &&
    git config commit.gpgsign false &&
    echo seed > seed.txt &&
    git add seed.txt &&
    git -c core.hooksPath=/dev/null commit -qm "chore: seed"
  ) || fixture_die "fresh fixture git setup failed"
  printf '%s\n' "$d"
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
# Pin a failing fixture command: otherwise an outer `.harness/test-cmd` (for
# example self-hosting `make test`) is resolved by this red-phase hook test.
assert_exit 0 "accepts red:"     -- env TEST_CMD='false' bash "$CHECKS/check-commit.sh" commit -m "red: failing test"
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
# Keep this fixture self-contained even when its caller created .harness/test-cmd
# for a real target suite (self-hosting writes `make test` there).
out=$(TEST_CMD='false' bash "$CHECKS/check-commit.sh" commit -m "red: t" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "red: with failing tests allowed" || no "red: with failing tests allowed" "rc=$rc"
# green: is never subject to the red-check, even if tests pass.
out=$(TEST_CMD='true'  bash "$CHECKS/check-commit.sh" commit -m "green: x" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "green: not red-checked" || no "green: not red-checked" "rc=$rc"

echo "== gate recursion bound (#63) =="
# The exec wrapper refuses at depth ≥ 2 with rc 213 — the re-entrancy backstop
# behind the env scrub. Runs the wrapper directly so the refusal is observable.
out=$(HARNESS_GATE_DEPTH=2 bash -c ". '$CHECKS/_pr_common.sh'; prc_run_test_cmd true" 2>&1); rc=$?
[ "$rc" -eq 213 ] && case "$out" in *"GATE RECURSION"*) ok "depth 2 exec refused (rc 213)";; *) no "depth 2 exec refused (rc 213)" "out=$out";; esac || no "depth 2 exec refused (rc 213)" "rc=$rc"
# Depth increments through the wrapper: a child at ambient depth 0 sees 1.
# (env -u pins ambient depth so this also holds when THIS suite itself runs
# under a real gate at depth 1 — the self-hosted case.)
out=$(env -u HARNESS_GATE_DEPTH bash -c ". '$CHECKS/_pr_common.sh'; prc_run_test_cmd 'test \"\$HARNESS_GATE_DEPTH\" = 1'" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "depth stamps child env" || no "depth stamps child env" "rc=$rc out=$out"
# The dynamic manifest (HARNESS_DEPLOY_KNOBS, from compose-env) is scrubbed
# too: a knob named there never reaches the target command, without anyone
# editing the static lists.
out=$(HARNESS_DEPLOY_KNOBS='CUSTOM_KNOB' CUSTOM_KNOB='boom' bash -c ". '$CHECKS/_pr_common.sh'; prc_run_test_cmd 'test -z \"\${CUSTOM_KNOB:-}\" && test -z \"\${HARNESS_DEPLOY_KNOBS:-}\"'" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "manifest knobs scrubbed from child" || no "manifest knobs scrubbed from child" "rc=$rc out=$out"
# A refusal fails CLOSED at red-verify, with the real cause named — not as a
# phantom "suite passed/failed" verdict the agent would chase.
out=$(HARNESS_GATE_DEPTH=2 TEST_CMD='true' bash "$CHECKS/check-commit.sh" commit -m "red: t" 2>&1); rc=$?
[ "$rc" -eq 1 ] && case "$out" in *"GATE RECURSION"*) ok "red-verify refusal fails closed, names recursion";; *) no "red-verify refusal fails closed, names recursion" "out=$out";; esac || no "red-verify refusal fails closed, names recursion" "rc=$rc"
# End-to-end: a SELF-REFERENTIAL .harness/test-cmd (the marker every fixer
# writes; content re-invokes the red-verify hook) must terminate at the bound —
# this is the exact 2026-07-29 fork-bomb shape. 124 = timeout = still forking.
d=$(fixture_dir) || fixture_die "fixture allocation failed"
mkdir -p "$d/.harness" || fixture_die "cannot create marker directory"
printf 'bash "%s/check-commit.sh" commit -m "red: x"\n' "$CHECKS" > "$d/.harness/test-cmd" || fixture_die "cannot write marker"
( fixture_cd "$d" && env -u HARNESS_GATE_DEPTH timeout 60 bash "$CHECKS/check-commit.sh" commit -m "red: x" ) >/dev/null 2>&1; rc=$?
rm -rf "$d"
[ "$rc" -ne 124 ] && ok "self-referential marker terminates (rc=$rc)" || no "self-referential marker terminates" "timed out — recursion unbounded"
# opt-out disables it.
out=$(VERIFY_RED_AT_COMMIT=0 TEST_CMD='true' bash "$CHECKS/check-commit.sh" commit -m "red: t" 2>&1); rc=$?
[ "$rc" -eq 0 ] && ok "VERIFY_RED_AT_COMMIT=0 disables" || no "VERIFY_RED_AT_COMMIT=0 disables" "rc=$rc"

echo "== check-commit.sh (generated instruction guard) =="
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && printf '%s\n' '<!-- issue-agent generated instructions: start -->' \
    > AGENTS.override.md && git add AGENTS.override.md )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "chore: instructions" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "generated AGENTS.override.md blocked" \
  || no "generated AGENTS.override.md blocked" "rc=$rc out=${out:0:160}"
( fixture_cd "$d" && printf '%s\n' '# target-owned instructions' > AGENTS.override.md \
    && git add AGENTS.override.md )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "chore: instructions" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "target-owned AGENTS.override.md allowed" \
  || no "target-owned AGENTS.override.md allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

echo "== check-commit.sh (oversized-image gate, harness #13) =="
# Oversized image in evidence/ (unstaged — the compound add&&commit case) blocks.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && mkdir evidence && head -c 800000 /dev/urandom > evidence/big.png )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "evidence: shots" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "oversized evidence/ png blocked" || no "oversized evidence/ png blocked" "rc=$rc out=${out:0:160}"
# The block message must name the file and a remedy.
case "$out" in *big.png*ffmpeg*|*big.png*downscale*) ok "block message names file + remedy" ;; *) no "block message names file + remedy" "out=${out:0:200}" ;; esac
# Small image passes.
( fixture_cd "$d" && head -c 10000 /dev/urandom > evidence/small.png && rm evidence/big.png )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "evidence: shots" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "small evidence png passes" || no "small evidence png passes" "rc=$rc out=${out:0:160}"
# Oversized STAGED image outside evidence/ also blocks.
( fixture_cd "$d" && head -c 800000 /dev/urandom > shot.png && git add shot.png )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "evidence: shots" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "oversized staged png blocked" || no "oversized staged png blocked" "rc=$rc out=${out:0:160}"
# Oversized non-image files are NOT this gate's business.
( fixture_cd "$d" && git rm -q --cached shot.png && rm shot.png && head -c 800000 /dev/urandom > evidence/trace.txt )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "evidence: shots" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "oversized non-image ignored" || no "oversized non-image ignored" "rc=$rc out=${out:0:160}"
# EVIDENCE_MAX_IMAGE_KB=0 disables; a custom limit is honoured.
( fixture_cd "$d" && head -c 800000 /dev/urandom > evidence/big.png )
out=$( fixture_cd "$d" && EVIDENCE_MAX_IMAGE_KB=0 bash "$CHECKS/check-commit.sh" commit -m "evidence: shots" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "EVIDENCE_MAX_IMAGE_KB=0 disables" || no "EVIDENCE_MAX_IMAGE_KB=0 disables" "rc=$rc"
out=$( fixture_cd "$d" && EVIDENCE_MAX_IMAGE_KB=5 bash "$CHECKS/check-commit.sh" commit -m "evidence: shots" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "custom limit honoured" || no "custom limit honoured" "rc=$rc"
rm -rf "$d"
# An oversized evidence image INHERITED from HEAD (committed by an earlier PR,
# untouched here) is not this author's to re-capture and must NOT block
# (2026-08-01: keifu PR #154's merged screenshot blocked every later commit).
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && mkdir evidence && head -c 800000 /dev/urandom > evidence/inherited.png \
    && git add evidence/inherited.png \
    && EVIDENCE_MAX_IMAGE_KB=0 git commit -qm 'evidence: inherited' )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "fix: unrelated" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "inherited committed oversized png passes" \
  || no "inherited committed oversized png passes" "rc=$rc out=${out:0:160}"
# …but the same file MODIFIED in the worktree is this author's again.
( fixture_cd "$d" && head -c 900000 /dev/urandom > evidence/inherited.png )
out=$( fixture_cd "$d" && bash "$CHECKS/check-commit.sh" commit -m "evidence: retake" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "modified inherited oversized png blocked" \
  || no "modified inherited oversized png blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

echo "== check-commit-order.sh (red before green) =="
# Case A: green with NO prior red on the branch → blocked.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && git switch -qc fix/1-x && echo a>a && git add a && git -c core.hooksPath=/dev/null commit -qm "plan: types" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit -m "green: impl" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "green without prior red is blocked" || no "green without prior red is blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Case B: green WITH a prior red on the branch → allowed.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && git switch -qc fix/1-x \
    && echo a>a && git add a && git -c core.hooksPath=/dev/null commit -qm "red: failing test" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit -m "green: impl" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "green with prior red is allowed" || no "green with prior red is allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Case C: a red: commit itself is never ordering-constrained.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && git switch -qc fix/1-x )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit -m "red: first test" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "red: is unconstrained" || no "red: is unconstrained" "rc=$rc"
rm -rf "$d"

# Case D: a NON-green --amend (re-editing a tip) is allowed.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && git switch -qc fix/1-x )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit --amend -m "docs: tidy" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "non-green --amend allowed" || no "non-green --amend allowed" "rc=$rc"
rm -rf "$d"

# Case D2: --amend to a green: with NO prior red → still BLOCKED (closed bypass).
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && git switch -qc fix/1-x \
    && echo a>a && git add a && git -c core.hooksPath=/dev/null commit -qm "chore: benign" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-commit-order.sh" commit --amend -m "green: sneaky" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "amend→green without red is blocked" || no "amend→green without red is blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Case E: unresolvable base → fail-open (allow).
d=$(fixture_dir) || fixture_die "fixture allocation failed"
( fixture_cd "$d" && git init -q -b main && git config user.email t@t && git config user.name t ) || fixture_die "base fixture git setup failed"
out=$( fixture_cd "$d" && BASE_BRANCH=nonexistent bash "$CHECKS/check-commit-order.sh" commit -m "green: x" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "unresolvable base fails open" || no "unresolvable base fails open" "rc=$rc"
rm -rf "$d"

echo "== check-pr.sh (full gate) =="
# Build a compliant fix/ branch in a temp repo, then probe each gate.
setup_pr_repo() {
  local d; d=$(fresh_repo) || fixture_die "fresh_repo failed"
  (
    fixture_cd "$d"
    mkdir -p .claude .harness tests docs/adr
    git switch -qc fix/7-token
    # red then green, with a regression test file in the diff
    echo "def test_token(): assert True" > tests/test_token_regression.py
    git add tests/test_token_regression.py
    git -c core.hooksPath=/dev/null commit -qm "red: regression test for token"
    echo "def token(): return 1" > token.py
    git add token.py
    git -c core.hooksPath=/dev/null commit -qm "green: implement token"
    # A GENUINE test command: passes only WITH the impl, so §1 is green and §7c
    # goes red on revert. The gate now fails CLOSED without one (ALLOW_UNTESTED=1
    # is the opt-out), so every case below would otherwise be blocked at §1 —
    # i.e. "blocked" would stop proving anything about the rule under test.
    echo 'test -f token.py' > .harness/test-cmd
  ) || fixture_die "fixture setup failed"
  printf '%s\n' "$d"
}

PR_BODY_OK=$'## Acceptance Criteria\n- token works\n\nFixes #7'
PR_BODY_NO_AC=$'Fixes #7\nsome text'
PR_BODY_NO_LINK=$'## Acceptance Criteria\n- token works'

d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "compliant PR passes" || no "compliant PR passes" "rc=$rc out=${out:0:200}"
rm -rf "$d"

d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_NO_AC" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "missing AC blocked" || no "missing AC blocked" "rc=$rc"
rm -rf "$d"

d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_NO_LINK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "missing Fixes #n blocked" || no "missing Fixes #n blocked" "rc=$rc"
rm -rf "$d"

# fix/ branch with NO test file in the diff → blocked.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && mkdir -p .claude .harness && git switch -qc fix/9-notest \
    && echo x>x.py && git add x.py && git -c core.hooksPath=/dev/null commit -qm "red: placeholder" \
    && echo y>y.py && git add y.py && git -c core.hooksPath=/dev/null commit -qm "green: impl" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'regression test'; } \
  && ok "fix/ without regression test blocked" || no "fix/ without regression test blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# evidence/ NON-IMAGE committed → blocked.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
( fixture_cd "$d" && mkdir -p evidence && echo shot > evidence/x.txt && git add evidence/x.txt \
    && git -c core.hooksPath=/dev/null commit -qm "chore: oops evidence" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "committed non-image evidence/ blocked" || no "committed non-image evidence/ blocked" "rc=$rc"
rm -rf "$d"

# evidence/ IMAGE committed → allowed (visual proof renders in the PR).
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
( fixture_cd "$d" && mkdir -p evidence && printf '\x89PNG' > evidence/shot.png && git add evidence/shot.png \
    && git -c core.hooksPath=/dev/null commit -qm "evidence: screenshot" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "committed evidence image allowed" \
  || no "committed evidence image allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# green-without-red detected at PR gate too (log re-check). Build with only green.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && mkdir -p .claude .harness && git switch -qc fix/11-x \
    && echo "def test_x(): pass" > test_x_regression.py && git add test_x_regression.py \
    && echo z>z.py && git add z.py && git -c core.hooksPath=/dev/null commit -qm "green: impl no red" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'TDD ordering'; } \
  && ok "PR gate catches green-without-red" || no "PR gate catches green-without-red" "rc=$rc out=${out:0:160}"
rm -rf "$d"

echo "== published history is exempt from the prefix rule (keifu #182) =="
# GitHub's "Update branch" button writes an UNPREFIXED merge commit onto the PR
# branch. The publish broker then requires it as an ancestor, so a gate that
# denied its subject wedged the PR (keifu #182, 2026-08-03) with no move the
# agent could make. A subject already on origin/<branch> is published history
# outside the agent's unilateral control and is skipped — but ONLY there.
#
# Fixture: setup_pr_repo + a real bare `origin` both branches are pushed to, and
# a base that has since advanced (which is what makes an update-branch merge a
# merge at all). Echoes the worktree dir; origin lives at <dir>.origin.
setup_published_repo() {
  local d; d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
  git init -q --bare "$d.origin" || fixture_die "local origin setup failed"
  (
    fixture_cd "$d"
    git remote add origin "$d.origin"
    git push -q origin dev fix/7-token
    git switch -q dev
    echo changelog > CHANGELOG.md
    git add CHANGELOG.md
    git -c core.hooksPath=/dev/null commit -qm "chore: base moves on"
    git push -q origin dev
    git switch -q fix/7-token
  ) || fixture_die "fixture setup failed"
  printf '%s\n' "$d"
}

# (a) the merge commit is created on the REMOTE (a clone standing in for
#     GitHub's button) and fetched back → the PR gate passes.
d=$(setup_published_repo) || fixture_die "setup_published_repo failed"
git clone -q --branch fix/7-token "$d.origin" "$d.clone" || fixture_die "local clone setup failed"
( fixture_cd "$d.clone"
  git config user.email t@t; git config user.name t; git config commit.gpgsign false
  git -c core.hooksPath=/dev/null merge -q --no-ff -m "Merge branch 'dev' into fix/7-token" origin/dev
  git push -q origin fix/7-token )
( fixture_cd "$d"
  git fetch -q origin
  git merge -q --ff-only refs/remotes/origin/fix/7-token
  echo "def token2(): return 2" >> token.py
  git add token.py
  git -c core.hooksPath=/dev/null commit -qm "green: refine token" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "published update-branch merge is exempt" || no "published update-branch merge is exempt" "rc=$rc out=${out:0:200}"
rm -rf "$d" "$d.origin" "$d.clone"

# (b) twin: the SAME unprefixed subject, made locally and never pushed → denied.
#     Without this the exemption could be "skip every merge commit", which is
#     the rule the incident did NOT ask for.
d=$(setup_published_repo) || fixture_die "setup_published_repo failed"
( fixture_cd "$d"
  git -c core.hooksPath=/dev/null merge -q --no-ff -m "Merge branch 'dev' into fix/7-token" refs/remotes/origin/dev
  echo "def token2(): return 2" >> token.py
  git add token.py
  git -c core.hooksPath=/dev/null commit -qm "green: refine token" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'lacks an allowed prefix'; } \
  && ok "unpublished merge commit still blocked" || no "unpublished merge commit still blocked" "rc=$rc out=${out:0:200}"
rm -rf "$d" "$d.origin"

echo "== check-pr.sh §0b (PR base must equal BASE_BRANCH) =="
# No --base at all → blocked (gh would default to the repo's default branch).
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "PR without --base blocked" || no "PR without --base blocked" "rc=$rc out=${out:0:160}"
printf '%s' "$out" | grep -q 'no --base' && ok "missing-base message is precise" || no "missing-base message is precise" "out=${out:0:160}"
rm -rf "$d"

# --base main when BASE_BRANCH=dev → blocked (the #49 bug).
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base main --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "PR base=main blocked when BASE_BRANCH=dev" || no "PR base=main blocked when BASE_BRANCH=dev" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# --base dev with BASE_BRANCH=dev → allowed (proven by the compliant case above, re-assert via -B alias).
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create -B dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "PR base via -B dev allowed" || no "PR base via -B dev allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

echo "== check-pr.sh (adversarial-review regressions) =="
# --web is rejected.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --web --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "--web blocked" || no "--web blocked" "rc=$rc"
rm -rf "$d"

# A commit with a bad prefix made via -F (file message) is caught by the PR-gate log re-check.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && mkdir -p .claude .harness && git switch -qc fix/12-fmsg \
    && echo "def test_a(): pass" > test_a_regression.py && git add test_a_regression.py \
    && git -c core.hooksPath=/dev/null commit -qm "red: a" \
    && echo impl>impl.py && git add impl.py \
    && printf 'wip arbitrary message no prefix\n' > /tmp/cm.txt \
    && git -c core.hooksPath=/dev/null commit -qF /tmp/cm.txt )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'allowed prefix'; } \
  && ok "PR gate catches -F bad-prefix commit" || no "PR gate catches -F bad-prefix commit" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# Modifying an EXISTING test file is ADMITTED (ADR 0014 revised, 2026-08-03):
# the edit is an automatic exemption request the reviewer must grant before the
# PR can merge. Denying it here was the #126/#163 comply-how loop.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d"
  # seed an existing test on dev
  mkdir -p tests && echo "def test_keep(): assert True" > tests/test_keep.py
  git add tests/test_keep.py && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  mkdir -p .claude .harness && git switch -qc fix/13-modtest
  echo "def test_new_regression(): assert True" > tests/test_new_regression.py
  git add tests/test_new_regression.py && git -c core.hooksPath=/dev/null commit -qm "red: new"
  # now MODIFY the pre-existing test
  echo "def test_keep(): assert False  # weakened" > tests/test_keep.py
  echo impl>impl.py; git add tests/test_keep.py impl.py
  git -c core.hooksPath=/dev/null commit -qm "green: impl + weaken existing test" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f impl.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] \
  && ok "modifying existing test admitted" || no "modifying existing test admitted" "rc=$rc out=${out:0:300}"
rm -rf "$d"

echo "== check-pr.sh #124 (reviewer-exemption DECLARATIONS) =="
# The fixer requests an exemption with a `Gate-Exemption: <rule> <subject>`
# commit trailer. A DECLARATION authorizes nothing — it makes the violation
# explicit and visible in the PR so the reviewer can judge it; the merge side
# withholds auto-merge until REVIEW_BOT grants. Twin of domain/exemptions.py +
# domain/gate.py; tests/test_gate_parity.py runs the same shapes through both.

# Base carries tests/test_keep.py; the branch CORRECTS it (the "the existing
# test is itself the bug" case). $1 is appended verbatim to the green commit's
# message — the trailer under test. --cleanup=verbatim so a trailer's own
# leading/trailing whitespace reaches the parser instead of being stripped by
# git's default message cleanup.
setup_exempt_repo() {  # setup_exempt_repo "<trailer-lines>"
  local d; d=$(fresh_repo) || fixture_die "fresh_repo failed"
  (
    fixture_cd "$d"
    mkdir -p tests
    echo "def test_keep(): assert True" > tests/test_keep.py
    git add tests/test_keep.py
    git -c core.hooksPath=/dev/null commit -qm "test: baseline"
    mkdir -p .claude .harness
    git switch -qc fix/13-modtest
    echo "def test_new_regression(): assert True" > tests/test_new_regression.py
    git add tests/test_new_regression.py
    git -c core.hooksPath=/dev/null commit -qm "red: new"
    echo "def test_keep(): assert True  # corrected expectation" > tests/test_keep.py
    echo impl > impl.py
    git add tests/test_keep.py impl.py
    printf 'green: impl + correct the existing test\n\n%s\n' "$1" > "$d.msg"
    git -c core.hooksPath=/dev/null commit -q --cleanup=verbatim -F "$d.msg"
  ) || fixture_die "fixture setup failed"
  printf '%s\n' "$d"
}
# TEST_CMD passes only WITH impl.py, so §1 is green and §7c genuinely goes red —
# otherwise "blocked" here would stop proving anything about §7b.
EXEMPT_TEST_CMD='test -f impl.py'

# (a) a modified existing test file is ADMITTED — the diff itself is the
#     request, and the reviewer must grant it before the PR can merge.
d=$(setup_exempt_repo "") || fixture_die "setup_exempt_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "modified existing test passes the PR gate" \
  || no "modified existing test passes the PR gate" "rc=$rc out=${out:0:300}"
rm -rf "$d" "$d.msg"

# (b) the deny message is GONE for this shape: nothing to teach, nothing to
#     comply with. A gate that still named `Gate-Exemption: tests-immutable`
#     would send the fixer after syntax that no longer exists.
d=$(setup_exempt_repo "") || fixture_die "setup_exempt_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 )
printf '%s' "$out" | grep -qF 'Gate-Exemption: tests-immutable' \
  && no "no tests-immutable trailer is taught any more" "out=${out:0:300}" \
  || ok "no tests-immutable trailer is taught any more"
rm -rf "$d" "$d.msg"

# (c) a stale trailer neither helps nor harms: the rule reads the diff only.
d=$(setup_exempt_repo "Gate-Exemption: tests-immutable tests/test_other.py") || fixture_die "setup_exempt_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "a stale tests-immutable trailer changes nothing" \
  || no "a stale tests-immutable trailer changes nothing" "rc=$rc out=${out:0:300}"
rm -rf "$d" "$d.msg"

# (d) scope bound: MORE than 7 modified existing test files is a rewrite, not a
#     correction — denied, with no trailer syntax in the message. (Bound raised
#     from 3 after tauri #589: four mechanical spec-path updates are a
#     correction; per-file reviewer grants carry the authorization. 7, not 8:
#     the per-rule caps must sum below the total declaration cap of 8 or a
#     change at every bound truncates required-test out of the grant schema.)
setup_rewrite_repo() {  # modifies $1 existing test files on the branch
  local d n; d=$(fresh_repo) || fixture_die "fresh_repo failed"; n=$1
  (
    fixture_cd "$d"
    mkdir -p tests
    for i in $(seq 1 "$n"); do echo "def test_$i(): assert True" > "tests/test_$i.py"; done
    git add tests
    git -c core.hooksPath=/dev/null commit -qm "test: baseline"
    mkdir -p .claude .harness
    git switch -qc fix/13-modtest
    echo "def test_new_regression(): assert True" > tests/test_new_regression.py
    git add tests/test_new_regression.py
    git -c core.hooksPath=/dev/null commit -qm "red: new"
    for i in $(seq 1 "$n"); do echo "def test_$i(): assert True  # corrected" > "tests/test_$i.py"; done
    echo impl > impl.py
    git add tests impl.py
    git -c core.hooksPath=/dev/null commit -qm "green: impl + correct the existing tests"
  ) || fixture_die "fixture setup failed"
  printf '%s\n' "$d"
}

d=$(setup_rewrite_repo 8) || fixture_die "setup_rewrite_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'existing test files'; } \
  && ok "8 modified existing test files blocked (past the bound)" \
  || no "8 modified existing test files blocked (past the bound)" "rc=$rc out=${out:0:300}"
printf '%s' "$out" | grep -qF 'Gate-Exemption' \
  && no "the rewrite denial teaches no trailer" "out=${out:0:300}" \
  || ok "the rewrite denial teaches no trailer"
rm -rf "$d"

# (e) four modified existing tests — the tauri #589 shape (a shared interaction
#     change forcing mechanical path updates across specs) — is within scope.
d=$(setup_rewrite_repo 4) || fixture_die "setup_rewrite_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "4 modified existing test files allowed (the #589 shape)" \
  || no "4 modified existing test files allowed (the #589 shape)" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (e2) exactly 7 is still within scope (the bound is the boundary, not 'many
#      edits are suspicious').
d=$(setup_rewrite_repo 7) || fixture_die "setup_rewrite_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "7 modified existing test files allowed (at the bound)" \
  || no "7 modified existing test files allowed (at the bound)" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (f) a renamed-away existing test is COUNTED: moving a test out of the suite
#     removes it as surely as deleting it, and deriving from the NEW-side name
#     list alone let it escape entirely (round-4 adversarial review). Eight
#     renames plus one in-place edit is nine subjects → over the bound.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d"
  mkdir -p tests docs
  for i in 1 2 3 4 5 6 7 8 9; do echo "def test_$i(): assert True" > "tests/test_$i.py"; done
  git add tests && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  mkdir -p .claude .harness && git switch -qc fix/13-renames
  echo "def test_new_regression(): assert True" > tests/test_new_regression.py
  git add tests/test_new_regression.py
  git -c core.hooksPath=/dev/null commit -qm "red: new"
  for i in 1 2 3 4 5 6 7 8; do git mv "tests/test_$i.py" "docs/old_$i.txt"; done
  echo "def test_9(): assert True  # corrected" > tests/test_9.py
  echo impl > impl.py
  git add -A
  git -c core.hooksPath=/dev/null commit -qm "green: impl + move eight tests out" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'existing test files'; } \
  && ok "renamed-away tests count toward the scope bound" \
  || no "renamed-away tests count toward the scope bound" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (f2) anti-vacuity twin for (f): the same branch with only TWO renames plus
#      one edit is three subjects — within the bound, so it passes. Without
#      this, "renames are counted" could be satisfied by denying everything.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d"
  mkdir -p tests docs
  for i in 1 2 3 4; do echo "def test_$i(): assert True" > "tests/test_$i.py"; done
  git add tests && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  mkdir -p .claude .harness && git switch -qc fix/13-renames
  echo "def test_new_regression(): assert True" > tests/test_new_regression.py
  git add tests/test_new_regression.py
  git -c core.hooksPath=/dev/null commit -qm "red: new"
  for i in 1 2; do git mv "tests/test_$i.py" "docs/old_$i.txt"; done
  echo "def test_4(): assert True  # corrected" > tests/test_4.py
  echo impl > impl.py
  git add -A
  git -c core.hooksPath=/dev/null commit -qm "green: impl + move two tests out" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "two renames plus one edit stay within the bound" \
  || no "two renames plus one edit stay within the bound" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (f4) #178 — the bound counts the NET diff vs the base, at evaluation time.
#      Two independent ways a branch can look bigger than it is:
#        1. its own history transiently touched more test files and then
#           restored them (the fixer consolidating an approach);
#        2. the BASE advanced past the merge-base with other PRs that touched
#           other test files.
#      (2) blocked tauri PR #614 with a net diff of two modified test files:
#      the check ran `git diff <base>..HEAD` (two dots = the ENDPOINT diff), so
#      dev's own test edits counted, in reverse, as this branch's.
setup_transient_repo() {  # branch touches 5 tests then restores 3; base moves too
  local d; d=$(fresh_repo) || fixture_die "fresh_repo failed"
  (
    fixture_cd "$d"
    mkdir -p tests
    for i in 1 2 3 4 5; do echo "def test_$i(): assert True" > "tests/test_$i.py"; done
    git add tests && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
    mkdir -p .claude .harness && git switch -qc fix/13-transient
    echo "def test_new_regression(): assert True" > tests/test_new_regression.py
    git add tests/test_new_regression.py
    git -c core.hooksPath=/dev/null commit -qm "red: new"
    # first approach: rewrite five existing tests
    for i in 1 2 3 4 5; do echo "def test_$i(): assert True  # wip" > "tests/test_$i.py"; done
    echo impl > impl.py
    git add -A && git -c core.hooksPath=/dev/null commit -qm "green: first approach"
    # consolidate: restore all but two, so the NET diff is two modified tests
    for i in 3 4 5; do echo "def test_$i(): assert True" > "tests/test_$i.py"; done
    git add -A && git -c core.hooksPath=/dev/null commit -qm "refactor: consolidate, restore the rest"
    # meanwhile the base advances with unrelated PRs touching OTHER test files
    git switch -q dev
    for i in 3 4 5; do echo "def test_$i(): assert True  # another PR" > "tests/test_$i.py"; done
    git add -A && git -c core.hooksPath=/dev/null commit -qm "chore: unrelated work merged into dev"
    git switch -q fix/13-transient
  ) || fixture_die "fixture setup failed"
  printf '%s\n' "$d"
}
d=$(setup_transient_repo) || fixture_die "setup_transient_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] \
  && ok "restored tests and base-side test churn drop out of the scope bound" \
  || no "restored tests and base-side test churn drop out of the scope bound" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (f5) anti-vacuity twin for (f4): a base that advanced does NOT excuse a
#      branch that really does rewrite nine existing tests in its net diff.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d"
  mkdir -p tests
  for i in 1 2 3 4 5 6 7 8 9 10; do echo "def test_$i(): assert True" > "tests/test_$i.py"; done
  git add tests && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  mkdir -p .claude .harness && git switch -qc fix/13-transient
  echo "def test_new_regression(): assert True" > tests/test_new_regression.py
  git add tests/test_new_regression.py
  git -c core.hooksPath=/dev/null commit -qm "red: new"
  for i in 1 2 3 4 5 6 7 8 9; do echo "def test_$i(): assert True  # rewritten" > "tests/test_$i.py"; done
  echo impl > impl.py
  git add -A && git -c core.hooksPath=/dev/null commit -qm "green: rewrite nine tests"
  git switch -q dev
  echo "def test_10(): assert True  # another PR" > tests/test_10.py
  git add -A && git -c core.hooksPath=/dev/null commit -qm "chore: unrelated work merged into dev"
  git switch -q fix/13-transient )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'existing test files'; } \
  && ok "a genuine nine-test rewrite is still blocked when the base advanced" \
  || no "a genuine nine-test rewrite is still blocked when the base advanced" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (f3) a modified existing test whose path cannot be written as a grant subject
#      is DENIED here, where the message can name the file — admitting it would
#      guarantee a permanent merge withhold and a confusing escalation later.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d"
  mkdir -p tests && echo "def test_keep(): assert True" > "tests/my test.py"
  git add "tests/my test.py" && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  mkdir -p .claude .harness && git switch -qc fix/13-spacey
  echo "def test_new_regression(): assert True" > tests/test_new_regression.py
  git add tests/test_new_regression.py
  git -c core.hooksPath=/dev/null commit -qm "red: new"
  echo "def test_keep(): assert True  # corrected" > "tests/my test.py"
  echo impl > impl.py
  git add -A
  git -c core.hooksPath=/dev/null commit -qm "green: impl + correct the spacey test" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD="$EXEMPT_TEST_CMD" bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'cannot be carried in a reviewer grant'; } \
  && ok "an ungrantable test path denies with the charset message" \
  || no "an ungrantable test path denies with the charset message" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# ── §5 required-test: the whole-change subject `*` ────────────────────────────
# A fix/ branch with NO test file anywhere in the diff. $1 is appended verbatim
# to the green commit's message.
setup_notest_repo() {  # setup_notest_repo "<trailer-lines>"
  local d; d=$(fresh_repo) || fixture_die "fresh_repo failed"
  (
    fixture_cd "$d"
    mkdir -p .claude .harness
    git switch -qc fix/9-notest
    echo x > x.py; git add x.py
    git -c core.hooksPath=/dev/null commit -qm "red: placeholder"
    echo y > y.py; git add y.py
    printf 'green: impl\n\n%s\n' "$1" > "$d.msg"
    git -c core.hooksPath=/dev/null commit -q --cleanup=verbatim -F "$d.msg"
  ) || fixture_die "fixture setup failed"
  printf '%s\n' "$d"
}

# (g) declared whole-change exemption → §5 passes.
d=$(setup_notest_repo "Gate-Exemption: required-test *") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "declared required-test exemption passes §5" \
  || no "declared required-test exemption passes §5" "rc=$rc out=${out:0:200}"
rm -rf "$d" "$d.msg"

# (g2) trailer-order starvation parity: 8 filler tests-immutable trailers
#      (inert — no diff behind them) consume every slot of the total
#      declaration cap IN ORDER, so a required-test declared after them is
#      dropped by BOTH stacks — the shell §5 must deny exactly as the Python
#      parser starves it, or the merge side sees nothing to withhold on and
#      the ungranted exemption slips through (adversarial review, MEDIUM 2).
_filler=""
for i in 1 2 3 4 5 6 7 8; do
  _filler="${_filler}Gate-Exemption: tests-immutable tests/filler_$i.py
"
done
d=$(setup_notest_repo "${_filler}Gate-Exemption: required-test *") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'regression test'; } \
  && ok "required-test starved past the declaration cap is denied (parity)" \
  || no "required-test starved past the declaration cap is denied (parity)" "rc=$rc out=${out:0:300}"
rm -rf "$d" "$d.msg"

# (g3) anti-vacuity twin for (g2): 7 fillers + required-test = 8 total fits
#      the cap, so the declaration survives and §5 passes.
_filler=""
for i in 1 2 3 4 5 6 7; do
  _filler="${_filler}Gate-Exemption: tests-immutable tests/filler_$i.py
"
done
d=$(setup_notest_repo "${_filler}Gate-Exemption: required-test *") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] \
  && ok "required-test within the declaration cap still passes §5" \
  || no "required-test within the declaration cap still passes §5" "rc=$rc out=${out:0:300}"
rm -rf "$d" "$d.msg"

# (h) twin: no trailer → still denied, and the deny teaches the trailer.
d=$(setup_notest_repo "") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'regression test'; } \
  && ok "undeclared test-less fix/ still blocked" \
  || no "undeclared test-less fix/ still blocked" "rc=$rc out=${out:0:200}"
printf '%s' "$out" | grep -qF 'Gate-Exemption: required-test' \
  && ok "§5 deny names the exemption trailer" \
  || no "§5 deny names the exemption trailer" "out=${out:0:300}"
rm -rf "$d" "$d.msg"

# (i) an UNKNOWN rule is inert — the exemptable set is a closed domain
#     decision, so a fixer cannot invent a rule id and waive itself through.
d=$(setup_notest_repo "Gate-Exemption: made-up-rule *") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'regression test'; } \
  && ok "unknown-rule declaration is inert" \
  || no "unknown-rule declaration is inert" "rc=$rc out=${out:0:200}"
rm -rf "$d" "$d.msg"

# (j) a trailer is a LINE, not a substring: surrounding whitespace parses, so a
#     git client (or a fixer) that indents the trailer block still gets its
#     declaration read. Both regexes allow [[:blank:]] on either side.
d=$(setup_notest_repo "   Gate-Exemption: required-test *   ") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "trailer with surrounding whitespace still parses" \
  || no "trailer with surrounding whitespace still parses" "rc=$rc out=${out:0:200}"
rm -rf "$d" "$d.msg"

# (k) round-3 MAJOR 3: `*` PLUS another subject exceeds required-test's cap of
#     1, and an out-of-scope declaration is no exemption at all. Denying here
#     (rather than passing the gate and wedging at merge) is the twin of
#     domain/gate.py check_required_test's scope guard.
d=$(setup_notest_repo "Gate-Exemption: required-test *
Gate-Exemption: required-test src/x.py")
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'declared but VOID' \
    && printf '%s' "$out" | grep -q 'src/x.py'; } \
  && ok "over-scope required-test declaration denies with self-diagnosing VOID" \
  || no "over-scope required-test declaration denies with self-diagnosing VOID" "rc=$rc out=${out:0:300}"
rm -rf "$d" "$d.msg"

# (l) the malformed-trailer HINT must see a TAB-indented line too. `\s`/` ` in
#     the grep class silently missed tabs, so an agent whose editor indents
#     trailers with a tab got the bare denial and looped on "how do I comply"
#     (#126 class). [[:blank:]] is the fix; this pins it. The hint rides the
#     required-test denial — the only rule a trailer still governs.
d=$(setup_notest_repo "$(printf '\tGate-Exemption: required-test @scope/pkg')") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'did not parse'; } \
  && ok "tab-indented malformed trailer is counted in the hint" \
  || no "tab-indented malformed trailer is counted in the hint" "rc=$rc out=${out:0:400}"
rm -rf "$d" "$d.msg"

# (m) anti-vacuity twin for (l): a WELL-FORMED trailer earns no hint, so the
#     note means "your trailer is broken", not "trailers exist".
d=$(setup_notest_repo "Gate-Exemption: made-up-rule *") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 )
printf '%s' "$out" | grep -q 'did not parse' \
  && ok "an unknown rule id is counted as a malformed trailer" \
  || no "an unknown rule id is counted as a malformed trailer" "out=${out:0:400}"
d2=$(setup_notest_repo "") || fixture_die "setup_notest_repo failed"
out=$( fixture_cd "$d2" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 )
printf '%s' "$out" | grep -q 'did not parse' \
  && no "a branch with no trailers earns no malformed hint" "out=${out:0:400}" \
  || ok "a branch with no trailers earns no malformed hint"
rm -rf "$d" "$d.msg" "$d2" "$d2.msg"

# feat/ branch accepts a conventionally-named test (test_*.py), not just e2e/integration.
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && mkdir -p .claude .harness tests && git switch -qc feat/14-modulo \
    && echo "def test_modulo_integration(): assert True" > tests/test_modulo.py && git add tests/test_modulo.py \
    && git -c core.hooksPath=/dev/null commit -qm "red: modulo test" \
    && echo "def modulo(a,b): return a%b" > mod.py && git add mod.py \
    && git -c core.hooksPath=/dev/null commit -qm "green: modulo" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f mod.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body '## Acceptance Criteria
- modulo

Fixes #14' 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "feat/ accepts conventional test name" || no "feat/ accepts conventional test name" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo "== check-pr.sh §7d (one screenshot per AC item) =="
# Fixture: a compliant fix/ PR that ALSO declares a visual acceptance criterion
# and commits <n> screenshots, all recorded as read by the agent.
setup_visual_repo() {  # setup_visual_repo <n-screenshots> [<n-read>]
  local d n read_n i; d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; n="$1"; read_n="${2:-$1}"
  : > "$d.trusted-screenshots-read"
  ( fixture_cd "$d"
    mkdir -p evidence
    : > .harness/visual-ac
    i=1
    while [ "$i" -le "$n" ]; do
      # Distinct bytes per shot: the gate rejects byte-identical evidence.
      printf '\x89PNG shot %s' "$i" > "evidence/ac-$i.png"
      [ "$i" -le "$read_n" ] && echo "ac-$i.png" >> "$d.trusted-screenshots-read"
      i=$((i + 1))
    done
    if [ "$n" -gt 0 ]; then
      git add evidence
      git -c core.hooksPath=/dev/null commit -qm "evidence: screenshots"
    fi ) || fixture_die "visual fixture setup failed"
  printf '%s\n' "$d"
}
AC3_LINKED=$'## Acceptance Criteria\n- [ ] one\n  ![the one state rendered](evidence/ac-1.png)\n- [ ] two\n  ![the two state rendered](evidence/ac-2.png)\n- [ ] three\n  ![the three state rendered](evidence/ac-3.png)\n\nFixes #7'
AC3_ONE_LINK=$'## Acceptance Criteria\n- [ ] one\n- [ ] two\n- [ ] three\n\n![only one](evidence/ac-1.png)\n\nFixes #7'

# 3 AC items but only 2 screenshots → blocked, message names both counts.
d=$(setup_visual_repo 2) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q '3 AC items but only 2'; } \
  && ok "3 AC items with 2 screenshots blocked (counts named)" \
  || no "per-AC screenshot count enforced" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# 3 AC items, 3 read+committed screenshots, each referenced → allowed.
d=$(setup_visual_repo 3) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "one screenshot per AC item passes" || no "one screenshot per AC item passes" "rc=$rc out=${out:0:200}"

# 3 AC items, images referenced but item 1's embed carries no claim (alt text
# under 12 chars) → blocked, message names the item and the remedy.
AC3_UNCLAIMED=$'## Acceptance Criteria\n- [ ] one\n  ![AC 1](evidence/ac-1.png)\n- [ ] two\n  ![the two state rendered](evidence/ac-2.png)\n- [ ] three\n  ![the three state rendered](evidence/ac-3.png)\n\nFixes #7'
d=$(setup_visual_repo 3) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_UNCLAIMED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'AC item 1 carries no evidence claim'; } \
  && ok "AC item without an evidence claim blocked (item named)" \
  || no "AC item without an evidence claim blocked" "rc=$rc out=${out:0:200}"
rm -rf "$d"
rm -rf "$d"

# Enough screenshots, but the body links only one → blocked (unlinked shots are
# invisible to the reviewer).
d=$(setup_visual_repo 3) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_ONE_LINK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'references only 1'; } \
  && ok "unreferenced screenshots blocked" || no "body must reference each screenshot" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Unparseable (prose) AC section → falls back to the ≥1-screenshot floor: one
# read+committed shot passes even though the body links nothing.
d=$(setup_visual_repo 1) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "prose AC section falls back to the 1-screenshot floor" || no "AC parse miss must not deny" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Floor still holds: visual-ac declared with no screenshot committed → blocked.
d=$(setup_visual_repo 0) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'no screenshot is committed'; } \
  && ok "visual-ac without any screenshot blocked" || no "visual-ac floor (>=1 shot)" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Floor still holds: a committed screenshot the agent never READ → blocked.
d=$(setup_visual_repo 3 2) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'never read/verified'; } \
  && ok "unread screenshot blocked" || no "visual-ac floor (all shots read)" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Receipt-less runtime (HARNESS_SCREENSHOTS_READ unset — codex, #153): the
# read check is skipped and committed evidence passes the fixer gate; the
# independent reviewer is the sole content verifier (keifu #142/#156). An
# agent-authored worktree marker is simply irrelevant, not a bypass vector.
d=$(setup_visual_repo 1) || fixture_die "setup_visual_repo failed"
rm -f "$d.trusted-screenshots-read"
printf '%s\n' "ac-1.png" > "$d/.harness/screenshots-read"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "receipt-less runtime passes evidence through to review" \
  || no "receipt-less visual path must not dead-end" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Receipt-less runtime keeps the committed floor: no screenshot → still blocked.
d=$(setup_visual_repo 0) || fixture_die "setup_visual_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'no screenshot is committed'; } \
  && ok "receipt-less runtime still requires a committed screenshot" \
  || no "receipt-less floor (>=1 shot)" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Runtime that DOES mint receipts, but this read failed (var set, file gone) →
# recoverable deny, never the capability dead-end.
d=$(setup_visual_repo 1) || fixture_die "setup_visual_repo failed"
rm -f "$d.trusted-screenshots-read"
out=$( fixture_cd "$d" && HARNESS_SCREENSHOTS_READ="$d.trusted-screenshots-read" BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'receipt is unavailable'; } \
  && ok "minted-but-unreadable receipt blocked as recoverable" \
  || no "unreadable receipt deny" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# Byte-identical evidence images → blocked on EVERY runtime (#153 reuse gaming).
d=$(setup_visual_repo 2) || fixture_die "setup_visual_repo failed"
cp "$d/evidence/ac-1.png" "$d/evidence/ac-2.png"
( fixture_cd "$d" && git add evidence && git -c core.hooksPath=/dev/null commit -qm "evidence: dupe" )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$PR_BODY_OK" 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'byte-identical'; } \
  && ok "byte-identical evidence images blocked" \
  || no "byte-identical evidence deny" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# No .harness/visual-ac → the whole rule is inert, even with a 3-item checklist.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$AC3_LINKED" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "no visual-ac declared → no screenshot demanded" || no "non-visual PR unaffected" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo "== check-pr.sh §7c (red-must-actually-fail) =="
# Build a feat/ branch whose NEW test genuinely depends on the implementation:
# TEST_CMD passes iff the impl file exists. The gate reverts the impl and re-runs;
# a real test goes red (allowed), a tautological TEST_CMD stays green (blocked).
setup_redfail_repo() {  # setup_redfail_repo <branch>
  local d; d=$(fresh_repo) || fixture_die "fresh_repo failed"
  ( fixture_cd "$d"
    mkdir -p .claude .harness tests
    git switch -qc "$1"
    echo "def test_add(): assert True" > tests/test_add.py   # NEW test (kept at HEAD)
    git add tests/test_add.py
    git -c core.hooksPath=/dev/null commit -qm "red: add test"
    echo "def add(a,b): return a+b" > add.py                 # NEW impl (reverted to red)
    git add add.py
    git -c core.hooksPath=/dev/null commit -qm "green: add" ) || fixture_die "redfail fixture setup failed"
  printf '%s\n' "$d"
}
RF_BODY=$'## Acceptance Criteria\n- add works\n\nFixes #20'

# (f) genuine red: TEST_CMD passes only WITH the impl → reverting makes it fail → allow.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f add.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "genuine red test passes the gate" || no "genuine red test passes the gate" "rc=$rc out=${out:0:200}"
# and the worktree was restored (impl back at HEAD)
[ -f "$d/add.py" ] && ok "impl restored after red-check" || no "impl restored after red-check" "add.py missing"
rm -rf "$d"

# Non-assertion exits must not become trusted red evidence. The first test run
# sees add.py and passes; only the revert simulation emits the selected
# infrastructure/cancellation code.
for spec in "2:runner failure" "127:command not found" "137:killed or OOM"; do
  bad_rc="${spec%%:*}"; label="${spec#*:}"
  d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
  out=$( fixture_cd "$d" && BASE_BRANCH=dev \
    TEST_CMD="if [ -f add.py ]; then exit 0; else exit $bad_rc; fi" \
    bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" \
    2>&1 ); rc=$?
  expected="infrastructure error"
  [ "$bad_rc" != 137 ] || expected="was cancelled"
  { [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "$expected"; } \
    && ok "$label is not accepted as red-test evidence" \
    || no "$label red-test classification" "rc=$rc out=${out:0:200}"
  rm -rf "$d"
done

# A nonstandard test runner may declare its documented assertion-failure code,
# but only through the trusted deployment environment.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev GATE_TEST_FAILURE_EXIT_CODES='1 2' \
  TEST_CMD='if [ -f add.py ]; then exit 0; else exit 2; fi' \
  bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" \
  2>&1 ); rc=$?
[ "$rc" -eq 0 ] \
  && ok "trusted runner failure-code contract is honoured" \
  || no "trusted runner failure-code contract" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (g) tautology: TEST_CMD always passes (even with impl reverted) → block.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "tautological test is blocked" || no "tautological test is blocked" "rc=$rc out=${out:0:200}"
printf '%s' "$out" | grep -q 'do not exercise the change' && ok "block message names the cause" || no "block message names the cause" "out=${out:0:200}"
[ -f "$d/add.py" ] && ok "impl restored after a blocking red-check" || no "impl restored after a blocking red-check" "add.py missing"
rm -rf "$d"

# (h) RED_MUST_FAIL=0 disables the check → tautology allowed (knob honoured).
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev RED_MUST_FAIL=0 TEST_CMD='true' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "RED_MUST_FAIL=0 disables the check" || no "RED_MUST_FAIL=0 disables the check" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (i) no test command at all → the gate must fail CLOSED. A warning was not
#     enough: with nothing to run, "untested" exited 0 exactly like "tests
#     passed", so §1 AND the anti-tautological §7c were silently disabled on any
#     target that never set TEST_CMD. The message must tell the agent how to fix it.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
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
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev ALLOW_UNTESTED=1 bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
{ [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "GATE WARNING: no test command"; } \
  && ok "ALLOW_UNTESTED=1 → allowed, but still warns" \
  || no "ALLOW_UNTESTED opt-out" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (j2) the opt-out covers only a MISSING command — a failing one still blocks.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev ALLOW_UNTESTED=1 TEST_CMD='false' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "ALLOW_UNTESTED does not excuse failing tests" || no "ALLOW_UNTESTED vs failing tests" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (k) The OPERATOR's $TEST_CMD WINS over the agent-written .harness/test-cmd
#     (review P1R-18 — precedence flipped; these two cases asserted the old
#     order). The agent declares a tautological 'true'; the operator's TEST_CMD
#     is genuine (passes only with the impl). The gate must run the OPERATOR's →
#     revert makes it red → allow. With the marker winning this would BLOCK, so
#     the case still distinguishes the two orders, just the other way round.
#     Why the flip: prc_check_tests is the push gate's only real teeth, and the
#     gated agent writes that marker under bypassPermissions — `echo true >
#     .harness/test-cmd` disarmed §1 AND §7c in one line.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
( fixture_cd "$d" && echo 'true' > .harness/test-cmd )
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='test -f add.py' bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "operator TEST_CMD preferred over .harness/test-cmd (P1R-18)" || no "operator TEST_CMD preferred" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (l) A weak agent-declared command can no longer disarm the gate: the marker is
#     'true' (always green on revert ⇒ would be read as tautological) but the
#     operator set none, so the marker IS the resolved command and red-fail
#     blocks it. The marker still governs where the operator has no TEST_CMD —
#     that fallback is the whole reason the marker exists — and it is still
#     self-policing there, because §7c requires whatever command runs to go red.
d=$(setup_redfail_repo feat/20-add) || fixture_die "setup_redfail_repo failed"
( fixture_cd "$d" && echo 'true' > .harness/test-cmd )
out=$( fixture_cd "$d" && BASE_BRANCH=dev bash "$CHECKS/check-pr.sh" pr create --base dev --title t --body "$RF_BODY" 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "weak agent-declared test-cmd still blocked by red-fail (self-policing)" || no "weak test-cmd self-policed" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo "== check-push.sh (push to an open PR) =="
# A stub `gh` on PATH controls the "open PR for this branch?" lookup. GH_STUB_PR
# non-empty → `gh pr list … --jq …` prints that number (PR exists); empty → prints
# nothing (no PR). All other gh calls print nothing.
make_gh_stub() {  # make_gh_stub <dir> <pr-number-or-empty>
  local d="$1" n="$2" payload="[]"
  [ -z "$n" ] || payload="[{\"number\":$n}]"
  fixture_require "$d"
  mkdir -p "$d/bin" || fixture_die "cannot create gh stub directory"
  cat > "$d/bin/gh" <<EOF
#!/usr/bin/env bash
# stub: only answers 'pr list … --head …'
case " \$* " in
  *" pr list "*) printf '%s' '$payload' ;;
  *) : ;;
esac
EOF
  [ "$?" -eq 0 ] || fixture_die "cannot write gh stub"
  chmod +x "$d/bin/gh" || fixture_die "cannot make gh stub executable"
}

# (a) governed branch, NO open PR → allow (pre-PR push is fine).
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_stub "$d" ""
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev bash "$CHECKS/check-push.sh" push origin fix/7-token 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push w/ no open PR allowed" || no "push w/ no open PR allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (b) open PR + failing TEST_CMD → blocked. Drop the fixture's .harness/test-cmd
#     first: the marker deliberately WINS over $TEST_CMD, so leaving it would make
#     this case assert the marker's command, not the failing one.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_stub "$d" "7"; rm -f "$d/.harness/test-cmd"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 1 ] && ok "push w/ open PR + failing tests blocked" || no "push w/ open PR + failing tests blocked" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (c) open PR + passing tests + clean commits → allow.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_stub "$d" "7"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push w/ open PR + green allowed" || no "push w/ open PR + green allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# (d) open PR + a pre-existing (base-branch) test file modified → ADMITTED.
#     The push gate shares §7b with check-pr.sh; a rule the PR gate honours and
#     the push gate denies would wedge the fixer between two gates with no
#     legal move.
d=$(fresh_repo) || fixture_die "fresh_repo failed"; make_gh_stub "$d" "7"
( fixture_cd "$d"
  mkdir -p tests && echo "def test_keep(): assert True" > tests/test_keep.py
  git add tests/test_keep.py && git -c core.hooksPath=/dev/null commit -qm "test: baseline"
  git switch -qc fix/13-modtest
  echo "def test_new(): assert True" > tests/test_new.py
  git add tests/test_new.py && git -c core.hooksPath=/dev/null commit -qm "red: new"
  echo "def test_keep(): assert False  # weakened" > tests/test_keep.py
  echo impl>impl.py; git add tests/test_keep.py impl.py
  git -c core.hooksPath=/dev/null commit -qm "green: weaken existing test" )
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push modifying existing test admitted" || no "push modifying existing test admitted" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (d2) …and the scope bound still bites on the push gate, same as on the PR one.
d=$(setup_rewrite_repo 9) || fixture_die "setup_rewrite_repo failed"; make_gh_stub "$d" "7"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'existing test files'; } \
  && ok "push rewriting 9 existing tests blocked" \
  || no "push rewriting 9 existing tests blocked" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (d3) #178 on the push gate: the address-review push is exactly when the base
#      HAS advanced and when the branch has consolidated an earlier approach,
#      so this is where the endpoint-diff bug fired (tauri PR #614). Shares
#      setup_transient_repo with the PR-gate case (f4) above.
d=$(setup_transient_repo) || fixture_die "setup_transient_repo failed"; make_gh_stub "$d" "7"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev TEST_CMD='true' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] \
  && ok "push with restored tests and base-side churn stays within the bound" \
  || no "push with restored tests and base-side churn stays within the bound" "rc=$rc out=${out:0:300}"
rm -rf "$d"

# (e) non-governed branch → allow regardless (and never even calls gh).
d=$(fresh_repo) || fixture_die "fresh_repo failed"
( fixture_cd "$d" && git switch -qc scratch-branch ) || fixture_die "scratch fixture setup failed"
out=$( fixture_cd "$d" && BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push on non-governed branch allowed" || no "push on non-governed branch allowed" "rc=$rc out=${out:0:160}"
rm -rf "$d"

# A `gh` that FAILS the lookup (network/auth error), logging one line per call so
# the retry is observable. GH_CALL_LOG selects the log file.
make_gh_failing_stub() {  # make_gh_failing_stub <dir> [<attempt-that-succeeds> <pr-number>]
  local d="$1" ok_at="${2:-0}" n="${3:-}" payload="[]"
  [ -z "$n" ] || payload="[{\"number\":$n}]"
  fixture_require "$d"
  mkdir -p "$d/bin" || fixture_die "cannot create gh stub directory"
  cat > "$d/bin/gh" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" pr list "*)
    echo call >> "\$GH_CALL_LOG"
    if [ "$ok_at" != 0 ] && [ "\$(wc -l < "\$GH_CALL_LOG")" -ge "$ok_at" ]; then
      printf '%s' '$payload'; exit 0
    fi
    echo "error connecting to api.github.com" >&2; exit 1 ;;
  *) : ;;
esac
EOF
  [ "$?" -eq 0 ] || fixture_die "cannot write gh stub"
  chmod +x "$d/bin/gh" || fixture_die "cannot make gh stub executable"
}

# (f) the lookup fails on BOTH attempts → the push is BLOCKED (fail-closed).
#     Regression: this used to be `|| echo ""`, i.e. a gh error looked exactly
#     like "no open PR" and skipped the ENTIRE push gate — the gate's own
#     precondition was the bypass.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_failing_stub "$d"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" GH_CALL_LOG="$d/gh-calls" GATE_GH_RETRY_SLEEP=0 \
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
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_failing_stub "$d" 2 ""      # 2nd call answers "no PR"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" GH_CALL_LOG="$d/gh-calls" GATE_GH_RETRY_SLEEP=0 \
       BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "transient lookup failure recovers on retry" || no "transient lookup recovers" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (h) retry succeeds and reports an OPEN PR → the shared checks still run
#     (recovering must not skip enforcement).
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_failing_stub "$d" 2 "7"; rm -f "$d/.harness/test-cmd"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" GH_CALL_LOG="$d/gh-calls" GATE_GH_RETRY_SLEEP=0 \
       BASE_BRANCH=dev TEST_CMD='false' bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'test suite failed'; } \
  && ok "open PR found on retry still enforces the push gate" \
  || no "retry then enforce" "rc=$rc out=${out:0:200}"
rm -rf "$d"

# (i) no test command + open PR → the push gate fails CLOSED too (prc_check_tests
#     is shared with check-pr.sh), and ALLOW_UNTESTED=1 opts out.
d=$(setup_pr_repo) || fixture_die "setup_pr_repo failed"; make_gh_stub "$d" "7"; rm -f "$d/.harness/test-cmd"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'no test command resolved'; } \
  && ok "push w/ open PR + no test command blocked" || no "push no-test-command fails closed" "rc=$rc out=${out:0:200}"
out=$( fixture_cd "$d" && PATH="$d/bin:$PATH" BASE_BRANCH=dev ALLOW_UNTESTED=1 bash "$CHECKS/check-push.sh" push 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "push w/ ALLOW_UNTESTED=1 allowed" || no "push ALLOW_UNTESTED opt-out" "rc=$rc out=${out:0:200}"
rm -rf "$d"

echo ""
printf 'hooks: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
