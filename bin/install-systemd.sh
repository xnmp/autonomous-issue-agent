#!/usr/bin/env bash
# Install the three per-repo role services and host-wide dashboard service
# (Linux). Mirrors install-launchd.sh for the role services.
#
#   bin/install-systemd.sh                # install + enable + start all roles,
#                                          # plus the singleton dashboard,
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
UD="$HOME/.config/systemd/user"
mkdir -p "$UD"

canonical_deployment_id() {
  normalized="$(printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9._-]/-/g; s/^-+//; s/-+$//')"
  printf '%s' "${normalized:-target}"
}

services=(issue-agent-fix-watcher issue-agent-review-watcher issue-agent-steward)
DASHBOARD_SERVICE=issue-agent-dashboard
DRY_RUN=0
[ "${1:-}" != "--dry-run" ] || DRY_RUN=1

if [ "${1:-}" = "uninstall" ]; then
  if [ -n "${2:-}" ]; then
    uninstall_name="$(canonical_deployment_id "$2")"
    pattern="issue-agent-*-$uninstall_name.service"
  else
    pattern="issue-agent-*.service"
  fi
  targets=()
  for f in "$UD"/$pattern; do
    [ -e "$f" ] || continue
    targets+=("$f")
  done
  if [ "${#targets[@]}" -eq 0 ]; then
    echo "no matching units found (${2:-all repos})"
    exit 0
  fi
  stop_failed=0
  for f in "${targets[@]}"; do
    s="$(basename "$f" .service)"
    if ! systemctl --user disable --now "$s.service"; then
      echo "REFUSING uninstall: failed to disable and stop $s.service; service definitions were preserved" >&2
      stop_failed=1
    fi
  done
  [ "$stop_failed" -eq 0 ] || exit 1
  for f in "${targets[@]}"; do
    rm -f -- "$f"
  done
  systemctl --user daemon-reload
  echo "removed ${#targets[@]} unit(s)${2:+ for '$2'}"
  exit 0
fi

CONFIG="${AGENT_ENV:?AGENT_ENV is unset or empty; use issue-agent install-service or source bin/compose-env.sh <name>}"
[ -f "$CONFIG" ] || { echo "config not found: $CONFIG (run issue-agent install-service or source bin/compose-env.sh <name>)"; exit 1; }
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
NAME="$(canonical_deployment_id "$_raw_name")"
DEPLOYMENT_NAME="${ISSUE_AGENT_DEPLOYMENT_ID:-$NAME}"
[ -z "${ISSUE_AGENT_DEPLOYMENT_ID:-}" ] \
  || [ "$ISSUE_AGENT_DEPLOYMENT_ID" = "$NAME" ] || {
  echo "REFUSING non-canonical deployment identity '$ISSUE_AGENT_DEPLOYMENT_ID'; use '$NAME'" >&2
  exit 2
}
if [ -n "${ISSUE_AGENT_DEPLOYMENT_ID:-}" ]; then
  for installed in "$UD"/issue-agent-*.service; do
    [ -e "$installed" ] || continue
    installed_identity="$(
      sed -n 's/^Environment=ISSUE_AGENT_DEPLOYMENT_ID=//p' "$installed" \
        | tail -n 1
    )"
    [ -n "$installed_identity" ] || continue
    [ "$(canonical_deployment_id "$installed_identity")" = "$NAME" ] \
      || continue
    [ "$installed_identity" = "$DEPLOYMENT_NAME" ] || {
      echo "REFUSING deployment identity alias '$DEPLOYMENT_NAME': $installed embeds '$installed_identity' but both map to '$NAME'" >&2
      exit 2
    }
  done
fi
_legacy_target_raw="$(basename "${TARGET_DIR:-target}")"
LEGACY_TARGET_NAME="$(printf '%s' "$_legacy_target_raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]/-/g; s/^-+//; s/-+$//')"
LEGACY_TARGET_NAME="${LEGACY_TARGET_NAME:-target}"
_legacy_repo_raw="${CONFIG_REPO//\//-}"
LEGACY_REPO_NAME="$(printf '%s' "$_legacy_repo_raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]/-/g; s/^-+//; s/-+$//')"

SNAP_DIR="$HOME/.config/issue-agent"
mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"
SNAP_CONFIG="$SNAP_DIR/$NAME.env"
if [ -f "$SNAP_CONFIG" ]; then
  SNAP_REPO="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${REPO:-}")"
  SNAP_TARGET="$(set -a; . "$SNAP_CONFIG"; set +a; printf '%s' "${TARGET_DIR:-}")"
  if [ "$SNAP_REPO" != "$CONFIG_REPO" ]; then
    echo "REFUSING deployment '$NAME': existing snapshot belongs to REPO='$SNAP_REPO' TARGET_DIR='$SNAP_TARGET', requested REPO='$CONFIG_REPO' TARGET_DIR='$TARGET_DIR'" >&2
    exit 2
  fi
