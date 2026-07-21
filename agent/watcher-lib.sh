#!/usr/bin/env bash
# Pure helper functions for the fix watcher, factored out so they can be unit
# tested by sourcing this file directly (bash 3.2 cannot `source <(...)` a
# process substitution, so the watcher's logic must live in a real file).
#
# Requires agent/lib.sh to have been sourced first (for gh_fix, relabel, log,
# REPO, FIX_BOT, AUTO_MERGE, MERGE_METHOD, IDENTITY_MODE, STATE_DIR).
#
# RUN_LOCK must be set by the caller (watcher.sh sets it; tests set it too).

fixer_busy() {
  [ -f "${RUN_LOCK:-}" ] || return 1
  local pid
  pid=$(cat "$RUN_LOCK" 2>/dev/null || echo "")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

dispatch() {  # dispatch <script> <args...> — run under the run-lock, in background
  # The PARENT is the sole writer of the lock: it backgrounds the job and records
  # the job PID. The child does NOT remove the lock (that would race the parent's
  # write and could strand a stale PID). Release happens via fixer_busy (a dead
  # PID reads as not-busy) and reap (which removes the stale lock file). Single
  # writer ⇒ no PID-write-after-fork race.
  ( "$@" ) &
  echo $! > "$RUN_LOCK"
}

# CI state for a PR. Echoes ok | pending | failure | none.
#   none    = no checks configured on the PR (nothing to wait on).
#   pending = ANY check still queued/running — CI has NOT settled yet.
#   failure = CI has SETTLED and at least one check failed.
#   ok      = CI has settled and all checks passed.
#
# CRITICAL: `pending` takes precedence over `failure`. A run with one failed
# check but others still queued/running is NOT yet a failure — acting on it makes
# the fixer push a commit that re-triggers the in-flight jobs, looping and burning
# budget on a verdict that wasn't final. The fixer must wait for CI to settle and
# only then react to a genuine, complete failure. (Observed live on PR #49: a
# transient half-finished run dispatched fix-ci repeatedly.)
ci_state() {  # ci_state <pr>
  local out
  # CI_IGNORE_CHECKS (ext-regex of check names) drops environmentally-stuck checks
  # — e.g. jobs queued forever on starved runners — BEFORE deciding state, so they
  # can't pin us at `pending` and deadlock fix-CI + auto-merge. Real failures on
  # any non-ignored check still count.
  #
  # IMPORTANT: `gh pr checks` supports `--jq` but NOT `--arg` (only `gh api` does);
  # passing `--arg` makes it error "unknown flag" and ci_state silently falls back
  # to pending. So fetch raw JSON from gh and pipe through a SEPARATE `jq` we
  # control, where --arg works. (Real-vs-fake seam: the test `gh` fake accepted
  # --arg, so unit tests passed while the live run with real gh did not.)
  local json
  json=$(gh_fix pr checks "$1" --repo "$REPO" --json name,state,bucket 2>/dev/null)
  [ -n "$json" ] || { echo "pending"; return; }
  out=$(printf '%s' "$json" | jq -r --arg ignore "${CI_IGNORE_CHECKS:-}" '
    [ .[] | select($ignore == "" or ((.name // "") | test($ignore) | not)) ]
    | if   length == 0                                            then "none"
      elif any(.[]; .bucket=="pending" or .state=="QUEUED" or .state=="IN_PROGRESS") then "pending"
      elif any(.[]; .bucket=="fail")                             then "failure"
      else "ok" end' 2>/dev/null)
  echo "${out:-pending}"
}

# Latest review state submitted by someone OTHER than the fixer. Echoes
# "APPROVED <login>", "CHANGES_REQUESTED <login>", or "".
latest_other_review() {  # latest_other_review <pr>
  gh_fix api "repos/$REPO/pulls/$1/reviews" --jq \
    "[.[] | select(.user.login != \"$FIX_BOT\")] | sort_by(.submitted_at) | last
     | if . == null then \"\" else \"\(.state) \(.user.login)\" end" 2>/dev/null || echo ""
}

# Enable GitHub auto-merge once approved + green. Idempotent: a marker file stops
# us from re-issuing the API call every poll.
maybe_automerge() {  # maybe_automerge <pr>
  [ "${AUTO_MERGE:-1}" = "1" ] || return 0
  local pr="$1"
  local marker="$STATE_DIR/pr-$pr.automerge"
  [ -f "$marker" ] && return 0

  local review
  review=$(latest_other_review "$pr")
  case "$review" in APPROVED*) : ;; *) return 0 ;; esac

  local ci
  ci=$(ci_state "$pr")
  # Allow merge when CI is green OR no checks are configured (nothing to wait on).
  case "$ci" in ok|none) : ;; *) return 0 ;; esac

  # Auto-merge only in three-identity mode. solo has no second approver (self-
  # approval is GitHub 422). Any other value is a misconfiguration: fail safe by
  # leaving the merge to a human rather than silently auto-merging.
  case "${IDENTITY_MODE:-three}" in
    three) : ;;
    solo)
      log "PR #$pr approved+green but IDENTITY_MODE=solo → leaving for human merge"
      return 0 ;;
    *)
      log "PR #$pr approved+green but IDENTITY_MODE='${IDENTITY_MODE}' is unrecognised → leaving for human merge"
      return 0 ;;
  esac

  if gh_fix pr merge "$pr" --repo "$REPO" --auto "--${MERGE_METHOD:-squash}" --delete-branch 2>/dev/null; then
    : > "$marker"
    log "⇢ PR #$pr: auto-merge enabled (${MERGE_METHOD:-squash}) — approved by ${review#APPROVED }, CI=$ci"
  else
    log "PR #$pr: could not enable auto-merge (branch protection? perms?) — leaving for human"
  fi
}

# Reaper: if no fixer is alive, anything still labelled ai-working is orphaned
# (the script died before resetting the label). Requeue both ISSUES and PRs.
#   - an orphaned issue → ai-ready (re-resolve from scratch)
#   - an orphaned PR    → just drop ai-working; the poll loop re-picks the right
#     next action (re-review/CI/auto-merge) from GitHub ground truth. We do NOT
#     send a PR back to ai-ready (that label is for issues), which would strand it.
reap() {
  fixer_busy && return 0
  rm -f "$RUN_LOCK" 2>/dev/null || true
  for n in $(gh_fix issue list --repo "$REPO" --label ai-working --json number --jq '.[].number' 2>/dev/null); do
    relabel issue "$n" "ai-working" "ai-ready"
    log "reaper: requeued orphaned issue #$n (no live fixer)"
  done
  for pr in $(gh_fix pr list --repo "$REPO" --label ai-working --json number --jq '.[].number' 2>/dev/null); do
    relabel pr "$pr" "ai-working" ""
    log "reaper: cleared ai-working on orphaned PR #$pr (no live fixer)"
  done
}
