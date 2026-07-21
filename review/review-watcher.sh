#!/usr/bin/env bash
# Review watcher — independent poll loop, crash-isolated. Run under launchd/systemd.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"
POLL="${POLL_SECONDS:-60}"

# In solo identity mode there is no separate reviewer — a human reviews instead.
if [ "$IDENTITY_MODE" = "solo" ]; then
  log "review watcher: IDENTITY_MODE=solo → review agent disabled (humans review PRs). Idling."
  # Stay alive (so the service unit doesn't flap) but do nothing.
  while true; do sleep "${POLL}"; done
fi

poll_once() {
  for pr in $(gh_review pr list --repo "$REPO" --label ai-pr-open --json number --jq '.[].number' 2>/dev/null); do
    sf="$STATE_DIR/pr-$pr.session"
    if [ ! -s "$sf" ]; then
      log "→ initial review PR #$pr"; "$HERE/review-pr.sh" "$pr" || true
    else
      cur=$(gh_review pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || echo "")
      last=$(cat "$STATE_DIR/pr-$pr.last-sha" 2>/dev/null || echo "")
      if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
        log "→ re-review PR #$pr"; "$HERE/re-review-pr.sh" "$pr" || true
      fi
    fi
  done
  # Drop sessions for merged/closed PRs.
  for sf in "$STATE_DIR"/pr-*.session; do
    [ -e "$sf" ] || continue
    n=$(basename "$sf" .session | sed 's/^pr-//')
    st=$(gh_review pr view "$n" --repo "$REPO" --json state --jq '.state' 2>/dev/null || echo UNKNOWN)
    case "$st" in
      MERGED|CLOSED) rm -f "$STATE_DIR/pr-$n".{session,last-sha,round}; log "cleaned review state pr-$n" ;;
    esac
  done
}

log "review watcher: $REPO (identity=$REVIEW_BOT, runs-tests=$REVIEWER_RUNS_TESTS, model=${REVIEW_MODEL:-default})"
# Heartbeat: periodic "alive" (throttled to HEARTBEAT_SECONDS) + immediate warning
# when the wall-clock gap >> POLL (laptop-suspend stall / wedged pass). See
# agent/watcher.sh for rationale.
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
  poll_once || log "review poll failed (continuing)"
  sleep "$POLL"
done