fi
STAGE="$(mktemp -d "$UD/.issue-agent-install.XXXXXX")"
cleanup() {
  rm -rf -- "$STAGE"
}
trap cleanup EXIT

# The installer's PATH is baked into the units (@INSTALL_PATH@): the daemons
# must resolve the operator's toolchain (codex under nvm, bunx, rustup shims)
# that systemd's minimal default PATH cannot. Escaped for the sed RHS — PATH
# entries may legitimately contain '&', '\' or the '|' delimiter.
_install_path_escaped="$(printf '%s' "$PATH" | sed -e 's/[&|\\]/\\&/g')"
for index in "${!services[@]}"; do
  s="${services[$index]}"
  src="$HARNESS_ROOT/ops/systemd/$s.service"
  candidate="$STAGE/$s-$NAME.service"
  sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" -e "s#@CONFIG@#$SNAP_CONFIG#g" \
      -e "s#@TARGET_DIR@#${TARGET_DIR:-/}#g" -e "s#@NAME@#$NAME#g" \
      -e "s#@DEPLOYMENT_ID@#$DEPLOYMENT_NAME#g" \
      -e "s|@INSTALL_PATH@|$_install_path_escaped|g" \
      -e "s#@UV_BIN@#$UV_BIN#g" "$src" > "$candidate"
  "$SYSTEMD_ANALYZE" --user verify "$candidate"
done
dashboard_candidate="$STAGE/$DASHBOARD_SERVICE.service"
sed -e "s#@HARNESS_ROOT@#$HARNESS_ROOT#g" \
    -e "s|@INSTALL_PATH@|$_install_path_escaped|g" \
    -e "s#@UV_BIN@#$UV_BIN#g" \
    "$HARNESS_ROOT/ops/systemd/$DASHBOARD_SERVICE.service" \
    > "$dashboard_candidate"
"$SYSTEMD_ANALYZE" --user verify "$dashboard_candidate"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "validated $((${#services[@]} + 1)) unit(s) for deployment '$NAME'"
  echo "executable: $UV_BIN"
  echo "config snapshot: $SNAP_CONFIG"
  exit 0
fi

if [ -f "$SNAP_CONFIG" ]; then
  cp "$SNAP_CONFIG" "$STAGE/config.previous"
fi

legacy_units=()
legacy_unit_names=()
append_legacy_unit() {
  candidate="$1"
  current="$2"
  [ "$candidate" != "$current" ] || return 0
  if [ -f "$candidate" ]; then
    embedded_deployment="$(sed -n 's/^Environment=ISSUE_AGENT_DEPLOYMENT_ID=//p' "$candidate" | tail -n 1)"
    embedded_target="$(sed -n 's/^Environment=TARGET_DIR=//p' "$candidate" | tail -n 1)"
    if { [ -n "$embedded_deployment" ] \
         && [ "$embedded_deployment" != "$DEPLOYMENT_NAME" ]; } \
       || { [ -n "$embedded_target" ] \
            && [ "$embedded_target" != "$TARGET_DIR" ]; }; then
      return 0
    fi
  fi
  candidate_name="$(basename "$candidate")"
  for existing in "${legacy_unit_names[@]}"; do
    [ "$existing" != "$candidate_name" ] || return 0
  done
  legacy_unit_names+=("$candidate_name")
  [ ! -f "$candidate" ] || legacy_units+=("$candidate")
}

for index in "${!services[@]}"; do
  s="${services[$index]}"
  dst="$UD/$s-$NAME.service"
  [ ! -f "$dst" ] || cp "$dst" "$STAGE/$index.previous"
  current_active=0
  current_enabled=0
  systemctl --user is-active --quiet "$s-$NAME.service" \
    && current_active=1 || true
  systemctl --user is-enabled --quiet "$s-$NAME.service" \
    && current_enabled=1 || true
  if [ ! -f "$dst" ] \
     && { [ "$current_active" -eq 1 ] || [ "$current_enabled" -eq 1 ]; }; then
    echo "REFUSING cutover: active/enabled current unit $s-$NAME.service has no recoverable definition at $dst" >&2
    exit 2
  fi
  [ "$current_active" -ne 1 ] || touch "$STAGE/$index.active"
  [ "$current_enabled" -ne 1 ] || touch "$STAGE/$index.enabled"
  append_legacy_unit "$UD/$s.service" "$dst"
  append_legacy_unit "$UD/$s-$LEGACY_TARGET_NAME.service" "$dst"
  if [ -n "$LEGACY_REPO_NAME" ]; then
    append_legacy_unit "$UD/$s-$LEGACY_REPO_NAME.service" "$dst"
  fi
