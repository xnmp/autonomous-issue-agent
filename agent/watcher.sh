#!/usr/bin/env bash
# Fix watcher — crash-isolated poll loop. Run under launchd/systemd (auto-restart).
#
# Concurrency model: ONE fixer, sequential dispatch, guarded by a global run-lock
# holding the live PID. Two PRs are never built concurrently, so they cannot race
# on the same files — file-level conflict-freedom by construction.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
source "$(dirname "$0")/watcher-lib.sh"   # fixer_busy/dispatch/ci_state/latest_other_review/maybe_automerge/reap
HERE="$(cd "$(dirname "$0")" && pwd)"
POLL="${POLL_SECONDS:-60}"
RUN_LOCK="$STATE_DIR/locks/fixer.run"

poll_once() {
  reap

  # 1) New issues, priority order, ONE at a time.
  if ! fixer_busy; then
    next=$(gh_fix issue list --repo "$REPO" --label ai-ready --json number,labels --jq '
      [.[] | {number, p:(if (.labels|map(.name)|index("P1")) then 1
                         elif (.labels|map(.name)|index("P2")) then 2
                         elif (.labels|map(.name)|index("P3")) then 3 else 4 end)}]
      | sort_by(.p) | .[0].number // ""' 2>/dev/null || echo "")
    if [ -n "$next" ]; then
      relabel issue "$next" "ai-ready" "ai-working"
      log "→ resolving #$next"
      dispatch "$HERE/resolve-issue.sh" "$next"
    fi
  fi

  # 2) Blocked issues with a NEW human reply. We track the timestamp of the last
  #    reply we already resumed on (issue-N.resumed-at) so a single reply does not
  #    re-trigger resume-issue.sh on every poll (the infinite-resume loop).
  if ! fixer_busy; then
    for n in $(gh_fix issue list --repo "$REPO" --label ai-blocked --json number --jq '.[].number' 2>/dev/null); do
      [ -s "$STATE_DIR/issue-$n.session" ] || continue
      last_at=$(gh_fix issue view "$n" --repo "$REPO" --json comments \
        --jq "[.comments[] | select(.author.login != \"$FIX_BOT\" and (.author.login|endswith(\"[bot]\")|not))] | last | .createdAt // \"\"" 2>/dev/null)
      [ -n "$last_at" ] || continue
      seen_at=$(cat "$STATE_DIR/issue-$n.resumed-at" 2>/dev/null || echo "")
      if [ "$last_at" != "$seen_at" ]; then
        echo "$last_at" > "$STATE_DIR/issue-$n.resumed-at"
        log "→ resume #$n (new reply $last_at)"
        dispatch "$HERE/resume-issue.sh" "$n"; break
      fi
    done
  fi

  # 3) Open PRs: review responses, CI, and auto-merge.
  for pr in $(gh_fix pr list --repo "$REPO" --label ai-pr-open --json number --jq '.[].number' 2>/dev/null); do
    # 3z) A PR awaiting human clarification (ai-blocked) is left alone — do not
    #     re-dispatch address-review every poll. A human removes ai-blocked (or
    #     replies and clears it) to resume. This prevents the blocked-review loop.
    blocked=$(gh_fix pr view "$pr" --repo "$REPO" --json labels --jq '[.labels[].name]|index("ai-blocked")!=null' 2>/dev/null || echo false)
    [ "$blocked" = true ] && continue

    # 3a) New CHANGES_REQUESTED by the OTHER identity (review-bot or a human).
    if ! fixer_busy; then
      review=$(latest_other_review "$pr")
      case "$review" in
        CHANGES_REQUESTED*)
          log "→ address review on PR #$pr"; dispatch "$HERE/address-review.sh" "$pr"; continue ;;
      esac
    fi
    # 3b) CI failing and not already being worked.
    if ! fixer_busy; then
      working=$(gh_fix pr view "$pr" --repo "$REPO" --json labels --jq '[.labels[].name]|index("ai-working")!=null' 2>/dev/null || echo false)
      if [ "$working" != true ] && [ "$(ci_state "$pr")" = failure ]; then
        log "→ fix CI on PR #$pr"; dispatch "$HERE/fix-ci.sh" "$pr"; continue
      fi
    fi
    # 3c) Approved + green → enable auto-merge (does not need the fixer lock).
    maybe_automerge "$pr"
  done

  # 4) Merged/closed PRs → cleanup.
  for pr in $(gh_fix pr list --repo "$REPO" --label ai-pr-open --state all --json number,state \
                --jq '.[] | select(.state=="MERGED" or .state=="CLOSED") | .number' 2>/dev/null); do
    log "→ cleanup PR #$pr"
    rm -f "$STATE_DIR/pr-$pr.automerge" 2>/dev/null || true
    "$HERE/cleanup.sh" "$pr" || true
  done
}

log "fix watcher: $REPO (single fixer, mode=$IDENTITY_MODE, automerge=$AUTO_MERGE, poll ${POLL}s)"
# Heartbeat: a 60s poll would flood the log if logged every wake, so emit a
# periodic "alive" line at most every HEARTBEAT_SECONDS, PLUS an immediate warning
# whenever the actual wall-clock gap greatly exceeds POLL — the signature of a
# laptop suspend (monotonic sleep pauses while suspended) or a wedged pass. This
# makes "is it actually running, or frozen?" answerable from the log.
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-900}"
_hb_last=$(date +%s 2>/dev/null || echo 0); _hb_logged=$_hb_last
while true; do
  _hb_now=$(date +%s 2>/dev/null || echo 0)
  _hb_elapsed=$(( _hb_now - _hb_last )); _hb_last=$_hb_now
  if [ "$_hb_elapsed" -gt $(( POLL * 4 )) ]; then
    log "♥ heartbeat: woke after ${_hb_elapsed}s (expected ~${POLL}s) — likely suspended/stalled; catching up"
    _hb_logged=$_hb_now
  elif [ $(( _hb_now - _hb_logged )) -ge "$HEARTBEAT_SECONDS" ]; then
    log "♥ heartbeat: alive (poll ${POLL}s)"; _hb_logged=$_hb_now
  fi
  poll_once || log "poll pass failed (continuing): $?"
  sleep "$POLL"
done
