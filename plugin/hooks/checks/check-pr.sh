#!/usr/bin/env bash
# Governed: `gh pr create …`. The full pre-PR gate.
# Receives the whole `gh pr create` invocation tokens as argv.
#
# Re-derives truth from git + files rather than trusting commit prefixes, so a
# mislabeled commit cannot sneak through. Each check echoes a precise reason and
# exits 1 to block PR creation.
set -uo pipefail
fail() { echo "PR blocked: $1"; exit 1; }
# shellcheck source=_pr_common.sh
. "$(dirname "$0")/_pr_common.sh"

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
base="${BASE_BRANCH:-dev}"

# Resolve the diff range against the base (prefer origin/, fall back to local).
range=""
for ref in "origin/$base" "$base"; do
  if git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
    range="$ref...HEAD"; break
  fi
done
[ -n "$range" ] || fail "cannot resolve base branch '$base' to compute the diff"

# ── 0. Extract PR title/body/base from argv (supports common gh flag spellings) ─
#       and reject --web (browser PR creation evades the gate's body checks).
body=""
pr_base=""
for ((i = 1; i <= $#; i++)); do
  case "${!i}" in
    --web)             fail "browser PR creation (--web) bypasses the gate; use a non-interactive 'gh pr create'" ;;
    --body | -b)       j=$((i + 1)); body="${!j:-}" ;;
    --body=*)          body="${!i#--body=}" ;;
    --body-file | -F)  j=$((i + 1)); f="${!j:-/dev/null}"; body="$(cat "$f" 2>/dev/null || true)" ;;
    --body-file=*)     f="${!i#--body-file=}"; body="$(cat "$f" 2>/dev/null || true)" ;;
    --base | -B)       j=$((i + 1)); pr_base="${!j:-}" ;;
    --base=*)          pr_base="${!i#--base=}" ;;
  esac
done

# ── 0b. PR MUST target BASE_BRANCH. `gh pr create` with no --base defaults to the
#        repo's DEFAULT branch (often main), not BASE_BRANCH (dev) — which routes
#        the PR through the wrong branch's CI and breaks auto-merge. Require an
#        explicit --base equal to BASE_BRANCH so the agent can't open against the
#        default branch by omission. (Observed live: #49 opened against main.)
[ -n "$pr_base" ] \
  || fail "PR has no --base; it must explicitly target '$base' (e.g. 'gh pr create --base $base …'). Without --base, gh defaults to the repo's default branch."
[ "$pr_base" = "$base" ] \
  || fail "PR base is '$pr_base' but must be '$base' (BASE_BRANCH). Re-create with '--base $base'."

# ── 1. Tests pass once, locally, before the PR (shared with check-push.sh) ─────
prc_check_tests

# ── 2. PR body carries acceptance criteria ─────────────────────────────────────
printf '%s' "$body" | grep -qF '## Acceptance Criteria' \
  || fail "PR body is missing the '## Acceptance Criteria' section"

# ── 3. PR body links the issue with a closing keyword (Fixes/Closes/Resolves #N)
#       so issue_for_pr() / GitHub's closing graph can connect them. ────────────
printf '%s' "$body" | grep -qiE '(fix(es|ed)?|close[sd]?|resolve[sd]?) +#[0-9]+' \
  || fail "PR body must link the issue, e.g. 'Fixes #<issue>'"

# ── 4. Acceptance Criteria + Test Plan were posted on the issue BEFORE coding.
#       The agent writes these marker files immediately after each issue comment
#       (see AGENT.md). They make the "AC/Test-Plan first" invariant mechanical.
[ -f .harness/ac-posted ]        || fail "no Acceptance Criteria posted on the issue yet (.harness/ac-posted missing — post AC as an issue comment FIRST)"
[ -f .harness/test-plan-posted ] || fail "no Test Plan posted on the issue yet (.harness/test-plan-posted missing)"

# ── 5. Branch type ⇒ required new test in the diff (derive from git, not prefix) ─
changed=$(git diff --name-only "$range" 2>/dev/null || echo "")
case "$branch" in
  feat/*)
    # Prefer an explicit e2e/integration test, but accept any conventional test
    # file path (test_*.py, *_test.go, tests/…) so projects without "e2e" in the
    # filename are not falsely blocked.
    printf '%s\n' "$changed" | grep -qE '(e2e|integration).*\.(spec|test)\.|(^|/)test_.*\.py|.*_test\.(py|go|ts|js)|tests?/.*\.(py|spec|test)' \
      || fail "feat/ branch must add an e2e/integration (or conventional) test (none found in the diff)" ;;
  fix/*)
    printf '%s\n' "$changed" | grep -qE "$PR_TEST_FILE_RE" \
      || fail "fix/ branch must add a regression test (none found in the diff)" ;;
esac

# ── 6/7/7b. Commit hygiene + evidence + test-immutability (shared, state-derived) ─
prc_check_commit_log
prc_check_no_evidence "$changed"
prc_check_tests_immutable "$changed"

# ── 7c. The new tests must ACTUALLY fail without the implementation (real red).
#        Create-only: proves the tests exercise the change (anti-tautology). ─────
prc_check_red_fails "$changed"

# ── 8. Adversarial pre-PR verdict exists and passed ─────────────────────────────
verdict=$(cat .harness/adversarial-verdict 2>/dev/null || echo "")
printf '%s' "$verdict" | grep -q '^PASS' \
  || fail "adversarial pre-PR review did not return PASS (.harness/adversarial-verdict)"

# ── 9. ADR reference: if the diff touches a module an ADR governs, cite it ──────
if [ -d docs/adr ]; then
  for adr in docs/adr/[0-9]*.md; do
    [ -e "$adr" ] || continue
    governs=$(grep -i '^Governs:' "$adr" 2>/dev/null | sed 's/^[Gg]overns://') || true
    [ -n "$governs" ] || continue
    for path in $governs; do
      # -F: treat the governed path as a literal, not a regex (a '.' in a path
      # must not match arbitrary chars, and a lone '.' must not match everything).
      if printf '%s\n' "$changed" | grep -qF "$path"; then
        id=$(basename "$adr" | grep -oE '^[0-9]+')
        printf '%s' "$body" | grep -qiE "adr[ -]?$id|$(basename "$adr")" \
          || fail "diff touches $path governed by ADR $id — reference it in the PR body"
      fi
    done
  done
fi

exit 0