done
for unit in "${legacy_unit_names[@]}"; do
  legacy_path="$UD/$unit"
  legacy_active=0
  legacy_enabled=0
  systemctl --user is-active --quiet "$unit" \
    && legacy_active=1 || true
  systemctl --user is-enabled --quiet "$unit" \
    && legacy_enabled=1 || true
  if [ ! -f "$legacy_path" ] \
     && { [ "$legacy_active" -eq 1 ] || [ "$legacy_enabled" -eq 1 ]; }; then
    echo "REFUSING cutover: active/enabled legacy unit $unit has no recoverable definition at $legacy_path" >&2
    exit 2
  fi
done
for index in "${!legacy_units[@]}"; do
  legacy="${legacy_units[$index]}"
  unit="$(basename "$legacy")"
  cp "$legacy" "$STAGE/legacy.$index.previous"
  systemctl --user is-active --quiet "$unit" \
    && touch "$STAGE/legacy.$index.active" || true
  systemctl --user is-enabled --quiet "$unit" \
    && touch "$STAGE/legacy.$index.enabled" || true
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
  for index in "${!legacy_units[@]}"; do
    legacy="${legacy_units[$index]}"
    if [ -f "$STAGE/legacy.$index.previous" ]; then
      mv "$STAGE/legacy.$index.previous" "$legacy"
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
  for index in "${!legacy_units[@]}"; do
    legacy="${legacy_units[$index]}"
    unit="$(basename "$legacy")"
    [ ! -f "$STAGE/legacy.$index.enabled" ] \
      || systemctl --user enable "$unit" >/dev/null 2>&1
    [ ! -f "$STAGE/legacy.$index.active" ] \
      || systemctl --user restart "$unit" >/dev/null 2>&1
  done
  echo "installation failed; restored previous known-good units" >&2
  exit "$status"
}
trap rollback ERR INT TERM

stop_unit() {
  unit="$1"
  if ! systemctl --user stop "$unit"; then
    if systemctl --user is-active --quiet "$unit"; then
      echo "failed to stop active legacy writer $unit" >&2
      return 1
    fi
  fi
}

assert_running() {
  unit="$1"
  active_state="$(
    systemctl --user show "$unit" --property=ActiveState --value
  )"
  sub_state="$(
    systemctl --user show "$unit" --property=SubState --value
  )"
  if [ "$active_state" != "active" ] || [ "$sub_state" != "running" ]; then
    echo "activation check failed for $unit: ${active_state:-unknown} (${sub_state:-unknown})" >&2
    return 1
  fi
}

# Stop all currently named roles plus the unsuffixed, target-basename, and
# repository-derived predecessors before any replacement role may import the
# shared ledger. Every removed predecessor is restored by rollback above.
for s in "${services[@]}"; do
  stop_unit "$s-$NAME.service"
done
for unit in "${legacy_unit_names[@]}"; do
  stop_unit "$unit"
done
for legacy in "${legacy_units[@]}"; do
  unit="$(basename "$legacy")"
  systemctl --user disable "$unit" 2>/dev/null || true
  rm -f -- "$legacy"
done

install -m 600 "$CONFIG" "$STAGE/config.next"
mv "$STAGE/config.next" "$SNAP_CONFIG"
for index in "${!services[@]}"; do
  s="${services[$index]}"
  mv "$STAGE/$s-$NAME.service" "$UD/$s-$NAME.service"
done
systemctl --user daemon-reload
for s in "${services[@]}"; do
  systemctl --user enable "$s-$NAME.service"
  systemctl --user restart "$s-$NAME.service"
  systemctl --user is-active --quiet "$s-$NAME.service"
  assert_running "$s-$NAME.service"
  echo "✓ enabled+started $s-$NAME: active (running)"
done
trap - ERR INT TERM

