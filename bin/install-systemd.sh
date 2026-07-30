#!/usr/bin/env bash
# Install the three systemd user services (Linux). Mirrors install-launchd.sh.
#
#   bin/install-systemd.sh                # install + enable + start all three,
#                                          # for the repo config AGENT_ENV points at
#   bin/install-systemd.sh uninstall      # stop + disable + remove ALL issue-agent
#                                          # units (every repo installed this way)
#   bin/install-systemd.sh uninstall <name>  # remove just <name>'s three units
#
# Per-deployment naming: the CLI passes its resolved overlay identity as
# ISSUE_AGENT_DEPLOYMENT_ID. Direct legacy invocations derive owner+repository
# from REPO, then fall back to TARGET_DIR's basename only when REPO is absent.
#
# Config snapshot: AGENT_ENV usually points at the shared, mutable
# config/.active.<name>.env, which bin/point-at.sh / `issue-agent install-service`
# REWRITE on every run — for whichever repo composed it last. Baking that path
# straight into the unit would mean installing repo B later silently repoints
# repo A's unit at repo B's config the next time it restarts. Instead this
# script copies the resolved config to a stable per-repo file
# (~/.config/issue-agent/<name>.env, chmod 600 — it may carry tokens) and bakes
# THAT path in, so each repo's unit keeps reading its own config.
set -euo pipefail
HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"
UD="$HOME/.config/systemd/user"
mkdir -p "$UD"

services=(issue-agent-fix-watcher issue-agent-review-watcher issue-agent-steward)
DRY_RUN=0
[ "${1:-}" != "--dry-run" ] || DRY_RUN=1

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
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "uv executable not found"; exit 1; }
SYSTEMD_ANALYZE="$(command -v systemd-analyze || true)"
[ -n "$SYSTEMD_ANALYZE" ] || {
  echo "systemd-analyze is required to validate rendered units"
  exit 1
}
# Watchers must run with cwd = target repo (cwd-relative git/worktree ops). Inject
# WorkingDirectory from the config's TARGET_DIR. (lib.sh also cd's as a backstop.)
TARGET_DIR="$(set -a; . "$CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
CONFIG_REPO="$(set -a; . "$CONFIG"; set +a; printf '%s' "${REPO:-}")"
[ -n "$TARGET_DIR" ] || echo "⚠ TARGET_DIR not set in $CONFIG — watchers would run from the harness dir (wrong repo). Set TARGET_DIR."

# The CLI identity includes the repository owner and preserves explicit
# multi-checkout names. Direct invocations use the same owner/repository shape.
_raw_name="${ISSUE_AGENT_DEPLOYMENT_ID:-}"
if [ -z "$_raw_name" ] && [ -n "$CONFIG_REPO" ]; then
  _raw_name="${CONFIG_REPO//\//-}"
fi
[ -n "$_raw_name" ] || _raw_name="$(basename "${TARGET_DIR:-target}")"
NAME="$(printf '%s' "$_raw_name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]/-/g; s/^-+//; s/-+$//')"
NAME="${NAME:-target}"

SNAP_DIR="$HOME/.config/issue-agent"
mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
SNAP_CONFIG="$SNAP_DIR/$NAME.env"
if [ -f "$SNAP_CONFIG" ]; then
  SNAP_REPO="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${REPO:-}")"
  SNAP_TARGET="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
  if [ "$SNAP_REPO" != "$CONFIG_REPO" ] || [ "$SNAP_TARGET" != "$TARGET_DIR" ]; then
    echo "REFUSING deployment '$NAME': existing snapshot belongs to REPO='$SNAP_REPO' TARGET_DIR='$SNAP_TARGET', requested REPO='$CONFIG_REPO' TARGET_DIR='$TARGET_DIR'" >&2
    exit 2
  fi
fi
STAGE="$(mktemp -d "$UD/.issue-agent-install.XXXXXX")"
cleanup() {
  rm -rf -- "$STAGE"
}
trap cleanup EXIT

for index in "${!services[@]}"; do
  s="${services[$index]}"
  src="$HARNESS_ROOT/ops/systemd/$s.service"
  candidate="$STAGE/$s-$NAME.service"
  sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" -e "s#@CONFIG@#$SNAP_CONFIG#g" \
      -e "s#@TARGET_DIR@#${TARGET_DIR:-/}#g" -e "s#@NAME@#$NAME#g" \
      -e "s#@UV_BIN@#$UV_BIN#g" "$src" > "$candidate"
  "$SYSTEMD_ANALYZE" --user verify "$candidate"
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "validated ${#services[@]} unit(s) for deployment '$NAME'"
  echo "executable: $UV_BIN"
  echo "config snapshot: $SNAP_CONFIG"
  exit 0
fi

if [ -f "$SNAP_CONFIG" ]; then
  cp "$SNAP_CONFIG" "$STAGE/config.previous"
fi

for index in "${!services[@]}"; do
  s="${services[$index]}"
  dst="$UD/$s-$NAME.service"
  [ ! -f "$dst" ] || cp "$dst" "$STAGE/$index.previous"
  systemctl --user is-active --quiet "$s-$NAME.service" \
    && touch "$STAGE/$index.active" || true
  systemctl --user is-enabled --quiet "$s-$NAME.service" \
    && touch "$STAGE/$index.enabled" || true
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
  for index in "${!services[@]}"; do
    s="${services[$index]}"
    dst="$UD/$s-$NAME.service"
    systemctl --user disable --now "$s-$NAME.service" >/dev/null 2>&1
    if [ -f "$STAGE/$index.previous" ]; then
      mv "$STAGE/$index.previous" "$dst"
    else
      rm -f -- "$dst"
    fi
  done
  systemctl --user daemon-reload >/dev/null 2>&1
  for index in "${!services[@]}"; do
    s="${services[$index]}"
    [ ! -f "$STAGE/$index.enabled" ] \
      || systemctl --user enable "$s-$NAME.service" >/dev/null 2>&1
    [ ! -f "$STAGE/$index.active" ] \
      || systemctl --user restart "$s-$NAME.service" >/dev/null 2>&1
  done
  echo "installation failed; restored previous known-good units" >&2
  exit "$status"
}
trap rollback ERR INT TERM

install -m 600 "$CONFIG" "$STAGE/config.next"
mv "$STAGE/config.next" "$SNAP_CONFIG"
for index in "${!services[@]}"; do
  s="${services[$index]}"
  mv "$STAGE/$s-$NAME.service" "$UD/$s-$NAME.service"
done
systemctl --user daemon-reload
# Stop the complete legacy writer set before any new role imports the shared
# spend ledger. Sequential restart would let the first new role admit against a
# snapshot while an old review/steward process could still append a late charge.
for s in "${services[@]}"; do
  systemctl --user stop "$s-$NAME.service"
done
for s in "${services[@]}"; do
  systemctl --user enable "$s-$NAME.service"
  systemctl --user restart "$s-$NAME.service"
  systemctl --user is-active --quiet "$s-$NAME.service"
  echo "✓ enabled+started $s-$NAME"
done
trap - ERR INT TERM
echo "Done. 'systemctl --user status issue-agent-*-$NAME' or"
echo "'journalctl --user -u issue-agent-fix-watcher-$NAME -f' to confirm."
