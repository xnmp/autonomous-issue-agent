# Duplicate-launcher guard shared by point-at.sh and its regression tests.
#
# History: this started as a pgrep -f heuristic over "point-at.sh <name>"
# argvs. That is unwinnable: `$(...)`/pipeline subshells of the launcher
# inherit its argv, wrapper processes (systemd-inhibit, sh -c, the operator's
# shell snapshot) quote the launch command in THEIR argv, and forked
# subshells of any such ancestor inherit it too — each variant made the guard
# refuse its own first run (observed twice on 2026-08-01), and every
# exclusion broad enough to silence them also hid a genuine duplicate
# started from the same terminal. A lock held for the launcher's lifetime has
# none of those failure modes: wrappers and forked subshells never hold it,
# and a real second launcher always collides on it.
#
# The fd is deliberately inherited by the role daemons the launcher spawns:
# if the launcher dies but daemons survive, a second launch would still
# double-dispatch, so the lock must outlive the launcher itself.
acquire_launch_lock() {
  _lg_dir="${XDG_STATE_HOME:-$HOME/.local/state}/issue-agent"
  mkdir -p "$_lg_dir" || return 1
  LAUNCH_LOCK_FILE="$_lg_dir/point-at.$1.lock"
  # Append-mode open: MUST NOT truncate the holder's pid hint on a lost race.
  exec 9>>"$LAUNCH_LOCK_FILE" || return 1
  flock -n 9 || return 1
  # We hold the lock — now the truncating write is safe. Operator hint only.
  printf '%s\n' "$$" >| "$LAUNCH_LOCK_FILE"
  return 0
}

launch_lock_holder_hint() {
  head -n 1 "$LAUNCH_LOCK_FILE" 2>/dev/null || true
}
