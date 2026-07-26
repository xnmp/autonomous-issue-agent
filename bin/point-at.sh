#!/usr/bin/env bash
# Point the harness at a repo — one command.
#
#   bin/point-at.sh <name>        # composes config/shared.env + config/repos/<name>.env
#   make point-at REPO=<name>
#
# Composes the env → preflight (doctor) → ensures the BASE_BRANCH exists on the
# target → ensures GitHub labels + local worktree-scratch ignores (NO repo file
# mutation) → runs all three watchers (fix, review, steward) in the FOREGROUND
# under one trap. Ctrl-C stops all three. Idempotent.
#
# Point-and-fire: nothing is installed into the target's tracked tree. The SDK
# daemons inject the governor hook + AGENT.md in-process; the only target-side
# effects are GitHub labels and a local .git/info/exclude.
#
# Env knobs:
#   ROLES="fix review steward"    which watchers to start (default: all three)
#   ISSUE_AGENT_MAX_TICKS=N       bound each daemon's loop (dry run / CI); unset = forever
#   LOG_DIR=<path>                 daemon log directory (default:
#                                  ${XDG_STATE_HOME:-$HOME/.local/state}/issue-agent/logs)
#
# NOTE: this foreground mode is the dev / first-run path. For real (unattended)
# operation, prefer the systemd units (`issue-agent install-service` /
# `make install-systemd`) — they auto-restart the process itself across a
# suspend/OOM-kill/reboot, which foreground mode (no supervisor) cannot. See
# HARNESS.md "Running for real: systemd".
set -uo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${1:?usage: bin/point-at.sh <name>  (reads config/repos/<name>.env)}"
SHARED="$HARNESS_ROOT/config/shared.env"
REPO_ENV="$HARNESS_ROOT/config/repos/$NAME.env"
ROLES="${ROLES:-fix review steward}"
# Private, persistent log dir — NOT world-readable /tmp (any local user could
# read prior /tmp/issue-agent-*.log, which may carry issue/PR content) and NOT
# lost on reboot (tmpfs / systemd-tmpfiles clears /tmp). XDG_STATE_HOME is the
# freedesktop convention for this kind of non-essential, host-local log data.
LOG_DIR="${LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/issue-agent/logs}"

red()  { printf '\033[31m%s\033[0m\n' "$1"; }
grn()  { printf '\033[32m%s\033[0m\n' "$1"; }
info() { printf '\033[36m▸ %s\033[0m\n' "$1"; }

[ -f "$SHARED" ]   || { red "missing $SHARED";   exit 1; }
[ -f "$REPO_ENV" ] || { red "missing $REPO_ENV (create config/repos/$NAME.env)"; exit 1; }

mkdir -p "$LOG_DIR" || { red "could not create log dir $LOG_DIR"; exit 1; }
chmod 700 "$(dirname "$LOG_DIR")" 2>/dev/null || true

# ── 1. Compose: shared first, per-repo overlays (per-repo wins) ───────────────
# Delegated to bin/compose-env.sh (the single source of truth for the compose
# step, shared with the `issue-agent` CLI). Sourced (not executed) so REPO /
# TARGET_DIR / BASE_BRANCH / ROLES-relevant vars land in THIS shell for the steps
# below; it also writes config/.active.env and exports AGENT_ENV to it.
info "composing env: shared.env + repos/$NAME.env → config/.active.env"
# shellcheck disable=SC1090
. "$HARNESS_ROOT/bin/compose-env.sh" "$NAME" || { red "env compose failed"; exit 1; }

: "${REPO:?REPO unset after composing config}"
grn "  ✓ REPO=$REPO  TARGET_DIR=${TARGET_DIR:-<none>}  BASE_BRANCH=${BASE_BRANCH:-dev}  ROLES=$ROLES"

# ── 2. Preflight ──────────────────────────────────────────────────────────────
info "doctor preflight"
if ! bash "$HARNESS_ROOT/bin/doctor.sh"; then
  red "doctor reported errors — fix them before starting the daemons."
  exit 1
fi

# ── 3. Ensure BASE_BRANCH exists on the target ────────────────────────────────
BASE_BRANCH="${BASE_BRANCH:-dev}"
# `[ -d .git ]` is WRONG for a linked worktree (its .git is a FILE, not a dir).
# Ask git instead, so TARGET_DIR may be a worktree (issue-agent start --worktree).
if [ -n "${TARGET_DIR:-}" ] && git -C "$TARGET_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  info "ensuring '$BASE_BRANCH' branch on $REPO"
  if git -C "$TARGET_DIR" ls-remote --exit-code --heads origin "$BASE_BRANCH" >/dev/null 2>&1; then
    grn "  ✓ '$BASE_BRANCH' already exists on origin"
  else
    default="$(git -C "$TARGET_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#origin/##')"
    default="${default:-main}"
    info "  creating '$BASE_BRANCH' off origin/$default"
    git -C "$TARGET_DIR" fetch origin "$default" --quiet \
      && git -C "$TARGET_DIR" branch "$BASE_BRANCH" "origin/$default" 2>/dev/null \
      && git -C "$TARGET_DIR" push -u origin "$BASE_BRANCH" \
      && grn "  ✓ created and pushed '$BASE_BRANCH'" \
      || { red "  ✗ could not create '$BASE_BRANCH' (check push access to $REPO)"; exit 1; }
  fi