dashboard_port_owner() {
  port="$1"
  # Injectable for tests: set to a command to delegate the probe; set EMPTY to
  # assert the port is free (the suite runs on hosts where a real dashboard is
  # legitimately listening — the probe must not read the host).
  if [ "${ISSUE_AGENT_DASHBOARD_PORT_CHECK+set}" = "set" ]; then
    if [ -n "$ISSUE_AGENT_DASHBOARD_PORT_CHECK" ]; then
      "$ISSUE_AGENT_DASHBOARD_PORT_CHECK" "$port"
      return
    fi
    return 1
  fi

  owner_pid=""
  if command -v ss >/dev/null 2>&1; then
    socket_line="$(ss -H -ltnp "sport = :$port" 2>/dev/null | head -n 1)"
    [ -n "$socket_line" ] || return 1
    owner_pid="$(printf '%s' "$socket_line" \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p')"
  elif command -v lsof >/dev/null 2>&1; then
    owner_pid="$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null \
      | head -n 1)"
    [ -n "$owner_pid" ] || return 1
  elif command -v fuser >/dev/null 2>&1; then
    owner_pid="$(fuser -n tcp "$port" 2>/dev/null | awk '{print $1}')"
    [ -n "$owner_pid" ] || return 1
  elif (: <> "/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    owner_pid="unknown"
  else
    return 1
  fi

  owner_command="unknown command"
  if [ "$owner_pid" != "unknown" ]; then
    owner_command="$(ps -p "$owner_pid" -o args= 2>/dev/null || true)"
    owner_command="${owner_command:-unknown command}"
  fi
  printf '%s\t%s\n' "$owner_pid" "$owner_command"
}

dashboard_owner=""
if dashboard_owner="$(dashboard_port_owner 8321)"; then
  dashboard_owner_pid="${dashboard_owner%%$'\t'*}"
  dashboard_owner_command="${dashboard_owner#*$'\t'}"
  dashboard_unit_pid=""
  if systemctl --user is-active --quiet "$DASHBOARD_SERVICE.service"; then
    dashboard_unit_pid="$(
      systemctl --user show "$DASHBOARD_SERVICE.service" \
        --property=MainPID --value
    )"
  fi
  if [ -z "$dashboard_unit_pid" ] \
     || [ "$dashboard_unit_pid" = "0" ] \
     || [ "$dashboard_owner_pid" != "$dashboard_unit_pid" ]; then
    echo "dashboard install failed: TCP 8321 is already bound by PID $dashboard_owner_pid ($dashboard_owner_command), not $DASHBOARD_SERVICE.service. Stop the manual \`issue-agent dashboard\` process and rerun this installer; role units remain installed." >&2
    exit 1
  fi
fi

dashboard_dst="$UD/$DASHBOARD_SERVICE.service"
[ ! -f "$dashboard_dst" ] || cp "$dashboard_dst" "$STAGE/dashboard.previous"
dashboard_active=0
dashboard_enabled=0
systemctl --user is-active --quiet "$DASHBOARD_SERVICE.service" \
  && dashboard_active=1 || true
systemctl --user is-enabled --quiet "$DASHBOARD_SERVICE.service" \
  && dashboard_enabled=1 || true
if [ ! -f "$dashboard_dst" ] \
   && { [ "$dashboard_active" -eq 1 ] || [ "$dashboard_enabled" -eq 1 ]; }; then
  echo "REFUSING dashboard cutover: active/enabled unit $DASHBOARD_SERVICE.service has no recoverable definition at $dashboard_dst; role units remain installed" >&2
  exit 2
fi
[ "$dashboard_active" -ne 1 ] || touch "$STAGE/dashboard.active"
[ "$dashboard_enabled" -ne 1 ] || touch "$STAGE/dashboard.enabled"

dashboard_rollback() {
  status=$?
  [ "$status" -ne 0 ] || status=1
  trap - ERR INT TERM
  set +e
  systemctl --user disable --now "$DASHBOARD_SERVICE.service" >/dev/null 2>&1
  if [ -f "$STAGE/dashboard.previous" ]; then
    mv "$STAGE/dashboard.previous" "$dashboard_dst"
  else
    rm -f -- "$dashboard_dst"
  fi
  systemctl --user daemon-reload >/dev/null 2>&1
  [ ! -f "$STAGE/dashboard.enabled" ] \
    || systemctl --user enable "$DASHBOARD_SERVICE.service" >/dev/null 2>&1
  [ ! -f "$STAGE/dashboard.active" ] \
    || systemctl --user restart "$DASHBOARD_SERVICE.service" >/dev/null 2>&1
  echo "dashboard installation failed; restored its previous unit; role units remain installed" >&2
  exit "$status"
}
trap dashboard_rollback ERR INT TERM

stop_unit "$DASHBOARD_SERVICE.service"
mv "$dashboard_candidate" "$dashboard_dst"
systemctl --user daemon-reload
systemctl --user enable "$DASHBOARD_SERVICE.service"
systemctl --user restart "$DASHBOARD_SERVICE.service"
systemctl --user is-active --quiet "$DASHBOARD_SERVICE.service"
assert_running "$DASHBOARD_SERVICE.service"
echo "✓ enabled+started $DASHBOARD_SERVICE: active (running)"
trap - ERR INT TERM
echo "Done. 'systemctl --user status issue-agent-*-$NAME' or"
echo "'systemctl --user status issue-agent-dashboard' to confirm."
