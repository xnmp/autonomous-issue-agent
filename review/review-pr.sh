#!/usr/bin/env bash
# Initial review of a new PR.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
PR_NUMBER="${1:?usage: review-pr.sh <pr-number>}"

meta=$(gh_review pr view "$PR_NUMBER" --repo "$REPO" --json title,body,headRefName,headRefOid 2>/dev/null)
[ -n "$meta" ] || { log "PR #$PR_NUMBER not readable"; exit 1; }
title=$(jq -r '.title' <<<"$meta"); body=$(jq -r '.body // ""' <<<"$meta")
head_sha=$(jq -r '.headRefOid' <<<"$meta"); head_branch=$(jq -r '.headRefName' <<<"$meta")
diff=$(gh_review pr diff "$PR_NUMBER" --repo "$REPO" 2>/dev/null || true)

checkout_note=""
if [ "$REVIEWER_RUNS_TESTS" = 1 ]; then
  wt="$STATE_DIR/wt-pr-$PR_NUMBER"
  git worktree remove "$wt" --force 2>/dev/null || true
  gh_review pr checkout "$PR_NUMBER" --repo "$REPO" >/dev/null 2>&1 || true
  git worktree add "$wt" "$head_branch" 2>/dev/null || true
  checkout_note="A checkout is at $wt. You MAY run the test command (${TEST_CMD:-none}) there to verify behavior."
fi

review_claude_args
prompt="${REVIEW_PROMPT//\$ROUND_NUMBER/1}"
result=$(claude -p "${REVIEW_ARGS[@]}" \
  --append-system-prompt "$prompt" \
  "Review PR #$PR_NUMBER: $title

Description:
$body

$checkout_note

Diff:
$diff" 2>/dev/null); rc=$?

[ "$REVIEWER_RUNS_TESTS" = 1 ] && git worktree remove "$STATE_DIR/wt-pr-$PR_NUMBER" --force 2>/dev/null || true

# NOTE: jq's `//` treats `false` as empty, so BOTH `.is_error // true` and
# `(.is_error // true) == true` wrongly report success as an error. Test
# membership explicitly: only an explicit is_error:false counts as success.
err=$(jq -r 'if (has("is_error") and .is_error == false) then "no" else "yes" end' <<<"$result" 2>/dev/null || echo yes)
if [ "$rc" -ne 0 ] || [ "$err" = yes ]; then
  log "review error on PR #$PR_NUMBER (rc=$rc) — leaving for next pass"; exit 0
fi
jq -r '.session_id // ""' <<<"$result" > "$STATE_DIR/pr-$PR_NUMBER.session"
echo "$head_sha"                       > "$STATE_DIR/pr-$PR_NUMBER.last-sha"
echo 1                                 > "$STATE_DIR/pr-$PR_NUMBER.round"

decision=$(jq -r '.structured_output.decision' <<<"$result")
rbody=$(    jq -r '.structured_output.body'     <<<"$result")
event=$([ "$decision" = APPROVE ] && echo APPROVE || echo REQUEST_CHANGES)
submit_review "$PR_NUMBER" "$event" "$rbody" || log "could not submit review on #$PR_NUMBER"
log "✓ PR #$PR_NUMBER reviewed: $event"
