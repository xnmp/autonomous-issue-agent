#!/usr/bin/env bash
# Install the three systemd user services (Linux). Mirrors install-launchd.sh.
#
#   bin/install-systemd.sh            # install + enable + start all three
#   bin/install-systemd.sh uninstall  # stop + disable + remove all
set -uo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
UD="$HOME/.config/systemd/user"
mkdir -p "$UD"

services=(issue-agent-fix-watcher issue-agent-review-watcher issue-agent-steward)

if [ "${1:-}" = "uninstall" ]; then
  for s in "${services[@]}"; do
    systemctl --user disable --now "$s.service" 2>/dev/null || true
    rm -f "$UD/$s.service"
  done
  systemctl --user daemon-reload 2>/dev/null || true
  echo "removed all units"; exit 0
fi

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG"; exit 1; }
# Watchers must run with cwd = target repo (cwd-relative git/worktree ops). Inject
# WorkingDirectory from the config's TARGET_DIR. (lib.sh also cd's as a backstop.)
TARGET_DIR="$(set -a; . "$CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
[ -n "$TARGET_DIR" ] || echo "⚠ TARGET_DIR not set in $CONFIG — watchers would run from the harness dir (wrong repo). Set TARGET_DIR."
for s in "${services[@]}"; do
  src="$HARNESS_ROOT/ops/systemd/$s.service"
  sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" -e "s#@CONFIG@#$CONFIG#g" -e "s#@TARGET_DIR@#${TARGET_DIR:-/}#g" "$src" > "$UD/$s.service"
done
systemctl --user daemon-reload
for s in "${services[@]}"; do
  systemctl --user enable --now "$s.service"
  echo "✓ enabled+started $s"
done
echo "Done. 'systemctl --user status issue-agent-*' to confirm."