else
  red "TARGET_DIR ('${TARGET_DIR:-}') is not a git working copy — cannot ensure the base branch."
  exit 1
fi

# ── 4. Point-and-fire prep: NO repo mutation ──────────────────────────────────
# The SDK daemons inject the governor hook + AGENT.md IN-PROCESS (governor.py /
# main.py:_agent_md) and run the fixer under bypassPermissions — so the target
# needs NO installed .claude/ plugin, settings.json, or AGENT.md. We touch only:
#   (a) GitHub labels — the lifecycle bus the watchers key off (not repo files).
#   (b) the target's LOCAL .git/info/exclude — so the fixer's per-issue worktree
#       scratch (.claude/worktrees/, .harness/, evidence non-images, .codex/,
#       AGENTS.md) never lands in a PR. This is local + untracked: the tracked
#       tree is left pristine.
# INVARIANTS.md / docs/adr are read by the steward IF the repo has them; the
# harness never creates them.

info "ensuring GitHub lifecycle labels on $REPO"
for l in ai-triage ai-ready ai-working ai-blocked ai-pr-open ai-done ai-suggestion \
         trivial minor major needs-human-review needs-design invariant \
         P1 P2 P3 harness; do
  gh label create "$l" --repo "$REPO" >/dev/null 2>&1 || true
done
grn "  ✓ labels ensured"

# Enable GitHub native auto-merge on the target so an approved+green PR merges
# itself (the harness's merge leg uses `gh pr merge --auto`, which the repo must
# permit — otherwise it loops "Auto merge is not allowed"). Default on; set
# ENSURE_AUTO_MERGE=0 to skip (e.g. a repo whose policy forbids it). Idempotent;
# needs admin on the repo — a non-admin failure is a warning, not fatal.
if [ "${ENSURE_AUTO_MERGE:-1}" != "0" ]; then
  info "ensuring GitHub auto-merge is enabled on $REPO"
  if gh api -X PATCH "repos/$REPO" -F allow_auto_merge=true >/dev/null 2>&1; then
    grn "  ✓ allow_auto_merge=true"
  else
    red "  ⚠ could not enable allow_auto_merge (need admin on $REPO?) — approved PRs"
    red "    won't auto-merge; enable it in repo Settings → General, or merge by hand."
  fi
fi

# Resolve info/exclude via git (NOT $TARGET_DIR/.git/… — a linked worktree's .git
# is a file, and excludes live in the SHARED primary .git anyway). One exclude
# file per repo, applied to every worktree.
EXCLUDE="$(git -C "$TARGET_DIR" rev-parse --git-path info/exclude 2>/dev/null)"
case "$EXCLUDE" in /*) : ;; *) EXCLUDE="$TARGET_DIR/$EXCLUDE" ;; esac  # abs (git may return relative)
info "adding worktree-scratch ignores to $EXCLUDE (local, untracked)"
mkdir -p "$(dirname "$EXCLUDE")"; [ -f "$EXCLUDE" ] || : > "$EXCLUDE"
add_exclude() { grep -qxF "$1" "$EXCLUDE" 2>/dev/null || echo "$1" >> "$EXCLUDE"; }
grep -qF '# issue-agent harness (local)' "$EXCLUDE" 2>/dev/null \
  || printf '\n# issue-agent harness (local) — worktree scratch, never committed\n' >> "$EXCLUDE"
add_exclude "${WORKTREE_ROOT:-.claude/worktrees}/"
add_exclude ".harness/"
add_exclude ".codex/"
add_exclude "AGENTS.md"
add_exclude "evidence/**"
add_exclude "!evidence/**/"
add_exclude "!evidence/**/*.png"
add_exclude "!evidence/**/*.jpg"
add_exclude "!evidence/**/*.jpeg"
add_exclude "!evidence/**/*.gif"
add_exclude "!evidence/**/*.webp"
grn "  ✓ .git/info/exclude updated (tracked tree untouched)"

# ── 4b. Network/proxy sanity ──────────────────────────────────────────────────
# The daemons inherit THIS shell's env for all outbound calls (gh + LLM gateway).
# Nothing proxy-related is hardcoded (the harness is general). But a common failure
# is launching from a shell with no proxy on a network that needs one: the daemons
# then loop on `error connecting to api.github.com`. Surface it up front — report
# whether a proxy is set, and whether the git host is actually reachable now.
if [ -n "${HTTPS_PROXY:-}${https_proxy:-}" ]; then
  info "proxy: HTTPS_PROXY set (daemons inherit it) — NO_PROXY=${NO_PROXY:-${no_proxy:-<unset>}}"
