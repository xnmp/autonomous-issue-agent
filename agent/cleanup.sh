#!/usr/bin/env bash
# Post-merge (and closed-unmerged) cleanup. Detects merge via PR state, removes
# the worktree, drops per-issue state, and relabels.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
PR_NUMBER="${1:?usage: cleanup.sh <pr-number>}"
ISSUE_NUMBER=$(issue_for_pr "$PR_NUMBER")
# Fallback: derive the issue from the PR's head branch when there is no closing
# reference, so a PR without "Fixes #n" is not stranded in ai-pr-open forever.
if [ -z "$ISSUE_NUMBER" ]; then
  ISSUE_NUMBER=$(issue_from_branch "$(branch_for_pr "$PR_NUMBER")")
fi

git worktree remove "$WORKTREE_ROOT/issue-$ISSUE_NUMBER" --force 2>/dev/null || true
# prune any dangling worktree admin entry
git worktree prune 2>/dev/null || true
rm -f "$STATE_DIR/issue-$ISSUE_NUMBER".{session,ci-retries,resumes,resumed-at,automerge} 2>/dev/null || true

# Always drop ai-pr-open from the PR itself so the watcher's cleanup query stops
# matching it (otherwise a PR with no resolvable issue re-triggers cleanup every
# poll forever).
relabel pr "$PR_NUMBER" "ai-pr-open" ""

# GitHub auto-closes a linked issue ONLY when the PR merges into the DEFAULT
# branch. We merge into BASE_BRANCH (dev), so the closing-reference never fires
# and the issue would sit OPEN+ai-done forever. Close it ourselves (knob:
# CLOSE_ISSUE_ON_MERGE=0 to leave it open). The close carries a comment linking
# the PR, since GitHub adds no "closed via #n" event for non-default-branch merges.
CLOSE_ISSUE_ON_MERGE="${CLOSE_ISSUE_ON_MERGE:-1}"

state=$(gh_fix pr view "$PR_NUMBER" --repo "$REPO" --json state --jq '.state' 2>/dev/null || echo UNKNOWN)
if [ "$state" = "MERGED" ]; then
  if [ -n "$ISSUE_NUMBER" ]; then
    relabel issue "$ISSUE_NUMBER" "ai-pr-open" "ai-done"
    if [ "$CLOSE_ISSUE_ON_MERGE" = 1 ]; then
      gh_fix issue close "$ISSUE_NUMBER" --repo "$REPO" --reason completed \
        --comment "Resolved by #$PR_NUMBER (merged into \`$BASE_BRANCH\`)." 2>/dev/null || true
    fi
  fi
  log "✓ PR #$PR_NUMBER merged; issue #${ISSUE_NUMBER:-?} cleaned up"
else
  [ -n "$ISSUE_NUMBER" ] && relabel issue "$ISSUE_NUMBER" "ai-pr-open" "ai-ready"  # closed unmerged → re-queue
  log "✓ PR #$PR_NUMBER $state; cleaned up${ISSUE_NUMBER:+, issue #$ISSUE_NUMBER re-queued}"
fi
