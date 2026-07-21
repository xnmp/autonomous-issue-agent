## Fix Watcher

### Concurrency model: one fixer, sequential dispatch

The fixer dispatches **one issue at a time**. At most one `resolve-issue.sh` / `resume` /
`fix-ci` / `address-review` runs at any moment, guarded by a global run-lock (`fixer.run`,
holding the live PID). This single rule removes every v1 concurrency hazard — double-dispatch,
fictional `area/*` locks, unbounded background fan-out — and gives file-level conflict-freedom by
construction: two PRs are never built concurrently, so they cannot race on the same files. The
only remaining overlap risk is base-drift (an open PR going stale after another merges), which is
a rebase concern handled at merge time, not a dispatch-time lock.

> v2 described a secondary "file-set lock" recorded at PR-open. It was redundant: with a single
> sequential fixer there is never a second concurrent build to lock against. Removed for
> leanness — sequential dispatch already provides the guarantee the lock was meant to add.

```bash
#!/usr/bin/env bash
# agent/watcher.sh — crash-isolated poll loop. Run under launchd/systemd (auto-restart).
set -uo pipefail
source "$(dirname "$0")/lib.sh"
HERE="$(dirname "$0")"
POLL="${POLL_SECONDS:-60}"
RUN_LOCK="$STATE_DIR/locks/fixer.run"        # global single-fixer lock

# A single fixer is busy if the run-lock is held by a live PID.
fixer_busy() {
  [ -f "$RUN_LOCK" ] || return 1
  local pid; pid=$(cat "$RUN_LOCK" 2>/dev/null || echo "")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
# Run a fix script under the run-lock, in the background, recording its PID.
dispatch() {  # dispatch <script> <args...>
  ( "$@"; rm -f "$RUN_LOCK" ) &
  echo $! > "$RUN_LOCK"
}

# Map any non-success CI conclusion to failure; queued/in-progress → pending. (v1 2.6)
ci_state() {  # ci_state <pr> → ok | pending | failure
  gh pr checks "$1" --repo "$REPO" --json state,bucket --jq '
    if   any(.[]; .bucket=="fail")    then "failure"
    elif any(.[]; .bucket=="pending") then "pending"
    elif any(.[]; .state=="QUEUED" or .state=="IN_PROGRESS") then "pending"
    else "ok" end' 2>/dev/null || echo "pending"
}

# Reaper: if no fixer process is alive, any issue still labelled ai-working is orphaned
# (its script died before resetting the label, e.g. v1's set -e abort). Requeue it. (v1 0.3)
reap() {
  fixer_busy && return 0                       # a fixer is genuinely running — leave labels alone
  rm -f "$RUN_LOCK" 2>/dev/null || true        # stale lock from a dead PID
  for n in $(gh issue list --repo "$REPO" --label ai-working --json number --jq '.[].number' 2>/dev/null); do
    relabel issue "$n" "ai-working" "ai-ready"
    log "reaper: requeued orphaned #$n (no live fixer)"
  done
}

# One poll pass, fully isolated so a single gh failure can't kill the daemon. (v1 0.4)
poll_once() {
  reap

  # 1) New issues, priority order, dispatched ONE at a time.
  if ! fixer_busy; then
    next=$(gh issue list --repo "$REPO" --label ai-ready --json number,labels --jq '
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

  # 2) Blocked issues with a fresh human reply.
  if ! fixer_busy; then
    for n in $(gh issue list --repo "$REPO" --label ai-blocked --json number --jq '.[].number' 2>/dev/null); do
      [ -s "$STATE_DIR/issue-$n.session" ] || continue
      last=$(gh issue view "$n" --repo "$REPO" --json comments \
        --jq "[.comments[] | select(.author.login != \"$FIX_BOT\" and (.author.login|endswith(\"[bot]\")|not))] | last | .author.login // \"\"" 2>/dev/null)
      if [ -n "$last" ]; then log "→ resume #$n"; dispatch "$HERE/resume-issue.sh" "$n"; break; fi
    done
  fi

  # 3) Open PRs: review responses and CI.
  for pr in $(gh pr list --repo "$REPO" --label ai-pr-open --json number --jq '.[].number' 2>/dev/null); do
    # 3a) New review by the OTHER identity (review-bot or a human) → address it.
    if ! fixer_busy; then
      reviewer=$(gh api "repos/$REPO/pulls/$pr/reviews" \
        --jq "sort_by(.submitted_at) | last | select(.state==\"CHANGES_REQUESTED\") | .user.login" 2>/dev/null || echo "")
      if [ -n "$reviewer" ] && [ "$reviewer" != "$FIX_BOT" ]; then
        log "→ address review on PR #$pr"; dispatch "$HERE/address-review.sh" "$pr"; continue
      fi
    fi
    # 3b) CI failing and not already being worked.
    if ! fixer_busy; then
      working=$(gh pr view "$pr" --repo "$REPO" --json labels --jq '[.labels[].name]|index("ai-working")!=null' 2>/dev/null || echo false)
      if [ "$working" != true ] && [ "$(ci_state "$pr")" = failure ]; then
        log "→ fix CI on PR #$pr"; dispatch "$HERE/fix-ci.sh" "$pr"
      fi
    fi
  done

  # 4) Merged/closed PRs → cleanup.
  for pr in $(gh pr list --repo "$REPO" --label ai-pr-open --state all --json number,state \
                --jq '.[] | select(.state=="MERGED" or .state=="CLOSED") | .number' 2>/dev/null); do
    log "→ cleanup PR #$pr"; "$HERE/cleanup.sh" "$pr" || true
  done
}

log "fix watcher: $REPO (single fixer, poll ${POLL}s)"
while true; do
  poll_once || log "poll pass failed (continuing): $?"   # isolation: never exit the loop
  sleep "$POLL"
done
```

The crash-isolation guarantee: `poll_once` is a single function call guarded by `|| log …`,
and **no** `gh` call inside it can abort the daemon (each is `|| true`/`|| echo …`, and the
script has no `set -e`). A transient GitHub 5xx or secondary rate-limit degrades one pass, not
the process.

### Service unit (auto-restart)

The daemon must be supervised. macOS launchd:

```xml
<!-- ~/Library/LaunchAgents/dev.issue-agent.fix-watcher.plist -->
<plist version="1.0"><dict>
  <key>Label</key><string>dev.issue-agent.fix-watcher</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/path/to/.claude/agent/watcher.sh</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>REPO</key><string>owner/repo</string>
    <key>FIX_BOT</key><string>fix-bot</string>
    <key>BASE_BRANCH</key><string>dev</string>
  </dict>
  <key>KeepAlive</key><true/>           <!-- restart on crash -->
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardErrorPath</key><string>/tmp/fix-watcher.log</string>
</dict></plist>
```

Linux systemd equivalent: a unit with `Restart=always`, `RestartSec=30`, and the same env.
Either way the loop's internal isolation plus the supervisor's restart give two independent
layers against the v1 "one error kills everything forever" failure.

---

← Prev: [Fix Agent Scripts](04-fix-scripts.md) · [Index](README.md) · Next: [Review Agent](06-review-agent.md)  →
