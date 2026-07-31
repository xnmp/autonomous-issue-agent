#!/usr/bin/env bash
# Install the three launchd agents (macOS). Fills @HARNESS_ROOT@ / @CONFIG@ in
# the plist templates, copies them to ~/Library/LaunchAgents, and loads them.
#
#   bin/install-launchd.sh           # install + load all three
#   bin/install-launchd.sh uninstall        # unload + remove all
#   bin/install-launchd.sh uninstall <name> # unload + remove AGENT_ENV deployment
set -euo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

deployment_id_for_target() {
  target_dir="$1"
  if [ -n "${ISSUE_AGENT_DEPLOYMENT_ID:-}" ]; then
    normalized="$(printf '%s' "$ISSUE_AGENT_DEPLOYMENT_ID" \
      | tr '[:upper:]' '[:lower:]' \
      | sed -E 's/[^a-z0-9._-]/-/g; s/^-+//; s/-+$//')"
    printf '%s' "${normalized:-target}"
    return
  fi
  printf '%s' "$CONFIG|$target_dir" | cksum | awk '{print $1}'
}

plist_deployment_identity() {
  sed -n \
    's#.*<key>ISSUE_AGENT_DEPLOYMENT_ID</key>[[:space:]]*<string>\([^<]*\)</string>.*#\1#p' \
    "$1" | head -n 1
}

units=(fix-watcher review-watcher steward)
DRY_RUN=0
[ "${1:-}" != "--dry-run" ] || DRY_RUN=1
case "${1:-}" in
  --fix|--review|--steward)
    echo "partial launchd upgrades are unsafe: install all roles so every legacy writer stops before SQLite migration" >&2
    exit 2
    ;;
esac

if [ "${1:-}" = "uninstall" ]; then
  if [ -n "${2:-}" ]; then
    [ -f "$CONFIG" ] || {
      echo "config not found: $CONFIG; refusing scoped uninstall" >&2
      exit 1
    }
    TARGET_DIR="$(set -a; . "$CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
    deployment_id="$(deployment_id_for_target "$TARGET_DIR")"
    deployment_name="${ISSUE_AGENT_DEPLOYMENT_ID:-$deployment_id}"
    patterns=(
      "$LA/dev.issue-agent.fix-watcher.$deployment_id.plist"
      "$LA/dev.issue-agent.review-watcher.$deployment_id.plist"
      "$LA/dev.issue-agent.steward.$deployment_id.plist"
    )
    if [ -n "${ISSUE_AGENT_DEPLOYMENT_ID:-}" ]; then
      for plist in "$LA"/dev.issue-agent.*.plist; do
        [ -e "$plist" ] || continue
        [ "$(plist_deployment_identity "$plist")" = "$deployment_name" ] \
          || continue
        patterns+=("$plist")
      done
    fi
  else
    patterns=(
      "$LA"/dev.issue-agent.fix-watcher*.plist
      "$LA"/dev.issue-agent.review-watcher*.plist
      "$LA"/dev.issue-agent.steward*.plist
    )
  fi
  for plist in "${patterns[@]}"; do
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
deployment_id="$(deployment_id_for_target "$TARGET_DIR")"
DEPLOYMENT_NAME="${ISSUE_AGENT_DEPLOYMENT_ID:-$deployment_id}"
LOG_DIR="$HOME/Library/Logs/issue-agent/$deployment_id"
mkdir -p "$LOG_DIR"
SNAP_DIR="$HOME/Library/Application Support/issue-agent"
mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
SNAP_CONFIG="$SNAP_DIR/$deployment_id.env"
if [ -f "$SNAP_CONFIG" ]; then
  SNAP_REPO="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${REPO:-}")"
  SNAP_TARGET="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
  if [ "$SNAP_REPO" != "$CONFIG_REPO" ]; then
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
      -e "s#@DEPLOYMENT_ID@#$DEPLOYMENT_NAME#g" \
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
  if launchctl list "$label" >/dev/null 2>&1; then
    if [ ! -f "$dst" ]; then
      echo "REFUSING cutover: loaded current job $label has no recoverable plist at $dst" >&2
      exit 2
    fi
    touch "$STAGE/$index.loaded"
  fi
done
for index in "${!units[@]}"; do
  u="${units[$index]}"
  legacy_label="dev.issue-agent.$u"
  legacy_dst="$LA/$legacy_label.plist"
  if launchctl list "$legacy_label" >/dev/null 2>&1; then
    if [ ! -f "$legacy_dst" ]; then
      echo "REFUSING cutover: loaded legacy job $legacy_label has no recoverable plist at $legacy_dst" >&2
      exit 2
    fi
    touch "$STAGE/$index.legacy.loaded"
  fi
  [ ! -f "$legacy_dst" ] \
    || cp "$legacy_dst" "$STAGE/$index.legacy.previous"
done