else
  info "proxy: no HTTPS_PROXY/https_proxy in this shell — daemons will connect directly"
fi
if gh api rate_limit >/dev/null 2>&1; then
  grn "  ✓ github API reachable from this environment"
else
  red "  ✗ github API NOT reachable from this shell (gh api rate_limit failed)."
  red "    The daemons inherit THIS env — they will fail the same way. If your network"
  red "    needs a proxy, export HTTPS_PROXY/HTTP_PROXY (+ NO_PROXY for the git host)"
  red "    and re-run. Continuing, but expect 'error connecting to api.github.com'."
fi

# ── 4c. Refuse to stack a second launcher for this repo ───────────────────────
# The Python daemons hold a per-(role,repo) flock and will refuse to start a
# duplicate anyway (the authoritative guard). But catch it HERE too, before the
# doctor/branch/label dance, so a double `point-at` fails fast with a clear
# message instead of racing to the flock. Match other point-at.sh processes for
# THIS repo name, excluding ourselves and our parent. Anchored with a trailing
# word boundary (end-of-string or space) — unanchored `pgrep -f` substring-
# matches, so NAME=app would also catch a launcher for NAME=myapp.
#
# Excluding $$/$PPID is NOT enough: `$(...)`, pipelines and `&&` lists all fork
# bash subshells that INHERIT our argv, so /proc shows them as extra
# "point-at.sh <name>" processes under fresh pids. Matching those made the guard
# fire on literally every first run (it reported its own command-substitution
# subshell, already reaped by the time the operator ran `ps`). A genuine second
# launcher is never parented by us, so drop any candidate whose PPID is $$ — and
# any that exited between pgrep and the check, since a dead process is not a
# running duplicate.
_others=""
for _p in $(pgrep -f "point-at\.sh ${NAME}([[:space:]]|\$)" 2>/dev/null || true); do
  if [ "$_p" = "$$" ] || [ "$_p" = "$PPID" ]; then
    continue
  fi
  _pp="$(ps -o ppid= -p "$_p" 2>/dev/null | tr -d '[:space:]' || true)"
  # empty ⇒ exited between pgrep and now (not a live duplicate);
  # $$      ⇒ a subshell we forked (inherits our argv), not a launcher.
  if [ -z "$_pp" ] || [ "$_pp" = "$$" ]; then
    continue
  fi
  _others="${_others:+$_others }$_p"
done
if [ -n "$_others" ]; then
  red "a point-at launcher for '$NAME' is already running (pids: $(echo $_others | tr '\n' ' '))."
  red "Refusing to start a second — duplicate daemons double-dispatch. Stop it first:"
  red "  kill $_others"
  exit 1
fi

# ── 5. Run the watchers in the foreground; Ctrl-C stops all ───────────────────
run_role() {
  local role="$1" log="$LOG_DIR/$1-$NAME.log"
  info "starting $role watcher → $log"
  # Run uv from the HARNESS root (that's where the issue_agent package + project
  # live). The daemon self-chdirs into TARGET_DIR (config.py os.chdir(target_dir)),
  # so git/worktree ops still land in the target repo.
  ( cd "$HARNESS_ROOT" && exec uv run --extra sdk --with tree-sitter --with tree-sitter-bash \
      python -m issue_agent.app.main "$role" ) >"$log" 2>&1 &
}

# Keep the machine awake while the watchers run — on a sleeping Mac the poll loop
# stalls and pickup latency balloons to "whenever it next wakes". `-w $$` ties the
# assertion to THIS process: when point-at exits (Ctrl-C, crash), caffeinate sees
# its wait-target gone and exits too, so there's no orphan holding the machine
# awake. macOS-only (Linux has no caffeinate); skipped silently elsewhere.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -w $$ &
  info "caffeinate: idle-sleep suppressed while watchers run (pid $!)"
fi

pids=()
trap 'echo; info "stopping watchers…"; kill 0 2>/dev/null; wait 2>/dev/null; exit 0' INT TERM

for role in $ROLES; do
  case "$role" in
    fix|review|steward) run_role "$role"; pids+=("$!") ;;
    *) red "unknown role '$role' (want: fix review steward)"; ;;
  esac
done

[ "${#pids[@]}" -gt 0 ] || { red "no watchers started"; exit 1; }
grn "✓ $ROLES watchers running against $REPO (Ctrl-C stops all). Tailing logs from $LOG_DIR:"
echo
# Follow all role logs together until the daemons exit or Ctrl-C.
tail -n +1 -F "$LOG_DIR"/*-"$NAME".log &
tail_pid=$!
wait "${pids[@]}"
kill "$tail_pid" 2>/dev/null || true
