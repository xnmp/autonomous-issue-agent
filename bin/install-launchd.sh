#!/usr/bin/env bash
# Install the three launchd agents (macOS). Fills @HARNESS_ROOT@ / @CONFIG@ in
# the plist templates, copies them to ~/Library/LaunchAgents, and loads them.
#
#   bin/install-launchd.sh           # install + load all three
#   bin/install-launchd.sh --fix     # only the fix watcher
#   bin/install-launchd.sh uninstall # unload + remove all
set -euo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

units=(fix-watcher review-watcher steward)
DRY_RUN=0
[ "${1:-}" != "--dry-run" ] || DRY_RUN=1
[ "${1:-}" = "--fix" ]    && units=(fix-watcher)
[ "${1:-}" = "--review" ] && units=(review-watcher)
[ "${1:-}" = "--steward" ] && units=(steward)

if [ "${1:-}" = "uninstall" ]; then
  for plist in "$LA"/dev.issue-agent.fix-watcher*.plist \
               "$LA"/dev.issue-agent.review-watcher*.plist \
               "$LA"/dev.issue-agent.steward*.plist; do
    [ -e "$plist" ] || continue
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "unloaded + removed $(basename "$plist" .plist)"
  done
  exit 0
fi

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG (copy config/agent.env.example → config/agent.env)"; exit 1; }
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "uv executable not found"; exit 1; }
PLUTIL="$(command -v plutil || true)"
[ -n "$PLUTIL" ] || { echo "plutil is required to validate rendered plists"; exit 1; }

# Resolve TARGET_DIR from the config → the watchers MUST run with cwd = target
# repo (cwd-relative git/worktree ops). launchd has no working dir by default, so
# inject it into the plist. (lib.sh also cd's to TARGET_DIR as a backstop.)
TARGET_DIR="$(set -a; . "$CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
CONFIG_REPO="$(set -a; . "$CONFIG"; set +a; printf '%s' "${REPO:-}")"
[ -n "$TARGET_DIR" ] || echo "⚠ TARGET_DIR not set in $CONFIG — watchers will run from the harness dir and create worktrees in the wrong repo. Set TARGET_DIR."
deployment_id="$(printf '%s' "$CONFIG|$TARGET_DIR" | cksum | awk '{print $1}')"
LOG_DIR="$HOME/Library/Logs/issue-agent/$deployment_id"
mkdir -p "$LOG_DIR"
SNAP_DIR="$HOME/Library/Application Support/issue-agent"
mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
SNAP_CONFIG="$SNAP_DIR/$deployment_id.env"
if [ -f "$SNAP_CONFIG" ]; then
  SNAP_REPO="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${REPO:-}")"
  SNAP_TARGET="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
  if [ "$SNAP_REPO" != "$CONFIG_REPO" ] || [ "$SNAP_TARGET" != "$TARGET_DIR" ]; then
    echo "REFUSING deployment '$deployment_id': existing snapshot belongs to REPO='$SNAP_REPO' TARGET_DIR='$SNAP_TARGET', requested REPO='$CONFIG_REPO' TARGET_DIR='$TARGET_DIR'" >&2
    exit 2
  fi
fi
STAGE="$(mktemp -d "$LA/.issue-agent-install.XXXXXX")"
cleanup() {
  rm -rf -- "$STAGE"
}
trap cleanup EXIT

for index in "${!units[@]}"; do
  u="${units[$index]}"
  template_label="dev.issue-agent.$u"
  label="$template_label.$deployment_id"
  src="$HARNESS_ROOT/ops/launchd/$template_label.plist"
  candidate="$STAGE/$label.plist"
  sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" \
      -e "s#@CONFIG@#$SNAP_CONFIG#g" \
      -e "s#@TARGET_DIR@#${TARGET_DIR:-/}#g" \
      -e "s#@LABEL@#$label#g" \
      -e "s#@LOG_DIR@#$LOG_DIR#g" \
      -e "s#@UV_BIN@#$UV_BIN#g" "$src" > "$candidate"
  "$PLUTIL" -lint "$candidate" >/dev/null
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "validated ${#units[@]} plist(s) for deployment '$deployment_id'"
  echo "executable: $UV_BIN"
  echo "config snapshot: $SNAP_CONFIG"
  exit 0
fi

[ ! -f "$SNAP_CONFIG" ] || cp "$SNAP_CONFIG" "$STAGE/config.previous"
for index in "${!units[@]}"; do
  u="${units[$index]}"
  label="dev.issue-agent.$u.$deployment_id"
  dst="$LA/$label.plist"
  [ ! -f "$dst" ] || cp "$dst" "$STAGE/$index.previous"
  launchctl list "$label" >/dev/null 2>&1 \
    && touch "$STAGE/$index.loaded" || true
done

rollback() {
  status=$?
  [ "$status" -ne 0 ] || status=1
  trap - ERR INT TERM
  set +e
  if [ -f "$STAGE/config.previous" ]; then
    mv "$STAGE/config.previous" "$SNAP_CONFIG"
  else
    rm -f -- "$SNAP_CONFIG"
  fi
  for index in "${!units[@]}"; do
    u="${units[$index]}"
    label="dev.issue-agent.$u.$deployment_id"
    dst="$LA/$label.plist"
    launchctl unload "$dst" >/dev/null 2>&1
    if [ -f "$STAGE/$index.previous" ]; then
      mv "$STAGE/$index.previous" "$dst"
    else
      rm -f -- "$dst"
    fi
    [ ! -f "$STAGE/$index.loaded" ] \
      || launchctl load "$dst" >/dev/null 2>&1
  done
  echo "installation failed; restored previous known-good plists and config" >&2
  exit "$status"
}
trap rollback ERR INT TERM

install -m 600 "$CONFIG" "$STAGE/config.next"
mv "$STAGE/config.next" "$SNAP_CONFIG"
for index in "${!units[@]}"; do
  u="${units[$index]}"
  label="dev.issue-agent.$u.$deployment_id"
  dst="$LA/$label.plist"
  [ ! -f "$dst" ] || launchctl unload "$dst" 2>/dev/null || true
  mv "$STAGE/$label.plist" "$dst"
done

for u in "${units[@]}"; do
  label="dev.issue-agent.$u.$deployment_id"
  dst="$LA/$label.plist"
  launchctl load "$dst"
  echo "✓ loaded $label  (logs: $LOG_DIR/$u.*.log)"
done
trap - ERR INT TERM
echo "Done. 'launchctl list | grep issue-agent' to confirm."
