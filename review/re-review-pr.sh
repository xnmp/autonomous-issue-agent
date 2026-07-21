#!/usr/bin/env bash
# Re-review after the author pushed new commits. Incremental diff; escalate at the
# round cap.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
PR_NUMBER="${1:?usage: re-review-pr.sh <pr-number>}"
SESSION_FILE="$STATE_DIR/pr-$PR_NUMBER.session"
[ -s "$SESSION_FILE" ] || { log "no review session for PR #$PR_NUMBER"; exit 1; }
session_id=$(cat "$SESSION_FILE")

# Compute the round locally; only PERSIST round + last-sha AFTER a successful
# review, so a failed/errored re-review neither burns a round nor advances the
# last-seen SHA (which would silently skip the changes on the next poll).
round=$(cat "$STATE_DIR/pr-$PR_NUMBER.round" 2>/dev/null || echo 1); round=$((round + 1))

head_sha=$(gh_review pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null)
last_sha=$(cat "$STATE_DIR/pr-$PR_NUMBER.last-sha" 2>/dev/null || echo "")

new_diff=""
if [ -n "$last_sha" ]; then
  new_diff=$(gh_review api "repos/$REPO/compare/$last_sha...$head_sha" \
    --jq '.files[]? | "--- \(.filename)\n\(.patch // "[binary or too large — see full diff]")\n"' 2>/dev/null || echo "")
fi
[ -z "$new_diff" ] && new_diff=$(gh_review pr diff "$PR_NUMBER" --repo "$REPO" 2>/dev/null || true)

review_claude_args
prompt="${REVIEW_PROMPT//\$ROUND_NUMBER/$round}"
result=$(claude -p "${REVIEW_ARGS[@]}" \
  --append-system-prompt "$prompt" \
  --resume "$session_id" \
  "Author pushed new commits. Review round $round of $REVIEW_MAX_ROUNDS.
Consider whether your prior feedback was addressed; do not re-raise resolved points.
If this is the final round and still unsatisfied, decision=ESCALATE.

Changes since last review ($last_sha → $head_sha):
$new_diff" 2>/dev/null); rc=$?

# See review-pr.sh: jq's `//` treats false as empty; test membership explicitly.
err=$(jq -r 'if (has("is_error") and .is_error == false) then "no" else "yes" end' <<<"$result" 2>/dev/null || echo yes)
if [ "$rc" -ne 0 ] || [ "$err" = yes ]; then
  # Do NOT advance last-sha or round — retry the same diff next poll.
  log "re-review error PR #$PR_NUMBER — will retry next pass (round/SHA not advanced)"; exit 0
fi
# Review succeeded → now it is safe to record progress.
echo "$head_sha" > "$STATE_DIR/pr-$PR_NUMBER.last-sha"
echo "$round"    > "$STATE_DIR/pr-$PR_NUMBER.round"

decision=$(jq -r '.structured_output.decision' <<<"$result")
rbody=$(    jq -r '.structured_output.body'     <<<"$result")
if [ "$decision" = ESCALATE ]; then
  submit_review "$PR_NUMBER" COMMENT "Through $round review rounds without resolution. Requesting human review.

$rbody" || true
  gh_review pr edit "$PR_NUMBER" --repo "$REPO" --add-label needs-human-review --remove-label ai-pr-open 2>/dev/null || true
  log "⚠ PR #$PR_NUMBER escalated after $round rounds"; exit 0
fi
event=$([ "$decision" = APPROVE ] && echo APPROVE || echo REQUEST_CHANGES)
submit_review "$PR_NUMBER" "$event" "$rbody" || true
log "✓ PR #$PR_NUMBER re-reviewed (round $round): $event"
