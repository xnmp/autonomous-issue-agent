#!/usr/bin/env bash
# Fix failing CI on an open PR. Uses the REAL head branch for log lookup,
# guards `gh pr checks` non-zero exit, re-enters the worktree, caps retries.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
PR_NUMBER="${1:?usage: fix-ci.sh <pr-number>}"

branch=$(branch_for_pr "$PR_NUMBER")
[ -n "$branch" ] || { log "cannot resolve head branch for PR #$PR_NUMBER"; exit 1; }

# Resolve the issue. issue_for_pr uses GitHub's closing-reference graph, which is
# only populated for PRs against the repo's DEFAULT branch — a PR targeting dev
# (as ours do) has no closing ref, so fall back to the head-branch number (same
# pattern as cleanup.sh). Without this, fix-ci bailed "no linked issue" on every
# dev-targeted PR and never repaired CI.
ISSUE_NUMBER=$(issue_for_pr "$PR_NUMBER")
[ -n "$ISSUE_NUMBER" ] || ISSUE_NUMBER=$(issue_from_branch "$branch")
[ -n "$ISSUE_NUMBER" ] || { log "no linked issue for PR #$PR_NUMBER (no closing ref, unparseable branch '$branch')"; exit 1; }
SESSION_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.session"
[ -s "$SESSION_FILE" ] || { log "no session for PR #$PR_NUMBER"; exit 1; }
session_id=$(cat "$SESSION_FILE")

RETRY_FILE="$STATE_DIR/issue-$ISSUE_NUMBER.ci-retries"
retries=$(cat "$RETRY_FILE" 2>/dev/null || echo 0)
if [ "$retries" -ge "$MAX_CI_RETRIES" ]; then
  gh_fix pr comment "$PR_NUMBER" --repo "$REPO" --body \
"**Agent exhausted CI fixes after $MAX_CI_RETRIES attempts.** Manual intervention needed.

Failing checks:
$(gh_fix pr checks "$PR_NUMBER" --repo "$REPO" 2>/dev/null | grep -i fail || true)" 2>/dev/null || true
  relabel pr "$PR_NUMBER" "ai-working" "ai-blocked"
  exit 1
fi

# gh pr checks exits non-zero when checks fail — capture, don't die.
failed_checks=$(gh_fix pr checks "$PR_NUMBER" --repo "$REPO" 2>/dev/null | grep -iE 'fail|error' || true)

# Logs keyed on the REAL branch.
failed_run_id=$(gh_fix run list --repo "$REPO" --branch "$branch" --status failure \
  --json databaseId --jq '.[0].databaseId' 2>/dev/null || echo "")
failed_log=""
[ -n "$failed_run_id" ] && failed_log=$(gh_fix run view "$failed_run_id" --repo "$REPO" --log-failed 2>/dev/null | tail -150 || true)

ensure_worktree "$ISSUE_NUMBER" "$branch" >/dev/null
relabel pr "$PR_NUMBER" "" "ai-working"
# Pass the PR so common_claude_args can attach conditional CI-fix MCP servers
# (e.g. the SonarQube MCP) — ONLY when this PR's CI actually has a matching check.
common_claude_args "$PR_NUMBER"

# shellcheck disable=SC2046
run_claude RESULT "${COMMON_ARGS[@]}" \
  --worktree "issue-$ISSUE_NUMBER" \
  $(resume_args "$ISSUE_NUMBER" "$session_id") \
  "CI is failing on PR #$PR_NUMBER. Diagnose the failure, fix the CODE (not the
tests unless a test is genuinely wrong), verify locally, and push. Return
status=pushed when pushed, or status=blocked with 'needs' if it is a flaky/infra
failure you cannot fix.

Diagnose by failure class:
- Format/lint check (e.g. 'Format Backend', 'Format & Build Frontend'): the job
  runs a formatter then 'git diff --exit-code'. RUN the same formatter locally
  (e.g. 'npm run format', black) and commit the result — do not hand-edit.
- Build failure: reproduce the build locally, fix the cause.
- SonarQube quality gate: the exact failing conditions are NOT always in the
  Actions log — if a SonarQube MCP tool (mcp__sonarqube__*) is available, use it
  (quality_gate_status / issues for this pull request) to read the precise
  failing conditions, then address them in code; otherwise diagnose from the log.
  If the failing condition is COVERAGE on new code, the fix is to make the code
  genuinely covered (ensure the new tests are under a path SonarQube ingests as
  tests — sonar.tests / sonar.test.inclusions — and that coverage.xml reports the
  file), NOT to add the file to sonar.coverage.exclusions.
- Security scan (Wiz/Snyk): address the flagged finding if it is in your diff.

Do NOT suppress a quality/security signal for the code THIS PR adds/modifies just
to go green — default to fixing the cause (exclusions, threshold drops,
nosec/noqa/eslint-disable/# type: ignore, skipping/weakening a test, or bogus
won't-fix/false-positive marks all remove evidence about your own code, and the
reviewer will reject suppressing the very code this PR introduces). Suppression is
legitimate ONLY in narrow cases — generated/vendored code (migrations, protobuf
stubs), a genuine scanner false positive, or truly untestable glue — and only if
you justify it in the PR description AND at the suppression site, scoped as narrowly
as possible. Never suppress code that is the point of the change or that your tests
prove is testable.

Failed checks:
$failed_checks

Log (last 150 lines):
$failed_log"

claude_outcome "$RESULT"
save_session "$ISSUE_NUMBER" "$RESULT"
echo $((retries + 1)) > "$RETRY_FILE"
relabel pr "$PR_NUMBER" "ai-working" ""

if [ "$OUTCOME" = "ok" ] || [ "$ENV_STATUS" = "pushed" ]; then
  log "✓ PR #$PR_NUMBER CI fix pushed (attempt $((retries + 1)))"
else
  gh_fix pr comment "$PR_NUMBER" --repo "$REPO" --body \
"**Agent could not fix CI (attempt $((retries + 1))/$MAX_CI_RETRIES):**

$ENV_SUMMARY" 2>/dev/null || true
  [ "$OUTCOME" = "blocked" ] && relabel pr "$PR_NUMBER" "" "ai-blocked"
fi
