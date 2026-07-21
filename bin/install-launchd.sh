#!/usr/bin/env bash
# Install the three launchd agents (macOS). Fills @HARNESS_ROOT@ / @CONFIG@ in
# the plist templates, copies them to ~/Library/LaunchAgents, and loads them.
#
#   bin/install-launchd.sh           # install + load all three
#   bin/install-launchd.sh --fix     # only the fix watcher
#   bin/install-launchd.sh uninstall # unload + remove all
set -uo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

units=(fix-watcher review-watcher steward)
[ "${1:-}" = "--fix" ]    && units=(fix-watcher)
[ "${1:-}" = "--review" ] && units=(review-watcher)
[ "${1:-}" = "--steward" ] && units=(steward)

if [ "${1:-}" = "uninstall" ]; then
  for u in fix-watcher review-watcher steward; do
    label="dev.issue-agent.$u"
    launchctl unload "$LA/$label.plist" 2>/dev/null || true
    rm -f "$LA/$label.plist"
    echo "unloaded + removed $label"
  done
  exit 0
fi

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG (copy config/agent.env.example → config/agent.env)"; exit 1; }

# Resolve TARGET_DIR from the config → the watchers MUST run with cwd = target
# repo (cwd-relative git/worktree ops). launchd has no working dir by default, so
# inject it into the plist. (lib.sh also cd's to TARGET_DIR as a backstop.)
TARGET_DIR="$(set -a; . "$CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
[ -n "$TARGET_DIR" ] || echo "⚠ TARGET_DIR not set in $CONFIG — watchers will run from the harness dir and create worktrees in the wrong repo. Set TARGET_DIR."

for u in "${units[@]}"; do
  label="dev.issue-agent.$u"
  src="$HARNESS_ROOT/ops/launchd/$label.plist"
  dst="$LA/$label.plist"
  sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" -e "s#@CONFIG@#$CONFIG#g" -e "s#@TARGET_DIR@#${TARGET_DIR:-/}#g" "$src" > "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "✓ loaded $label  (logs: /tmp/issue-agent-$u.*.log)"
done
echo "Done. 'launchctl list | grep issue-agent' to confirm."
