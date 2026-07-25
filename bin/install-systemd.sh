#!/usr/bin/env bash
# Install the three systemd user services (Linux). Mirrors install-launchd.sh.
#
#   bin/install-systemd.sh                # install + enable + start all three,
#                                          # for the repo config AGENT_ENV points at
#   bin/install-systemd.sh uninstall      # stop + disable + remove ALL issue-agent
#                                          # units (every repo installed this way)
#   bin/install-systemd.sh uninstall <name>  # remove just <name>'s three units
#
# Per-repo naming: units install as issue-agent-{fix-watcher,review-watcher,
# steward}-<NAME>.service, where NAME is derived from the config's TARGET_DIR
# (sanitized basename) — the same "one checkout, one name" convention
# bin/point-at.sh uses for per-repo logs. Without this, a second `install-
# systemd.sh` run (for a different repo) would silently overwrite the FIRST
# repo's units (same fixed names) and repoint its already-enabled units at the
# second repo on next restart.
#
# Config snapshot: AGENT_ENV usually points at the shared, mutable
# config/.active.env, which bin/point-at.sh / `issue-agent install-service`
# REWRITE on every run — for whichever repo composed it last. Baking that path
# straight into the unit would mean installing repo B later silently repoints
# repo A's unit at repo B's config the next time it restarts. Instead this
# script copies the resolved config to a stable per-repo file
# (~/.config/issue-agent/<name>.env, chmod 600 — it may carry tokens) and bakes
# THAT path in, so each repo's unit keeps reading its own config.
set -uo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
UD="$HOME/.config/systemd/user"
mkdir -p "$UD"

services=(issue-agent-fix-watcher issue-agent-review-watcher issue-agent-steward)

if [ "${1:-}" = "uninstall" ]; then
  if [ -n "${2:-}" ]; then
    pattern="issue-agent-*-$2.service"
  else
    pattern="issue-agent-*.service"
  fi
  removed=0
  for f in "$UD"/$pattern; do
    [ -e "$f" ] || continue
    s="$(basename "$f" .service)"
    systemctl --user disable --now "$s.service" 2>/dev/null || true
    rm -f "$f"
    removed=$((removed + 1))
  done
  systemctl --user daemon-reload 2>/dev/null || true
  if [ "$removed" -eq 0 ]; then
    echo "no matching units found (${2:-all repos})"
  else
    echo "removed $removed unit(s)${2:+ for '$2'}"
  fi
  exit 0
fi

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG"; exit 1; }
# Watchers must run with cwd = target repo (cwd-relative git/worktree ops). Inject
# WorkingDirectory from the config's TARGET_DIR. (lib.sh also cd's as a backstop.)
TARGET_DIR="$(set -a; . "$CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
[ -n "$TARGET_DIR" ] || echo "⚠ TARGET_DIR not set in $CONFIG — watchers would run from the harness dir (wrong repo). Set TARGET_DIR."

# Derive NAME from the checkout dir (lowercased, sanitized basename) — falls
# back to "target" if TARGET_DIR is empty (legacy single-repo config with no
# TARGET_DIR set; still installs, just without a distinguishing suffix).
_raw_name="$(basename "${TARGET_DIR:-target}")"
NAME="$(printf '%s' "$_raw_name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]/-/g; s/^-+//; s/-+$//')"
NAME="${NAME:-target}"

SNAP_DIR="$HOME/.config/issue-agent"
mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
SNAP_CONFIG="$SNAP_DIR/$NAME.env"
cp "$CONFIG" "$SNAP_CONFIG"
chmod 600 "$SNAP_CONFIG"

for s in "${services[@]}"; do
  src="$HARNESS_ROOT/ops/systemd/$s.service"
  dst="$UD/$s-$NAME.service"
  sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" -e "s#@CONFIG@#$SNAP_CONFIG#g" \
      -e "s#@TARGET_DIR@#${TARGET_DIR:-/}#g" -e "s#@NAME@#$NAME#g" "$src" > "$dst"
done
systemctl --user daemon-reload
for s in "${services[@]}"; do
  systemctl --user enable --now "$s-$NAME.service"
  echo "✓ enabled+started $s-$NAME"
done
echo "Done. 'systemctl --user status issue-agent-*-$NAME' or"
echo "'journalctl --user -u issue-agent-fix-watcher-$NAME -f' to confirm."