# Releases before stable deployment labels keyed plist names by
# CONFIG|TARGET_DIR even though the plist already carried the stable CLI
# identity. Discover those predecessors by their embedded identity so a
# checkout repoint cannot leave the old persistent writers running.
predecessor_plists=()
predecessor_labels=()
if [ -n "${ISSUE_AGENT_DEPLOYMENT_ID:-}" ]; then
  for u in "${units[@]}"; do
    current="$LA/dev.issue-agent.$u.$deployment_id.plist"
    unsuffixed="$LA/dev.issue-agent.$u.plist"
    for predecessor in "$LA/dev.issue-agent.$u."*.plist; do
      [ -e "$predecessor" ] || continue
      [ "$predecessor" != "$current" ] || continue
      [ "$predecessor" != "$unsuffixed" ] || continue
      [ "$(plist_deployment_identity "$predecessor")" = "$DEPLOYMENT_NAME" ] \
        || continue
      predecessor_plists+=("$predecessor")
      predecessor_labels+=("$(basename "$predecessor" .plist)")
    done
  done
fi
for index in "${!predecessor_plists[@]}"; do
  predecessor="${predecessor_plists[$index]}"
  label="${predecessor_labels[$index]}"
  cp "$predecessor" "$STAGE/predecessor.$index.previous"
  launchctl list "$label" >/dev/null 2>&1 \
    && touch "$STAGE/predecessor.$index.loaded" || true
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
  done
  for index in "${!units[@]}"; do
    u="${units[$index]}"
    legacy_label="dev.issue-agent.$u"
    legacy_dst="$LA/$legacy_label.plist"
    launchctl unload "$legacy_dst" >/dev/null 2>&1
    if [ -f "$STAGE/$index.legacy.previous" ]; then
      mv "$STAGE/$index.legacy.previous" "$legacy_dst"
    else
      rm -f -- "$legacy_dst"
    fi
  done
  for index in "${!predecessor_plists[@]}"; do
    predecessor="${predecessor_plists[$index]}"
    launchctl unload "$predecessor" >/dev/null 2>&1
    if [ -f "$STAGE/predecessor.$index.previous" ]; then
      mv "$STAGE/predecessor.$index.previous" "$predecessor"
    fi
  done
  for index in "${!units[@]}"; do
    u="${units[$index]}"
    label="dev.issue-agent.$u.$deployment_id"
    dst="$LA/$label.plist"
    [ ! -f "$STAGE/$index.loaded" ] \
      || launchctl load "$dst" >/dev/null 2>&1
  done
  for index in "${!units[@]}"; do
    u="${units[$index]}"
    legacy_label="dev.issue-agent.$u"
    legacy_dst="$LA/$legacy_label.plist"
    [ ! -f "$STAGE/$index.legacy.loaded" ] \
      || launchctl load "$legacy_dst" >/dev/null 2>&1
  done
  for index in "${!predecessor_plists[@]}"; do
    [ ! -f "$STAGE/predecessor.$index.loaded" ] \
      || launchctl load "${predecessor_plists[$index]}" >/dev/null 2>&1
  done
  echo "installation failed; restored previous known-good plists and config" >&2
  exit "$status"
}
trap rollback ERR INT TERM

# Stop every deployment-scoped writer while the rollback snapshot is still the
# config they were launched with. Replacing it first lets an old process reload
# new authority during the cutover window.
for index in "${!units[@]}"; do
  u="${units[$index]}"
  label="dev.issue-agent.$u.$deployment_id"
  dst="$LA/$label.plist"
  if [ -f "$dst" ] && [ -f "$STAGE/$index.loaded" ]; then
    launchctl unload "$dst"
  elif [ -f "$dst" ]; then
    launchctl unload "$dst" 2>/dev/null || true
  fi
done

# Stop and remove target-derived predecessors owned by this stable deployment
# before changing its snapshot. Rollback above restores both their files and
# loaded state if any replacement job fails to load.
for index in "${!predecessor_plists[@]}"; do
  predecessor="${predecessor_plists[$index]}"
  label="${predecessor_labels[$index]}"
  if [ -f "$STAGE/predecessor.$index.loaded" ]; then
    launchctl unload "$predecessor"
  else
    launchctl unload "$predecessor" 2>/dev/null || true
  fi
  launchctl remove "$label" 2>/dev/null || true
  rm -f -- "$predecessor"
done

# Unsuffixed jobs predate deployment-scoped labels. Stop the complete writer
# set before replacing config or loading any role, and retain their files in
# STAGE so a later load failure can restore the old deployment atomically.
for index in "${!units[@]}"; do
  u="${units[$index]}"
  legacy_label="dev.issue-agent.$u"
  legacy_dst="$LA/$legacy_label.plist"
  if [ -f "$legacy_dst" ] && [ -f "$STAGE/$index.legacy.loaded" ]; then
    launchctl unload "$legacy_dst"
  elif [ -f "$legacy_dst" ]; then
    launchctl unload "$legacy_dst" 2>/dev/null || true
  fi
  launchctl remove "$legacy_label" 2>/dev/null || true
  rm -f -- "$legacy_dst"
done

install -m 600 "$CONFIG" "$STAGE/config.next"
mv "$STAGE/config.next" "$SNAP_CONFIG"
for index in "${!units[@]}"; do
  u="${units[$index]}"
  label="dev.issue-agent.$u.$deployment_id"
  dst="$LA/$label.plist"
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
